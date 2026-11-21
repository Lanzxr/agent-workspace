from __future__ import annotations

from dataclasses import dataclass, field
from ipaddress import IPv4Address, ip_address
from pathlib import Path
from urllib.parse import urlsplit

from ..ports import DEFAULT_KNOWLEDGE_API_PORT, resolve_port_settings
from ..runtime_config import RuntimeConfig

MAX_KNOWLEDGE_FILE_BYTES = 500 * 1024 * 1024


@dataclass(frozen=True)
class KnowledgeSettings:
    base_url: str
    api_key: str = field(repr=False)
    grant_secret: str = field(repr=False)
    admin_user_ids: frozenset[str] = frozenset()
    max_file_bytes: int = MAX_KNOWLEDGE_FILE_BYTES
    parse_timeout_seconds: float = 180
    query_api_port: int = DEFAULT_KNOWLEDGE_API_PORT
    query_api_host: str = "127.0.0.1"
    query_api_key: str = field(default="", repr=False)
    workspace_prefix: str = "company-agent"

    def __post_init__(self) -> None:
        parsed = urlsplit(self.base_url)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment):
            raise ValueError("ANYTHINGLLM_BASE_URL must be an HTTP(S) base URL without credentials or query")
        if not self.api_key.strip() or any(char in self.api_key for char in "\r\n\x00"):
            raise ValueError("ANYTHINGLLM_API_KEY must be a single-line value")
        if len(self.grant_secret.encode("utf-8")) < 32 or any(char in self.grant_secret for char in "\r\n\x00"):
            raise ValueError("COMPANY_AGENT_KNOWLEDGE_GRANT_SECRET must contain at least 32 bytes")
        if not 1 <= self.max_file_bytes <= MAX_KNOWLEDGE_FILE_BYTES:
            raise ValueError("knowledge file limit must be between 1 byte and 500 MiB")
        if not 1 <= self.parse_timeout_seconds <= 1800:
            raise ValueError("AnythingLLM request timeout must be between 1 and 1800 seconds")
        if (not self.workspace_prefix.strip() or len(self.workspace_prefix) > 32
                or not self.workspace_prefix.replace("-", "").isalnum()):
            raise ValueError("COMPANY_AGENT_KNOWLEDGE_WORKSPACE_PREFIX must contain only letters, numbers, and hyphens")
        if not 1 <= self.query_api_port <= 65535:
            raise ValueError("knowledge query API port must be between 1 and 65535")
        try:
            host_address = ip_address(self.query_api_host)
        except ValueError as error:
            raise ValueError("COMPANY_AGENT_KNOWLEDGE_API_HOST must be an IPv4 address") from error
        if not isinstance(host_address, IPv4Address):
            raise ValueError("COMPANY_AGENT_KNOWLEDGE_API_HOST must be an IPv4 address")
        if any(char in self.query_api_key for char in "\r\n\x00"):
            raise ValueError("COMPANY_AGENT_KNOWLEDGE_API_KEY must be a single-line value")
        if not host_address.is_loopback and len(self.query_api_key.encode("utf-8")) < 32:
            raise ValueError("COMPANY_AGENT_KNOWLEDGE_API_KEY is required for non-loopback binding")

    @classmethod
    def load_optional(cls, path: Path) -> KnowledgeSettings | None:
        values = RuntimeConfig._parse_env(path)
        legacy = [key for key in ("RAGFLOW_BASE_URL", "RAGFLOW_API_KEY", "RAGFLOW_DATASET_ID")
                  if values.get(key, "").strip()]
        if legacy:
            raise ValueError("legacy RAGFlow configuration is no longer supported; migrate to AnythingLLM settings")
        ports = resolve_port_settings(values)
        effective_values = dict(values)
        if not (effective_values.get("ANYTHINGLLM_BASE_URL") or "").strip() and (
            (effective_values.get("COMPANY_AGENT_ANYTHINGLLM_PORT") or "").strip()
            or (effective_values.get("COMPANY_AGENT_PORT_BASE") or "").strip()
        ):
            effective_values["ANYTHINGLLM_BASE_URL"] = ports.anythingllm_url

        required = ("ANYTHINGLLM_BASE_URL", "ANYTHINGLLM_API_KEY",
                    "COMPANY_AGENT_KNOWLEDGE_GRANT_SECRET")
        supplied = [bool(effective_values.get(key, "").strip()) for key in required]
        if not any(supplied):
            return None
        if not all(supplied):
            missing = [key for key, present in zip(required, supplied) if not present]
            raise ValueError("incomplete knowledge configuration; missing " + ", ".join(missing))
        try:
            max_bytes = int(values.get("COMPANY_AGENT_KNOWLEDGE_MAX_FILE_BYTES", str(MAX_KNOWLEDGE_FILE_BYTES)))
            parse_timeout = float(values.get("ANYTHINGLLM_REQUEST_TIMEOUT_SECONDS", "180"))
            query_api_port = ports.knowledge_api_port
            query_api_host = ports.knowledge_api_host
        except ValueError as error:
            raise ValueError("knowledge file limit, parse timeout, and API port must be numeric") from error
        admins = frozenset(item.strip() for item in values.get("COMPANY_AGENT_KNOWLEDGE_ADMIN_USER_IDS", "").split(",")
                           if item.strip())
        return cls(effective_values["ANYTHINGLLM_BASE_URL"].strip().rstrip("/"),
                   effective_values["ANYTHINGLLM_API_KEY"].strip(),
                   effective_values["COMPANY_AGENT_KNOWLEDGE_GRANT_SECRET"], admins, max_bytes, parse_timeout,
                   query_api_port, query_api_host, values.get("COMPANY_AGENT_KNOWLEDGE_API_KEY", ""),
                   values.get("COMPANY_AGENT_KNOWLEDGE_WORKSPACE_PREFIX", "company-agent").strip())
