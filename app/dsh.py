from __future__ import annotations

import hashlib
import json
import math
import mimetypes
import os
import platform
import re
import shlex
import shutil
import stat
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping, Sequence

from .dsh_session import DshSessionMonitor, resolve_node_executable
from .execution_state import private_directory, write_private_json
from .mcp_registry import McpServerSpec, all_mcp_secret_env_names
from .models import ExecutionRequest, ExecutionResult
from .reply_contract import (
    MAX_DELIVERABLE_FILES,
    deliverable_files_markers,
    extract_deliverable_files,
    extract_final_reply,
    final_reply_markers,
    has_deliverable_files_block,
    has_malformed_model_markup,
    strip_thinking_blocks,
)
from .process_runner import ProcessResult, run_process
from .runtime_config import DSH_VERSION, POLICY_VERSION, RuntimeConfig
from .workspace import Workspace, WorkspaceManager
from .workspace_retention import touch_workspace_files

_DELIVERABLE_AUTO_EXTENSIONS = frozenset({
    ".xlsx", ".xls", ".csv", ".pdf", ".docx", ".doc", ".pptx", ".ppt", ".png", ".jpg", ".jpeg", ".zip"
})
_MAX_DELIVERABLE_BYTES = 50 * 1024 * 1024


_DISABLED_DSH_PLUGINS = (
    "tool-fs", "tool-fs-search", "tool-bash-persistent", "tool-pwsh",
    "tool-pwsh-persistent", "tool-terminal", "tool-str-replace-editor",
    "tool-working-directory",
)


@dataclass(frozen=True)
class DshTask:
    prompt: str
    expected_artifacts: tuple[str, ...]
    timeout_seconds: float = 3600
    idle_timeout_seconds: float = 600
    compaction_timeout_seconds: float = 600
    compaction_timeout_explicit: bool = False
    soft_wrapup_seconds: float = 300
    soft_wrapup_explicit: bool = False
    max_output_bytes: int = 16_000_000
    max_artifact_bytes: int = 16_000_000
    knowledge_document_ids: tuple[str, ...] = ()
    attachment_ids: tuple[str, ...] = ()
    require_user_reply: bool = False
    resume_session_id: str | None = None

    @classmethod
    def from_request(
        cls,
        request: ExecutionRequest,
        *,
        max_timeout_seconds: float = 7200.0,
        default_timeout_seconds: float = 3600.0,
        default_idle_timeout_seconds: float = 600.0,
        default_compaction_timeout_seconds: float = 600.0,
        default_soft_wrapup_seconds: float = 300.0,
    ) -> "DshTask":
        if request.capability != "dsh.task":
            raise ValueError("DSH Adapter only accepts dsh.task")
        parameters = request.parameters
        allowed = {
            "prompt", "expected_artifacts", "timeout_seconds", "idle_timeout_seconds",
            "compaction_timeout_seconds", "soft_wrapup_seconds", "knowledge_document_ids",
            "attachment_ids", "require_user_reply", "resume_session_id",
        }
        if set(parameters) - allowed:
            raise ValueError("unsupported dsh.task parameters")
        prompt = parameters.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt.encode("utf-8")) > 64000:
            raise ValueError("prompt must contain 1-64000 UTF-8 bytes")
        artifacts = parameters.get("expected_artifacts", [])
        if not isinstance(artifacts, list) or len(artifacts) > 100:
            raise ValueError("expected_artifacts must be a list of at most 100 paths")
        for name in artifacts:
            if (not isinstance(name, str) or not name or "\\" in name or ":" in name
                    or name.startswith("/") or any(part in {"", ".", ".."} for part in name.split("/"))):
                raise ValueError("artifact paths must be relative paths inside artifacts/")
        if len(set(artifacts)) != len(artifacts):
            raise ValueError("expected artifact paths must be unique")
        timeout = parameters.get("timeout_seconds", default_timeout_seconds)
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout) or not 0 < timeout <= max_timeout_seconds):
            raise ValueError(f"timeout_seconds must be in (0, {max_timeout_seconds:g}]")
        idle_timeout = parameters.get("idle_timeout_seconds", default_idle_timeout_seconds)
        if (isinstance(idle_timeout, bool) or not isinstance(idle_timeout, (int, float))
                or not math.isfinite(idle_timeout) or not 0 < idle_timeout <= max_timeout_seconds):
            raise ValueError(f"idle_timeout_seconds must be in (0, {max_timeout_seconds:g}]")
        compaction_timeout = parameters.get("compaction_timeout_seconds", default_compaction_timeout_seconds)
        if (isinstance(compaction_timeout, bool) or not isinstance(compaction_timeout, (int, float))
                or not math.isfinite(compaction_timeout) or not 0 < compaction_timeout <= max_timeout_seconds):
            raise ValueError(f"compaction_timeout_seconds must be in (0, {max_timeout_seconds:g}]")
        max_wrapup = min(600.0, max_timeout_seconds)
        soft_wrapup = parameters.get("soft_wrapup_seconds", default_soft_wrapup_seconds)
        if (isinstance(soft_wrapup, bool) or not isinstance(soft_wrapup, (int, float))
                or not math.isfinite(soft_wrapup) or not 0 <= soft_wrapup <= max_wrapup):
            raise ValueError(f"soft_wrapup_seconds must be in [0, {max_wrapup:g}]")
        document_ids = parameters.get("knowledge_document_ids", [])
        if (not isinstance(document_ids, list) or len(document_ids) > 20
                or any(not isinstance(value, str) or not value or len(value) > 200 for value in document_ids)
                or len(set(document_ids)) != len(document_ids)):
            raise ValueError("knowledge_document_ids must contain at most twenty unique IDs")
        attachment_ids = parameters.get("attachment_ids", [])
        if (not isinstance(attachment_ids, list) or len(attachment_ids) > 20
                or any(not isinstance(value, str) or not value or len(value) > 200 for value in attachment_ids)
                or len(set(attachment_ids)) != len(attachment_ids)):
            raise ValueError("attachment_ids must contain at most twenty unique IDs")
        if (document_ids or attachment_ids) and len(prompt.encode("utf-8")) > 63_000:
            raise ValueError("prompt leaves too little space for attached-file references")
        require_user_reply = parameters.get("require_user_reply", False)
        if not isinstance(require_user_reply, bool):
            raise ValueError("require_user_reply must be a boolean")
        resume_session_id = parameters.get("resume_session_id")
        if resume_session_id is not None:
            if (not isinstance(resume_session_id, str)
                    or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", resume_session_id)):
                raise ValueError("resume_session_id must be a safe session identifier")
        return cls(
            prompt, tuple(artifacts), float(timeout),
            idle_timeout_seconds=float(idle_timeout),
            compaction_timeout_seconds=float(compaction_timeout),
            compaction_timeout_explicit="compaction_timeout_seconds" in parameters,
            soft_wrapup_seconds=float(soft_wrapup),
            soft_wrapup_explicit="soft_wrapup_seconds" in parameters,
            knowledge_document_ids=tuple(document_ids),
            attachment_ids=tuple(attachment_ids),
            require_user_reply=require_user_reply,
            resume_session_id=resume_session_id,
        )


class _LiveDshTrace:
    """Monitor DSH NDJSON lines in real time and append sanitized events to logs/dsh/."""

    _ACTIVITY_TYPES = frozenset({"session", "status", "tool_call", "tool_result", "thinking", "text", "final"})

    def __init__(self, manager: WorkspaceManager, workspace_id: str, trace_name: str,
                 cwd: Path, redact: Callable[[str], str]) -> None:
        self.manager = manager
        self.workspace_id = workspace_id
        self.trace_name = trace_name
        self.cwd = cwd.resolve()
        self.redact = redact
        self._lock = threading.Lock()
        self.session_id: str | None = None
        self.tools: set[str] = set()
        self.completed_steps = 0
        self.completed_tools = 0
        self.saw_final = False
        self.event_count = 0

    def progress_counts(self) -> tuple[int, int]:
        with self._lock:
            return self.completed_steps, self.completed_tools

    def record_external_event(self, event: dict[str, Any]) -> None:
        with self._lock:
            self.manager.append_log(
                self.workspace_id,
                self.trace_name,
                self.redact(json.dumps(event, ensure_ascii=False) + "\n"),
            )

    def on_line(self, raw_line: bytes) -> bool:
        if not raw_line.strip():
            return False
        try:
            event = json.loads(raw_line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return False
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            return False
        kind = event["type"]
        if kind not in self._ACTIVITY_TYPES:
            return False
        with self._lock:
            if kind == "session":
                session_id = event.get("sessionId")
                event_cwd = event.get("cwd")
                if isinstance(session_id, str) and session_id:
                    try:
                        if isinstance(event_cwd, str) and Path(event_cwd).resolve() == self.cwd:
                            self.session_id = session_id
                    except OSError:
                        pass
            elif kind == "tool_call":
                tool = event.get("tool")
                if isinstance(tool, str) and tool:
                    self.tools.add(tool)
            elif kind == "tool_result":
                self.completed_tools += 1
            elif kind == "status" and event.get("phase") == "step_end":
                self.completed_steps += 1
            elif kind == "final":
                self.saw_final = True
            self.event_count += 1
            if kind not in {"thinking", "text"}:
                self.manager.append_log(
                    self.workspace_id,
                    self.trace_name,
                    self.redact(json.dumps(event, ensure_ascii=False) + "\n"),
                )
        return True


class DshAdapter:
    """Drive the official headless transport; the control plane owns task state."""

    def __init__(self, config: RuntimeConfig, manager: WorkspaceManager, runtime_directory: Path,
                 *, bubblewrap_executable: Path | None = None,
                 web_search_enabled: Callable[[], bool] | None = None,
                 mcp_servers_provider: Callable[[], Sequence[McpServerSpec]] | None = None,
                 mcp_secrets_provider: Callable[[], Mapping[str, str]] | None = None) -> None:
        self.config = config
        self.manager = manager
        self.runtime_directory = private_directory(runtime_directory.resolve())
        self.private_data_directory = self.runtime_directory.parent
        self.bubblewrap_executable = bubblewrap_executable
        self.web_search_enabled = web_search_enabled
        self.mcp_servers_provider = mcp_servers_provider
        self.mcp_secrets_provider = mcp_secrets_provider
        if not manager.base_directory.is_relative_to(self.private_data_directory):
            raise ValueError("Workspace and DSH runtime data must share one private data directory")
        if self.runtime_directory.is_relative_to(manager.base_directory):
            raise ValueError("DSH configuration and receipts must be outside Agent workspaces")

    def _refresh_config(self) -> RuntimeConfig:
        """Reload RuntimeConfig from source_path when valid on disk so console model edits take effect immediately."""
        src = self.config.source_path
        if src is None or not src.is_file():
            return self.config
        try:
            reloaded = RuntimeConfig.load(src)
        except (OSError, ValueError):
            return self.config
        if self.config.dsh_command and not reloaded.dsh_command:
            reloaded = replace(reloaded, dsh_command=self.config.dsh_command)
        self.config = reloaded
        return self.config

    def all_mcp_secrets(self) -> dict[str, str]:
        merged = dict(self.config.mcp_secrets)
        if self.mcp_secrets_provider is not None:
            for k, v in (self.mcp_secrets_provider() or {}).items():
                val = str(v or "").strip()
                if val:
                    merged[str(k)] = val
        return merged

    def _redact(self, text: str) -> str:
        clean = self.config.redact(text)
        for secret in sorted(self.all_mcp_secrets().values(), key=len, reverse=True):
            if secret:
                clean = clean.replace(secret, "[REDACTED]")
        return clean

    def all_mcp_servers(self) -> tuple[McpServerSpec, ...]:
        """Return currently configured MCP servers (from live provider or baseline RuntimeConfig)."""
        if self.mcp_servers_provider is not None:
            return tuple(self.mcp_servers_provider())
        return tuple(self.config.mcp_servers)

    def authorized_mcp_servers(self, workspace_tier: str | None) -> tuple[McpServerSpec, ...]:
        """Filter enabled MCP servers permitted for this workspace tier."""
        return tuple(spec for spec in self.all_mcp_servers() if spec.allows_tier(workspace_tier))

    def _resolve_mcp_client_module_path(self) -> str | None:
        try:
            command = self.config.resolve_command()
        except ValueError:
            return None
        for part in command:
            candidate_bin = Path(part)
            if candidate_bin.name == "bin.js" and len(candidate_bin.parents) >= 3:
                sibling = candidate_bin.parents[2] / "dsh-mcp-client" / "lib" / "index.js"
                if sibling.is_file():
                    return str(sibling)
        return None

    def preflight(self, request: ExecutionRequest, workspace: Workspace) -> DshTask:
        self._refresh_config()
        task = DshTask.from_request(
            request,
            max_timeout_seconds=self.config.dsh_max_timeout_seconds,
            default_timeout_seconds=self.config.dsh_default_timeout_seconds,
            default_idle_timeout_seconds=self.config.dsh_idle_timeout_seconds,
            default_compaction_timeout_seconds=self.config.dsh_compaction_timeout_seconds,
            default_soft_wrapup_seconds=self.config.dsh_soft_wrapup_seconds,
        )
        self.config.resolve_command()
        self._resolve_bubblewrap()
        workspace_root = workspace.root.resolve()
        private_data_root = self.private_data_directory.resolve()
        workspace_base = self.manager.base_directory.resolve()
        if (workspace_root in {private_data_root, workspace_base}
                or private_data_root.is_relative_to(workspace_root)
                or workspace_base.is_relative_to(workspace_root)):
            raise ValueError("Agent Workspace cannot contain private runtime data or other Workspaces")
        if self.config.source_path is not None and self.config.source_path.expanduser().resolve().is_relative_to(workspace_root):
            raise ValueError("runtime configuration cannot be inside an Agent Workspace")
        if os.name != "nt":
            host_home = Path.home().resolve()
            hidden_roots = (Path("/mnt"), Path("/run"), Path("/tmp"))
            if any(workspace_root.is_relative_to(root) for root in hidden_roots):
                raise ValueError("Agent Workspace must be outside /mnt, /run and /tmp for DSH Shell isolation")
            if private_data_root == Path("/") or any(private_data_root.is_relative_to(root) for root in hidden_roots):
                raise ValueError("Company Agent private data must be outside /mnt, /run and /tmp")
            required_system_paths = (Path("/bin"), Path("/dev"), Path("/etc"), Path("/proc"),
                                     Path("/sys"), Path("/usr"))
            if any(path.is_relative_to(private_data_root) for path in required_system_paths):
                raise ValueError("Company Agent private data cannot contain Linux system directories")
            if workspace_root == host_home or host_home.is_relative_to(workspace_root):
                raise ValueError("Agent Workspace cannot contain the WSL user home")
            protected_paths = (".config", ".ssh", ".aws", ".azure", ".gcloud", ".kube", ".docker",
                               ".gnupg", ".local", ".local/share/company-agent", ".git-credentials",
                               ".netrc", ".npmrc", ".pypirc")
            if any((host_home / path).is_relative_to(workspace_root) for path in protected_paths):
                raise ValueError("Agent Workspace cannot contain WSL user configuration or runtime data")
        for name in task.expected_artifacts:
            path = self._artifact_path(workspace, name)
            if path.exists():
                raise ValueError(f"expected artifact already exists; choose a new output path: {name}")
        return task

    def _build_provider_patch(
        self,
        runner_path: Path,
        *,
        web_enabled: bool | None,
        workspace_tier: str = "client_group",
        include_mcp: bool = True,
        config_override: RuntimeConfig | None = None,
    ) -> list[dict[str, Any]]:
        active_cfg = config_override or self.config
        enabled = active_cfg.web_search_enabled if web_enabled is None else web_enabled
        provider_patch = [
            dict(entry) for entry in active_cfg.provider_patch(web_enabled=enabled)
        ]
        plugin_path = Path(__file__).resolve().parent / "dsh_plugins" / "searxng-search.mjs"
        if enabled and active_cfg.web_gate_search_url and active_cfg.web_gate_api_key and plugin_path.is_file():
            provider_patch = [entry for entry in provider_patch if entry.get("id") != "tool-web"]
            provider_patch.extend([
                {"insert": [{"id": "company-agent-searxng", "name": str(plugin_path)}]},
                {"id": "web", "config": {"searchProvider": "company-agent-searxng", "fetchProvider": "http"}},
                {"id": "tool-web", "disabled": False, "config": {"search": True, "fetch": True}},
            ])
        if include_mcp:
            mcp_plugin_path = Path(__file__).resolve().parent / "dsh_plugins" / "controlled-mcp.mjs"
            authorized_mcp = self.authorized_mcp_servers(workspace_tier)
            if authorized_mcp and mcp_plugin_path.is_file():
                mcp_client_module = self._resolve_mcp_client_module_path()
                provider_patch.append({
                    "insert": [
                        {
                            "id": f"company-agent-mcp-{spec.server_id}",
                            "name": str(mcp_plugin_path),
                            "config": spec.to_plugin_config(mcp_client_module_path=mcp_client_module),
                        }
                        for spec in authorized_mcp
                    ]
                })
        provider_patch.extend([
            {"id": "sandbox", "config": {
                "runnerCommand": [str(runner_path)],
                "runnerFailureSignatures": ["company-agent-bwrap:"],
            }},
            *[{"id": plugin, "disabled": True} for plugin in _DISABLED_DSH_PLUGINS],
        ])
        return provider_patch

    @staticmethod
    def _task_output_dir(request: ExecutionRequest) -> str:
        raw = request.work_item_id if re.fullmatch(r"[A-Za-z0-9._-]{1,128}", request.work_item_id or "") else request.execution_id
        return f"work/outputs/{raw}"

    @classmethod
    def _build_execution_contract(
        cls,
        request: ExecutionRequest,
        task: DshTask,
        *,
        web_enabled: bool,
        mcp_servers: Sequence[McpServerSpec] = (),
    ) -> str:
        if web_enabled:
            network_guidance = (
                "The bash sandbox is network-isolated (no external network access); "
                "never run curl, wget, or HTTP requests inside bash. "
                "Use the built-in web_fetch and web_search tools directly for public web pages or APIs "
                "(combine related search terms in one web_search call and only fetch URLs you need to verify)."
            )
        else:
            network_guidance = (
                "The bash sandbox is network-isolated and web tools are disabled for this run; "
                "do not run curl, wget, or external network requests."
            )
        has_explicit_inputs = bool(
            task.knowledge_document_ids
            or task.attachment_ids
            or "Approved reference files staged in this workspace:" in task.prompt
            or "Retained workspace files" in task.prompt
            or "work/inputs/" in task.prompt
            or "work/outputs/" in task.prompt
            or "Prior-step artifact path(s)" in task.prompt
            or "Prior step artifacts created so far:" in task.prompt
        )
        input_guidance = (
            "Read ONLY the explicit input/prior-step paths named in your prompt; do not scan unrelated folders. "
            if has_explicit_inputs
            else "There are no staged input files unless explicitly named in the task; do NOT run ls/find to probe work/, tools/, or legacy artifacts/. "
        )
        output_dir = cls._task_output_dir(request)
        execution_contract = (
            "Execute the task cleanly without exploratory directory scans. "
            f"{input_guidance}"
            "Write each expected artifact under artifacts/, using exactly the relative names listed, "
            "creating parent directories and checking non-empty output in a single bash call without redundant read-back turns. "
            f"When generating user-deliverable files (e.g. .xlsx, .pdf, .docx, .csv, charts), save them inside {output_dir}/ (never use legacy work/out/). "
            "Do not dump large raw datasets, full tables, or long JSON payloads in thinking or chat text; aggregate data via scripts inside the workspace and write only concise summaries and key metrics into the artifact. "
            "Even if blocked by a missing file or tool error, you MUST still write a clear Chinese blocker explanation into the expected artifact file via bash before exiting (never finish with only raw text or <thinking>). "
            "Keep host files, runtime configuration, logs/ and state/ unchanged. "
            "Do not publish messages, access production systems or widen permissions. "
            f"{network_guidance} "
            "Report what actually ran and any failures."
        )
        if mcp_servers:
            mcp_desc = ", ".join(
                f"{spec.server_id} (tools: mcp__{spec.server_id}__*; {spec.description or spec.transport})"
                for spec in mcp_servers
            )
            execution_contract += (
                f" Active controlled MCP server(s) in this session: {mcp_desc}. "
                "Use those mcp__* tools directly when querying external systems instead of curling from bash."
            )
        if task.resume_session_id:
            execution_contract += (
                " You are continuing a multi-step session (--session-id); build directly on prior step context "
                "and existing workspace outputs without re-reading unchanged files."
            )
        if task.require_user_reply:
            begin, end = final_reply_markers(request.execution_id)
            fbegin, fend = deliverable_files_markers(request.execution_id)
            execution_contract += (
                " The final expected artifact must contain exactly one final answer block bounded by these exact "
                f"markers: {begin} and {end}. Only the text inside that block is eligible for delivery to the user. "
                f"If producing downloadable files to send back to the IM chat (such as generated .xlsx reports), declare their workspace-relative paths inside {fbegin} and {fend}."
            )
        else:
            execution_contract += (
                " This is an intermediate milestone stage; record structured findings concisely in the expected artifact "
                "without writing a user final-reply marker block."
            )
        return execution_contract

    @staticmethod
    def _wrapup_reserve(task: DshTask) -> float:
        if task.soft_wrapup_seconds <= 0:
            return 0.0
        if task.soft_wrapup_explicit:
            return min(task.soft_wrapup_seconds, task.timeout_seconds * 0.5) if task.timeout_seconds > task.soft_wrapup_seconds else 0.0
        if task.timeout_seconds < 30.0:
            return 0.0
        return min(task.soft_wrapup_seconds, task.timeout_seconds * 0.2)

    def execute(self, request: ExecutionRequest, workspace: Workspace, task: DshTask, *, lease_fd: int,
                workspace_tier: str | None = None) -> ExecutionResult:
        if request.status != "running":
            raise ValueError("DSH requires a running, control-plane-approved Execution Request")
        self._refresh_config()
        started_epoch = time.time()
        started_at = datetime.now(timezone.utc).isoformat()
        tier = workspace_tier or getattr(workspace, "tier", "client_group") or "client_group"
        authorized_mcp = self.authorized_mcp_servers(tier)
        runtime_home = private_directory(self.runtime_directory / "dsh" / workspace.workspace_id)
        home = private_directory(workspace.root / "cache" / "home")
        temporary_directory = private_directory(workspace.root / "cache" / "tmp")
        runner_path = self._write_sandbox_runner(runtime_home, workspace.root, workspace_tier=tier)
        patch_path = runtime_home / "provider.patch.yml"
        web_enabled = self.web_search_enabled() if self.web_search_enabled is not None else self.config.web_search_enabled
        write_private_json(patch_path, self._build_provider_patch(runner_path, web_enabled=web_enabled, workspace_tier=tier))
        environment = self.config.environment(home=home, dsh_home=runtime_home, temporary_directory=temporary_directory)
        self._apply_runtime_environment(
            environment, web_enabled=web_enabled, workspace_tier=tier, authorized_mcp_servers=authorized_mcp,
        )
        command = self.config.resolve_command()
        version = run_process((*command, "--version"), cwd=workspace.root, env=environment, input_data=b"",
                              timeout_seconds=15, max_output_bytes=65536, lease_fd=lease_fd)
        if (version.error_category or version.returncode != 0
                or version.stdout.decode("utf-8", errors="replace").strip() != DSH_VERSION):
            return ExecutionResult(request.execution_id, "failed", error=f"DSH {DSH_VERSION} is required",
                                   metadata={"error_category": "runtime_version", "started_at": started_at,
                                             "finished_at": datetime.now(timezone.utc).isoformat()})
        self.manager.record_dependency(workspace.workspace_id, "dsh", DSH_VERSION, "official-headless")
        execution_contract = self._build_execution_contract(
            request, task, web_enabled=web_enabled, mcp_servers=authorized_mcp,
        )
        payload = {
            "execution_id": request.execution_id, "workspace_id": workspace.workspace_id,
            "plan_step_id": request.plan_step_id, "prompt": task.prompt,
            "expected_artifacts": list(task.expected_artifacts),
            "execution_contract": execution_contract,
        }
        trace_name = f"dsh/{request.execution_id}.events.jsonl"
        live_trace = _LiveDshTrace(self.manager, workspace.workspace_id, trace_name, workspace.root, self._redact)
        effective_compaction_timeout = (
            task.compaction_timeout_seconds
            if task.compaction_timeout_explicit
            else max(task.idle_timeout_seconds, task.compaction_timeout_seconds)
        )
        session_monitor = DshSessionMonitor(
            runtime_home,
            live_trace,
            compaction_timeout_seconds=effective_compaction_timeout,
            node_executable=resolve_node_executable(command),
        )
        wrapup_reserve = self._wrapup_reserve(task)
        soft_timeout = (task.timeout_seconds - wrapup_reserve) if wrapup_reserve > 0 else None
        if wrapup_reserve > 0:
            graceful_seconds = min(3.0, max(0.2, wrapup_reserve * 0.25))
        else:
            graceful_seconds = 3.0 if task.timeout_seconds >= 5.0 else 0.0
        hard_deadline = time.monotonic() + task.timeout_seconds
        session_args = ("--session-id", task.resume_session_id) if task.resume_session_id else ()
        process = run_process(
            (*command, "--profile", "headless", "--patch", str(patch_path), *session_args, "--json",
             "--permission-mode", "workspace-write", "--approval-policy", "never", "-"),
            cwd=workspace.root, env=environment, input_data=json.dumps(payload).encode("utf-8"),
            timeout_seconds=task.timeout_seconds, max_output_bytes=task.max_output_bytes, lease_fd=lease_fd,
            idle_timeout_seconds=task.idle_timeout_seconds,
            soft_timeout_seconds=soft_timeout,
            graceful_terminate_seconds=graceful_seconds,
            on_stdout_line=live_trace.on_line,
            idle_state_probe=session_monitor.probe,
        )
        session_monitor.probe()
        session_resumed = bool(task.resume_session_id)
        phase1_steps, phase1_tools = live_trace.progress_counts()
        if (task.resume_session_id and process.returncode != 0 and process.error_category is None
                and live_trace.session_id is None and phase1_steps == 0 and phase1_tools == 0):
            # Fallback to a clean fresh session if the previous session cannot be resumed at startup.
            remaining_for_retry = max(1.0, hard_deadline - time.monotonic())
            if remaining_for_retry >= 1.0:
                session_resumed = False
                live_trace = _LiveDshTrace(self.manager, workspace.workspace_id, trace_name,
                                           workspace.root, self._redact)
                session_monitor = DshSessionMonitor(
                    runtime_home,
                    live_trace,
                    compaction_timeout_seconds=effective_compaction_timeout,
                    node_executable=resolve_node_executable(command),
                )
                retry_soft_timeout = (remaining_for_retry - wrapup_reserve) if (
                    wrapup_reserve > 0 and remaining_for_retry > wrapup_reserve
                ) else None
                process = run_process(
                    (*command, "--profile", "headless", "--patch", str(patch_path), "--json",
                     "--permission-mode", "workspace-write", "--approval-policy", "never", "-"),
                    cwd=workspace.root, env=environment, input_data=json.dumps(payload).encode("utf-8"),
                    timeout_seconds=remaining_for_retry, max_output_bytes=task.max_output_bytes, lease_fd=lease_fd,
                    idle_timeout_seconds=min(task.idle_timeout_seconds, remaining_for_retry),
                    soft_timeout_seconds=retry_soft_timeout,
                    graceful_terminate_seconds=graceful_seconds,
                    on_stdout_line=live_trace.on_line,
                    idle_state_probe=session_monitor.probe,
                )
                session_monitor.probe()
                phase1_steps, phase1_tools = live_trace.progress_counts()
        fallback_model_info: dict[str, Any] | None = None
        if self.config.has_fallback_model and process.error_category is None and phase1_tools == 0 and not live_trace.tools:
            probe_events, _, probe_tools, _, probe_err = self._read_protocol(process.stdout, workspace.root)
            if not probe_tools and probe_err in {"upstream_error", "empty_response"}:
                remaining_for_fb = hard_deadline - time.monotonic()
                if remaining_for_fb >= 1.0:
                    primary_err_detail = self._extract_dsh_error_detail(probe_events, process.stderr)
                    fb_cfg = self.config.with_fallback_as_primary()
                    fb_patch_path = runtime_home / "provider.fallback.patch.yml"
                    write_private_json(
                        fb_patch_path,
                        self._build_provider_patch(
                            runner_path, web_enabled=web_enabled, workspace_tier=tier, config_override=fb_cfg,
                        ),
                    )
                    fb_env = fb_cfg.environment(home=home, dsh_home=runtime_home, temporary_directory=temporary_directory)
                    self._apply_runtime_environment(
                        fb_env, web_enabled=web_enabled, workspace_tier=tier, authorized_mcp_servers=authorized_mcp,
                    )
                    session_resumed = False
                    live_trace = _LiveDshTrace(self.manager, workspace.workspace_id, trace_name,
                                               workspace.root, self._redact)
                    session_monitor = DshSessionMonitor(
                        runtime_home,
                        live_trace,
                        compaction_timeout_seconds=effective_compaction_timeout,
                        node_executable=resolve_node_executable(command),
                    )
                    fb_soft_timeout = (remaining_for_fb - wrapup_reserve) if (
                        wrapup_reserve > 0 and remaining_for_fb > wrapup_reserve
                    ) else None
                    process = run_process(
                        (*command, "--profile", "headless", "--patch", str(fb_patch_path), "--json",
                         "--permission-mode", "workspace-write", "--approval-policy", "never", "-"),
                        cwd=workspace.root, env=fb_env, input_data=json.dumps(payload).encode("utf-8"),
                        timeout_seconds=remaining_for_fb, max_output_bytes=task.max_output_bytes, lease_fd=lease_fd,
                        idle_timeout_seconds=min(task.idle_timeout_seconds, remaining_for_fb),
                        soft_timeout_seconds=fb_soft_timeout,
                        graceful_terminate_seconds=graceful_seconds,
                        on_stdout_line=live_trace.on_line,
                        idle_state_probe=session_monitor.probe,
                    )
                    session_monitor.probe()
                    phase1_steps, phase1_tools = live_trace.progress_counts()
                    environment = fb_env
                    fallback_model_info = {
                        "primary_model": self.config.model_name,
                        "fallback_model": fb_cfg.model_name,
                        "fallback_url": fb_cfg.model_url,
                        "primary_error_category": probe_err,
                        **({"primary_error": primary_err_detail} if primary_err_detail else {}),
                    }
        soft_wrapup_info: dict[str, Any] | None = None
        dsh_error_detail: dict[str, str] | None = None
        active_process = process
        remaining = hard_deadline - time.monotonic()
        min_wrapup_budget = 0.5 if task.soft_wrapup_explicit else 5.0
        manifest: list[dict[str, Any]] = []
        deliverable_files: list[dict[str, Any]] = []
        can_timeout_wrapup = (
            process.error_category in {"soft_timeout", "idle_timeout", "compaction_timeout"}
            and live_trace.session_id is not None
            and not live_trace.saw_final
            and task.soft_wrapup_seconds > 0
            and remaining > min_wrapup_budget
            and (process.error_category == "soft_timeout"
                 or phase1_steps > 0
                 or phase1_tools > 0)
        )
        if can_timeout_wrapup and live_trace.session_id is not None:
            wrapup_budget = (
                remaining if process.error_category == "soft_timeout"
                else min(remaining, task.soft_wrapup_seconds if task.soft_wrapup_explicit else max(task.soft_wrapup_seconds, 60.0))
            )
            if process.error_category == "soft_timeout":
                reason_text = "接近总执行时长上限"
            elif process.error_category == "compaction_timeout":
                reason_text = f"上下文压缩超过 {effective_compaction_timeout:g} 秒仍未结束"
            else:
                reason_text = f"连续 {task.idle_timeout_seconds:g} 秒无新进展"
            artifacts_text = ", ".join(f"artifacts/{name}" for name in task.expected_artifacts) or "目标产物文件"
            wrapup_prompt_lines = [
                f"[超时前软收尾] 触发原因：{reason_text}。时间非常紧张，请严格按最小步骤完成收尾：",
                "1) 立即停止新增外部搜索、网页抓取，也不要再遍历目录或更新中间笔记文件；",
                f"2) 直接在当前单步内用一次 bash 调用读取已有上下文/工作区内容，将目前已采集和整理出的阶段性结论（并注明未完成项）直接写入 {artifacts_text}；",
                "3) 写入完成后立即结束并回复。",
            ]
            if task.require_user_reply:
                begin, end = final_reply_markers(request.execution_id)
                fbegin, fend = deliverable_files_markers(request.execution_id)
                wrapup_prompt_lines.append(
                    f"注意：最终产物仍必须严格包含一对起止标记 {begin} 与 {end}，标记内放给用户的中文总结（不超过1500字）；"
                    f"若已生成交付文件，请在同一报告中写入 {fbegin} 与 {fend}。"
                )
            phase2, session_id, tools, output, category = self._run_wrapup_phase(
                command=command,
                runtime_home=runtime_home,
                runner_path=runner_path,
                workspace=workspace,
                environment=environment,
                lease_fd=lease_fd,
                request=request,
                task=task,
                session_id=live_trace.session_id,
                prompt_lines=wrapup_prompt_lines,
                wrapup_budget=wrapup_budget,
                phase1_process=process,
                effective_compaction_timeout=effective_compaction_timeout,
                live_trace=live_trace,
                session_monitor=session_monitor,
            )
            active_process = phase2
            soft_wrapup_info = {
                "triggered": True,
                "reason": process.error_category,
                "session_id": live_trace.session_id,
                "succeeded": category is None,
            }
            if category is not None:
                soft_wrapup_info["phase2_error_category"] = category
            if category is None:
                category, manifest, deliverable_files, diag = self._validate_outputs(
                    workspace, request, task, started_epoch=started_epoch,
                )
                if diag:
                    output = f"{output}\n{diag}" if output else diag
            else:
                category, manifest, deliverable_files, output = self._recover_post_write_artifacts(
                    workspace, request, task, category=category, output=output,
                    phase1_steps=phase1_steps, phase1_tools=phase1_tools,
                    live_trace=live_trace, soft_wrapup_info=soft_wrapup_info,
                    started_epoch=started_epoch,
                )
        else:
            phase1_events, session_id, tools, output, protocol_error = self._read_protocol(process.stdout, workspace.root)
            dsh_error_detail = self._extract_dsh_error_detail(phase1_events, process.stderr)
            session_id = session_id or live_trace.session_id
            tools = tools | live_trace.tools
            raw_final_text = ""
            if phase1_events and phase1_events[-1].get("type") == "final":
                candidate_text = phase1_events[-1].get("text")
                if isinstance(candidate_text, str):
                    raw_final_text = candidate_text
            model_markup_leak = has_malformed_model_markup(raw_final_text)
            if (process.error_category == "soft_timeout" and live_trace.saw_final
                    and protocol_error is None and process.returncode == 0):
                category = None
            else:
                category = process.error_category or protocol_error
                if category == "soft_timeout":
                    category = "timeout"
                if category is None and process.returncode != 0:
                    category = "process_exit"
            if category == "max_tokens":
                val_cat, val_manifest, val_deliverables, val_diag = self._validate_outputs(
                    workspace, request, task, started_epoch=started_epoch,
                )
                if val_cat is None:
                    category = None
                    manifest = val_manifest
                    deliverable_files = val_deliverables
                else:
                    remaining = hard_deadline - time.monotonic()
                    can_max_tokens_wrapup = (
                        session_id is not None
                        and task.soft_wrapup_seconds > 0
                        and remaining > min_wrapup_budget
                    )
                    if can_max_tokens_wrapup and session_id is not None:
                        mt_budget = min(
                            remaining,
                            task.soft_wrapup_seconds if task.soft_wrapup_explicit else max(task.soft_wrapup_seconds, 120.0),
                        )
                        artifacts_text = ", ".join(f"artifacts/{name}" for name in task.expected_artifacts) or "目标产物文件"
                        mt_lines = [
                            "[输出超限自动收尾] 上一轮响应因单次输出达到 token 上限被截断。请严格按最小步骤完成收尾：",
                            "1) 严禁重复运行已完成的查询或长篇复述明细数据；",
                            f"2) 立即用一次 bash 调用将目前已得出的核心汇总结论直接写入 {artifacts_text}（确保文件存在且非空）；",
                            "3) 写入完成后立即结束并回复简短一句话。",
                        ]
                        if task.require_user_reply:
                            begin, end = final_reply_markers(request.execution_id)
                            fbegin, fend = deliverable_files_markers(request.execution_id)
                            mt_lines.append(
                                f"注意：最终产物仍必须严格包含一对起止标记 {begin} 与 {end}，标记内放给用户的中文总结（不超过1500字）；"
                                f"若已生成交付文件，请在同一报告中写入 {fbegin} 与 {fend}。"
                            )
                        phase2, p2_session_id, p2_tools, p2_output, category = self._run_wrapup_phase(
                            command=command,
                            runtime_home=runtime_home,
                            runner_path=runner_path,
                            workspace=workspace,
                            environment=environment,
                            lease_fd=lease_fd,
                            request=request,
                            task=task,
                            session_id=session_id,
                            prompt_lines=mt_lines,
                            wrapup_budget=mt_budget,
                            phase1_process=process,
                            effective_compaction_timeout=effective_compaction_timeout,
                            live_trace=live_trace,
                            session_monitor=session_monitor,
                        )
                        active_process = phase2
                        session_id = p2_session_id or session_id
                        tools = tools | p2_tools
                        if p2_output:
                            output = p2_output
                        soft_wrapup_info = {
                            "triggered": True,
                            "reason": "max_tokens",
                            "session_id": session_id,
                            "succeeded": category is None,
                        }
                        if category is not None:
                            soft_wrapup_info["phase2_error_category"] = category
                        if category is None:
                            category, manifest, deliverable_files, diag_mt = self._validate_outputs(
                                workspace, request, task, started_epoch=started_epoch,
                            )
                            if diag_mt:
                                output = f"{output}\n{diag_mt}" if output else diag_mt
                        else:
                            category, manifest, deliverable_files, output = self._recover_post_write_artifacts(
                                workspace, request, task, category=category, output=output,
                                phase1_steps=phase1_steps, phase1_tools=phase1_tools,
                                live_trace=live_trace, soft_wrapup_info=soft_wrapup_info,
                                started_epoch=started_epoch,
                            )
                    elif val_diag:
                        output = f"{output}\n{val_diag}" if output else val_diag
            elif category is None:
                category, manifest, deliverable_files, diag = self._validate_outputs(
                    workspace, request, task, started_epoch=started_epoch,
                )
                remaining = hard_deadline - time.monotonic()
                can_contract_wrapup = (
                    category in {"artifact_validation", "reply_contract"}
                    and live_trace.session_id is not None
                    and task.soft_wrapup_seconds > 0
                    and remaining > min_wrapup_budget
                    and (phase1_steps > 0 or phase1_tools > 0)
                    and (model_markup_leak or task.timeout_seconds >= 30.0 or task.soft_wrapup_explicit)
                )
                if can_contract_wrapup and live_trace.session_id is not None:
                    initial_category = category
                    contract_budget = min(
                        remaining,
                        task.soft_wrapup_seconds if task.soft_wrapup_explicit else max(task.soft_wrapup_seconds, 120.0),
                    )
                    repair_lines = self._contract_wrapup_prompt_lines(
                        request,
                        task,
                        failed_category=initial_category,
                        diagnostic=diag,
                        model_markup_leak=model_markup_leak,
                    )
                    phase2, p2_session_id, p2_tools, p2_output, category = self._run_wrapup_phase(
                        command=command,
                        runtime_home=runtime_home,
                        runner_path=runner_path,
                        workspace=workspace,
                        environment=environment,
                        lease_fd=lease_fd,
                        request=request,
                        task=task,
                        session_id=live_trace.session_id,
                        prompt_lines=repair_lines,
                        wrapup_budget=contract_budget,
                        phase1_process=process,
                        effective_compaction_timeout=effective_compaction_timeout,
                        live_trace=live_trace,
                        session_monitor=session_monitor,
                    )
                    active_process = phase2
                    session_id = p2_session_id or session_id
                    tools = tools | p2_tools
                    if p2_output:
                        output = p2_output
                    soft_wrapup_info = {
                        "triggered": True,
                        "reason": f"contract_{initial_category}",
                        "session_id": live_trace.session_id,
                        "succeeded": category is None,
                    }
                    if model_markup_leak:
                        soft_wrapup_info["model_markup_leak"] = True
                    if category is not None:
                        soft_wrapup_info["phase2_error_category"] = category
                    if category is None:
                        category, manifest, deliverable_files, diag2 = self._validate_outputs(
                            workspace, request, task, started_epoch=started_epoch,
                        )
                        if diag2:
                            output = f"{output}\n{diag2}" if output else diag2
                    else:
                        category, manifest, deliverable_files, output = self._recover_post_write_artifacts(
                            workspace, request, task, category=category, output=output,
                            phase1_steps=phase1_steps, phase1_tools=phase1_tools,
                            live_trace=live_trace, soft_wrapup_info=soft_wrapup_info,
                            started_epoch=started_epoch,
                        )
                elif diag:
                    output = f"{output}\n{diag}" if output else diag
        if deliverable_files:
            touch_workspace_files(self.manager, workspace.workspace_id,
                                  [item["path"] for item in deliverable_files])
        if soft_wrapup_info is not None:
            soft_wrapup_info["succeeded"] = category is None
        output = strip_thinking_blocks(output)
        redacted_dsh_error: dict[str, str] | None = None
        if category is not None and dsh_error_detail:
            redacted_dsh_error = {
                k: self._redact(str(v))[:300]
                for k, v in dsh_error_detail.items()
                if v
            }
        metadata = {
            "workspace_id": workspace.workspace_id, "cwd": ".", "session_id": session_id,
            "started_at": started_at, "finished_at": datetime.now(timezone.utc).isoformat(),
            "returncode": active_process.returncode, "error_category": category, "tools_used": sorted(tools),
            "stderr_bytes": len(process.stderr) + (len(active_process.stderr) if active_process is not process else 0),
            "runtime_versions": {"dsh": DSH_VERSION, "python": platform.python_version(), "platform": platform.system()},
            "shell_sandbox": "bubblewrap-user-namespace",
            "artifact_manifest": manifest, "policy_version": POLICY_VERSION,
            **({"deliverable_files": deliverable_files} if deliverable_files else {}),
            **({"mcp_servers": [spec.server_id for spec in authorized_mcp]} if authorized_mcp else {}),
            "permission_mode": "workspace-write", "approval_policy": "never",
            "network_policy": "model-provider-only; shell-network-namespace-isolated",
            "limits": {"timeout_seconds": task.timeout_seconds, "idle_timeout_seconds": task.idle_timeout_seconds,
                       "compaction_timeout_seconds": effective_compaction_timeout,
                       "soft_wrapup_seconds": task.soft_wrapup_seconds,
                       "max_output_bytes": task.max_output_bytes, "max_artifact_bytes": task.max_artifact_bytes},
        }
        if soft_wrapup_info is not None:
            metadata["soft_wrapup"] = soft_wrapup_info
        if fallback_model_info is not None:
            metadata["fallback_model"] = fallback_model_info
        if redacted_dsh_error:
            metadata["dsh_error"] = redacted_dsh_error
        if task.resume_session_id:
            metadata["session_resumed"] = session_resumed
        status = "succeeded" if category is None else (
            "timed_out" if category in {"timeout", "idle_timeout", "compaction_timeout", "soft_timeout"} else "failed"
        )
        error_msg: str | None = None
        if category is not None:
            detail_parts = [
                part for part in (
                    (redacted_dsh_error or {}).get("code"),
                    (redacted_dsh_error or {}).get("message"),
                ) if part
            ]
            if detail_parts:
                error_msg = f"DSH execution failed: {category} ({': '.join(detail_parts)[:240]})"
            else:
                error_msg = f"DSH execution failed: {category}"
        return ExecutionResult(
            request.execution_id, status, self._redact(output),
            [f"logs/{trace_name}"],
            [entry["path"] for entry in manifest], error_msg, metadata,
        )

    def _run_wrapup_phase(
        self,
        *,
        command: tuple[str, ...],
        runtime_home: Path,
        runner_path: Path,
        workspace: Workspace,
        environment: dict[str, str],
        lease_fd: int,
        request: ExecutionRequest,
        task: DshTask,
        session_id: str,
        prompt_lines: list[str],
        wrapup_budget: float,
        phase1_process: ProcessResult,
        effective_compaction_timeout: float,
        live_trace: _LiveDshTrace,
        session_monitor: DshSessionMonitor,
    ) -> tuple[ProcessResult, str | None, set[str], str, str | None]:
        """Resume the active DSH session once with web disabled to complete wrap-up outputs."""
        wrapup_patch = runtime_home / "provider.wrapup.patch.yml"
        write_private_json(
            wrapup_patch,
            self._build_provider_patch(runner_path, web_enabled=False, include_mcp=False),
        )
        wrapup_payload = {
            "execution_id": request.execution_id,
            "workspace_id": workspace.workspace_id,
            "plan_step_id": request.plan_step_id,
            "prompt": "\n".join(prompt_lines),
            "expected_artifacts": list(task.expected_artifacts),
            "execution_contract": self._build_execution_contract(request, task, web_enabled=False),
        }
        wrapup_bytes = max(1_000_000, task.max_output_bytes - len(phase1_process.stdout) - len(phase1_process.stderr))
        session_monitor.compaction_timeout_seconds = min(effective_compaction_timeout, wrapup_budget)
        session_monitor.reset_for_resume()
        phase2 = run_process(
            (*command, "--profile", "headless", "--patch", str(wrapup_patch),
             "--session-id", session_id, "--json",
             "--permission-mode", "workspace-write", "--approval-policy", "never", "-"),
            cwd=workspace.root,
            env=environment,
            input_data=json.dumps(wrapup_payload).encode("utf-8"),
            timeout_seconds=wrapup_budget,
            max_output_bytes=wrapup_bytes,
            lease_fd=lease_fd,
            idle_timeout_seconds=min(task.idle_timeout_seconds, wrapup_budget),
            graceful_terminate_seconds=2.0 if wrapup_budget >= 5.0 else 0.0,
            on_stdout_line=live_trace.on_line,
            idle_state_probe=session_monitor.probe,
        )
        session_monitor.probe()
        _, parsed_sid, tools, output, protocol_error = self._read_protocol(phase2.stdout, workspace.root)
        merged_sid = parsed_sid or live_trace.session_id or session_id
        merged_tools = tools | live_trace.tools
        category = phase2.error_category or protocol_error
        if category is None and phase2.returncode != 0:
            category = "process_exit"
        return phase2, merged_sid, merged_tools, output, category

    def _validate_outputs(
        self,
        workspace: Workspace,
        request: ExecutionRequest,
        task: DshTask,
        *,
        started_epoch: float,
    ) -> tuple[str | None, list[dict[str, Any]], list[dict[str, Any]], str]:
        """Validate expected artifacts and optional final user-reply/deliverable contract."""
        try:
            manifest = self._artifact_manifest(workspace, task)
        except (OSError, ValueError) as error:
            return "artifact_validation", [], [], str(error)
        deliverable_files: list[dict[str, Any]] = []
        if task.require_user_reply:
            try:
                reply_found = self._has_valid_user_reply(workspace, manifest, request.execution_id)
            except (OSError, UnicodeError, ValueError):
                reply_found = False
            if not reply_found:
                return "reply_contract", manifest, [], "DSH final artifact is missing a valid user-reply block"
            try:
                deliverable_files = self._collect_deliverable_files(
                    workspace, request, manifest, started_epoch=started_epoch,
                )
            except (OSError, UnicodeError, ValueError) as error:
                return "artifact_validation", manifest, [], f"Deliverable validation failed: {error}"
        return None, manifest, deliverable_files, ""

    def _recover_post_write_artifacts(
        self,
        workspace: Workspace,
        request: ExecutionRequest,
        task: DshTask,
        *,
        category: str,
        output: str,
        phase1_steps: int,
        phase1_tools: int,
        live_trace: _LiveDshTrace,
        soft_wrapup_info: dict[str, Any],
        started_epoch: float,
    ) -> tuple[str | None, list[dict[str, Any]], list[dict[str, Any]], str]:
        """Accept verified wrap-up artifacts if phase2 timed out or compacted right after writing them."""
        if category not in {"timeout", "idle_timeout", "compaction_timeout"} or not task.expected_artifacts:
            return category, [], [], output
        phase2_steps, phase2_tools = live_trace.progress_counts()
        if phase2_steps <= phase1_steps and phase2_tools <= phase1_tools:
            return category, [], [], output
        try:
            candidate_manifest = self._artifact_manifest(workspace, task)
            if not candidate_manifest or not all(entry["bytes"] > 0 for entry in candidate_manifest):
                return category, [], [], output
            deliverables: list[dict[str, Any]] = []
            if task.require_user_reply:
                if not self._has_valid_user_reply(workspace, candidate_manifest, request.execution_id):
                    return category, [], [], output
                deliverables = self._collect_deliverable_files(
                    workspace, request, candidate_manifest, started_epoch=started_epoch,
                )
            soft_wrapup_info["recovered_from_category"] = category
            return None, candidate_manifest, deliverables, (output or "Soft wrap-up recovered verified expected artifacts.")
        except (OSError, UnicodeError, ValueError):
            return category, [], [], output

    @classmethod
    def _contract_wrapup_prompt_lines(
        cls,
        request: ExecutionRequest,
        task: DshTask,
        *,
        failed_category: str,
        diagnostic: str,
        model_markup_leak: bool,
    ) -> list[str]:
        """Build concise repair instructions for an active session that stopped before writing required outputs."""
        artifacts_text = ", ".join(f"artifacts/{name}" for name in task.expected_artifacts) or "目标产物文件"
        output_dir = cls._task_output_dir(request)
        reason_bits = ["上一轮模型输出意外提前中断或工具调用格式异常"] if model_markup_leak else ["上一轮结束时未通过产物契约校验"]
        if failed_category == "reply_contract":
            reason_bits.append("缺少合法的用户最终答复标记或字数超限")
        else:
            reason_bits.append(f"预期产物校验失败（{diagnostic or 'missing artifact'}）")
        lines = [
            f"[未完结自动补全] {'；'.join(reason_bits)}。请严格按最小步骤完成收尾：",
            "1) 严禁重复搜索、重复遍历目录或从头重算已完成的步骤；直接复用当前会话与工作区已有成果（如已生成的交付文件）；",
            f"2) 立即用一次 bash 调用写入或补全 {artifacts_text}（确保文件存在且非空；若此前仅完成部分工作或存在阻塞，也必须将当前进展、{output_dir}/ 中已有产出及未完成原因写入该文件）；",
            "3) 写入并校验通过后立即结束并回复。",
        ]
        if task.require_user_reply:
            begin, end = final_reply_markers(request.execution_id)
            fbegin, fend = deliverable_files_markers(request.execution_id)
            lines.append(
                f"注意：最终产物仍必须严格包含一对起止标记 {begin} 与 {end}，标记内放给用户的中文总结（不超过1500字）；"
                f"若 {output_dir}/ 中已生成需要发群的文件，请在同一报告中写入 {fbegin} 与 {fend}。"
            )
        return lines

    def _apply_runtime_environment(self, environment: dict[str, str], *, web_enabled: bool,
                                   workspace_tier: str,
                                   authorized_mcp_servers: Sequence[McpServerSpec] | None = None) -> None:
        if web_enabled and self.config.web_gate_search_url and self.config.web_gate_api_key:
            environment["COMPANY_AGENT_WEB_GATE_URL"] = self.config.web_gate_search_url
            environment["COMPANY_AGENT_WEB_GATE_API_KEY"] = self.config.web_gate_api_key
        active_mcp = (
            tuple(authorized_mcp_servers)
            if authorized_mcp_servers is not None
            else self.authorized_mcp_servers(workspace_tier)
        )
        merged_secrets = self.all_mcp_secrets()
        allowed_secret_vars: set[str] = set()
        for spec in active_mcp:
            for var_name in spec.referenced_host_env_vars():
                allowed_secret_vars.add(var_name)
                secret_val = merged_secrets.get(var_name) or os.environ.get(var_name, "").strip()
                if secret_val:
                    environment[var_name] = secret_val
        for var_name in all_mcp_secret_env_names(self.all_mcp_servers(), merged_secrets):
            if var_name not in allowed_secret_vars:
                environment.pop(var_name, None)
        shared_root = self.private_data_directory / "shared-tools"
        layers = [shared_root / "common"]
        if workspace_tier in {"internal", "admin"}:
            layers.append(shared_root / "internal")
        bin_paths = [str((layer / "bin").resolve()) for layer in layers if (layer / "bin").is_dir()]
        if bin_paths:
            current_path = environment.get("PATH", "")
            environment["PATH"] = os.pathsep.join([*bin_paths, *([current_path] if current_path else [])])
        python_paths = [str((layer / "python").resolve()) for layer in layers if (layer / "python").is_dir()]
        if python_paths:
            current_python = environment.get("PYTHONPATH", "")
            environment["PYTHONPATH"] = os.pathsep.join([*python_paths, *([current_python] if current_python else [])])

    def _write_sandbox_runner(self, runtime_home: Path, workspace_root: Path, *,
                              workspace_tier: str = "client_group") -> Path:
        executable = self._resolve_bubblewrap()
        host_home = Path.home().resolve()
        workspace_root = workspace_root.resolve()
        private_data_root = self.private_data_directory.resolve()
        mask_candidates = [private_data_root, host_home]
        masked_roots: list[Path] = []
        for root in sorted(set(mask_candidates), key=lambda path: len(path.parts)):
            if not any(root.is_relative_to(parent) for parent in masked_roots):
                masked_roots.append(root)
        insert = [
            "--tmpfs", "/mnt", "--tmpfs", "/run", "--unshare-net",
            "--unsetenv", "COMPANY_AGENT_MODEL_API_KEY",
            "--unsetenv", "COMPANY_AGENT_WEB_GATE_API_KEY",
        ]
        for var_name in all_mcp_secret_env_names(self.all_mcp_servers(), self.all_mcp_secrets()):
            if var_name not in {"COMPANY_AGENT_MODEL_API_KEY", "COMPANY_AGENT_WEB_GATE_API_KEY"}:
                insert.extend(["--unsetenv", var_name])
        for root in masked_roots:
            insert.extend(["--tmpfs", str(root)])
        created_dirs: set[Path] = set()

        def expose(path: Path, *, mode: str) -> None:
            parent_root = next((root for root in masked_roots if path.is_relative_to(root)), None)
            if parent_root is not None:
                relative = path.relative_to(parent_root)
                destination = parent_root
                for part in relative.parts:
                    destination /= part
                    if destination not in created_dirs:
                        insert.extend(["--dir", str(destination)])
                        created_dirs.add(destination)
            insert.extend([mode, str(path), str(path)])

        expose(workspace_root, mode="--bind")
        shared_root = self.private_data_directory / "shared-tools"
        common_tools = shared_root / "common"
        if common_tools.is_dir():
            expose(common_tools.resolve(), mode="--ro-bind")
        if workspace_tier in {"internal", "admin"}:
            internal_tools = shared_root / "internal"
            if internal_tools.is_dir():
                expose(internal_tools.resolve(), mode="--ro-bind")
        config_path = self.config.source_path
        if config_path is not None:
            config_path = config_path.expanduser().resolve()
            hidden_trees = (*masked_roots, Path("/mnt"), Path("/run"), Path("/tmp"))
            if not any(config_path.is_relative_to(root) for root in hidden_trees):
                insert.extend(["--ro-bind", "/dev/null", str(config_path)])
        runner = runtime_home / "workspace-sandbox"
        script = "\n".join([
            "#!/bin/bash",
            "set -euo pipefail",
            f"bwrap={shlex.quote(str(executable))}",
            "if [[ ! -x $bwrap ]]; then echo 'company-agent-bwrap: executable unavailable' >&2; exit 127; fi",
            "args=()",
            "inserted=0",
            "for arg in \"$@\"; do",
            "  if [[ $inserted == 0 && $arg == -- ]]; then",
            f"    args+=({ ' '.join(shlex.quote(value) for value in insert) })",
            "    inserted=1",
            "  fi",
            "  args+=(\"$arg\")",
            "done",
            "if [[ $inserted == 0 ]]; then echo 'company-agent-bwrap: invalid sandbox profile' >&2; exit 127; fi",
            "exec \"$bwrap\" \"${args[@]}\"",
            "",
        ])
        runner.write_text(script, encoding="utf-8")
        if os.name != "nt":
            runner.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
        return runner

    def _resolve_bubblewrap(self) -> Path:
        executable = self.bubblewrap_executable
        if executable is None:
            discovered = shutil.which("bwrap")
            executable = Path(discovered) if discovered else Path.home() / ".local/opt/bubblewrap/usr/bin/bwrap"
        executable = executable.expanduser().resolve()
        if not executable.is_file() or (os.name != "nt" and not os.access(executable, os.X_OK)):
            raise ValueError("Bubblewrap is required for DSH Shell isolation; see docs/04-step3-dsh.md")
        return executable

    @staticmethod
    def _read_protocol(data: bytes, cwd: Path) -> tuple[list[dict[str, Any]], str | None, set[str], str, str | None]:
        events: list[dict[str, Any]] = []
        session_id = None
        tools: set[str] = set()
        calls: set[str] = set()
        results: set[str] = set()
        final = None
        try:
            for line in data.decode("utf-8").splitlines():
                if not line.strip():
                    continue
                event = json.loads(line)
                if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                    raise ValueError("invalid event")
                kind = event["type"]
                if kind == "thinking":
                    continue
                if final is not None:
                    raise ValueError("events after final")
                if kind == "session":
                    if session_id or not isinstance(event.get("sessionId"), str) or not event["sessionId"]:
                        raise ValueError("invalid session")
                    if not isinstance(event.get("cwd"), str) or Path(event["cwd"]).resolve() != cwd.resolve():
                        raise ValueError("wrong execution cwd")
                    session_id = event["sessionId"]
                elif session_id is None:
                    raise ValueError("missing session")
                elif kind == "tool_call":
                    call = event.get("callId")
                    if not isinstance(call, str) or not call or call in calls or not isinstance(event.get("tool"), str):
                        raise ValueError("invalid tool call")
                    calls.add(call)
                    tools.add(event["tool"])
                    tool_input = event.get("input")
                    if isinstance(tool_input, dict) and tool_input.get("run_in_background") is True:
                        raise ValueError("background tools are not allowed in one-shot execution")
                elif kind == "tool_result":
                    call = event.get("callId")
                    if call not in calls or call in results or event.get("status") not in {"completed", "error"}:
                        raise ValueError("invalid tool result")
                    results.add(call)
                elif kind == "final":
                    if event.get("sessionId") != session_id or not isinstance(event.get("text"), str):
                        raise ValueError("invalid final")
                    final = event
                elif kind not in {"status", "text"}:
                    raise ValueError("unsupported event")
                if kind != "text":
                    events.append(event)
            if final is None or calls != results:
                raise ValueError("incomplete execution")
            reason = final.get("reason")
            clean_text = strip_thinking_blocks(final["text"])
            if not isinstance(reason, dict):
                return events, session_id, tools, clean_text, "dsh_incomplete"
            rkind = str(reason.get("kind") or "")
            if rkind == "completed":
                return events, session_id, tools, clean_text, None
            if rkind == "max-tokens":
                return events, session_id, tools, clean_text, "max_tokens"
            if rkind == "error":
                err_obj = (
                    reason.get("error")
                    if isinstance(reason.get("error"), dict)
                    else (reason.get("failure") if isinstance(reason.get("failure"), dict) else {})
                )
                code = str(err_obj.get("code") or "").upper()
                msg = str(err_obj.get("message") or "").lower()
                if "CONTEXT_WINDOW_EXCEEDED" in code or "context overflow" in msg or "context_length_exceeded" in msg:
                    return events, session_id, tools, clean_text, "context_overflow"
                if "EMPTY_RESPONSE" in code:
                    return events, session_id, tools, clean_text, "empty_response"
                return events, session_id, tools, clean_text, "upstream_error"
            if rkind == "aborted":
                return events, session_id, tools, clean_text, "aborted"
            return events, session_id, tools, clean_text, "dsh_incomplete"
        except (ValueError, TypeError, KeyError, UnicodeError):
            return events, session_id, tools, "", "protocol_error"

    @staticmethod
    def _extract_dsh_error_detail(events: list[dict[str, Any]], stderr: bytes) -> dict[str, str] | None:
        rkind = ""
        code = ""
        msg = ""
        if events and isinstance(events[-1], dict) and events[-1].get("type") == "final":
            reason = events[-1].get("reason")
            if isinstance(reason, dict):
                rkind = str(reason.get("kind") or "").strip()
                err_obj = (
                    reason.get("error")
                    if isinstance(reason.get("error"), dict)
                    else (reason.get("failure") if isinstance(reason.get("failure"), dict) else {})
                )
                code = str(err_obj.get("code") or "").strip()[:120]
                msg = str(err_obj.get("message") or "").strip()[:400]
        if not msg and stderr:
            for raw_ln in stderr.decode("utf-8", errors="replace").splitlines():
                ln = raw_ln.strip()
                if ln.startswith("dsh: "):
                    rest = ln[5:].strip()
                    if ": " in rest and not code:
                        c_part, m_part = rest.split(": ", 1)
                        code = c_part.strip()[:120]
                        msg = m_part.strip()[:400]
                    else:
                        msg = rest[:400]
                    break
        if not (rkind and rkind != "completed") and not code and not msg:
            return None
        out: dict[str, str] = {}
        if rkind and rkind != "completed":
            out["kind"] = rkind
        if code:
            out["code"] = code
        if msg:
            out["message"] = msg
        return out

    @staticmethod
    def _artifact_path(workspace: Workspace, name: str) -> Path:
        path = workspace.root / "artifacts"
        for component in ("", *PurePosixPath(name).parts):
            if component:
                path = path / component
            if path.is_symlink():
                raise ValueError(f"artifact path cannot contain symbolic links: {name}")
        if not path.resolve().is_relative_to(workspace.root / "artifacts"):
            raise ValueError("artifact path escapes artifacts/")
        return path

    def _has_valid_user_reply(self, workspace: Workspace, manifest: list[dict[str, Any]], execution_id: str) -> bool:
        return any(
            extract_final_reply(
                self._artifact_path(workspace, entry["path"].removeprefix("artifacts/")).read_text(encoding="utf-8"),
                execution_id,
            ) is not None
            for entry in manifest
            if isinstance(entry.get("path"), str) and entry["path"].startswith("artifacts/")
        )

    def _artifact_manifest(self, workspace: Workspace, task: DshTask) -> list[dict[str, Any]]:
        manifest = []
        total_bytes = 0
        secret = self.config.api_key.encode("utf-8")
        for name in task.expected_artifacts:
            path = self._artifact_path(workspace, name)
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(descriptor, "rb") as stream:
                info = os.fstat(stream.fileno())
                total_bytes += info.st_size
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or total_bytes > task.max_artifact_bytes:
                    raise ValueError(f"artifact is not a bounded regular file: {name}")
                digest = hashlib.sha256()
                tail = b""
                while chunk := stream.read(65536):
                    if secret in tail + chunk:
                        raise ValueError(f"artifact contains a runtime credential: {name}")
                    tail = (tail + chunk)[-len(secret):]
                    digest.update(chunk)
                manifest.append({"path": f"artifacts/{name}", "bytes": info.st_size, "sha256": digest.hexdigest()})
        return manifest

    def _collect_deliverable_files(
        self,
        workspace: Workspace,
        request: ExecutionRequest,
        manifest: list[dict[str, Any]],
        *,
        started_epoch: float,
    ) -> list[dict[str, Any]]:
        declared_paths: list[str] = []
        has_explicit_block = False
        for entry in manifest:
            rel = entry.get("path")
            if not isinstance(rel, str) or not rel.startswith("artifacts/"):
                continue
            text = self._artifact_path(workspace, rel.removeprefix("artifacts/")).read_text(encoding="utf-8")
            if has_deliverable_files_block(text, request.execution_id):
                has_explicit_block = True
                for item in extract_deliverable_files(text, request.execution_id):
                    if item not in declared_paths:
                        declared_paths.append(item)

        if not has_explicit_block:
            output_dir = self._safe_workspace_path(workspace, self._task_output_dir(request))
            if output_dir.exists() and output_dir.is_dir() and not output_dir.is_symlink():
                candidates: list[tuple[float, str]] = []
                for dirpath, dirnames, filenames in os.walk(output_dir, followlinks=False):
                    current = Path(dirpath)
                    dirnames[:] = [d for d in dirnames if not (current / d).is_symlink() and d != "__pycache__"]
                    for fname in filenames:
                        if fname.startswith(("~$", ".")) or fname.endswith((".partial", ".tmp")):
                            continue
                        fpath = current / fname
                        if fpath.is_symlink() or fpath.suffix.lower() not in _DELIVERABLE_AUTO_EXTENSIONS:
                            continue
                        try:
                            st = fpath.stat()
                        except OSError:
                            continue
                        if stat.S_ISREG(st.st_mode) and st.st_size > 0:
                            candidates.append((st.st_mtime, fpath.relative_to(workspace.root).as_posix()))
                candidates.sort(key=lambda pair: (pair[0], pair[1]))
                declared_paths = [rel for _, rel in candidates[:MAX_DELIVERABLE_FILES]]

        validated: list[dict[str, Any]] = []
        secret = self.config.api_key.encode("utf-8")
        for rel in declared_paths[:MAX_DELIVERABLE_FILES]:
            posix = PurePosixPath(rel)
            if not posix.parts or posix.parts[0] not in {"work", "artifacts"}:
                raise ValueError(f"deliverable file must be under work/ or artifacts/: {rel}")
            if rel.startswith("work/inputs/") or rel.startswith("artifacts/executions/"):
                raise ValueError(f"deliverable file cannot point to staged inputs or execution report: {rel}")
            path = self._safe_workspace_path(workspace, rel)
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(descriptor, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not 0 < info.st_size <= _MAX_DELIVERABLE_BYTES:
                    raise ValueError(f"deliverable file is empty, non-regular, or exceeds limit: {rel}")
                digest = hashlib.sha256()
                tail = b""
                while chunk := stream.read(65536):
                    if secret in tail + chunk:
                        raise ValueError(f"deliverable file contains a runtime credential: {rel}")
                    tail = (tail + chunk)[-len(secret):]
                    digest.update(chunk)
            content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            validated.append({
                "path": rel,
                "filename": path.name,
                "bytes": info.st_size,
                "sha256": digest.hexdigest(),
                "content_type": content_type,
                "workspace_id": workspace.workspace_id,
            })
        return validated

    @staticmethod
    def _safe_workspace_path(workspace: Workspace, rel: str) -> Path:
        root = workspace.root.resolve()
        path = root
        for component in PurePosixPath(rel).parts:
            if component in {"", ".", ".."}:
                raise ValueError(f"invalid workspace relative path: {rel}")
            path = path / component
            if path.is_symlink():
                raise ValueError(f"workspace path cannot contain symbolic links: {rel}")
        resolved = path.resolve()
        if not resolved.is_relative_to(root):
            raise ValueError(f"workspace path escapes root: {rel}")
        return resolved
