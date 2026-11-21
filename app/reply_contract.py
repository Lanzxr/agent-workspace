from __future__ import annotations

import json
import re
from pathlib import PurePosixPath


MAX_FINAL_REPLY_CHARS = 1500
MAX_DELIVERABLE_FILES = 5
_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_THINKING_CLOSED_RE = re.compile(r"<thinking>.*?</thinking>", re.DOTALL | re.IGNORECASE)
_THINKING_TRAILING_RE = re.compile(r"<thinking>.*$", re.DOTALL | re.IGNORECASE)
_DSML_FUNCTION_BLOCK_RE = re.compile(
    r"<[\u007c\uff5c]+DSML[\u007c\uff5c]+function_calls>.*?(?:</[\u007c\uff5c]+DSML[\u007c\uff5c]+function_calls>|$)",
    re.DOTALL | re.IGNORECASE,
)
_DSML_TAG_RE = re.compile(r"</?[\u007c\uff5c]+DSML[\u007c\uff5c]*[^>]*>?", re.IGNORECASE)
_MALFORMED_MODEL_MARKUP_RE = re.compile(
    r"</?[\u007c\uff5c]+DSML[\u007c\uff5c]*|</?(?:tool_call|function_calls|invoke)\b",
    re.IGNORECASE,
)


def has_malformed_model_markup(text: str) -> bool:
    """Return True when raw model output leaked internal tool-call/DSML delimiters or unclosed <thinking>."""
    if not text:
        return False
    if _MALFORMED_MODEL_MARKUP_RE.search(text):
        return True
    without_closed_thinking = _THINKING_CLOSED_RE.sub("", text)
    return bool(_THINKING_TRAILING_RE.search(without_closed_thinking))


def strip_thinking_blocks(text: str) -> str:
    """Remove raw <thinking> blocks and leaked model DSML delimiters from persisted output snippets."""
    cleaned = _THINKING_CLOSED_RE.sub("", text or "")
    cleaned = _THINKING_TRAILING_RE.sub("", cleaned)
    cleaned = _DSML_FUNCTION_BLOCK_RE.sub("", cleaned)
    cleaned = _DSML_TAG_RE.sub("", cleaned)
    return cleaned.strip()


def final_reply_markers(token: str) -> tuple[str, str]:
    if not isinstance(token, str) or not _TOKEN_PATTERN.fullmatch(token):
        raise ValueError("final reply token is invalid")
    return (
        f"<!-- COMPANY_AGENT_FINAL_REPLY_BEGIN:{token} -->",
        f"<!-- COMPANY_AGENT_FINAL_REPLY_END:{token} -->",
    )


def deliverable_files_markers(token: str) -> tuple[str, str]:
    if not isinstance(token, str) or not _TOKEN_PATTERN.fullmatch(token):
        raise ValueError("deliverable files token is invalid")
    return (
        f"<!-- COMPANY_AGENT_FILES_BEGIN:{token} -->",
        f"<!-- COMPANY_AGENT_FILES_END:{token} -->",
    )


def has_deliverable_files_block(text: str, token: str) -> bool:
    begin, end = deliverable_files_markers(token)
    if text.count(begin) != 1 or text.count(end) != 1:
        return False
    return text.index(end) >= text.index(begin) + len(begin)


def _normalize_relative_deliverable(raw_value: str) -> str | None:
    candidate = raw_value.strip().strip("`\"'")
    if not candidate or "\\" in candidate or ":" in candidate or candidate.startswith("/"):
        return None
    posix = PurePosixPath(candidate)
    if posix.is_absolute() or any(part in {"", ".", ".."} for part in posix.parts):
        return None
    return posix.as_posix()


def extract_deliverable_files(text: str, token: str) -> list[str]:
    """Parse declared workspace-relative deliverable file paths inside COMPANY_AGENT_FILES markers."""
    begin, end = deliverable_files_markers(token)
    if text.count(begin) != 1 or text.count(end) != 1:
        return []
    start = text.index(begin) + len(begin)
    stop = text.index(end)
    if stop <= start:
        return []
    inner = text[start:stop].strip()
    if not inner:
        return []
    raw_items: list[str] = []
    if inner.startswith("["):
        try:
            decoded = json.loads(inner)
            if isinstance(decoded, list):
                raw_items = [str(item) for item in decoded if isinstance(item, str)]
        except ValueError:
            raw_items = []
    else:
        for line in inner.splitlines():
            cleaned = line.strip()
            if cleaned.startswith(("- ", "* ")):
                cleaned = cleaned[2:].strip()
            if cleaned:
                raw_items.append(cleaned)
    paths: list[str] = []
    seen: set[str] = set()
    for raw in raw_items:
        normalized = _normalize_relative_deliverable(raw)
        if normalized and normalized not in seen:
            seen.add(normalized)
            paths.append(normalized)
            if len(paths) >= MAX_DELIVERABLE_FILES:
                break
    return paths


def extract_final_reply(text: str, token: str) -> str | None:
    begin, end = final_reply_markers(token)
    if text.count(begin) != 1 or text.count(end) != 1:
        return None
    start = text.index(begin) + len(begin)
    stop = text.index(end)
    if stop <= start:
        return None
    reply = text[start:stop].strip()
    if not reply or len(reply) > MAX_FINAL_REPLY_CHARS:
        return None
    return reply
