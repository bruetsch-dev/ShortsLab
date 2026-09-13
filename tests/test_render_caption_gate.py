"""Every clip that actually carries a caption goes through the removal pass before render.

Measured 2026-09-02 on a finished Short (`japanese_convenience_stores_look_effortless_but_the`):
five of thirteen scenes still had Japanese creator text burned in at render time - 4.8%, 2.8%,
10.0%, 10.0% and 10.1% of the frame - while only four scenes had been cleaned. The gate read
`scene["blur_captions"]`, which the scraper sets only where the vision review happened to call the
caption "plain_small"; text it described any other way went straight to the render untouched.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent_core
import scrape_v4


class RenderCaptionGate(unittest.TestCase):

    def setUp(self):
        with open(agent_core.__file__, encoding="utf-8") as handle:
            self.core = handle.read()
        with open(scrape_v4.__file__, encoding="utf-8") as handle:
            self.v4 = handle.read()

    def test_the_clip_is_measured_when_no_flag_says_to_clean_it(self):
        """It is measured AT THE SCENE'S OWN IN-POINT.

        This used to assert the call without a start, which is what the code did: it read the
        first 2.2s of the file while the edit cuts in somewhere else entirely - 36s into the
        source, for scene 01 of the eating-walk Short. Wrong in both directions. A clip clean at
        its head and captioned where the scene starts was never cleaned; one captioned only at
        its head was cleaned for nothing.
        """
        self.assertIn("_cov = _capcheck.caption_coverage(", self.core)
        self.assertIn('start=_seconds_of(scene, "seedance_start_trim", "source_trim",',
                      self.core)
        self.assertIn("if 0.015 < _cov <= _capcheck.MAX_COVERAGE:", self.core)

    def test_an_explicit_user_toggle_still_decides(self):
        """The measurement only runs when the user has not chosen for this scene."""
        self.assertIn("and sid not in blur_by_id:", self.core)

    def test_a_filled_beat_prefers_a_source_that_is_not_on_screen_yet(self):
        """Scenes 09 and 11 of that same Short were both the same snack-aisle clip.

        A repeat is now allowed as a LAST resort - an unfilled beat is worse than a second
        window of a clip already used - but only after every unused source is gone.

        The picking lives in _pick_coverage_fill since the fill learned about fingerprints, so
        this exercises it rather than reading the source text.
        """
        def cand(source_id, fingerprint, start=0.0):
            return scrape_v4.Candidate(source_id, "snack aisle", f"{source_id}.mp4", start,
                                       start + 2.75, 5.0, "", "available",
                                       fingerprint=fingerprint)

        on_screen = cand("aisle", "0e08080e1f3f3f7f", start=20.0)
        fresh = cand("other", "3f7d3c2c67030100")
        pick, repeats = scrape_v4._pick_coverage_fill(
            [on_screen, fresh], {"snack aisle"}, {"aisle": 1}, [("aisle", 0.0, 2.75)],
            [("aisle", "0e08080e1f3f3f7f", "0e08080e1f3f3f7f")])
        self.assertEqual(pick.source_id, "other")
        self.assertFalse(repeats)
        # ...and with nothing unused left the beat is still filled, flagged as a repeat.
        pick, repeats = scrape_v4._pick_coverage_fill(
            [on_screen], {"snack aisle"}, {"aisle": 1}, [("aisle", 0.0, 2.75)],
            [("aisle", "0e08080e1f3f3f7f", "0e08080e1f3f3f7f")])
        self.assertEqual(pick.source_id, "aisle")
        self.assertTrue(repeats)


if __name__ == "__main__":
    unittest.main()
