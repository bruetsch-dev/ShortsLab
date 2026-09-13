"""Rescrape the clips selected in the timeline with the FULL V3 engine.

Asked for by name: "select clips and then press rescrape and then it does a whole scrape just
like the v3 one for the selected clips in the timeline".

The timeline already had a targeted replacement, but it ran `clip_scraper.scrape_bucket` - the
old flat keyword bucket. A fresh project gets `scrape_v3`, which plans visual chapters and
searches per chapter. This makes the button use that same engine on the marked scenes only,
while the rest of the timeline is left alone.
"""

import unittest
from pathlib import Path
from unittest import mock

import agent_core
import scrape_v3

ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "app.py").read_text(encoding="utf-8")


class CandidateShapeTests(unittest.TestCase):
    """V3 hands back its own row shape; the replacement pipeline reads the bucket shape."""

    ROW = {"source_id": "abc123", "platform": "tiktok", "path": "/tmp/x.mp4",
           "query": "train doors closing", "item": {"desc": "a caption", "id": "abc123",
                                                    "stats": {"diggCount": 4200}}}

    class Chapter:
        title = "Arrival"
        chapter_id = 2

    def test_it_carries_the_fields_the_matcher_reads(self):
        got = scrape_v3._as_bucket_candidate(self.ROW, self.Chapter())
        for key in ("path", "query", "platform", "clip_id", "likes", "meta",
                    "black_bar_score", "text_heaviness", "rapid_internal_cut_count"):
            self.assertIn(key, got)
        self.assertEqual(got["likes"], 4200)
        self.assertEqual(got["clip_id"], "abc123")
        self.assertEqual(got["meta"]["caption"], "a caption")
        self.assertEqual(got["meta"]["chapter"], "Arrival")

    def test_a_record_with_nothing_in_it_does_not_explode(self):
        got = scrape_v3._as_bucket_candidate({"path": "/tmp/y.mp4"}, self.Chapter())
        self.assertEqual(got["likes"], 0)
        self.assertEqual(got["platform"], "tiktok")

    def test_the_quality_scores_are_measured_not_assumed(self):
        """A flat 0.0 would tell the ranker 'no letterboxing, no burnt-in captions' about
        footage nobody looked at - which is what those gates exist to catch."""
        with mock.patch.object(scrape_v3.clip_scraper, "detect_fake_vertical_or_black_bars",
                               return_value={"black_bar_score": 7.5}), \
             mock.patch.object(scrape_v3.clip_scraper, "text_heaviness_score", return_value=3.25):
            got = scrape_v3._as_bucket_candidate(self.ROW, self.Chapter(), ffmpeg="ffmpeg")
        self.assertEqual(got["black_bar_score"], 7.5)
        self.assertEqual(got["text_heaviness"], 3.25)

    def test_a_broken_probe_does_not_lose_the_candidate(self):
        with mock.patch.object(scrape_v3.clip_scraper, "detect_fake_vertical_or_black_bars",
                               side_effect=OSError("no ffmpeg")), \
             mock.patch.object(scrape_v3.clip_scraper, "text_heaviness_score",
                               side_effect=OSError("no ffmpeg")):
            got = scrape_v3._as_bucket_candidate(self.ROW, self.Chapter(), ffmpeg="ffmpeg")
        self.assertEqual(got["path"], "/tmp/x.mp4")
        self.assertEqual(got["black_bar_score"], 0.0)


class GatherForSelectionTests(unittest.TestCase):
    SCENES = [{"id": "3", "script": "the doors close on a packed train"},
              {"id": "4", "script": "a gloved hand pushes the last person in"}]

    def test_it_plans_chapters_for_the_selection_and_searches_each(self):
        chapters = [mock.Mock(title="A", chapter_id=1, explicit_queries=[]),
                    mock.Mock(title="B", chapter_id=2, explicit_queries=[])]
        rows = [{"path": "/tmp/a.mp4", "source_id": "1", "platform": "tiktok",
                 "query": "q", "item": {}}]
        with mock.patch.object(scrape_v3, "plan_chapters_v3", return_value=chapters) as planned, \
             mock.patch.object(scrape_v3, "gather_chapter_sources", return_value=rows) as gathered:
            got = scrape_v3.gather_candidates_for_scenes(
                {}, self.SCENES, "/tmp/project", ["tiktok"], measure=False)
        self.assertEqual(planned.call_args[0][0], self.SCENES)
        self.assertEqual(gathered.call_count, 2)
        self.assertEqual(len(got), 2)

    def test_no_scenes_means_no_search(self):
        with mock.patch.object(scrape_v3, "gather_chapter_sources") as gathered:
            self.assertEqual(
                scrape_v3.gather_candidates_for_scenes({}, [], "/tmp/p", ["tiktok"]), [])
        self.assertFalse(gathered.called)

    def test_it_stops_when_the_run_is_cancelled(self):
        chapters = [mock.Mock(title="A", chapter_id=1, explicit_queries=[]),
                    mock.Mock(title="B", chapter_id=2, explicit_queries=[])]
        with mock.patch.object(scrape_v3, "plan_chapters_v3", return_value=chapters), \
             mock.patch.object(scrape_v3, "gather_chapter_sources", return_value=[]) as gathered:
            scrape_v3.gather_candidates_for_scenes(
                {}, self.SCENES, "/tmp/p", ["tiktok"], cancel_check=lambda: True, measure=False)
        self.assertFalse(gathered.called)

    def test_the_creators_own_search_terms_are_honoured(self):
        chapter = mock.Mock(title="A", chapter_id=1, explicit_queries=[])
        with mock.patch.object(scrape_v3, "plan_chapters_v3", return_value=[chapter]), \
             mock.patch.object(scrape_v3, "gather_chapter_sources", return_value=[]):
            scrape_v3.gather_candidates_for_scenes(
                {"scrape_terms": "tokyo rush hour, oshiya"}, self.SCENES, "/tmp/p",
                ["tiktok"], measure=False)
        self.assertTrue(chapter.explicit_queries)


class EngineSelectionTests(unittest.TestCase):
    def test_the_replacement_can_run_either_engine(self):
        source = (ROOT / "agent_core.py").read_text(encoding="utf-8")
        body = source[source.index("def replace_timeline_scrape_scenes("):]
        body = body[:body.index("\ndef ", 10)]
        self.assertIn('engine="bucket"', body)          # default keeps the old behaviour
        self.assertIn("gather_candidates_for_scenes(", body)
        self.assertIn("clip_scraper.scrape_bucket(", body)

    def test_v3_is_what_the_timeline_button_asks_for(self):
        self.assertIn('engine="v3"', APP)

    def test_it_returns_the_shape_the_pipeline_already_consumes(self):
        """Only the SEARCH changes; nothing downstream should learn about chapters."""
        source = (ROOT / "agent_core.py").read_text(encoding="utf-8")
        body = source[source.index("def replace_timeline_scrape_scenes("):]
        body = body[:body.index("\ndef ", 10)]
        after = body[body.index("gather_candidates_for_scenes("):]
        # the very next thing done with `got` is the shared pool-building loop
        self.assertIn("for item in got:", after)


class WiringTests(unittest.TestCase):
    def test_the_button_exists_and_says_what_it_does(self):
        self.assertIn('id="tl-insp-rescrape"', APP)
        self.assertIn("Rescrape selection", APP)

    def test_it_lives_in_the_rework_popup(self):
        """Where media replacement already lives. The floating clip inspector was not findable
        ("wo is die verdammte rescrap selected media funktion?")."""
        popup = APP[APP.index('id="tl-rework-pop"'):]
        popup = popup[:popup.index('id="tl-rework"')]
        self.assertIn('id="tl-rescrape-pop"', popup)
        self.assertIn("Rescrape selected clips", popup)

    def test_it_says_what_it_will_act_on_before_it_is_pressed(self):
        """It starts a paid search, so an empty selection must be visible, not discovered."""
        self.assertIn("syncRescrapeCount", APP)
        self.assertIn("Select clips on the timeline first.", APP)
        self.assertIn("inPop.disabled = n===0", APP)

    def test_it_acts_on_the_multi_selection(self):
        handler = APP[APP.index("function startRescrape(btn){"):]
        handler = handler[:handler.index("document.getElementById('tl-insp-blurcap')")]
        self.assertIn("selectedClipIds.indexOf(x.id)!==-1", handler)
        self.assertIn("/timeline-rescrape", handler)
        # the edit on screen is saved before the search is planned from it
        self.assertIn("/timeline-save", handler)
        self.assertIn(handler.index("/timeline-save") < handler.index("/timeline-rescrape"), [True])
        # it costs money, so it asks
        self.assertIn("confirm(", handler)

    def test_the_endpoint_refuses_what_it_cannot_do(self):
        route = APP[APP.index('if parsed.path == "/timeline-rescrape":'):]
        route = route[:route.index('if parsed.path == "/timeline-rework":')]
        self.assertIn("Unknown project.", route)
        self.assertIn("Select at least one clip.", route)
        # generated-clip projects have nothing to scrape
        self.assertIn('clip_source', route)
        # and the timeline is versioned first, so a bad rescrape is recoverable
        self.assertIn("snapshot_timeline_version(", route)


if __name__ == "__main__":
    unittest.main()
