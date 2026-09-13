"""Scene 13 started at 30.95s in a file 25.0s long, and scene 11 described footage it no longer had.

The duplicate guard swaps a repeated clip for an unused one from the pool. It rewrote `clip`,
`asset` and `scrape_clip_id` - the comment above that line even says "ALWAYS re-point the source
id at the file the scene now plays" - and left the in-point, and the vision verdict, describing
the file that had just been dropped.

Measured on the roundabouts Short (2026-09-09), from its own report:

    scene 13   in-point 30.95s   that window belongs to tiktok__7532847965329755400, a 60.3s source
               file             tiktok__7351667105453870368, 25.0s (750 frames at 30fps)
               -> the seek lands past the end of the file

    scene 11   v4_reason "the Traffic Light Tree sculpture on a UK roundabout with lights cycling"
               on screen: an operating theatre

No window anywhere in that report starts at 30.95 for the file scene 13 holds; the number came
from another video entirely. Both scenes also carried an empty `scrape_clip_id`, so the guard
that made the swap could not see its own work afterwards.

Two defences, because the guard is one route to this and not the only one:
  * the swap resets the in-point and drops the stale verdict, and flags the beat;
  * the render clamps ANY in-point that is not inside its own file, whatever wrote it.
"""

import inspect
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent_core

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TheSwapTakesTheInPointWithIt(unittest.TestCase):
    def setUp(self):
        self.src = inspect.getsource(agent_core.enforce_unique_scene_clips)
        self.swap = self.src[self.src.index('sc["clip"] = repl["name"]'):]

    def test_the_in_point_is_reset(self):
        self.assertIn('sc["seedance_start_trim"] = 0.0', self.swap)

    def test_the_other_two_offset_names_are_reset_as_well(self):
        for key in ("source_trim", "start_trim"):
            self.assertIn(key, self.swap)

    def test_the_verdict_about_the_old_clip_is_dropped(self):
        self.assertIn('sc.pop(_verdict, None)', self.swap)
        self.assertIn('"v4_reason"', self.swap)

    def test_the_beat_is_flagged_rather_than_looking_finished(self):
        self.assertIn('sc["needs_replacement"] = True', self.swap)
        self.assertIn('sc["coverage_gap"] = True', self.swap)

    def test_the_source_id_still_follows_the_file(self):
        self.assertIn('sc["scrape_clip_id"] = repl["clip_id"] or ""', self.swap)


class TheRenderRefusesAnInPointOutsideItsFile(unittest.TestCase):
    def setUp(self):
        self.src = inspect.getsource(agent_core.prepare_timeline_clips)

    def test_the_clamp_exists_and_probes_the_real_file(self):
        self.assertIn("_tools._probe_duration(str(path)", self.src)

    def test_an_in_point_past_the_end_goes_to_zero(self):
        block = self.src[self.src.index("if _trim_now >= _dur:"):][:400]
        self.assertIn("_fixed = 0.0", block)

    def test_an_overhang_keeps_as_much_of_the_late_moment_as_fits(self):
        block = self.src[self.src.index("elif _trim_now + _shown > _dur"):][:300]
        self.assertIn("_fixed = max(0.0, _dur - _shown)", block)

    def test_it_runs_before_anything_reads_the_window(self):
        clamp = self.src.index("_tools._probe_duration(str(path)")
        bars = self.src.index("_rebuilt = _repair_dead_bands(path, scene, name)")
        self.assertLess(clamp, bars,
                        "the bars are measured over a window that may not exist yet")

    def test_the_repaired_beat_is_flagged(self):
        block = self.src[self.src.index("if _fixed is not None:"):][:700]
        self.assertIn('scene["needs_replacement"] = True', block)


if __name__ == "__main__":
    unittest.main()
