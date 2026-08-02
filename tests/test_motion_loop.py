import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import motion_loop


class MotionLoopModeTest(unittest.TestCase):
    def _run(self, seamless=True):
        temp = tempfile.TemporaryDirectory()
        root = Path(temp.name)
        seen = {"image": 0, "video": 0, "payload": None, "url": None}

        def image_submit(prompt, config, key):
            seen["image"] += 1
            return "image-id", {"ok": True}

        def request_json(method, url, key, payload, timeout=0):
            seen["video"] += 1; seen["payload"] = dict(payload); seen["url"] = url
            return {"data": {"id": "video-id"}}

        def poll(prediction_id, key, **kwargs):
            return (["https://example.test/keyframe.png"] if prediction_id == "image-id"
                    else ["https://example.test/video.mp4"]), {"status": "completed"}

        def download(url, path):
            Path(path).parent.mkdir(parents=True, exist_ok=True); Path(path).write_bytes(b"asset")
            return 5

        def finish(source, output, duration, profile, intensity, status_cb=None):
            Path(output).write_bytes(b"video")

        with mock.patch.object(motion_loop.agent_core, "PROJECTS_DIR", root), \
             mock.patch.object(motion_loop.pipeline, "api_key", return_value="test"), \
             mock.patch.object(motion_loop.pipeline, "submit_wavespeed_image", side_effect=image_submit), \
             mock.patch.object(motion_loop.pipeline, "upload_media", return_value=("https://example.test/motion.mp4", {"ok": True})), \
             mock.patch.object(motion_loop.pipeline, "request_json", side_effect=request_json), \
             mock.patch.object(motion_loop.pipeline, "poll_wavespeed", side_effect=poll), \
             mock.patch.object(motion_loop.pipeline, "download_file", side_effect=download), \
             mock.patch.object(motion_loop.pipeline, "extract_poster_frame", side_effect=lambda src, dst, at=0: Path(dst).write_bytes(b"poster")), \
             mock.patch.object(motion_loop, "MOTION_DNA_TEMPLATE", root / "template.mp4"), \
             mock.patch.object(motion_loop.subprocess, "run", side_effect=lambda *a, **k: (a[0][-1] and Path(a[0][-1]).write_bytes(b"template"))), \
             mock.patch.object(motion_loop, "finish_motion_dna", side_effect=lambda src, out: Path(out).write_bytes(b"video")):
            motion_loop.MOTION_DNA_TEMPLATE.write_bytes(b"template")
            result = motion_loop.run_motion_loop({
                "motion_loop_concept": "Neon forest ride",
                "motion_loop_duration": "5",  # legacy input must be ignored
                "motion_loop_profile": "lateral",
                "motion_loop_intensity": "balanced",
                "motion_loop_seamless": "on" if seamless else "",
            })
        budget = json.loads((Path(result["project_dir"]) / "config" / "generation_budget.json").read_text())
        return temp, seen, budget

    def test_paid_generation_budget_is_exactly_one_each(self):
        temp, seen, budget = self._run(True)
        try:
            self.assertEqual(seen["image"], 1)
            self.assertEqual(seen["video"], 1)
            self.assertEqual(budget["gpt_image_2_submissions"], 1)
            self.assertEqual(budget["seedance_2_submissions"], 1)
            self.assertTrue(seen["url"].endswith("bytedance/seedance-2.0-fast/video-edit"))
            self.assertEqual(seen["payload"]["resolution"], "480p")
            self.assertEqual(seen["payload"]["duration"], 15)
            self.assertIn("one uninterrupted 15-second", seen["payload"]["prompt"])
            self.assertIn("0-4 seconds", seen["payload"]["prompt"])
            self.assertIn("10-15 seconds", seen["payload"]["prompt"])
        finally:
            temp.cleanup()

    def test_motion_edit_uses_generated_keyframe_as_style_reference(self):
        temp, seen, _ = self._run(True)
        try:
            self.assertEqual(seen["payload"]["reference_images"], ["https://example.test/keyframe.png"])
        finally:
            temp.cleanup()

    def test_non_loop_omits_last_frame(self):
        temp, seen, _ = self._run(False)
        try:
            self.assertNotIn("last_image", seen["payload"])
        finally:
            temp.cleanup()


if __name__ == "__main__":
    unittest.main()
