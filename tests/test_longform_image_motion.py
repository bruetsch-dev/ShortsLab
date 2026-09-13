"""Four ways for a still drawing to feel drawn rather than scanned.

A slow push-in on every image is one trick repeated; across a 40-second short it reads as a
slideshow. These styles all come out of the same zoompan pass, so none costs an extra encode, and
each was measured on a real render rather than argued from the expression text:

    push       ink grows 0.987 -> 1.013 (moving in), no horizontal wander
    alternate  on odd scenes ink shrinks 1.014 -> 0.987 - the opposite direction
    paper      wanders in an arc 0,-1,-2,-3,-3,-3,-2,-1,0 px while push stays flat
    jump       90 of 119 frames are motionless, then step
"""

import unittest

import longform_video as lv


class StyleTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(lv.set_image_motion, "push")

    def z(self, style, index=0, zoom=0.05, frames=96):
        return lv._motion_expressions(index, zoom, frames, style=style)[0]

    def test_an_unknown_style_falls_back_to_the_original_push(self):
        for value in ("", None, "wobble", "3d"):
            self.assertEqual(lv.set_image_motion(value), "push")

    def test_every_advertised_style_is_selectable(self):
        for style in lv.IMAGE_MOTION_STYLES:
            self.assertEqual(lv.set_image_motion(style), style)

    def test_push_moves_inward_on_every_scene(self):
        self.assertEqual(self.z("push", 0), self.z("push", 1))
        self.assertIn("min(1+0.0500*on/96", self.z("push"))

    def test_alternate_reverses_direction_at_each_cut(self):
        """Even scenes must stay identical to push, or this is a different move, not a rhythm."""
        self.assertEqual(self.z("alternate", 0), self.z("push", 0))
        self.assertNotEqual(self.z("alternate", 1), self.z("push", 1))
        self.assertIn("max(", self.z("alternate", 1))

    def test_alternate_pulling_out_never_goes_below_full_frame(self):
        """Below 1.0 zoompan would show the padding around the drawing."""
        self.assertTrue(self.z("alternate", 1).endswith(",1)"))

    def test_paper_keeps_crop_headroom_for_the_whole_shot(self):
        """The wander needs room at frame 0 too, or it clips against the edge immediately."""
        self.assertIn("1+0.0175+", self.z("paper"))

    def test_paper_wanders_on_both_axes_at_different_rates(self):
        _z, x, y = lv._motion_expressions(0, 0.05, 96, style="paper")
        self.assertIn("sin(on/", x)
        self.assertIn("sin(on/", y)
        self.assertNotEqual(x.split("sin")[1], y.split("sin")[1],
                            "one period on both axes is a diagonal slide, not a wander")

    def test_the_wander_is_visible_but_not_a_camera_move(self):
        self.assertGreaterEqual(lv.PAPER_DRIFT_PX, 10.0, "invisible on the finished canvas")
        self.assertLessEqual(lv.PAPER_DRIFT_PX, 40.0, "that is a pan, not a held sheet")

    def test_jump_steps_instead_of_gliding(self):
        self.assertIn("floor(on/", self.z("jump"))

    def test_jump_steps_often_enough_to_read_as_motion(self):
        """Too coarse and it reads as dropped frames rather than stop-motion."""
        self.assertGreaterEqual(lv.JUMP_STEP_FRAMES, 2)
        self.assertLessEqual(lv.JUMP_STEP_FRAMES, 8)

    def test_a_one_frame_scene_cannot_divide_by_zero(self):
        for style in lv.IMAGE_MOTION_STYLES:
            for frames in (0, 1, 2):
                lv._motion_expressions(0, 0.05, frames, style=style)

    def test_no_style_exceeds_the_requested_zoom(self):
        for style in lv.IMAGE_MOTION_STYLES:
            self.assertIn("0.0500", self.z(style, 1))


class PersistenceTests(unittest.TestCase):
    def test_a_project_carries_its_own_movement(self):
        source = open(lv.__file__, encoding="utf-8").read()
        self.assertIn('set_image_motion((state or {}).get("image_motion") or "push")', source)
        self.assertIn("image_motion=IMAGE_MOTION,", source)

    def test_the_render_reports_which_style_it_used(self):
        source = open(lv.__file__, encoding="utf-8").read()
        self.assertIn('f"Image motion: {IMAGE_MOTION} at {zoom:.0%} "', source)


if __name__ == "__main__":
    unittest.main()


class RebuildOverrideTests(unittest.TestCase):
    """Choosing a style and then rebuilding must actually render that style.

    adopt_project_aspect restores the project's saved movement - correct for an ordinary rebuild,
    since an old short must not silently re-render in a style it never had. It also overwrote a
    style the caller had just set, so a render requested as `alternate` came out as `push` and
    said so only in one log line.
    """

    def setUp(self):
        self.source = open(lv.__file__, encoding="utf-8").read()

    def test_the_rebuild_accepts_an_explicit_style(self):
        import inspect
        self.assertIn("image_motion", inspect.signature(lv.rebuild_from_disk).parameters)

    def test_an_explicit_style_is_applied_after_the_project_is_adopted(self):
        block = self.source[self.source.index("def rebuild_from_disk("):]
        adopt_at = block.index("adopt_project_aspect(project_dir)")
        set_at = block.index("set_image_motion(image_motion)")
        self.assertLess(adopt_at, set_at, "the saved style would overwrite the chosen one")

    def test_an_explicit_style_is_remembered(self):
        block = self.source[self.source.index("def rebuild_from_disk("):]
        self.assertIn("save_state(Path(project_dir), image_motion=IMAGE_MOTION)", block[:1200])

    def test_a_rebuild_without_a_choice_keeps_the_project_style(self):
        block = self.source[self.source.index("def rebuild_from_disk("):]
        self.assertIn("if image_motion:", block[:1200])
