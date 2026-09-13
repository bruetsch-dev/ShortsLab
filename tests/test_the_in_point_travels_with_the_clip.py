"""A scraped clip is not a file name. It is a file name AND the second the edit cuts in.

A TikTok runs sixty to ninety seconds; a beat shows two of them. `plan_config` copied the file
name onto the config scene and dropped `seedance_start_trim`, so every pass that ran before the
enforcement merge restored it (the caption cleanup at agent_core.py:13651, the merge at :13784)
was looking at second 0 of a different moment.

Measured on the eating-walk Short (2026-09-09), with the remover's own measurement path:

    scene 01   refused at 22.2%   head of file 21.6%   the window the edit shows  8.2%
    scene 04   refused at 35.1%   head of file 34.4%   the window the edit shows  6.8%
    scene 15   refused at 34.6%   head of file 34.7%   the window the edit shows  5.5%

Scenes 04 and 15 are the same source at 4.15s and 22.08s and were refused at almost the same
number - which is only possible if neither in-point was used. All three sit under the 12%
ceiling where the edit actually cuts, so all three were cleanable, and all three went out with
the creator's Japanese text burned across them instead.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent_core

ROOT = Path(__file__).resolve().parent.parent


class TheInPointSurvivesThePlan(unittest.TestCase):
    def config_for(self, scene):
        with tempfile.TemporaryDirectory() as folder:
            return agent_core.plan_config(Path(folder), "T", scene["script"], 3.0,
                                          allow_seedance=False, max_seedance=0,
                                          static_gpt_image_count=0, scenes_override=[scene])

    def scene(self, **extra):
        base = {"id": "0", "start": 0.0, "end": 2.0, "script": "A stall.",
                "clip": "scraped_00.mp4", "source_has_captions": True,
                "seedance_start_trim": 37.55}
        base.update(extra)
        return base

    def test_the_second_the_edit_cuts_in_is_carried_onto_the_scene(self):
        config = self.config_for(self.scene())
        self.assertEqual(37.55, config["scenes"][0].get("seedance_start_trim"),
                         "the clip name survived planning without the moment it points at")

    def test_a_clip_with_no_offset_does_not_gain_a_false_one(self):
        scene = self.scene()
        scene.pop("seedance_start_trim")
        config = self.config_for(scene)
        self.assertIsNone(config["scenes"][0].get("seedance_start_trim"))

    def test_the_flag_the_editor_outlines_in_red_survives_too(self):
        """`needs_replacement` reached the saved config as None on all fifteen scenes."""
        config = self.config_for(self.scene(needs_replacement=True, coverage_gap=True))
        self.assertTrue(config["scenes"][0].get("needs_replacement"))
        self.assertTrue(config["scenes"][0].get("coverage_gap"))

    def test_and_the_clip_can_still_be_told_apart_from_its_neighbours(self):
        config = self.config_for(self.scene(scrape_clip_id="tiktok__1", scrape_source="tiktok"))
        self.assertEqual("tiktok__1", config["scenes"][0].get("scrape_clip_id"))


class AMovedInPointIsNotStampedBackOver(unittest.TestCase):
    """The enforcement merge runs ~170 lines AFTER the caption cleanup, and that pass may move
    the in-point: a window crossing an internal source cut is slid to the longest clean segment
    and the clip is cleaned there. Restoring the scrape's original trim afterwards would leave
    the render playing a window that was never cleaned. That branch was unreachable before the
    in-point was carried; carrying it made this live."""

    def test_the_merge_only_fills_a_gap(self):
        src = (ROOT / "agent_core.py").read_text(encoding="utf-8")
        self.assertIn('and _cs.get("seedance_start_trim") is None):', src)

    def test_the_pass_that_moves_it_can_actually_run(self):
        """It is gated on assignment_type or match_class, both of which plan_config dropped."""
        src = (ROOT / "agent_core.py").read_text(encoding="utf-8")
        self.assertIn('str(scene.get("assignment_type") or scene.get("match_class") or "")', src)
        self.assertIn('scene["seedance_start_trim"] = _new_trim', src)


class TheCleanerIsToldWhereToLook(unittest.TestCase):
    def test_the_removal_pass_is_given_the_scene_in_point(self):
        src = (ROOT / "agent_core.py").read_text(encoding="utf-8")
        call = src[src.index("found = _capfix.remove_caption_regions("):][:400]
        self.assertIn("start=_trim", call,
                      "the removal rebuilds whatever second it is pointed at; unset means 0")

    def test_and_that_in_point_comes_from_the_scene_not_from_zero(self):
        src = (ROOT / "agent_core.py").read_text(encoding="utf-8")
        self.assertIn('_trim = _seconds_of(scene, "seedance_start_trim", "source_trim", '
                      '"start_trim")', src)


if __name__ == "__main__":
    unittest.main()
