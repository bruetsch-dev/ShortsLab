"""The hook belongs in the intro OR in the video, never in both.

Reported twice. The Google intro types the hook and reads it aloud, and the script's opening
sentence is usually that same hook - so the finished short asked its question, then asked it
again as the first line of the video. Confirmed in the project state: `tts_part_texts[0]` was
"Why does your stomach growl when you are NOT hungry?" and the intro spoke the same words.

The second half is what the search box shows. Nobody googles their own symptom in the second
person: a script that says "why does YOUR stomach growl" came from a search for "why does MY
stomach growl". The box now shows what a person typed while the narrator keeps the script's
own wording.
"""

import unittest

import longform_video as lv


class TypedQueryTests(unittest.TestCase):
    def test_the_box_shows_a_first_person_search(self):
        self.assertEqual(lv.as_typed_search("Why does your stomach growl when you are NOT hungry?"),
                         "why does my stomach growl when im not hungry")

    def test_it_reads_like_something_someone_typed(self):
        """Lower case, no trailing punctuation, no apostrophe - a real search history."""
        typed = lv.as_typed_search("Why do you get an earworm?")
        self.assertEqual(typed, "why do i get an earworm")
        self.assertNotIn("?", typed)

    def test_only_whole_words_are_rewritten(self):
        """A naive replace turns "young" into "iung" and "yourself" into "myrself"."""
        self.assertEqual(lv.as_typed_search("Young people yawn more."), "young people yawn more")
        self.assertIn("myself", lv.as_typed_search("You ask yourself why."))

    def test_longer_phrases_win_over_shorter_ones(self):
        """"you are" must be rewritten before "you", or it becomes "i are"."""
        self.assertIn("im", lv.as_typed_search("Why you are tired."))
        self.assertNotIn("i are", lv.as_typed_search("Why you are tired."))

    def test_a_first_person_script_is_left_alone(self):
        line = "Why does only one of my nostrils work at a time?"
        self.assertEqual(lv.as_typed_search(line),
                         "why does only one of my nostrils work at a time")

    def test_nothing_in_nothing_out(self):
        for value in ("", None, "   "):
            self.assertEqual(lv.as_typed_search(value), "")


class NoDoubleHookTests(unittest.TestCase):
    SCRIPT = ("Why does your stomach growl when you are NOT hungry?\n\n"
              "That noise is not your stomach asking for food. It is called borborygmi.")

    def test_the_narration_starts_after_the_hook(self):
        hook = lv.hook_line_of(self.SCRIPT, "")
        body = lv.drop_hook_from_narration(self.SCRIPT, hook)
        self.assertTrue(body.startswith("That noise"))
        self.assertNotIn("stomach growl when", body)

    def test_a_hook_marked_mid_script_is_still_narrated(self):
        """Only an EXACT opening match is removed - the user may mark any sentence."""
        body = lv.drop_hook_from_narration(self.SCRIPT, "It is called borborygmi.")
        self.assertEqual(body, self.SCRIPT.strip())

    def test_punctuation_and_spacing_do_not_defeat_the_match(self):
        body = lv.drop_hook_from_narration(
            self.SCRIPT, "why does your stomach   growl when you are not hungry")
        self.assertTrue(body.startswith("That noise"))

    def test_no_hook_means_no_change(self):
        self.assertEqual(lv.drop_hook_from_narration(self.SCRIPT, ""), self.SCRIPT.strip())

    def test_it_only_runs_when_the_intro_is_actually_used(self):
        src = open(lv.__file__, encoding="utf-8").read()
        # Bounded by STRUCTURE, not by a character count. Twice now a line added inside this
        # block pushed the call past a fixed window and failed a test that is about WHERE the
        # call lives, not how much sits above it - first a log line, then the opener prompt.
        # The block ends where the next statement at that indent begins.
        block = src[src.index("    if hook_intro:"):]
        block = block[:block.index("\n    without_headings")]
        self.assertIn("drop_hook_from_narration", block)
        self.assertNotIn("drop_hook_from_narration",
                         src[:src.index("    if hook_intro:")].rsplit("def run_longform_video(", 1)[-1],
                         "the narration must only lose its hook when the intro speaks it")

    def test_the_intro_speaks_the_script_line_not_the_typed_query(self):
        """The box shows "why does my stomach growl"; the narrator should not read that back."""
        src = open(lv.__file__, encoding="utf-8").read()
        self.assertIn("spoken_text=spoken", src)
        intro = open("sketch_hook_intro.py", encoding="utf-8").read()
        self.assertIn("spoken_text", intro)
        self.assertIn("say = ", intro)


class WhichSentenceGetsTypedTests(unittest.TestCase):
    """Reported: the box typed the script's SECOND sentence.

    The nostrils script is written the way all of these are - a "# question" title followed by
    the body - and the intro typed "try breathing through my nose right now" instead of "why
    does only one of my nostrils work". Two separate causes, both reproduced before the fix:
    the fallback dropped the heading as a document title, and the sentence trimmer scanned
    ". ", "? ", "! " in tuple order rather than by position.
    """

    HEADING = ("# Why does only one of my nostrils work?\n\n"
               "Try breathing through your nose right now. One side feels more open.")
    PLAIN = ("Why does only one of my nostrils work?\n\n"
             "Try breathing through your nose right now. One side feels more open.")
    ONE_PARAGRAPH = ("Why does only one of my nostrils work? "
                     "Try breathing through your nose right now. One side feels more open.")
    WANTED = "why does only one of my nostrils work"

    def typed(self, script, marked=None):
        return lv.as_typed_search(lv.hook_line_of(script, marked))

    def test_a_question_heading_is_the_hook_not_a_title(self):
        self.assertEqual(self.typed(self.HEADING), self.WANTED)

    def test_a_plain_opening_line_still_works(self):
        self.assertEqual(self.typed(self.PLAIN), self.WANTED)

    def test_the_trim_stops_at_the_first_terminator_not_the_first_pattern(self):
        """A "? " earlier in the string must beat a ". " further along it."""
        self.assertEqual(self.typed(self.ONE_PARAGRAPH), self.WANTED)

    def test_a_heading_that_is_not_a_question_is_still_skipped(self):
        """The original reason for dropping headings survives: a title is not a search."""
        script = "# The Nasal Cycle\n\nWhy does only one of my nostrils work? Try breathing."
        self.assertEqual(self.typed(script), self.WANTED)

    def test_the_narrations_own_question_beats_the_title(self):
        """Both ask the same thing, and only the narration's copy can be removed from it.

        Preferring the title here would leave the body's near-identical sentence in place, which
        is the "asked twice" bug this file's other half exists for.
        """
        script = ("# Why Does Only One of My Nostrils Work?\n\n"
                  "Why does only one of my nostrils work at a time? Try breathing.")
        hook = lv.hook_line_of(script)
        self.assertEqual(hook, "Why does only one of my nostrils work at a time?")
        self.assertTrue(lv.drop_hook_from_narration(lv.strip_script_headings(script), hook)
                        .startswith("Try breathing"))

    def test_a_marked_hook_always_wins(self):
        self.assertEqual(self.typed(self.HEADING, "# Why Does Only One of My Nostrils Work?"),
                         self.WANTED)

    def test_the_question_is_still_asked_only_once(self):
        for script in (self.HEADING, self.PLAIN, self.ONE_PARAGRAPH):
            hook = lv.hook_line_of(script)
            body = lv.strip_script_headings(lv.drop_hook_from_narration(script, hook))
            self.assertTrue(body.startswith("Try breathing"), body[:60])

    def test_the_run_says_which_sentence_it_picked(self):
        """Silence is how the wrong sentence reached a finished video unnoticed."""
        src = open(lv.__file__, encoding="utf-8").read()
        self.assertIn("Google intro will type", src)


if __name__ == "__main__":
    unittest.main()
