"""Footage that changes has to say so, and the saved plan has to be the one that shipped.

Traced from a finished render on 2026-09-08. Scene 01's saved plan named
capblur_01_82494e43d3.mp4 at trim 36.411; the cleaned derivative was correct and the window cut
was correct, both verified frame by frame. The video showed v4_tiktok_7594422119228886292.mp4 -
a pool clip that appears nowhere in that plan, with no line in the render log.

Two causes, both here:

  the record was written before the work   render_project_timeline saved the config and THEN
                                           called prepare_timeline_clips, which is what swaps
                                           footage. The file therefore recorded the intention.
  one swap branch was silent               a clip with more text than the fill can rebuild is
                                           announced; a fill that RAN and was then refused goes
                                           through `elif look_elsewhere()`, which said nothing.
                                           That run refused sixteen fills.
"""

import inspect
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent_core

PREP = inspect.getsource(agent_core.prepare_timeline_clips)
RENDER = inspect.getsource(agent_core.render_project_timeline)


class EverySwapIsAnnounced(unittest.TestCase):
    def test_moving_the_in_point_says_so(self):
        self.assertIn("moves its", PREP)
        self.assertIn("to miss the text", PREP)

    def test_taking_a_pool_clip_says_which_one_and_which_one_it_replaced(self):
        self.assertIn("drops {_path.name} and takes", PREP)
        self.assertIn("{swap[0]} at {swap[1]:.2f}s instead", PREP)

    def test_giving_up_says_so_too(self):
        """Leaving the creator's text on screen is a decision, not a non-event."""
        self.assertIn("no clean window and nothing in the pool", PREP)

    def test_the_quiet_branch_is_named_in_the_source(self):
        """The `elif look_elsewhere()` path fires far more often than the loud one."""
        self.assertIn("this branch used to be", PREP)

    def test_the_scene_id_is_in_every_one_of_those_lines(self):
        """A log line that does not say which scene cannot be traced back to a frame."""
        for line in ("moves its", "drops {_path.name} and takes", "no clean window"):
            at = PREP.index(line)
            window = PREP[max(0, at - 260):at]
            self.assertIn("scene {_sid}", window, line)


class TheSavedPlanIsTheOneThatShipped(unittest.TestCase):
    def test_the_config_is_written_after_the_clips_are_prepared(self):
        first = RENDER.index("out_config_path.write_text")
        prep = RENDER.index("prepare_timeline_clips(config")
        second = RENDER.index("out_config_path.write_text", prep)
        self.assertLess(first, prep, "the early write is still there, which is fine")
        self.assertGreater(second, prep, "nothing rewrites the plan after the swaps")

    def test_the_reason_is_written_down(self):
        flat = " ".join(RENDER.split())
        self.assertIn("AND AGAIN, NOW THAT IT IS TRUE", flat)
        self.assertIn("the plan that actually shipped", flat)

    def test_both_writes_serialise_the_same_config(self):
        writes = re.findall(r"out_config_path\.write_text\(([^\n]*)", RENDER)
        self.assertEqual(len(writes), 2)
        self.assertEqual(writes[0].strip(), writes[1].strip())


if __name__ == "__main__":
    unittest.main()
