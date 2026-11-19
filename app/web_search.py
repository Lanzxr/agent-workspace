from __future__ import annotations

import json
from datetime import datetime, timezone
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from typing import Any


class SearxngSearch:
    """Small Company Agent adapter for a loopback SearXNG JSON endpoint."""

    def __init__(self, endpoint: str, timeout: float = 20.0) -> None:
        self.endpoint = endpoint
        self.timeout = timeout

    def __call__(self, query: str, max_results: int = 8) -> dict[str, Any]:
        if not isinstance(query, str) or not query.strip() or len(query) > 2000:
            raise ValueError("query must contain 1-2000 characters")
        count = max(1, min(int(max_results), 8))
        separator = "&" if "?" in self.endpoint else "?"
        request_url = self.endpoint + separator + urlencode({"q": query.strip(), "format": "json", "safesearch": 1})
        request = Request(request_url, headers={"Accept": "application/json", "User-Agent": "CompanyAgent/1.0"})
        with urlopen(request, timeout=self.timeout) as response:
            raw = response.read(4_000_001)
        if len(raw) > 4_000_000:
            raise ValueError("search response exceeded 4 MB")
        payload = json.loads(raw)
        raw_results = payload.get("results", []) if isinstance(payload, dict) else []
        sources = []
        for item in raw_results:
            if not isinstance(item, dict):
                continue
            url = item.get("url")
            title = item.get("title")
            snippet = item.get("content")
            if not isinstance(url, str) or not url.startswith(("https://", "http://")):
                continue
            source: dict[str, Any] = {"url": url}
            if isinstance(title, str) and title.strip():
                source["title"] = title.strip()[:500]
            if isinstance(snippet, str) and snippet.strip():
                source["snippet"] = snippet.strip()[:3000]
            if isinstance(item.get("publishedDate"), str):
                source["publishedAt"] = item["publishedDate"][:80]
            if isinstance(item.get("engine"), str):
                source["engine"] = item["engine"][:80]
            sources.append(source)
            if len(sources) >= count:
                break
        content = "\n\n".join(
            f"{source.get('title', source['url'])}\n{source.get('snippet', '')}\n{source['url']}".strip()
            for source in sources
        )
        return {
            "query": str(payload.get("query", query.strip()))[:2000],
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "content": content,
            "truncated": bool(raw_results and len(raw_results) > count),
            "sources": sources,
        }
