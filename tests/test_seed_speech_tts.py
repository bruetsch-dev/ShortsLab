import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pipeline


class SeedSpeechTtsTests(unittest.TestCase):
    def test_seed_uses_provider_specific_payload(self):
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(pipeline, "request_json", return_value={"data": {"id": "p1"}}) as post, \
                mock.patch.object(pipeline, "poll_wavespeed", return_value=(["https://x.test/out.mp3"], {})), \
                mock.patch.object(pipeline, "download_file", side_effect=lambda _url, path: Path(path).write_bytes(b"audio")):
            out = pipeline.generate_speech_gemini(
                "Hello world", Path(td) / "voice.wav", key="test",
                model=pipeline.SEED_SPEECH_TTS_MODEL, voice="stokie_en",
                voice_instruction="warm and calm", language="en", tts_speed=1.2,
                volume=0.8, pitch=-2, sample_rate=48000, output_format="mp3")

        payload = post.call_args.args[3]
        self.assertEqual(payload["text"], "Hello world")
        self.assertEqual(payload["voice"], "stokie_en")
        self.assertEqual(payload["voice_instruction"], "warm and calm")
        self.assertEqual(payload["language"], "en")
        self.assertEqual(payload["speed"], 1.2)
        self.assertEqual(payload["volume"], 0.8)
        self.assertEqual(payload["pitch"], -2)
        self.assertEqual(payload["sample_rate"], 48000)
        self.assertNotIn("speakers", payload)
        self.assertFalse(payload["text"].startswith("Narrator:"))
        self.assertEqual(Path(out).suffix, ".mp3")

    def test_invalid_seed_values_are_safely_normalized(self):
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(pipeline, "request_json", return_value={"data": {"id": "p1"}}) as post, \
                mock.patch.object(pipeline, "poll_wavespeed", return_value=(["https://x.test/out.mp3"], {})), \
                mock.patch.object(pipeline, "download_file", side_effect=lambda _url, path: Path(path).write_bytes(b"audio")):
            pipeline.generate_speech_gemini(
                "Test", Path(td) / "voice", key="test",
                model=pipeline.SEED_SPEECH_TTS_MODEL, voice="Achernar",
                language="English (United States)", tts_speed=9, volume=-4,
                pitch=40, sample_rate=12345, output_format="wav")

        payload = post.call_args.args[3]
        self.assertEqual(payload["voice"], "stokie_en")
        self.assertEqual(payload["language"], "en")
        self.assertEqual(payload["speed"], 2.0)
        self.assertEqual(payload["volume"], 0.5)
        self.assertEqual(payload["pitch"], 12)
        self.assertEqual(payload["sample_rate"], 24000)
        self.assertEqual(payload["output_format"], "mp3")
        self.assertEqual(payload["voice_instruction"], pipeline.SEED_SHORT_STYLE_INSTRUCTION)


if __name__ == "__main__":
    unittest.main()
