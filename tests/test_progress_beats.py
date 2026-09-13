"""The progress bar is ruled by the video's own beats, and each phase reports its own count.

A percentage says how far. It does not say WHERE - which is the thing the owner wanted to see
while a render runs ("dass man sieht wo es etwa ist"). The bar is a bar of the video's time, so
it is divided exactly where the video is cut: one segment per beat, as wide as that beat is long,
and the segment the fill has reached names the shot.

Under it, whatever the current phase counts: ProPainter rebuilding frames of one clip, the
generator on clip 3 of 10. A phase that reports no total gets no bar - a progress bar that
guesses is worse than none.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import shell_v3


def _project(tmp, scenes):
    root = Path(tmp)
    (root / "config").mkdir(parents=True, exist_ok=True)
    (root / "config" / "project.json").write_text(json.dumps({"scenes": scenes}), encoding="utf-8")
    return {"project_dir": str(root)}


class TheBarKnowsTheBeats(unittest.TestCase):
    def test_the_beats_come_out_in_order_with_their_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            job = _project(tmp, [
                {"start": 1.63, "end": 3.27, "exact_voice_text": "that confuse foreigners."},
                {"start": 0.0, "end": 1.63, "exact_voice_text": "Three things about dating in Japan"},
            ])
            beats = shell_v3.run_beats(job)
            self.assertEqual([b["start"] for b in beats], [0.0, 1.63])
            self.assertEqual(beats[0]["text"], "Three things about dating in Japan")

    def test_a_zero_length_beat_is_not_a_segment(self):
        with tempfile.TemporaryDirectory() as tmp:
            job = _project(tmp, [{"start": 2.0, "end": 2.0, "script": "nothing"},
                                 {"start": 2.0, "end": 4.0, "script": "a shot"}])
            self.assertEqual(len(shell_v3.run_beats(job)), 1)

    def test_a_project_without_a_config_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(shell_v3.run_beats({"project_dir": tmp}), [])
        self.assertEqual(shell_v3.run_beats({}), [])

    def test_the_answer_is_cached_on_the_file_and_refreshed_when_it_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            job = _project(tmp, [{"start": 0.0, "end": 2.0, "script": "one"}])
            self.assertEqual(len(shell_v3.run_beats(job)), 1)
            path = Path(tmp) / "config" / "project.json"
            path.write_text(json.dumps({"scenes": [{"start": 0.0, "end": 2.0, "script": "one"},
                                                   {"start": 2.0, "end": 3.0, "script": "two"}]}),
                            encoding="utf-8")
            os.utime(path, (path.stat().st_atime + 10, path.stat().st_mtime + 10))
            self.assertEqual(len(shell_v3.run_beats(job)), 2)


class EachPhaseCountsItsOwnWork(unittest.TestCase):
    def test_the_caption_rebuild_reports_frames(self):
        """The unit only - which shot and what is being done to it is the beat label's job."""
        out = shell_v3.sub_progress(["Timeline render: cleaning ...",
                                     "Caption removal: filling frames 37-63 of 63."])
        self.assertEqual((out["done"], out["total"]), (63, 63))
        self.assertEqual(out["label"], "frames")

    def test_the_generator_reports_clips_and_images(self):
        self.assertEqual(shell_v3.sub_progress(["Seedance clip 3/10 queued."])["done"], 3)
        self.assertEqual(shell_v3.sub_progress(["GPT image 4/21 done."])["total"], 21)

    def test_the_newest_line_wins(self):
        out = shell_v3.sub_progress(["Seedance clip 3/10", "Caption removal: filling frames 1-9 of 40."])
        self.assertEqual(out["total"], 40)

    def test_a_phase_that_counts_nothing_gets_no_bar(self):
        self.assertIsNone(shell_v3.sub_progress(["Voice: speed 1.00x", "SFX: whoosh at 3.20"]))
        self.assertIsNone(shell_v3.sub_progress([]))

    def test_a_total_of_zero_is_not_a_bar(self):
        self.assertIsNone(shell_v3.sub_progress(["GPT image 0/0"]))


class ThePrePassMovesAlongTheSameBar(unittest.TestCase):
    """Cleaning captions IS work on a beat. It used to count frames under a bar frozen on shot 1,
    which read as two unrelated things happening at once."""

    BEATS = [{"start": 0.0, "end": 2.0, "clip": "pool_v4_tiktok_7619259980184653063.mp4"},
             {"start": 2.0, "end": 4.0, "clip": "pool_v4_tiktok_7636680962046315792.mp4"},
             {"start": 4.0, "end": 6.0, "clip": "capblur_02_f0dd28a4b4.mp4"}]

    def _clips(self):
        return [b["clip"] for b in self.BEATS]

    def test_a_clean_up_line_places_the_bar_on_that_beat(self):
        logs = ["Timeline render: pool_v4_tiktok_7636680962046315792.mp4 carries text on 7.8% of "
                "the frame; cleaning the 2.5s this scene shows."]
        i, doing = shell_v3.phase_position(logs, self.BEATS, self._clips())
        self.assertEqual(i, 1)
        self.assertIn("caption", doing)

    def test_a_derived_copy_still_finds_its_beat(self):
        # what prepare_timeline_clips actually writes: win_{stem[:26]}_{ms}.mp4, which cuts the
        # 19-digit post id down to its first 11
        logs = ["Timeline render: win_pool_v4_tiktok_76366809620_0001440.mp4 had a dead band; "
                "reused rebuilt frame."]
        i, doing = shell_v3.phase_position(logs, self.BEATS, self._clips())
        self.assertEqual(i, 1)
        self.assertIn("frame", doing)

    def test_a_beat_is_found_by_the_source_it_was_cleaned_from(self):
        """The scene points at the DERIVED file, the clean-up logs the SOURCE, and on a project
        whose clips carry no post id the two names share nothing. Measured on the eating-walk
        render: every beat came back -1."""
        beats = [{"start": 0.0, "end": 1.9, "clip": "capblur_01_3f28d78dcf.mp4",
                  "src": "scraped_00.mp4"},
                 {"start": 1.9, "end": 3.7, "clip": "capblur_02_4256bfe3f9.mp4",
                  "src": "scraped_01.mp4"}]
        logs = ["Timeline render: scraped_01.mp4 carries text on 4.2% of the frame; cleaning it."]
        i, doing = shell_v3.phase_position(logs, beats,
                                           [[b["clip"], b["src"]] for b in beats])
        self.assertEqual(i, 1)
        self.assertIn("caption", doing)

    def test_the_newest_placeable_line_wins(self):
        logs = ["Timeline render: pool_v4_tiktok_7636680962046315792.mp4 carries text on 7.8%.",
                "Timeline render: pool_v4_tiktok_7619259980184653063.mp4 carries text on 3.1%."]
        self.assertEqual(shell_v3.phase_position(logs, self.BEATS, self._clips())[0], 0)

    def test_nothing_placeable_means_no_claim(self):
        self.assertEqual(shell_v3.phase_position(["Rendering frames: 40%"], self.BEATS,
                                                 self._clips()), (-1, ""))
        self.assertEqual(shell_v3.phase_position(["anything"], [], []), (-1, ""))

    def test_the_shell_prefers_that_index_over_the_percentage(self):
        js = (Path(__file__).resolve().parent.parent / "static" / "shell-v3.js").read_text(
            encoding="utf-8", errors="replace")
        self.assertIn("if (typeof x.beat_index === \"number\" && x.beat_index >= 0)", js)
        self.assertIn("x.beat_doing", js)


class TheShellDrawsIt(unittest.TestCase):
    def setUp(self):
        self.js = (Path(__file__).resolve().parent.parent / "static" / "shell-v3.js").read_text(
            encoding="utf-8", errors="replace")

    def test_the_payload_reaches_the_bar(self):
        self.assertIn("function paintBeats", self.js)
        self.assertIn("paintBeats(rail, bar, x)", self.js)
        self.assertIn("paintSub(rail, x)", self.js)

    def test_a_segment_is_as_wide_as_its_beat_is_long(self):
        self.assertIn('seg.style.flexGrow = String(Math.max(0.001, b.end - b.start))', self.js)

    def test_the_reached_segment_is_the_one_marked_now(self):
        self.assertIn('seg.classList.toggle("now", i === current)', self.js)

    def test_the_sub_line_disappears_when_the_phase_reports_nothing(self):
        self.assertIn("if (!sub || !sub.total) { if (line) line.remove(); return; }", self.js)


if __name__ == "__main__":
    unittest.main()
