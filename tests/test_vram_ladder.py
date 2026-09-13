"""When the card says no, give up the thing the viewer cannot see.

Four levers decide what ProPainter holds at once, and they are not equal:

    resolution        quadratic - half the width is a quarter of the memory, and it is the ONLY
                      one of the four the viewer can see. A fill computed at 360 and scaled back
                      to 1080 is a threefold enlargement of an invented area: the smeared slab
                      the owner kept pointing at.
    subvideo_length   linear, invisible. Their default is 80; the pass starts at 40.
    neighbor_length   the attention window - how many frames go in at once. Costs nothing until
                      it gets small.
    fp16              halves everything, always on.

The pass used to reach for the resolution first and never touch the attention window at all.
It now spends the temporal levers in order and re-encodes smaller only when they are exhausted.
"""

import inspect
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import caption_remover as cr

RUN = inspect.getsource(cr._run_propainter)
LADDER = inspect.getsource(cr._fill_piece_with_width_ladder)
WIDTH = inspect.getsource(cr._run_propainter_at_width)


class TheCheapLeversGoFirst(unittest.TestCase):
    def test_fp16_is_always_on(self):
        self.assertIn('"--fp16"', RUN)

    def test_the_sub_video_starts_below_their_default(self):
        self.assertIn("subvideo_length=40", inspect.signature(cr._run_propainter).__str__()
                      .replace(" ", "").replace("=40", "=40"))
        self.assertIn("default=80", "default=80")          # theirs, for the record

    def test_the_attention_window_is_passed_at_all(self):
        """It was left at their default and never used as a lever."""
        self.assertIn('"--neighbor_length"', RUN)

    def test_the_temporal_levers_are_spent_in_order(self):
        first = RUN.index("subvideo_length=24")
        second = RUN.index("subvideo_length=16")
        third = RUN.index("neighbor_length=6")
        self.assertLess(first, second)
        self.assertLess(second, third)

    def test_the_resolution_is_not_dropped_inside_the_run(self):
        """The width is baked into the copy handed to the child; giving it up means re-encoding,
        which is the caller's job and the last resort."""
        self.assertNotIn("FILL_WIDTH", RUN)


class TheResolutionIsTheLastResort(unittest.TestCase):
    def test_the_ladder_starts_at_the_width_the_card_allows(self):
        self.assertIn("first = fill_width_now()", LADDER)

    def test_only_an_out_of_memory_gives_up_picture(self):
        self.assertIn("except OutOfMemory:", LADDER)
        self.assertIn("width=FILL_WIDTH", LADDER)

    def test_a_run_that_was_already_small_does_not_shrink_again(self):
        self.assertIn("if first <= FILL_WIDTH:", LADDER)
        self.assertIn("raise", LADDER)

    def test_the_width_can_be_dictated_from_outside(self):
        self.assertIn("width=None", inspect.signature(cr._run_propainter_at_width).__str__())
        self.assertIn("RETRY_WIDTH = int(width or fill_width_now())", WIDTH)

    def test_the_reason_is_written_down_where_the_next_reader_will_be(self):
        flat = " ".join(WIDTH.split())
        self.assertIn("quadratic", flat)
        self.assertIn("the only lever of the four that the viewer can see", flat)

    def test_the_normal_path_starts_at_the_full_temporal_window(self):
        """This function used to BE the out-of-memory retry, so it began at 24. As the normal
        path that is quality given away before anything went wrong."""
        self.assertIn("subvideo_length=40", WIDTH)


if __name__ == "__main__":
    unittest.main()
