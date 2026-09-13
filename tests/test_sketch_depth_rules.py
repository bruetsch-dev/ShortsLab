"""The sketch drawing rules must not ask for three depth layers on a close-up.

Reported after a Library-of-Alexandria sketch short: the images came back as intersecting flat
shapes with nothing behind anything, and the character's head changed size violently between
frames. Two rules were fighting each other - 9a demanded all three layers named "every time",
while 9d demanded a different camera framing every frame, so an extreme close-up still had to
carry a far horizon and a foreground branch.

Both the Short and the longform film read the SAME rules: `fit_aspect` only swaps the aspect
wording and appends a shorts addendum, so a fix here has to reach both.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import longform_video


def rules():
    with open(longform_video.__file__, encoding="utf-8") as handle:
        source = handle.read()
    start = source.index("9a. PUT THE SCENE SOMEWHERE")
    return source[start:start + 6000]


class SketchDepthRules(unittest.TestCase):

    def test_the_layers_are_tied_to_the_framing(self):
        text = rules()
        self.assertIn("THE LAYERS MUST BE AT DIFFERENT DISTANCES", text)
        self.assertIn("On an extreme close-up there is no far horizon", text)

    def test_the_camera_moves_one_step_at_a_time(self):
        text = rules()
        self.assertIn("MOVE ONE STEP AT A TIME", text)
        self.assertIn("head change size violently", text)

    def test_the_character_keeps_its_proportions(self):
        self.assertIn("the same head-to-body", rules())

    def test_the_short_gets_the_same_rules_as_the_film(self):
        """A 9:16 run must not quietly drop them - it shares the text."""
        short = longform_video.fit_aspect(rules(), "9:16")
        self.assertIn("THE LAYERS MUST BE AT DIFFERENT DISTANCES", short)
        self.assertIn("MOVE ONE STEP AT A TIME", short)


if __name__ == "__main__":
    unittest.main()


class ShortFormRulesFromAMeasuredRun(unittest.TestCase):
    """Both faults were measured on `for_thousands_of_years_people_slept` (17 images, 45s).

    * the background colour changed at 16 of 16 cuts - seventeen colours in forty-five seconds,
      which flickers rather than moving. The old rule said "changes every few pictures" and
      warned that two identical backgrounds in a row read as a frozen video, so the model
      changed it on every single frame.
    * the one labelled diagram carried the whole point of the video - FIRST SLEEP / MIDNIGHT
      WAKE / SECOND SLEEP - at a size nobody can read on a phone. Rule 10 already asked for
      one-word labels; nothing said how BIG they have to be, or what to do when the idea does
      not fit in one.
    """

    def setUp(self):
        self.short = longform_video.fit_aspect("STAGE 3 rules", "9:16")

    def test_the_background_holds_across_several_pictures(self):
        self.assertIn("HOLDS FOR THREE OR FOUR PICTURES", self.short)
        self.assertNotIn("THE BACKGROUND COLOUR CHANGES EVERY FEW PICTURES", self.short)

    def test_two_identical_backgrounds_in_a_row_are_no_longer_a_fault(self):
        self.assertIn("Two identical backgrounds in a row are fine", self.short)

    def test_a_label_has_to_be_readable_on_a_phone(self):
        self.assertIn("READABLE ON A PHONE", self.short)
        self.assertIn("ONE capital word each", self.short)

    def test_an_idea_that_does_not_fit_becomes_pictures_instead(self):
        self.assertIn("draw it as consecutive pictures instead", self.short)

    def test_none_of_this_reaches_the_landscape_film(self):
        """The 20-minute format has room to read; only the short is constrained."""
        wide = longform_video.fit_aspect("STAGE 3 rules", "16:9")
        self.assertNotIn("READABLE ON A PHONE", wide)
        self.assertNotIn("HOLDS FOR THREE OR FOUR PICTURES", wide)
