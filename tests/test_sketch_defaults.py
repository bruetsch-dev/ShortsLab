"""The Sketch Explainer has explicit defaults, not "whatever is first in the list".

Requested 2026-09-02: Seed TTS with the `jess` voice, English, speed 1.14, volume 1.05, and
Gemini Flash 3.7 as the reasoning model.

Before this, `S.longform` started as `{}` and every value fell through to
`firstVal(OPT.longform_tts)` / `firstVal(OPT.tts_voice)` / `firstVal(OPT.longform_reasoning)` -
so the defaults were an accident of list order (Gemini 2.5 Pro TTS, the first Gemini voice,
Claude Opus 4.8) and would change silently whenever a list was reordered.
"""

import re
import unittest
from pathlib import Path

JS = Path("static/chat-shell.js").read_text(encoding="utf-8")
BLOCK = JS[JS.index("const SKETCH_DEFAULTS"):]
BLOCK = BLOCK[:BLOCK.index("}")]


class DefaultsTests(unittest.TestCase):
    def value(self, key):
        match = re.search(rf'{key}:\s*"([^"]*)"', BLOCK)
        self.assertIsNotNone(match, key)
        return match.group(1)

    def test_the_narrator_is_seed_jess_in_english(self):
        self.assertEqual(self.value("tts_model"), "bytedance/seed-speech-tts-2.0")
        self.assertEqual(self.value("tts_voice"), "jess_ja_es_id_pt_en_zh")
        self.assertEqual(self.value("tts_language"), "en")

    def test_the_delivery_numbers(self):
        self.assertEqual(self.value("tts_native_speed"), "1.14")
        self.assertEqual(self.value("tts_volume"), "1.05")

    def test_the_reasoning_model(self):
        self.assertEqual(self.value("reasoning_model"), "google/gemini-3.7-flash")


class ApplicationTests(unittest.TestCase):
    def test_the_flow_seeds_them(self):
        flow = JS[JS.index("function renderLongformFlow"):]
        self.assertIn("applySketchDefaults(S.longform)", flow[:200])

    def test_a_deliberate_choice_is_never_overwritten(self):
        """Re-entering the settings step must not undo what the user picked."""
        helper = JS[JS.index("function applySketchDefaults"):]
        helper = helper[:helper.index("\n}")]
        self.assertIn('== null || holder[key] === ""', helper)

    def test_the_nine_sixteen_bump_cannot_contradict_the_default(self):
        """The 9:16 switch sets SHORTS_VOICE_SPEED; two different numbers for one thing is how
        a configured default gets silently undone."""
        match = re.search(r'const SHORTS_VOICE_SPEED = "([^"]+)"', JS)
        self.assertIsNotNone(match)
        speed = re.search(r'tts_native_speed:\s*"([^"]*)"', BLOCK).group(1)
        self.assertEqual(match.group(1), speed)


class OptionExistenceTests(unittest.TestCase):
    """A default that is not in its own dropdown silently falls back to something else."""

    def test_every_default_is_a_real_option(self):
        import chat_ui
        options = chat_ui.extract_legacy_options()

        def values(key):
            return {x.get("value") if isinstance(x, dict) else x for x in (options.get(key) or [])}

        self.assertIn("bytedance/seed-speech-tts-2.0", values("longform_tts"))
        self.assertIn("jess_ja_es_id_pt_en_zh", values("tts_voice_seed"))
        self.assertIn("google/gemini-3.7-flash", values("longform_reasoning"))


if __name__ == "__main__":
    unittest.main()
