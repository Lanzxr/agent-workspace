from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import BinaryIO


class ExecutionBusy(Exception):
    """A live executor owns the workspace lease."""


def private_directory(path: Path) -> Path:
    if path.is_symlink():
        raise ValueError(f"runtime directory cannot be a symbolic link: {path}")
    path.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        path.chmod(0o700)
    return path


def write_private_json(path: Path, value: object) -> None:
    private_directory(path.parent)
    if path.is_symlink():
        raise ValueError(f"runtime file cannot be a symbolic link: {path}")
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
            temporary_path = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
        if os.name != "nt":
            descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


class WorkspaceLease:
    def __init__(self, path: Path, *, wait_seconds: float = 0.0, poll_interval: float = 0.2) -> None:
        self.path = path
        self.wait_seconds = max(0.0, float(wait_seconds))
        self.poll_interval = max(0.01, min(1.0, float(poll_interval)))
        self.stream: BinaryIO | None = None

    @staticmethod
    def _try_acquire(descriptor: int, stream: BinaryIO, path: Path) -> None:
        if os.name == "nt":
            import msvcrt

            if path.stat().st_size == 0:
                stream.write(b"0")
            stream.seek(0)
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def __enter__(self) -> "WorkspaceLease":
        private_directory(self.path.parent)
        if self.path.is_symlink():
            raise ValueError("workspace lease must not be a symbolic link")
        descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        self.stream = os.fdopen(descriptor, "r+b", buffering=0)
        deadline = time.monotonic() + self.wait_seconds if self.wait_seconds > 0 else 0.0
        while True:
            try:
                self._try_acquire(descriptor, self.stream, self.path)
                return self
            except OSError as error:
                if self.wait_seconds > 0:
                    remaining = deadline - time.monotonic()
                    if remaining > 0:
                        time.sleep(min(self.poll_interval, remaining))
                        continue
                self.stream.close()
                self.stream = None
                raise ExecutionBusy("workspace already has a live execution") from error

    def fileno(self) -> int:
        if self.stream is None:
            raise ValueError("workspace lease is not held")
        return self.stream.fileno()

    def __exit__(self, *exception: object) -> None:
        if self.stream is not None:
            if os.name == "nt":
                import msvcrt

                self.stream.seek(0)
                msvcrt.locking(self.fileno(), msvcrt.LK_UNLCK, 1)
            # On POSIX, close rather than LOCK_UN: an orphaned DSH child keeps the lease.
            self.stream.close()
            self.stream = None
