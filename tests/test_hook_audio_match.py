"""The hook intro must sound like part of the same video.

Reported as "wie ers spricht is weird in der hook und anfang". Two measurable causes, neither of
them the voice itself:

1. LOUDNESS. The intro was mastered to -15 LUFS and glued in front of narration sitting at
   -20.3 - measured on a delivered short: intro -14.9, video -20.3, a 5.4 dB jolt on the first
   word of every video.

2. THE DIRECTION. Inworld takes a stage direction as a short bracket clause; its own example is
   one line. The intro was handing it the GEMINI style text - 395 characters - which reads as
   instruction weight rather than performance. Known Gemini styles now map to their Inworld
   twins, and anything else is trimmed to its first clause.

Separately, a hard-wrapped script put pauses inside sentences on every provider: measured on the
same three sentences, four pauses over 0.45s when wrapped against one when flattened.
"""

import unittest

import pipeline
import sketch_hook_intro as hook


class LoudnessTests(unittest.TestCase):
    def test_the_intro_targets_the_narration_s_level(self):
        self.assertEqual(hook.HOOK_TARGET_LUFS, -20.0)

    def test_the_mix_uses_that_target(self):
        src = open(hook.__file__, encoding="utf-8").read()
        self.assertIn("loudnorm=I={HOOK_TARGET_LUFS}", src)
        self.assertNotIn("loudnorm=I=-15", src)

    def test_it_matches_what_the_voiceover_parts_are_levelled_to(self):
        """Both ends of the join have to agree, or the fix only moves the step."""
        import longform_video as lv
        self.assertEqual(hook.HOOK_TARGET_LUFS, lv.VOICEOVER_TARGET_LUFS)


class DirectionTests(unittest.TestCase):
    def test_gemini_styles_map_to_inworld_clauses(self):
        for style in (pipeline.TTS_STYLE_SKETCH_SHORT_HOOK, pipeline.TTS_STYLE_SKETCH_SHORT,
                      pipeline.TTS_STYLE_LONGFORM):
            twin = pipeline.INWORLD_STYLE_EQUIVALENTS.get(style)
            self.assertTrue(twin, f"{style[:40]!r} has no Inworld equivalent")
            self.assertLess(len(twin), 160)

    def test_an_unknown_briefing_is_trimmed_rather_than_sent_whole(self):
        src = open(pipeline.__file__, encoding="utf-8").read()
        block = src[src.index("INWORLD_STYLE_EQUIVALENTS.get(note.strip(), note)"):][:400]
        self.assertIn("if len(note) > 160:", block)

    def test_the_gemini_style_is_much_longer_than_what_inworld_wants(self):
        """The number this rule exists for: 395 characters against a one-clause example."""
        self.assertGreater(len(pipeline.TTS_STYLE_SKETCH_SHORT_HOOK), 300)


class FlatteningTests(unittest.TestCase):
    def test_a_wrapped_sentence_becomes_one_line(self):
        wrapped = "A song gets stuck, usually" + chr(10) + "fifteen seconds long."
        self.assertEqual(pipeline.flatten_for_tts(wrapped),
                         "A song gets stuck, usually fifteen seconds long.")

    def test_paragraph_breaks_survive(self):
        text = "First para." + chr(10) * 2 + "Second para."
        self.assertEqual(pipeline.flatten_for_tts(text).count(chr(10) * 2), 1)

    def test_every_provider_gets_the_flattened_text(self):
        """Done once at the top of the call, not per branch - all three engines pause on a
        newline."""
        src = open(pipeline.__file__, encoding="utf-8").read()
        block = src[src.index("def generate_speech_gemini("):][:1800]
        self.assertIn("text = flatten_for_tts(text)", block)
        self.assertLess(block.index("text = flatten_for_tts(text)"),
                        block.index("is_seed ="))

    def test_empty_input_stays_empty(self):
        for value in ("", None, "   "):
            self.assertEqual(pipeline.flatten_for_tts(value), "")


if __name__ == "__main__":
    unittest.main()
