"""Every short gets its own opener, generated from its own search line.

The shipped opener is one stock clip reused in every short - measured at 3.993056s in all ten
delivered projects, the same frames every time. Instead of disguising that, the run hands the
user a prompt at the START, so the clip can be generated while the rest of the pipeline works.

The prompt is written for a director-style video model: labelled sections, one continuous shot,
and deliberately NO model name, duration or aspect ratio in the text - a model that reads those
in the prompt tends to render them.
"""

import unittest

import sketch_hook_intro as shi


QUERY = "why does my vision go dark when i stand up too fast"


class ContentTests(unittest.TestCase):
    def prompt(self, query=QUERY, seed="a_project"):
        return shi.hook_clip_prompt(query, seed=seed)

    def test_the_search_line_is_in_the_prompt_verbatim(self):
        """The whole point: the screen shows THIS short's question."""
        self.assertIn(QUERY, self.prompt())

    def test_it_names_the_sections_a_director_prompt_uses(self):
        text = self.prompt()
        for label in ("Setting:", "Subject:", "Action:", "Camera:", "Lighting:", "Style:",
                      "Audio:"):
            self.assertIn(label, text, label)

    def test_it_never_states_a_duration_or_aspect_or_model(self):
        """Those belong in the generation controls; in the text they get rendered."""
        text = self.prompt().lower()
        for banned in ("9:16", "16:9", "seconds", "aspect", "1080", "seedance", "veo", "sora"):
            self.assertNotIn(banned, text, banned)

    def test_it_asks_for_one_continuous_shot(self):
        """A cut inside the opener would fight the cut into the short."""
        text = self.prompt().lower()
        self.assertNotIn("shot 2", text)
        self.assertNotIn("cut to", text)

    def test_it_forbids_burned_in_text(self):
        """Anything the model writes on screen would collide with the short's own captions."""
        text = self.prompt().lower()
        self.assertIn("no text overlays", text)
        self.assertIn("no captions", text)

    def test_a_missing_query_is_not_a_crash(self):
        self.assertIsInstance(shi.hook_clip_prompt("", seed="x"), str)
        self.assertIsInstance(shi.hook_clip_prompt(None, seed=None), str)

    def test_whitespace_in_the_query_is_normalised(self):
        self.assertIn("why does my eye twitch",
                      shi.hook_clip_prompt("  why   does my\neye twitch ", seed="x"))


class AestheticTests(unittest.TestCase):
    """The look is measured off assets/hook_intros/google_search.mp4, not invented.

    The first version said "a tidy home desk" and rotated through oak desks and grey mats, and
    read as exactly what it was - generic. The reference clip is a pink plush mat, pastel
    keycaps with a mint spacebar and a brass knob, a warm LED strip under a wooden riser, a
    magenta-to-peach wash on the wall, a moon lamp and tulips, all under a soft haze.
    """

    SEEDS = ["a", "b", "c", "d", "e", "f", "g", "h"]

    def prompts(self):
        return [shi.hook_clip_prompt(QUERY, seed=s) for s in self.SEEDS]

    def test_every_variant_is_pastel(self):
        for text in self.prompts():
            self.assertIn("pastel", text.lower())

    def test_every_variant_keeps_the_plush_mat_and_the_keyboard(self):
        for text in self.prompts():
            low = text.lower()
            self.assertTrue("plush" in low or "velvet" in low or "fluffy" in low, low[:120])
            self.assertIn("mechanical keyboard", low)

    def test_every_variant_has_the_soft_background_glow(self):
        """"im hintergrund sieht man noch smoothes zeug" - the gradient wash and the bloom."""
        for text in self.prompts():
            low = text.lower()
            # The concept, not one word: every variant washes colour across the wall behind the
            # monitor, whether it calls that a gradient, a glow or a wash.
            self.assertIn("wall", low)
            self.assertTrue(any(w in low for w in ("gradient", "glow", "wash")), low[:200])
            self.assertTrue("bloom" in low or "bokeh" in low, low[-300:])
            # The strip light under the riser, however each variant words it.
            self.assertIn("strip", low, "the light under the riser is part of the look")
            self.assertTrue(any(w in low for w in ("riser", "under-shelf", "under the riser")),
                            low[:200])

    def test_no_variant_wanders_off_the_look(self):
        """These are what made it read as a stock desk instead of this desk."""
        for text in self.prompts():
            low = text.lower()
            for stray in ("oak desk", "grey desk", "matte black stand", "birch desk"):
                self.assertNotIn(stray, low, stray)


class VariationTests(unittest.TestCase):
    SEEDS = ["when_you_stand_up_quickly_gravity", "you_re_just_sitting_there_doing",
             "why_do_your_fingers_turn_wrinkly", "that_noise_is_not_your_stomach"]

    def test_the_same_short_always_gets_the_same_prompt(self):
        self.assertEqual(shi.hook_clip_prompt(QUERY, seed="s"),
                         shi.hook_clip_prompt(QUERY, seed="s"))

    def test_different_shorts_get_different_setups(self):
        seen = {shi.hook_clip_prompt(QUERY, seed=s) for s in self.SEEDS}
        self.assertGreater(len(seen), 1, "every short would generate the same opener again")


class WiringTests(unittest.TestCase):
    def test_the_run_produces_it_before_the_work_starts(self):
        import longform_video as lf
        source = open(lf.__file__, encoding="utf-8").read()
        body = source[source.index("def run_longform_video("):]
        self.assertIn("hook_clip_prompt(", body)
        # ...and before the voiceover, which is the first expensive step
        self.assertLess(body.index("hook_clip_prompt("), body.index("generate_voiceover("))

    def test_it_is_saved_beside_the_project(self):
        """A job log scrolls away; the user comes back to this at the editing step."""
        import longform_video as lf
        source = open(lf.__file__, encoding="utf-8").read()
        self.assertIn('"opener_prompt.txt"', source)


if __name__ == "__main__":
    unittest.main()
