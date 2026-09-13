"""A finished encode that never exits, because ffmpeg was still watching a keyboard.

ffmpeg reads stdin for its interactive keys ("q" to stop, "+"/"-" for verbosity). A child that
inherits a console handle nobody ever writes to can block on that read after the encode is
done - the pipes are drained, the caller's timeout is waiting on a process that will not leave.

Measured 2026-09-09: the Action Edit render inside the test suite burned 322 CPU-seconds, went
to exactly 0% and held the entire suite for two hours. The parent had spent 1.6 CPU-seconds in
those two hours; the encode itself had finished long before.

Same family as the ProPainter stdin deadlock. The fix is both halves: `-nostdin` so ffmpeg does
not look, and `stdin=DEVNULL` so there is nothing to look at even where the flag is not passed.
"""

import inspect
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import action_editor


class EveryFfmpegThisModuleStartsIsDeaf(unittest.TestCase):
    def test_the_shared_runner_closes_stdin(self):
        src = inspect.getsource(action_editor._run)
        self.assertIn("stdin=subprocess.DEVNULL", src)

    def test_and_asks_ffmpeg_not_to_watch_it(self):
        src = inspect.getsource(action_editor._run)
        self.assertIn('"-nostdin"', src)

    def test_the_runner_actually_inserts_the_flag(self):
        seen = {}

        def fake(argv, **kw):
            seen["argv"], seen["kw"] = argv, kw
            return subprocess.CompletedProcess(argv, 0, "", "")

        original = subprocess.run
        subprocess.run = fake
        try:
            action_editor._run(["C:/tools/ffmpeg.exe", "-y", "-i", "a.mp4", "out.mp4"])
        finally:
            subprocess.run = original
        self.assertEqual("-nostdin", seen["argv"][1])
        self.assertIs(subprocess.DEVNULL, seen["kw"].get("stdin"))

    def test_it_is_not_added_twice(self):
        seen = {}

        def fake(argv, **kw):
            seen["argv"] = argv
            return subprocess.CompletedProcess(argv, 0, "", "")

        original = subprocess.run
        subprocess.run = fake
        try:
            action_editor._run(["ffmpeg", "-nostdin", "-i", "a.mp4", "out.mp4"])
        finally:
            subprocess.run = original
        self.assertEqual(1, seen["argv"].count("-nostdin"))

    def test_a_non_ffmpeg_tool_is_left_alone(self):
        seen = {}

        def fake(argv, **kw):
            seen["argv"], seen["kw"] = argv, kw
            return subprocess.CompletedProcess(argv, 0, "", "")

        original = subprocess.run
        subprocess.run = fake
        try:
            action_editor._run(["ffprobe", "-show_streams", "a.mp4"])
        finally:
            subprocess.run = original
        self.assertNotIn("-nostdin", seen["argv"])
        self.assertIs(subprocess.DEVNULL, seen["kw"].get("stdin"),
                      "ffprobe inherits the same console handle and can block on it too")

    def test_the_direct_decode_closes_stdin_as_well(self):
        """motion_signal pipes rawvideo out of ffmpeg and does not go through _run."""
        src = inspect.getsource(action_editor.motion_signal)
        self.assertIn("stdin=subprocess.DEVNULL", src)
        self.assertIn('"-nostdin"', src)


if __name__ == "__main__":
    unittest.main()
