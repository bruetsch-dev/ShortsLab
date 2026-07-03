"""Sanity checks for the Viral Transformation mode (hermetic - no API key needed)."""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.pop("WAVESPEED_API_KEY", None)   # force the deterministic fallbacks

import viral_transformation
from viral_transformation import agents


class ViralTransformationTests(unittest.TestCase):
    def test_strategist_fallback_has_required_fields(self):
        s = agents.topic_strategist("street dog salon")
        for key in ("narrowed_topic", "concept_title", "subject", "metric_number",
                    "before_state", "after_state", "safe"):
            self.assertIn(key, s)
        self.assertTrue(s["safe"])
        self.assertTrue(s["concept_title"].startswith("I "))
        self.assertIn("For This", s["concept_title"])

    def test_unsafe_topic_flagged(self):
        s = agents.topic_strategist("bloody knife fight gore")
        self.assertFalse(s["safe"])

    def test_scene_plan_structure(self):
        import math
        s = agents.topic_strategist("rusty knife restoration")
        plan = agents.plan_scenes(s)
        scenes = plan["scenes"]
        self.assertGreaterEqual(len(scenes), 8)
        self.assertLessEqual(len(scenes), 10)
        phases = [sc["phase"] for sc in scenes]
        # all six phases present, in the fixed order
        self.assertEqual(set(phases), set(agents.PHASES))
        order = {p: i for i, p in enumerate(agents.PHASES)}
        self.assertEqual(phases, sorted(phases, key=lambda p: order[p]))
        # PROCESS has the most scenes
        counts = {p: phases.count(p) for p in agents.PHASES}
        self.assertEqual(max(counts, key=counts.get), "PROCESS")
        # durations + total in range; captions 1-5 words; prompts exist
        total = 0.0
        for sc in scenes:
            self.assertGreaterEqual(sc["duration_seconds"], 1.5)
            self.assertLessEqual(sc["duration_seconds"], 4.0)
            self.assertTrue(1 <= len(sc["caption"].split()) <= 5)
            self.assertTrue(sc["image_prompt"] and sc["video_motion_prompt"])
            self.assertIn("negative_prompt", sc)
            total += sc["duration_seconds"]
        self.assertGreaterEqual(total, 28.0)
        self.assertLessEqual(total, 40.0)
        # the GENERATED Seedance footage (the API generates min 4s per scene) stays ~40s
        generated = sum(max(4, int(math.ceil(sc["duration_seconds"]))) for sc in scenes)
        self.assertLessEqual(generated, 40)

    def test_metadata_fallback_uses_formula(self):
        s = agents.topic_strategist("filthy sneaker cleaning")
        md = agents.make_metadata(s)
        for key in ("youtube_title", "tiktok_title", "description", "hashtags", "internal_name"):
            self.assertIn(key, md)
        self.assertIn("For This", md["youtube_title"])
        self.assertNotIn("higgsfield", str(md).lower())

    def test_ken_burns_fallback_clip(self):
        import pipeline
        from viral_transformation import media as vt_media
        ffmpeg = pipeline.find_ffmpeg()
        ffprobe = pipeline.find_ffprobe(ffmpeg)
        if not ffmpeg:
            self.skipTest("no ffmpeg")
        from PIL import Image
        td = Path(tempfile.mkdtemp(prefix="vt_test_"))
        img = td / "still.png"
        Image.new("RGB", (1080, 1920), (40, 60, 80)).save(img)
        clip = vt_media.ken_burns_clip(img, td / "kb.mp4", 2.0, ffmpeg)
        self.assertIsNotNone(clip)
        ok, why = vt_media.validate_clip(clip, ffprobe)
        self.assertTrue(ok, why)

    def test_builder_assembles_final_short(self):
        import pipeline
        from viral_transformation import builder, media as vt_media
        ffmpeg = pipeline.find_ffmpeg()
        if not ffmpeg:
            self.skipTest("no ffmpeg")
        from PIL import Image
        td = Path(tempfile.mkdtemp(prefix="vt_build_"))
        s = agents.topic_strategist("old coin cleaning")
        plan = agents.plan_scenes(s)
        plan["scenes"] = plan["scenes"][:5]                 # keep the test fast
        media = {}
        for i, sc in enumerate(plan["scenes"]):
            img = td / f"img_{i}.png"
            Image.new("RGB", (1080, 1920), (30 + i * 20, 50, 90)).save(img)
            clip = vt_media.ken_burns_clip(img, td / f"clip_{i}.mp4",
                                           sc["duration_seconds"] + 0.5, ffmpeg)
            media[sc["scene_id"]] = {"image": str(img), "clip": str(clip),
                                     "source": "kenburns", "status": "accepted"}
        out = builder.build_short(plan, media, td, td / "final.mp4")
        self.assertTrue(Path(out["video"]).exists())
        self.assertGreaterEqual(out["scenes_used"], 4)

    def test_app_wiring_and_old_modes_intact(self):
        import app
        home = app.page("t", app.brand_header())
        # new mode present on the choosing screen
        menu = app.form_page() if hasattr(app, "form_page") else None
        # the mode cards live in the home form; render it via the index body builder
        self.assertTrue(hasattr(app, "viraltrans_page"))
        html = app.viraltrans_page()
        self.assertIn(b"/viraltrans-generate", html)
        self.assertIn(b"street dog salon", html)
        # old modes unchanged
        self.assertIn(b"/sfx-run", app.sfx_page())
        self.assertIn(b"/captions-run", app.caption_page())
        self.assertIn(b"/visual-run", app.visual_page())


if __name__ == "__main__":
    unittest.main(verbosity=1)
