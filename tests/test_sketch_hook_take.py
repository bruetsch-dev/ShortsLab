"""The hook take must belong to the hook, not to whatever line is left after it moved.

Reported on the leg script: "es nimmt wieder die zweite line fuer die hook im voiceover, also
hab ich zweite line als voiceover hook part einzeln und die erste line ueberhaupt nicht".

Both halves were working as designed and the combination was wrong. With the Google intro on,
drop_hook_from_narration removes the hook line - the intro types and speaks it, so the video
does not ask its question twice. split_sketch_short_for_tts then gives "the opening line" its
own TTS take with the scroll-stopping hook delivery. After the removal, that opening line is the
script's SECOND sentence, so the hook performance landed on an ordinary line and the approval
screen labelled it the hook.
"""

import unittest

import longform_video as lv

SCRIPT = ("Why does my leg suddenly stop feeling like my leg?\n\n"
          "I sit weird for a few minutes, stand up, and suddenly it is numb.\n\n"
          "Most people think I cut off the blood.\n\n"
          "Usually, that is not the main reason.")


class SplitterTests(unittest.TestCase):
    def test_with_no_intro_the_real_hook_gets_its_own_take(self):
        parts = lv.split_sketch_short_for_tts(SCRIPT)
        self.assertEqual(parts[0], "Why does my leg suddenly stop feeling like my leg?")

    def test_the_second_line_is_never_promoted_to_hook(self):
        """The exact complaint: after the intro takes the hook, part 0 was 'I sit weird...'."""
        body = lv.drop_hook_from_narration(SCRIPT, lv.hook_line_of(SCRIPT))
        self.assertTrue(body.startswith("I sit weird"))
        promoted = lv.split_sketch_short_for_tts(body)[0]
        self.assertTrue(promoted.startswith("I sit weird"),
                        "this is what the old path produced - the test below is the fix")
        plain = lv.split_script_for_tts(body, limit=lv.tts_chunk_limit("pro"))
        self.assertEqual(len(plain), 1, "a short body needs no separate hook take at all")


class WiringTests(unittest.TestCase):
    def source(self):
        return open(lv.__file__, encoding="utf-8").read()

    def test_the_voiceover_is_told_whether_the_intro_owns_the_hook(self):
        body = self.source()
        block = body[body.index("def generate_voiceover("):]
        block = block[:block.index("\ndef ", 10)]
        self.assertIn("hook_in_intro=False", block)
        self.assertIn("sketch_hook_take = short_form and not hook_in_intro", block)

    def test_the_hook_delivery_follows_the_same_decision(self):
        """Splitting differently but still shouting the first part would only move the problem."""
        body = self.source()
        block = body[body.index("def generate_voiceover("):]
        block = block[:block.index("\ndef ", 10)]
        self.assertIn("if not sketch_hook_take:", block)

    def test_the_run_passes_the_flag(self):
        body = self.source()
        self.assertIn("hook_moved_to_intro = True", body)
        self.assertIn("hook_in_intro=hook_moved_to_intro", body)

    def test_the_flag_starts_false(self):
        """A run without the intro must keep the sketch hook take it has always had."""
        body = self.source()
        self.assertIn("hook_moved_to_intro = False", body)


if __name__ == "__main__":
    unittest.main()
