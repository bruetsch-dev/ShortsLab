"""How much narration fits in ONE Seed TTS call - measured, not assumed.

The model declares no maxLength for `text`, so the ceiling is only discoverable by trying it.
Against the live endpoint on 2026-08-27:

    2200 chars -> 2.22 min, completed          (the old limit)
    4500 chars -> 5.21 min, completed in 34s
    5200 chars -> failed prediction
    5600 chars -> failed prediction
    6000 chars -> failed prediction
    9000 chars -> failed prediction

4400 was tried end to end - a 4323-char part produced 4.36 minutes with the script's final words
present in the transcript, so the audio was not truncated - and then deliberately reverted. A part
is also the unit that gets REGENERATED when a chunk returns silent, and the unit a reviewer
approves or declines in "halt after speech"; five-minute parts make both far more expensive than
the saved API calls are worth. 2600 keeps a part between two and three minutes.
"""

import unittest

import longform_video as lv


# Measured speaking rate, from the two completed calls: 2200 chars -> 133s and 4500 -> 313s,
# i.e. 16.5 and 14.4 characters per second. The slower figure is the safe one for a ceiling.
CHARS_PER_SECOND = 14.4


class LimitTests(unittest.TestCase):
    def test_a_part_lands_between_two_and_three_minutes(self):
        """The chosen size. A part is also the unit that gets regenerated when a chunk comes back
        silent, and the unit a reviewer approves in "halt after speech" - five-minute parts make
        both far more expensive than the saved API calls are worth."""
        minutes = lv.TTS_PART_CHAR_LIMIT / CHARS_PER_SECOND / 60
        self.assertGreater(minutes, 2.0, f"{minutes:.1f} min")
        self.assertLess(minutes, 3.2, f"{minutes:.1f} min")

    def test_it_stays_well_under_the_first_size_that_failed(self):
        """5200 was the smallest failure seen; anything near it is a coin flip."""
        self.assertLess(lv.TTS_PART_CHAR_LIMIT, 5200)

    def test_the_measurements_are_written_down_where_the_number_lives(self):
        source = open(lv.__file__, encoding="utf-8").read()
        block = source[source.index("# Sentence-safe chunking limit"):
                       source.index("TTS_PART_CHAR_LIMIT = 2600")]
        # Normalised: the comment wraps, so a phrase can straddle a line break.
        flat = " ".join(block.replace("#", " ").split())
        for evidence in ("4500", "5200", "declares no maxLength"):
            self.assertIn(evidence, flat)


class SplitterTests(unittest.TestCase):
    SCRIPT = ("The nasal cycle is a slow rhythm your body runs without asking you. "
              "One nostril opens while the other narrows, and hours later they trade "
              "places.\n\n") * 60

    def test_no_part_exceeds_the_limit(self):
        for part in lv.split_script_for_tts(self.SCRIPT):
            self.assertLessEqual(len(part), lv.TTS_PART_CHAR_LIMIT)

    def test_a_long_script_needs_fewer_calls_than_before(self):
        old = lv.split_script_for_tts(self.SCRIPT, limit=2200)
        new = lv.split_script_for_tts(self.SCRIPT)
        self.assertLessEqual(len(new), len(old), f"{len(new)} vs {len(old)}")

    def test_sentences_are_never_cut_in_half(self):
        """A part that ends mid-sentence makes the seam audible."""
        for part in lv.split_script_for_tts(self.SCRIPT):
            self.assertTrue(part.rstrip().endswith((".", "!", "?")), part[-60:])

    def test_a_short_script_is_still_a_single_part(self):
        self.assertEqual(len(lv.split_script_for_tts("One sentence only.")), 1)

    def test_an_empty_script_produces_nothing(self):
        self.assertEqual(lv.split_script_for_tts(""), [])
        self.assertEqual(lv.split_script_for_tts(None), [])

    def test_one_enormous_paragraph_is_still_broken_up(self):
        """Paragraph-first splitting must fall back to sentences, or a wall of text becomes one
        oversized part and the call fails."""
        wall = "This is a sentence that keeps going and going. " * 200
        parts = lv.split_script_for_tts(wall)
        self.assertGreater(len(parts), 1)
        for part in parts:
            self.assertLessEqual(len(part), lv.TTS_PART_CHAR_LIMIT)


if __name__ == "__main__":
    unittest.main()
