from __future__ import annotations

import csv
import io
import json
import re
import socket
import shutil
import tempfile
import zipfile
import hashlib
import os
from dataclasses import replace
from datetime import datetime, timezone
from http.client import HTTPSConnection
from html import unescape
from ipaddress import ip_address
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request, build_opener, urlopen
from xml.etree import ElementTree

from .models import Event
from .store import Store
from .knowledge.config import MAX_KNOWLEDGE_FILE_BYTES
from .knowledge.file_types import detect_file
from .knowledge.service import KnowledgeService

MAX_ATTACHMENT_BYTES = 12 * 1024 * 1024
MAX_EXTRACTED_CHARS = 40_000
ALLOWED_EXTENSIONS = {".txt", ".md", ".csv", ".json", ".html", ".htm", ".docx", ".xlsx", ".pdf",
                     ".png", ".jpg", ".jpeg"}


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("attachment redirects are not permitted")


def _is_qq_attachment_host(host: str) -> bool:
    normalized = host.lower().rstrip(".")
    return normalized == "ftn.qq.com" or normalized.endswith(".ftn.qq.com")


def _resolve_qq_attachment_addresses(host: str, timeout: float) -> tuple[str, ...]:
    query = urlencode({"name": host, "type": "A"})
    request = Request(f"https://cloudflare-dns.com/dns-query?{query}",
                      headers={"Accept": "application/dns-json", "User-Agent": "CompanyAgent/1.0"})
    with urlopen(request, timeout=timeout) as response:
        data = json.loads(response.read(65536))
    answers = data.get("Answer", [])
    addresses = tuple(dict.fromkeys(
        str(ip_address(item["data"])) for item in answers
        if isinstance(item, dict) and item.get("type") == 1 and isinstance(item.get("data"), str)
    ))
    if not addresses or any(not ip_address(address).is_global for address in addresses):
        raise ValueError("QQ attachment DNS did not return only public addresses")
    return addresses


def _resolve_attachment_addresses(host: str, timeout: float) -> tuple[str, ...]:
    try:
        literal = ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if not literal.is_global:
            raise ValueError("private or reserved attachment hosts are not permitted")
        return (str(literal),)

    try:
        addresses = tuple(dict.fromkeys(
            str(ip_address(result[4][0]))
            for result in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        ))
    except socket.gaierror:
        addresses = ()
    if addresses and all(ip_address(address).is_global for address in addresses):
        return addresses
    if _is_qq_attachment_host(host):
        return _resolve_qq_attachment_addresses(host, timeout)
    if addresses:
        raise ValueError("attachment host resolves to a private or reserved address")
    raise ValueError("attachment host did not resolve")


def _attachment_opener(host: str, timeout: float):
    addresses = _resolve_attachment_addresses(host, timeout)
    address = addresses[0]

    class PinnedHTTPSConnection(HTTPSConnection):
        def connect(self):
            sock = socket.create_connection((address, self.port), self.timeout, self.source_address)
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)

    class PinnedHTTPSHandler(HTTPSHandler):
        def https_open(self, request):
            return self.do_open(PinnedHTTPSConnection, request, context=self._context)

    return build_opener(ProxyHandler({}), _NoRedirect(), PinnedHTTPSHandler())


class AttachmentIngestor:
    """Persist stable attachment references before processing and update them in place."""

    def __init__(self, store: Store, *, max_bytes: int | None = None,
                 max_chars: int = MAX_EXTRACTED_CHARS, timeout: float = 15,
                 knowledge_service: KnowledgeService | None = None,
                 storage_directory: Path | None = None) -> None:
        ceiling = MAX_KNOWLEDGE_FILE_BYTES if knowledge_service else MAX_ATTACHMENT_BYTES
        max_bytes = max_bytes if max_bytes is not None else (
            knowledge_service.settings.max_file_bytes if knowledge_service else MAX_ATTACHMENT_BYTES
        )
        if not 1 <= max_bytes <= ceiling or not 1 <= max_chars <= MAX_EXTRACTED_CHARS:
            raise ValueError("attachment limits exceed the configured safety ceiling")
        self.store, self.max_bytes, self.max_chars, self.timeout = store, max_bytes, max_chars, timeout
        self.knowledge_service = knowledge_service
        self.storage_directory = storage_directory.resolve() if storage_directory else None

    def register_event(self, event: Event) -> Event:
        """Create pending attachment references without waiting for download or OCR."""
        safe_attachments: list[dict[str, Any]] = []
        scope_type, scope_id = self._scope(event)
        now = datetime.now(timezone.utc).isoformat()
        for index, item in enumerate(event.attachments, start=1):
            filename = _safe_filename(item.get("filename"), index)
            content_type = str(item.get("content_type", "application/octet-stream"))[:200]
            attachment_id = "attachment-" + hashlib.sha256(
                f"{event.event_id}\0{index}".encode("utf-8")).hexdigest()[:32]
            raw_url = item.get("url") if isinstance(item.get("url"), str) else None
            payload = {
                "attachment_id": attachment_id, "event_id": event.event_id, "index": index,
                "filename": filename, "content_type": content_type,
                "occurred_at": event.occurred_at, "received_at": now, "status": "received",
                "scope_type": scope_type, "scope_id": scope_id, "actor_id": event.actor_id,
                "url": raw_url, "original_path": None, "size_bytes": 0,
                "content_hash": None, "document_id": None, "sensitive": _looks_sensitive(filename),
                "failure_category": None,
            }
            self._save_payload(payload)
            saved = self._load_payload(attachment_id)
            safe_attachments.append(self._public(saved))
        grant = self.knowledge_service.issue_grant(event) if self.knowledge_service else event.knowledge_grant
        return Event(event.event_id, event.source_type, event.source_id, event.conversation_id,
                     event.actor_id, event.occurred_at, event.content[:12000],
                     safe_attachments, event.capabilities,
                     external_actor_id=event.external_actor_id, knowledge_grant=grant)

    def prepare_originals(self, event: Event, *, on_update: Callable[[Event], None] | None = None) -> Event:
        """Persist originals before Agent execution; indexing may continue afterward."""
        current = event
        for item in event.attachments:
            attachment_id = item.get("attachment_id") if isinstance(item, dict) else None
            if not isinstance(attachment_id, str):
                continue
            payload = self._load_payload(attachment_id)
            if payload.get("status") in {"stored", "processing", "ready"}:
                original_path = payload.get("original_path")
                if isinstance(original_path, str) and Path(original_path).is_file():
                    continue
            try:
                url = payload.get("url")
                if not isinstance(url, str):
                    raise ValueError("attachment URL is missing")
                payload.update(status="downloading", failure_category=None)
                self._save_payload(payload)
                current = self._refresh_event(current, on_update)
                with tempfile.TemporaryDirectory(prefix="company-agent-attachment-") as temp_dir:
                    path = Path(temp_dir) / "download"
                    size_bytes = self._download_to(url, path)
                    if self.knowledge_service:
                        original = self.knowledge_service.preserve_original_file(
                            current, payload["index"], path, payload["filename"])
                    else:
                        original = self._store_local_original(path, payload["filename"])
                    payload.update(original)
                    payload.update(status="stored", size_bytes=size_bytes, failure_category=None)
                    self._save_payload(payload)
                    current = self._refresh_event(current, on_update)
            except Exception as error:
                payload = self._load_payload(attachment_id)
                payload["status"] = "failed"
                payload["failure_category"] = type(error).__name__
                self._save_payload(payload)
                current = self._refresh_event(current, on_update)
        return current

    def process_event(self, event: Event, *, on_update: Callable[[Event], None] | None = None) -> Event:
        current = self.prepare_originals(event, on_update=on_update)
        for item in current.attachments:
            attachment_id = item.get("attachment_id") if isinstance(item, dict) else None
            if not isinstance(attachment_id, str) or not self.knowledge_service:
                continue
            payload = self._load_payload(attachment_id)
            if payload.get("status") in {"ready", "failed"}:
                continue
            original_path = payload.get("original_path")
            if payload.get("status") != "stored" or not isinstance(original_path, str):
                continue
            try:
                path = Path(original_path).resolve(strict=True)
                payload["status"] = "processing"
                payload["failure_category"] = None
                self._save_payload(payload)
                current = self._refresh_event(current, on_update)
                indexed = self.knowledge_service.ingest_file(
                    current, payload["index"], path, payload["filename"])
                payload.update(indexed)
                payload["original_path"] = original_path
                payload["status"] = indexed["status"]
                self._save_payload(payload)
                current = self._refresh_event(current, on_update)
            except Exception as error:
                payload = self._load_payload(attachment_id)
                payload["status"] = "failed"
                payload["failure_category"] = type(error).__name__
                self._save_payload(payload)
                current = self._refresh_event(current, on_update)
        return current

    def enrich_event(self, event: Event) -> Event:
        """Compatibility wrapper for callers that still process an attachment inline."""
        return self.process_event(self.register_event(event))

    def pending_event_ids(self) -> list[str]:
        pending: set[str] = set()
        for row in self.store.fetchall("SELECT event_id,payload FROM attachments"):
            payload = self.store.decode(row["payload"])
            if payload.get("status") in {"received", "downloading", "stored", "processing"}:
                pending.add(row["event_id"])
        return sorted(pending)

    def _store_local_original(self, source: Path, filename: str) -> dict[str, Any]:
        if self.storage_directory is None:
            raise ValueError("durable attachment storage is not configured")
        detected = detect_file(source, filename)
        digest = _sha256_file(source)
        directory = self.storage_directory / digest[:2]
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        target = directory / digest
        if not target.exists():
            partial = directory / f".{digest}.{os.getpid()}.partial"
            try:
                with source.open("rb") as input_file, partial.open("xb") as output:
                    shutil.copyfileobj(input_file, output, length=1024 * 1024)
                partial.chmod(0o600)
                os.replace(partial, target)
            finally:
                partial.unlink(missing_ok=True)
        return {"filename": detected.filename, "content_type": detected.content_type,
                "content_hash": digest, "document_id": None, "size_bytes": target.stat().st_size,
                "sensitive": detected.sensitive, "original_path": str(target), "status": "stored"}

    def _refresh_event(self, event: Event, on_update: Callable[[Event], None] | None) -> Event:
        rows = self.store.fetchall("SELECT payload FROM attachments WHERE event_id=? ORDER BY created_at",
                                   (event.event_id,))
        payloads = [self.store.decode(row["payload"]) for row in rows]
        payloads.sort(key=lambda value: value.get("index", 0))
        updated = replace(event, attachments=[self._public(value) for value in payloads])
        if on_update:
            on_update(updated)
        return updated

    def _scope(self, event: Event) -> tuple[str, str]:
        if self.knowledge_service is not None and hasattr(self.knowledge_service, "source_scope"):
            return self.knowledge_service.source_scope(event)
        if event.source_type in {"qq.group", "qq.guild"}:
            return "group", event.source_id
        if event.source_type in {"qq.direct", "qq.private", "qq.c2c"}:
            return "direct", event.conversation_id
        return event.source_type, event.conversation_id

    @staticmethod
    def _public(payload: dict[str, Any]) -> dict[str, Any]:
        keys = ("attachment_id", "event_id", "filename", "content_type", "occurred_at", "received_at", "status",
                "size_bytes", "content_hash", "document_id", "sensitive", "failure_category")
        return {key: payload[key] for key in keys if key in payload}

    def _load_payload(self, attachment_id: str) -> dict[str, Any]:
        row = self.store.fetchone("SELECT payload FROM attachments WHERE attachment_id=?",
                                  (attachment_id,))
        if row is None:
            raise ValueError("attachment record does not exist")
        return self.store.decode(row["payload"])

    def _save_payload(self, payload: dict[str, Any]) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self.store.transaction() as connection:
            connection.execute(
                "INSERT INTO attachments(attachment_id,event_id,payload,created_at) VALUES (?,?,?,?) "
                "ON CONFLICT(attachment_id) DO UPDATE SET payload=excluded.payload",
                (payload["attachment_id"], payload["event_id"], json.dumps(payload, ensure_ascii=False), now),
            )

    def _download(self, url: str) -> bytes:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("attachment URL must use HTTPS")
        if parsed.port not in (None, 443):
            raise ValueError("attachment URL must use the standard HTTPS port")
        request = Request(url, headers={"User-Agent": "CompanyAgent/1.0", "Accept": "*/*"})
        with _attachment_opener(parsed.hostname, self.timeout).open(request, timeout=self.timeout) as response:
            length = response.headers.get("Content-Length")
            if length and int(length) > self.max_bytes:
                raise ValueError("attachment exceeds the size limit")
            data = response.read(self.max_bytes + 1)
        if len(data) > self.max_bytes:
            raise ValueError("attachment exceeds the size limit")
        return data

    def _download_to(self, url: str, destination: Path) -> int:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("attachment URL must use HTTPS")
        if parsed.port not in (None, 443):
            raise ValueError("attachment URL must use the standard HTTPS port")
        request = Request(url, headers={"User-Agent": "CompanyAgent/1.0", "Accept": "*/*"})
        total = 0
        with _attachment_opener(parsed.hostname, self.timeout).open(request, timeout=self.timeout) as response, \
                destination.open("xb") as output:
            length = response.headers.get("Content-Length")
            if length and int(length) > self.max_bytes:
                raise ValueError("attachment exceeds the size limit")
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > self.max_bytes:
                    raise ValueError("attachment exceeds the size limit")
                output.write(chunk)
        return total


def _looks_sensitive(filename: str) -> bool:
    name = Path(filename).name.lower()
    return name == ".env" or name.startswith(".env.")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def extract_text(data: bytes, filename: str, content_type: str, max_chars: int = MAX_EXTRACTED_CHARS) -> str:
    extension = Path(filename).suffix.lower()
    if extension not in ALLOWED_EXTENSIONS:
        raise ValueError(f"unsupported attachment format: {extension or content_type}")
    if extension in {".txt", ".md", ".json", ".html", ".htm"}:
        text = data.decode("utf-8-sig", errors="replace")
        if extension in {".html", ".htm"}:
            text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", text)
            text = re.sub(r"(?s)<[^>]+>", " ", text)
            text = unescape(text)
    elif extension == ".csv":
        rows = csv.reader(io.StringIO(data.decode("utf-8-sig", errors="replace")))
        text = "\n".join("\t".join(row) for row in rows)
    elif extension in {".docx", ".xlsx"}:
        text = _extract_openxml(data, extension)
    elif extension == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise ValueError("PDF extraction requires the optional pypdf package") from exc
        reader = PdfReader(io.BytesIO(data), strict=False)
        text = "\n".join(page.extract_text() or "" for page in reader.pages[:100])
    elif extension in {".png", ".jpg", ".jpeg"}:
        raise ValueError("image attachments require the AnythingLLM OCR knowledge pipeline")
    else:
        raise ValueError("unsupported attachment format")
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())[:max_chars]


def _extract_openxml(data: bytes, extension: str) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = archive.namelist()
        if len(names) > 2000 or sum(info.file_size for info in archive.infolist()) > 50 * 1024 * 1024:
            raise ValueError("Office document expands beyond the safety limit")
        if extension == ".docx":
            source = archive.read("word/document.xml")
            root = ElementTree.fromstring(source)
            return "\n".join(node.text or "" for node in root.iter() if node.tag.endswith("}t"))
        shared: list[str] = []
        if "xl/sharedStrings.xml" in names:
            root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
            shared = ["".join(node.text or "" for node in item.iter() if node.tag.endswith("}t"))
                      for item in root if item.tag.endswith("}si")]
        parts = []
        for name in sorted(n for n in names if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n)):
            root = ElementTree.fromstring(archive.read(name))
            for cell in root.iter():
                if not cell.tag.endswith("}c"):
                    continue
                value = next((child.text or "" for child in cell if child.tag.endswith("}v")), "")
                if cell.attrib.get("t") == "s" and value.isdigit() and int(value) < len(shared):
                    value = shared[int(value)]
                if value:
                    parts.append(value)
        return "\t".join(parts)


def _safe_filename(value: object, index: int) -> str:
    name = Path(str(value or f"attachment-{index}")).name[:180]
    return name if name not in {"", ".", ".."} else f"attachment-{index}"
