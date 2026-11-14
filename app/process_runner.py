from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Mapping, Sequence


@dataclass(frozen=True)
class ProcessResult:
    returncode: int | None
    stdout: bytes
    stderr: bytes
    error_category: str | None = None


def terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    if os.name == "nt":
        if process.poll() is None:
            subprocess.run(["taskkill", "/pid", str(process.pid), "/t", "/f"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
            if process.poll() is None:
                process.kill()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def run_process(
    command: Sequence[str], *, cwd: Path, env: Mapping[str, str], input_data: bytes,
    timeout_seconds: float, max_output_bytes: int, lease_fd: int | None = None,
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
    overflow = threading.Event()
    pipe_error = threading.Event()
    remaining = max_output_bytes

    def read_pipe(stream: BinaryIO, target: bytearray) -> None:
        nonlocal remaining
        try:
            while chunk := stream.read1(16384):
                with buffer_lock:
                    count = min(len(chunk), remaining)
                    target.extend(chunk[:count])
                    remaining -= count
                    if count < len(chunk):
                        overflow.set()
        except OSError:
            pipe_error.set()
        finally:
            stream.close()

    def write_input() -> None:
        try:
            process.stdin.write(input_data)
            process.stdin.flush()
        except (BrokenPipeError, OSError):
            pass
        finally:
            process.stdin.close()

    readers = [threading.Thread(target=read_pipe, args=(stream, target), daemon=True)
               for stream, target in zip((process.stdout, process.stderr), buffers)]
    writer = threading.Thread(target=write_input, daemon=True)
    for thread in [*readers, writer]:
        thread.start()
    deadline = time.monotonic() + timeout_seconds
    error_category = None
    try:
        while process.poll() is None or any(thread.is_alive() for thread in readers):
            if overflow.is_set():
                error_category = "output_limit"
                break
            if time.monotonic() >= deadline:
                error_category = "timeout"
                break
            time.sleep(0.01)
    finally:
        # A tool may leave descendants alive after the headless parent has exited.
        terminate_process_group(process)
        process.wait(timeout=5)
        for thread in [*readers, writer]:
            thread.join(timeout=2)
    if overflow.is_set():
        error_category = error_category or "output_limit"
    if pipe_error.is_set() or any(thread.is_alive() for thread in [*readers, writer]):
        error_category = error_category or "pipe_error"
    return ProcessResult(process.returncode, bytes(buffers[0]), bytes(buffers[1]), error_category)
