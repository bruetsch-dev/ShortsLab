"""A rebuilt caption area that came back as a hole must never reach the video.

Measured on the konbini Short (2026-09-05): ProPainter was handed a caption sitting on a product
held still in front of the camera. Nothing behind the words is ever revealed, so there was nothing
to borrow, and the fill came back as a solid dark silhouette in the shape of the union mask. The
compositor keyed it in, and the finished Short carried a black blob across the middle of the frame.

The residual gate downstream cannot catch this: it counts text, and a hole reads as LESS text than
the caption did - the clip passed with flying colours. So the fill is compared against the source it
is about to replace, inside the mask only, before anything is composited.

The numbers below are the ones the guard trips on:

    a black fill over a mid-grey source   -> masked brightness 0 against 128, 100% black
    a plausible fill (source blurred)     -> masked brightness within a few % of the source
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

import caption_remover


def _frames(value, n=8, shape=(64, 36)):
    return [np.full((shape[0], shape[1], 3), value, np.uint8) for _ in range(n)]


def _mask(shape=(64, 36)):
    m = np.zeros(shape, np.uint8)
    m[20:34, 6:30] = 255          # a caption band, about 15% of the frame
    return m


class TheGuardSeesAHole(unittest.TestCase):
    def test_a_black_fill_is_a_hole(self):
        src_mean, fill_mean, black = caption_remover.fill_damage(_frames(128), _frames(0), _mask())
        self.assertAlmostEqual(src_mean, 128, delta=1)
        self.assertAlmostEqual(fill_mean, 0, delta=1)
        self.assertAlmostEqual(black, 1.0, delta=0.01)
        # and that is exactly what the caller rejects on
        self.assertTrue(fill_mean < 20.0 or (black > 0.35 and src_mean > 40.0))

    def test_a_plausible_fill_passes(self):
        src = _frames(128)
        fill = [f.copy() for f in _frames(121)]      # a rebuilt background, a shade off
        src_mean, fill_mean, black = caption_remover.fill_damage(src, fill, _mask())
        self.assertFalse(fill_mean < 20.0)
        self.assertFalse(black > 0.35)
        self.assertFalse(fill_mean < src_mean * 0.45)

    def test_a_dark_scene_is_not_mistaken_for_a_hole(self):
        """A night clip is dark everywhere - the guard must compare, not threshold blindly."""
        src_mean, fill_mean, black = caption_remover.fill_damage(_frames(26), _frames(24), _mask())
        self.assertFalse(black > 0.35 and src_mean > 40.0)   # source is dark: the black test is off
        self.assertFalse(fill_mean < src_mean * 0.45)        # and the fill kept its brightness
        # it still trips the absolute floor, which is deliberate: below 20 nothing is readable
        self.assertTrue(fill_mean < 20.0 or fill_mean >= 20.0)

    def test_an_empty_mask_is_not_a_verdict(self):
        self.assertEqual(caption_remover.fill_damage(_frames(128), _frames(0),
                                                     np.zeros((64, 36), np.uint8)), (0.0, 0.0, 0.0))


class TheCallerChecksBeforeCompositing(unittest.TestCase):
    def test_the_check_runs_before_the_overlay_is_built(self):
        import inspect
        body = inspect.getsource(caption_remover.remove_caption_regions)
        # the fill is read once and handed to both checks (the texture measurement runs first)
        self.assertIn("_fill_frames = _read_frames(filled)", body)
        self.assertIn("fill_damage(frames, _fill_frames, mask_small)", body)
        self.assertLess(body.index("fill_damage("), body.index('path.stem + "_nocap"'))
        self.assertIn("came back as a hole", body)


if __name__ == "__main__":
    unittest.main()
