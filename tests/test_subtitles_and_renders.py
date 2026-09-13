"""Subtitles are the edit clock, and every finished render lands in one folder.

Reported: a nineteen-minute explainer was "komplett kacke geschnitten". The pictures changed on
a word-count rule applied to the script, so a cut could land in the middle of a phrase.

For NEW runs the subtitle cues are the beat clock - one picture per cue - so a cut can only
happen where a caption changes. For a project whose pictures already exist the cues supply the
cut TIMES only, snapping a cut onto a caption edge when one is within 0.6s; giving every
existing picture its own cue boundary was built, measured and rejected (see
SnapToCaptionBoundaryTests).

The cue limits are measured off the user's own hand-corrected file for that video: 547 cues,
<=2 display lines of <=44 characters, <=70 characters and <=11 words per cue, 0.41-3.92s long.
"""

import json
import tempfile
import unittest
from pathlib import Path

import longform_video as lv
import renders
import subtitles


def words(*pairs):
    """[(text, start, end), ...] -> the shape voice_align hands over."""
    return [{"word": w, "start": s, "end": e} for w, s, e in pairs]


SENTENCE = words(
    ("Why", 0.30, 0.55), ("you", 0.55, 0.75), ("can't", 0.75, 1.10),
    ("remember", 1.10, 1.70), ("falling", 1.70, 2.10), ("asleep.", 2.10, 2.39),
    ("Try", 2.94, 3.20), ("to", 3.20, 3.35), ("remember", 3.35, 3.90),
    ("the", 3.90, 4.00), ("exact", 4.00, 4.40), ("moment", 4.40, 4.90),
    ("you", 4.90, 5.05), ("fell", 5.05, 5.35), ("asleep", 5.35, 5.70),
    ("last", 5.70, 5.85), ("night.", 5.85, 5.90),
)


class CueShapeTests(unittest.TestCase):
    def test_a_cue_ends_where_the_sentence_does(self):
        cues = subtitles.subtitle_cues(SENTENCE)
        self.assertEqual(len(cues), 2)
        self.assertEqual(cues[0]["text"], "Why you can't remember falling asleep.")
        self.assertEqual(cues[1]["text"],
                         "Try to remember the exact moment you fell asleep last night.")

    def test_it_lands_on_the_hand_corrected_timings(self):
        """The reference file says 0.298->2.386 and 2.937->5.901; within a frame is right."""
        cues = subtitles.subtitle_cues(SENTENCE)
        self.assertAlmostEqual(cues[0]["start"], 0.30, places=2)
        self.assertAlmostEqual(cues[0]["end"], 2.39, places=2)
        self.assertAlmostEqual(cues[1]["start"], 2.94, places=2)

    def test_no_cue_outgrows_the_reading_limits(self):
        long_run = words(*[(f"word{i}", i * 0.4, i * 0.4 + 0.35) for i in range(60)])
        for cue in subtitles.subtitle_cues(long_run):
            self.assertLessEqual(len(cue["text"]), subtitles.SRT_MAX_CHARS)
            self.assertLessEqual(len(cue["text"].split()), subtitles.SRT_MAX_WORDS)
            self.assertLessEqual(cue["end"] - cue["start"], subtitles.SRT_MAX_SECONDS + 0.01)

    def test_a_long_pause_ends_a_cue(self):
        broken = words(("One", 0.0, 0.4), ("two", 0.4, 0.8), ("three", 3.0, 3.4))
        self.assertEqual(len(subtitles.subtitle_cues(broken)), 2)

    def test_cues_never_overlap(self):
        cues = subtitles.subtitle_cues(SENTENCE)
        for a, b in zip(cues, cues[1:]):
            self.assertLessEqual(a["end"], b["start"])

    def test_display_lines_stay_readable(self):
        lines = subtitles.wrap_subtitle(
            "Try to remember the exact moment you fell asleep last night.")
        self.assertLessEqual(len(lines), 2)
        for line in lines:
            self.assertLessEqual(len(line), subtitles.SRT_MAX_LINE_CHARS)

    def test_a_short_cue_is_not_wrapped(self):
        self.assertEqual(subtitles.wrap_subtitle("You can't."), ["You can't."])


class SrtFileTests(unittest.TestCase):
    def test_the_file_round_trips(self):
        cues = subtitles.subtitle_cues(SENTENCE)
        with tempfile.TemporaryDirectory() as tmp:
            path = subtitles.write_srt(cues, Path(tmp) / "s.srt")
            back = subtitles.parse_srt(path)
        self.assertEqual([(c["start"], c["end"]) for c in back],
                         [(c["start"], c["end"]) for c in cues])

    def test_the_clock_is_subrips_own_format(self):
        self.assertEqual(subtitles.srt_clock(3723.456), "01:02:03,456")
        self.assertEqual(subtitles.srt_clock(0), "00:00:00,000")

    def test_a_foreign_file_still_parses(self):
        """Hand-edited files use dots, stray blank lines and missing numbers."""
        raw = "1\n00:00:01.000 --> 00:00:02.500\nHello\n\n\n00:00:03,000 --> 00:00:04,000\nWorld\n"
        cues = subtitles.parse_srt(raw)
        self.assertEqual([c["text"] for c in cues], ["Hello", "World"])
        self.assertEqual(cues[0]["start"], 1.0)


class EditClockTests(unittest.TestCase):
    """The pictures drive the timeline; the subtitles only supply the cut TIMES.

    Letting the cues drive it instead was built, rendered and rejected: there are more cues than
    pictures, so pictures were held across two captions while the film gained cuts with no
    picture of their own, and the drawings drifted a measured 3.2s median from the words.
    """

    LINES = [{"start": 0.0, "end": 2.0, "text": "One two three.",
              "words": [{"w": "One", "s": 0.0}, {"w": "two", "s": 0.6}, {"w": "three.", "s": 1.2}]},
             {"start": 2.0, "end": 4.0, "text": "Four five six.",
              "words": [{"w": "Four", "s": 2.0}, {"w": "five", "s": 2.6}, {"w": "six.", "s": 3.2}]}]

    def test_a_cue_takes_the_picture_of_the_beat_holding_its_first_word(self):
        cues = [{"start": 0.0, "end": 1.9, "text": "One two three."},
                {"start": 2.0, "end": 3.9, "text": "Four five six."}]
        self.assertEqual(lv.map_cues_to_frames(self.LINES, cues), [0, 1])

    def test_more_cues_than_pictures_hold_rather_than_run_ahead(self):
        """Measured on the sleep explainer: forcing a distinct picture per cue pulled the
        pictures a median of 10.9s away from the words. A held picture beats the wrong one."""
        cues = [{"start": 0.0, "end": 0.9, "text": "One two"},
                {"start": 1.0, "end": 1.9, "text": "three."},
                {"start": 2.0, "end": 3.9, "text": "Four five six."}]
        self.assertEqual(lv.map_cues_to_frames(self.LINES, cues), [0, 0, 1])

    def test_the_mapping_never_goes_backwards(self):
        cues = [{"start": 0.0, "end": 1.0, "text": "three."},
                {"start": 1.0, "end": 2.0, "text": "One two"},
                {"start": 2.0, "end": 3.0, "text": "six."}]
        owned = lv.map_cues_to_frames(self.LINES, cues)
        self.assertEqual(owned, sorted(owned))

    def test_punctuation_and_case_do_not_break_the_match(self):
        cues = [{"start": 0.0, "end": 1.9, "text": "ONE, TWO -- THREE!"},
                {"start": 2.0, "end": 3.9, "text": "four five six"}]
        self.assertEqual(lv.map_cues_to_frames(self.LINES, cues), [0, 1])

    def test_the_beats_come_from_the_cues(self):
        """transcribe_lines must derive its beats from subtitle cues, not a word-count rule."""
        source = open(lv.__file__, encoding="utf-8").read()
        body = source[source.index("def transcribe_lines("):]
        body = body[:body.index("\ndef ", 10)]
        self.assertIn("subtitle_cues(", body)
        self.assertIn("write_srt(", body)
        self.assertNotIn("split_script_lines(", body)


class SubtitlesFollowTheRenderTests(unittest.TestCase):
    def test_a_render_rewrites_the_srt_from_the_beats_it_is_about_to_cut(self):
        """Writing the file only at transcription would let an edited timeline drift from it."""
        source = open(lv.__file__, encoding="utf-8").read()
        body = source[source.index("def rebuild_from_disk("):]
        body = body[:body.index("\ndef ", 10)]
        self.assertIn("ensure_subtitles(", body)

    def test_ensure_subtitles_writes_the_lines_that_are_on_screen(self):
        lines = [{"start": 0.0, "end": 1.5, "text": "# A title"},
                 {"start": 1.6, "end": 3.0, "text": "Real narration here."}]
        with tempfile.TemporaryDirectory() as tmp:
            path = lv.ensure_subtitles(Path(tmp), lines)
            cues = subtitles.parse_srt(path)
        self.assertEqual(path.name, lv.SUBTITLE_FILE)
        # The heading marker is decoration; a subtitle must not show it.
        self.assertEqual([c["text"] for c in cues], ["A title", "Real narration here."])

    def test_a_clip_short_render_also_writes_one(self):
        source = open(__import__("pipeline").__file__, encoding="utf-8").read()
        body = source[source.index("def render_video("):]
        body = body[:body.index("\ndef ", 10)]
        self.assertIn("write_render_subtitles(", body)

    def test_clip_short_word_times_are_put_on_the_render_clock(self):
        import pipeline
        config = {"scenes": [{"start": 0.0, "end": 2.0,
                              "word_timings": [{"word": "Hello", "start": 0.0, "end": 0.5}]},
                             {"start": 5.0, "end": 7.0,
                              "word_timings": [{"word": "again", "start": 0.25, "end": 0.75}]}]}
        got = pipeline.render_word_timings(config)
        self.assertEqual([(w["word"], w["start"]) for w in got],
                         [("Hello", 0.0), ("again", 5.25)])


class RendersFolderTests(unittest.TestCase):
    def test_it_copies_the_video_and_keeps_the_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "short.mp4"
            source.write_bytes(b"video-bytes")
            renders.RENDERS_DIR = Path(tmp) / ".renders"
            copied = renders.publish(source, project="my_project")
            self.assertTrue(source.is_file(), "the project keeps its own copy")
            self.assertEqual(copied.read_bytes(), b"video-bytes")
            # One folder per project, holding everything that project produced - flat, the
            # language tracks of a dozen projects were an unreadable pile.
            self.assertEqual(copied.parent.name, "my_project")
            self.assertEqual(copied.name, "short.mp4")

    def test_an_illegal_project_name_still_makes_a_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "short.mp4"
            source.write_bytes(b"v")
            renders.RENDERS_DIR = Path(tmp) / ".renders"
            copied = renders.publish(source, project='a/b:c*?"<>|')
            self.assertTrue(copied.is_file())
            self.assertNotIn(":", copied.parent.name)

    def test_the_subtitles_travel_with_the_video(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "short.mp4"
            source.write_bytes(b"v")
            source.with_suffix(".srt").write_text("1\n", encoding="utf-8")
            renders.RENDERS_DIR = Path(tmp) / ".renders"
            copied = renders.publish(source, project="p")
            self.assertTrue(copied.with_suffix(".srt").is_file())

    def test_a_second_render_never_overwrites_the_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            renders.RENDERS_DIR = Path(tmp) / ".renders"
            first = Path(tmp) / "a" / "short.mp4"
            first.parent.mkdir()
            first.write_bytes(b"one")
            second = Path(tmp) / "b" / "short.mp4"
            second.parent.mkdir()
            second.write_bytes(b"two")
            a = renders.publish(first, project="p")
            b = renders.publish(second, project="p")
            self.assertNotEqual(a, b)
            self.assertEqual(a.read_bytes(), b"one")
            self.assertEqual(b.read_bytes(), b"two")

    def test_a_missing_file_is_not_an_error(self):
        """A publish failure must never take a delivered video down with it."""
        self.assertIsNone(renders.publish(Path("does-not-exist.mp4")))

    def test_every_render_path_publishes(self):
        for module, marker, call in (
                ("pipeline", "def render_video(", "renders.publish("),
                ("longform_video", "def rebuild_from_disk(", "_publish_with_subtitles("),
                ("longform_video", "def recut_to_subtitles(", "_publish_with_subtitles(")):
            source = open(__import__(module).__file__, encoding="utf-8").read()
            body = source[source.index(marker):]
            body = body[:body.index("\ndef ", 10)]
            self.assertIn(call, body, module)

    def test_the_action_edit_library_call_does_not_publish(self):
        """build_short is what the test suite renders with; hooking it copied 92 MB of fixture
        video into .renders on every run. The app publishes at its own call site instead."""
        import action_editor
        source = open(action_editor.__file__, encoding="utf-8").read()
        body = source[source.index("def build_short("):]
        body = body[:body.index("\ndef ", 10)]
        self.assertNotIn("renders.publish(", body)
        app_source = open(Path(__file__).resolve().parents[1] / "app.py", encoding="utf-8").read()
        self.assertIn("renders.publish(out_path, project=short_dir.name", app_source)


if __name__ == "__main__":
    unittest.main()


class SnapToCaptionBoundaryTests(unittest.TestCase):
    """One cut per picture, moved onto a caption edge only when one is genuinely close.

    Measured on the sleep explainer (541 pictures, 547 cues): giving every picture its own
    boundary - greedily, and then as an optimal monotonic matching - pulled the cuts a median of
    6.3s and 4.7s away from the words they illustrate. That is structural, not a search bug: the
    beats and the captions divide the same narration at different rates in places. Snapping only
    within 0.6s puts 54% of the cuts on a caption edge and moves none of them by more than that.
    """

    def lines(self, *starts):
        return [{"start": s, "end": s + 1.0, "text": f"line {i}",
                 "words": [{"w": "w", "s": s}]} for i, s in enumerate(starts)]

    def cues(self, *starts):
        return [{"start": s, "end": s + 0.5, "text": "c"} for s in starts]

    def test_one_cut_per_picture_and_the_first_opens_the_video(self):
        cuts = lv.snap_frames_to_cue_starts(self.lines(0.0, 2.0, 4.0), self.cues(0.0, 2.1, 4.1))
        self.assertEqual(len(cuts), 3)
        self.assertEqual(cuts[0], 0.0)

    def test_a_nearby_boundary_wins(self):
        cuts = lv.snap_frames_to_cue_starts(self.lines(0.0, 2.0), self.cues(0.0, 1.8))
        self.assertEqual(cuts[1], 1.8)

    def test_a_distant_boundary_is_left_alone(self):
        """Better on the spoken word than dragged seconds away to reach a caption edge."""
        cuts = lv.snap_frames_to_cue_starts(self.lines(0.0, 5.0), self.cues(0.0, 9.0))
        self.assertEqual(cuts[1], 5.0)

    def test_it_never_moves_further_than_the_window(self):
        cuts = lv.snap_frames_to_cue_starts(
            self.lines(0.0, 2.0, 4.0, 6.0),
            self.cues(0.0, 2.4, 3.5, 6.9))
        for cut, line in zip(cuts[1:], self.lines(2.0, 4.0, 6.0)):
            self.assertLessEqual(abs(cut - line["start"]), lv.SNAP_WINDOW_S + 1e-9)

    def test_the_cuts_stay_in_order(self):
        cuts = lv.snap_frames_to_cue_starts(
            self.lines(0.0, 1.0, 1.2, 3.0), self.cues(0.0, 0.9, 0.95, 3.1))
        self.assertEqual(cuts, sorted(cuts))
        self.assertEqual(len(set(cuts)), len(cuts))

    def test_the_recut_uses_it_and_keeps_every_picture_once(self):
        source = open(lv.__file__, encoding="utf-8").read()
        body = source[source.index("def recut_to_subtitles("):]
        body = body[:body.index("\ndef ", 10)]
        self.assertIn("snap_frames_to_cue_starts(", body)
        self.assertNotIn("map_cues_to_frames(", body)
