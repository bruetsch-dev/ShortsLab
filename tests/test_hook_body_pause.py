import math
import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock

import agent_core
import pipeline


def _speech_like_wav(path, rate=44100):
    """Tone - silence - tone, so the single-take hook split has a gap to cut at.

    The cost fix (one TTS call, then a LOCAL split at the pause) made a continuous tone an
    impossible input: there is no silence to place the hook edit on, so the take can only
    raise. Real narration always has the gap this fixture now contains."""
    # long enough for the real rule: the silence has to start at least 0.5s in and end at
    # least 0.5s before the take does, or there is nowhere legal to put the edit.
    return _tone_wav(path, duration=1.5, rate=rate, silence=0.8, repeats=2)


def _tone_wav(path, duration=0.12, rate=44100, silence=0.0, repeats=1):
    path = Path(path).with_suffix(".wav")
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = int(duration * rate)
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        samples = bytearray()
        for block in range(max(1, repeats)):
            if block:
                samples.extend(bytes(2 * int(silence * rate)))
            for idx in range(frames):
                value = int(8000 * math.sin(2 * math.pi * 440 * idx / rate))
                samples.extend(int(value).to_bytes(2, "little", signed=True))
        out.writeframes(bytes(samples))
    return path


class HookBodyPauseTests(unittest.TestCase):
    def test_smart_dash_in_marked_hook_still_splits(self):
        script = "This machine looks abandoned—but it still cooks lunch. Then the door opens."
        hook = "This machine looks abandoned—but it still cooks lunch."
        split_hook, body = agent_core.split_hook_from_script(script, hook)
        self.assertEqual(split_hook, "This machine looks abandoned-but it still cooks lunch.")
        self.assertEqual(body, "Then the door opens.")

    def test_pause_is_mandatory_when_split_flag_is_off(self):
        ffmpeg = pipeline.find_ffmpeg()
        if not ffmpeg:
            self.skipTest("ffmpeg unavailable")
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            calls = []

            def fake_tts(text, out_path, **_kwargs):
                calls.append(text)
                return _speech_like_wav(out_path)

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

            # ONE TTS call, then a LOCAL split at the pause (cost fix) - it used to bill two
            # separate calls for the hook and the body. What must not change is the outcome:
            # the hook/body pause is mandatory whether or not the split flag is set.
            self.assertEqual(calls, ["This is the hook. This is the body."])
            self.assertTrue(Path(result).exists())
            self.assertEqual(form["hook_pause_s"], 0.5)
            self.assertTrue(Path(form["_hook_audio_path"]).exists())
            self.assertTrue(Path(form["_body_audio_path"]).exists())
            # The old expectation (0.12 + 0.5 + 0.12) described two separate TTS takes joined
            # by a pause. There is one take now, so what has to be true is that the mandatory
            # pause was inserted INTO it: the result is the take plus roughly hook_pause_s.
            take = 1.5 + 0.8 + 1.5
            duration = agent_core.probe_audio_duration(result)
            self.assertGreaterEqual(duration, take + form["hook_pause_s"] - 0.1)
            self.assertLessEqual(duration, take + form["hook_pause_s"] + 0.2)
            self.assertTrue(agent_core.hook_edit_cache_valid(project, script, form["hook_text"], result))

    def test_old_voiceover_without_marker_is_not_pause_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            voiceover = _tone_wav(project / "input" / "voiceover")
            self.assertFalse(agent_core.hook_edit_cache_valid(
                project, "Hook words. Body words.", "Hook words.", voiceover))

    def test_a_failed_voiceover_stops_the_run(self):
        """When WaveSpeed withdrew the Gemini TTS models, every request came back 400 and the run
        carried on to plan beats, scrape footage and cut a timeline against ESTIMATED word timing.
        Forty minutes and paid vision calls for a video whose captions line up with nothing."""
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / "input").mkdir()
            with mock.patch.object(pipeline, "generate_speech_gemini",
                                   side_effect=RuntimeError("400 Model not found")):
                with self.assertRaises(RuntimeError) as caught:
                    agent_core.generate_project_voiceover(
                        "Some narration.", project,
                        {"speaker_name": "Narrator", "tts_voice": "Charon"})
            self.assertIn("no audio to cut against", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
