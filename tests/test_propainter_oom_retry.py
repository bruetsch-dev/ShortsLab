"""An out-of-memory ProPainter run must retry SMALLER, and the caller must be able to tell.

Measured on an 11 GB card, 2026-08-29, same 9:16 clip:
  subvideo_length 40 -> OOM      width 540 -> OOM
  subvideo_length 24 -> OOM      width 432 -> OOM
  subvideo_length 12 -> OOM      width 360 -> completed in 77s
  subvideo_length  8 -> OOM

So the shortage is SPATIAL, not temporal: the model wants ~5 GiB whatever the window length.
Only reducing the working width helps.

The retry existed once before and never fired, twice, for the same reason: `_run_propainter`
puts only the LAST 400 characters of the child stderr into its RuntimeError, and "CUDA out of
memory" OPENS that message - so it is precisely the part that gets cut off. Any caller matching
on the text sees nothing. Hence a dedicated exception type.
"""

import inspect
import unittest

import caption_remover as cr


class ExceptionTypeTests(unittest.TestCase):
    def test_there_is_a_dedicated_out_of_memory_type(self):
        self.assertTrue(issubclass(cr.OutOfMemory, RuntimeError),
                        "must stay a RuntimeError so old handlers keep working")

    def test_the_runner_raises_it_from_the_whole_stderr(self):
        """The decision is made where the full text still exists, not from the 400-char tail."""
        body = inspect.getsource(cr._run_propainter)
        self.assertIn('if "out of memory" in stderr_text.lower():', body)
        self.assertIn("raise OutOfMemory(message)", body)

    def test_the_caller_catches_the_type_not_the_text(self):
        body = inspect.getsource(cr.remove_caption_regions)
        self.assertIn("except OutOfMemory:", body)
        self.assertNotIn('"out of memory" not in str(exc)', body)


class WidthLadderTests(unittest.TestCase):
    def test_the_retry_width_is_smaller_than_the_working_width(self):
        self.assertLess(cr.RETRY_WIDTH, cr.WORK_WIDTH)

    def test_the_retry_runs_at_that_width(self):
        body = inspect.getsource(cr._run_propainter_at_width)
        self.assertIn("scale={RETRY_WIDTH}:-2", body)

    def test_the_mask_is_detected_again_at_the_new_scale(self):
        """Scaling a glyph-tight mask down by a third stops it covering its own glyph edges."""
        body = inspect.getsource(cr._run_propainter_at_width)
        self.assertIn("detect_caption_mask(retry_frames)", body)

    def test_a_failed_retry_still_falls_back_to_the_cpu(self):
        """Two OOMs must produce a clean clip, not an exception."""
        self.assertIn("return None", inspect.getsource(cr._run_propainter_at_width))
        body = inspect.getsource(cr.remove_caption_regions)
        self.assertIn("if filled is None:", body)

    def test_cancel_still_reaches_through_the_retry(self):
        body = inspect.getsource(cr._run_propainter_at_width)
        self.assertIn("except Cancelled:", body)
        self.assertIn("cancel_check=cancel_check", body)


if __name__ == "__main__":
    unittest.main()
