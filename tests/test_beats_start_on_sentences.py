"""A visual beat must not start in the middle of a sentence.

`enforce_reference_pacing` splits a long beat into EQUAL parts, and an equal part lands wherever
the arithmetic puts it. On the couples Short (2026-09-05) that produced a beat whose spoken text
was "for some couples. Matching" - the tail of one sentence plus the head of the next. The clip is
then chosen for the second half ("Matching accessories") and put on screen at the first half's
start, so the picture arrives about a second before the word.

The same mechanism, one beat later, is what the owner actually watched: the fancy-dinner shot came
up at 12.97s while the word "dinner" is spoken at 15.64s - **2.7 seconds early**.

So `sync_scenes_to_voice_timeline`, which already makes the audio authoritative, now pulls every
internal beat boundary to the nearest sentence start within 0.9s. Nothing moves when there is no
sentence break nearby, and a boundary is never pulled so far that it swallows a neighbour.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent_core


def _words(pairs):
    """(word, start, end) -> the word-timeline shape the pipeline uses."""
    return [{"word": w, "start": s, "end": e} for w, s, e in pairs]


# "...feel awkward for some couples. Matching accessories, phone cases..."
TIMELINE = _words([
    ("feel", 5.0, 5.3), ("awkward", 5.3, 5.7), ("for", 5.7, 5.9), ("some", 5.9, 6.2),
    ("couples.", 6.2, 6.9), ("Matching", 7.0, 7.5), ("accessories,", 7.5, 8.1),
    ("phone", 8.1, 8.4), ("cases", 8.4, 8.9),
])


class TheBoundaryMovesToTheSentence(unittest.TestCase):
    def test_a_beat_that_starts_mid_sentence_is_pulled_to_the_next_sentence(self):
        scenes = [{"start": 5.0, "end": 6.3}, {"start": 6.3, "end": 8.9}]
        out = agent_core.sync_scenes_to_voice_timeline(scenes, TIMELINE, target_duration=8.9)
        self.assertAlmostEqual(out[1]["start"], 7.0, places=2)
        self.assertAlmostEqual(out[0]["end"], 7.0, places=2)

    def test_the_words_of_each_beat_follow_the_new_boundary(self):
        scenes = [{"start": 5.0, "end": 6.3}, {"start": 6.3, "end": 8.9}]
        out = agent_core.sync_scenes_to_voice_timeline(scenes, TIMELINE, target_duration=8.9)
        self.assertIn("couples", out[0]["exact_voice_text"])
        self.assertTrue(out[1]["exact_voice_text"].startswith("Matching"),
                        out[1]["exact_voice_text"])
        self.assertNotIn("couples", out[1]["exact_voice_text"])

    def test_a_boundary_far_from_any_sentence_end_is_left_alone(self):
        """Only a break within 0.9s is a break worth honouring."""
        scenes = [{"start": 5.0, "end": 5.4}, {"start": 5.4, "end": 8.9}]
        out = agent_core.sync_scenes_to_voice_timeline(scenes, TIMELINE, target_duration=8.9)
        self.assertAlmostEqual(out[1]["start"], 5.4, places=2)

    def test_a_beat_is_never_swallowed_by_the_move(self):
        """The pull must not push a boundary past its own neighbour's edges."""
        scenes = [{"start": 6.8, "end": 7.1}, {"start": 7.1, "end": 7.3}]
        out = agent_core.sync_scenes_to_voice_timeline(scenes, TIMELINE, target_duration=8.9)
        self.assertGreater(out[1]["start"], out[0]["start"])
        self.assertGreater(out[1]["end"], out[1]["start"])

    def test_it_survives_a_timeline_with_no_sentence_ends(self):
        flat = _words([("one", 0.0, 0.5), ("two", 0.5, 1.0), ("three", 1.0, 1.5)])
        scenes = [{"start": 0.0, "end": 0.7}, {"start": 0.7, "end": 1.5}]
        out = agent_core.sync_scenes_to_voice_timeline(scenes, flat, target_duration=1.5)
        self.assertAlmostEqual(out[1]["start"], 0.7, places=2)


if __name__ == "__main__":
    unittest.main()
