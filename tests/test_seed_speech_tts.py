import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pipeline


class SeedSpeechTtsTests(unittest.TestCase):
    def test_the_pro_alias_speaks_gemini_again(self):
        """It briefly spoke Seed, and that is why every narrator sounded the same.

        Gemini TTS answered 400 "Model not found" on 2026-08-20, so every alias was pointed at
        ByteDance. That stopped the outage and silently destroyed narrator selection: the thirty
        Gemini voice names are not Seed voices, so each one failed the Seed check and fell back
        to stokie_en. Re-measured 2026-08-27 - the models are back and complete normally once the
        narrator is sent as a TOP-LEVEL `voice`; the old code sent a `speakers` array, which the
        provider fails every time. Four narrators then measured 188-250 Hz median pitch, i.e.
        genuinely different speakers.
        """
        with tempfile.TemporaryDirectory() as td,                 mock.patch.object(pipeline, "request_json", return_value={"data": {"id": "p1"}}) as post,                 mock.patch.object(pipeline, "poll_wavespeed", return_value=(["https://x.test/out.mp3"], {})),                 mock.patch.object(pipeline, "download_file", side_effect=lambda _url, path: Path(path).write_bytes(b"audio")):
            pipeline.generate_speech_gemini(
                "Hello world", Path(td) / "voice", key="test",
                model="pro", voice="Laomedeia")

        model_url, payload = post.call_args.args[1], post.call_args.args[3]
        self.assertIn("google/gemini-2.5-pro/text-to-speech", model_url)
        self.assertEqual(payload["voice"], "Laomedeia", "the narrator must reach the model")
        self.assertNotIn("speakers", payload,
                         "the speakers array is the multi-speaker form and fails for one narrator")

    def test_a_gemini_outage_falls_back_instead_of_going_silent(self):
        """The 2026-08-20 outage produced no voiceover and the run continued on ESTIMATED word
        timing, so captions and cuts drifted against a track that did not exist. Whichever side
        is down, a run must still come back with audio."""
        with tempfile.TemporaryDirectory() as td,                 mock.patch.object(pipeline, "request_json",
                                  side_effect=[RuntimeError("400 Model not found"),
                                               {"data": {"id": "p1"}}]) as post,                 mock.patch.object(pipeline, "poll_wavespeed", return_value=(["https://x.test/out.mp3"], {})),                 mock.patch.object(pipeline, "download_file", side_effect=lambda _url, path: Path(path).write_bytes(b"audio")):
            out = pipeline.generate_speech_gemini(
                "Hello world", Path(td) / "voice", key="test", model="pro", voice="Laomedeia")
            # inside the block: the temp dir is gone once it exits
            self.assertTrue(Path(out).is_file(), "the run went silent instead of falling back")
        second_url, second_payload = post.call_args.args[1], post.call_args.args[3]
        self.assertIn(pipeline.SEED_SPEECH_TTS_MODEL, second_url)
        self.assertEqual(second_payload["voice"], "stokie_en",
                         "a Gemini narrator does not exist on Seed")

    def test_seed_itself_does_not_loop_when_it_fails(self):
        """Seed IS the fallback. Retrying it forever would hide a real outage."""
        with tempfile.TemporaryDirectory() as td,                 mock.patch.object(pipeline, "request_json", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                pipeline.generate_speech_gemini(
                    "Hello world", Path(td) / "voice", key="test",
                    model=pipeline.SEED_SPEECH_TTS_MODEL, voice="stokie_en")

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


if __name__ == "__main__":
    unittest.main()
