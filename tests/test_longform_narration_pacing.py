"""Regression checks for clean TTS input and comfortable First-Dog-style image pacing."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import longform_video as lf


class LongformNarrationPacingTest(unittest.TestCase):
    def test_hidden_web_citations_never_reach_tts(self):
        script = ("Humans remember stories. \ue200cite\ue202turn113search1\ue202turn113search3\ue201 "
                  "But the source marker should never be spoken. turn99fetch2")
        clean = lf.clean_narration_script(script)
        self.assertEqual(clean, "Humans remember stories. But the source marker should never be spoken.")
        self.assertNotIn("turn", " ".join(lf.split_script_for_tts(clean)).lower())

    def test_visual_beats_have_no_orphan_word_cuts(self):
        script = " ".join(f"word{i}" for i in range(46)) + "."
        lines = lf.split_script_lines(script)
        sizes = [len(line.split()) for line in lines]
        # The ceiling may be crossed by a few words, but ONLY to finish a sentence: chopping at
        # exactly 15 here left a one-word tail that then had to be rebalanced into two short
        # beats. Anything longer than the ceiling has to earn it with a full stop.
        self.assertTrue(all(size >= 6 for size in sizes), sizes)
        for line, size in zip(lines, sizes):
            if size > 15:
                self.assertLessEqual(size, 19, sizes)
                self.assertTrue(line.rstrip().endswith((".", "!", "?")),
                                f"a beat ran past the ceiling without finishing a sentence: {line!r}")

    def test_a_finished_sentence_beats_a_comma_at_the_centre(self):
        """A comma nearer the target used to win over a full stop, so the drawing changed in the
        middle of a thought."""
        script = ("Alpha bravo charlie delta echo foxtrot golf. "
                  "Hotel india juliet, kilo lima mike november oscar papa quebec romeo sierra.")
        self.assertEqual(lf.split_script_lines(script)[0],
                         "Alpha bravo charlie delta echo foxtrot golf.")

    def test_a_long_clause_is_not_cut_between_two_of_its_own_words(self):
        """The real failure: no punctuation inside the window, so it chopped at exactly the
        ceiling - "...the exact moment you fell" / "asleep last night."."""
        script = ("Try to remember the exact moment you fell asleep last night. "
                  "Not the last thing you remember doing.")
        first = lf.split_script_lines(script)[0]
        self.assertTrue(first.rstrip().endswith("."), first)
        self.assertIn("fell asleep last night", first)

    def test_natural_punctuation_is_preferred_near_target(self):
        script = ("One two three four five six seven. "
                  "Eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen.")
        lines = lf.split_script_lines(script)
        self.assertEqual(lines[0], "One two three four five six seven.")


if __name__ == "__main__":
    unittest.main()
