"""After a pool swap the beat played one video and went on advertising another.

When a caption cannot be rebuilt, the render swaps the beat onto another window from the run's
own pool. That exit rewrote `clip`, `asset` and the in-point - everything the renderer reads -
but left `scrape_clip_id` pointing at the post the scene had just dropped.

The duplicate guard keys on `scrape_clip_id` where it exists. So two beats swapped onto the SAME
pool source still carried their two different old ids, read as two different videos, and the
repeat went out. That is the failure the guard exists to catch, hidden by the field it trusts.
"""

import inspect
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent_core


class TheSwapCarriesTheNewPostId(unittest.TestCase):
    def setUp(self):
        self.src = inspect.getsource(agent_core.prepare_timeline_clips)

    def test_the_swap_writes_the_new_id(self):
        exit_block = self.src[self.src.index('_scene["clip"] = _scene["asset"] = swap[0]'):][:900]
        self.assertIn('_scene["scrape_clip_id"] = swap[2]', exit_block)

    def test_and_drops_the_old_one_when_the_new_source_has_no_id(self):
        """A stale id is worse than none: none makes the guard fall back to the file name."""
        exit_block = self.src[self.src.index('_scene["clip"] = _scene["asset"] = swap[0]'):][:900]
        self.assertIn('_scene.pop("scrape_clip_id", None)', exit_block)

    def test_moving_the_in_point_does_not_touch_the_id(self):
        """That exit keeps the same source - only the second the edit cuts in changes."""
        move_block = self.src[self.src.index("keeps {_path.name} but moves its"):
                              self.src.index("swap = _clean_replacement_from_pool")]
        self.assertNotIn("scrape_clip_id", move_block)


class ThePoolReportsWhichPostItHandedOver(unittest.TestCase):
    def test_the_picker_returns_the_source_id(self):
        src = inspect.getsource(agent_core._clean_replacement_from_pool)
        self.assertIn("return dest.name, round(best[2], 3), best[3]", src)
        self.assertIn('str(cand.get("source_id") or "")', src)


if __name__ == "__main__":
    unittest.main()
