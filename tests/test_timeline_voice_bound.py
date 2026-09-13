"""A clip may not be dragged past the end of the voiceover, and the end is a snap target.

Reported 2026-09-02: "ich konnte immernoch die clips länger ziehen als das actual voiceover. das
darf nicht sein., muss auch autofang haben, also das ende."

`clipMaxDur` capped only against the SOURCE clip's own length, so the timeline could grow past
the narration and the tail played over silence. The voiceover end was drawn as a marker but was
not one of the magnetic snap targets, so landing on it exactly was guesswork.
"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app


def editor_js():
    with open(app.__file__, encoding="utf-8") as handle:
        return handle.read()


class TimelineIsBoundedByTheVoice(unittest.TestCase):

    def setUp(self):
        self.source = editor_js()

    def test_the_duration_cap_subtracts_the_other_clips_from_the_voice(self):
        start = self.source.index("function clipMaxDur")
        body = self.source[start:start + 1200]
        self.assertIn("voiceDuration - others", body)
        self.assertIn("visualDur()", body)

    def test_the_end_of_the_voice_is_a_snap_target(self):
        start = self.source.index("function snapTime")
        body = self.source[start:start + 900]
        self.assertIn("targets.push(voiceDuration)", body)

    def test_snapping_still_works_with_no_cut_lines_at_all(self):
        """The old guard returned early when there were no beat lines, which would have skipped
        the voice-end target entirely."""
        start = self.source.index("function snapTime")
        body = self.source[start:start + 900]
        self.assertNotIn("beatTimesLocked===null || !beatTimesLocked.length) return t", body)

    def test_ctrl_still_turns_snapping_off(self):
        start = self.source.index("function snapTime")
        body = self.source[start:start + 900]
        self.assertIn("if(noSnap) return t;", body)


class FilledBeatsAreFlaggedForTheEditor(unittest.TestCase):
    """A beat filled with the best remaining source must show the editor's red border."""

    def test_the_scraper_sets_the_flag_the_editor_reads(self):
        import scrape_v4
        with open(scrape_v4.__file__, encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn('"coverage_gap": True,', source)
        self.assertIn("coverage_gap_reason", source)

    def test_the_editor_outlines_that_flag_in_red(self):
        self.assertIn(".tl-clip.coverage-gap { outline:2px solid #ff5a52", editor_js())


if __name__ == "__main__":
    unittest.main()
