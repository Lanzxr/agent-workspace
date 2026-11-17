from __future__ import annotations

import re


MAX_FINAL_REPLY_CHARS = 1500
_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def final_reply_markers(token: str) -> tuple[str, str]:
    if not isinstance(token, str) or not _TOKEN_PATTERN.fullmatch(token):
        raise ValueError("final reply token is invalid")
    return (
        f"<!-- COMPANY_AGENT_FINAL_REPLY_BEGIN:{token} -->",
        f"<!-- COMPANY_AGENT_FINAL_REPLY_END:{token} -->",
    )


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
