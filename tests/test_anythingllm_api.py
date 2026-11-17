from __future__ import annotations

import json
import tempfile
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

from app.knowledge.anythingllm import AnythingLLMClient


class AnythingLLMApiContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.received: list[tuple[str, str, dict[str, str], bytes]] = []
        self.workspaces: list[dict[str, str]] = []
        received, workspaces = self.received, self.workspaces

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, _format: str, *_args: object) -> None:
                return

            def _reply(self, payload: dict, status: int = 200) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                received.append(("GET", self.path, dict(self.headers), b""))
                if self.path == "/api/v1/workspaces":
                    self._reply({"workspaces": workspaces})
                elif self.path == "/api/ping":
                    self._reply({"online": True})
                else:
                    self._reply({"error": "missing"}, 404)

            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                received.append(("POST", self.path, dict(self.headers), body))
                if self.path == "/api/v1/workspace/new":
                    value = json.loads(body)
                    workspaces.append({"slug": value["name"]})
                    self._reply({"workspace": {"slug": value["name"]}})
                elif self.path == "/api/v1/document/upload":
                    self._reply({"success": True, "documents": [{"location": "custom-documents/hash.txt"}]})
                elif self.path == "/api/v1/workspace/company-agent-library/vector-search":
                    self._reply({"results": [{"id": "chunk-1", "text": "retrieved evidence",
                                               "metadata": {"docSource": "custom-documents/ca-" + "a" * 64 + "-notes.txt",
                                                            "title": "minutes.txt", "page": 2},
                                               "score": 0.93}]})
                else:
                    self._reply({"error": "missing"}, 404)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.client = AnythingLLMClient(f"http://127.0.0.1:{self.server.server_port}", "test-api-key", timeout=2)

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_workspace_creation_upload_and_vector_search_use_developer_api(self) -> None:
        source_hash = "a" * 64
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "meeting minutes.txt"
            source.write_text("meeting evidence", encoding="utf-8")
            self.assertEqual(self.client.ensure_workspace("company-agent-library"), "company-agent-library")
            self.assertEqual(self.client.upload_document(source, source.name,
                                                         workspaces=["company-agent-library"],
                                                         source_hash=source_hash),
                             "custom-documents/hash.txt")
        results = self.client.search("company-agent-library", "meeting", limit=4)
        self.assertEqual(results[0]["text"], "retrieved evidence")
        self.assertEqual(self.client.health()["status"], 200)

        paths = [path for _, path, _, _ in self.received]
        self.assertIn("/api/v1/workspaces", paths)
        self.assertIn("/api/v1/workspace/new", paths)
        self.assertIn("/api/v1/document/upload", paths)
        self.assertIn("/api/v1/workspace/company-agent-library/vector-search", paths)
        for method, path, headers, body in self.received:
            if path.startswith("/api/v1/"):
                self.assertEqual(headers.get("Authorization"), "Bearer test-api-key")
        upload_headers = next(headers for method, path, headers, _ in self.received
                              if method == "POST" and path == "/api/v1/document/upload")
        self.assertTrue(upload_headers["Content-Type"].startswith("multipart/form-data; boundary="))
        upload_body = next(body for method, path, _, body in self.received
                           if method == "POST" and path == "/api/v1/document/upload")
        self.assertIn(f"filename=\"ca-{source_hash}.txt\"".encode(), upload_body)
        self.assertIn(b'"docSource":"company-agent-sha256:' + source_hash.encode() + b'"', upload_body)
        self.assertIn(b"addToWorkspaces", upload_body)
        self.assertIn(b"company-agent-library", upload_body)


if __name__ == "__main__":
    unittest.main()
