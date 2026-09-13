"""A clip that is a little short is slowed, never frozen.

Reported 2026-09-02: "da is standbild am schluss bei fast allen clips" - and it was systematic:

    agent_core:  per_clip = min(2.75, max(2.25, LONGEST beat))   # one length for the whole run
    scrape_v4:   _windows(duration, required) clamps to MAX_SHOT = 2.75

Every window was the same length, taken from the longest beat and capped at 2.75s, so any beat
longer than its window held its final frame for the remainder. V2 already stretched instead
(`timeline_speed` + `timeline_speed_src`, floor 0.88); V4 had no equivalent.

Measured across 374 beats in 12 real projects: average 1.9-2.1s, and 91% are at or under 2.75s -
so the stretch covers the great majority outright. The remaining 9% are longer than one shot
should be and want splitting into two cuts, not more slowing.
"""

import unittest
from pathlib import Path

import scrape_v4 as v4


class FloorTests(unittest.TestCase):
    def test_the_floor_is_the_requested_value(self):
        self.assertEqual(v4.V4_STRETCH_FLOOR, 0.84)

    def test_it_is_not_slower_than_slow_motion(self):
        """Past ~15% the retime reads as slow motion rather than as a normal shot."""
        self.assertGreaterEqual(v4.V4_STRETCH_FLOOR, 0.80)
        self.assertLess(v4.V4_STRETCH_FLOOR, 1.0)


class FitTests(unittest.TestCase):
    def scene(self):
        return {"clip": "v4_00_123.mp4"}

    def test_an_exact_fit_is_left_alone(self):
        scene = self.scene()
        self.assertEqual(v4._fit_clip_to_beat(scene, 2.30, 2.30), 0.0)
        self.assertNotIn("timeline_speed", scene)

    def test_a_longer_clip_is_left_alone(self):
        scene = self.scene()
        self.assertEqual(v4._fit_clip_to_beat(scene, 2.30, 2.00), 0.0)
        self.assertNotIn("timeline_speed", scene)

    def test_a_short_clip_is_slowed_to_cover_the_beat_exactly(self):
        scene = self.scene()
        speed = v4._fit_clip_to_beat(scene, 2.30, 2.60)
        self.assertGreater(speed, v4.V4_STRETCH_FLOOR)
        self.assertAlmostEqual(2.30 / speed, 2.60, places=2)

    def test_it_never_goes_below_the_floor(self):
        scene = self.scene()
        speed = v4._fit_clip_to_beat(scene, 2.30, 6.00)
        self.assertEqual(speed, v4.V4_STRETCH_FLOOR)

    def test_it_names_the_source_the_renderer_must_retime(self):
        """Without timeline_speed_src the renderer has nothing to slow."""
        scene = self.scene()
        v4._fit_clip_to_beat(scene, 2.30, 2.60)
        self.assertEqual(scene["timeline_speed_src"], "v4_00_123.mp4")

    def test_the_typical_beat_is_covered_completely(self):
        """91% of 374 measured beats are <= 2.75s; those must come out with no hold at all."""
        for need in (1.90, 2.10, 2.40, 2.60, 2.74):
            scene = self.scene()
            speed = v4._fit_clip_to_beat(scene, 2.30, need)
            covered = 2.30 / speed if speed else 2.30
            self.assertGreaterEqual(covered + 0.01, need, f"{need}s beat only covered {covered}s")

    def test_rubbish_input_is_not_a_crash(self):
        scene = self.scene()
        self.assertEqual(v4._fit_clip_to_beat(scene, None, 2.0), 0.0)
        self.assertEqual(v4._fit_clip_to_beat(scene, "x", "y"), 0.0)
        self.assertEqual(v4._fit_clip_to_beat(scene, 0, 0), 0.0)


class WiringTests(unittest.TestCase):
    def body(self):
        """The assignment block, bounded by its own end rather than a character count.

        A fixed 1400-character slice stopped reaching the call it checks as soon as the excerpt
        extension was added above it - the test failed on a comment, not on behaviour.
        """
        source = Path(v4.__file__).read_text(encoding="utf-8")
        start = source.index('"v4_reason": chosen.reason})')
        end = source.index("out_scenes[_pos] = copy; scene_clips[_pos] = str(dest)", start)
        return source[start:end]

    def test_the_assignment_fits_every_chosen_clip(self):
        self.assertIn("_fit_clip_to_beat(copy,", self.body())

    def test_it_uses_the_beat_length_not_the_global_window(self):
        block = self.body()
        self.assertIn('scene.get("end")', block)
        self.assertIn('scene.get("start")', block)

    def test_it_says_what_it_did(self):
        """A silent retime is impossible to review afterwards."""
        self.assertIn("instead of holding a still", self.body())


if __name__ == "__main__":
    unittest.main()
