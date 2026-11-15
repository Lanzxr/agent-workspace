from __future__ import annotations

import csv
import io
import json
import re
import socket
import zipfile
from dataclasses import asdict, dataclass
from html import unescape
from ipaddress import ip_address
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4
from xml.etree import ElementTree

from .models import Event
from .store import Store

MAX_ATTACHMENT_BYTES = 12 * 1024 * 1024
MAX_EXTRACTED_CHARS = 40_000
ALLOWED_EXTENSIONS = {".txt", ".md", ".csv", ".json", ".html", ".htm", ".docx", ".xlsx", ".pdf"}


@dataclass(frozen=True)
class AttachmentRecord:
    attachment_id: str
    event_id: str
    filename: str
    content_type: str
    status: str
    extracted_chars: int
    error: str | None = None


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("attachment redirects are not permitted")


class AttachmentIngestor:
    """Download and extract bounded text from supported QQ attachment formats."""

    def __init__(self, store: Store, *, max_bytes: int = MAX_ATTACHMENT_BYTES,
                 max_chars: int = MAX_EXTRACTED_CHARS, timeout: float = 15) -> None:
        if not 1 <= max_bytes <= MAX_ATTACHMENT_BYTES or not 1 <= max_chars <= MAX_EXTRACTED_CHARS:
            raise ValueError("attachment limits exceed the configured safety ceiling")
        self.store, self.max_bytes, self.max_chars, self.timeout = store, max_bytes, max_chars, timeout

    def enrich_event(self, event: Event) -> Event:
        if not event.attachments:
            return event
        extracts: list[str] = []
        extracted_total = 0
        for index, item in enumerate(event.attachments, start=1):
            filename = _safe_filename(item.get("filename"), index)
            content_type = str(item.get("content_type", "application/octet-stream"))[:200]
            try:
                url = item.get("url")
                if not isinstance(url, str):
                    raise ValueError("attachment URL is missing")
                data = self._download(url)
                text = extract_text(data, filename, content_type, self.max_chars - extracted_total)
                status, error = "ready", None
                if text.strip():
                    extracts.append(f"[附件 {filename}]\n{text}")
                    extracted_total += len(text)
            except Exception as exc:
                detail = re.sub(r"https?://\S+", "[URL]", str(exc))
                text, status, error = "", "failed", f"{type(exc).__name__}: {detail[:300]}"
            record = AttachmentRecord(f"attachment-{uuid4().hex}", event.event_id, filename,
                                      content_type, status, len(text), error)
            self._save(record, {**asdict(record), "text": text})
        content = (event.content[:self.max_chars] + ("\n\n" + "\n\n".join(extracts) if extracts else ""))
        return Event(event.event_id, event.source_type, event.source_id, event.conversation_id,
                     event.actor_id, event.occurred_at, content[:self.max_chars],
                     event.attachments, event.capabilities, external_actor_id=event.external_actor_id)

    def _download(self, url: str) -> bytes:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("attachment URL must use HTTPS")
        if parsed.port not in (None, 443):
            raise ValueError("attachment URL must use the standard HTTPS port")
        try:
            address = ip_address(parsed.hostname)
        except ValueError:
            address = None
        if address and (not address.is_global):
            raise ValueError("private or reserved attachment hosts are not permitted")
        if not address:
            for result in socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM):
                if not ip_address(result[4][0]).is_global:
                    raise ValueError("attachment host resolves to a private or reserved address")
        request = Request(url, headers={"User-Agent": "CompanyAgent/1.0", "Accept": "*/*"})
        with build_opener(_NoRedirect).open(request, timeout=self.timeout) as response:
            length = response.headers.get("Content-Length")
            if length and int(length) > self.max_bytes:
                raise ValueError("attachment exceeds the size limit")
            data = response.read(self.max_bytes + 1)
        if len(data) > self.max_bytes:
            raise ValueError("attachment exceeds the size limit")
        return data

    def _save(self, record: AttachmentRecord, payload: dict) -> None:
        now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()
        with self.store.transaction() as connection:
            connection.execute("INSERT INTO attachments(attachment_id,event_id,payload,created_at) VALUES (?,?,?,?)",
                               (record.attachment_id, record.event_id,
                                json.dumps(payload, ensure_ascii=False), now))


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
