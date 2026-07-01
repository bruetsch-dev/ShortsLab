import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import agent_core
import app
import clip_scraper


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class TikTokScrapeLogicTests(unittest.TestCase):
    def test_planning_text_does_not_duplicate_narration(self):
        scene = {"script": "This is the spoken line.", "visual_script": "show a train"}
        self.assertEqual(
            agent_core.scene_text_for_planning(scene),
            "This is the spoken line. show a train",
        )

    def test_semantic_threshold_relaxes_after_each_failed_round(self):
        thresholds = [agent_core.adaptive_script_match_threshold(80, attempt) for attempt in range(4)]
        self.assertEqual(thresholds, [7.0, 6.0, 5.0, 4.5])

    def test_pacing_and_voice_sync_use_only_words_in_each_cut(self):
        source = [{
            "start": 0.0,
            "end": 6.0,
            "script": "one two three four five six",
            "exact_voice_text": "one two three four five six",
        }]
        cuts = agent_core.enforce_reference_pacing(source, max_s=2.0)
        self.assertEqual([c["script"] for c in cuts], ["one two", "three four", "five six"])

        timeline = [
            {"word": word, "start": i, "end": i + 0.8}
            for i, word in enumerate(("one", "two", "three", "four", "five", "six"))
        ]
        synced = agent_core.sync_scenes_to_voice_timeline(cuts, timeline, target_duration=6.0)
        self.assertEqual([s["exact_voice_text"] for s in synced],
                         ["one two", "three four", "five six"])

    def test_scrape_pacing_merges_isolated_sub_secondish_beats(self):
        scenes = [
            {"start": 0.0, "end": 1.8, "exact_voice_text": "hook"},
            {"start": 1.8, "end": 3.0, "exact_voice_text": "hot coffee"},
            {"start": 3.0, "end": 4.1, "exact_voice_text": "clean socks"},
            {"start": 4.1, "end": 6.1, "exact_voice_text": "concert tickets"},
        ]
        stable = agent_core.coalesce_short_scrape_scenes(scenes, min_s=1.45, max_s=3.2)
        self.assertEqual(len(stable), 3)
        self.assertEqual(stable[1]["exact_voice_text"], "hot coffee clean socks")
        self.assertAlmostEqual(stable[1]["end"] - stable[1]["start"], 2.3)
        self.assertEqual(stable[0]["exact_voice_text"], "hook")

    def test_stable_segment_profile_avoids_internal_cut_cluster(self):
        with mock.patch.object(clip_scraper, "_probe_duration", return_value=12.0), \
             mock.patch.object(clip_scraper, "hard_cut_times",
                               return_value=[0.4, 0.8, 5.0, 9.5]):
            profile = clip_scraper.stable_segment_profile(
                "clip.mp4", "ffmpeg", "ffprobe", seconds=4.0)
        self.assertTrue(profile["stable"])
        self.assertGreaterEqual(profile["start"], 0.8)
        self.assertEqual(profile["rapid_internal_cut_count"], 0)

    def test_scrape_editor_sfx_are_sparse_punchy_and_not_ambient(self):
        scenes = [
            {"start": i * 2.0, "end": (i + 1) * 2.0,
             "exact_voice_text": "a normal line" if i % 5 else "the shocking truth"}
            for i in range(25)
        ]
        config = {
            "project_slug": "sfx-test", "duration": 50.0, "scenes": scenes,
            "sfx_enabled": True, "editor_sfx_max_per_minute": 14,
            "editor_sfx_volume_with_speech": 0.46,
        }
        fake_pack = [Path(f"whoosh_transition_{i:02d}.wav") for i in range(1, 4)]
        fake_pack += [Path("whoosh_hit_combo_01.wav"), Path("bass_impact_01.wav"),
                      Path("caption_pop_01.wav"), Path("ui_click_01.wav")]
        with mock.patch.object(agent_core.pipeline, "sfx_category_files",
                               side_effect=lambda _config, category: fake_pack if category == "editor_pack" else []):
            count = agent_core.place_editor_sfx(config)
        self.assertGreater(count, 0)
        # bounded by the per-minute budget (mpm=14 over 50s) - not an ambient wall of sound
        self.assertLessEqual(count, 14)
        # each event is a SHORT, event-based hit (<=2.1s), never a continuous ambient bed
        self.assertTrue(all(0.0 < event["duration"] <= 2.1
                            for event in config["ai_content_sfx"]))
        # categories are real editor SFX types (not a generic/ambient tag)
        self.assertTrue(all(str(event.get("category") or "").strip()
                            for event in config["ai_content_sfx"]))
        # SFX are audible (they were boosted louder); allow the quiet ui_click accents
        self.assertTrue(all(event["volume"] >= 0.15
                            for event in config["ai_content_sfx"]))

    def test_novi_request_uses_valid_minimum_and_relevance_region(self):
        captured = {}

        def fake_urlopen(request, timeout=0):
            captured["body"] = json.loads(request.data.decode("utf-8"))
            return _Response([])

        with mock.patch.dict(os.environ, {"APIFY_TOKEN": "test-token"}), \
             mock.patch.object(clip_scraper, "APIFY_ACTOR", "novi~tiktok-scraper-ultimate"), \
             mock.patch.object(clip_scraper, "APIFY_LOCATION", "JP"), \
             mock.patch.object(clip_scraper.urllib.request, "urlopen", side_effect=fake_urlopen):
            clip_scraper.apify_search(["満員電車"], 4)

        body = captured["body"]
        self.assertGreaterEqual(body["maxItems"], 20)
        self.assertEqual(body["sortType"], "MOST_LIKED")
        self.assertEqual(body["location"], "JP")
        self.assertTrue(body["includeSearchKeywords"])

    def test_novi_duration_is_converted_from_milliseconds(self):
        accepted, reason, meta = clip_scraper.pre_download_candidate_filter({
            "id": "duration-test",
            "video": {"width": 720, "height": 1280, "duration": 47_000},
        })
        self.assertTrue(accepted, reason)
        self.assertEqual(meta["duration"], 47.0)

    def test_hook_like_gate_requires_at_least_twenty_thousand(self):
        base = {"id": "likes-test", "video": {"width": 720, "height": 1280, "duration": 15_000}}
        popular = dict(base, statistics={"digg_count": 25_000})
        accepted, reason, meta = clip_scraper.pre_download_candidate_filter(popular, min_likes=20_000)
        self.assertTrue(accepted, reason)
        self.assertEqual(meta["likes"], 25_000)

        weak = dict(base, id="weak", statistics={"digg_count": 19_999})
        accepted, reason, _meta = clip_scraper.pre_download_candidate_filter(weak, min_likes=20_000)
        self.assertFalse(accepted)
        self.assertIn("below 20,000", reason)

    def test_string_scene_ids_are_normalized_as_whole_numbers(self):
        plan = {"buckets": [{
            "bucket_id": "late_scenes",
            "used_by_scene_ids": "10 11",
            "query_tiers": {"exact": ["会社員 疲れた"]},
        }]}
        scenes = [{"script": f"line {i}"} for i in range(12)]
        with mock.patch.dict(os.environ, {"WAVESPEED_API_KEY": "test-key"}), \
             mock.patch.object(agent_core, "_post_llm_json", return_value=plan):
            result = agent_core.build_social_search_plan("title", "script", scenes)
        self.assertEqual(result["buckets"][0]["used_by_scene_ids"], [10, 11])

    def test_failed_project_can_be_loaded_for_full_rerun(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / "input").mkdir()
            (project / "input" / "script.txt").write_text("failed project script", encoding="utf-8")
            (project / "input" / "run_form.json").write_text(json.dumps({
                "script": "failed project script", "clip_source": "scrape",
                "script_relevancy": "80",
            }), encoding="utf-8")
            state = app.project_form_state(project)
            self.assertEqual(state["loaded_project_mode"], "normal")
            self.assertTrue(state["rerun_failed_project"])
            self.assertEqual(state["clip_source"], "scrape")
            (project / "config").mkdir()
            (project / "config" / "project.json").write_text('{"title":"failed after config"}', encoding="utf-8")
            self.assertEqual(app.project_form_state(project)["loaded_project_mode"], "normal")
            (project / "renders").mkdir()
            (project / "renders" / "finished.mp4").write_bytes(b"video")
            self.assertEqual(app.project_form_state(project)["loaded_project_mode"], "recut_existing_only")
            (project / "review").mkdir()
            (project / "review" / "agent_report.json").write_text(
                '{"needs_media_recut": true}', encoding="utf-8")
            self.assertEqual(app.project_form_state(project)["loaded_project_mode"], "normal")

    def test_existing_failed_project_clips_are_staged_before_new_search(self):
        with tempfile.TemporaryDirectory() as tmp:
            projects = Path(tmp) / "projects"
            current = projects / "konbini_rescue"
            clips = current / "seedance 2.0"
            inputs = current / "input"
            clips.mkdir(parents=True)
            inputs.mkdir(parents=True)
            script = "Japanese convenience stores provide meals, umbrellas, bills and emergency essentials."
            (inputs / "script.txt").write_text(script, encoding="utf-8")
            (clips / "scraped_00.mp4").write_bytes(b"h" * 5000)  # unverified old hook: skipped
            (clips / "scraped_01.mp4").write_bytes(b"b" * 5000)
            with mock.patch.object(agent_core, "PROJECTS_DIR", projects), \
                 mock.patch.dict(os.environ, {"WAVESPEED_API_KEY": ""}):
                body, hooks, meta, report = agent_core.find_reusable_social_clips(
                    current, "Konbini Rescue", script)
            self.assertEqual(len(body), 1)
            self.assertEqual(hooks, [])
            self.assertIn("_existing_reuse", str(body[0]))
            self.assertEqual(len(meta), 1)
            self.assertEqual(report["selected_projects"][0]["slug"], "konbini_rescue")

    def test_candidate_names_do_not_collide_across_tiers(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)

            def fake_search(queries, results_per_query, status_cb=None, sort_type="RELEVANCE"):
                item_id = "id-exact" if queries[0] == "exact query" else "id-broad"
                return [{
                    "id": item_id,
                    "desc": queries[0],
                    "video": {"width": 720, "height": 1280, "duration": 12,
                              "play_addr": {"url_list": ["https://example.invalid/video.mp4"]}},
                }]

            def fake_download(_url, dest, status_cb=None):
                Path(dest).write_bytes(b"x" * 5000)
                return Path(dest)

            def fake_normalize(_raw, final, _ffmpeg, seconds=4.0, start=0.0):
                Path(final).parent.mkdir(parents=True, exist_ok=True)
                Path(final).write_bytes(b"video")
                return Path(final)

            common = (
                mock.patch.object(clip_scraper, "apify_active", return_value=True),
                mock.patch.object(clip_scraper, "apify_search", side_effect=fake_search),
                mock.patch.object(clip_scraper, "_apify_download", side_effect=fake_download),
                mock.patch.object(clip_scraper, "_ffmpeg_tools", return_value=("ffmpeg", "ffprobe")),
                mock.patch.object(clip_scraper, "is_vertical_hq", return_value=True),
                mock.patch.object(clip_scraper, "detect_fake_vertical_or_black_bars",
                                  return_value={"is_fake_vertical": False, "black_bar_score": 0.0,
                                                "reason": "clean vertical"}),
                mock.patch.object(clip_scraper, "text_heaviness_score", return_value=0.0),
                mock.patch.object(clip_scraper, "stable_segment_profile",
                                  return_value={"stable": True, "start": 0.0,
                                                "internal_cut_count": 0,
                                                "rapid_internal_cut_count": 0,
                                                "min_shot_seconds": 4.0}),
                mock.patch.object(clip_scraper, "normalize_clip", side_effect=fake_normalize),
                # The default backend is now a logged-in TikTok session; force it OFF and opt into
                # Apify so this test hermetically exercises the mocked apify_search/download path
                # regardless of whether the dev machine happens to have a TikTok login saved.
                mock.patch.object(clip_scraper, "tiktok_backend_ready", return_value=False),
                mock.patch.dict(os.environ, {"SCRAPE_BACKEND": "apify"}, clear=False),
            )
            with common[0], common[1], common[2], common[3], common[4], common[5], common[6], common[7], common[8], common[9], common[10]:
                first = clip_scraper.scrape_bucket(out_dir, ["exact query"], 1,
                                                   bucket_id="workers", tier="exact")
                second = clip_scraper.scrape_bucket(out_dir, ["broad query"], 1,
                                                    bucket_id="workers", tier="broad")

            self.assertNotEqual(first[0]["path"], second[0]["path"])
            self.assertTrue(first[0]["path"].exists())
            self.assertTrue(second[0]["path"].exists())


if __name__ == "__main__":
    unittest.main()
