import math
import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock

import agent_core
import pipeline


def _tone_wav(path, duration=0.12, rate=44100):
    path = Path(path).with_suffix(".wav")
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = int(duration * rate)
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        samples = bytearray()
        for idx in range(frames):
            value = int(8000 * math.sin(2 * math.pi * 440 * idx / rate))
            samples.extend(int(value).to_bytes(2, "little", signed=True))
        out.writeframes(bytes(samples))
    return path


class HookBodyPauseTests(unittest.TestCase):
    def test_pause_is_mandatory_when_split_flag_is_off(self):
        ffmpeg = pipeline.find_ffmpeg()
        if not ffmpeg:
            self.skipTest("ffmpeg unavailable")
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            calls = []

            def fake_tts(text, out_path, **_kwargs):
                calls.append(text)
                return _tone_wav(out_path)

            form = {
                "hook_text": "This is the hook.",
                "split_hook_tts": False,
                "enable_speaker_hook": False,
                "voice_speed": 1.0,
                "clip_source": "generate",
            }
            script = "This is the hook. This is the body."
            with mock.patch.object(pipeline, "generate_speech_gemini", side_effect=fake_tts):
                result = agent_core.generate_project_voiceover(script, project, form)

            self.assertEqual(calls, ["This is the hook.", "This is the body."])
            self.assertTrue(Path(result).exists())
            self.assertEqual(form["hook_pause_s"], 0.5)
            self.assertTrue(Path(form["_hook_audio_path"]).exists())
            self.assertTrue(Path(form["_body_audio_path"]).exists())
            duration = agent_core.probe_audio_duration(result)
            self.assertAlmostEqual(duration, 0.12 + 0.5 + 0.12, delta=0.035)
            self.assertTrue(agent_core.hook_edit_cache_valid(project, script, form["hook_text"], result))

    def test_old_voiceover_without_marker_is_not_pause_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            voiceover = _tone_wav(project / "input" / "voiceover")
            self.assertFalse(agent_core.hook_edit_cache_valid(
                project, "Hook words. Body words.", "Hook words.", voiceover))


if __name__ == "__main__":
    unittest.main()
