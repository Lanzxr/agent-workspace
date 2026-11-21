from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Callable, Mapping, Sequence


@dataclass(frozen=True)
class ProcessResult:
    returncode: int | None
    stdout: bytes
    stderr: bytes
    error_category: str | None = None


@dataclass(frozen=True)
class IdleProbeState:
    reset_activity: bool = False
    idle_timeout_seconds: float | None = None
    error_category: str | None = None


def terminate_process_group(process: subprocess.Popen[bytes], *, graceful_seconds: float = 0) -> None:
    if os.name == "nt":
        if process.poll() is None:
            subprocess.run(["taskkill", "/pid", str(process.pid), "/t", "/f"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
            if process.poll() is None:
                process.kill()
        return
    if graceful_seconds > 0 and process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        deadline = time.monotonic() + graceful_seconds
        while process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def run_process(
    command: Sequence[str], *, cwd: Path, env: Mapping[str, str], input_data: bytes,
    timeout_seconds: float, max_output_bytes: int, lease_fd: int | None = None,
    idle_timeout_seconds: float | None = None,
    soft_timeout_seconds: float | None = None,
    graceful_terminate_seconds: float = 0.0,
    on_stdout_line: Callable[[bytes], bool] | None = None,
    idle_state_probe: Callable[[], IdleProbeState | None] | None = None,
    idle_probe_interval_seconds: float = 5.0,
) -> ProcessResult:
    """Drain both pipes concurrently, retaining at most the shared byte budget."""
    options = {"pass_fds": (lease_fd,)} if os.name != "nt" and lease_fd is not None else {}
    try:
        process = subprocess.Popen(
            list(command), cwd=cwd, env=dict(env), stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=os.name != "nt", **options,
        )
    except OSError:
        return ProcessResult(None, b"", b"", "process_start")

    buffers = [bytearray(), bytearray()]
    buffer_lock = threading.Lock()
    activity_lock = threading.Lock()
    last_activity = time.monotonic()
    overflow = threading.Event()
    pipe_error = threading.Event()
    remaining = max_output_bytes

    def mark_activity() -> None:
        nonlocal last_activity
        with activity_lock:
            last_activity = time.monotonic()

    def read_pipe(
        stream: BinaryIO,
        target: bytearray,
        line_callback: Callable[[bytes], bool] | None,
        *,
        activity_on_bytes: bool,
    ) -> None:
        nonlocal remaining
        pending_line = bytearray()
        try:
            while chunk := stream.read1(16384):
                with buffer_lock:
                    count = min(len(chunk), remaining)
                    accepted = chunk[:count]
                    target.extend(accepted)
                    remaining -= count
                    if count < len(chunk):
                        overflow.set()
                if not accepted:
                    continue
                if line_callback is None:
                    if activity_on_bytes:
                        mark_activity()
                else:
                    pending_line.extend(accepted)
                    while b"\n" in pending_line:
                        idx = pending_line.index(b"\n")
                        line = bytes(pending_line[:idx]).rstrip(b"\r")
                        del pending_line[:idx + 1]
                        if line_callback(line):
                            mark_activity()
        except OSError:
            pipe_error.set()
        finally:
            if line_callback is not None and pending_line:
                try:
                    if line_callback(bytes(pending_line).rstrip(b"\r")):
                        mark_activity()
                except OSError:
                    pipe_error.set()
            stream.close()

    def write_input() -> None:
        try:
            process.stdin.write(input_data)
            process.stdin.flush()
        except (BrokenPipeError, OSError):
            pass
        finally:
            process.stdin.close()

    readers = [
        threading.Thread(
            target=read_pipe,
            args=(process.stdout, buffers[0], on_stdout_line),
            kwargs={"activity_on_bytes": on_stdout_line is None},
            daemon=True,
        ),
        threading.Thread(
            target=read_pipe,
            args=(process.stderr, buffers[1], None),
            kwargs={"activity_on_bytes": on_stdout_line is None},
            daemon=True,
        ),
    ]
    writer = threading.Thread(target=write_input, daemon=True)
    for thread in [*readers, writer]:
        thread.start()
    start = time.monotonic()
    deadline = start + timeout_seconds
    soft_deadline = (
        start + soft_timeout_seconds
        if soft_timeout_seconds is not None and 0 < soft_timeout_seconds < timeout_seconds
        else None
    )
    probe_step = (
        min(idle_probe_interval_seconds, max(0.02, (idle_timeout_seconds or idle_probe_interval_seconds) * 0.25))
        if idle_state_probe is not None and idle_probe_interval_seconds > 0
        else None
    )
    last_probe = start
    probe_override_timeout: float | None = None
    probe_override_category: str | None = None
    error_category = None
    graceful_seconds = 0.0
    try:
        while process.poll() is None or any(thread.is_alive() for thread in readers):
            if overflow.is_set():
                error_category = "output_limit"
                break
            now = time.monotonic()
            if now >= deadline:
                error_category = "timeout"
                break
            if process.poll() is None:
                if soft_deadline is not None and now >= soft_deadline:
                    error_category = "soft_timeout"
                    graceful_seconds = graceful_terminate_seconds
                    break
                effective_idle = probe_override_timeout if probe_override_timeout is not None else idle_timeout_seconds
                effective_category = probe_override_category or "idle_timeout"
                with activity_lock:
                    idle_elapsed = now - last_activity
                should_probe = (
                    idle_state_probe is not None
                    and probe_step is not None
                    and (
                        (effective_idle is not None and idle_elapsed >= effective_idle)
                        or ((idle_elapsed >= probe_step or probe_override_timeout is not None)
                            and (now - last_probe) >= probe_step)
                    )
                )
                if should_probe and idle_state_probe is not None:
                    last_probe = now
                    probe_state = idle_state_probe()
                    if probe_state is not None:
                        if probe_state.reset_activity:
                            mark_activity()
                            now = time.monotonic()
                            with activity_lock:
                                idle_elapsed = now - last_activity
                        probe_override_timeout = probe_state.idle_timeout_seconds
                        probe_override_category = probe_state.error_category
                        effective_idle = (
                            probe_override_timeout
                            if probe_override_timeout is not None
                            else idle_timeout_seconds
                        )
                        effective_category = probe_override_category or "idle_timeout"
                    elif probe_override_timeout is not None:
                        probe_override_timeout = None
                        probe_override_category = None
                        effective_idle = idle_timeout_seconds
                        effective_category = "idle_timeout"
                if effective_idle is not None and idle_elapsed >= effective_idle:
                    error_category = effective_category
                    graceful_seconds = graceful_terminate_seconds
                    break
            time.sleep(0.01)
    finally:
        # A tool may leave descendants alive after the headless parent has exited.
        terminate_process_group(process, graceful_seconds=graceful_seconds)
        process.wait(timeout=5)
        for thread in [*readers, writer]:
            thread.join(timeout=2)
    if overflow.is_set():
        error_category = error_category or "output_limit"
    if pipe_error.is_set() or any(thread.is_alive() for thread in [*readers, writer]):
        error_category = error_category or "pipe_error"
    return ProcessResult(process.returncode, bytes(buffers[0]), bytes(buffers[1]), error_category)
