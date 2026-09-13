"""The sketch explainer in two formats: the long 16:9 video and the ~1 minute 9:16 short.

Same pipeline, same drawing style, same cut rhythm - only the canvas and the script length
differ. The format was hardcoded in seventeen places (every resume check, the generator request,
four prompt texts and the final assembly), so these pin the parts that have to move together.
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from PIL import Image

import longform_video as lv


class ProjectFormatTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(lv.set_project_aspect, "16:9")
        self.tmp = Path(tempfile.mkdtemp(prefix="lf_fmt_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_the_canvas_follows_the_format(self):
        self.assertEqual(lv.set_project_aspect("16:9"), (1920, 1080))
        self.assertEqual(lv.video_size(), (1920, 1080))
        self.assertEqual(lv.set_project_aspect("9:16"), (1080, 1920))
        self.assertEqual(lv.video_size(), (1080, 1920))

    def test_an_unknown_format_stays_landscape(self):
        """A malformed field must never quietly render a long script as a vertical video."""
        for value in ("", None, "4:3", "portrait", "nonsense"):
            self.assertEqual(lv.set_project_aspect(value), (1920, 1080))

    def test_the_prompt_text_is_translated_for_a_short(self):
        lv.set_project_aspect("9:16")
        sent = lv.fit_aspect(lv.STAGE3_PROMPT)
        self.assertNotIn("16:9", sent)
        self.assertIn("9:16 vertical aspect ratio", sent)
        self.assertIn("9:16 vertical aspect ratio", lv.fit_aspect(lv.NO_TEXT_LOCK))

    def test_the_long_format_prompt_is_left_exactly_alone(self):
        lv.set_project_aspect("16:9")
        self.assertEqual(lv.fit_aspect(lv.STAGE3_PROMPT), lv.STAGE3_PROMPT)

    def test_the_drawing_rules_are_not_duplicated_per_format(self):
        """One STAGE-3 prompt, translated where the ratio is spoken. Two copies would drift."""
        lv.set_project_aspect("9:16")
        sent = lv.fit_aspect(lv.STAGE3_PROMPT)
        for rule in ("Simple MS Paint style illustration", "PUT THE SCENE SOMEWHERE",
                     "DIAGRAMS AND PANELS ARE PART OF THE RHYTHM"):
            self.assertIn(rule, sent, "a shorts run lost one of the drawing rules")

    def test_an_existing_project_adopts_its_own_format(self):
        """Otherwise the editor, the retime and the rebuild would test a vertical project's art
        against 16:9, call every frame wrong, and offer to regenerate work that is fine."""
        (self.tmp / lv.STATE_FILE).write_text(json.dumps({"aspect": "9:16", "script": "x"}),
                                              encoding="utf-8")
        lv.set_project_aspect("16:9")
        self.assertEqual(lv.adopt_project_aspect(self.tmp), (1080, 1920))
        self.assertEqual(lv.IMAGE_ASPECT, "9:16")

    def test_a_project_from_before_the_shorts_format_stays_landscape(self):
        (self.tmp / lv.STATE_FILE).write_text(json.dumps({"script": "x"}), encoding="utf-8")
        lv.set_project_aspect("9:16")
        self.assertEqual(lv.adopt_project_aspect(self.tmp), (1920, 1080))

    def test_an_unreadable_state_does_not_break_the_editor(self):
        (self.tmp / lv.STATE_FILE).write_text("{ not json", encoding="utf-8")
        self.assertEqual(lv.adopt_project_aspect(self.tmp), (1920, 1080))

    def test_nothing_still_hardcodes_the_ratio_in_a_resume_check(self):
        source = Path(lv.__file__).read_text(encoding="utf-8")
        self.assertNotIn('_image_done(path, "16:9")', source)
        self.assertNotIn('_image_done(p, "16:9")', source)
        self.assertNotIn('"aspect_ratio": "16:9"', source)



class ImageZoomTests(unittest.TestCase):
    """A still held for four seconds reads as frozen. The zoom is a deliberate 5% push-in across
    each image's hold - off by default, so every existing project renders exactly as before."""

    def setUp(self):
        self.addCleanup(lv.set_image_zoom, 0.0)
        self.tmp = Path(tempfile.mkdtemp(prefix="lf_zoom_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_it_is_off_unless_asked_for(self):
        self.assertEqual(lv.set_image_zoom(0.0), 0.0)
        self.assertEqual(lv.set_image_zoom(None), 0.0)
        self.assertEqual(lv.set_image_zoom("nonsense"), 0.0)

    def test_the_subtle_amount_is_small_enough_to_read_as_breathing(self):
        self.assertGreater(lv.IMAGE_ZOOM_SUBTLE, 0.0)
        self.assertLessEqual(lv.IMAGE_ZOOM_SUBTLE, 0.08)

    def test_an_absurd_amount_is_clamped(self):
        self.assertLessEqual(lv.set_image_zoom(9.0), 0.30)
        self.assertEqual(lv.set_image_zoom(-1.0), 0.0)

    def test_a_project_carries_its_own_zoom(self):
        (self.tmp / lv.STATE_FILE).write_text(
            json.dumps({"aspect": "9:16", "image_zoom": 0.05, "script": "x"}), encoding="utf-8")
        lv.set_image_zoom(0.0)
        lv.adopt_project_aspect(self.tmp)
        self.assertAlmostEqual(lv.IMAGE_ZOOM, 0.05)

    def test_a_project_from_before_the_zoom_renders_flat(self):
        (self.tmp / lv.STATE_FILE).write_text(json.dumps({"script": "x"}), encoding="utf-8")
        lv.set_image_zoom(0.05)
        lv.adopt_project_aspect(self.tmp)
        self.assertEqual(lv.IMAGE_ZOOM, 0.0)

    def test_a_long_explainer_falls_back_to_a_flat_render(self):
        """One decode chain per still in a single filtergraph stops being reasonable at some
        point; a 541-scene project must render flat rather than fail."""
        self.assertLess(lv.IMAGE_ZOOM_MAX_SCENES, 541)
        self.assertGreaterEqual(lv.IMAGE_ZOOM_MAX_SCENES, 60)


class ReturnedImageAspectTests(unittest.TestCase):
    """A provider-side ratio mistake must never turn real art into 'missing images'."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="lf_aspect_repair_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_landscape_provider_frame_is_repaired_for_a_vertical_short(self):
        path = self.tmp / "provider_landscape.png"
        Image.new("RGB", (1280, 720), "#abc123").save(path)
        self.assertFalse(lv._image_aspect_ok(path, "9:16"))
        self.assertTrue(lv._normalize_image_aspect(path, "9:16"))
        self.assertTrue(lv._image_aspect_ok(path, "9:16"))

class ShortsVoiceDefaultsTests(unittest.TestCase):
    """The shorts format fills in its own delivery defaults. They live in the shell, so they are
    checked there - a wrong speed is silent: the voice simply reads at the long-form pace."""

    def _shell(self):
        return (Path(lv.__file__).resolve().parent / "static" / "chat-shell.js"
                ).read_text(encoding="utf-8")

    def test_the_shorts_read_speed_matches_the_configured_default(self):
        """The number changed to 1.14 when the Sketch Explainer got explicit defaults. What must
        hold is that ONE number is used: two different values for "how fast a short is read" is
        how the 9:16 auto-bump silently undid the configured default."""
        import re
        shell = self._shell()
        bump = re.search(r'const SHORTS_VOICE_SPEED = "([^"]+)"', shell)
        default = re.search(r'tts_native_speed:\s*"([^"]*)"', shell)
        self.assertIsNotNone(bump)
        self.assertIsNotNone(default)
        self.assertEqual(bump.group(1), default.group(1))
        self.assertGreater(float(bump.group(1)), 1.0)

    def test_switching_back_to_the_long_format_restores_normal_speed(self):
        shell = self._shell()
        self.assertIn('S.longform.tts_native_speed = "1";', shell)

    def test_a_speed_typed_by_hand_is_never_overwritten(self):
        shell = self._shell()
        self.assertIn('String(S.longform.tts_native_speed || "1") === "1"', shell)

    def test_the_short_has_a_dedicated_hook_delivery_profile(self):
        source = Path(lv.__file__).read_text(encoding="utf-8")
        pipeline_source = (Path(lv.__file__).resolve().parent / "pipeline.py").read_text(
            encoding="utf-8")
        self.assertIn('short_form=(IMAGE_ASPECT == "9:16")', source)
        self.assertIn('delivery_profile = "sketch_short_hook_v2"', source)
        self.assertIn("TTS_STYLE_SKETCH_SHORT_HOOK", pipeline_source)

    def test_short_hook_is_a_separate_tts_part(self):
        parts = lv.split_sketch_short_for_tts(
            "You lose consciousness every night.\nYou remember getting into bed.\nThen it is morning.")
        self.assertEqual(parts[0], "You lose consciousness every night.")
        self.assertEqual(parts[1], "You remember getting into bed. Then it is morning.")

class ShortCuttingTests(unittest.TestCase):
    """A 37s short shipped with ten images - the twenty-minute pace - and its punchlines shared a
    picture with the setup: "The instant itself, where being awake ended. You can't." was one beat.
    A short cuts about twice as often and lets a finished sentence end the beat."""

    SCRIPT = ("Try to remember the exact moment you fell asleep last night. Not lying in bed. "
              "Not putting your phone down. You can't. Nobody can. And then there is a cut.")

    def test_a_short_cuts_far_more_often_than_the_long_form(self):
        long_form = lv.split_script_lines(self.SCRIPT)
        short_form = lv.split_script_lines(self.SCRIPT, short_form=True)
        self.assertGreater(len(short_form), len(long_form) * 1.4,
                           f"{len(short_form)} vs {len(long_form)} beats")

    def test_a_finished_short_sentence_gets_its_own_picture(self):
        beats = lv.split_script_lines(self.SCRIPT, short_form=True)
        self.assertIn("Not lying in bed.", beats)
        self.assertIn("Not putting your phone down.", beats)

    def test_the_long_form_rhythm_is_untouched(self):
        beats = lv.split_script_lines(self.SCRIPT)
        self.assertTrue(all(len(b.split()) >= 6 for b in beats), beats)

    def test_a_short_gets_its_own_drawing_rules(self):
        self.addCleanup(lv.set_project_aspect, "16:9")
        lv.set_project_aspect("16:9")
        self.assertEqual(lv.fit_aspect(lv.STAGE3_PROMPT), lv.STAGE3_PROMPT)
        lv.set_project_aspect("9:16")
        sent = lv.fit_aspect(lv.STAGE3_PROMPT)
        self.assertIn("ONE-MINUTE VERTICAL SHORT", sent)
        self.assertIn("Simple MS Paint style illustration", sent,
                      "the short lost the series drawing style")


if __name__ == "__main__":
    unittest.main()
