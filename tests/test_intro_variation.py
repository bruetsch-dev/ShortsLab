"""Two uploads must not begin with byte-identical footage.

Measured 2026-08-30 across ten delivered sketch shorts: `hook_intro.mp4` was 3.993056s in every
single one, cut from the one source clip at assets/hook_intros/google_search.mp4, with only the
typed query differing. The opening four seconds of every upload were the same frames.

The treatment is deliberately invisible - a slightly different crop window, grade and length -
so the intro still looks like itself while no two files share their pixels or their duration.
Seeded by the project name, so re-rendering one short reproduces its own intro exactly.
"""

import unittest

import sketch_hook_intro as shi


SLUGS = ["when_you_stand_up_quickly_gravity", "you_re_just_sitting_there_doing",
         "why_do_your_fingers_turn_wrinkly", "try_breathing_through_your_nose",
         "that_noise_is_not_your_stomach", "i_m_just_sitting_there_doing"]


class SeedTests(unittest.TestCase):
    def test_the_same_project_always_gets_the_same_intro(self):
        """A re-render must not quietly produce a different opening."""
        self.assertEqual(shi.intro_variation("a_project"), shi.intro_variation("a_project"))

    def test_different_projects_get_different_intros(self):
        seen = {tuple(sorted(shi.intro_variation(s).items())) for s in SLUGS}
        self.assertEqual(len(seen), len(SLUGS), "two projects share one variation")

    def test_each_parameter_varies_independently(self):
        """Deriving everything from one bucket looked varied and was not: with a dozen
        buckets, two of four real project names already collided exactly."""
        crops = {shi.intro_variation(s)["crop"] for s in SLUGS}
        hues = {shi.intro_variation(s)["hue"] for s in SLUGS}
        self.assertGreater(len(crops), 1)
        self.assertGreater(len(hues), 1)

    def test_the_treatment_stays_subtle(self):
        """It must not read as a filter - the intro is branding."""
        for slug in SLUGS:
            v = shi.intro_variation(slug)
            self.assertGreaterEqual(v["crop"], 0.97, slug)
            self.assertLessEqual(v["crop"], 1.0, slug)
            self.assertLess(abs(v["hue"]), 5.0, slug)
            self.assertGreater(v["saturation"], 0.9, slug)
            self.assertLess(v["saturation"], 1.1, slug)

    def test_the_length_varies_too(self):
        """All ten delivered intros were 3.993056s to the microsecond."""
        trims = {shi.intro_variation(s)["trim_frames"] for s in SLUGS}
        self.assertGreater(len(trims), 1)


class FilterTests(unittest.TestCase):
    def test_the_crop_produces_even_dimensions(self):
        """libx264 refuses odd sizes. `iw/2*2` is NOT a rounding idiom - ffmpeg expression
        arithmetic is floating point, so it returned iw unchanged and the encode died with
        'width not divisible by 2 (713x1268)'."""
        for slug in SLUGS:
            graph = shi.variation_filter(shi.intro_variation(slug))
            if "crop=" in graph:
                self.assertIn("trunc(", graph, slug)
                self.assertNotIn("iw/2*2", graph, slug)

    def test_no_variation_means_no_filter(self):
        self.assertEqual(shi.variation_filter(None), "")

    def test_the_grade_is_always_applied(self):
        graph = shi.variation_filter(shi.intro_variation("x"))
        self.assertIn("eq=", graph)


class WiringTests(unittest.TestCase):
    def test_the_builder_accepts_a_seed(self):
        import inspect
        self.assertIn("variation_seed",
                      inspect.signature(shi.build_hook_intro).parameters)

    def test_the_pipeline_passes_the_project_name(self):
        import longform_video as lf
        source = open(lf.__file__, encoding="utf-8").read()
        block = source[source.index("sketch_hook_intro.build_hook_intro("):]
        block = block[:block.index("joined =")]
        self.assertIn("variation_seed=project_dir.name", block)


if __name__ == "__main__":
    unittest.main()
