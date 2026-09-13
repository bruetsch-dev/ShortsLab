"""A beat about absence must still be drawn as a scene.

Measured on the finished sleep short: beat 16 ("It was simply never written down.") produced the
prompt "blank empty white notebook page with a crossed-out pencil floating beside a faint glowing
dot", and the generator obeyed exactly - 1.69% of the page carried any ink, against about 10% for
a normal drawing. Regenerating changed nothing, because the prompt was the defect. Beat 14 asked
for "a single glowing dot floating in a dark blue void" and landed at 3.88%.
"""

import unittest

import longform_video as lv


class AbsenceRuleTests(unittest.TestCase):
    def setUp(self):
        self.prompt = lv.STAGE3_PROMPT

    def test_the_rule_exists(self):
        self.assertIn("ABSENCE IS STILL DRAWN", self.prompt)

    def test_it_names_the_exact_phrasings_that_produced_bare_paper(self):
        for phrase in ('"a blank page"', '"an empty void"'):
            self.assertIn(phrase, self.prompt)

    def test_it_offers_an_action_to_draw_instead_of_only_a_ban(self):
        """A rule that only forbids leaves the writer with nothing to write."""
        self.assertIn("Draw the absence as an ACTION instead", self.prompt)

    def test_it_survives_translation_into_the_shorts_format(self):
        self.addCleanup(lv.set_project_aspect, "16:9")
        lv.set_project_aspect("9:16")
        self.assertIn("ABSENCE IS STILL DRAWN", lv.fit_aspect(lv.STAGE3_PROMPT))

    def test_the_scene_rule_it_sits_next_to_is_untouched(self):
        self.assertIn("PUT THE SCENE SOMEWHERE", self.prompt)
        self.assertIn("DIAGRAMS AND PANELS ARE PART OF THE RHYTHM", self.prompt)


if __name__ == "__main__":
    unittest.main()
