"""Contract fixture only. Real acceptance uses the installed official DSH CLI."""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path


if "--version" in sys.argv:
    print("0.2.2-rc.1")
    raise SystemExit(0)

task = json.loads(sys.stdin.read())
scenario = task["prompt"]
counter = Path("work/launch-count.txt")
counter.write_text(counter.read_text() + "run\n" if counter.exists() else "run\n")
if scenario == "timeout":
    time.sleep(10)
if scenario == "bad-protocol":
    print("this is not NDJSON")
    raise SystemExit(0)
print(json.dumps({"type": "session", "sessionId": "fixture-session", "cwd": os.getcwd()}))
print(json.dumps({"type": "thinking", "text": "PRIVATE_REASONING_MUST_NOT_BE_EXPORTED"}))
print(json.dumps({"type": "tool_call", "callId": "call-1", "tool": "bash", "input": {"command": "python3 tools/task.py"}}))
if scenario not in {"missing-artifact", "provider-error", "missing-final"}:
    for name in task["expected_artifacts"]:
        output = Path("artifacts") / name
        output.parent.mkdir(parents=True, exist_ok=True)
        prompt = task.get("prompt", "")
        contract = task.get("execution_contract", "")
        instructions = prompt + "\n" + contract
        begin = re.search(r"<!-- COMPANY_AGENT_FINAL_REPLY_BEGIN:[A-Za-z0-9_-]+ -->", instructions)
        end = re.search(r"<!-- COMPANY_AGENT_FINAL_REPLY_END:[A-Za-z0-9_-]+ -->", instructions)
        if "missing-reply-contract" in prompt:
            output.write_text("Internal execution notes without a user reply block.")
        elif begin and end:
            output.write_text(
                "Internal execution notes.\n\n"
                + begin.group(0) + "\nFixture final answer.\n" + end.group(0) + "\n"
            )
        else:
            output.write_text('{"total": 42}')
print(json.dumps({"type": "tool_result", "callId": "call-1", "status": "completed", "result": "done"}))
if scenario == "missing-final":
    raise SystemExit(0)
reason = {"kind": "failed"} if scenario == "provider-error" else {"kind": "completed"}
print(json.dumps({"type": "final", "sessionId": "fixture-session", "reason": reason,
                  "text": "done " + os.environ.get("COMPANY_AGENT_MODEL_API_KEY", "")}))
