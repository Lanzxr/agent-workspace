from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from app.dssuperagent import DssuperAgentAdapter, DssuperAgentProtocolError
from app.models import Event


class DssuperAgentAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.requests: list[tuple[str, dict[str, str], dict[str, object] | None]] = []
        self.thread_detail: dict[str, object] = {"decisions": []}
        requests = self.requests
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_: object) -> None:
                return

            def _write(self, status: int, value: dict[str, object]) -> None:
                body = json.dumps(value).encode("utf-8")
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers["content-length"])))
                requests.append((self.path, {key.lower(): value for key, value in self.headers.items()}, body))
                self._write(202, {"status": "stored", "trigger_id": "company-agent:event-1",
                                  "thread": "company-agent:conversation-1"})

            def do_GET(self) -> None:
                requests.append((self.path, {key.lower(): value for key, value in self.headers.items()}, None))
                self._write(200, owner.thread_detail)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.adapter = DssuperAgentAdapter(f"http://127.0.0.1:{self.server.server_port}", poll_interval=0.01)

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    @staticmethod
    def event() -> Event:
        return Event("event-1", "chat", "channel-1", "conversation-1", "user-1",
                     "2026-11-15T00:00:00+00:00", "Please review this discussion.", [], ["dsh.task"],
                     external_actor_id="platform-private-id")

    def test_submit_posts_normalized_event_and_keeps_stable_upstream_identity(self) -> None:
        receipt = self.adapter.submit_event(self.event(), context=[{"actor_id": "user-2", "content": "Earlier context"}])

        path, headers, body = self.requests[0]
        self.assertEqual(path, "/orchestrator/api/company-agent/events")
        self.assertEqual(headers["x-orchestrator-request"], "1")
        self.assertEqual(body["event_id"], "event-1")
        self.assertEqual(body["context"][0]["content"], "Earlier context")
        self.assertNotIn("external_actor_id", body)
        self.assertEqual(receipt.trigger_id, "company-agent:event-1")
        self.assertEqual(receipt.thread_key, "company-agent:conversation-1")

    def test_wait_returns_only_the_decision_for_the_submitted_trigger(self) -> None:
        self.thread_detail = {"decisions": [
            {"id": "older", "triggerIds": ["company-agent:previous"]},
            {"id": "decision-1", "kind": "ignore", "outcome": "ignore", "reason": "proposal recorded",
             "at": "2026-11-15T00:00:01+00:00", "strength": "inferred",
             "triggerIds": ["company-agent:event-1"], "thread": "company-agent:conversation-1",
             "assistantIntent": {"action": "observe", "rationale": "No work is needed", "plan_steps": [],
                                 "completion_conditions": [], "publication": {"recommendation": "suppress",
                                                                                  "reason": "No update is needed"}}}
        ]}

        decision = self.adapter.wait_for_decision("company-agent:conversation-1", "company-agent:event-1", timeout=0.2)

        self.assertEqual(decision.decision_id, "decision-1")
        self.assertEqual(decision.assistant_intent["action"], "observe")
        self.assertNotIn("confidence", decision.to_dict())

    def test_malformed_generic_intent_is_a_protocol_error(self) -> None:
        self.thread_detail = {"decisions": [
            {"id": "decision-1", "kind": "ignore", "outcome": "ignore", "reason": "proposal recorded",
             "at": "2026-11-15T00:00:01+00:00", "strength": "inferred",
             "triggerIds": ["company-agent:event-1"], "thread": "company-agent:conversation-1",
             "assistantIntent": {"action": "propose_work", "rationale": "Missing the required objective"}}
        ]}

        with self.assertRaises(DssuperAgentProtocolError):
            self.adapter.wait_for_decision("company-agent:conversation-1", "company-agent:event-1", timeout=0.2)

    def test_adapter_refuses_non_loopback_hosts(self) -> None:
        with self.assertRaises(ValueError):
            DssuperAgentAdapter("https://example.com")


if __name__ == "__main__":
    unittest.main()
