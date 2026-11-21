"""Load private runtime configuration and exec the installed release role."""
from __future__ import annotations
import json
import os
from pathlib import Path
import sys

release = Path(__file__).resolve().parent
sys.path.insert(0, str(release / "company"))
from app.ports import assert_port_available, resolve_port_settings
from app.runtime_config import RuntimeConfig
config_override = os.environ.get("COMPANY_AGENT_CONFIG_PATH", "").strip()
config = Path(config_override) if config_override else (Path.home() / ".config/company-agent/runtime.env")
data_override = os.environ.get("COMPANY_AGENT_DATA_DIR", "").strip()
data_dir = Path(data_override) if data_override else (Path.home() / ".local/share/company-agent")
values = RuntimeConfig._parse_env(config)
ports = resolve_port_settings(values)
manifest = json.loads((release / "manifest.json").read_text(encoding="utf-8"))
os.environ["COMPANY_AGENT_RELEASE_ID"] = manifest["release_id"]
os.environ.update({key: value for key, value in values.items() if key.startswith(("COMPANY_AGENT_", "AGENT_"))})
os.environ["COMPANY_AGENT_MODEL_API_KEY"] = values["AGENT_MODEL_API_KEY"]
os.environ.setdefault("COMPANY_AGENT_KNOWLEDGE_API_URL", ports.knowledge_api_url)
os.environ.setdefault("COMPANY_AGENT_WEB_GATE_URL", ports.web_gate_url)
os.environ.setdefault("COMPANY_AGENT_WEB_SEARCH_URL", ports.web_search_url)
role = sys.argv[1] if len(sys.argv) > 1 else ""
if role == "superagent":
    assert_port_available(
        ports.dssuperagent_host,
        ports.dssuperagent_port,
        service_name="DSSuperAgent",
        env_var="COMPANY_AGENT_DSSUPERAGENT_PORT",
    )
    data_dir.mkdir(parents=True, exist_ok=True)
    patch_path = data_dir / "superagent-port-patch.yml"
    tmp_path = data_dir / f".superagent-port-patch-{os.getpid()}.yml.tmp"
    patch_body = (
        "- id: webserver\n"
        "  config:\n"
        f"    host: {json.dumps(ports.dssuperagent_host)}\n"
        f"    port: {ports.dssuperagent_port}\n"
    )
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(patch_body)
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, patch_path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
    metadata = json.loads((release / "runtime.json").read_text())
    os.environ["DSH_HOME"] = metadata["dsh_home"]
    os.environ["DSH_TELEMETRY_DISABLED"] = "1"
    os.environ["PATH"] = str(Path(metadata["node"]).parent) + os.pathsep + os.environ.get("PATH", os.defpath)
    os.environ["COMPANY_AGENT_KNOWLEDGE_API_URL"] = ports.knowledge_api_url
    os.chdir(release / "dss")
    os.execv(
        metadata["node"],
        [
            metadata["node"],
            metadata["dsh_bin"],
            "--profile",
            "superagent",
            "--patch",
            str(patch_path),
            "--no-open",
        ],
    )
if role not in {"admin", "qq"}:
    raise SystemExit("role must be admin, qq, or superagent")
args = [sys.executable, "-m", "app", "--data-dir", str(data_dir)]
args += (
    ["admin-serve", "--host", ports.admin_host, "--port", str(ports.admin_port), "--dssuperagent-url", ports.dssuperagent_url]
    if role == "admin"
    else ["qq-serve", "--dssuperagent-url", ports.dssuperagent_url]
)
args += ["--config", str(config)]
os.environ["PYTHONPATH"] = str(release / "company")
os.chdir(release)
os.execv(sys.executable, args)
