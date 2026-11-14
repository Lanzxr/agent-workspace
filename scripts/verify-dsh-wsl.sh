#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"
python_bin="${COMPANY_AGENT_PYTHON:-python3}"
data_root="${COMPANY_AGENT_ACCEPTANCE_ROOT:-$HOME/.local/share/company-agent/acceptance/$(date -u +%Y%m%dT%H%M%SZ)-$$}"
config="${COMPANY_AGENT_CONFIG:-$HOME/.config/company-agent/runtime.env}"
windows_config="${COMPANY_AGENT_WINDOWS_CONFIG:-${repo_root%/company-agent}/company-agent-runtime/runtime.env}"
config="$(realpath "$config")"
windows_config="$(realpath "$windows_config")"
[[ -f "$config" ]] || { echo "WSL runtime configuration is missing: $config" >&2; exit 1; }
[[ -f "$windows_config" ]] || { echo "Windows runtime configuration is missing: $windows_config" >&2; exit 1; }
export PYTHONPATH="$repo_root" COMPANY_AGENT_ACCEPTANCE_ROOT="$data_root" \
       COMPANY_AGENT_CONFIG="$config" COMPANY_AGENT_CONFIG_PATH="$config" \
       COMPANY_AGENT_WINDOWS_CONFIG_PATH="$windows_config"

"$python_bin" - <<'PY'
import os
import hashlib
import shlex
import socket
from pathlib import Path
from app.control_plane import ControlPlane
from app.models import Event, PlanStep
from app.workspace import WorkspaceManager

root = Path(os.environ['COMPANY_AGENT_ACCEPTANCE_ROOT'])
manager = WorkspaceManager(root / 'workspaces')
manager.provision('acceptance-agent')
manager.save_input('acceptance-agent', 'records.csv', 'category,amount\nA,10\nB,20\nA,7\nB,5\n')
host_network_namespace = os.stat('/proc/self/ns/net').st_ino
probe_source = '''import json
import os
import socket
import sys
from pathlib import Path

def can_read(path):
    try:
        with Path(path).open("rb") as stream:
            stream.read(1)
        return True
    except OSError:
        return False

def can_connect():
    client = socket.socket()
    client.settimeout(2)
    try:
        return client.connect_ex(("1.1.1.1", 53)) == 0
    finally:
        client.close()

result = {
    "config_file_hidden": not can_read(sys.argv[1]),
    "windows_mount_hidden": not can_read(sys.argv[2]),
    "private_data_hidden": not can_read(sys.argv[4]),
    "model_key_visible": "COMPANY_AGENT_MODEL_API_KEY" in os.environ,
    "docker_socket_hidden": not any(Path(path).exists() for path in ("/run/docker.sock", "/var/run/docker.sock")),
    "network_namespace_isolated": os.stat("/proc/self/ns/net").st_ino != int(sys.argv[3]),
    "network_egress_blocked": not can_connect(),
}
Path("artifacts/sandbox-access.json").write_text(json.dumps(result, sort_keys=True), encoding="utf-8")
if (not result["config_file_hidden"] or not result["windows_mount_hidden"] or not result["private_data_hidden"]
        or result["model_key_visible"]
        or not result["docker_socket_hidden"] or not result["network_namespace_isolated"]
        or not result["network_egress_blocked"]):
    print("COMPANY_AGENT_SANDBOX_PROBE_FAILED " + json.dumps(result, sort_keys=True))
    raise SystemExit(1)
print("COMPANY_AGENT_SANDBOX_PROBE_OK " + json.dumps(result, sort_keys=True))
'''
probe_path = manager.get('acceptance-agent').root / 'work/verify_sandbox.py'
probe_path.write_text(probe_source, encoding='utf-8')
(root / 'sandbox_probe.sha256').write_text(hashlib.sha256(probe_path.read_bytes()).hexdigest(), encoding='ascii')
plane = ControlPlane.open(root / 'control.sqlite')
plane.ingest_event(Event('event-1', 'local-dev', 'acceptance', 'local', 'operator', plane.now(),
                        'Verify a generic local data task', [], ['dsh.task']))
plane.create_work_item(work_item_id='work-1', title='Local execution acceptance',
                       objective='Use Python to compute verifiable totals from a local input',
                       source_event_ids=['event-1'], completion_conditions=['JSON totals and script exist'],
                       risk_class='low', owner_ref='operator')
plane.propose_work_item('work-1')
plane.accept_work_item('work-1')
plane.queue_work_item('work-1')
plane.create_plan(plan_id='plan-1', work_item_id='work-1',
                  steps=[PlanStep('step-1', 1, 'Process local input', capability='dsh.task')])
plane.create_execution_request(
    execution_id='acceptance-1', work_item_id='work-1', plan_step_id='step-1',
    workspace_id='acceptance-agent', capability='dsh.task', parameters={
        'prompt': (
            f"Use the bash tool only. First run python3 work/verify_sandbox.py "
            f"{shlex.quote(os.environ['COMPANY_AGENT_CONFIG_PATH'])} "
            f"{shlex.quote(os.environ['COMPANY_AGENT_WINDOWS_CONFIG_PATH'])} {host_network_namespace} "
            f"{shlex.quote(str(root / 'control.sqlite'))} "
            "using exactly those four arguments. Do not edit this pre-existing probe. It must print "
            "COMPANY_AGENT_SANDBOX_PROBE_OK and create "
            'artifacts/sandbox-access.json; stop if it fails. Then read work/records.csv and write a reusable '
            'Python standard-library script at tools/summarize.py '
            'and execute it with python3 using a shell tool. The script must read that CSV and create '
            'artifacts/totals.json with keys rows (row count), total (sum of amounts), '
            'and by_category (sum per category). Also copy your script to artifacts/summarize.py. '
            'In the script, assert that os.environ.get("COMPANY_AGENT_MODEL_API_KEY") is None and include '
            'model_key_visible: false in totals.json. '
            'Do the calculation by executing the script. Do not use network or other inputs.'
        ), 'expected_artifacts': ['totals.json', 'summarize.py', 'sandbox-access.json'], 'timeout_seconds': 240,
    })
plane.close()
PY

"$python_bin" -m app --data-dir "$data_root" execute acceptance-1 --config "$config"

# A new process checks stored results independently of the DSH answer.
"$python_bin" - <<'PY'
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from app.control_plane import ControlPlane
from app.dsh import DshAdapter
from app.execution import ExecutionDispatcher
from app.runtime_config import RuntimeConfig
from app.workspace import WorkspaceManager

root = Path(os.environ['COMPANY_AGENT_ACCEPTANCE_ROOT'])
manager = WorkspaceManager(root / 'workspaces')
workspace = manager.get('acceptance-agent')
plane = ControlPlane.open(root / 'control.sqlite')
result = plane.get_execution_result('acceptance-1')
assert result.status == 'succeeded', result.error
assert plane.get_work_item('work-1').status == 'completed'
assert plane.get_plan('plan-1').steps[0].status == 'completed'
expected = {'rows': 4, 'total': 42, 'by_category': {'A': 17, 'B': 25}, 'model_key_visible': False}
assert json.loads((workspace.root / 'artifacts/totals.json').read_text()) == expected
probe_expected = {'config_file_hidden': True, 'windows_mount_hidden': True, 'private_data_hidden': True,
                  'model_key_visible': False,
                  'docker_socket_hidden': True, 'network_namespace_isolated': True, 'network_egress_blocked': True}
assert json.loads((workspace.root / 'artifacts/sandbox-access.json').read_text()) == probe_expected
probe_script = workspace.root / 'work/verify_sandbox.py'
expected_probe_sha256 = (root / 'sandbox_probe.sha256').read_text(encoding='ascii')
assert hashlib.sha256(probe_script.read_bytes()).hexdigest() == expected_probe_sha256
trace = (workspace.root / result.evidence_refs[0]).read_text()
events = [json.loads(line) for line in trace.splitlines()]
tool_calls = [event for event in events if event['type'] == 'tool_call']
assert tool_calls, 'no real tool calls'
assert all(event.get('tool') == 'bash' for event in tool_calls), 'execution used a tool outside the isolated Bash runner'
assert any(event['type'] == 'tool_result' and event['status'] == 'completed' for event in events)
assert 'python3' in trace, 'no script execution evidence'
assert 'work/verify_sandbox.py' in trace, 'sandbox probe was not executed through DSH'
assert 'COMPANY_AGENT_SANDBOX_PROBE_OK' in trace, 'sandbox probe did not report blocked access'
assert os.environ['COMPANY_AGENT_CONFIG_PATH'] in trace
assert os.environ['COMPANY_AGENT_WINDOWS_CONFIG_PATH'] in trace
assert not any(event['type'] == 'thinking' for event in events)
assert result.metadata['session_id']
for entry in result.metadata['artifact_manifest']:
    assert hashlib.sha256((workspace.root / entry['path']).read_bytes()).hexdigest() == entry['sha256']
script = workspace.root / 'artifacts/summarize.py'
assert script.is_file()
completed = subprocess.run([sys.executable, str(script)], cwd=workspace.root,
                           capture_output=True, timeout=15)
assert completed.returncode == 0, completed.returncode
assert json.loads((workspace.root / 'artifacts/totals.json').read_text()) == expected
config_path = Path(os.environ.get('COMPANY_AGENT_CONFIG', str(Path.home() / '.config/company-agent/runtime.env')))
config = RuntimeConfig.load(config_path)
adapter = DshAdapter(config, manager, root / 'runtime')
repeated = ExecutionDispatcher(plane, manager, adapter, root / 'runtime').dispatch('acceptance-1')
assert repeated == result
assert (workspace.root / result.evidence_refs[0]).read_text() == trace
assert config.api_key not in trace + json.dumps(result.to_dict())
plane.close()
print('REAL_DSH_EXECUTION_AND_RESTART_OK')
print('BUBBLEWRAP_SHELL_ACCESS_BOUNDARY_OK')
print('EVIDENCE_ROOT=' + str(root))
PY
