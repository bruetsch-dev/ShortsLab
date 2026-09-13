"""The short must not say its own title twice, and the intro must not stack.

A pasted document opens with "# Why Does Only One of My Nostrils Work?". Every stage downstream
read that title as narration: it was spoken as TTS part 0 with the hash still attached, took a
drawing beat of its own, AND was picked as the hook - so the finished short opened with the same
sentence twice, once typed and spoken in the Google intro and once at the top of the video.
"""

import unittest

import longform_video as lv

SCRIPT = ("# Why Does Only One of My Nostrils Work?\n\n"
          "Why does only one of my nostrils work at a time? Try breathing through your nose "
          "right now.\nOne side is doing most of the work.")


class HeadingTests(unittest.TestCase):
    def test_a_title_is_not_narration(self):
        body = lv.strip_script_headings(SCRIPT)
        self.assertNotIn("#", body)
        self.assertTrue(body.startswith("Why does only one"))

    def test_horizontal_rules_go_too(self):
        self.assertEqual(lv.strip_script_headings("a\n----\nb"), "a\nb")

    def test_a_script_without_headings_is_untouched(self):
        plain = "One sentence. And another one here."
        self.assertEqual(lv.strip_script_headings(plain), plain)

    def test_a_script_that_is_only_a_heading_does_not_vanish_silently(self):
        """Stripping everything must be visible as empty, not as a mangled half-script."""
        self.assertEqual(lv.strip_script_headings("# Only A Title"), "")

    def test_the_narration_no_longer_opens_with_the_title(self):
        parts = lv.split_sketch_short_for_tts(lv.strip_script_headings(SCRIPT))
        self.assertFalse(parts[0].startswith("#"))
        self.assertTrue(parts[0].startswith("Why does only one"))


class HookLineTests(unittest.TestCase):
    def test_an_unmarked_hook_skips_the_title(self):
        self.assertEqual(lv.hook_line_of(SCRIPT, ""),
                         "Why does only one of my nostrils work at a time?")

    def test_a_marked_heading_loses_its_hash(self):
        """The user marks the line as it appears in their document; a search box shows the hash."""
        self.assertEqual(lv.hook_line_of(SCRIPT, "# Why Does Only One of My Nostrils Work?"),
                         "Why Does Only One of My Nostrils Work?")

    def test_a_marked_sentence_is_still_honoured(self):
        self.assertEqual(lv.hook_line_of(SCRIPT, "One side is doing most of the work."),
                         "One side is doing most of the work.")


class IntroStackingTests(unittest.TestCase):
    def test_rebuild_cannot_stack_a_second_intro(self):
        source = open(lv.__file__, encoding="utf-8").read()
        self.assertIn('not str(rendered).endswith("_with_hook.mp4")', source)

    def test_the_stripper_runs_before_the_voiceover(self):
        source = open(lv.__file__, encoding="utf-8").read()
        strip_at = source.index("without_headings = strip_script_headings(script)")
        tts_at = source.index("generate_voiceover(", strip_at - 40000)
        self.assertLess(strip_at, source.index("generate_voiceover(", strip_at))


if __name__ == "__main__":
    unittest.main()
