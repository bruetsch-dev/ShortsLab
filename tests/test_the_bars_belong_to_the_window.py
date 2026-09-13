"""A quarter of the screen went out black, in an export whose log says the letterbox pass ran.

The render measures a scene's dead bands at its in-point and rebuilds the frame. The caption
stage runs AFTER that and can move the in-point - it slides the window to get away from burned-in
text the fill could not remove - or swap the clip for another one entirely. Nothing re-measured.

Measured in the school-rules export of 2026-09-09:

    scene 05   bands measured and repaired at 9.06s
               caption stage: "keeps capblur_05... but moves its in-point 9.06s -> 11.40s to
               miss the text"
               finished video at 8.2s: 256 black rows at the top, 196 at the bottom, of 1920

A band is a property of the window, not of the file. Any exit that changes which window plays
has to measure again.
"""

import inspect
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent_core


class EveryExitThatMovesTheWindowRemeasures(unittest.TestCase):
    def setUp(self):
        self.src = inspect.getsource(agent_core.prepare_timeline_clips)

    def test_the_repair_is_reusable_rather_than_a_one_shot_block(self):
        self.assertIn("def _repair_dead_bands(", self.src)

    def test_it_runs_before_the_caption_stage(self):
        """Text printed on a matte disappears with the matte, so bars come first."""
        first = self.src.index("_rebuilt = _repair_dead_bands(path, scene, name)")
        captions = self.src.index("coverage = caption_remover.caption_coverage(")
        self.assertLess(first, captions)

    def test_a_moved_in_point_measures_again(self):
        move = self.src[self.src.index("but moves its "):]
        self.assertIn("_repair_dead_bands(_path, _scene, _path.name)",
                      move[:move.index("return True") + 20])

    def test_a_swapped_clip_measures_again(self):
        swap = self.src[self.src.index('_scene["clip"] = _scene["asset"] = swap[0]'):]
        self.assertIn("_repair_dead_bands(_path.parent / swap[0], _scene, swap[0])",
                      swap[:swap.index("return True") + 20])

    def test_the_measurement_is_taken_over_the_scene_s_own_seconds(self):
        body = self.src[self.src.index("def _repair_dead_bands("):]
        body = body[:body.index("return None")]
        self.assertIn('_seconds_of(_scene, "seedance_start_trim"', body)
        self.assertIn("end=_trim + _shown", body)


if __name__ == "__main__":
    unittest.main()
