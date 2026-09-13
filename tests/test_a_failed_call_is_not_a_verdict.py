"""Infrastructure failures must not be recorded as judgements about the work.

Four of tonight's defects were the same shape, and together they cost an evening and a scrape
budget. In each one, something that could not be measured, could not be reached, or could not
be paid for was written down as a fact about the footage or the voice:

    an empty WaveSpeed account   -> 1485 windows "rejected", 15 beats assigned from the 75
                                    sources reviewed before the money ran out, 7 of them from
                                    ONE source, and a report reading `assigned: 15, total: 15`
    caption_coverage -> -1.0     -> "could not measure" failed the `0.015 < cov` test exactly
                                    like a clean clip and shipped with the creator's text on it
    forced alignment produced [] -> a words-per-minute guess, one WARNING, and a full scrape
                                    budget spent cutting to a two-line even split
    the timing's own source      -> written down as the API that was never called

The rule they all break: a "no" earned by looking is not the same as a "no" because nobody
looked, and only the first one may be acted on silently.
"""

import inspect
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent_core
import scrape_v4
from timeline.voice_timing import normalize_timestamps

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


class AnEmptyAccountStopsTheRun(unittest.TestCase):
    """It ran out at source 76 of 203 and the run finished as if nothing had happened."""

    def setUp(self):
        self.src = inspect.getsource(scrape_v4._vision_source_review)

    def test_the_guard_exists_and_names_the_balance(self):
        self.assertIn('"BalanceError" in str(_e)', self.src)
        self.assertIn("agent_core.WaveSpeedBalanceError(", self.src)

    def test_it_fires_before_anything_is_written_down_as_rejected(self):
        guard = self.src.index("_broke = sum(")
        loop = self.src.index("for key, candidate, _strip_path, _prompt in review_jobs:", guard)
        self.assertLess(guard, loop,
                        "the run records balance errors as rejections before it checks for them")

    def test_a_single_hiccup_does_not_stop_a_long_run(self):
        """One failed call among hundreds is noise; a tenth of them is an outage."""
        self.assertIn("max(3, len(review_jobs) // 10)", self.src)

    def test_the_error_says_the_downloads_are_not_lost(self):
        self.assertIn("cached", self.src)


class AFailedMeasurementIsNotACleanClip(unittest.TestCase):
    """caption_coverage returns -1.0 when it cannot measure; every caller read that as 0."""

    def test_the_measurement_still_reports_its_own_failure(self):
        doc = inspect.getdoc(agent_core.caption_remover.caption_coverage) if hasattr(
            agent_core, "caption_remover") else None
        import caption_remover
        self.assertIn("-1.0", inspect.getdoc(caption_remover.caption_coverage))

    def test_the_render_pre_pass_cleans_rather_than_assumes(self):
        src = read("agent_core.py")
        self.assertIn("if _cov < 0:", src)
        self.assertIn("could not be measured", src)

    def test_and_an_uncleanable_clip_says_so_out_loud(self):
        """Above the ceiling the clip ships with the creator's text - a decision, not a silence."""
        src = read("agent_core.py")
        self.assertIn("ABOVE the ", src)

    def test_the_scene_records_what_the_cleaning_did(self):
        src = read("agent_core.py")
        for outcome in ('"cleaned"', "refused: text covers", "no removable text found",
                        "skipped: ", "failed: "):
            self.assertIn(outcome, src)


class AGuessIsNotATimeline(unittest.TestCase):
    def test_a_scrape_run_refuses_to_spend_on_estimated_timing(self):
        src = read("agent_core.py")
        self.assertIn('_footage == "scrape" and not str(form.get("allow_estimated_timing")', src)
        self.assertIn("Voice timing is estimated", src)

    def test_the_message_does_not_send_the_reader_to_the_empty_stub(self):
        """word_timestamps.json is written as [] unconditionally - it proves nothing."""
        src = read("agent_core.py")
        guard = src[src.index("Voice timing is estimated ({target_duration"):][:900]
        self.assertIn("audio_analysis.json", guard)
        self.assertIn("voice_source.json", guard)
        self.assertIn("NOT at", guard)

    def test_the_fallback_can_actually_be_reached(self):
        """`audio_path.name` raised AttributeError on a str before Gemini was ever called."""
        src = read("agent_core.py")
        self.assertIn("Path(audio_path).name", src)
        self.assertNotIn("{audio_path.name}", src)

    def test_the_record_names_the_source_that_did_the_work(self):
        analysis = {"timing_source": "forced_alignment", "duration_seconds": 10.0,
                    "sentence_timestamps": [{"start": 0, "end": 5, "text": "one"},
                                            {"start": 5, "end": 10, "text": "two"}]}
        _scenes, source = normalize_timestamps(analysis, "one two", 10.0, 10.0)
        self.assertEqual("forced_alignment", source)

    def test_and_a_fallback_is_still_called_a_fallback(self):
        _scenes, source = normalize_timestamps({}, "one two three", 10.0, 10.0)
        self.assertEqual("estimated_script_timing", source)


class ALongSilentPhaseSaysItIsWorking(unittest.TestCase):
    """82 minutes of four saturated cores, nothing on disk, nothing in the log."""

    def test_the_media_gate_reports_progress(self):
        src = inspect.getsource(scrape_v4.scrape_social_plan_v4)
        self.assertIn("V4 media gate: inspected", src)
        self.assertIn("_step = max(10,", src)


if __name__ == "__main__":
    unittest.main()
