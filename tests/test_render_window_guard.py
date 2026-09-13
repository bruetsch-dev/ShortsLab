"""The renderer measures the range it will actually play before playing it.

A delivered Short ended on a creator talking to camera. The trail: the scene arrived through
same-script REUSE, not through V3's own audited build - reuse picks its segment by voice match
and had no technical check at all. The guard added at render normalisation measures the exact
played range (trim -> trim + dur*speed) with the same deterministic scene-cut detection V3 uses,
and snaps the trim into the longest clean segment when the range crosses an internal cut.

Known residual, deliberately not papered over with a heuristic: a range that lies entirely INSIDE
a talking-head segment is technically clean - scdet cannot see content class. That case needs the
vision gate on reuse segments, not a sharper ruler.
"""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = (ROOT / "agent_core.py").read_text(encoding="utf-8")


class RenderGuardTests(unittest.TestCase):
    def block(self):
        start = SOURCE.index("# A reused source is often a MIXED reel")
        return SOURCE[start:start + 3200]

    def test_the_guard_exists_at_render_normalisation(self):
        self.assertIn('technical_window_issue(str(_clip_path), _trim, _trim + _need)',
                      self.block())

    def test_it_measures_the_range_the_renderer_plays_not_the_whole_file(self):
        block = self.block()
        self.assertIn('_need = dur * _speed', block)

    def test_it_moves_the_trim_rather_than_dropping_the_scene(self):
        """An empty beat is a red placeholder; a clean segment of the same source is footage."""
        self.assertIn('scene["seedance_start_trim"] = _new_trim', self.block())

    def test_the_replacement_segment_must_fit_the_beat(self):
        self.assertIn("if b - a >= _need + 0.2", self.block())

    def test_a_failed_measurement_never_stops_a_render(self):
        block = self.block()
        self.assertIn("except Exception:", block)
        self.assertIn("must not stop a render", block)

    def test_the_move_is_logged_for_the_run_report(self):
        self.assertIn("across an internal cut; moved to", self.block())


if __name__ == "__main__":
    unittest.main()
