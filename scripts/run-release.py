"""Load private runtime configuration and exec the installed release role."""
from __future__ import annotations
import json
import os
from pathlib import Path
import sys

release = Path(__file__).resolve().parent
sys.path.insert(0, str(release / "company"))
from app.runtime_config import RuntimeConfig
config = Path.home() / ".config/company-agent/runtime.env"
values = RuntimeConfig._parse_env(config)
manifest = json.loads((release / "manifest.json").read_text(encoding="utf-8"))
os.environ["COMPANY_AGENT_RELEASE_ID"] = manifest["release_id"]
os.environ.update({key: value for key, value in values.items() if key.startswith(("COMPANY_AGENT_", "AGENT_"))})
os.environ["COMPANY_AGENT_MODEL_API_KEY"] = values["AGENT_MODEL_API_KEY"]
role = sys.argv[1] if len(sys.argv) > 1 else ""
if role == "superagent":
    metadata = json.loads((release / "runtime.json").read_text())
    os.environ["DSH_HOME"] = metadata["dsh_home"]
    os.environ["DSH_TELEMETRY_DISABLED"] = "1"
    os.environ["PATH"] = str(Path(metadata["node"]).parent) + os.pathsep + os.environ.get("PATH", os.defpath)
    api_port = values.get("COMPANY_AGENT_KNOWLEDGE_API_PORT", "8766")
    os.environ["COMPANY_AGENT_KNOWLEDGE_API_URL"] = f"http://127.0.0.1:{api_port}/internal/knowledge/search"
    os.chdir(release / "dss")
    os.execv(metadata["node"], [metadata["node"], metadata["dsh_bin"], "--profile", "superagent", "--no-open"])
if role not in {"admin", "qq"}:
    raise SystemExit("role must be admin, qq, or superagent")
args = [sys.executable, "-m", "app", "--data-dir", str(Path.home() / ".local/share/company-agent")]
args += ["admin-serve"] if role == "admin" else ["qq-serve", "--dssuperagent-url", "http://127.0.0.1:3082"]
args += ["--config", str(config)]
os.environ["PYTHONPATH"] = str(release / "company")
os.chdir(release)
os.execv(sys.executable, args)
