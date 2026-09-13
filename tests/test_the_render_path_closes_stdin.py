"""The deadlock that held the test suite for two hours reaches the render path too.

ffmpeg watches stdin for its interactive keys. The app runs under a hidden console, so a child
inherits a handle nobody ever writes to and can block on that read after the encode has finished.
Measured 2026-09-09 in the suite: an Action Edit encode burned 322 CPU-seconds, went to exactly
0% and never exited; its parent had used 1.6 CPU-seconds in those two hours.

`action_editor` was fixed first. These are the two places with the widest reach:

    pipeline.run_subprocess_with_cancel   the metadata sanitize AND the final mux of every render,
                                          and it polls in a loop with no timeout to break out of
    app._compress_render_file             runs after every render

plus the caption pass, which runs on every scraped Short. ProPainter's own worker keeps its pipe:
it is talked to on purpose.
"""

import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pipeline

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TheCancellableRunnerIsDeaf(unittest.TestCase):
    def run_with_spy(self, cmd):
        seen = {}

        class FakeProc:
            returncode = 0

            def poll(self):
                return 0

        def fake(argv, **kw):
            seen["argv"], seen["kw"] = argv, kw
            return FakeProc()

        original = subprocess.Popen
        subprocess.Popen = fake
        try:
            pipeline.run_subprocess_with_cancel(cmd)
        finally:
            subprocess.Popen = original
        return seen

    def test_stdin_is_closed(self):
        seen = self.run_with_spy(["C:/tools/ffmpeg.exe", "-i", "a.mp4", "out.mp4"])
        self.assertIs(subprocess.DEVNULL, seen["kw"].get("stdin"))

    def test_ffmpeg_is_told_not_to_watch_it(self):
        seen = self.run_with_spy(["C:/tools/ffmpeg.exe", "-i", "a.mp4", "out.mp4"])
        self.assertEqual("-nostdin", seen["argv"][1])

    def test_the_flag_is_not_doubled(self):
        seen = self.run_with_spy(["ffmpeg", "-nostdin", "-i", "a.mp4", "out.mp4"])
        self.assertEqual(1, seen["argv"].count("-nostdin"))

    def test_a_non_ffmpeg_child_keeps_its_arguments(self):
        seen = self.run_with_spy(["python", "worker.py"])
        self.assertNotIn("-nostdin", seen["argv"])
        self.assertIs(subprocess.DEVNULL, seen["kw"].get("stdin"))


class EveryFfmpegInTheCaptionPassIsDeaf(unittest.TestCase):
    """It runs on every scraped Short, once per scene, for a minute at a time."""

    def test_no_spawn_there_leaves_stdin_open(self):
        src = open(os.path.join(ROOT, "caption_remover.py"), encoding="utf-8").read()
        lines = src.split("\n")
        open_spawns = []
        for index, line in enumerate(lines):
            if "subprocess.run(" not in line and "subprocess.Popen(" not in line:
                continue
            if line.strip().startswith("#"):
                continue
            block = "\n".join(lines[index:index + 12])
            if "stdin=subprocess.DEVNULL" in block or "stdin=subprocess.PIPE" in block:
                continue
            open_spawns.append(index + 1)
        self.assertEqual([], open_spawns,
                         f"caption_remover.py spawns with an inherited stdin at {open_spawns}")

    def test_propainters_own_worker_still_has_its_pipe(self):
        src = open(os.path.join(ROOT, "caption_remover.py"), encoding="utf-8").read()
        self.assertIn("stdin=subprocess.PIPE", src,
                      "the ProPainter worker is talked to on purpose; do not close its stdin")


class TheRenderCompressorIsDeaf(unittest.TestCase):
    def test_it_passes_nostdin_and_closes_stdin(self):
        src = open(os.path.join(ROOT, "app.py"), encoding="utf-8").read()
        block = src[src.index('"-c:v", "libx264", "-preset", "slow", "-crf", "27"') - 400:]
        self.assertIn('"-nostdin"', block[:600])
        self.assertIn("stdin=subprocess.DEVNULL", block[:900])


if __name__ == "__main__":
    unittest.main()
