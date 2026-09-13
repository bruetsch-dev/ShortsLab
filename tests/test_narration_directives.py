"""Nothing that addresses the production may reach the narrator.

Measured 2026-08-29: a Luna scrape-mode script for "why nobody talks on Tokyo trains" opened a
paragraph with "Now film the payoff: riders read, sleep, or stare silently". The script prompt
uses the word "film" fourteen times to describe what footage EXISTS, and the model reused that
vocabulary in a line meant to be spoken. Nothing downstream flags it, because it is fluent -
the finished Short simply says the words out loud.

The prompt now forbids it and this strips whatever still gets through.
"""

import unittest

import agent_core
import longform_video as lf


class StripDirectivesTests(unittest.TestCase):
    def test_the_measured_leak_is_removed(self):
        got = lf.clean_narration_script(
            "Now film the payoff: riders read, sleep, or stare silently while the train moves.")
        self.assertEqual(got, "Riders read, sleep, or stare silently while the train moves.")

    def test_the_real_sentence_survives_the_cut(self):
        """Deleting the whole sentence would silently shorten the Short."""
        got = lf.clean_narration_script("Cut to: the doors slide open.")
        self.assertEqual(got, "The doors slide open.")

    def test_a_sentence_that_is_only_a_direction_goes_away(self):
        self.assertEqual(lf.clean_narration_script("B-roll of the platform."), "")

    def test_paragraphs_are_preserved(self):
        got = lf.clean_narration_script("First line.\n\nNow film the payoff: second line.")
        self.assertEqual(got, "First line.\n\nSecond line.")

    def test_ordinary_uses_of_those_verbs_are_untouched(self):
        """'watch' and 'film' are real English; only a production ADDRESS is an artifact."""
        for line in ("Commuters watch the doors close.",
                     "She films her lunch every morning.",
                     "Etiquette signs ask passengers to avoid phone calls.",
                     "The show goes on. Nobody speaks."):
            self.assertEqual(lf.clean_narration_script(line), line, line)

    def test_an_empty_script_stays_empty(self):
        self.assertEqual(lf.clean_narration_script(""), "")


class PromptTests(unittest.TestCase):
    def test_the_script_prompt_forbids_addressing_the_camera(self):
        """The stripper is a net. The model must be told, or it keeps writing them."""
        source = open(agent_core.__file__, encoding="utf-8").read()
        self.assertIn("EVERY WORD YOU WRITE IS SPOKEN ALOUD", source)

    def test_the_rule_sits_in_the_scrape_briefing(self):
        """That briefing is where the word 'film' appears - the rule has to answer it there."""
        source = open(agent_core.__file__, encoding="utf-8").read()
        block = source[source.index("FOOTAGE REALITY:"):]
        block = block[:block.index("if topic:")]
        self.assertIn("EVERY WORD YOU WRITE IS SPOKEN ALOUD", block)


if __name__ == "__main__":
    unittest.main()
