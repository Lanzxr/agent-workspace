from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from app.dsh import _LiveDshTrace
from app.dsh_session import DshSessionMonitor, resolve_node_executable, scan_zstd_frames
from app.workspace import WorkspaceManager


def _build_synthetic_zstd_frame(*, block_size: int = 3, single_segment: bool = True, checksum: bool = True) -> bytes:
    header = bytearray(b"\x28\xb5\x2f\xfd")
    descriptor = (0x20 if single_segment else 0x00) | (0x04 if checksum else 0x00)
    header.append(descriptor)
    header.append(0x01 if single_segment else 0x00)
    block_header = (block_size << 3) | (0 << 1) | 1
    header.extend(block_header.to_bytes(3, "little"))
    header.extend(b"a" * block_size)
    if checksum:
        header.extend(b"\x11\x22\x33\x44")
    return bytes(header)


class DshSessionTests(unittest.TestCase):
    def test_scan_zstd_frames_handles_concatenated_complete_and_torn_frames(self) -> None:
        frame1 = _build_synthetic_zstd_frame(block_size=4, single_segment=True, checksum=True)
        frame2 = _build_synthetic_zstd_frame(block_size=2, single_segment=False, checksum=False)
        complete = frame1 + frame2
        frames, torn = scan_zstd_frames(complete)
        self.assertEqual(frames, [(0, len(frame1)), (len(frame1), len(complete))])
        self.assertIsNone(torn)

        partial = complete + frame1[:-2]
        frames2, torn2 = scan_zstd_frames(partial)
        self.assertEqual(frames2, frames)
        self.assertEqual(torn2, len(complete))

        with self.assertRaises(ValueError):
            scan_zstd_frames(b"notzstd")

    def test_monitor_decodes_multiframe_zstd_session_compaction_events(self) -> None:
        node = resolve_node_executable()
        if not node:
            self.skipTest("node runtime not present for multi-frame Zstandard compression test")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = WorkspaceManager(root / "workspaces")
            workspace = manager.provision("ws-1")
            trace = _LiveDshTrace(manager, "ws-1", "dsh/exec-1.events.jsonl", workspace.root, lambda text: text)
            trace.session_id = "session-test-zstd"
            runtime_home = root / "runtime" / "dsh" / "ws-1"
            session_file = runtime_home / "sessions" / "ws" / "session-test-zstd" / "session.v4.jsonl.zstd"
            session_file.parent.mkdir(parents=True, exist_ok=True)

            def append_zstd_events(events: list[dict]) -> None:
                ndjson = "\n".join(json.dumps(item) for item in events) + "\n"
                compressed = subprocess.run(
                    [node, "-e", "const z=require('zlib'),fs=require('fs');process.stdout.write(z.zstdCompressSync(fs.readFileSync(0)));"],
                    input=ndjson.encode("utf-8"),
                    capture_output=True,
                    check=True,
                ).stdout
                with session_file.open("ab") as handle:
                    handle.write(compressed)

            monitor = DshSessionMonitor(runtime_home, trace, compaction_timeout_seconds=600.0, node_executable=node)
            append_zstd_events([{"seq": 1, "type": "step/start", "data": {"turn": 1, "step": 1}}])
            self.assertIsNone(monitor.probe())

            append_zstd_events([{"seq": 2, "type": "compaction/start", "data": {"compactionId": "cmp-z", "turn": 1}}])
            state_start = monitor.probe()
            self.assertIsNotNone(state_start)
            assert state_start is not None
            self.assertTrue(state_start.reset_activity)
            self.assertEqual(state_start.idle_timeout_seconds, 600.0)
            self.assertEqual(state_start.error_category, "compaction_timeout")

            state_ongoing = monitor.probe()
            self.assertIsNotNone(state_ongoing)
            assert state_ongoing is not None
            self.assertFalse(state_ongoing.reset_activity)
            self.assertEqual(state_ongoing.error_category, "compaction_timeout")

            append_zstd_events([{"seq": 3, "type": "compaction/end", "data": {"compactionId": "cmp-z", "turn": 1}}])
            state_end = monitor.probe()
            self.assertIsNotNone(state_end)
            assert state_end is not None
            self.assertTrue(state_end.reset_activity)
            self.assertIsNone(state_end.idle_timeout_seconds)
            self.assertIsNone(state_end.error_category)

            trace_text = (workspace.root / "logs/dsh/exec-1.events.jsonl").read_text(encoding="utf-8")
            self.assertIn('"phase": "compaction_start"', trace_text)
            self.assertIn('"phase": "compaction_end"', trace_text)


if __name__ == "__main__":
    unittest.main()
