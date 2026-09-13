"""A chapter the platforms cannot fill must not throw the whole run away.

Scrape V3 used to raise on the first uncovered beat, so a Short with two good chapters and one
thin one produced nothing at all - and the footage it HAD downloaded was unusable without
starting over. It now borrows a shot for the empty beat, flags it, and the timeline editor draws
that flag as a red border so the improvised picture is obvious and can be swapped by hand.
"""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class DeliverAndMarkTests(unittest.TestCase):
    def setUp(self):
        self.source = (ROOT / "scrape_v3.py").read_text(encoding="utf-8")

    def test_a_thin_chapter_no_longer_ends_the_run(self):
        self.assertNotIn("could not build a unique, fully covered", self.source,
                         "V3 still refuses instead of delivering what it found")

    def test_the_final_audit_no_longer_refuses_either(self):
        self.assertNotIn("refused to publish an invalid edit", self.source)

    def test_an_empty_beat_is_filled_and_flagged(self):
        self.assertIn('scene["coverage_gap"] = True', self.source)
        self.assertIn('scene["coverage_gap_reason"]', self.source)

    def test_a_repeated_shot_is_flagged_too(self):
        """Reuse is still a defect worth seeing - it just no longer costs the run."""
        block = self.source[self.source.index("for scene in new_scenes:"):]
        self.assertIn("this shot is used more than once", block)


class EditorMarkTests(unittest.TestCase):
    def setUp(self):
        self.app = (ROOT / "app.py").read_text(encoding="utf-8")

    def test_the_flag_reaches_the_editor(self):
        self.assertIn('"coverage_gap": bool(scene.get("coverage_gap"))', self.app)

    def test_the_block_gets_a_red_border(self):
        self.assertIn("s.coverage_gap?' coverage-gap':''", self.app)
        self.assertIn(".tl-clip.coverage-gap", self.app)
        self.assertIn("#ff5a52", self.app)

    def test_it_is_readable_without_hovering(self):
        self.assertIn("NO MATCH", self.app)


if __name__ == "__main__":
    unittest.main()


class EmptyRunTests(unittest.TestCase):
    """Deliver-and-flag only makes sense when something WAS found.

    A real run (japan_has_lockers_just_for_wet_umbrellas) came back with 11 beats and 0 clips,
    rendered thirty silent placeholder cards reading "SCENE 01", and the batch filed it as
    delivered with no defects. A slate video is a failure wearing a delivered filename.
    """

    def setUp(self):
        self.source = (ROOT / "scrape_v3.py").read_text(encoding="utf-8")

    def test_a_run_with_no_footage_at_all_is_refused(self):
        self.assertIn('covered = [s for s in new_scenes if s.get("clip")]', self.source)
        self.assertIn("if not covered:", self.source)
        block = self.source[self.source.index("if not covered:"):]
        self.assertIn("raise RuntimeError(", block[:400])

    def test_an_unfillable_beat_is_still_flagged(self):
        """The flag has to be set before the stand-in is looked for, or the very run that most
        needs red borders gets none."""
        block = self.source[self.source.index("    if empty:"):]
        block = block[:block.index("for scene in new_scenes:")]
        flag_at = block.index('scene["coverage_gap"] = True')
        fill_at = block.index('scene["clip"] = Path(stand_in.path).name')
        self.assertLess(flag_at, fill_at, "the beat is only flagged when it can be filled")

    def test_a_partial_run_still_delivers(self):
        """The user's rule: show the material that IS there. Only the empty case refuses."""
        block = self.source[self.source.index("covered = [s for s in new_scenes"):]
        self.assertIn("if not covered:", block[:200])
        self.assertNotIn("if len(covered) < len(new_scenes)", block[:600])
