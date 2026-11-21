from __future__ import annotations

import base64
import json
import os
import tempfile
import threading
import time
import unittest
import zipfile
from http.server import HTTPServer
from pathlib import Path
from threading import Thread
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from app.admin_api import make_handler
from app.assistant_workflow import AssistantPolicy, DssuperAgentWorkflow
from app.control_plane import ControlPlane
from app.dsh import DshAdapter
from app.dssuperagent import DssuperAgentDecision, DssuperAgentReceipt
from app.execution import ExecutionDispatcher
from app.knowledge.config import KnowledgeSettings
from app.knowledge.delivery import KnowledgeDelivery
from app.knowledge.service import KnowledgeService
from app.models import Event, ExecutionResult, OutboxMessage
from app.qq_channel import QQBotAPI, QQChannelService, _qq_media_file_type
from app.reply_contract import (
    deliverable_files_markers,
    extract_deliverable_files,
    final_reply_markers,
    strip_thinking_blocks,
)
from app.runtime_config import QQBotSettings, RuntimeConfig
from app.workspace import WorkspaceManager
from app.workspace_retention import (
    DEFAULT_WORKSPACE_RETENTION_DAYS,
    get_retention_days,
    list_workspace_files,
    prune_expired_workspace_files,
    resolve_workspace_file,
    set_retention_days,
    summarize_recent_workspace_files,
    touch_workspace_files,
)


class _DummyAnythingLlm:
    def ensure_workspace(self, slug: str) -> str:
        return slug

    def upload_document(self, path: Path, filename: str, *, workspaces: list[str],
                        source_hash: str, parser_extension: str | None = None) -> str:
        return f"custom-documents/{source_hash}.txt"

    def search(self, slug: str, query: str, *, limit: int = 8) -> list[dict]:
        return []


class WorkspaceRetentionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.plane = ControlPlane.open(self.root / "control.sqlite")
        self.manager = WorkspaceManager(self.root / "workspaces")
        self.ws = self.manager.provision("ws-retention-test")

    def tearDown(self) -> None:
        self.plane.close()
        self.temp.cleanup()

    def test_default_retention_setting_and_validation(self) -> None:
        self.assertEqual(DEFAULT_WORKSPACE_RETENTION_DAYS, 15)
        self.assertEqual(get_retention_days(self.plane), 15)
        self.assertEqual(set_retention_days(self.plane, 30), 30)
        self.assertEqual(get_retention_days(self.plane), 30)
        for bad in (0, -5, 366, True, "bad", None):
            with self.assertRaises(ValueError):
                set_retention_days(self.plane, bad)

    def test_list_touch_resolve_and_prune_respects_15_day_ttl_and_active_workspaces(self) -> None:
        fresh_in = self.ws.directory("work") / "inputs" / "exec-1" / "01-report.xlsx"
        fresh_out = self.ws.directory("work") / "outputs" / "work-1" / "derived.xlsx"
        stale_out = self.ws.directory("work") / "outputs" / "work-old" / "old.xlsx"
        stale_art = self.ws.directory("artifacts") / "executions" / "exec-old" / "step-01-result.md"
        state_file = self.ws.directory("state") / "executions" / "keep.json"
        for p, payload in (
            (fresh_in, b"fresh-input"),
            (fresh_out, b"fresh-output"),
            (stale_out, b"stale-output"),
            (stale_art, b"old-report"),
            (state_file, b"{}"),
        ):
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(payload)

        now = time.time()
        sixteen_days_ago = now - 16 * 86400.0
        for p in (stale_out, stale_art, state_file):
            os.utime(p, (sixteen_days_ago, sixteen_days_ago))

        resolved = resolve_workspace_file(self.manager, self.ws.workspace_id, "work/outputs/work-1/derived.xlsx")
        self.assertEqual(resolved.read_bytes(), b"fresh-output")
        with self.assertRaises(ValueError):
            resolve_workspace_file(self.manager, self.ws.workspace_id, "../secret.txt")
        with self.assertRaises(ValueError):
            resolve_workspace_file(self.manager, self.ws.workspace_id, "state/executions/keep.json")

        # Refreshing stale_out via touch_workspace_files keeps it alive
        self.assertEqual(
            touch_workspace_files(self.manager, self.ws.workspace_id, ["work/outputs/work-old/old.xlsx"]),
            1,
        )
        listed = list_workspace_files(self.manager, workspace_id=self.ws.workspace_id, retention_days=15, now=now)
        rels = {item["relative_path"] for item in listed}
        self.assertEqual(rels, {
            "work/inputs/exec-1/01-report.xlsx",
            "work/outputs/work-1/derived.xlsx",
            "work/outputs/work-old/old.xlsx",
            "artifacts/executions/exec-old/step-01-result.md",
        })
        compact = summarize_recent_workspace_files(self.manager, self.ws.workspace_id, retention_days=15)
        self.assertEqual({item["category"] for item in compact}, {"inputs", "outputs"})

        # Running workspace must be skipped during prune
        self.manager.mark_running(self.ws.workspace_id)
        skipped = prune_expired_workspace_files(self.manager, retention_days=15, now=now)
        self.assertEqual(skipped["skipped_active"], 1)
        self.assertTrue(stale_art.exists())

        self.manager.mark_ready(self.ws.workspace_id)
        summary = prune_expired_workspace_files(self.manager, retention_days=15, now=now)
        self.assertEqual(summary["deleted_files"], 1)
        self.assertFalse(stale_art.exists())
        self.assertTrue(fresh_in.exists())
        self.assertTrue(fresh_out.exists())
        self.assertTrue(stale_out.exists())  # touched!
        self.assertTrue(state_file.exists())  # state/ never touched by TTL prune

    def test_admin_api_retention_list_download_and_prune(self) -> None:
        out_file = self.ws.directory("work") / "outputs" / "work-abc" / "季度报表.xlsx"
        out_file.parent.mkdir(parents=True, exist_ok=True)
        out_file.write_bytes(b"PK\x03\x04excel-bytes")

        server = HTTPServer(("127.0.0.1", 0), make_handler(self.plane, self.root, "tok-secret-12345678901234"))
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        headers = {"Authorization": "Bearer tok-secret-12345678901234", "Content-Type": "application/json"}
        try:
            with urlopen(Request(base + "/api/settings/file-retention", headers=headers)) as r:
                self.assertEqual(json.loads(r.read())["retention_days"], 15)
            with urlopen(Request(base + "/api/settings/file-retention", data=json.dumps({"retention_days": 20}).encode(),
                                 headers=headers, method="PUT")) as r:
                self.assertEqual(json.loads(r.read())["retention_days"], 20)
            with urlopen(Request(base + "/api/workspace-files", headers=headers)) as r:
                items = json.loads(r.read())["files"]
                self.assertEqual(len(items), 1)
                self.assertEqual(items[0]["filename"], "季度报表.xlsx")
            dl_url = f"{base}/api/workspace-files/download?workspace_id={self.ws.workspace_id}&path=work/outputs/work-abc/%E5%AD%A3%E5%BA%A6%E6%8A%A5%E8%A1%A8.xlsx"
            with urlopen(Request(dl_url, headers=headers)) as r:
                self.assertEqual(r.read(), b"PK\x03\x04excel-bytes")
            with self.assertRaises(HTTPError) as ctx:
                urlopen(Request(f"{base}/api/workspace-files/download?workspace_id={self.ws.workspace_id}&path=state/workspace.json",
                                headers=headers))
            self.assertEqual(ctx.exception.code, 400)
            ctx.exception.close()
            with urlopen(Request(base + "/api/workspace-files/prune", data=b"{}", headers=headers, method="POST")) as r:
                self.assertEqual(json.loads(r.read())["deleted_files"], 0)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_dispatcher_retains_staged_inputs_and_workflow_inherits_across_steps(self) -> None:
        settings = KnowledgeSettings("http://127.0.0.1:3001", "k", "g" * 40, frozenset({"u1"}),
                                     max_file_bytes=10 * 1024 * 1024, query_api_port=39001, parse_timeout_seconds=1)
        ks = KnowledgeService(self.plane.store, settings, self.root / "knowledge-originals", client=_DummyAnythingLlm())
        up_ev = Event("ev-up", "qq.group", "grp-1", "grp-1", "u1", "2026-11-20T00:00:00Z", "upload", [], ["dsh.task"])
        src = self.root / "2026报表.xlsx"
        with zipfile.ZipFile(src, "w") as zf:
            zf.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
            zf.writestr("xl/workbook.xml", '<?xml version="1.0"?><workbook/>')
            zf.writestr("xl/worksheets/sheet1.xml", '<?xml version="1.0"?><worksheet/>')
        doc = ks.ingest_file(up_ev, 1, src, "2026报表.xlsx")

        # Stage via KnowledgeDelivery directly & verify idempotent reuse on same hash
        delivery = KnowledgeDelivery(ks, self.manager)
        staged1 = delivery.stage(up_ev, self.ws, "exec-idempotent", [doc["document_id"]])
        staged2 = delivery.stage(up_ev, self.ws, "exec-idempotent", [doc["document_id"]])
        self.assertEqual(staged1[0].relative_path, staged2[0].relative_path)

        bwrap = self.root / "bwrap"
        bwrap.write_text("stub", encoding="utf-8")
        bwrap.chmod(0o700)
        config = RuntimeConfig("https://api.example/v1", "m", "secret-key", ("dsh",))
        adapter = DshAdapter(config, self.manager, self.root / "runtime", bubblewrap_executable=bwrap)
        seen_prompts: list[str] = []

        def fake_execute(req, workspace, task, *, lease_fd, workspace_tier=None):
            seen_prompts.append(task.prompt)
            for rel in task.expected_artifacts:
                p = workspace.root / "artifacts" / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text("ok", encoding="utf-8")
            return ExecutionResult(req.execution_id, "succeeded", artifact_refs=[f"artifacts/{r}" for r in task.expected_artifacts],
                                   metadata={"session_id": "sess-shared"})

        adapter.execute = fake_execute  # type: ignore[method-assign]
        dispatcher = ExecutionDispatcher(self.plane, self.manager, adapter, self.root / "runtime", knowledge_service=ks)

        class Upstream:
            def __init__(self) -> None:
                self.caps = None

            def submit_event(self, event, *, context=None, interaction_mode=None, available_capabilities=None):
                self.caps = available_capabilities
                return DssuperAgentReceipt("stored", f"trig:{event.event_id}", f"thr:{event.conversation_id}")

            def submit_execution_result(self, event):
                return DssuperAgentReceipt("stored", f"res:{event.event_id}", f"thr:{event.conversation_id}")

            def wait_for_decision(self, thread_key, trigger_id, *, timeout=120):
                return DssuperAgentDecision(
                    "dec-1", [trigger_id], thread_key, "ignore", "ignore", [], "2026-11-20T00:00:01Z", "inferred",
                    assistant_intent={
                        "action": "propose_work",
                        "title": "二阶段处理报表",
                        "objective": "读取并写入公式列",
                        "completion_conditions": ["完成"],
                        "plan_steps": [
                            {
                                "title": "步骤一读表",
                                "objective": "读取原表",
                                "capability": "dsh.task",
                                "knowledge_document_ids": [doc["document_id"]],
                            },
                            {
                                "title": "步骤二改表（规划漏传 document_id）",
                                "objective": "写入公式列",
                                "capability": "dsh.task",
                                "knowledge_document_ids": [],
                            },
                        ],
                        "publication": {"recommendation": "group_update", "reason": "ok", "body": "ok"},
                    },
                )

        wf = DssuperAgentWorkflow(
            self.plane, Upstream(), manager=self.manager, dispatcher=dispatcher, knowledge_service=ks,
            policy=AssistantPolicy(auto_execute_capabilities=frozenset({"dsh.task"})),
        )
        run_ev = Event("ev-run", "qq.group", "grp-1", "grp-1", "u1", "2026-11-20T00:00:05Z", "加衍生公式列", [], ["dsh.task"])
        run = wf.process_event(run_ev, workspace_id=self.ws.workspace_id)
        self.assertEqual(run.status, "completed")
        self.assertEqual(len(run.execution_ids), 2)
        # Step 1 staged input MUST STILL EXIST after Step 1 & Step 2 complete!
        step1_input = self.ws.root / "work" / "inputs" / run.execution_ids[0] / "01-2026报表.xlsx"
        step2_input = self.ws.root / "work" / "inputs" / run.execution_ids[1] / "01-2026报表.xlsx"
        self.assertTrue(step1_input.is_file())
        self.assertTrue(step2_input.is_file())
        self.assertIn("Approved reference files staged in this workspace:", seen_prompts[1])
        self.assertIn("01-2026报表.xlsx", seen_prompts[1])


class DeliverableAndQQFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.plane = ControlPlane.open(self.root / "control.sqlite")
        self.manager = WorkspaceManager(self.root / "workspaces")
        self.ws = self.manager.provision("ws-qq-deliver")

    def tearDown(self) -> None:
        self.plane.close()
        self.temp.cleanup()

    def test_reply_contract_deliverables_and_thinking_strip(self) -> None:
        self.assertEqual(strip_thinking_blocks("<thinking>secret chain</thinking>hello"), "hello")
        self.assertEqual(strip_thinking_blocks("err line\n<thinking>truncated"), "err line")
        b, e = deliverable_files_markers("exec_1")
        sample = f"text\n{b}\n[\"work/outputs/w1/out.xlsx\", \"../bad.xlsx\", \"work/outputs/w1/out.xlsx\"]\n{e}"
        self.assertEqual(extract_deliverable_files(sample, "exec_1"), ["work/outputs/w1/out.xlsx"])

    def test_dsh_collects_deliverables_and_strips_thinking(self) -> None:
        bwrap = self.root / "bwrap"
        bwrap.write_text("stub", encoding="utf-8")
        bwrap.chmod(0o700)
        config = RuntimeConfig("https://api.example/v1", "m", "super-secret-xyz", ("dsh",))
        adapter = DshAdapter(config, self.manager, self.root / "runtime", bubblewrap_executable=bwrap)
        # _read_protocol must strip <thinking> blocks from final text
        ndjson = "\n".join([
            json.dumps({"type": "session", "sessionId": "s1", "cwd": str(self.ws.root)}),
            json.dumps({"type": "final", "sessionId": "s1", "reason": {"kind": "completed"},
                        "text": "<thinking>internal chain</thinking>\n\n[Errno 2] No such file"}),
        ]).encode("utf-8")
        _, sid, _, final_out, err = DshAdapter._read_protocol(ndjson, self.ws.root)
        self.assertEqual(sid, "s1")
        self.assertIsNone(err)
        self.assertEqual(final_out, "[Errno 2] No such file")
        ev = Event("ev-dl", "qq.group", "g1", "g1", "u1", "2026-11-20T00:00:00Z", "run", [], ["dsh.task"])
        self.plane.ingest_event(ev)
        self.plane.create_work_item(
            work_item_id="work-dl", title="t", objective="obj",
            source_event_ids=["ev-dl"], completion_conditions=["done"],
            risk_class="low", owner_ref="u1",
        )
        self.plane.propose_work_item("work-dl")
        self.plane.accept_work_item("work-dl")
        self.plane.queue_work_item("work-dl")
        from app.models import PlanStep
        self.plane.create_plan(plan_id="plan-dl", work_item_id="work-dl", steps=[PlanStep("step-1", 1, "do", capability="dsh.task")])
        req = self.plane.create_execution_request(
            execution_id="exec_dl", work_item_id="work-dl", plan_step_id="step-1",
            workspace_id=self.ws.workspace_id, capability="dsh.task",
            parameters={"prompt": "do", "expected_artifacts": ["executions/exec_dl/step-01-result.md"],
                        "require_user_reply": True},
        )
        self.plane.start_execution("exec_dl")
        # Write deliverable in task output dir AND omit explicit FILES marker -> fallback auto-discovers .xlsx!
        out_xlsx = self.ws.root / "work" / "outputs" / "work-dl" / "衍生报表.xlsx"
        out_xlsx.parent.mkdir(parents=True, exist_ok=True)
        out_xlsx.write_bytes(b"PK\x03\x04derived-xlsx")
        helper_py = self.ws.root / "work" / "outputs" / "work-dl" / "helper.py"
        helper_py.write_text("print(1)", encoding="utf-8")  # Must NOT be auto-sent when undeclared!

        task = adapter.preflight(req, self.ws)
        self.assertIsNotNone(task)
        rb, re_ = final_reply_markers("exec_dl")
        art = self.ws.root / "artifacts" / "executions" / "exec_dl" / "step-01-result.md"
        art.parent.mkdir(parents=True, exist_ok=True)
        art.write_text(f"{rb}\n已完成并导出。\n{re_}", encoding="utf-8")

        res = adapter._collect_deliverable_files(
            self.ws, req, [{"path": "artifacts/executions/exec_dl/step-01-result.md"}],
            started_epoch=time.time() - 5,
        )
        self.assertEqual([x["filename"] for x in res], ["衍生报表.xlsx"])
        self.assertEqual(res[0]["path"], "work/outputs/work-dl/衍生报表.xlsx")
        self.assertEqual(res[0]["workspace_id"], self.ws.workspace_id)

    def test_qq_api_upload_and_outbox_delivers_text_and_excel_separately(self) -> None:
        self.assertEqual(_qq_media_file_type("report.xlsx"), 4)
        self.assertEqual(_qq_media_file_type("chart.png"), 1)
        api = QQBotAPI(QQBotSettings("app-1", "sec-1", allowed_publications=frozenset({"group_update"})))
        api.access_token = Mock(return_value="tok-1")
        requests_seen: list[tuple[str, str, dict, dict]] = []

        def fake_request(method, url, *, body=None, token=None, extra_headers=None, timeout=None):
            requests_seen.append((method, url, dict(body or {}), dict(extra_headers or {})))
            if url.endswith("/files"):
                return {"file_uuid": "uuid-1", "file_info": "INFO_XLSX_1", "ttl": 3600}
            return {"id": "sent-1"}

        api._request = fake_request  # type: ignore[method-assign]
        xlsx_path = self.ws.root / "work" / "outputs" / "w1" / "新表.xlsx"
        xlsx_path.parent.mkdir(parents=True, exist_ok=True)
        xlsx_path.write_bytes(b"PK\x03\x04hello-xlsx")

        class _Wf:
            def __init__(self, manager):
                self.manager = manager

        svc = QQChannelService(
            self.plane, _Wf(self.manager), api,
            QQBotSettings("app-1", "sec-1", allowed_publications=frozenset({"group_update"})),
            workspace_manager=self.manager,
        )
        ev = Event("qq:app-1:group:m1", "qq.group", "grp-x", "grp-x", "u1", "2026-11-20T00:00:00Z", "做表", [], ["dsh.task"])
        self.plane.ingest_event(ev)
        self.plane.create_conversation(conversation_id="conv-x", kind="qq.group", participant_refs=["u1"],
                                       policy_version="p1")
        self.plane.create_work_item(
            work_item_id="w1", title="做表", objective="obj",
            source_event_ids=["qq:app-1:group:m1"], completion_conditions=["ok"],
            risk_class="low", owner_ref="u1",
        )

        art = self.ws.root / "artifacts" / "executions" / "e1" / "step-01-result.md"
        art.parent.mkdir(parents=True, exist_ok=True)
        rb, re_ = final_reply_markers("e1")
        art.write_text(f"{rb}\n新增6列已完成，见附件。\n{re_}", encoding="utf-8")

        svc._decide_publication(
            ev,
            {
                "status": "completed",
                "work_item_id": "w1",
                "conversation_id": "conv-x",
                "workspace_id": self.ws.workspace_id,
                "policy_version": "p1",
                "publication": {"recommendation": "group_update", "reason": "ok", "body": ""},
                "execution_results": [{
                    "execution_id": "e1",
                    "status": "succeeded",
                    "artifact_refs": ["artifacts/executions/e1/step-01-result.md"],
                    "deliverable_files": [{
                        "path": "work/outputs/w1/新表.xlsx",
                        "filename": "新表.xlsx",
                        "bytes": xlsx_path.stat().st_size,
                        "content_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        "workspace_id": self.ws.workspace_id,
                    }],
                }],
            },
            {
                "channel": "qq",
                "group_openid": "grp-x",
                "reply_to_message_id": "m1",
                "reply_deadline_epoch": str(time.time() + 240),
            },
        )
        svc.flush_outbox()
        self.assertEqual([item[1] for item in requests_seen], [
            "https://api.bot.qq.com/v2/groups/grp-x/messages",
            "https://api.bot.qq.com/v2/groups/grp-x/files",
            "https://api.bot.qq.com/v2/groups/grp-x/messages",
        ])
        # First message is text (seq=1), second upload + msg_type=7 (seq=2)
        self.assertEqual(requests_seen[0][2]["msg_type"], 0)
        self.assertEqual(requests_seen[0][2]["msg_seq"], 1)
        self.assertEqual(requests_seen[1][2]["file_type"], 4)
        self.assertEqual(requests_seen[1][2]["file_name"], "新表.xlsx")
        self.assertEqual(base64.b64decode(requests_seen[1][2]["file_data"]), b"PK\x03\x04hello-xlsx")
        self.assertEqual(requests_seen[1][3]["X-Union-Appid"], "app-1")
        self.assertEqual(requests_seen[2][2], {
            "msg_type": 7, "media": {"file_info": "INFO_XLSX_1"}, "msg_id": "m1", "msg_seq": 2,
        })
        outbox = self.plane.list_outbox()
        self.assertEqual([m.status for m in outbox], ["sent", "sent"])

        # Transient failure on file upload retries ONLY the file message without duplicating text
        fail_ev = Event("qq:app-1:group:m2", "qq.group", "grp-x", "grp-x", "u1", "2026-11-20T00:05:00Z", "做表2", [], ["dsh.task"])
        self.plane.ingest_event(fail_ev)
        self.plane.create_work_item(
            work_item_id="w2", title="做表2", objective="obj",
            source_event_ids=["qq:app-1:group:m2"], completion_conditions=["ok"],
            risk_class="low", owner_ref="u1",
        )
        rb2, re2 = final_reply_markers("e2")
        art2 = self.ws.root / "artifacts" / "executions" / "e2" / "step-01-result.md"
        art2.parent.mkdir(parents=True, exist_ok=True)
        art2.write_text(f"{rb2}\n第二份完成。\n{re2}", encoding="utf-8")
        should_fail_file_once = {"flag": True}

        def flaky_request(method, url, *, body=None, token=None, extra_headers=None, timeout=None):
            requests_seen.append((method, url, dict(body or {}), dict(extra_headers or {})))
            if url.endswith("/files"):
                if should_fail_file_once["flag"]:
                    should_fail_file_once["flag"] = False
                    raise RuntimeError("transient upload timeout")
                return {"file_uuid": "uuid-2", "file_info": "INFO_XLSX_2", "ttl": 3600}
            return {"id": "sent-ok"}

        api._request = flaky_request  # type: ignore[method-assign]
        svc._decide_publication(
            fail_ev,
            {
                "status": "completed", "work_item_id": "w2", "conversation_id": "conv-x",
                "workspace_id": self.ws.workspace_id, "policy_version": "p1",
                "publication": {"recommendation": "group_update", "reason": "ok", "body": ""},
                "execution_results": [{
                    "execution_id": "e2", "status": "succeeded",
                    "artifact_refs": ["artifacts/executions/e2/step-01-result.md"],
                    "deliverable_files": [{
                        "path": "work/outputs/w1/新表.xlsx", "filename": "新表.xlsx",
                        "bytes": xlsx_path.stat().st_size, "workspace_id": self.ws.workspace_id,
                    }],
                }],
            },
            {"channel": "qq", "group_openid": "grp-x"},
        )
        requests_seen.clear()
        svc.flush_outbox(respect_backoff=False)
        retry_msgs = [m for m in self.plane.list_outbox() if "qq:app-1:group:m2" in m.idempotency_key]
        self.assertEqual([m.status for m in retry_msgs], ["sent", "failed"])
        requests_seen.clear()
        svc.flush_outbox(respect_backoff=False)
        retry_msgs = [m for m in self.plane.list_outbox() if "qq:app-1:group:m2" in m.idempotency_key]
        self.assertEqual([m.status for m in retry_msgs], ["sent", "sent"])
        # On retry, text was NOT re-sent; only /files + msg_type=7 ran!
        self.assertEqual([item[1] for item in requests_seen], [
            "https://api.bot.qq.com/v2/groups/grp-x/files",
            "https://api.bot.qq.com/v2/groups/grp-x/messages",
        ])

        # Sequence > 5 gracefully avoids passive cap crash and falls back to active delivery
        for idx in range(3, 8):
            self.plane.decide_publication(
                decision_id=f"dec-extra-{idx}",
                work_item_id="w1",
                conversation_id="conv-x",
                disposition="group_update",
                reason="extra",
                policy_version="p1",
                body=f"msg {idx}",
                idempotency_key=f"extra:{idx}",
                destination={"channel": "qq", "group_openid": "grp-x",
                             "reply_to_message_id": "m1", "reply_deadline_epoch": str(time.time() + 240)},
            )
        extra_msgs = [m for m in self.plane.list_outbox() if m.idempotency_key.startswith("extra:")]
        self.assertEqual([m.destination.get("msg_seq") for m in extra_msgs], ["3", "4", "5", None, None])

    def test_qq_api_chunked_upload_over_5mb(self) -> None:
        api = QQBotAPI(QQBotSettings("app-1", "sec-1"))
        api.access_token = Mock(return_value="tok-1")
        put_calls: list[tuple[str, int]] = []
        api_calls: list[tuple[str, dict]] = []

        def fake_request(method, url, *, body=None, token=None, extra_headers=None, timeout=None):
            api_calls.append((url, dict(body or {})))
            if url.endswith("/upload_prepare"):
                return {
                    "upload_id": "up-1",
                    "block_size": 5 * 1024 * 1024,
                    "parts": [
                        {"index": 0, "block_size": 5 * 1024 * 1024, "presigned_url": "https://upload.example/p0"},
                        {"index": 1, "block_size": 1024, "presigned_url": "https://upload.example/p1"},
                    ],
                }
            if url.endswith("/upload_part_finish"):
                return {"code": 0}
            if url.endswith("/files"):
                return {"file_info": "INFO_BIG_FILE"}
            return {}

        api._request = fake_request  # type: ignore[method-assign]
        api._put_presigned_part = lambda url, chunk: put_calls.append((url, len(chunk)))  # type: ignore[method-assign]
        big_file = self.root / "big.xlsx"
        big_file.write_bytes(b"X" * (5 * 1024 * 1024 + 1024))
        file_info = api.upload_file(target_kind="group", target_id="grp-1", file_path=big_file)
        self.assertEqual(file_info, "INFO_BIG_FILE")
        self.assertEqual(put_calls, [("https://upload.example/p0", 5 * 1024 * 1024),
                                     ("https://upload.example/p1", 1024)])
        self.assertEqual([c[0].rsplit("/", 1)[-1] for c in api_calls], [
            "upload_prepare", "upload_part_finish", "upload_part_finish", "files",
        ])


if __name__ == "__main__":
    unittest.main()
