"""The in-point the vision review chose is the in-point the edit plays.

`pipeline.seedance_clip_start_trim` answered two different questions with one clamp:

  * the CONFIG value is a lead-in for generated clips - skip the first half second of a Seedance
    render. Three seconds is a sane ceiling for that.
  * the SCENE value is the in-point of the window the review picked out of a scraped source, and
    a TikTok is often two minutes long.

Both were clamped to 3.0s, so every scraped scene whose window started later played second 3.0 of
its source instead - in the render AND in the timeline editor. Measured on the finished projects
of 2026-09-05:

    konbini   9 of 10 scenes had an in-point past 3s, up to 47.6s, no win_ cut to save them
    couples   7 of 10, up to 27.9s

That is the mechanism behind the complaints about irrelevant footage: a window reviewed as "a
smiling person holding up a phone case with couple photos" (27.87s) reached the screen as a wooden
mallet standing in a yard, captioned COUPLES - because second 3.0 of that TikTok is a wooden
mallet. The reviewer was right; the edit never went where it said.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pipeline


class TheSceneKeepsItsInPoint(unittest.TestCase):
    def test_a_late_window_is_not_pulled_back_to_three_seconds(self):
        for value in (4.949, 6.272, 17.919, 27.868, 41.547, 47.645):
            self.assertAlmostEqual(
                pipeline.seedance_clip_start_trim({}, {"seedance_start_trim": value}), value,
                msg=f"{value}s was clamped")

    def test_an_early_window_is_untouched(self):
        self.assertAlmostEqual(pipeline.seedance_clip_start_trim({}, {"seedance_start_trim": 0.3}), 0.3)

    def test_a_negative_value_is_still_refused(self):
        self.assertEqual(pipeline.seedance_clip_start_trim({}, {"seedance_start_trim": -4}), 0.0)


class TheGeneratedLeadInKeepsItsCeiling(unittest.TestCase):
    """The clamp was right for the thing it was written for."""

    def test_the_config_default_is_still_capped_at_three_seconds(self):
        self.assertEqual(pipeline.seedance_clip_start_trim({"seedance_clip_start_trim": 9}), 3.0)

    def test_the_default_is_half_a_second(self):
        self.assertAlmostEqual(pipeline.seedance_clip_start_trim({}), 0.5)
        self.assertAlmostEqual(pipeline.seedance_clip_start_trim({}, {}), 0.5)

    def test_a_broken_scene_value_falls_back_instead_of_crashing(self):
        self.assertAlmostEqual(pipeline.seedance_clip_start_trim({}, {"seedance_start_trim": "x"}), 0.5)


class TheEditorAsksTheSameFunction(unittest.TestCase):
    def test_the_timeline_payload_uses_it(self):
        """The editor showed second 3.0 too - same call, same clamp."""
        import inspect
        import app
        body = inspect.getsource(app)
        self.assertIn("source_trim = pipeline.seedance_clip_start_trim(config, scene)", body)


if __name__ == "__main__":
    unittest.main()
