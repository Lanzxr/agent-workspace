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
