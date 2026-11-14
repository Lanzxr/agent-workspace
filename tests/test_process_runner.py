from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from app.process_runner import run_process


class ProcessRunnerTests(unittest.TestCase):
    def test_output_limit_stops_a_process_without_buffering_its_unbounded_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = run_process(
                (sys.executable, "-c", "import os; os.write(1, b'x' * 2000000)"),
                cwd=Path(directory), env=dict(os.environ), input_data=b"", timeout_seconds=5,
                max_output_bytes=1024,
            )
        self.assertEqual(result.error_category, "output_limit")
        self.assertLessEqual(len(result.stdout) + len(result.stderr), 1024)

    def test_timeout_terminates_the_process(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = run_process((sys.executable, "-c", "import time; time.sleep(10)"),
                                 cwd=Path(directory), env=dict(os.environ), input_data=b"",
                                 timeout_seconds=0.1, max_output_bytes=1024)
        self.assertEqual(result.error_category, "timeout")
        self.assertIsNotNone(result.returncode)

    def test_large_stdin_and_simultaneous_stderr_do_not_deadlock(self) -> None:
        script = "import sys; sys.stderr.write('e'*100000); print(len(sys.stdin.buffer.read()))"
        with tempfile.TemporaryDirectory() as directory:
            result = run_process((sys.executable, "-c", script), cwd=Path(directory), env=dict(os.environ),
                                 input_data=b"x" * 200000, timeout_seconds=5, max_output_bytes=150000)
        self.assertIsNone(result.error_category)
        self.assertEqual(result.stdout.strip(), b"200000")
        self.assertEqual(len(result.stderr), 100000)


if __name__ == "__main__":
    unittest.main()
