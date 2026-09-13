"""The sketch explainer must draw SITUATIONS, not concepts.

Side-by-side against a reference channel, the gap was art direction, not image quality: our
frames were a stick figure plus a giant symbol on a flat colour, theirs were characters inside a
world. Worse, the brief TAUGHT the failure - rule 6 held up "a huge hourglass with almost all the
sand fallen through" as its worked example, and rule 10 listed "one large object centred on a
plain background" as a frame type. Both are now the thing being removed.

Measured symptoms these rules answer: four consecutive dark-blue backgrounds in the opening
twenty seconds; frames whose subject was a giant red X, a giant question mark, a pair of
scissors, a gear over a head; and a character centred, front-on, full body six times running.
"""

import unittest

import longform_video as lv

PROMPT = lv.STAGE3_PROMPT


class SituationTests(unittest.TestCase):
    def test_giant_symbols_are_banned_as_subjects(self):
        self.assertIn("BANNED as the SUBJECT of a frame", PROMPT)
        for icon in ("giant question mark", "giant red X", "giant pair of\n   scissors",
                     "giant hourglass", "gear over a head"):
            self.assertIn(icon, PROMPT, f"{icon!r} is not ruled out")

    def test_they_are_still_allowed_as_small_props(self):
        """A blanket ban would remove a legitimate visual language - the rule is about SIZE and
        ROLE, not about ever drawing a question mark."""
        self.assertIn("may appear SMALL inside a scene", PROMPT)

    def test_the_worked_examples_now_stage_a_moment(self):
        self.assertIn("NOT a huge hourglass", PROMPT)
        self.assertIn("NOT a giant X over a bed", PROMPT)

    def test_the_icon_frame_type_is_gone_from_the_menu(self):
        self.assertNotIn("**Single concept object:**", PROMPT)
        self.assertIn("**Object IN USE:**", PROMPT)


class SceneTests(unittest.TestCase):
    def test_a_frame_is_built_in_three_named_layers(self):
        for layer in ("BACKGROUND:", "MIDGROUND:", "FOREGROUND:"):
            self.assertIn(layer, PROMPT)

    def test_the_requirement_is_a_quota_not_a_suggestion(self):
        self.assertIn("At least three quarters of the frames", PROMPT)

    def test_a_room_is_asked_for_by_its_props(self):
        self.assertIn("A bedroom is not \"a bedroom\"", PROMPT)

    def test_one_frame_can_hold_several_moments(self):
        self.assertIn("ONE FRAME, MORE THAN ONE MOMENT", PROMPT)
        self.assertIn("only \"awake | asleep\" is a caption with a border", PROMPT)

    def test_the_camera_rotates(self):
        self.assertIn("ROTATE THE CAMERA", PROMPT)
        for shot in ("extreme close-up of the eyes", "overhead shot", "side profile",
                     "silhouette"):
            self.assertIn(shot, PROMPT)


class PaletteTests(unittest.TestCase):
    def test_colour_comes_from_the_place(self):
        self.assertIn("IT COMES FROM THE PLACE, NOT THE\n   MOOD", PROMPT)

    def test_the_measured_failure_is_named(self):
        self.assertIn("dark blue, dark blue, dark blue, dark blue", PROMPT)

    def test_a_hard_ceiling_on_repeats(self):
        self.assertIn("never for more than three\n   consecutive frames", PROMPT)


class StyleTests(unittest.TestCase):
    def test_the_anchor_names_one_drawing_language_for_the_whole_frame(self):
        self.assertIn("Flat solid-color hand-drawn webcomic illustration", PROMPT)
        self.assertIn("including props and background shapes", PROMPT)

    def test_gradients_are_refused_in_the_anchor_not_only_the_lock(self):
        """The lock already said no gradients and they arrived anyway - the model read the anchor
        as the style and the lock as boilerplate."""
        anchor = PROMPT[PROMPT.index("2. Every prompt must open"):][:600]
        self.assertIn("no gradients", anchor)
        self.assertIn("no realistic lighting", anchor)

    def test_the_examples_use_the_new_anchor(self):
        """An example that contradicts a rule beats the rule."""
        gold = PROMPT[PROMPT.index("GOLD-STANDARD EXAMPLE - ORDINARY SCENE"):]
        gold = gold[:gold.index("COUNTER-EXAMPLE")]
        self.assertIn("Flat solid-color hand-drawn webcomic", gold)
        self.assertNotIn("Simple MS Paint style illustration,", gold)

    def test_the_counter_example_keeps_the_old_look_on_purpose(self):
        counter = PROMPT[PROMPT.index("COUNTER-EXAMPLE"):][:500]
        self.assertIn("Simple MS Paint style illustration", counter)
        self.assertIn("large red X", counter)

    def test_the_gold_scene_actually_has_layers(self):
        gold = PROMPT[PROMPT.index("GOLD-STANDARD EXAMPLE - ORDINARY SCENE"):]
        gold = gold[:gold.index("COUNTER-EXAMPLE")]
        self.assertIn("medium shot", gold)
        self.assertIn("behind them", gold)
        self.assertIn("in front of them", gold)


class ShortFormTests(unittest.TestCase):
    """A 60s short overrides the complexity rules - but not the requirement to be somewhere."""

    def test_the_short_still_needs_a_place(self):
        self.assertIn("Fewer ACTIONS, not no\nPLACE", lv.SHORT_FORM_ADDENDUM)

    def test_it_says_which_rules_it_overrides(self):
        self.assertIn("overrides rule 9c", lv.SHORT_FORM_ADDENDUM)
        self.assertIn("does NOT override rule 9a", lv.SHORT_FORM_ADDENDUM)

    def test_the_vertical_translation_keeps_every_new_rule(self):
        self.addCleanup(lv.set_project_aspect, "16:9")
        lv.set_project_aspect("9:16")
        sent = lv.fit_aspect(lv.STAGE3_PROMPT)
        for rule in ("VISUALISE THE SITUATION", "THREE LAYERS", "ROTATE THE CAMERA",
                     "ONE FRAME, MORE THAN ONE MOMENT"):
            self.assertIn(rule, sent)
        self.assertNotIn("16:9", sent)


if __name__ == "__main__":
    unittest.main()
