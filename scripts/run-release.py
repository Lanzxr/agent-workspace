"""Load private runtime configuration and run or hot-reload the installed release role."""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from typing import Mapping

release = Path(__file__).resolve().parent
sys.path.insert(0, str(release / "company"))
from app.ports import assert_port_available, resolve_port_settings
from app.runtime_config import QQBotSettings, RuntimeConfig, WeComBotSettings

_WATCHED_KEYS: dict[str, tuple[str, ...]] = {
    "superagent": (
        "AGENT_MODEL_URL", "AGENT_MODEL_NAME", "AGENT_MODEL_API_KEY",
        "AGENT_MODEL_CONTEXT_WINDOW", "AGENT_MODEL_MAX_TOKENS", "AGENT_MODEL_REASONING_EFFORT",
        "DEEPSEEK_BASE_URL", "DEEPSEEK_API_KEY",
        "COMPANY_AGENT_DSSUPERAGENT_PORT", "COMPANY_AGENT_PORT_BASE",
        "COMPANY_AGENT_KNOWLEDGE_API_PORT", "COMPANY_AGENT_WEB_GATE_PORT",
    ),
    "qq": (
        "QQBOT_APP_ID", "QQBOT_APPID", "QQBOT_CLIENT_SECRET", "QQBOT_SECRET",
        "QQBOT_API_BASE_URL", "QQBOT_ALLOWED_PUBLICATIONS", "QQBOT_AUTO_EXECUTE_CAPABILITIES",
        "COMPANY_AGENT_QQ_WORKSPACE", "COMPANY_AGENT_QQ_ISOLATE_WORKSPACES",
        "COMPANY_AGENT_DSSUPERAGENT_PORT", "COMPANY_AGENT_PORT_BASE",
    ),
    "wecom": (
        "WECOM_BOT_ID", "COMPANY_AGENT_WECOM_BOT_ID", "WECOM_BOT_SECRET", "COMPANY_AGENT_WECOM_BOT_SECRET",
        "WECOM_WS_URL", "WECOM_ALLOWED_PUBLICATIONS", "WECOM_AUTO_EXECUTE_CAPABILITIES",
        "COMPANY_AGENT_WECOM_WORKSPACE", "COMPANY_AGENT_WECOM_ISOLATE_WORKSPACES",
        "WECOM_GROUP_DIRECT_ON_CALLBACK",
        "COMPANY_AGENT_DSSUPERAGENT_PORT", "COMPANY_AGENT_PORT_BASE",
    ),
}


def _write_private_file(target: Path, content: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target.parent / f".{target.name}.{os.getpid()}.tmp"
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
        try:
            os.chmod(tmp_path, 0o600)
        except OSError:
            pass
        os.replace(tmp_path, target)
    finally:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


def _build_superagent_patch(runtime_cfg: RuntimeConfig, host: str, port: int, state_dir: str | None = None) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = [
        {"id": "webserver", "config": {"host": host, "port": port}},
    ]
    if state_dir:
        entries.append({"id": "dsh-superagent", "config": {"stateDir": state_dir}})
    for item in runtime_cfg.provider_patch(web_enabled=False):
        if item.get("id") in {"agent-default-model", "llm-pi-ai", "llm-deepseek"}:
            entries.append(item)
    return entries


def _sync_superagent_patches(config_path: Path, data_dir: Path, dsh_home: Path, host: str, port: int) -> Path:
    runtime_cfg = RuntimeConfig.load(config_path)
    patch_path = data_dir / "superagent-port-patch.yml"
    _write_private_file(
        patch_path,
        json.dumps(_build_superagent_patch(runtime_cfg, host, port), ensure_ascii=False, indent=2) + "\n",
    )
    profile_dir = dsh_home / "profiles" / "superagent"
    profile_dir.mkdir(parents=True, exist_ok=True)
    default_state_dir = (dsh_home.parent / "superagent-state").resolve()
    default_state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    pkg_json = profile_dir / "package.json"
    if not pkg_json.is_file():
        pkg_json.write_text(
            json.dumps({
                "name": "dsh-profile-superagent",
                "private": True,
                "dependencies": {
                    "dsh-superagent": f"link:{(release / 'dss').as_posix()}",
                },
                "dsh": {
                    "profile": {
                        "bundles": ["@deepseek-ai/dsh-base", "@deepseek-ai/dsh-web-app", "dsh-superagent"],
                    },
                },
            }, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    cordis_yml = profile_dir / "cordis.yml"
    if not cordis_yml.is_file():
        cordis_yml.write_text("[]\n", encoding="utf-8")
    nm_dir = profile_dir / "node_modules"
    nm_dir.mkdir(parents=True, exist_ok=True)
    dss_link = nm_dir / "dsh-superagent"
    if not dss_link.exists() and not dss_link.is_symlink():
        dss_link.symlink_to(release / "dss", target_is_directory=True)
    profile_patch = profile_dir / "cordis.patch.yml"
    state_dir = str(default_state_dir)
    if profile_patch.is_file():
        for line in profile_patch.read_text(encoding="utf-8", errors="replace").splitlines():
            stripped = line.strip()
            if stripped.startswith("stateDir:") or '"stateDir"' in stripped:
                raw_val = stripped.split(":", 1)[1].strip().strip(",").strip('"').strip("'")
                if raw_val:
                    state_dir = raw_val
                    break
    _write_private_file(
        profile_patch,
        json.dumps(_build_superagent_patch(runtime_cfg, host, port, state_dir=state_dir), ensure_ascii=False, indent=2) + "\n",
    )
    return patch_path


def _watched_signature(values: Mapping[str, str], role_name: str) -> tuple[tuple[str, str], ...]:
    keys = _WATCHED_KEYS.get(role_name, ())
    return tuple((k, values.get(k, "")) for k in keys)


def _validate_role_config(config_path: Path, role_name: str) -> dict[str, str]:
    values = RuntimeConfig._parse_env(config_path)
    if role_name == "superagent":
        RuntimeConfig.load(config_path)
    elif role_name == "qq":
        QQBotSettings.load(config_path)
    elif role_name == "wecom":
        WeComBotSettings.load(config_path)
    return values


def _prepare_child_Invocation(role_name: str, config_path: Path, data_dir: Path, manifest: dict[str, object]) -> tuple[list[str], dict[str, str], Path]:
    values = _validate_role_config(config_path, role_name)
    ports = resolve_port_settings(values)
    env = dict(os.environ)
    env["COMPANY_AGENT_RELEASE_ID"] = str(manifest["release_id"])
    env.update({key: value for key, value in values.items() if key.startswith(("COMPANY_AGENT_", "AGENT_"))})
    if "AGENT_MODEL_API_KEY" in values:
        env["COMPANY_AGENT_MODEL_API_KEY"] = values["AGENT_MODEL_API_KEY"]
    elif "DEEPSEEK_API_KEY" in values:
        env["COMPANY_AGENT_MODEL_API_KEY"] = values["DEEPSEEK_API_KEY"]
    env["COMPANY_AGENT_KNOWLEDGE_API_URL"] = ports.knowledge_api_url
    env["COMPANY_AGENT_WEB_GATE_URL"] = ports.web_gate_url
    env["COMPANY_AGENT_WEB_SEARCH_URL"] = ports.web_search_url
    if role_name == "superagent":
        metadata = json.loads((release / "runtime.json").read_text(encoding="utf-8"))
        dsh_home = Path(metadata["dsh_home"])
        patch_path = _sync_superagent_patches(config_path, data_dir, dsh_home, ports.dssuperagent_host, ports.dssuperagent_port)
        env["DSH_HOME"] = metadata["dsh_home"]
        env["DSH_TELEMETRY_DISABLED"] = "1"
        env["PATH"] = str(Path(metadata["node"]).parent) + os.pathsep + env.get("PATH", os.defpath)
        cmd = [
            metadata["node"],
            metadata["dsh_bin"],
            "--profile",
            "superagent",
            "--patch",
            str(patch_path),
            "--no-open",
        ]
        return cmd, env, release / "dss"
    env["PYTHONPATH"] = str(release / "company")
    args = [sys.executable, "-m", "app", "--data-dir", str(data_dir)]
    if role_name == "admin":
        args += ["admin-serve", "--host", ports.admin_host, "--port", str(ports.admin_port), "--dssuperagent-url", ports.dssuperagent_url]
    elif role_name == "qq":
        args += ["qq-serve", "--dssuperagent-url", ports.dssuperagent_url]
    elif role_name == "wecom":
        args += ["wecom-serve", "--dssuperagent-url", ports.dssuperagent_url]
    else:
        raise SystemExit("role must be admin, qq, wecom, or superagent")
    args += ["--config", str(config_path)]
    return args, env, release


def _stop_child(proc: subprocess.Popen[bytes], *, timeout: float = 10.0) -> None:
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
    except OSError:
        return
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return
        time.sleep(0.1)
    try:
        proc.kill()
    except OSError:
        pass
    try:
        proc.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        pass


config_override = os.environ.get("COMPANY_AGENT_CONFIG_PATH", "").strip()
config = Path(config_override) if config_override else (Path.home() / ".config/company-agent/runtime.env")
data_override = os.environ.get("COMPANY_AGENT_DATA_DIR", "").strip()
data_dir = Path(data_override) if data_override else (Path.home() / ".local/share/company-agent")
manifest = json.loads((release / "manifest.json").read_text(encoding="utf-8"))
role = sys.argv[1] if len(sys.argv) > 1 else ""
if role not in {"admin", "qq", "wecom", "superagent"}:
    raise SystemExit("role must be admin, qq, wecom, or superagent")

if role == "admin":
    cmd, env, cwd = _prepare_child_Invocation(role, config, data_dir, manifest)
    os.environ.update(env)
    os.chdir(cwd)
    os.execv(cmd[0], cmd)

initial_values = _validate_role_config(config, role)
initial_ports = resolve_port_settings(initial_values)
if role == "superagent":
    assert_port_available(
        initial_ports.dssuperagent_host,
        initial_ports.dssuperagent_port,
        service_name="DSSuperAgent",
        env_var="COMPANY_AGENT_DSSUPERAGENT_PORT",
    )
active_sig = _watched_signature(initial_values, role)
cmd, env, cwd = _prepare_child_Invocation(role, config, data_dir, manifest)
child = subprocess.Popen(cmd, cwd=str(cwd), env=env)
shutting_down = False


def _handle_signal(signum: int, _frame: object) -> None:
    global shutting_down
    shutting_down = True
    _stop_child(child)
    raise SystemExit(0)


signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)

while True:
    rc = child.poll()
    if rc is not None:
        raise SystemExit(rc)
    time.sleep(2.0)
    if shutting_down or not config.is_file():
        continue
    try:
        latest_values = _validate_role_config(config, role)
    except Exception:
        continue
    latest_sig = _watched_signature(latest_values, role)
    if latest_sig == active_sig:
        continue
    print(f"company-agent: runtime.env change detected for role '{role}'; hot-reloading process", file=sys.stderr)
    _stop_child(child)
    time.sleep(0.5)
    try:
        cmd, env, cwd = _prepare_child_Invocation(role, config, data_dir, manifest)
    except Exception as error:
        print(f"company-agent: failed to reload role '{role}' ({type(error).__name__}: {error})", file=sys.stderr)
        raise SystemExit(1)
    active_sig = latest_sig
    child = subprocess.Popen(cmd, cwd=str(cwd), env=env)
