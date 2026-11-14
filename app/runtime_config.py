from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

DSH_VERSION = "0.2.2-rc.1"
POLICY_VERSION = "workspace-dev-v1"


@dataclass(frozen=True)
class RuntimeConfig:
    model_url: str
    model_name: str
    api_key: str = field(repr=False)
    dsh_command: tuple[str, ...] = ()
    source_path: Path | None = None

    @classmethod
    def load(cls, path: Path) -> "RuntimeConfig":
        if not path.is_file():
            raise ValueError(f"runtime configuration not found: {path}")
        values = cls._parse_env(path)
        url = (values.get("AGENT_MODEL_URL") or values.get("DEEPSEEK_BASE_URL") or "").strip().rstrip("/")
        key = (values.get("AGENT_MODEL_API_KEY") or values.get("DEEPSEEK_API_KEY") or "").strip()
        model = (values.get("AGENT_MODEL_NAME") or "").strip()
        if not url or not key or not model:
            raise ValueError("AGENT_MODEL_URL, AGENT_MODEL_API_KEY and AGENT_MODEL_NAME are required")
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError("model URL must be an HTTP(S) base URL without embedded credentials")
        if parsed.query or parsed.fragment:
            raise ValueError("model URL must not contain a query or fragment")
        if url.endswith("/chat/completions"):
            url = url.removesuffix("/chat/completions")
        if any(character in key + model for character in "\r\n\x00"):
            raise ValueError("model name and API key must be single-line values")
        command: tuple[str, ...] = ()
        if values.get("AGENT_DSH_COMMAND"):
            try:
                decoded = json.loads(values["AGENT_DSH_COMMAND"])
            except json.JSONDecodeError as error:
                raise ValueError("AGENT_DSH_COMMAND must be a JSON argument array") from error
            if not isinstance(decoded, list) or not decoded or any(not isinstance(arg, str) or not arg for arg in decoded):
                raise ValueError("AGENT_DSH_COMMAND must be a non-empty JSON string array")
            command = tuple(decoded)
        return cls(url, model, key, command, path.expanduser().resolve())

    @staticmethod
    def _parse_env(path: Path) -> dict[str, str]:
        values: dict[str, str] = {}
        for line_number, raw_line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), start=1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            if "=" not in line:
                raise ValueError(f"invalid runtime configuration at line {line_number}")
            name, raw_value = line.split("=", 1)
            name = name.strip()
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                raise ValueError(f"invalid runtime configuration key at line {line_number}")
            value = raw_value.strip()
            if value.startswith(("'", '"')):
                quote = value[0]
                escaped = False
                end = None
                for index, character in enumerate(value[1:], start=1):
                    if character == quote and (quote == "'" or not escaped):
                        end = index
                        break
                    if character == "\\" and quote == '"' and not escaped:
                        escaped = True
                    else:
                        escaped = False
                if end is None or value[end + 1:].strip() and not value[end + 1:].lstrip().startswith("#"):
                    raise ValueError(f"invalid quoted runtime value at line {line_number}")
                value = value[1:end]
            else:
                value = re.sub(r"\s+#.*$", "", value).rstrip()
            values[name] = value
        return values

    def provider_patch(self) -> list[dict[str, object]]:
        return [
            {"id": "agent-default-model", "config": {"provider": "company-agent", "model": self.model_name}},
            {"id": "llm-pi-ai", "config": {"providers": {"company-agent": {
                "apiKeyEnv": "COMPANY_AGENT_MODEL_API_KEY",
                "api": "openai-completions",
                "baseURL": self.model_url,
                "compat": {"supportsDeveloperRole": False, "maxTokensField": "max_tokens"},
                "models": [{"id": self.model_name, "contextWindow": 128000, "maxTokens": 4096}],
            }}}},
            {"id": "llm-deepseek", "disabled": True},
            {"id": "tool-web", "disabled": True},
            {"id": "plugin-manager", "disabled": True},
            {"id": "tool-plugin-manager", "disabled": True},
        ]

    def resolve_command(self) -> tuple[str, ...]:
        if self.dsh_command:
            return self.dsh_command
        installed = shutil.which("dsh")
        if installed:
            return (installed,)
        node = shutil.which("node")
        if not node:
            nodes = sorted(Path.home().glob("node-v24.*-linux-x64/bin/node"))
            node = str(nodes[-1]) if nodes else None
        if node:
            candidates = sorted(Path.home().glob(".npm-company-agent/_npx/*/node_modules/@deepseek-ai/dsh/package.json"))
            for manifest in candidates:
                if json.loads(manifest.read_text(encoding="utf-8")).get("version") == DSH_VERSION:
                    return (node, str(manifest.parent / "lib" / "bin.js"))
        raise ValueError(f"DSH {DSH_VERSION} is not installed; see docs/04-step3-dsh.md")

    def environment(self, *, home: Path, dsh_home: Path, temporary_directory: Path) -> dict[str, str]:
        environment = {name: os.environ[name] for name in ("PATH", "LANG", "LC_ALL", "SystemRoot", "WINDIR")
                       if name in os.environ}
        command = self.resolve_command()
        node_directory = str(Path(command[0]).resolve().parent)
        environment.update({
            "PATH": node_directory + os.pathsep + environment.get("PATH", os.defpath),
            "HOME": str(home), "USERPROFILE": str(home),
            "TMPDIR": str(temporary_directory), "TMP": str(temporary_directory), "TEMP": str(temporary_directory),
            "DSH_HOME": str(dsh_home), "DSH_TELEMETRY_DISABLED": "1",
            "COMPANY_AGENT_MODEL_API_KEY": self.api_key,
        })
        return environment

    def redact(self, text: str) -> str:
        for value in (self.api_key, json.dumps(self.api_key)[1:-1]):
            text = text.replace(value, "[REDACTED]")
        return text
