"""One cut per subtitle timestamp, and the surplus pictures thrown out.

Asked for on 2026-08-29: "du sollst die ueberschuessigen bilder rausschmeissen, da waren
zuvioele cuts, ein cut pro srt timeangabe".

Measured on the delivered `_srtcut_v2.mp4` first, because the counts already looked right and
were not the problem: 541 pictures against 547 cues, 541 cuts - but only 305 of them (56%) sat
on a cue boundary. The other 236 fell mid-phrase, up to 1.93s from one, so the film cut inside
a sentence AND again at its end. That is what reads as "too many cuts".

`snap_frames_to_cue_starts` keeps one segment per PICTURE and only pulls a cut onto a boundary
within SNAP_WINDOW_S. `fit_pictures_to_cues` lets the CUES drive instead: 547 cues collapse to
481 segments, all on a cue start, and the 60 pictures no cue asked for are dropped.
"""

import json
import unittest

import longform_video as lf
import subtitles


def cues(*spans):
    return [{"start": s, "end": e, "text": t} for s, e, t in spans]


def lines(*texts):
    return [{"start": float(i), "end": float(i + 1), "text": t,
             "words": [{"w": w} for w in t.split()]} for i, t in enumerate(texts)]


class FitTests(unittest.TestCase):
    def test_the_edit_starts_at_zero_even_when_the_first_cue_does_not(self):
        """The assembly plays durations back to back from t=0. Measured: leaving the leading
        silence out shifted every later cut earlier by exactly it - 0/480 cuts on a boundary,
        all 0.298s early."""
        starts, durations, _ = lf.fit_pictures_to_cues(
            lines("hello there", "second line"),
            cues((0.298, 2.0, "hello there"), (2.0, 4.0, "second line")), 4.0)
        self.assertEqual(starts[0], 0.0)
        self.assertAlmostEqual(durations[0], 2.0, places=3)

    def test_every_boundary_is_a_cue_start(self):
        c = cues((0.5, 2.0, "one"), (2.0, 3.5, "two"), (3.5, 5.0, "three"))
        starts, durations, _ = lf.fit_pictures_to_cues(lines("one", "two", "three"), c, 5.0)
        clock, boundaries = 0.0, []
        for d in durations[:-1]:
            clock += d
            boundaries.append(round(clock, 3))
        for boundary in boundaries:
            self.assertTrue(any(abs(boundary - x["start"]) < 0.005 for x in c),
                            f"{boundary} is not a cue start")

    def test_the_total_matches_the_audio(self):
        """A short edit would end on a frozen frame; a long one would run past the voice."""
        c = cues((0.3, 2.0, "one"), (2.0, 4.0, "two"))
        _, durations, _ = lf.fit_pictures_to_cues(lines("one", "two"), c, 4.0)
        self.assertAlmostEqual(sum(durations), 4.0, places=2)

    def test_consecutive_cues_on_one_picture_become_one_segment(self):
        """Otherwise the film gains a cut with no new picture behind it."""
        c = cues((0.0, 1.0, "one two"), (1.0, 2.0, "three four"))
        starts, _, pictures = lf.fit_pictures_to_cues(lines("one two three four"), c, 2.0)
        self.assertEqual(len(starts), 1)
        self.assertEqual(pictures, [0])

    def test_empty_input_is_not_a_crash(self):
        self.assertEqual(lf.fit_pictures_to_cues([], [], 0.0), ([], [], []))


class StrictPathTests(unittest.TestCase):
    def test_the_strict_recut_exists_and_is_reachable(self):
        self.assertTrue(hasattr(lf, "_recut_strict"))
        source = open(lf.__file__, encoding="utf-8").read()
        body = source[source.index("def recut_to_subtitles("):]
        body = body[:body.index("\ndef ", 10)]
        self.assertIn("if strict:", body)
        self.assertIn("_recut_strict(", body)

    def test_it_reports_what_it_dropped(self):
        """Silently deleting pictures is how a shorter film looks like a bug."""
        body = open(lf.__file__, encoding="utf-8").read()
        body = body[body.index("def _recut_strict("):]
        body = body[:body.index("\ndef ", 10)]
        self.assertIn("surplus picture(s) dropped", body)

    def test_it_writes_its_own_file_name(self):
        """The previous srtcut render must survive for comparison."""
        body = open(lf.__file__, encoding="utf-8").read()
        body = body[body.index("def _recut_strict("):]
        body = body[:body.index("\ndef ", 10)]
        self.assertIn("_cuecut.mp4", body)


if __name__ == "__main__":
    unittest.main()
