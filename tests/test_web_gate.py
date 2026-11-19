from __future__ import annotations

import json
import tempfile
import unittest
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from app.web_gate import WebGateServer, WebGateSettings, make_handler
from http.server import ThreadingHTTPServer


class WebGateTests(unittest.TestCase):
    def request(self, endpoint: str, *, key: str = "gate-key", body: dict | None = None) -> dict:
        request = Request(endpoint, method="POST", data=json.dumps(body or {"operation": "search", "query": "test"}).encode(),
                          headers={"Content-Type": "application/json", "X-Company-Agent-Bridge-Key": key})
        with urlopen(request, timeout=2) as response:
            return json.loads(response.read())

    def test_gate_rechecks_switch_without_restart(self) -> None:
        enabled = [False]
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(enabled=lambda: enabled[0], api_key="gate-key"))
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        endpoint = f"http://127.0.0.1:{server.server_port}/internal/web/authorize"
        try:
            with self.assertRaises(HTTPError) as error:
                self.request(endpoint)
            self.assertEqual(error.exception.code, 403)
            enabled[0] = True
            self.assertTrue(self.request(endpoint)["authorized"])
            enabled[0] = False
            with self.assertRaises(HTTPError) as error:
                self.request(endpoint)
            self.assertEqual(error.exception.code, 403)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


    def test_search_endpoint_returns_backend_sources_only_when_enabled(self) -> None:
        enabled = [True]
        calls = []
        def search(query: str, max_results: int) -> dict:
            calls.append((query, max_results))
            return {"query": query, "sources": [{"title": "Example", "url": "https://example.com/", "snippet": "proof"}], "truncated": False}
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(enabled=lambda: enabled[0], api_key="gate-key", search=search))
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        endpoint = f"http://127.0.0.1:{server.server_port}/internal/web/search"
        try:
            result = self.request(endpoint, body={"query": "release notes", "max_results": 4, "request_id": "r1"})
            self.assertEqual(result["sources"][0]["url"], "https://example.com/")
            self.assertEqual(calls, [("release notes", 4)])
            enabled[0] = False
            with self.assertRaises(HTTPError) as error:
                self.request(endpoint, body={"query": "release notes"})
            self.assertEqual(error.exception.code, 403)
            self.assertEqual(len(calls), 1)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


    def test_search_audit_does_not_store_raw_query(self) -> None:
        records = []
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(
            enabled=lambda: True, api_key="gate-key", search=lambda query, count: {"query": query, "sources": [], "truncated": False},
            audit=lambda action, payload: records.append((action, payload))))
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            self.request(f"http://127.0.0.1:{server.server_port}/internal/web/search",
                         body={"query": "private project question", "request_id": "r2"})
            self.assertEqual(records[0][0], "web_authorize")
            self.assertNotIn("private project question", json.dumps(records))
            self.assertEqual(records[1][0], "web_search_completed")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_search_backend_failure_is_not_reported_as_success(self) -> None:
        def fail(_query: str, _max_results: int) -> dict:
            raise RuntimeError("private diagnostic")
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(enabled=lambda: True, api_key="gate-key", search=fail))
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with self.assertRaises(HTTPError) as error:
                self.request(f"http://127.0.0.1:{server.server_port}/internal/web/search", body={"query": "test"})
            self.assertEqual(error.exception.code, 502)
            self.assertEqual(json.loads(error.exception.read())["error"], "web search backend failed")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_wrong_key_is_rejected(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(enabled=lambda: True, api_key="gate-key"))
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with self.assertRaises(HTTPError) as error:
                self.request(f"http://127.0.0.1:{server.server_port}/internal/web/authorize", key="wrong")
            self.assertEqual(error.exception.code, 401)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_settings_reuse_knowledge_key_and_validate_port(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = __import__("pathlib").Path(directory) / "runtime.env"
            path.write_text("COMPANY_AGENT_KNOWLEDGE_API_KEY=" + "k" * 40 + "\nCOMPANY_AGENT_WEB_GATE_PORT=8768\n", encoding="utf-8")
            settings = WebGateSettings.load(path)
            self.assertEqual(settings.port, 8768)
            self.assertEqual(settings.api_key, "k" * 40)
