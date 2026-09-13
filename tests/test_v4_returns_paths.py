"""V4's finished edit must survive the hand-off into the project.

Measured 2026-09-02 on `in_strict_japanese_ramen_shops_customers_complete`: the V4 report said
`assigned 10 / 10`, all ten clips were copied into the project's media folder - and the saved
project came back with ONE clip and nine `uncovered_still` slates. V4 put bare filenames in the
`scene_clips` list it returns; the consumer in agent_core tests `Path(clip).exists()` against the
working directory before copying, and only resolved the name when the entry was EMPTY. A bare
filename is truthy, so it failed the test and the scene was discarded silently.

V3 has always returned `str(dest)`. This pins both halves of the fix.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent_core
import scrape_v4


class EnginesReturnUsablePaths(unittest.TestCase):

    def setUp(self):
        with open(scrape_v4.__file__, encoding="utf-8") as handle:
            self.v4 = handle.read()
        with open(agent_core.__file__, encoding="utf-8") as handle:
            self.core = handle.read()

    def test_the_assignment_returns_a_path(self):
        self.assertIn("out_scenes[_pos] = copy; scene_clips[_pos] = str(dest)", self.v4)
        self.assertNotIn("out_scenes.append(copy); scene_clips.append(name)", self.v4)

    def test_the_coverage_fill_returns_a_path(self):
        self.assertIn("scene_clips[index] = str(dest)", self.v4)

    def test_the_consumer_resolves_a_name_that_does_not_exist(self):
        """The deeper guard: a truthy but unusable entry must not drop the scene."""
        self.assertIn("if not clip or not Path(clip).exists():", self.core)
