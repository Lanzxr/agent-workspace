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
session_arg = sys.argv[sys.argv.index("--session-id") + 1] if "--session-id" in sys.argv else None
patch_arg = sys.argv[sys.argv.index("--patch") + 1] if "--patch" in sys.argv else None
counter = Path("work/launch-count.txt")
counter.write_text(counter.read_text() + "run\n" if counter.exists() else "run\n")
if session_arg:
    Path("work/wrapup-meta.json").write_text(json.dumps({
        "session_id": session_arg,
        "patch": json.loads(Path(patch_arg).read_text(encoding="utf-8")) if patch_arg else None,
        "prompt": task.get("prompt", ""),
        "execution_contract": task.get("execution_contract", ""),
    }), encoding="utf-8")
if scenario == "timeout":
    time.sleep(10)
if scenario == "broken-resume" and session_arg:
    sys.stderr.write("cannot resume stale session\n")
    raise SystemExit(2)
if scenario == "bad-protocol":
    print("this is not NDJSON")
    raise SystemExit(0)

def emit(obj: dict) -> None:
    print(json.dumps(obj), flush=True)


def append_session_event(session_id: str, event: dict) -> None:
    dsh_home = Path(os.environ["DSH_HOME"])
    session_file = dsh_home / "sessions" / "fixture-ws" / session_id / "session.v4.jsonl"
    session_file.parent.mkdir(parents=True, exist_ok=True)
    with session_file.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event) + "\n")
        handle.flush()


session_id = session_arg or "fixture-session"
emit({"type": "session", "sessionId": session_id, "cwd": os.getcwd()})
if scenario == "idle-hang-early":
    emit({"type": "status", "phase": "step_start", "turn": 1, "step": 1})
    time.sleep(10)
if scenario == "compaction-ok":
    emit({"type": "status", "phase": "step_start", "turn": 1, "step": 1})
    emit({"type": "status", "phase": "step_end", "turn": 1, "step": 1})
    append_session_event(session_id, {"seq": 1, "type": "compaction/start", "data": {"compactionId": "cmp-ok", "turn": 1}})
    time.sleep(0.55)
    append_session_event(session_id, {"seq": 2, "type": "compaction/end", "data": {"compactionId": "cmp-ok", "turn": 1}})
if scenario == "compaction-hang":
    emit({"type": "status", "phase": "step_start", "turn": 1, "step": 1})
    emit({"type": "status", "phase": "step_end", "turn": 1, "step": 1})
    append_session_event(session_id, {"seq": 1, "type": "compaction/start", "data": {"compactionId": "cmp-hang", "turn": 1}})
    time.sleep(10)
if scenario in {"soft-wrapup", "idle-hang-with-progress", "soft-wrapup-fail", "wrapup-writes-then-compacts"} and not session_arg:
    if scenario == "wrapup-writes-then-compacts":
        Path("work/wrapup-post-compact.txt").write_text("1", encoding="utf-8")
    emit({"type": "status", "phase": "step_start", "turn": 1, "step": 1})
    emit({"type": "tool_call", "callId": "call-p1", "tool": "web_fetch", "input": {"url": "https://example.test"}})
    Path("work/partial-evidence.txt").write_text("collected phase1 evidence", encoding="utf-8")
    if scenario == "soft-wrapup-fail":
        Path("work/fail-wrapup.txt").write_text("1", encoding="utf-8")
    emit({"type": "tool_result", "callId": "call-p1", "status": "completed", "result": "ok"})
    emit({"type": "status", "phase": "step_end", "turn": 1, "step": 1})
    time.sleep(10)
if session_arg and Path("work/fail-wrapup.txt").exists():
    time.sleep(10)
if session_arg and Path("work/wrapup-post-compact.txt").exists():
    emit({"type": "status", "phase": "turn_start", "turn": 2})
    emit({"type": "status", "phase": "step_start", "turn": 2, "step": 1})
    emit({"type": "tool_call", "callId": "call-wrap", "tool": "bash", "input": {"command": "write artifact"}})
    for name in task["expected_artifacts"]:
        output = Path("artifacts") / name
        output.parent.mkdir(parents=True, exist_ok=True)
        begin = re.search(r"<!-- COMPANY_AGENT_FINAL_REPLY_BEGIN:[A-Za-z0-9_-]+ -->",
                          task.get("prompt", "") + "\n" + task.get("execution_contract", ""))
        end = re.search(r"<!-- COMPANY_AGENT_FINAL_REPLY_END:[A-Za-z0-9_-]+ -->",
                        task.get("prompt", "") + "\n" + task.get("execution_contract", ""))
        if begin and end:
            output.write_text(f"{begin.group(0)}\n阶段性整理结果：已写入。\n{end.group(0)}\n", encoding="utf-8")
        else:
            output.write_text('{"partial": "collected phase1 evidence"}', encoding="utf-8")
    emit({"type": "tool_result", "callId": "call-wrap", "status": "completed", "result": "written"})
    emit({"type": "status", "phase": "step_end", "turn": 2, "step": 1})
    append_session_event(session_id, {"seq": 10, "type": "compaction/start", "data": {"compactionId": "cmp-wrap", "turn": 2}})
    time.sleep(10)
if scenario == "active-stream":
    for step in range(1, 5):
        time.sleep(0.12)
        emit({"type": "status", "phase": "step_start", "turn": 1, "step": step})
        emit({"type": "tool_call", "callId": f"call-s-{step}", "tool": "web_fetch", "input": {"url": "https://example.test"}})
        emit({"type": "tool_result", "callId": f"call-s-{step}", "status": "completed", "result": "ok"})
        emit({"type": "status", "phase": "step_end", "turn": 1, "step": step})
if scenario == "live-trace-check":
    emit({"type": "tool_call", "callId": "call-live", "tool": "web_fetch", "input": {"url": "https://example.test"}})
    trace_file = Path("logs/dsh") / f"{task['execution_id']}.events.jsonl"
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and (not trace_file.exists() or "call-live" not in trace_file.read_text(encoding="utf-8")):
        time.sleep(0.02)
    Path("work/live-trace-saw-call.txt").write_text(
        "yes" if trace_file.exists() and "call-live" in trace_file.read_text(encoding="utf-8") else "no",
        encoding="utf-8",
    )
    emit({"type": "tool_result", "callId": "call-live", "status": "completed", "result": "done"})
emit({"type": "thinking", "text": "PRIVATE_REASONING_MUST_NOT_BE_EXPORTED"})
emit({"type": "tool_call", "callId": "call-1", "tool": "bash", "input": {"command": "python3 tools/task.py"}})
if scenario in {"malformed-dsml-missing-artifact", "contract-missing-artifact-ok", "contract-missing-artifact-fail"} and not session_arg:
    Path("work/partial-evidence.txt").write_text("collected workbook output", encoding="utf-8")
    if scenario == "contract-missing-artifact-fail":
        Path("work/fail-contract-wrapup.txt").write_text("1", encoding="utf-8")
elif scenario == "contract-missing-reply-ok" and not session_arg:
    for name in task["expected_artifacts"]:
        output = Path("artifacts") / name
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text("Draft notes missing reply markers.", encoding="utf-8")
elif scenario not in {"missing-artifact", "provider-error", "missing-final"} and not (session_arg and Path("work/fail-contract-wrapup.txt").exists()):
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
            partial = Path("work/partial-evidence.txt").read_text(encoding="utf-8") if Path("work/partial-evidence.txt").exists() else ""
            output.write_text(
                "Internal execution notes.\n\n"
                + begin.group(0) + f"\nFixture final answer. {partial}\n" + end.group(0) + "\n"
            )
        elif session_arg and Path("work/partial-evidence.txt").exists():
            output.write_text(json.dumps({"partial": Path("work/partial-evidence.txt").read_text(encoding="utf-8")}))
        else:
            output.write_text('{"total": 42}')
emit({"type": "tool_result", "callId": "call-1", "status": "completed", "result": "done"})
if scenario == "missing-final":
    raise SystemExit(0)
reason = {"kind": "failed"} if scenario == "provider-error" else {"kind": "completed"}
final_text = (
    "Let me inspect notes.</｜｜DSML｜｜ parameter>\n"
    if scenario == "malformed-dsml-missing-artifact" and not session_arg
    else "done " + os.environ.get("COMPANY_AGENT_MODEL_API_KEY", "")
)
emit({"type": "final", "sessionId": session_arg or "fixture-session", "reason": reason,
      "text": final_text})
