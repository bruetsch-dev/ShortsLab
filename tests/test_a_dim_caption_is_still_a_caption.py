"""The run splitter called captioned frames clean, and the fill was blamed for the leftovers.

Inpainting a frame that carries no text is pure cost, so the pass splits a clip into the frame
ranges that carry the caption. It decided "clean" RELATIVELY - the geometric mean of the
brightest and dimmest frame scores - on a scale where, by `_text_frame_scores`' own definition,
an empty band sits at about 1 and the guard above already calls a whole clip textless under 2.0.

So on a clip whose caption is merely dimmer in places, frames scoring 4 or 6 fell under the line
and were passed through with the text still on them. Measured across six split clips
(2026-09-09), the tenth-percentile frame scored:

    4.05   1.67   1.41   4.66   6.31   0.72

In five of the six, the frames the split called clean carried 2.4-8.3% text. The fill was then
rejected downstream for "leaving" a caption it had never been given. Re-running one of those
clips across the whole window produced a genuinely clean result - the split, not ProPainter, had
been the problem.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

import caption_remover


class RunsFromScores(unittest.TestCase):
    """`_text_frame_runs` reads a clip; these drive it through the scores it would compute."""

    def runs_for(self, scores):
        mask = np.zeros((80, 60), np.uint8)
        mask[30:50, 10:50] = 255
        original = caption_remover._text_frame_scores
        caption_remover._text_frame_scores = lambda *_a, **_k: list(scores)
        try:
            return caption_remover._text_frame_runs("unused.mp4", mask)
        finally:
            caption_remover._text_frame_scores = original

    def test_a_real_split_still_splits(self):
        """The case this was built for: text stops, and the empty frames sit at about 1."""
        runs = self.runs_for([12.0] * 20 + [1.0] * 20)
        self.assertIsNotNone(runs, "a clip that genuinely goes clean is no longer split")
        self.assertEqual(1, len(runs))
        self.assertLess(runs[0][1], 40)

    def test_a_dimmer_stretch_of_the_same_caption_is_not_called_clean(self):
        """15.0 against 4.0: the geometric mean is 7.7, so the 4.0 frames were skipped - and
        4.0 is four times what an empty band reads."""
        self.assertIsNone(self.runs_for([15.0] * 20 + [4.0] * 20),
                          "frames scoring 4.0 were treated as carrying no text")

    def test_the_floor_is_the_scale_the_module_already_uses(self):
        self.assertEqual(2.0, caption_remover.TEXT_FRAME_FLOOR)
        self.assertIn("min(math.sqrt(high * max(low, 0.05)), TEXT_FRAME_FLOOR)",
                      open(os.path.join(os.path.dirname(os.path.dirname(
                          os.path.abspath(__file__))), "caption_remover.py"),
                          encoding="utf-8").read())

    def test_a_clip_with_no_caption_anywhere_is_still_refused(self):
        self.assertIsNone(self.runs_for([1.1] * 40))

    def test_a_caption_on_every_frame_is_still_refused(self):
        """Nothing to skip - and the old relative line would have carved this one up too."""
        self.assertIsNone(self.runs_for([11.0, 12.0, 13.0] * 14))

    def test_a_single_dim_flicker_does_not_break_a_run(self):
        runs = self.runs_for([12.0] * 18 + [1.9] + [12.0] * 18 + [1.0] * 12)
        self.assertIsNotNone(runs)
        self.assertEqual(1, len(runs), "a one-frame dip split the caption into two runs")


if __name__ == "__main__":
    unittest.main()
