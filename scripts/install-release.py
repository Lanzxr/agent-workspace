"""Install a tested project release in WSL while preserving user config and state."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import shutil
import signal
import stat
import sqlite3
import subprocess
import sys
import tarfile
import zipfile
import time
from datetime import datetime, timezone

try:
    import grp
    import pwd
except ImportError:  # Windows unit tests import pure helpers cleanly.
    grp = None  # type: ignore[assignment]
    pwd = None  # type: ignore[assignment]

SYSTEMD = Path("/etc/systemd/system")
EGRESS_SCRIPT = Path("/usr/local/sbin/company-agent-anythingllm-egress")
_DEFAULT_LEGACY_USER = "lan_z"
_UNIX_USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")


def resolve_deploy_target(
    *,
    user: str | None = None,
    group: str | None = None,
    home: str | None = None,
    deploy_dir: str | None = None,
    data_dir: str | None = None,
    config_path: str | None = None,
    env: dict[str, str] | None = None,
    passwd_lookup=None,
    group_lookup=None,
    legacy_config_exists: bool | None = None,
) -> dict[str, object]:
    """Resolve target Linux deployment user, group, and directory layout cleanly."""
    environ = os.environ if env is None else env
    pw_fn = passwd_lookup or (pwd.getpwnam if pwd is not None else None)
    gr_fn = group_lookup or (grp.getgrgid if grp is not None else None)
    if pw_fn is None or gr_fn is None:
        raise RuntimeError("POSIX pwd/grp modules are required on Linux hosts")

    chosen_user = (user or environ.get("COMPANY_AGENT_DEPLOY_USER") or "").strip()
    if not chosen_user:
        sudo_user = (environ.get("SUDO_USER") or "").strip()
        if sudo_user and sudo_user != "root":
            chosen_user = sudo_user
        else:
            if legacy_config_exists is None:
                legacy_config_exists = Path(f"/home/{_DEFAULT_LEGACY_USER}/.config/company-agent/runtime.env").is_file()
            if legacy_config_exists:
                chosen_user = _DEFAULT_LEGACY_USER
            else:
                raise ValueError("pass --user <linux-username> (or set COMPANY_AGENT_DEPLOY_USER) for this host")

    if chosen_user == "root" or not _UNIX_USER_RE.fullmatch(chosen_user):
        raise ValueError(f"invalid or disallowed deployment user {chosen_user!r}; must be a non-root Linux account")

    pw = pw_fn(chosen_user)
    resolved_group = (group or "").strip() or gr_fn(pw.pw_gid).gr_name

    def _norm_posix(raw: str) -> PurePosixPath:
        cleaned = posixpath.normpath(raw.strip())
        p = PurePosixPath(cleaned)
        if not p.is_absolute():
            raise ValueError(f"path must be absolute Linux POSIX path: {raw!r}")
        return p

    resolved_home = _norm_posix(home or pw.pw_dir)
    resolved_deploy = _norm_posix(deploy_dir or str(resolved_home / "company-agent-deploy"))
    resolved_data = _norm_posix(data_dir or str(resolved_home / ".local/share/company-agent"))
    resolved_config = _norm_posix(config_path or str(resolved_home / ".config/company-agent/runtime.env"))

    for label, path in (("deploy_dir", resolved_deploy), ("data_dir", resolved_data), ("config", resolved_config)):
        if any(path == bad or bad in path.parents for bad in (PurePosixPath("/tmp"), PurePosixPath("/mnt"), PurePosixPath("/run"))):
            raise ValueError(f"{label} cannot reside under /tmp, /mnt, or /run: {path}")

    return {
        "user": chosen_user,
        "group": resolved_group,
        "home": resolved_home,
        "deploy": resolved_deploy,
        "data": resolved_data,
        "config": resolved_config,
        "legacy_state": resolved_home / "company-agent-step0/superagent-state",
        "legacy_code": resolved_home / "company-agent-step0/dsh-superagent",
    }


def render_systemd_unit(
    template_text: str,
    *,
    user: str,
    group: str,
    home: Path | PurePosixPath,
    deploy: Path | PurePosixPath,
    data: Path | PurePosixPath,
    config: Path | PurePosixPath | None = None,
) -> str:
    """Substitute deployment account and directory paths into a systemd unit template."""
    home_s = home.as_posix()
    deploy_s = deploy.as_posix()
    data_s = data.as_posix()
    rendered = template_text.replace("\r\n", "\n")
    rendered = rendered.replace("User=lan_z", f"User={user}")
    rendered = rendered.replace("Group=lan_z", f"Group={group}")
    rendered = rendered.replace("Environment=HOME=/home/lan_z", f"Environment=HOME={home_s}")
    rendered = rendered.replace("/home/lan_z/company-agent-deploy", deploy_s)
    rendered = rendered.replace("/home/lan_z/.local/share/company-agent", data_s)
    rendered = rendered.replace("/home/lan_z", home_s)
    extra_env = ""
    if data_s != f"{home_s}/.local/share/company-agent":
        extra_env += f"Environment=COMPANY_AGENT_DATA_DIR={data_s}\n"
    if config is not None and config.as_posix() != f"{home_s}/.config/company-agent/runtime.env":
        extra_env += f"Environment=COMPANY_AGENT_CONFIG_PATH={config.as_posix()}\n"
    if extra_env:
        rendered = rendered.replace(f"Environment=HOME={home_s}\n", f"Environment=HOME={home_s}\n{extra_env}")
    return rendered


def run(*args: str, check: bool = True, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    result = subprocess.run(args, text=True, capture_output=True, env=env)
    if check and result.returncode:
        raise RuntimeError(f"command failed ({result.returncode}): {' '.join(args)}\n{result.stderr[-2000:]}")
    return result


def read_env(path: Path) -> dict[str, str]:
    values = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key, value = stripped.split("=", 1)
            values[key.strip()] = value
    return values


def validate_runtime_config(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise RuntimeError(f"private runtime config is missing: {path}")
    values = read_env(path)
    if not values.get("COMPANY_AGENT_ADMIN_TOKEN"):
        raise RuntimeError("admin token is missing from the private runtime config")
    if os.name != "nt" and stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise RuntimeError("private runtime config must not be group/world-readable")
    return values


def snapshot_sqlite(source: Path, destination: Path) -> None:
    if not source.exists():
        return
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=30)
    dst = sqlite3.connect(destination)
    try:
        src.backup(dst, pages=256, sleep=0.05)
    finally:
        dst.close()
        src.close()
    os.chmod(destination, 0o600)


def stop_legacy(data_dir: Path) -> None:
    candidates = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            pid = int(entry.name)
            cmd = (entry / "cmdline").read_bytes().decode(errors="replace").replace("\0", " ")
            cwd = os.readlink(entry / "cwd")
        except (OSError, PermissionError):
            continue
        company_data = str(data_dir)
        if (("/tmp/company-agent-run-bin/dsh --profile superagent" in cmd)
                or (" -m app " in f" {cmd} " and company_data in cmd
                    and ("qq-serve" in cmd or "wecom-serve" in cmd or "admin-serve" in cmd))
                or ("node " in cmd and "/dsh-superagent/" in cwd)):
            candidates[pid] = cmd[:240]
    for pid in candidates:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 20
    while candidates and time.monotonic() < deadline:
        candidates = {pid: cmd for pid, cmd in candidates.items() if Path(f"/proc/{pid}").exists()}
        time.sleep(0.25)
    for pid in candidates:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def safe_extract(archive: Path, release: Path) -> None:
    release.mkdir(parents=True, exist_ok=False)
    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        for member in members:
            target = (release / member.name).resolve()
            if release.resolve() not in target.parents and target != release.resolve():
                raise RuntimeError("release archive contains an unsafe path")
        tar.extractall(release, members=members, filter="data")
    manifest = json.loads((release / "manifest.json").read_text(encoding="utf-8"))
    for name, expected in manifest["artifacts"].items():
        path = release / name
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != expected:
            raise RuntimeError(f"release artifact hash mismatch: {name}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Install a built Company Agent release onto WSL2 or Linux servers.")
    parser.add_argument("archive", type=Path, help="Path to built <release>.tar.gz archive")
    parser.add_argument("--user", default=None, help="Target non-root Linux service user (default: COMPANY_AGENT_DEPLOY_USER / SUDO_USER / lan_z)")
    parser.add_argument("--group", default=None, help="Target Linux group (default: primary group of --user)")
    parser.add_argument("--home", default=None, help="Override target HOME directory (default: passwd home of --user)")
    parser.add_argument("--deploy-dir", default=None, help="Override release deploy root (default: <home>/company-agent-deploy)")
    parser.add_argument("--data-dir", default=None, help="Override private data directory (default: <home>/.local/share/company-agent)")
    parser.add_argument("--config", default=None, help="Override runtime.env path (default: <home>/.config/company-agent/runtime.env)")
    parser.add_argument("--skip-docker", action="store_true", help="Skip SearXNG docker compose and AnythingLLM egress firewall setup")
    args = parser.parse_args(argv)
    if os.geteuid() != 0:
        raise SystemExit("run this deployment installer as Linux root (e.g. sudo python3 install-release.py ...)")

    target = resolve_deploy_target(
        user=args.user,
        group=args.group,
        home=args.home,
        deploy_dir=args.deploy_dir,
        data_dir=args.data_dir,
        config_path=args.config,
    )
    deploy_user = str(target["user"])
    deploy_group = str(target["group"])
    HOME = Path(str(target["home"]))
    DEPLOY = Path(str(target["deploy"]))
    DATA = Path(str(target["data"]))
    CONFIG = Path(str(target["config"]))
    STATE = Path(str(target["legacy_state"]))
    OLD_CODE = Path(str(target["legacy_code"]))

    archive = args.archive.expanduser().resolve()
    if not archive.is_file():
        raise SystemExit(f"release archive was not found: {archive}")
    runtime_values = validate_runtime_config(CONFIG)
    DATA.mkdir(mode=0o700, parents=True, exist_ok=True)
    shutil.chown(DATA, user=deploy_user, group=deploy_group)
    release_id = archive.name.removesuffix(".tar.gz")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = DEPLOY / "backups" / stamp
    backup.mkdir(parents=True, exist_ok=True)
    shutil.copy2(CONFIG, backup / "runtime.env.before")
    shutil.chown(DEPLOY, user=deploy_user, group=deploy_group)
    shutil.chown(DEPLOY / "backups", user=deploy_user, group=deploy_group)
    shutil.chown(backup, user=deploy_user, group=deploy_group)
    for path in backup.iterdir():
        shutil.chown(path, user=deploy_user, group=deploy_group)
        os.chmod(path, 0o600)

    # Extract and validate the whole immutable release before stopping any service.
    releases = DEPLOY / "releases"
    releases.mkdir(parents=True, exist_ok=True)
    shutil.chown(releases, user=deploy_user, group=deploy_group)
    release = releases / release_id
    safe_extract(archive, release)
    wheel = next((release / "company").glob("*.whl"))
    package_root = release / "company"
    with zipfile.ZipFile(wheel) as package:
        for member in package.infolist():
            target_path = (package_root / member.filename).resolve()
            if package_root.resolve() not in target_path.parents and target_path != package_root.resolve():
                raise RuntimeError("Company Agent wheel contains an unsafe path")
        package.extractall(package_root)
    metadata = json.loads((release / "runtime.json").read_text(encoding="utf-8"))
    node = Path(metadata["node"]).resolve(strict=True)
    dsh_bin = Path(metadata["dsh_bin"]).resolve(strict=True)
    dsh_modules = Path(metadata["dsh_node_modules"]).resolve(strict=True)
    dsh_home = Path(metadata["dsh_home"]).resolve(strict=True)
    if not node.is_file() or not dsh_bin.is_file() or not dsh_home.is_dir() or dsh_modules.name != "node_modules":
        raise RuntimeError("release runtime paths are incomplete or invalid")
    if str(dsh_bin).startswith("/tmp/"):
        raise RuntimeError("release DSH entrypoint must use its stable installation path")
    if not (release / "run-release.py").is_file() or not (package_root / "web/index.html").is_file():
        raise RuntimeError("release is missing the Company Agent launcher or console")

    sys.path.insert(0, str(package_root))
    from app.ports import assert_port_available, resolve_port_settings  # noqa: PLC0415

    ports = resolve_port_settings(runtime_values)

    for unit in [
        "company-agent.target",
        "company-agent-qq.service",
        "company-agent-wecom.service",
        "company-agent-superagent.service",
        "company-agent@admin.service",
    ]:
        run("systemctl", "stop", unit, check=False)
    stop_legacy(DATA)

    assert_port_available(ports.admin_host, ports.admin_port, service_name="Admin Console", env_var="COMPANY_AGENT_ADMIN_PORT")
    assert_port_available(ports.web_gate_host, ports.web_gate_port, service_name="Web Gate", env_var="COMPANY_AGENT_WEB_GATE_PORT")
    assert_port_available(ports.dssuperagent_host, ports.dssuperagent_port, service_name="DSSuperAgent", env_var="COMPANY_AGENT_DSSUPERAGENT_PORT")
    if (runtime_values.get("COMPANY_AGENT_FORGE_MCP_ENABLED") or "true").strip().lower() in {"1", "true", "yes", "on"}:
        assert_port_available(ports.forge_mcp_host, ports.forge_mcp_port, service_name="Tool Forge MCP", env_var="COMPANY_AGENT_FORGE_MCP_PORT")
    if (runtime_values.get("ANYTHINGLLM_API_KEY") or "").strip():
        assert_port_available(ports.knowledge_api_host, ports.knowledge_api_port, service_name="Knowledge API", env_var="COMPANY_AGENT_KNOWLEDGE_API_PORT")

    snapshot_sqlite(DATA / "control.sqlite", backup / "company-agent.sqlite")
    snapshot_sqlite(STATE / "orchestrator.sqlite", backup / "dssuperagent.sqlite")

    dss_node_modules = release / "dss/node_modules"
    dss_node_modules.symlink_to(dsh_modules, target_is_directory=True)
    run("chown", "-R", f"{deploy_user}:{deploy_group}", str(release))

    current = DEPLOY / "current"
    temporary_link = DEPLOY / "current.next"
    if temporary_link.exists() or temporary_link.is_symlink():
        temporary_link.unlink()
    temporary_link.symlink_to(release, target_is_directory=True)
    if current.exists() and not current.is_symlink():
        raise RuntimeError("deployment current path exists and is not a symlink")
    os.replace(temporary_link, current)

    if OLD_CODE.exists() and not OLD_CODE.is_symlink():
        if OLD_CODE.resolve() != (HOME / "company-agent-step0/dsh-superagent").resolve():
            raise RuntimeError("refusing to remove unexpected legacy directory")
        for entry in Path("/proc").iterdir():
            if entry.name.isdigit():
                try:
                    cwd = os.readlink(entry / "cwd")
                except OSError:
                    continue
                if cwd == str(OLD_CODE) or cwd.startswith(str(OLD_CODE) + "/"):
                    raise RuntimeError("legacy code directory is still in use")
        artifacts = OLD_CODE / ".artifacts"
        if artifacts.exists():
            archived = DATA / "legacy-dsh-artifacts" / stamp
            archived.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(artifacts), str(archived))
        shutil.rmtree(OLD_CODE)
    elif OLD_CODE.is_symlink():
        OLD_CODE.unlink()
    if OLD_CODE.parent.exists():
        OLD_CODE.symlink_to(f"{DEPLOY}/current/dss", target_is_directory=True)

    for name in [
        "company-agent@.service",
        "company-agent-qq.service",
        "company-agent-wecom.service",
        "company-agent-superagent.service",
        "company-agent.target",
    ]:
        src_unit = release / "systemd" / name
        if not src_unit.is_file():
            continue
        unit_text = src_unit.read_text(encoding="utf-8")
        rendered_unit = render_systemd_unit(
            unit_text,
            user=deploy_user,
            group=deploy_group,
            home=HOME,
            deploy=DEPLOY,
            data=DATA,
            config=CONFIG,
        )
        (SYSTEMD / name).write_text(rendered_unit, encoding="utf-8")

    if not args.skip_docker:
        shutil.copy2(release / "anythingllm/apply-egress-firewall.sh", EGRESS_SCRIPT)
        os.chmod(EGRESS_SCRIPT, 0o755)
        shutil.copy2(release / "anythingllm/anythingllm-egress-firewall.service", SYSTEMD / "anythingllm-egress-firewall.service")
        run("systemctl", "daemon-reload")
        search_env_path = HOME / ".config/company-agent/search.env"
        search_env_vals = read_env(search_env_path) if search_env_path.is_file() else {}
        search_port = search_env_vals.get("COMPANY_AGENT_SEARCH_PORT") or str(ports.search_port)
        compose_env = {**os.environ, "COMPANY_AGENT_SEARCH_PORT": search_port}
        compose = ["docker", "compose", "--project-name", "company-agent-search", "--env-file",
                   str(search_env_path), "-f", str(current / "search/compose.yaml")]
        run(*compose, "up", "-d", env=compose_env)
        run("systemctl", "daemon-reload")
        run("systemctl", "enable", "anythingllm-egress-firewall.service")
        run("systemctl", "restart", "anythingllm-egress-firewall.service")
    else:
        run("systemctl", "daemon-reload")

    qq_configured = bool(
        (runtime_values.get("QQBOT_APP_ID") or runtime_values.get("QQBOT_APPID") or "").strip()
        and (runtime_values.get("QQBOT_CLIENT_SECRET") or runtime_values.get("QQBOT_SECRET") or "").strip()
    )
    wecom_configured = bool(
        (runtime_values.get("WECOM_BOT_ID") or runtime_values.get("COMPANY_AGENT_WECOM_BOT_ID") or "").strip()
        and (runtime_values.get("WECOM_BOT_SECRET") or runtime_values.get("COMPANY_AGENT_WECOM_BOT_SECRET") or "").strip()
    )
    if wecom_configured:
        venv_python = DATA / "venv/bin/python"
        if venv_python.is_file():
            dep_check = run(str(venv_python), "-c", "import cryptography", check=False)
            if dep_check.returncode != 0:
                raise RuntimeError(
                    "WECOM_BOT_ID is configured, but 'cryptography' is missing from runtime venv; "
                    f"install it first via: {venv_python} -m pip install 'cryptography>=42,<52'"
                )

    if qq_configured:
        run("systemctl", "enable", "company-agent-qq.service", check=False)
    else:
        run("systemctl", "disable", "--now", "company-agent-qq.service", check=False)
        run("systemctl", "reset-failed", "company-agent-qq.service", check=False)
    if (SYSTEMD / "company-agent-wecom.service").is_file():
        if wecom_configured:
            run("systemctl", "enable", "company-agent-wecom.service", check=False)
        else:
            run("systemctl", "disable", "--now", "company-agent-wecom.service", check=False)
            run("systemctl", "reset-failed", "company-agent-wecom.service", check=False)
    run("systemctl", "enable", "company-agent.target")
    run("systemctl", "start", "company-agent.target")
    if qq_configured:
        run("systemctl", "start", "company-agent-qq.service")
    if wecom_configured and (SYSTEMD / "company-agent-wecom.service").is_file():
        run("systemctl", "start", "company-agent-wecom.service")
    print(json.dumps({
        "user": deploy_user,
        "release": str(release),
        "backup": str(backup),
        "current": str(current.resolve()),
        "channels": {
            "qq": qq_configured,
            "wecom": wecom_configured,
        },
        "ports": {
            "admin": ports.admin_port,
            "knowledge": ports.knowledge_api_port,
            "web_gate": ports.web_gate_port,
            "forge_mcp": ports.forge_mcp_port,
            "dssuperagent": ports.dssuperagent_port,
            "search": ports.search_port,
            "anythingllm": ports.anythingllm_port,
        },
    }))


if __name__ == "__main__":
    main()
