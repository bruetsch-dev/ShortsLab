"""The narrator is called by a name, and the engine still gets its id.

Forty-one Seed voices were listed by their engine key - "jess_ja_es_id_pt_en_zh" - in the
dropdown, in the Narration panel's corner label and again in the bottom dock. So the one item on
the screen that actually HAS a human name was the only one wearing a machine id, three times over.

What matters here is that only the label changed. The value posted to the pipeline is the key the
engine knows, and if that ever drifts the run fails with an unknown voice.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import chat_ui
import pipeline

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class NarratorHasAName(unittest.TestCase):
    def test_the_key_is_written_out_for_a_reader(self):
        self.assertEqual("Jess · JA ES ID PT EN ZH",
                         chat_ui.seed_voice_label("jess_ja_es_id_pt_en_zh"))
        self.assertEqual("Stokie · EN", chat_ui.seed_voice_label("stokie_en"))
        # two words before the first language code are one narrator, not two
        self.assertEqual("Monkey King · ZH", chat_ui.seed_voice_label("monkey_king_zh"))
        # "mixed" is a property of the voice, not a language, so it is not listed as one
        self.assertEqual("Vivi · EN ZH JA ES ID · mixed",
                         chat_ui.seed_voice_label("vivi_mixed_en_zh_ja_es_id"))

    def test_an_unreadable_key_is_left_exactly_as_it_is(self):
        """Better a raw key than a confident mistranslation of one."""
        self.assertEqual("en", chat_ui.seed_voice_label("en"))
        self.assertEqual("", chat_ui.seed_voice_label(""))
        self.assertEqual("", chat_ui.seed_voice_label(None))

    def test_every_shipped_voice_keeps_the_key_the_engine_knows(self):
        options = {}
        for voice in pipeline.SEED_SPEECH_TTS_VOICES:
            options[voice] = chat_ui.seed_voice_label(voice)
        self.assertEqual(sorted(options), sorted(pipeline.SEED_SPEECH_TTS_VOICES),
                         "a voice key was changed, not just its label")
        for voice, label in options.items():
            self.assertTrue(label, f"{voice} has no label at all")
            self.assertNotEqual(voice, label,
                                f"{voice} is still shown to the reader as its engine key")

    def test_the_panel_and_the_dock_read_the_label_the_dropdown_reads(self):
        shell = open(os.path.join(ROOT, "static", "shell-v3.js"), encoding="utf-8").read()
        self.assertIn("const voiceLabel = (model, value) =>", shell)
        self.assertIn('group("Narration", voiceLabel(L.tts_model, L.tts_voice)', shell)
        self.assertIn('["Narrator", voiceLabel(L.tts_model, L.tts_voice)]', shell)


if __name__ == "__main__":
    unittest.main()
