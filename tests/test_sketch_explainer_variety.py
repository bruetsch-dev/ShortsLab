import unittest

import longform_video as lf


class SketchExplainerVarietyTests(unittest.TestCase):
    """Ten frames of a finished explainer showed the same figure in the same bed at the same
    angle on the same dark blue for six of them. Three explicit rules had caused it: the style
    lock pinned a flat single-colour background, an absolute no-text ban made labelled diagrams
    impossible, and a hold-the-scene rule was read as hold-the-picture."""

    def test_the_background_colour_is_required_to_change(self):
        """Consistency belongs on the thumbnail, not inside twenty minutes of film."""
        self.assertIn("BACKGROUND COLOUR MUST CHANGE", lf.STAGE3_PROMPT)
        self.assertIn("ninety seconds", lf.STAGE3_PROMPT)

    def test_scenes_must_have_a_place_not_just_a_colour(self):
        """Now a quota and three named layers, not just "put it somewhere". A side-by-side with
        a reference channel showed the gap was art direction: our frames were a figure plus a
        symbol on a colour field, theirs were characters inside a world."""
        self.assertIn("PUT THE SCENE SOMEWHERE", lf.STAGE3_PROMPT)
        self.assertIn("At least three quarters of the frames", lf.STAGE3_PROMPT)
        for prop in ("window", "nightstand", "lamp", "slippers"):
            self.assertIn(prop, lf.STAGE3_PROMPT)
        for layer in ("BACKGROUND:", "MIDGROUND:", "FOREGROUND:"):
            self.assertIn(layer, lf.STAGE3_PROMPT)

    def test_labelled_diagrams_are_required_and_possible(self):
        """The no-text ban forbade exactly the labels these frames exist for, so there are two
        style locks now: the wordless one for ordinary scenes, and one that permits short caps."""
        self.assertIn("DIAGRAMS AND PANELS ARE PART OF THE RHYTHM", lf.STAGE3_PROMPT)
        self.assertIn("short bold capital labels only", lf.STAGE3_PROMPT)
        for frame in ("Labelled cycle", "Labelled before/after", "Four-panel grid",
                      "Labelled anatomy"):
            self.assertIn(frame, lf.STAGE3_PROMPT)

    def test_ordinary_frames_stay_wordless(self):
        """Relaxing the ban everywhere would fill the film with garbled model text."""
        self.assertIn("NO TEXT IN ORDINARY SCENES", lf.STAGE3_PROMPT)
        self.assertIn("no captions, no labels, no signage", lf.STAGE3_PROMPT)

    def test_holding_a_moment_does_not_mean_repeating_the_frame(self):
        self.assertIn("Hold the SUBJECT", lf.STAGE3_PROMPT)
        self.assertIn("change the PICTURE", lf.STAGE3_PROMPT)

    def test_both_locks_are_shown_as_worked_examples(self):
        """A rule the model has never seen in example form is a rule it approximates."""
        self.assertIn("GOLD-STANDARD EXAMPLE - ORDINARY SCENE", lf.STAGE3_PROMPT)
        self.assertIn("GOLD-STANDARD EXAMPLE - DIAGRAM FRAME", lf.STAGE3_PROMPT)


if __name__ == "__main__":
    unittest.main()
