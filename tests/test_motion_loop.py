import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import motion_loop


class MotionLoopModeTest(unittest.TestCase):
    def _run(self, seamless=True, with_first_frame=False):
        temp = tempfile.TemporaryDirectory()
        root = Path(temp.name)
        seen = {"begin": 0, "calls": []}

        def generate(prompt, out_path, first_frame=None, status_cb=None, timeout_s=None):
            seen["calls"].append({"prompt": prompt, "out": str(out_path), "first_frame": first_frame})
            Path(out_path).parent.mkdir(parents=True, exist_ok=True)
            Path(out_path).write_bytes(b"video-" + str(len(seen["calls"])).encode())
            return str(out_path)

        first_frame = root / "opening.png"
        if with_first_frame:
            first_frame.write_bytes(b"png")
        def begin_preflight(**kwargs):
            seen["begin"] += 1
            seen["begin_kwargs"] = kwargs
            return True

        with mock.patch.object(motion_loop.agent_core, "PROJECTS_DIR", root), \
             mock.patch.object(motion_loop.agent_core, "_post_llm_json", return_value={"chapters": [
                 {"prompt": "Gemini-directed first scene with a physically coherent surreal world and natural environment."},
                 {"prompt": "Gemini-directed continuation scene preserving the exact prior-frame perspective and natural environment."},
                 {"prompt": "Gemini-directed final scene with coherent forward motion and natural environmental sound."},
             ]}) as prompt_call, \
             mock.patch.object(motion_loop.higgsfield_login, "begin_seedance_25_video_session", side_effect=begin_preflight), \
             mock.patch.object(motion_loop.higgsfield_login, "generate_seedance_25_video_sync", side_effect=generate), \
             mock.patch.object(motion_loop, "_last_frame", side_effect=lambda src, out: Path(out).write_bytes(b"frame" * 400)), \
             mock.patch.object(motion_loop, "_assemble", side_effect=lambda clips, out: Path(out).write_bytes(b"video")), \
             mock.patch.object(motion_loop.pipeline, "extract_poster_frame", side_effect=lambda src, dst, at=0: Path(dst).write_bytes(b"poster")):
            result = motion_loop.run_motion_loop({
                "motion_loop_concept": "Neon forest ride",
                "motion_loop_duration": "5",  # legacy value must be ignored
                "motion_loop_profile": "lateral",
                "motion_loop_pov": "motorcycle",
                "motion_loop_speed": "4",
                "motion_loop_intensity": "balanced",
                "motion_loop_seamless": "on" if seamless else "",
                "motion_loop_unlimited": "on",
                "motion_loop_first_frame": str(first_frame) if with_first_frame else "",
            })
        manifest = json.loads((Path(result["project_dir"]) / "config" / "generation_manifest.json").read_text())
        project = json.loads((Path(result["project_dir"]) / "config" / "project.json").read_text())
        seen["prompt_call"] = prompt_call
        return temp, seen, manifest, project

    def test_uses_three_frame_linked_seedance_25_generations_at_10_seconds_each(self):
        temp, seen, manifest, project = self._run(True)
        try:
            self.assertEqual(seen["begin"], 1)
            self.assertIsNone(seen["begin_kwargs"]["timeout_s"],
                              "human verification must wait for cancel, never fail on a timer")
            self.assertEqual(seen["prompt_call"].call_args.args[0], "google/gemini-3.1-flash-lite")
            self.assertEqual(len(seen["calls"]), 3)
            self.assertIsNone(seen["calls"][0]["first_frame"])
            self.assertIn("native 10-second", seen["calls"][0]["prompt"])
            self.assertIn("prompt alone", seen["calls"][0]["prompt"])
            self.assertIn("synchronized native ASMR environmental soundtrack", seen["calls"][0]["prompt"])
            self.assertIn("Move quickly with controlled, stable forward energy", seen["calls"][0]["prompt"])
            self.assertIn("helmet-mounted motorcycle POV", seen["calls"][0]["prompt"])
            self.assertTrue(str(seen["calls"][1]["first_frame"]).endswith("chapter_01_last_frame.png"))
            self.assertTrue(str(seen["calls"][2]["first_frame"]).endswith("chapter_02_last_frame.png"))
            self.assertEqual(project["duration"], 30)
            self.assertEqual(project["motion_loop"]["model"], "Seedance 2.5")
            self.assertEqual(project["motion_loop"]["chapters"], 3)
            self.assertEqual(project["motion_loop"]["speed"], 4)
            self.assertEqual(project["motion_loop"]["pov"], "motorcycle")
            self.assertEqual(manifest["chapters"][0]["first_frame"], None)
        finally:
            temp.cleanup()

    def test_explicit_escooter_tag_overrides_legacy_mountain_bike_default(self):
        self.assertEqual(
            motion_loop._infer_pov("mountain_bike", "E-scooter POV through an impossible moonlit city"),
            "e_scooter",
        )

    def test_reference_places_add_visible_landmarks_to_the_fallback_prompt(self):
        prompt = motion_loop._chapter_prompt("Ride through Elwynn Forest from World of Warcraft", "lateral", "e_scooter", 3, 1, False, False)
        self.assertIn("red-roofed human village buildings", prompt)
        self.assertIn("stone bridge over a clear stream", prompt)

    def test_existing_complete_chapter_is_reused_on_resume(self):
        temp = tempfile.TemporaryDirectory()
        root = Path(temp.name)
        slug = "resume_motion"
        generated = root / slug / "generated"; config = root / slug / "config"
        generated.mkdir(parents=True); config.mkdir(parents=True)
        clip = generated / "ai_motion_chapter_01.mp4"; frame = generated / "ai_motion_chapter_01_last_frame.png"
        clip.write_bytes(b"v" * 2000); frame.write_bytes(b"f" * 200)
        (config / "generation_manifest.json").write_text(json.dumps({"chapters": [{"index": 1, "clip": str(clip), "last_frame": str(frame), "prompt": "existing"}]}), encoding="utf-8")
        calls=[]
        def generate(prompt, out_path, first_frame=None, **_):
            calls.append(first_frame); Path(out_path).write_bytes((b"v" * 1990) + str(len(calls)).encode()); return str(out_path)
        try:
            with mock.patch.object(motion_loop.agent_core, "PROJECTS_DIR", root), \
                 mock.patch.object(motion_loop.agent_core, "_post_llm_json", return_value={"chapters": [{"prompt":"x"*100} for _ in range(3)]}), \
                 mock.patch.object(motion_loop.higgsfield_login, "begin_seedance_25_video_session", return_value=True), \
                 mock.patch.object(motion_loop.higgsfield_login, "generate_seedance_25_video_sync", side_effect=generate), \
                 mock.patch.object(motion_loop, "_last_frame", side_effect=lambda src, out: Path(out).write_bytes(b"f"*200)), \
                 mock.patch.object(motion_loop, "_assemble", side_effect=lambda clips, out: Path(out).write_bytes(b"v"*2000)), \
                 mock.patch.object(motion_loop.pipeline, "extract_poster_frame", side_effect=lambda src, dst, at=0: Path(dst).write_bytes(b"p"*200)):
                motion_loop.run_motion_loop({"motion_loop_concept":"resume", "motion_loop_mode":"on", "motion_loop_unlimited":"on", "motion_loop_resume_project":slug})
            self.assertEqual(len(calls), 2)
            self.assertEqual(Path(calls[0]), frame)
        finally:
            temp.cleanup()

    def test_requires_unlimited(self):
        with self.assertRaisesRegex(RuntimeError, "Unlimited"):
            motion_loop.run_motion_loop({"motion_loop_concept": "test"})

    def test_manual_unlimited_workflow_imports_three_user_generated_chapters(self):
        temp = tempfile.TemporaryDirectory()
        root = Path(temp.name)
        sources = []
        for index in range(1, 4):
            source = root / f"manual_{index}.mp4"
            source.write_bytes((f"manual-video-{index}".encode()) * 200)
            sources.append(source)
        calls = []
        def gate(index, prompt, first_frame):
            calls.append((index, prompt, first_frame))
            return str(sources[index - 1])
        try:
            with mock.patch.object(motion_loop.agent_core, "PROJECTS_DIR", root), \
                 mock.patch.object(motion_loop.agent_core, "_post_llm_json", return_value={"chapters": [{"prompt": "x" * 100} for _ in range(3)]}), \
                 mock.patch.object(motion_loop.higgsfield_login, "open_manual_seedance_browser", return_value=True) as open_browser, \
                 mock.patch.object(motion_loop.higgsfield_login, "generate_seedance_25_video_sync") as generate, \
                 mock.patch.object(motion_loop, "_last_frame", side_effect=lambda src, out: Path(out).write_bytes(b"f" * 200)), \
                 mock.patch.object(motion_loop, "_assemble", side_effect=lambda clips, out: Path(out).write_bytes(b"v" * 2000)), \
                 mock.patch.object(motion_loop.pipeline, "extract_poster_frame", side_effect=lambda src, dst, at=0: Path(dst).write_bytes(b"p" * 200)):
                result = motion_loop.run_motion_loop({
                    "motion_loop_concept": "manual", "motion_loop_unlimited": "on",
                    "_motion_manual_chapter_gate": gate,
                })
            self.assertTrue(Path(result["video"]).is_file())
            open_browser.assert_called_once()
            generate.assert_not_called()
            self.assertEqual([row[0] for row in calls], [1, 2, 3])
            self.assertIsNone(calls[0][2])
            self.assertTrue(str(calls[1][2]).endswith("chapter_01_last_frame.png"))
        finally:
            temp.cleanup()

    def test_uploaded_image_is_passed_as_the_first_frame(self):
        temp, seen, manifest, project = self._run(with_first_frame=True)
        try:
            self.assertTrue(str(seen["calls"][0]["first_frame"]).endswith("opening.png"))
            self.assertTrue(manifest["chapters"][0]["first_frame"].endswith("opening.png"))
            self.assertTrue(str(seen["calls"][1]["first_frame"]).endswith("chapter_01_last_frame.png"))
        finally:
            temp.cleanup()


if __name__ == "__main__":
    unittest.main()
