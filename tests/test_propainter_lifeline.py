"""A fill child must never outlive its parent holding the GPU.

Measured 2026-08-26: a ProPainter child from 12:34 was still alive at 20:36 - eight hours - with
4.2 seconds of CPU, an empty output folder, and 55% of the card reserved. Its parent was long
gone. The watchdog that should have killed it had never started, because it was created in the
same statement that imported torch, and `import torch` is where the child was hanging.
"""

import ast
import re
import unittest
from pathlib import Path

SOURCE = (Path(__file__).resolve().parent.parent / "caption_remover.py").read_text(encoding="utf-8")


def bootstrap_code():
    """Only the string LITERALS of the bootstrap - never comment text. The first version
    regex-scraped every quoted span in the block, so a comment quoting an error message
    ("5.49 GiB free") landed in the middle of the reconstructed code and failed the parse."""
    block = SOURCE[SOURCE.index("    bootstrap = ("):SOURCE.index("    command = [")]
    lines = [line.strip() for line in block.splitlines()
             if line.strip().startswith(('"', 'f"'))]
    code = "".join(re.findall(r'"([^"]*)"', chr(10).join(lines).replace('f"', '"')))
    return code.replace("{VRAM_FRACTION}", "0.55").replace("{mem_fraction}", "0.55")


class LifelineTests(unittest.TestCase):
    def setUp(self):
        self.code = bootstrap_code()

    def test_the_bootstrap_is_valid_python(self):
        """It is passed to `python -c`; a syntax slip here is a silent, unfillable clip."""
        ast.parse(self.code)

    def test_the_watchdog_is_armed_before_torch_is_imported(self):
        self.assertLess(self.code.index("w.start()"), self.code.index("torch"),
                        "the watchdog does not exist while the child is importing torch")

    def test_there_is_no_stdin_watchdog_thread(self):
        """Bisected on a live hang: a thread blocked in sys.stdin.buffer.read(1) deadlocks the
        main thread's very next import on Windows - even a bare print never ran - which is why
        every ProPainter child sat at ~1s of CPU forever. It also never fired (an orphan was
        observed with its parent dead and the read still blocked). The kernel watchdog is the
        one that works, and it works alone."""
        self.assertNotIn("sys.stdin.buffer.read(1)", self.code)
        self.assertIn("os._exit(3)", self.code)

    def test_the_watchdog_cannot_hold_the_process_open(self):
        self.assertIn("daemon=True", self.code)

    def test_there_is_a_kernel_level_parent_watch_too(self):
        """Measured on a live orphan: the parent was dead and stdin read(1) still blocked - the
        pipe EOF never arrived on Windows. WaitForSingleObject on the parent's process handle is
        deterministic: proven by killing a parent and watching the child exit within a second."""
        self.assertIn("OpenProcess(0x00100000, False, os.getppid())", self.code)
        self.assertIn("WaitForSingleObject(h, 0xFFFFFFFF), os._exit(3)", self.code)

    def test_both_watchdogs_are_armed_before_torch(self):
        self.assertLess(self.code.index("w.start()"), self.code.index("torch"))

    def test_the_child_caps_itself_by_what_is_actually_free(self):
        """A fixed 0.55 of the card asked for 6.05 GiB while zombies and the desktop held all
        but 5.49 - every real-size clip died OOM inside its own allowance. The cap now follows
        free memory, with 0.55 as the ceiling."""
        self.assertIn("free, total = torch.cuda.mem_get_info()", self.code)
        self.assertIn("set_per_process_memory_fraction(min(0.55, free * 0.92 / total))",
                      self.code)

    def test_an_oom_spends_the_temporal_levers_in_order(self):
        """This asserted a single retry at subvideo 24 and stopped there.

        The ladder has three rungs now, and the order is the point: what the card holds is set
        by resolution (quadratic, and the ONLY one the viewer can see), then subvideo_length
        (linear, invisible), then the attention window. So the invisible levers are spent first
        - 40 to 24 to 16, then neighbour 10 to 6 - and the resolution is never dropped here at
        all. It is raised as OutOfMemory so the caller can re-encode, because the width is baked
        into the copy this function was handed.
        """
        block = SOURCE[SOURCE.index("def _run_propainter"):]
        block = block[:block.index("def _dimensions")]
        # Against the WHOLE stderr, not the 400-char tail: the phrase opens the CUDA message
        # and the tail cuts exactly that off - the first version of this retry never fired.
        self.assertIn('if "out of memory" in stderr_text.lower():', block)
        self.assertIn('PYTORCH_CUDA_ALLOC_CONF', SOURCE)
        first = block.index("subvideo_length=24")
        second = block.index("subvideo_length=16")
        third = block.index("neighbor_length=6")
        self.assertLess(first, second, "16 must come after 24")
        self.assertLess(second, third, "the attention window is the last temporal lever")
        self.assertNotIn("FILL_WIDTH", block, "the resolution is not given up inside the run")
        self.assertIn("raise OutOfMemory(message)", block)

    def test_the_parent_still_kills_on_cancel_and_on_timeout(self):
        block = SOURCE[SOURCE.index("def _run_propainter"):]
        block = block[:block.index("def _dimensions")]
        self.assertIn("process.kill()", block)
        self.assertIn("ProPainter timed out", block)
        self.assertIn("stdin=subprocess.PIPE", block)


if __name__ == "__main__":
    unittest.main()


class ParentVramTests(unittest.TestCase):
    """The parent's own CUDA tenants were the OOM all along.

    Whisper's cached weights plus torch's arena held ~5.8 GiB of an 11 GiB card while the
    ProPainter child needed ~6 - so every fill of the night died at "5.5 GiB free, 6.05 GiB
    allowed" no matter which knob the child turned. Proven in one process: resident 2.7 GiB ->
    release -> 1.1 GiB -> inference OK in 34s -> alignment reloads transparently (112 words
    before and after).
    """

    def test_the_release_exists_and_clears_every_cache(self):
        import voice_align
        self.assertTrue(hasattr(voice_align, "release_gpu_models"))
        import inspect
        src = inspect.getsource(voice_align.release_gpu_models)
        for piece in ("_MODEL = None", "_WX_MODEL = None", "empty_cache()"):
            self.assertIn(piece, src)

    def test_the_fill_releases_the_parent_before_spawning(self):
        """What matters is the ORDER: release the parent's own CUDA tenants, then start the fill.

        The anchors have moved twice. The gate is three early returns now (one per reason,
        because a single shared "unavailable" message blamed the backend for the caller's own
        default), and the spawn itself moved into _fill_in_pieces, so the child is no longer
        started inside this function at all.
        """
        block = SOURCE[SOURCE.index('os.environ.get("CAPTION_REMOVER_PROPAINTER", "1") == "0"'):]
        block = block[:block.index("_fill_in_pieces(")]
        self.assertIn("voice_align.release_gpu_models()", block)

    def test_a_missing_voice_align_cannot_break_the_fill(self):
        block = SOURCE[SOURCE.index('os.environ.get("CAPTION_REMOVER_PROPAINTER", "1") == "0"'):]
        head = block[:block.index("_fill_in_pieces(")]
        self.assertIn("except Exception:", head)
