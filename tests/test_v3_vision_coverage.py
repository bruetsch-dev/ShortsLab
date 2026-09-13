"""The vision pass must see enough of a source to judge it.

`unrelated_filler` was the largest rejection bucket of the delivered run
`japan_has_hotels_where_you_can_stay` - 32 of 61 rejections. Eight of those sources were pulled
off disk and inspected by hand for the two capsule-hotel chapters. At least three plainly showed
capsule hotels: an airport pod with the caption 空港内にある最新型カプセル, a hotel corridor with a
pod interior, and a clip captioned "Can this overweight American fit in a Japanese capsule
hotel". Two were correctly rejected (a classroom, a ramen vlog).

The gate was not being too strict - it was being shown too little. V3 hands the whole source to
the vision model and asks whether any usable moment exists in it, but sampled it with V2's four
frames, which for a 25-second TikTok is one frame every six seconds.
"""

import inspect
import unittest

import scrape_v2
import scrape_v3


class SamplingTests(unittest.TestCase):
    def test_v3_asks_for_more_frames_than_the_v2_default(self):
        default = inspect.signature(scrape_v2._segment_strip).parameters["frames_n"].default
        self.assertGreater(scrape_v3.V3_CONFIG["vision_frames_per_source"], default)

    def test_the_v2_default_is_unchanged(self):
        """V2 inspects an already-chosen segment; widening it there buys nothing and costs
        vision tokens on every candidate."""
        self.assertEqual(
            inspect.signature(scrape_v2._segment_strip).parameters["frames_n"].default, 4)

    def test_v3_passes_its_own_setting(self):
        source = open(scrape_v3.__file__, encoding="utf-8").read()
        self.assertIn('frames_n=V3_CONFIG["vision_frames_per_source"]', source)

    def test_the_count_stays_affordable(self):
        """Every frame is another tile in the strip the vision model reads."""
        frames = scrape_v3.V3_CONFIG["vision_frames_per_source"]
        self.assertGreaterEqual(frames, 6)
        self.assertLessEqual(frames, 12)

    def test_a_nonsense_count_cannot_produce_an_empty_strip(self):
        signature = inspect.getsource(scrape_v2._segment_strip)
        self.assertIn("max(2, int(frames_n or 4))", signature)


class CoverageMathTests(unittest.TestCase):
    """What the change buys, stated as the number it is chosen for."""

    def test_a_three_second_moment_in_a_typical_clip_is_far_more_likely_to_be_seen(self):
        # median rejected source length in the measured run
        clip = 25.3
        moment = 3.0
        def chance(frames):
            # frames evenly spaced: the moment is seen when any sample lands inside it
            return min(1.0, frames * moment / clip)
        before = chance(4)
        after = chance(scrape_v3.V3_CONFIG["vision_frames_per_source"])
        self.assertGreater(after, before)
        self.assertGreaterEqual(after / before, 1.5)


if __name__ == "__main__":
    unittest.main()


class GradePromptTests(unittest.TestCase):
    """The gate must judge 'is there a usable moment', not 'is this video on topic'."""

    def test_one_usable_moment_is_enough(self):
        self.assertIn("ONE GOOD MOMENT IS ENOUGH", scrape_v3.GRADE_PROMPT)

    def test_unrelated_filler_is_defined_as_nothing_at_all(self):
        self.assertIn("unrelated_filler means NO part of the strip", scrape_v3.GRADE_PROMPT)

    def test_the_prompt_says_the_strip_is_a_sample(self):
        """The model must know it is seeing frames FROM a longer clip, not the whole clip."""
        self.assertIn("frames sampled across ONE source video", scrape_v3.GRADE_PROMPT)

    def test_it_still_refuses_the_things_it_should(self):
        for guard in ("talking_head", "duet_or_stitch", "screen_recording",
                      "OBJECT IDENTITY IS NON-NEGOTIABLE", "SUBJECT-IDENTITY RULE"):
            self.assertIn(guard, scrape_v3.GRADE_PROMPT, guard)
