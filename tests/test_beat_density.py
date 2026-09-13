"""Pictures may change more often than subtitles do.

Measured against the user's reference explainer (ink rxplainerr.mp4, 672s): 244 cuts, one image
every 2.8s, 21.8 images per minute. Their own longforms ran 13.3-18.0 per minute.

The image count was not set by `split_script_lines` - the longform path never calls it. It came
from `subtitle_cues`: one cue became one beat became one picture. And those cue limits were
measured off a HAND-CORRECTED srt (70 chars / 11 words / 4.0s), so raising the picture count
through them would have rewritten the subtitles as a side effect.

Now a long cue carries more than one picture, split on its own word timings, and the .srt is
written from the untouched cues. Measured on four real projects: 21.0 per minute at 2.85s.
"""

import unittest

import longform_video as lf


def cue(start, end, text):
    words = text.split()
    step = (end - start) / max(1, len(words))
    return {"start": start, "end": end, "text": text,
            "words": [{"w": w, "s": start + i * step, "e": start + (i + 1) * step}
                      for i, w in enumerate(words)]}


class SplitTests(unittest.TestCase):
    def test_a_short_cue_is_left_alone(self):
        c = cue(0.0, 2.0, "one two three four five")
        self.assertEqual(lf.split_cue_into_beats(c), [
            {"start": 0.0, "end": 2.0, "text": c["text"], "words": c["words"]}])

    def test_a_long_cue_becomes_two_pictures(self):
        c = cue(0.0, 5.8, "the first half of the sentence, and then the second half of it too")
        self.assertEqual(len(lf.split_cue_into_beats(c)), 2)

    def test_the_pieces_tile_the_cue_exactly(self):
        """No gap and no overlap - the .srt and the pictures share every timestamp."""
        c = cue(10.0, 15.8, "the first half of the sentence, and then the second half of it too")
        parts = lf.split_cue_into_beats(c)
        self.assertAlmostEqual(parts[0]["start"], 10.0, places=3)
        self.assertAlmostEqual(parts[-1]["end"], 15.8, places=3)
        for a, b in zip(parts, parts[1:]):
            self.assertAlmostEqual(a["end"], b["start"], places=3)

    def test_it_prefers_a_clause_boundary(self):
        c = cue(0.0, 5.8, "the first half of the sentence, and then the second half of it too")
        parts = lf.split_cue_into_beats(c)
        self.assertTrue(parts[0]["text"].rstrip().endswith(","), parts[0]["text"])

    def test_no_words_are_lost_or_duplicated(self):
        c = cue(0.0, 8.7, "a b c d e f g h i j k l m n o p q r s t u")
        parts = lf.split_cue_into_beats(c)
        self.assertEqual(" ".join(p["text"] for p in parts).split(), c["text"].split())

    def test_no_piece_is_a_flicker(self):
        """A split that lands awkwardly must fold into its neighbour, not flash by."""
        c = cue(0.0, 5.8, "the first half of the sentence, and then the second half of it too")
        for p in lf.split_cue_into_beats(c):
            self.assertGreaterEqual(p["end"] - p["start"], lf.BEAT_MIN_SECONDS - 0.01)

    def test_too_few_words_are_never_split(self):
        """Six seconds of three words is a slow line, not two pictures."""
        c = cue(0.0, 6.0, "one two three")
        self.assertEqual(len(lf.split_cue_into_beats(c)), 1)

    def test_it_never_returns_nothing(self):
        self.assertEqual(len(lf.split_cue_into_beats({"start": 0, "end": 0, "text": "", "words": []})), 1)


class WiringTests(unittest.TestCase):
    def test_the_transcript_splits_after_the_srt_is_written(self):
        """The subtitles must come from the untouched cues."""
        source = open(lf.__file__, encoding="utf-8").read()
        body = source[source.index("def transcribe_lines("):]
        body = body[:body.index("\ndef ", 10)]
        self.assertLess(body.index("write_srt(cues"), body.index("split_cue_into_beats(cue)"))

    def test_the_subtitle_limits_are_untouched(self):
        """Measured off the user's hand-corrected srt; they are not the lever."""
        import subtitles
        self.assertEqual(subtitles.SRT_MAX_WORDS, 11)
        self.assertEqual(subtitles.SRT_MAX_SECONDS, 4.0)
        self.assertEqual(subtitles.SRT_MAX_CHARS, 70)

    def test_the_target_matches_the_reference(self):
        self.assertAlmostEqual(lf.BEAT_TARGET_SECONDS, 2.9, places=1)


if __name__ == "__main__":
    unittest.main()
