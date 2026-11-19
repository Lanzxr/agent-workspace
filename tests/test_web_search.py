import json
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from urllib.parse import parse_qs, urlsplit

from app.web_search import SearxngSearch


class SearchBackendTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        self.payload = {"query": "Python docs", "results": [
            {"url": "https://docs.python.org/", "title": "Python", "content": "Official documentation", "engine": "test"},
            {"url": "file:///etc/passwd", "title": "Invalid"},
            {"url": "https://www.python.org/doc/", "title": "Documentation", "content": "Guides"},
        ]}
        parent = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self):
                parent.requests.append(parse_qs(urlsplit(self.path).query))
                data = json.dumps(parent.payload).encode()
                self.send_response(200)
                self.end_headers()
                self.wfile.write(data)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.backend = SearxngSearch(f"http://127.0.0.1:{self.server.server_port}/search", 2)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_maps_real_sources_and_filters_non_web_urls(self):
        result = self.backend("Python docs", 8)
        self.assertEqual(len(result["sources"]), 2)
        self.assertEqual(result["sources"][0]["snippet"], "Official documentation")
        self.assertEqual(result["sources"][0]["engine"], "test")
        self.assertIn("https://docs.python.org/", result["content"])
        self.assertTrue(result["retrieved_at"])
        self.assertEqual(self.requests[0]["format"], ["json"])

    def test_truncates_sources_and_rejects_empty_query(self):
        self.assertEqual(len(self.backend("中文 查询", 1)["sources"]), 1)
        self.assertEqual(self.requests[0]["q"], ["中文 查询"])
        with self.assertRaises(ValueError): self.backend(" ")
        self.assertEqual(len(self.requests), 1)

    def test_empty_results_do_not_invent_sources(self):
        self.payload = {"query": "missing", "results": []}
        result = self.backend("missing")
        self.assertEqual(result["sources"], [])
        self.assertEqual(result["content"], "")
