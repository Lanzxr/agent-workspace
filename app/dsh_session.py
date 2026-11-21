from __future__ import annotations

import importlib
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable, Protocol, Sequence

from .process_runner import IdleProbeState

_ZSTD_MAGIC = 0xFD2FB528


def resolve_node_executable(command: Sequence[str] = ()) -> str | None:
    """Resolve an available Node.js binary for Zstandard session log inspection."""
    if command:
        first = Path(command[0])
        if first.stem.lower().startswith("node") and first.is_file():
            return str(first)
    discovered = shutil.which("node")
    if discovered:
        return discovered
    nodes = sorted(Path.home().glob("node-v24.*-linux-x64/bin/node"))
    if nodes and nodes[-1].is_file():
        return str(nodes[-1])
    return None

_NODE_FRAME_DECODER = (
    "const fs=require('fs'),zlib=require('zlib');"
    "const buf=fs.readFileSync(0);"
    "const nl=buf.indexOf(10);"
    "const ranges=JSON.parse(buf.subarray(0,nl).toString('utf8'));"
    "const base=nl+1;"
    "const out=[];"
    "for(const[s,e]of ranges){out.push(zlib.zstdDecompressSync(buf.subarray(base+s,base+e)));}"
    "process.stdout.write(Buffer.concat(out));"
)


class TraceSink(Protocol):
    @property
    def session_id(self) -> str | None: ...

    def record_external_event(self, event: dict[str, Any]) -> None: ...


def scan_zstd_frames(buffer: bytes) -> tuple[list[tuple[int, int]], int | None]:
    """Scan concatenated Zstandard frames and stop cleanly before any partial tail frame."""
    frames: list[tuple[int, int]] = []
    offset = 0
    length = len(buffer)
    while offset < length:
        start = offset
        if length - offset < 4:
            return frames, start
        if int.from_bytes(buffer[offset:offset + 4], "little") != _ZSTD_MAGIC:
            raise ValueError(f"corrupt Zstandard session log: invalid frame magic at byte {offset}")
        offset += 4
        if offset == length:
            return frames, start
        descriptor = buffer[offset]
        offset += 1
        if (descriptor & 24) != 0:
            raise ValueError(f"corrupt Zstandard session log: reserved frame-header bit at byte {offset - 1}")
        content_size_flag = descriptor >> 6
        single_segment = (descriptor & 32) != 0
        checksum = (descriptor & 4) != 0
        dictionary_flag = descriptor & 3
        dictionary_bytes = 4 if dictionary_flag == 3 else dictionary_flag
        content_size_bytes = (1 if single_segment else 0) if content_size_flag == 0 else (1 << content_size_flag)
        remaining_header = (0 if single_segment else 1) + dictionary_bytes + content_size_bytes
        if length - offset < remaining_header:
            return frames, start
        offset += remaining_header
        while True:
            if length - offset < 3:
                return frames, start
            block_header = int.from_bytes(buffer[offset:offset + 3], "little")
            offset += 3
            last_block = (block_header & 1) != 0
            block_type = (block_header >> 1) & 3
            block_size = block_header >> 3
            if block_type == 3:
                raise ValueError(f"corrupt Zstandard session log: reserved block type at byte {offset - 3}")
            payload_bytes = 1 if block_type == 1 else block_size
            if length - offset < payload_bytes:
                return frames, start
            offset += payload_bytes
            if last_block:
                break
        if checksum:
            if length - offset < 4:
                return frames, start
            offset += 4
        frames.append((start, offset))
    return frames, None


def _python_zstd_decompress() -> Callable[[bytes], bytes] | None:
    for module_name, attr in (("compression.zstd", "decompress"), ("pyzstd", "decompress")):
        try:
            module = importlib.import_module(module_name)
            fn = getattr(module, attr, None)
            if callable(fn):
                return fn
        except ImportError:
            pass
    try:
        zstandard = importlib.import_module("zstandard")
        decompressor_cls = getattr(zstandard, "ZstdDecompressor", None)
        if callable(decompressor_cls):
            return lambda payload: decompressor_cls().decompress(payload)
    except ImportError:
        pass
    return None


def decode_zstd_frames(
    buffer: bytes,
    frames: Sequence[tuple[int, int]],
    *,
    node_executable: str | None = None,
) -> bytes:
    """Decompress one or more complete Zstandard frames into plaintext bytes."""
    if not frames:
        return b""
    decoder = _python_zstd_decompress()
    if decoder is not None:
        return b"".join(decoder(buffer[start:end]) for start, end in frames)
    end_limit = frames[-1][1]
    complete_slice = buffer[:end_limit]
    if node_executable:
        header = json.dumps(list(frames), separators=(",", ":")).encode("utf-8") + b"\n"
        result = subprocess.run(
            [node_executable, "-e", _NODE_FRAME_DECODER],
            input=header + complete_slice,
            capture_output=True,
            timeout=5.0,
            check=False,
        )
        if result.returncode == 0:
            return result.stdout
        raise ValueError(f"node zstd decode failed ({result.returncode})")
    zstd_bin = shutil.which("zstd")
    if zstd_bin:
        result = subprocess.run(
            [zstd_bin, "-dc"],
            input=complete_slice,
            capture_output=True,
            timeout=5.0,
            check=False,
        )
        if result.returncode == 0:
            return result.stdout
        raise ValueError(f"zstd decode failed ({result.returncode})")
    raise ValueError("no Zstandard decoder available")


class DshSessionMonitor:
    """Inspect durable DSH session logs to distinguish active compaction from a hung process."""

    def __init__(
        self,
        runtime_home: Path,
        live_trace: TraceSink,
        *,
        compaction_timeout_seconds: float,
        node_executable: str | None = None,
    ) -> None:
        self.runtime_home = runtime_home
        self.live_trace = live_trace
        self.compaction_timeout_seconds = compaction_timeout_seconds
        self.node_executable = node_executable
        self._session_file: Path | None = None
        self._offset = 0
        self._last_stat: tuple[int, int] | None = None
        self._active_compactions: dict[str, dict[str, Any]] = {}
        self._emitted_phases: set[tuple[str, str]] = set()

    def reset_for_resume(self) -> None:
        """Clear transient per-process compaction state before resuming the session."""
        self._active_compactions.clear()
        self._last_stat = None

    def probe(self) -> IdleProbeState | None:
        session_file = self._locate_session_file()
        if session_file is None:
            return None
        try:
            info = session_file.stat()
        except OSError:
            return self._current_state(saw_start=False, saw_clean_end=False)
        stat_key = (info.st_size, info.st_mtime_ns)
        if stat_key == self._last_stat:
            return self._current_state(saw_start=False, saw_clean_end=False)
        if info.st_size < self._offset:
            self._offset = 0
            self._active_compactions.clear()
        try:
            events = self._read_new_events(session_file)
        except (OSError, ValueError, UnicodeError):
            return self._current_state(saw_start=False, saw_clean_end=False)
        self._last_stat = stat_key

        saw_start = False
        saw_clean_end = False
        for event in events:
            kind = event.get("type")
            data = event.get("data")
            payload = data if isinstance(data, dict) else {}
            if kind == "compaction/start":
                cid = str(payload.get("compactionId") or f"seq-{event.get('seq', '')}")
                turn = payload.get("turn")
                self._active_compactions[cid] = {"turn": turn}
                saw_start = True
                self._emit_once("compaction_start", cid, {
                    "type": "status",
                    "phase": "compaction_start",
                    "compactionId": cid,
                    **({"turn": turn} if turn is not None else {}),
                })
            elif kind == "compaction/end":
                cid = str(payload.get("compactionId") or "")
                turn = payload.get("turn")
                error = payload.get("error")
                if cid and cid in self._active_compactions:
                    self._active_compactions.pop(cid, None)
                elif not cid and self._active_compactions:
                    self._active_compactions.clear()
                else:
                    self._active_compactions.pop(cid, None)
                if not error:
                    saw_clean_end = True
                end_event: dict[str, Any] = {
                    "type": "status",
                    "phase": "compaction_end",
                    "compactionId": cid,
                }
                if turn is not None:
                    end_event["turn"] = turn
                if error:
                    end_event["error"] = str(error)[:300]
                self._emit_once("compaction_end", cid, end_event)
            elif kind in {"turn/end", "session/end-seed"}:
                self._active_compactions.clear()
            elif kind == "assistant/attempt":
                turn = payload.get("turn")
                step = payload.get("step")
                seq = str(event.get("seq", ""))
                self._emit_once("assistant_attempt", seq, {
                    "type": "status",
                    "phase": "assistant_attempt",
                    **({"turn": turn} if turn is not None else {}),
                    **({"step": step} if step is not None else {}),
                })
        return self._current_state(saw_start=saw_start, saw_clean_end=saw_clean_end)

    def _current_state(self, *, saw_start: bool, saw_clean_end: bool) -> IdleProbeState | None:
        if self._active_compactions:
            return IdleProbeState(
                reset_activity=saw_start,
                idle_timeout_seconds=self.compaction_timeout_seconds,
                error_category="compaction_timeout",
            )
        if saw_clean_end:
            return IdleProbeState(
                reset_activity=True,
                idle_timeout_seconds=None,
                error_category=None,
            )
        return None

    def _emit_once(self, phase: str, key: str, event: dict[str, Any]) -> None:
        marker = (phase, key)
        if key and marker in self._emitted_phases:
            return
        if key:
            self._emitted_phases.add(marker)
        self.live_trace.record_external_event(event)

    def _locate_session_file(self) -> Path | None:
        if self._session_file is not None and self._session_file.is_file():
            return self._session_file
        session_id = self.live_trace.session_id
        if not session_id or "/" in session_id or "\\" in session_id or ".." in session_id:
            return None
        sessions_root = self.runtime_home / "sessions"
        if not sessions_root.is_dir():
            return None
        for filename in ("session.v4.jsonl", "session.v4.jsonl.zstd"):
            matches = sorted(sessions_root.glob(f"*/{session_id}/{filename}"))
            if matches and matches[0].is_file():
                self._session_file = matches[0]
                return self._session_file
        return None

    def _read_new_events(self, session_file: Path) -> list[dict[str, Any]]:
        with session_file.open("rb") as stream:
            stream.seek(self._offset)
            chunk = stream.read()
        if not chunk:
            return []
        if session_file.name.endswith(".zstd"):
            frames, _torn_start = scan_zstd_frames(chunk)
            if not frames:
                return []
            plaintext = decode_zstd_frames(chunk, frames, node_executable=self.node_executable)
            self._offset += frames[-1][1]
        else:
            last_newline = chunk.rfind(b"\n")
            if last_newline < 0:
                return []
            plaintext = chunk[:last_newline + 1]
            self._offset += last_newline + 1
        events: list[dict[str, Any]] = []
        for raw_line in plaintext.decode("utf-8").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                events.append(parsed)
        return events
