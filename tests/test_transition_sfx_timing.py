"""A transition sound must HIT on the cut, not start on it.

Reported repeatedly: "die sound die man bei dem transition da einfügen kann, werden nie genau an
der transition stelle eingefügt, immer zu spät".

A transition sound starts quiet - its transient arrives 8-14ms in for a click, up to 250ms for a
riser - so playing the FILE at the cut time puts the audible moment after the cut. `pipeline`
already corrected for that, but only `if ov.get("start_abs") is None`, on the reasoning that a
sound the user dragged should stay put. The timeline editor writes `start_abs` for EVERY sound it
round-trips, moved or not, so once the editor became the normal path the correction never ran
again and every transition sat late by its own attack time.

The fix separates "has a position" from "a person chose that position".
"""
import glob
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent_core
import app
import pipeline


class TheAnchorIsReal(unittest.TestCase):
    """The correction is only worth anything if the measured lead-in is non-zero."""

    def test_a_click_has_a_measurable_lead_in(self):
        hits = [f for f in glob.glob("soundeffects/**/*.mp3", recursive=True)
                if "ding-click" in os.path.basename(f)]
        if not hits:
            self.skipTest("the bundled click sound is not present")
        anchor = pipeline.sfx_transient_anchor(hits[0])
        self.assertGreater(anchor, 0.004, "no lead-in measured, so nothing to correct")
        self.assertLess(anchor, 0.25)


class OnlyAHumanPositionIsLeftAlone(unittest.TestCase):

    def test_the_render_corrects_unless_a_person_moved_the_sound(self):
        with open(pipeline.__file__, encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn('if is_transition and not ov.get("start_abs_user"):', source)
        self.assertNotIn('if is_transition and ov.get("start_abs") is None:', source)

    def test_the_editor_marks_a_sound_it_actually_moved(self):
        with open(app.__file__, encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn("fx.start_abs_user=true;", source)
        self.assertIn("start_abs_user:!!f.start_abs_user", source)
        self.assertIn("start_abs_user:!!t.start_abs_user", source)

    def test_the_flag_survives_the_save(self):
        config = {"scenes": [{"id": "01", "start": 0.0, "end": 4.0, "dur": 4.0}]}
        edits = {"scenes": [{"id": "01", "dur": 4.0}],
                 "sfx": [{"id": "fx1", "start_abs": 3.5, "start_abs_user": True},
                         {"id": "fx2", "start_abs": 7.0}]}
        agent_core.apply_timeline_edits_to_config(config, edits, "test-slug",
                                                  prepare_media=False, create_media=False)
        overrides = config.get("sfx_overrides") or {}
        self.assertTrue(overrides.get("fx1", {}).get("start_abs_user"),
                        "a hand-placed sound lost its flag")
        self.assertFalse(overrides.get("fx2", {}).get("start_abs_user"),
                         "an untouched sound was marked as hand-placed")

    def test_an_untouched_sound_still_carries_its_position(self):
        """The position itself must survive - only its MEANING changed."""
        config = {"scenes": [{"id": "01", "start": 0.0, "end": 4.0, "dur": 4.0}]}
        agent_core.apply_timeline_edits_to_config(
            config, {"scenes": [{"id": "01", "dur": 4.0}],
                     "sfx": [{"id": "fx2", "start_abs": 7.0}]}, "test-slug",
            prepare_media=False, create_media=False)
        self.assertEqual(config["sfx_overrides"]["fx2"]["start_abs"], 7.0)


if __name__ == "__main__":
    unittest.main()
