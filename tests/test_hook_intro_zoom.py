"""The intro does not stop - it flies into the query it just typed.

A hook clip that simply cuts to the first drawing reads as two videos glued together. The push-in
creeps for most of the clip and then accelerates into a blur over the last fraction of a second,
so the cut lands inside the movement.
"""

import unittest

import numpy as np

import sketch_hook_intro as hi


class ZoomCurveTests(unittest.TestCase):
    SPAN, RUSH_FROM = 4.0, 3.55

    def factor(self, t):
        return hi._zoom_factor(t, self.SPAN, self.RUSH_FROM, hi.ZOOM_CREEP, hi.ZOOM_PEAK)

    def test_it_starts_at_no_zoom(self):
        self.assertAlmostEqual(self.factor(0.0), 1.0, places=4)

    def test_the_creep_is_barely_visible(self):
        """Most of the clip must still read as a static screen recording."""
        self.assertLessEqual(self.factor(self.RUSH_FROM), 1.10)

    def test_it_ends_effectively_at_infinity(self):
        self.assertGreaterEqual(self.factor(self.SPAN), hi.ZOOM_PEAK - 0.01)

    def test_it_never_moves_backwards(self):
        values = [self.factor(t) for t in np.arange(0.0, self.SPAN + 0.01, 0.02)]
        for before, after in zip(values, values[1:]):
            self.assertGreaterEqual(after + 1e-9, before)

    def test_the_rush_accelerates_rather_than_slides(self):
        """Each later slice of the flight must cover more ground than the one before it."""
        rush = self.SPAN - self.RUSH_FROM
        steps = [self.factor(self.RUSH_FROM + rush * p / 5.0) for p in range(6)]
        gains = [b / a for a, b in zip(steps, steps[1:])]
        for before, after in zip(gains, gains[1:]):
            self.assertGreater(after, before, gains)

    def test_half_way_through_the_flight_it_is_still_readable(self):
        """The hook has just finished typing; blowing it up instantly would hide it."""
        self.assertLess(self.factor(self.RUSH_FROM + (self.SPAN - self.RUSH_FROM) * 0.5), 3.0)

    def test_it_can_be_switched_off(self):
        for peak in (0, 1.0, None):
            self.assertEqual(hi._zoom_factor(3.9, 4.0, 3.5, 1.08, peak or 0), 1.0)


class ZoomFrameTests(unittest.TestCase):
    def frame(self):
        return np.random.randint(0, 255, (192, 108, 3), dtype=np.uint8)

    def test_no_zoom_returns_the_frame_untouched(self):
        f = self.frame()
        self.assertIs(hi._zoom_frame(f, 1.0, (54, 96)), f)

    def test_the_size_never_changes(self):
        f = self.frame()
        for factor in (1.05, 2.0, 30.0, 500.0):
            self.assertEqual(hi._zoom_frame(f, factor, (54, 96)).shape, f.shape)

    def test_an_off_frame_target_cannot_crop_outside_the_picture(self):
        """A bad track must degrade to a centred zoom, never to a black bar or a crash."""
        f = self.frame()
        for target in ((-900, -900), (9000, 9000), (0, 0)):
            self.assertEqual(hi._zoom_frame(f, 6.0, target).shape, f.shape)

    def test_the_early_creep_stays_centred(self):
        """Committing to the search box at 1.05x would look like a drift, not a push-in."""
        f = np.zeros((192, 108, 3), dtype=np.uint8)
        f[0:20, 0:20] = 255                      # a marker in the far corner
        centred = hi._zoom_frame(f, 1.05, (54, 96))
        pulled = hi._zoom_frame(f, 1.05, (10, 10))
        self.assertLess(float(np.abs(centred.astype(int) - pulled.astype(int)).mean()), 12.0)


class WiringTests(unittest.TestCase):
    def test_every_frame_goes_through_the_camera(self):
        """Both write paths - painted and untrackable - must zoom, or the clip stutters."""
        source = open(hi.__file__, encoding="utf-8").read()
        loop = source[source.index("for index, (frame, entry) in enumerate"):]
        loop = loop[:loop.index("        # The hook is SPOKEN")]
        self.assertEqual(loop.count("cv2.imwrite"), 0,
                         "a frame is still written past the push-in")
        self.assertEqual(loop.count("write_frame(index, frame)"), 2,
                         "the painted and the untrackable path must both zoom")

    def test_the_flight_is_anchored_to_the_finished_hook(self):
        source = open(hi.__file__, encoding="utf-8").read()
        self.assertIn("rush_from = min(type_until / float(speed or 1.0), "
                      "span_real - ZOOM_MIN_RUSH_S)", source)


if __name__ == "__main__":
    unittest.main()
