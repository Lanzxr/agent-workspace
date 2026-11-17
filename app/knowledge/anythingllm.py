from __future__ import annotations

import http.client
import json
import mimetypes
import secrets
import shutil
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("AnythingLLM redirects are not permitted")


class AnythingLLMError(RuntimeError):
    pass


class AnythingLLMClient:
    """Streaming client for AnythingLLM's authenticated Developer API."""

    def __init__(self, base_url: str, api_key: str, *, timeout: float = 180) -> None:
        parsed = urlsplit(base_url)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment):
            raise ValueError("invalid AnythingLLM base URL")
        self.base_url = base_url.rstrip("/")
        self._parsed, self._api_key, self.timeout = parsed, api_key, timeout
        self._opener = build_opener(_NoRedirect())

    def list_workspaces(self) -> list[dict[str, Any]]:
        body = self._request_json("GET", "/api/v1/workspaces")
        values = body.get("workspaces", [])
        return [item for item in values if isinstance(item, dict)] if isinstance(values, list) else []

    def ensure_workspace(self, slug: str) -> str:
        if any(item.get("slug") == slug for item in self.list_workspaces()):
            return slug
        body = self._request_json("POST", "/api/v1/workspace/new", {"name": slug})
        workspace = body.get("workspace")
        actual = workspace.get("slug") if isinstance(workspace, dict) else None
        if actual != slug:
            raise AnythingLLMError("AnythingLLM did not create the requested workspace slug")
        return actual

    def upload_document(self, path: Path, filename: str, *, workspaces: list[str], source_hash: str,
                        parser_extension: str | None = None) -> str:
        boundary = "----CompanyAgent" + secrets.token_hex(18)
        fields = {
            "addToWorkspaces": ",".join(workspaces),
            "metadata": json.dumps({"title": filename, "docSource": f"company-agent-sha256:{source_hash}"},
                                   ensure_ascii=False, separators=(",", ":")),
        }
        prefix = bytearray()
        for name, value in fields.items():
            prefix.extend(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n".encode())
            prefix.extend(value.encode("utf-8") + b"\r\n")
        extension = parser_extension or Path(filename).suffix.lower()
        if not extension.startswith(".") or not extension[1:].isalnum():
            raise ValueError("invalid AnythingLLM parser extension")
        indexed_filename = f"ca-{source_hash}{extension.lower()}"
        encoded_name = quote(indexed_filename, safe="")
        mime_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        prefix.extend((f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
                       f"filename=\"{indexed_filename}\"; filename*=UTF-8''{encoded_name}\r\n"
                       f"Content-Type: {mime_type}\r\n\r\n").encode("ascii"))
        suffix = f"\r\n--{boundary}--\r\n".encode("ascii")
        connection = self._connection()
        try:
            connection.putrequest("POST", self._target("/api/v1/document/upload"))
            self._headers(connection, f"multipart/form-data; boundary={boundary}")
            connection.putheader("Content-Length", str(len(prefix) + path.stat().st_size + len(suffix)))
            connection.endheaders()
            connection.send(prefix)
            with path.open("rb") as source:
                shutil.copyfileobj(source, _SocketWriter(connection), length=1024 * 1024)
            connection.send(suffix)
            response = connection.getresponse()
            raw = response.read(4 * 1024 * 1024 + 1)
            if len(raw) > 4 * 1024 * 1024:
                raise AnythingLLMError("AnythingLLM upload response exceeded 4 MiB")
            body = self._decode_response(response.status, raw)
        except (OSError, http.client.HTTPException, URLError, TimeoutError) as error:
            raise AnythingLLMError(f"AnythingLLM upload failed: {type(error).__name__}") from error
        finally:
            connection.close()
        if body.get("success") is not True:
            raise AnythingLLMError("AnythingLLM rejected the document upload")
        docs = body.get("documents")
        if isinstance(docs, dict):
            docs = docs.get("items", [])
        location = next((item.get("location") for item in docs or [] if isinstance(item, dict)
                         and isinstance(item.get("location"), str) and item["location"]), None)
        if not location:
            raise AnythingLLMError("AnythingLLM upload returned no stable document location")
        return location

    def search(self, workspace_slug: str, query: str, *, limit: int = 8) -> list[dict[str, Any]]:
        path = f"/api/v1/workspace/{quote(workspace_slug, safe='')}/vector-search"
        body = self._request_json("POST", path, {"query": query, "topN": max(1, min(limit, 20))})
        results = body.get("results", [])
        return [item for item in results if isinstance(item, dict)] if isinstance(results, list) else []

    def health(self) -> dict[str, Any]:
        request = Request(self.base_url + "/api/ping", headers={"Accept": "application/json"})
        try:
            with self._opener.open(request, timeout=min(self.timeout, 10)) as response:
                return {"status": response.status, "body": self._parse_json(response.read(1024 * 1024))}
        except (HTTPError, URLError, OSError, TimeoutError) as error:
            raise AnythingLLMError(f"AnythingLLM health check failed: {type(error).__name__}") from error

    def _request_json(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        request = Request(self.base_url + path, data=data, method=method,
                          headers={"Authorization": f"Bearer {self._api_key}", "Accept": "application/json",
                                   **({"Content-Type": "application/json"} if data is not None else {})})
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                raw = response.read(4 * 1024 * 1024 + 1)
        except HTTPError as error:
            raise AnythingLLMError(f"AnythingLLM returned HTTP {error.code}") from error
        except (URLError, OSError, TimeoutError) as error:
            raise AnythingLLMError(f"AnythingLLM request failed: {type(error).__name__}") from error
        return self._decode_response(200, raw)

    def _decode_response(self, status: int, raw: bytes) -> dict[str, Any]:
        if status < 200 or status >= 300:
            raise AnythingLLMError(f"AnythingLLM returned HTTP {status}")
        result = self._parse_json(raw)
        if not isinstance(result, dict):
            raise AnythingLLMError("AnythingLLM returned invalid JSON")
        return result

    @staticmethod
    def _parse_json(raw: bytes) -> Any:
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise AnythingLLMError("AnythingLLM returned invalid JSON") from error

    def _connection(self):
        if self._parsed.scheme == "https":
            return http.client.HTTPSConnection(self._parsed.hostname, self._parsed.port or 443,
                                               timeout=self.timeout)
        return http.client.HTTPConnection(self._parsed.hostname, self._parsed.port or 80, timeout=self.timeout)

    def _target(self, endpoint: str) -> str:
        return (self._parsed.path.rstrip("/") + endpoint) or endpoint

    def _headers(self, connection, content_type: str) -> None:
        connection.putheader("Authorization", f"Bearer {self._api_key}")
        connection.putheader("Accept", "application/json")
        connection.putheader("Content-Type", content_type)


class _SocketWriter:
    def __init__(self, connection: http.client.HTTPConnection) -> None:
        self.connection = connection

    def write(self, value: bytes) -> int:
        self.connection.send(value)
        return len(value)
