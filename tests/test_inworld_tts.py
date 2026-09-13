"""Inworld Realtime TTS-2 as a third provider.

Three providers, three different ways of taking direction, and that is the whole difficulty:
Gemini reads a directive prefixed to the text, Seed reads a `voice_instruction` field, and
Inworld reads a SQUARE-BRACKET stage direction on its own line - the model's own example being
"[speak quickly, clearly, and calmly, like an automated travel notification]". Measured live on
one sentence: 8.4s with the default upbeat note, 11.3s with a calm documentary note, so the
bracket is performed and not spoken.

Inworld is also the only TTS here that declares a text limit - 2000 characters - which is why
the chunk size had to become per-provider. The 2600 that suits Seed is a hard overflow here.
"""

import unittest
from pathlib import Path

import longform_video as lv
import pipeline

ROOT = Path(__file__).resolve().parent.parent
SRC = (ROOT / "longform_video.py").read_text(encoding="utf-8")


class ProviderTests(unittest.TestCase):
    def test_the_aliases_resolve_to_inworld(self):
        for alias in ("inworld", "realtime-tts-2", pipeline.INWORLD_TTS_MODEL):
            self.assertEqual(pipeline.tts_provider(alias), "inworld", alias)

    def test_the_other_providers_are_unaffected(self):
        self.assertEqual(pipeline.tts_provider("pro"), "gemini")
        self.assertEqual(pipeline.tts_provider("seed-speech"), "seed")

    def test_each_provider_offers_only_its_own_voices(self):
        self.assertEqual(len(pipeline.tts_voices_for("inworld")), 65)
        self.assertIn("Dennis", pipeline.tts_voices_for("inworld"))
        self.assertNotIn("stokie_en", pipeline.tts_voices_for("inworld"))
        self.assertNotIn("Laomedeia", pipeline.tts_voices_for("inworld"))

    def test_accented_voice_names_survived_the_transcription(self):
        """Hélène, Étienne and Maitê are real entries in the enum; a mangled name is rejected."""
        names = pipeline.INWORLD_TTS_VOICES
        self.assertIn("Hélène", names)
        self.assertIn("Étienne", names)
        self.assertIn("Maitê", names)


class DirectionTests(unittest.TestCase):
    def test_a_direction_becomes_a_bracket_line(self):
        out = pipeline.inworld_direction("Hello there.", "speak calmly")
        self.assertTrue(out.startswith("[speak calmly]"))
        self.assertIn("\nHello there.", out)

    def test_no_direction_leaves_the_text_alone(self):
        self.assertEqual(pipeline.inworld_direction("Hello there.", ""), "Hello there.")
        self.assertEqual(pipeline.inworld_direction("Hello there.", None), "Hello there.")

    def test_an_existing_bracket_is_not_doubled(self):
        already = "[already directed]\nHello."
        self.assertEqual(pipeline.inworld_direction(already, "speak calmly"), already)

    def test_brackets_typed_by_the_user_are_not_nested(self):
        out = pipeline.inworld_direction("Hi.", "[speak calmly]")
        self.assertEqual(out.count("["), 1)


class PayloadTests(unittest.TestCase):
    def test_the_branch_sends_the_fields_this_model_declares(self):
        src = (ROOT / "pipeline.py").read_text(encoding="utf-8")
        block = src[src.index("if is_inworld:", src.index("def generate_speech_gemini")):]
        block = block[:block.index("elif is_seed:")]
        for field in ('"voice_id"', '"speaking_rate"', '"output_format"'):
            self.assertIn(field, block)
        # the fields the OTHER providers use must not leak in
        for foreign in ('"speakers"', '"voice_instruction"'):
            self.assertNotIn(foreign, block)

    def test_an_oversized_part_is_refused_with_the_reason(self):
        block = (ROOT / "pipeline.py").read_text(encoding="utf-8")
        self.assertIn("the caller must chunk with pipeline.tts_text_limit()", block)

    def test_an_unknown_voice_falls_back_to_a_real_one(self):
        self.assertIn(pipeline.INWORLD_DEFAULT_VOICE, pipeline.INWORLD_TTS_VOICES)


class ChunkingTests(unittest.TestCase):
    SCRIPT = ("Your body runs a slow rhythm you never agreed to. One nostril opens while the "
              "other narrows. ") * 40

    def test_inworld_chunks_smaller_than_its_declared_limit(self):
        limit = lv.tts_chunk_limit("inworld")
        self.assertLess(limit, pipeline.INWORLD_TEXT_LIMIT)
        for part in lv.split_script_for_tts(self.SCRIPT, limit=limit):
            self.assertLess(len(part), pipeline.INWORLD_TEXT_LIMIT)

    def test_room_is_left_for_the_bracket_line(self):
        """The direction is prepended AFTER chunking, so the limit has to allow for it."""
        headroom = pipeline.INWORLD_TEXT_LIMIT - lv.tts_chunk_limit("inworld")
        self.assertGreater(headroom, len(pipeline.INWORLD_LONGFORM_DIRECTION) + 10)

    def test_a_provider_without_a_declared_limit_keeps_the_global_one(self):
        self.assertEqual(lv.tts_chunk_limit("seed-speech"), lv.TTS_PART_CHAR_LIMIT)
        self.assertEqual(lv.tts_chunk_limit("pro"), lv.TTS_PART_CHAR_LIMIT)

    def test_the_splitter_is_asked_for_the_model_s_limit(self):
        self.assertIn("split_script_for_tts(script, limit=tts_chunk_limit(tts_model))", SRC)


class WiringTests(unittest.TestCase):
    def test_longform_gets_the_documentary_direction(self):
        block = SRC[SRC.index("def _part_tts_kwargs"):]
        block = block[:block.index("if short_form")] if "if short_form" in block else block[:2500]
        self.assertIn("INWORLD_LONGFORM_DIRECTION", block)

    def test_the_short_hook_gets_its_own(self):
        self.assertIn("INWORLD_SHORT_HOOK_DIRECTION", SRC)
        self.assertIn("INWORLD_SHORT_DIRECTION", SRC)

    def test_a_hand_typed_direction_still_wins_everywhere(self):
        block = SRC[SRC.index("def _part_tts_kwargs"):][:2600]
        self.assertIn("if supplied or provider ==", block)

    def test_the_ui_offers_the_model_and_its_voices(self):
        app = (ROOT / "app.py").read_text(encoding="utf-8")
        shell = (ROOT / "static" / "chat-shell.js").read_text(encoding="utf-8")
        chat = (ROOT / "chat_ui.py").read_text(encoding="utf-8")
        self.assertIn("Inworld Realtime TTS-2", app)
        self.assertIn("tts_voice_inworld", chat)
        self.assertIn("inworld: OPT.tts_voice_inworld", shell)


if __name__ == "__main__":
    unittest.main()
