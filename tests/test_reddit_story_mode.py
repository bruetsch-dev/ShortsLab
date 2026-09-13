"""Smoke tests for Reddit Story Mode (no network / no ffmpeg execution).

Covers the parts that must be correct without hitting WaveSpeed or running ffmpeg:
content moderation, story normalization/duration, the empty-parkour-pool error, and the
ffmpeg command shape the video builder produces.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# allow running directly (`python tests/test_reddit_story_mode.py`) by putting the repo root on path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from reddit_story_mode import story_moderator, story_generator, parkour_picker, video_builder


class StoryModeratorTests(unittest.TestCase):
    def test_clean_story_has_no_flags(self):
        ok, story = story_moderator.moderate_story(
            {"title": "The quiet neighbor", "script": "A calm, wholesome story about a lost dog."})
        self.assertTrue(ok)
        self.assertEqual(story["risk_flags"], [])

    def test_unsafe_story_is_flagged(self):
        ok, story = story_moderator.moderate_story(
            {"title": "x", "script": "here is how to kill myself and shoot up the school"})
        self.assertFalse(ok)
        self.assertIn("self_harm", story["risk_flags"])
        self.assertIn("school_threat", story["risk_flags"])


class StoryGeneratorTests(unittest.TestCase):
    def test_estimate_seconds_from_words(self):
        script = " ".join(["word"] * 330)     # ~330 words -> ~2 min at 165 wpm
        self.assertGreaterEqual(story_generator._estimate_seconds(script), 100)
        self.assertLessEqual(story_generator._estimate_seconds(script), 160)

    def test_estimate_seconds_respects_valid_given(self):
        self.assertEqual(story_generator._estimate_seconds("a b c", given=150), 150)

    def test_normalize_fills_schema(self):
        s = story_generator._normalize({"title": "T", "hook": "H", "summary": "S",
                                        "script": "word " * 200})
        for key in ("id", "title", "hook", "summary", "script", "estimated_duration_sec",
                    "source_type", "risk_flags"):
            self.assertIn(key, s)
        self.assertTrue(s["id"].startswith("story_"))
        self.assertEqual(s["source_type"], "generated_original")


class ParkourPickerTests(unittest.TestCase):
    def test_empty_pool_raises_readable_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(parkour_picker, "PARKOUR_DIR", Path(tmp)):
                with self.assertRaises(RuntimeError) as ctx:
                    parkour_picker.pick_clip()
                self.assertIn("assets/parkour_pool", str(ctx.exception))

    def test_picks_present_clip(self):
        with tempfile.TemporaryDirectory() as tmp:
            clip = Path(tmp) / "parkour1.mp4"
            clip.write_bytes(b"0" * 5000)      # non-empty file
            with mock.patch.object(parkour_picker, "PARKOUR_DIR", Path(tmp)):
                self.assertEqual(parkour_picker.pick_clip(seed=1), clip)


class VideoBuilderTests(unittest.TestCase):
    def test_ffmpeg_command_shape(self):
        # Build the command without running ffmpeg: assert loop, single-audio mapping,
        # 1080x1920 crop, voiceover duration and -shortest are all present.
        captured = {}

        class _Proc:
            returncode = 0
            stderr = ""

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            return _Proc()

        with tempfile.TemporaryDirectory() as tmp:
            park = Path(tmp) / "p.mp4"; park.write_bytes(b"0" * 5000)
            voice = Path(tmp) / "v.mp3"; voice.write_bytes(b"0" * 5000)
            out = Path(tmp) / "out.mp4"
            with mock.patch.object(video_builder.pipeline, "find_ffmpeg", return_value="ffmpeg"), \
                 mock.patch.object(video_builder, "_probe_duration", return_value=12.0), \
                 mock.patch("reddit_story_mode.video_builder.subprocess.run", side_effect=fake_run):
                # out won't actually be created by the fake; assert on the command instead
                try:
                    video_builder.build_video(park, voice, out)
                except RuntimeError:
                    pass  # expected: fake run doesn't write the file
            cmd = captured.get("cmd", [])
            joined = " ".join(cmd)
            self.assertIn("-stream_loop", cmd)
            self.assertIn("0:v:0", joined)      # parkour video
            self.assertIn("1:a:0", joined)      # voice audio only
            self.assertIn("crop=1080:1920", joined)
            self.assertIn("-shortest", cmd)
            self.assertIn("12.000", joined)     # -t voiceover duration


if __name__ == "__main__":
    unittest.main()
