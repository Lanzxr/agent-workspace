from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from app.process_runner import IdleProbeState, run_process


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

    def test_idle_timeout_resets_on_active_stdout_lines_and_stops_when_silent(self) -> None:
        script = (
            "import sys, time\n"
            "for i in range(4):\n"
            "    time.sleep(0.08)\n"
            "    print(f'tick-{i}', flush=True)\n"
            "time.sleep(5)\n"
        )
        seen: list[bytes] = []
        with tempfile.TemporaryDirectory() as directory:
            result = run_process(
                (sys.executable, "-c", script),
                cwd=Path(directory),
                env=dict(os.environ),
                input_data=b"",
                timeout_seconds=5.0,
                idle_timeout_seconds=0.25,
                max_output_bytes=4096,
                on_stdout_line=lambda line: (seen.append(line), True)[1],
            )
        self.assertEqual(result.error_category, "idle_timeout")
        self.assertEqual(seen, [b"tick-0", b"tick-1", b"tick-2", b"tick-3"])

    def test_soft_timeout_triggers_before_hard_timeout(self) -> None:
        script = (
            "import sys, time\n"
            "for i in range(20):\n"
            "    print(f'step-{i}', flush=True)\n"
            "    time.sleep(0.05)\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            result = run_process(
                (sys.executable, "-c", script),
                cwd=Path(directory),
                env=dict(os.environ),
                input_data=b"",
                timeout_seconds=5.0,
                soft_timeout_seconds=0.15,
                idle_timeout_seconds=2.0,
                graceful_terminate_seconds=0.1,
                max_output_bytes=4096,
                on_stdout_line=lambda _line: True,
            )
        self.assertEqual(result.error_category, "soft_timeout")

    def test_idle_state_probe_extends_wait_for_compaction_and_bounds_it(self) -> None:
        script = (
            "import sys, time\n"
            "print('step_start', flush=True)\n"
            "time.sleep(0.35)\n"
            "print('done', flush=True)\n"
        )
        polls = 0

        def probe() -> IdleProbeState | None:
            nonlocal polls
            polls += 1
            if polls == 1:
                return IdleProbeState(reset_activity=True, idle_timeout_seconds=1.0, error_category="compaction_timeout")
            if polls < 5:
                return IdleProbeState(reset_activity=False, idle_timeout_seconds=1.0, error_category="compaction_timeout")
            return IdleProbeState(reset_activity=True, idle_timeout_seconds=None, error_category=None)

        with tempfile.TemporaryDirectory() as directory:
            succeeded = run_process(
                (sys.executable, "-c", script),
                cwd=Path(directory),
                env=dict(os.environ),
                input_data=b"",
                timeout_seconds=5.0,
                idle_timeout_seconds=0.15,
                max_output_bytes=4096,
                on_stdout_line=lambda _line: True,
                idle_state_probe=probe,
                idle_probe_interval_seconds=0.05,
            )
            self.assertIsNone(succeeded.error_category)
            self.assertIn(b"done", succeeded.stdout)

            timed_out = run_process(
                (sys.executable, "-c", "import time; print('s', flush=True); time.sleep(5)"),
                cwd=Path(directory),
                env=dict(os.environ),
                input_data=b"",
                timeout_seconds=5.0,
                idle_timeout_seconds=0.1,
                max_output_bytes=4096,
                on_stdout_line=lambda _line: True,
                idle_state_probe=lambda: IdleProbeState(
                    reset_activity=False,
                    idle_timeout_seconds=0.25,
                    error_category="compaction_timeout",
                ),
                idle_probe_interval_seconds=0.04,
            )
        self.assertEqual(timed_out.error_category, "compaction_timeout")


if __name__ == "__main__":
    unittest.main()
