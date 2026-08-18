import tempfile
import unittest
from pathlib import Path
from unittest import mock

import agent_core
import scrape_v2


class FactShortVisualChapterTests(unittest.TestCase):

    def test_download_ranking_interleaves_scene_coverage(self):
        def source(source_id, scene_id, score):
            return scrape_v2.SourceVideoCandidate(
                platform="tiktok", source_id=source_id, creator_id=source_id,
                url=f"https://example.test/{source_id}", scene_ids=[scene_id],
                rank_score=score,
            )

        ranked = scrape_v2.interleave_ranked_sources_by_scene([
            source("a1", 0, 9.9), source("a2", 0, 9.8), source("a3", 0, 9.7),
            source("b1", 1, 7.2), source("b2", 1, 7.1),
            source("c1", 2, 6.8),
        ])
        self.assertEqual([row.source_id for row in ranked[:3]], ["a1", "b1", "c1"])
        self.assertEqual(len({row.source_id for row in ranked}), 6)

    def test_chapter_cut_audit_rejects_hidden_double_cut(self):
        self.assertTrue(scrape_v2.chapter_montage_cuts_are_clean([3.0], [3.0], 6.0))
        self.assertFalse(scrape_v2.chapter_montage_cuts_are_clean([3.0, 3.13], [3.0], 6.0))
        self.assertFalse(scrape_v2.chapter_montage_cuts_are_clean([2.1], [3.0], 6.0))

    def test_architect_primary_queries_precede_vibe_variants(self):
        plan = scrape_v2._architect_primary_query_plan({
            "native_object_query": "音姫 トイレ",
            "native_action_query": "音姫 使い方",
            "english_discovery_query": "Otohime toilet device",
            "platform_queries": {"tiktok": {
                "japanese": ["日本 トイレ あるある"],
                "english": ["japan restroom vlog"],
            }},
        })
        # Coverage round zero executes only the first query. The uploader-style action phrase
        # must therefore precede the bare object, which is noisier on TikTok.
        self.assertEqual(plan["tiktok"]["japanese"][:2], ["音姫 使い方", "音姫 トイレ"])
        self.assertEqual(plan["tiktok"]["english"][0], "Otohime toilet device")

    def test_one_continuous_exact_source_can_cover_a_long_chapter(self):
        segment = scrape_v2.SegmentCandidate(
            segment_id="continuous", source_id="source-a", platform="tiktok",
            source_path="continuous.mp4", start_time=0.0, end_time=8.0, duration=8.0,
            quality_score=9.0, semantic_score=9.0, japanese_context=True,
            source_width=720, source_height=1280, query="umbrella bag machine",
            visual_description={"subjects": ["umbrella machine"],
                                "action": "umbrella enters a sleeve"},
        )
        intent = scrape_v2.VisualIntent(
            scene_id=1, scene_text="umbrella bag machine", subject="umbrella machine",
            action="wraps wet umbrella", required_elements=["machine", "umbrella sleeve"])
        plan = scrape_v2.plan_chapter_montage_rows(
            segment, [{"segment": segment, "overall_match": 9.5,
                       "covers": ["machine", "umbrella sleeve"], "match_class": "A_MATCH"}],
            7.6, intent, scene_idx=1)
        self.assertEqual(len(plan), 1)
        self.assertAlmostEqual(plan[0][1], 7.6)

    def test_native_query_metadata_targets_native_caption_terms(self):
        intent = scrape_v2.VisualIntent(
            scene_id=2, scene_text="Otohime privacy button", subject="toilet sound device",
            action="pressing the privacy sound button", location="Japanese restroom",
            platform_queries={"tiktok": {"japanese": ["音姫 使い方"], "english": []}},
        )
        query = scrape_v2.queries_for_intent(intent)[0]
        self.assertEqual(query.expected_subject, "音姫 使い方")
        relevant = scrape_v2.estimate_metadata_relevance(
            {"caption": "自宅用にセンサー音姫を買いました。使い方を紹介します。"}, query)
        unrelated = scrape_v2.estimate_metadata_relevance(
            {"caption": "東京で新しいラーメン店を見つけました。"}, query)
        self.assertGreater(relevant, unrelated)

    def test_card_dates_do_not_trigger_relationship_searches(self):
        intent = scrape_v2.VisualIntent(
            scene_id=4,
            scene_text="Cards go into plastic pages beside phone photos, dates, and locations.",
            subject="manhole card collector",
            action="files cards with dates and locations",
            location="home desk",
        )
        self.assertEqual(scrape_v2._relationship_scene_seeds_v2(intent), [])

    def test_listicle_microbeats_become_three_searchable_visual_chapters(self):
        scenes = [
            {"id": 0, "start": 0.0, "end": 2.0, "script": "Three things in Japan make sense."},
            {"id": 1, "start": 2.0, "end": 4.0, "script": "First, umbrella lockers stop theft."},
            {"id": 2, "start": 4.0, "end": 6.0, "script": "You lock the umbrella outside."},
            {"id": 3, "start": 6.0, "end": 8.0, "script": "Second, taxis open their own doors."},
            {"id": 4, "start": 8.0, "end": 10.0, "script": "The driver controls the mechanism."},
            {"id": 5, "start": 10.0, "end": 14.0, "script": "Third, heated mirrors clear the steam."},
        ]

        chapters = agent_core.coalesce_scrape_visual_chapters(scenes)

        self.assertEqual(len(chapters), 3)
        self.assertEqual([(row["start"], row["end"]) for row in chapters],
                         [(0.0, 6.0), (6.0, 10.0), (10.0, 14.0)])
        self.assertIn("umbrella lockers", chapters[0]["script"])
        self.assertTrue(all(row["visual_chapter"] for row in chapters))

    def test_successful_manual_edit_keeps_each_complete_list_item_as_one_search_chapter(self):
        scenes = [
            {"id": 0, "start": 0.0, "end": 7.60,
             "script": "Three things in Japan that make sense. First, umbrella bag machines. "
                       "A wet umbrella comes out inside a sleeve."},
            {"id": 1, "start": 7.60, "end": 14.24,
             "script": "Second, ramen ticket machines. Pick the bowl and pay before sitting."},
            {"id": 2, "start": 14.24, "end": 24.81,
             "script": "And third, the Otohime toilet button. It plays privacy water sounds."},
        ]

        chapters = agent_core.coalesce_scrape_visual_chapters(scenes)

        self.assertEqual(len(chapters), 3)
        self.assertEqual([(row["start"], row["end"]) for row in chapters],
                         [(0.0, 7.6), (7.6, 14.24), (14.24, 24.81)])

    def test_non_listicle_paragraphs_become_semantic_search_chapters(self):
        blocks = [
            "Women wear surgical masks on empty Tokyo streets. They are hiding a makeup-free face.",
            "Showing a bare face can feel humiliating. The social rule demands flawless makeup.",
            "Clothing stores provide white fabric hoods. Shoppers cover their heads before shirts.",
        ]
        scenes = [
            {"start": 0.0, "end": 2.0, "script": "Women wear surgical masks on empty Tokyo streets."},
            {"start": 2.0, "end": 4.0, "script": "They are hiding a makeup-free face."},
            {"start": 4.0, "end": 6.0, "script": "Showing a bare face can feel humiliating."},
            {"start": 6.0, "end": 8.0, "script": "The social rule demands flawless makeup."},
            {"start": 8.0, "end": 10.0, "script": "Clothing stores provide white fabric hoods."},
            {"start": 10.0, "end": 12.0, "script": "Shoppers cover their heads before shirts."},
        ]

        chapters = agent_core.coalesce_scrape_visual_chapters(
            scenes, semantic_blocks=blocks)

        self.assertEqual(len(chapters), 3)
        self.assertEqual([(row["start"], row["end"]) for row in chapters],
                         [(0.0, 4.0), (4.0, 8.0), (8.0, 12.0)])

    def test_structured_paragraph_never_demands_a_window_longer_than_the_scrape_limit(self):
        """A paragraph is a semantic boundary, not permission for an impossible 12s TikTok cut."""
        scenes = [
            {"start": 0.0, "end": 3.0, "script": "Couples walk under Christmas lights."},
            {"start": 3.0, "end": 6.0, "script": "The street glows on Christmas Eve."},
            {"start": 6.0, "end": 9.0, "script": "They keep affection quiet on trains."},
            {"start": 9.0, "end": 12.0, "script": "Standing together is enough."},
        ]
        chapters = agent_core.coalesce_scrape_visual_chapters(
            scenes, max_s=6.2,
            semantic_blocks=["Couples walk under Christmas lights. The street glows on Christmas Eve.",
                             "They keep affection quiet on trains. Standing together is enough."])

        self.assertTrue(chapters)
        self.assertTrue(all(float(row["end"]) - float(row["start"]) <= 6.2
                            for row in chapters))

    def test_long_chapter_is_planned_as_distinct_overview_action_result_shots(self):
        intent = scrape_v2.VisualIntent(
            scene_id=0, scene_text="Use the machine and receive the result",
            subject="ticket machine", action="press buttons and receive bowl",
            required_elements=["ticket machine", "press buttons", "finished bowl"],
        )

        def segment(identifier, source, start, subject, action):
            return scrape_v2.SegmentCandidate(
                segment_id=identifier, source_id=source, platform="tiktok",
                source_path=f"{identifier}.mp4", start_time=start, end_time=start + 4.0,
                duration=4.0, quality_score=8.0, japanese_context=True,
                query="ramen ticket machine",
                visual_description={"subjects": [subject], "action": action,
                                    "location": "ramen shop"},
            )

        overview = segment("overview", "source-a", 0.0, "ticket machine", "shows machine")
        action = segment("action", "source-a", 4.2, "hands", "press buttons")
        result = segment("result", "source-b", 0.0, "finished bowl", "bowl is served")
        rows = [
            {"segment": overview, "overall_match": 8.4, "covers": ["ticket machine"]},
            {"segment": action, "overall_match": 8.2, "covers": ["press buttons"]},
            {"segment": result, "overall_match": 8.0, "covers": ["finished bowl"]},
        ]

        plan = scrape_v2.plan_chapter_montage_rows(
            overview, rows, 10.5, intent, source_owner={"source-a": 0}, scene_idx=0)

        self.assertEqual([row["segment"].segment_id for row, _seconds in plan],
                         ["overview", "action", "result"])
        self.assertAlmostEqual(sum(seconds for _row, seconds in plan), 10.5)
        self.assertTrue(all(seconds >= 1.25 for _row, seconds in plan))

    def test_query_plan_uses_only_platforms_enabled_for_the_run(self):
        intent = scrape_v2.VisualIntent(
            scene_id=4,
            scene_text="A taxi door opens automatically",
            subject="taxi door",
            action="opens automatically",
            location="street",
        )
        queries = [
            scrape_v2.SearchQueryV2("taxi automatic door", "en", "exact_action", "scene_4",
                                    platforms=["tiktok"]),
            scrape_v2.SearchQueryV2("タクシー 自動ドア", "ja", "exact_action", "scene_4",
                                    platforms=["tiktok"]),
            scrape_v2.SearchQueryV2("タクシー ドア 開く", "ja", "action_location", "scene_4",
                                    platforms=["tiktok"]),
            scrape_v2.SearchQueryV2("タクシー ドア", "ja", "action_location", "scene_4",
                                    platforms=["twitter"]),
            scrape_v2.SearchQueryV2("automatic taxi in Japan", "en", "broad_context", "scene_4",
                                    platforms=["twitter"]),
            scrape_v2.SearchQueryV2("taxi vlog", "en", "native_vlog", "scene_4",
                                    platforms=["tiktok"]),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            with (
                mock.patch.object(scrape_v2, "queries_for_intent", return_value=queries),
                mock.patch.object(scrape_v2, "_log"),
            ):
                planned, audit = scrape_v2.build_scene_bound_query_plan(
                    [intent], tmp, "A taxi door opens automatically",
                    active_platforms=["tiktok"])

        self.assertEqual(len(planned[4]), 3)
        self.assertEqual([row.query for row in planned[4]],
                         ["タクシー 自動ドア", "タクシー ドア 開く", "taxi automatic door"])
        self.assertEqual(audit["query_count"], 3)
        self.assertEqual(len(audit["rejected_before_search"]), 3)
        self.assertTrue(all(row.platforms == ["tiktok"] for row in planned[4]))
        self.assertTrue(all("twitter" not in row["source"] for row in audit["queries"]))

    def test_native_event_queries_keep_compounds_and_visible_verb(self):
        intent = scrape_v2.VisualIntent(
            scene_id=0, scene_text="A salaryman sleeps outside after missing the last train",
            subject="salaryman", action="sleeps outside", location="Tokyo street",
        )
        compound = scrape_v2.SearchQueryV2(
            "終電難民", "ja", "exact_action", "scene_0", platforms=["tiktok"])
        ok, _reason, normalized = scrape_v2.validate_query_against_intent(compound, intent)

        self.assertTrue(ok)
        self.assertEqual(normalized, "終電難民")
        self.assertEqual(scrape_v2._clean_japanese_queries(["路上 酔っ払い 寝てる"]),
                         ["路上 酔っ払い 寝てる"])

    def test_assignment_never_uses_a_window_shorter_than_the_voice_chapter(self):
        intent = scrape_v2.VisualIntent(scene_id=1, scene_text="proof")
        short = scrape_v2.SegmentCandidate(
            segment_id="short", source_id="source-short", platform="tiktok",
            source_path="short.mp4", start_time=0.0, end_time=3.0, duration=3.0,
            quality_score=9.0,
        )
        long = scrape_v2.SegmentCandidate(
            segment_id="long", source_id="source-long", platform="tiktok",
            source_path="long.mp4", start_time=0.0, end_time=6.0, duration=6.0,
            quality_score=7.0,
        )
        candidates = {1: [
            {"segment": short, "overall_match": 9.5, "style_match": 8.0,
             "match_class": "A_MATCH"},
            {"segment": long, "overall_match": 8.0, "style_match": 7.0,
             "match_class": "A_MATCH"},
        ]}
        cfg = dict(scrape_v2.SCRAPE_V2_CONFIG)
        cfg["required_duration_by_scene"] = {1: 5.5}

        assigned = scrape_v2.assign_segments_globally_v2([intent], candidates, cfg)

        self.assertEqual(assigned[1].segment_id, "long")

    def test_finalizer_refuses_to_extend_past_inspected_cut_clean_window(self):
        segment = scrape_v2.SegmentCandidate(
            segment_id="seg", source_id="src", platform="tiktok",
            source_path="source.mp4", start_time=2.0, end_time=5.0, duration=3.0,
        )
        with mock.patch.object(scrape_v2.clip_scraper, "normalize_clip") as normalize:
            result = scrape_v2._finalize_segment_clip(
                segment, Path("unused-project"), "ffmpeg", min_seconds=4.0)

        self.assertEqual(result, "")
        normalize.assert_not_called()

    def test_literal_evidence_gate_rejects_keyword_association(self):
        intent = scrape_v2.VisualIntent(
            scene_id=4,
            scene_text="Restaurants provide baskets under chairs so handbags stay off the floor.",
            visual_type="concrete", match_category="literal",
            communication_role="demonstration",
            subject="restaurant handbag basket",
            action="customer places handbag in basket beneath chair",
            location="Japanese restaurant",
            required_elements=["handbag", "basket beneath chair", "placing bag"],
        )
        intent.script_relevancy = 90
        segment = scrape_v2.SegmentCandidate(
            segment_id="box", source_id="source-box", platform="tiktok",
            source_path="box.mp4", start_time=0.0, end_time=4.0, duration=4.0,
            query="restaurant bag basket", scene_ids=[4], japanese_context=True,
            visual_description={
                "subjects": ["man", "food container"],
                "action": "holds a container", "location": "kitchen", "usable": True,
            },
        )
        model_answer = {"scenes": {"4": [{
            "seg": 0, "subject_match": 8, "action_match": 8,
            "location_match": 7, "mood_match": 8, "script_match": 8,
            "style_match": 8, "literal_match": False,
            "visible_evidence": ["man holds food container in kitchen"],
            "missing_required": ["handbag", "basket beneath chair", "placing bag"],
            "covers": [], "reason": "container context",
        }]}}
        with mock.patch.object(scrape_v2, "_llm_json", return_value=model_answer):
            matched = scrape_v2.match_segments_to_scenes_v2([intent], [segment])

        self.assertFalse(matched.get(4))

    def test_visual_chapter_accepts_direct_partial_evidence_for_montage(self):
        intent = scrape_v2.VisualIntent(
            scene_id=2,
            scene_text="Salarymen miss the last train, sleep outside, then wake for work.",
            visual_type="concrete", match_category="literal",
            communication_role="demonstration",
            subject="Japanese salaryman",
            action="misses last train, sleeps outside, wakes for work",
            location="Tokyo street",
            required_elements=["last train", "sleeping outside", "waking for work"],
        )
        intent.script_relevancy = 90
        intent.allows_partial_coverage = True
        segment = scrape_v2.SegmentCandidate(
            segment_id="sleep", source_id="source-sleep", platform="tiktok",
            source_path="sleep.mp4", start_time=1.0, end_time=5.0, duration=4.0,
            query="路上寝 サラリーマン", scene_ids=[2], japanese_context=True,
            visual_description={
                "subjects": ["man in business suit"],
                "action": "sleeping on a pavement at night",
                "location": "Japanese city street", "usable": True,
            },
        )
        model_answer = {"scenes": {"2": [{
            "seg": 0, "subject_match": 9, "action_match": 9,
            "location_match": 8, "mood_match": 8, "script_match": 9,
            "style_match": 8, "literal_match": True,
            "visible_evidence": ["suited man visibly sleeping on pavement"],
            "missing_required": ["last train", "waking for work"],
            "covers": ["sleeping outside"], "reason": "direct middle action",
        }]}}
        with mock.patch.object(scrape_v2, "_llm_json", return_value=model_answer):
            matched = scrape_v2.match_segments_to_scenes_v2([intent], [segment])

        self.assertEqual(matched[2][0]["segment"].segment_id, "sleep")
        self.assertEqual(matched[2][0]["covers"], ["sleeping outside"])


if __name__ == "__main__":
    unittest.main()
