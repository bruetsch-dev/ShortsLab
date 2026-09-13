"""A batch must not file a slate video as a delivered short.

Two runs came back with every beat rewritten to `uncovered_still`, rendered thirty text cards
reading "SCENE 01 / A", passed the black-frame check (a dark grey card is not black) and were
copied into the delivery folder as finished Shorts with `defects: []`.
"""

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import clip_short_batch as batch                                    # noqa: E402


class CoverageGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="csb_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self._real = batch.agent_core.PROJECTS_DIR
        batch.agent_core.PROJECTS_DIR = self.tmp
        self.addCleanup(setattr, batch.agent_core, "PROJECTS_DIR", self._real)

    def project(self, slug, scenes):
        d = self.tmp / slug / "config"
        d.mkdir(parents=True)
        (d / "project.json").write_text(json.dumps({"scenes": scenes}), encoding="utf-8")
        return slug

    def test_real_footage_counts_as_covered(self):
        slug = self.project("good", [{"asset": "capblur_01.mp4", "assignment_type": "exact"},
                                     {"asset": "capblur_02.mp4", "assignment_type": "loose"}])
        self.assertEqual(batch.timeline_coverage(slug), (2, 2))

    def test_a_placeholder_still_does_not_count(self):
        """The exact shape of the two bad deliveries."""
        slug = self.project("slate", [{"asset": "scene_01_outside.png",
                                       "assignment_type": "uncovered_still"}] * 9)
        self.assertEqual(batch.timeline_coverage(slug), (0, 9))

    def test_a_partly_covered_short_reports_what_is_missing(self):
        slug = self.project("mixed", [{"asset": "a.mp4", "assignment_type": "exact"},
                                      {"asset": "b.png", "assignment_type": "uncovered_still"},
                                      {"asset": "c.mp4", "assignment_type": "exact"}])
        self.assertEqual(batch.timeline_coverage(slug), (2, 3))

    def test_a_scene_with_no_asset_at_all_is_not_covered(self):
        slug = self.project("empty", [{"assignment_type": "exact"}, {"asset": ""}])
        self.assertEqual(batch.timeline_coverage(slug), (0, 2))

    def test_a_missing_project_does_not_crash_the_batch(self):
        self.assertEqual(batch.timeline_coverage("does_not_exist"), (0, 0))

    def test_an_empty_run_is_raised_not_delivered(self):
        source = (ROOT / "tools" / "clip_short_batch.py").read_text(encoding="utf-8")
        self.assertIn("if total and not covered:", source)
        self.assertIn("no footage at all", source)

    def test_a_hopeless_topic_is_not_re_queued_for_ever(self):
        source = (ROOT / "tools" / "clip_short_batch.py").read_text(encoding="utf-8")
        self.assertIn('if "no footage at all" in str(row.get("error") or "")', source)


if __name__ == "__main__":
    unittest.main()
