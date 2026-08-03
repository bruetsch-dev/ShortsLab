import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import agent_core
import app
import clip_scraper
import pipeline
import scrape_v2
import sfx_agent
import sfx_library
import instagram_login
import tiktok_login
import discovery_short


class TikTokScrapeLogicTests(unittest.TestCase):
    def test_discovery_topic_history_rejects_reused_subject(self):
        history = [{"topic": "Japanese school festival haunted houses"}]
        self.assertTrue(discovery_short._topic_has_been_used(
            "Japanese school festival haunted houses", history))
        self.assertTrue(discovery_short._topic_has_been_used(
            "Japanese school festival haunted house", history))
        self.assertFalse(discovery_short._topic_has_been_used(
            "Unusual Tokyo vending machine meals", history))

    def test_unified_discovery_requires_japan_context_before_download(self):
        self.assertTrue(discovery_short._has_japan_context(
            {"query": "Japanese school festival haunted house", "desc": ""}))
        self.assertTrue(discovery_short._has_japan_context(
            {"query": "文化祭 お化け屋敷", "desc": ""}))
        self.assertFalse(discovery_short._has_japan_context(
            {"query": "realistic fake fried egg", "desc": "sculpture fake food"}))

    def test_selected_discovery_candidate_can_cut_around_isolated_hitches(self):
        # An explicitly selected library video must not be rejected merely because a
        # few single duplicated source frames can be avoided by its recut window.
        start = discovery_short._start_away_from_hitches(0.0, 10.0, 2.0, [0.6, 4.0])
        self.assertGreater(start, 0.6)
        self.assertLessEqual(start + 2.0, 4.0)

    def test_discovery_copy_guard_flags_verbatim_source_narration(self):
        source = ("You cannot say no to anything at this restaurant because the chef spends days "
                  "learning recipes and refuses customers who cannot appreciate his work.")
        copied = ("You cannot say no to anything at this restaurant because the chef spends days "
                  "learning recipes and refuses customers who cannot appreciate his work.")
        fresh = ("This chef plans every course before you arrive. Turning down a plate is not a "
                 "casual choice here, because the entire meal is treated as a personal craft.")
        self.assertGreaterEqual(discovery_short._source_copy_score(copied, source), 0.9)
        self.assertLess(discovery_short._source_copy_score(fresh, source), 0.34)

    def test_tiktok_response_is_bound_to_current_query(self):
        self.assertTrue(tiktok_login._search_response_matches_query(
            "https://www.tiktok.com/api/search/general/full/?keyword=japan%20speed%20dating",
            "japan speed dating"))
        self.assertFalse(tiktok_login._search_response_matches_query(
            "https://www.tiktok.com/api/search/general/full/?keyword=fitness",
            "財布落とした"))
        self.assertFalse(tiktok_login._search_response_matches_query(
            "https://www.tiktok.com/api/challenge/item_list/?challengeID=1",
            "wallet japan", is_tag=False))
        self.assertTrue(tiktok_login._search_response_matches_query(
            "https://www.tiktok.com/api/challenge/item_list/?challengeID=1",
            "#踊ってみた", is_tag=True))

    def test_tiktok_metadata_relevance_rejects_random_feed_but_keeps_dance(self):
        random_feed = {"id": "1", "desc": "fitness workout bikini animal compilation"}
        speed_date = {"id": "2", "desc": "SPEED DATE IN JAPAN with a stranger"}
        dance = {"id": "3", "desc": "可愛い 踊ってみた #ダンス"}
        self.assertEqual(tiktok_login._filter_search_results([random_feed], "財布落とした"), [])
        self.assertEqual(tiktok_login._filter_search_results([speed_date], "japan speed dating"),
                         [speed_date])
        self.assertEqual(tiktok_login._filter_search_results([dance], "可愛い ダンス"), [dance])

    def test_all_tiktok_callers_clean_platform_boilerplate(self):
        self.assertEqual(tiktok_login._sanitize_tiktok_query("女子高校生 TikTok"), "女子高校生")
        self.assertEqual(clip_scraper.sanitize_social_search_query(
            "japan speed dating tiktok reels"), "japan speed dating")

    def test_clip_short_quality_profile_defaults(self):
        import chat_ui
        self.assertAlmostEqual(agent_core.SCRAPE_VOICE_SPEED, 1.10)
        self.assertNotIn("influencer_hook", chat_ui.RUN_MANIFEST["always"])
        self.assertIn("influencer_hook", chat_ui.RUN_MANIFEST["check"])
        self.assertEqual(scrape_v2.match_thresholds_for_relevancy("concrete", 70),
                         {"script_floor": 5.8, "overall": 6.5})
        strict = scrape_v2.match_thresholds_for_relevancy("concrete", 90)
        self.assertGreater(strict["script_floor"], 5.8)
        self.assertGreater(strict["overall"], 6.5)

    def test_v2_validation_accepts_topic_or_optional_influencer_hook(self):
        base = {"voice_speed": 1.10, "scenes": [
            {"clip": "a.mp4", "visual_role": "hook_topic", "match_class": "A_MATCH",
             "assignment_type": "exact", "black_bar_score": 0, "native_9_16": True},
            {"clip": "b.mp4", "visual_role": "body", "match_class": "B_MATCH",
             "assignment_type": "exact", "black_bar_score": 0, "native_9_16": True},
        ]}
        self.assertTrue(scrape_v2.validate_scrape_render_v2(base))
        cute = json.loads(json.dumps(base))
        cute["influencer_hook"] = True
        cute["scenes"][0]["visual_role"] = "hook_influencer"
        self.assertTrue(scrape_v2.validate_scrape_render_v2(cute))

    def test_transition_only_profile_places_no_semantic_sfx(self):
        records = []
        library = {}
        for cat in ("bright_whoosh", "swipe_whoosh", "caption_pop", "ui_click"):
            path = f"{cat}.wav"
            library[cat] = [path]
            records.append({"use_path": path, "file": path, "trim_len": 0.35})
        fake = {"library": library, "records": records, "reactions": {}, "risers": [],
                "hook_risers": [{"path": "hook_riser2.wav", "dur": 2.4}], "report": {}}
        config = {"sfx_enabled": True, "scrape_transition_only_sfx": True,
                  "duration": 8.0, "scenes": [
                      {"start": 0.0, "end": 2.0, "exact_voice_text": "Hook"},
                      {"start": 2.0, "end": 4.0, "exact_voice_text": "Body"},
                      {"start": 4.0, "end": 6.0, "exact_voice_text": "More"},
                  ]}
        with mock.patch.object(sfx_library, "build_library", return_value=fake):
            agent_core.place_editor_sfx(config)
        events = config["ai_content_sfx"]
        self.assertEqual(events[0]["category"], "hook_riser")
        self.assertEqual(events[0]["start"], 0.0)
        self.assertAlmostEqual(events[0]["duration"], 2.0)
        self.assertAlmostEqual(events[0]["source_duration"] / events[0]["playback_rate"], 2.0)
        self.assertTrue(all(e["category"] in {
            "hook_riser", "bright_whoosh", "swipe_whoosh", "caption_pop", "ui_click"
        } for e in events))

    def test_clip_short_riser_uses_full_hook_and_does_not_stack_first_cut(self):
        records = []
        library = {}
        for cat in ("bright_whoosh", "swipe_whoosh", "caption_pop", "ui_click"):
            path = f"{cat}.wav"
            library[cat] = [path]
            records.append({"use_path": path, "file": path, "trim_len": 0.35})
        fake = {"library": library, "records": records, "reactions": {}, "risers": [],
                "hook_risers": [{"path": "hook_riser3.wav", "dur": 2.1}], "report": {}}
        config = {
            "sfx_enabled": True, "scrape_transition_only_sfx": True,
            "hook_riser_full_hook": True, "impact_word": "angry", "duration": 5.0,
            "canonical_words": [{"word": "angry", "start": 0.8, "end": 1.1}],
            "scenes": [
                {"start": 0.0, "end": 2.5, "exact_voice_text": "An angry hook"},
                {"start": 2.5, "end": 5.0, "exact_voice_text": "The body"},
            ],
        }
        with mock.patch.object(sfx_library, "build_library", return_value=fake):
            agent_core.place_editor_sfx(config)
        events = config["ai_content_sfx"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["category"], "hook_riser")
        self.assertEqual(events[0]["start"], 0.0)
        self.assertAlmostEqual(events[0]["duration"], 2.5)

    def test_transition_toggle_renders_planned_riser_when_content_sfx_is_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            riser = Path(tmp) / "riser.wav"
            riser.touch()
            config = {
                "sfx_enabled": True, "sfx_content_enabled": False,
                "transition_sfx_enabled": True, "clip_source": "scrape",
                "scrape_transition_only_sfx": True,
                "duration": 5.0, "scenes": [{"start": 0, "end": 5}],
                "ai_content_sfx": [{"path": str(riser), "start": 0.0, "duration": 2.5,
                                    "source_duration": 2.0, "playback_rate": 0.8,
                                    "volume": 0.2, "category": "hook_riser", "id": "hook"},
                                   {"path": str(riser), "start": 1.0, "duration": 0.4,
                                    "volume": 0.2, "category": "subtle_impacts", "id": "stale"}],
            }
            segments = pipeline.build_sfx_segments(config, has_speech=True)
        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0]["category"], "hook_riser")
        self.assertAlmostEqual(segments[0]["playback_rate"], 0.8)

    def test_v2_queries_are_platform_scoped_and_preserve_x_disambiguator(self):
        intent = scrape_v2.VisualIntent(
            scene_id=3, scene_text="Women were ordered off the sumo ring.",
            subject="women", action="providing emergency aid", location="sumo ring",
            platform_queries={
                "tiktok": {"japanese": ["女性 土俵 tiktok"], "english": []},
                "instagram": {"keywords": ["女性 土俵"], "hashtags": ["#女人禁制"]},
                "twitter": {"japanese": ["女性 土俵 救命"], "english": []},
            })
        rows = scrape_v2.queries_for_intent(intent)
        self.assertTrue(any(row.query == "女性 土俵" and row.platforms == ["tiktok"] for row in rows))
        self.assertTrue(any(row.query == "女性 土俵 救命" and row.platforms == ["twitter"] for row in rows))
        self.assertNotIn("tiktok", " ".join(row.query for row in rows).casefold())
        tag = next(row for row in rows if row.query == "#女人禁制")
        self.assertEqual(tag.platforms, ["instagram"])

    def test_instagram_hashtag_uses_tag_surface(self):
        self.assertEqual(
            instagram_login.instagram_search_url("#名頃かかしの里"),
            "https://www.instagram.com/explore/tags/%E5%90%8D%E9%A0%83%E3%81%8B%E3%81%8B%E3%81%97%E3%81%AE%E9%87%8C/",
        )
        self.assertIn("/explore/search/keyword/?q=", instagram_login.instagram_search_url("名頃 かかし"))

    def test_v2_search_executes_scoped_backend_once_and_does_not_single_token_broaden(self):
        query = scrape_v2.SearchQueryV2(
            query="名頃 かかし", language="ja", tier="exact_action", visual_intent_id="scene_1",
            platforms=["tiktok"])
        state = {}
        with mock.patch.object(scrape_v2.clip_scraper, "backend_search", return_value=[]) as search:
            rows = scrape_v2._search_sources(
                [query], ["tiktok", "twitter", "instagram"], None, time.monotonic() + 20,
                set(), state, None, sort="ALL")
        self.assertEqual(rows, [])
        self.assertEqual(search.call_count, 1)
        self.assertEqual(search.call_args.kwargs["platforms"], ["tiktok"])
        self.assertEqual(search.call_args.kwargs["sort"], "MOST_LIKED")
        self.assertEqual(state.get("broadened_queries", 0), 0)

    def test_v2_metadata_rank_uses_views_as_well_as_likes(self):
        query = scrape_v2.SearchQueryV2(
            query="名頃 かかし", language="ja", tier="exact_action", visual_intent_id="scene_1",
            expected_subject="名頃 かかし")
        low = scrape_v2.SourceVideoCandidate(
            platform="tiktok", source_id="low", creator_id="a", url="", caption="名頃 かかし",
            likes=100, views=1_000, width=720, height=1280)
        viral = scrape_v2.SourceVideoCandidate(
            platform="tiktok", source_id="viral", creator_id="b", url="", caption="名頃 かかし",
            likes=100, views=1_000_000, width=720, height=1280)
        ranked = scrape_v2.rank_metadata_candidates_v2([low, viral], query)
        self.assertEqual(ranked[0].source_id, "viral")

    def test_sidebar_has_subtle_dev_entry_and_dev_page_lists_real_trainers(self):
        shell = app.chat_ui.chat_shell_page({}).decode("utf-8")
        self.assertIn('id="sb-dev"', shell)
        self.assertIn('href="/dev-tools"', shell)
        dev_page = app.dev_tools_page().decode("utf-8")
        self.assertIn("SFX Trainer", dev_page)
        self.assertIn("Scrape Trainer", dev_page)
        self.assertIn("/dev-trainer-open", dev_page)

    def test_transition_add_hitbox_cannot_cover_timeline_sfx(self):
        # Transitions moved OFF the SFX track entirely: the "+" hover slot and the transition marker
        # both render on the CLIP row (tl-trans-on-clip), and a transition sound added via the "+"
        # is flagged is_transition so it is drawn as a clip-boundary diamond, never as an SFX note.
        source = Path(app.__file__).read_text(encoding="utf-8")
        self.assertIn(".tl-trans-slot { position:absolute; top:50%; width:26px; height:26px", source)
        self.assertIn("pointer-events:none", source)
        self.assertIn(".tl-trans.tl-trans-on-clip, .tl-trans-slot.tl-trans-on-clip", source)
        # the "+" slot and the transition marker are appended to the clip track, not the SFX track
        self.assertIn("slot.className='tl-trans-slot tl-trans-on-clip'", source)
        self.assertIn("clipsEl.appendChild(slot);", source)
        self.assertIn("clipsEl.appendChild(node);", source)
        # transition sounds added via the "+" carry the is_transition flag and route off the SFX lane
        self.assertIn("is_transition:!!opts.transition", source)
        self.assertIn("if(fx.is_transition){", source)
        self.assertIn("button.addEventListener('click'", source)

    def test_dev_scrape_trainer_rejects_unknown_run_before_starting_server(self):
        with mock.patch.object(app, "scrape_trainer_runs", return_value=[{"id": "known", "edited": 1}]):
            with self.assertRaisesRegex(RuntimeError, "Unknown or incomplete"):
                app.start_dev_trainer("scrape", "missing")

    def test_dev_sfx_trainer_starts_on_free_local_port(self):
        key = ("sfx", "")
        app.DEV_TRAINER_SERVERS.pop(key, None)
        url = app.start_dev_trainer("sfx")
        server, thread, saved_url = app.DEV_TRAINER_SERVERS[key]
        try:
            self.assertEqual(url, saved_url)
            self.assertRegex(url, r"^http://127\.0\.0\.1:\d+/$")
            self.assertTrue(thread.is_alive())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
            app.DEV_TRAINER_SERVERS.pop(key, None)

    def test_hook_riser_chooses_nearest_duration_and_fills_zero_to_climax(self):
        pool = [
            {"path": "short.wav", "dur": 1.1},
            {"path": "near.wav", "dur": 2.8},
            {"path": "long.wav", "dur": 5.4},
            {"path": "longest.wav", "dur": 8.0},
        ]
        item, source_duration, rate = sfx_library.choose_riser_for_target(pool, 3.0)
        self.assertEqual(item["path"], "near.wav")
        self.assertAlmostEqual(source_duration, 2.8)
        self.assertAlmostEqual(source_duration / rate, 3.0)

        # resolve_vision_segments trusts the SCANNED library first, so mock it to the fake pool.
        # The fake .wav files don't exist on disk, so build_progressive_riser returns None and the
        # riser keeps the plain uniform-rate path (source_duration / playback_rate == target).
        fake_lib = {"library": {}, "hook_risers": pool, "reactions": {}}
        with mock.patch.object(sfx_library, "build_library", return_value=fake_lib):
            segments = sfx_agent.resolve_vision_segments(
                [{"timestamp": 0.0, "end_timestamp": 3.0, "sfx_type": "hook_riser",
                  "trigger_detail": "hook climax", "reasoning": ""}],
                fake_lib, duration=20.0, ffprobe=None)
            riser = segments[0]
            self.assertEqual(riser["start"], 0.0)
            self.assertEqual(riser["duration"], 3.0)
            self.assertEqual(str(riser["path"]), "near.wav")
            self.assertAlmostEqual(riser["source_duration"] / riser["playback_rate"], 3.0, places=4)

            late = sfx_agent.resolve_vision_segments(
                [{"timestamp": 0.0, "end_timestamp": 9.0, "sfx_type": "hook_riser",
                  "trigger_detail": "late hook climax", "reasoning": ""}],
                fake_lib, duration=20.0, ffprobe=None)[0]
            self.assertEqual(late["start"], 0.0)
            self.assertEqual(late["duration"], 9.0)  # explicit timestamp remains authoritative
            self.assertEqual(str(late["path"]), "longest.wav")

    def test_atempo_chain_supports_stretching_and_speeding_up(self):
        self.assertEqual(pipeline.atempo_filter_chain(1.0), "")
        self.assertEqual(pipeline.atempo_filter_chain(2.8 / 3.0), "atempo=0.933333")
        fast = pipeline.atempo_filter_chain(5.0)
        self.assertEqual(fast, "atempo=2.000000,atempo=2.000000,atempo=1.250000")

    def test_visual_repair_replaces_generic_transition_and_rejects_audio_events(self):
        initial = [
            {"timestamp": 8.0, "sfx_type": "transition", "trigger_source": "visual",
             "trigger_detail": "cut", "reasoning": ""},
            {"timestamp": 16.0, "sfx_type": "reaction: money_cash", "trigger_source": "audio",
             "trigger_detail": "money spoken", "reasoning": ""},
        ]
        repair = [
            {"timestamp": 8.05, "sfx_type": "reaction: camera_photo", "trigger_source": "visual",
             "trigger_detail": "camera flash fires", "reasoning": "visible flash"},
            {"timestamp": 22.0, "sfx_type": "reaction: sad_downer", "trigger_source": "audio",
             "trigger_detail": "sad word", "reasoning": "audio keyword"},
        ]
        tags = ["transition", "reaction: camera_photo", "reaction: sad_downer",
                "reaction: money_cash"]
        merged, added = sfx_agent.merge_visual_repair_events(initial, repair, tags)
        self.assertEqual(added, 1)
        self.assertFalse(any(sfx_agent._tag_key(row["sfx_type"]) == "transition" for row in merged))
        self.assertTrue(any(sfx_agent._tag_key(row["sfx_type"]) == "reaction:camera_photo"
                            for row in merged))
        self.assertFalse(any(float(row["timestamp"]) == 22.0 for row in merged))
        self.assertEqual(sfx_agent.visual_semantic_target(53.55, "medium"), 7)
        prompt = sfx_agent.build_visual_repair_prompt(tags, 3, initial)
        self.assertIn("IGNORE transcript keywords", prompt)
        self.assertIn("Do NOT return generic cuts", prompt)

    def test_audio_director_runs_visual_repair_when_first_pass_is_audio_heavy(self):
        initial_json = {"choices": [{"message": {"content": json.dumps({"sfx_timeline": [
            {"timestamp": 0.0, "end_timestamp": 2.0, "sfx_type": "hook_riser",
             "trigger_source": "audio"},
            {"timestamp": 2.0, "sfx_type": "impact", "trigger_source": "visual"},
            {"timestamp": 8.0, "sfx_type": "transition", "trigger_source": "visual"},
            {"timestamp": 12.0, "sfx_type": "reaction: money_cash", "trigger_source": "audio"},
        ]})}}]}
        repair_json = {"choices": [{"message": {"content": json.dumps({"sfx_timeline": [
            {"timestamp": 8.05, "sfx_type": "reaction: camera_photo", "trigger_source": "visual"},
            {"timestamp": 15.0, "sfx_type": "reaction: cute_aww", "trigger_source": "visual"},
            {"timestamp": 20.0, "sfx_type": "reaction: question", "trigger_source": "visual"},
        ]})}}]}
        tags = ["hook_riser", "impact", "transition", "reaction: money_cash",
                "reaction: camera_photo", "reaction: cute_aww", "reaction: question"]
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.dict(os.environ, {"WAVESPEED_API_KEY": "test-key"}), \
             mock.patch.object(sfx_agent, "prepare_audio_director_proxy",
                               return_value=(Path(tmp) / "proxy.mp4", Path(tmp) / "proxy")), \
             mock.patch.object(sfx_agent, "upload_audio_director_media",
                               return_value=("https://media.example/proxy.mp4", {})), \
             mock.patch.object(sfx_agent.agent_core, "post_json_url",
                               side_effect=[initial_json, repair_json]) as post:
            events = sfx_agent.plan_sfx_with_vision(
                Path(tmp) / "source.mp4", 24.0, [], [], tags=tags, sfx_amount="medium")
        self.assertEqual(post.call_count, 2)
        self.assertEqual(sum(1 for row in events if sfx_agent.is_visual_semantic_event(row)), 3)
        self.assertFalse(any(sfx_agent._tag_key(row["sfx_type"]) == "transition"
                             and abs(float(row["timestamp"]) - 8.0) < 0.3 for row in events))

    def test_audio_director_proxy_is_480p_and_keeps_aac_audio(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.mp4"
            source.write_bytes(b"source-video")

            def fake_ffmpeg(command, **_kwargs):
                Path(command[-1]).write_bytes(b"proxy-video")
                return mock.Mock(returncode=0, stderr="")

            with mock.patch.object(sfx_agent.pipeline, "find_ffmpeg", return_value="ffmpeg"), \
                 mock.patch.object(sfx_agent.subprocess, "run", side_effect=fake_ffmpeg) as run:
                proxy, proxy_dir = sfx_agent.prepare_audio_director_proxy(source)
            try:
                command = run.call_args.args[0]
                self.assertIn("scale=480:-2", command)
                self.assertIn("0:a:0?", command)
                self.assertIn("aac", command)
                self.assertTrue(proxy.exists())
            finally:
                import shutil
                shutil.rmtree(proxy_dir, ignore_errors=True)

    def test_audio_director_upload_retries_connection_failures_three_times(self):
        failures = [ConnectionResetError(10054, "reset"), TimeoutError("timeout")]
        with mock.patch.object(sfx_agent.pipeline, "upload_media",
                               side_effect=failures + [("https://media.example/proxy.mp4", {})]) as upload, \
             mock.patch.object(sfx_agent.time, "sleep") as sleep:
            result = sfx_agent.upload_audio_director_media(
                Path("proxy.mp4"), "test-key", attempts=3)
        self.assertEqual(result[0], "https://media.example/proxy.mp4")
        self.assertEqual(upload.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [2, 4])

    def test_planning_text_does_not_duplicate_narration(self):
        scene = {"script": "This is the spoken line.", "visual_script": "show a train"}
        self.assertEqual(
            agent_core.scene_text_for_planning(scene),
            "This is the spoken line. show a train",
        )

    def test_untimed_visual_notes_follow_local_claim_not_uniform_scene_index(self):
        scenes = [
            {"start": 0.0, "end": 2.0, "script": "Women are banned from entering the sumo ring."},
            {"start": 2.0, "end": 10.0, "script": "Office workers endure painful heels all day."},
            {"start": 10.0, "end": 12.0, "script": "Some sales staff were forbidden to wear glasses."},
        ]
        # Deliberately not ordered like the narration: semantic anchors must beat uniform spreading.
        visual_notes = """Woman stopped beside a sumo ring.
Sales worker removes her glasses before serving a customer.
Office worker rubs painful feet after taking off heels."""
        mapped = agent_core.apply_visual_script_to_scenes(scenes, visual_notes, 12.0)
        self.assertIn("sumo ring", mapped[0]["visual_script"])
        self.assertIn("painful feet", mapped[1]["visual_script"])
        self.assertIn("glasses", mapped[2]["visual_script"])

    def test_explicit_visual_timestamps_still_map_by_overlap(self):
        scenes = [
            {"start": 0.0, "end": 5.0, "script": "first"},
            {"start": 5.0, "end": 10.0, "script": "second"},
        ]
        mapped = agent_core.apply_visual_script_to_scenes(
            scenes, "00:00 - 00:05 show red apples\n00:05 - 00:10 show yellow bananas", 10.0)
        self.assertIn("apples", mapped[0]["visual_script"])
        self.assertIn("bananas", mapped[1]["visual_script"])

    def test_semantic_threshold_relaxes_after_each_failed_round(self):
        # At 80% relevancy the first pass starts moderately strict (6.0/10) and relaxes toward the
        # 4.0 floor on each failed round, so abstract scripts still reach a threshold the pool can
        # meet (was 7.0->4.5; lowered so 80% no longer rejects every clip).
        thresholds = [agent_core.adaptive_script_match_threshold(80, attempt) for attempt in range(4)]
        self.assertEqual(thresholds, [6.0, 5.0, 4.0, 4.0])
        # monotonically non-increasing and never below the 4.0 floor
        self.assertTrue(all(a >= b for a, b in zip(thresholds, thresholds[1:])))
        self.assertGreaterEqual(min(thresholds), 4.0)

    def test_final_semantic_rescore_never_repeats_the_same_floor_pass(self):
        # Regression: a cancelled run matched 10/21 at 4.0, then rescored all 84 clips at
        # the same 4.0 and hit the 45-minute watchdog.
        self.assertFalse(agent_core.should_run_final_semantic_rescore(
            4.0, 4.0, remaining_count=11, pool_count=84,
            any_matched=True, scene_count=21))
        self.assertFalse(agent_core.should_run_final_semantic_rescore(
            5.8, 4.0, remaining_count=21, pool_count=72,
            any_matched=False, scene_count=21))
        self.assertTrue(agent_core.should_run_final_semantic_rescore(
            5.8, 4.0, remaining_count=8, pool_count=30,
            any_matched=True, scene_count=21))

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

    def test_twitter_items_normalize_and_scaled_like_gate(self):
        import twitter_login
        payload = {"data": {"instructions": [{"entries": [{"content": {"tweet_results": {"result": {
            "core": {"user_results": {"result": {"legacy": {"screen_name": "trainwatcher"}}}},
            "legacy": {
                "id_str": "1234567890",
                "full_text": "rush hour #train",
                "favorite_count": 3200,
                "entities": {"hashtags": [{"text": "train"}]},
                "extended_entities": {"media": [{
                    "type": "video",
                    "original_info": {"width": 720, "height": 1280},
                    "video_info": {"duration_millis": 21000},
                }]},
            }}}}}]}]}}
        items = twitter_login._extract_video_tweets(payload)
        self.assertEqual(len(items), 1)
        it = items[0]
        self.assertEqual(it["_source"], "twitter_login")
        self.assertIn("x.com/trainwatcher/status/1234567890", it["webVideoUrl"])
        ok, reason, m = clip_scraper.pre_download_candidate_filter(it, "", set())
        self.assertTrue(ok, reason)
        self.assertEqual(m["platform"], "twitter")
        self.assertEqual(m["duration"], 21.0)
        # X likes are scaled ~4x for ranking, so the same count ranks HIGHER on twitter
        tik = dict(it)
        tik.pop("_platform")
        _ok2, _r2, m2 = clip_scraper.pre_download_candidate_filter(tik, "", set())
        self.assertGreater(m["rank_score"], m2["rank_score"])

    def test_backend_search_merges_both_platforms(self):
        import concurrent.futures
        tw_item = {"id": "tw1", "_source": "twitter_login", "_platform": "twitter",
                   "stats": {"diggCount": 40_000},
                   "webVideoUrl": "https://x.com/u/status/tw1"}
        tk_item = {"id": "tk1", "statistics": {"diggCount": 20_000},
                   "webVideoUrl": "https://www.tiktok.com/@u/video/tk1"}
        fut = concurrent.futures.Future()
        fut.set_result([tw_item])

        # TikTok searches now run on tiktok_login's dedicated worker thread via search_sync
        with mock.patch.object(clip_scraper, "tiktok_backend_ready", return_value=True), \
             mock.patch.object(clip_scraper, "twitter_backend_ready", return_value=True), \
             mock.patch.object(clip_scraper.twitter_login, "search_async", return_value=fut), \
             mock.patch.object(clip_scraper, "_ensure_tiktok_cookies", return_value=True), \
             mock.patch.object(clip_scraper.tiktok_login, "search_sync", return_value=[tk_item]), \
             mock.patch.object(clip_scraper, "_merge_backend_cookies", return_value=None):
            items = clip_scraper.backend_search("query", 8)
        self.assertEqual([i["id"] for i in items], ["tw1", "tk1"])

    def test_backend_search_forwards_user_sort_to_x(self):
        import concurrent.futures
        future = concurrent.futures.Future()
        future.set_result([])
        with mock.patch.object(clip_scraper, "twitter_backend_ready", return_value=True), \
             mock.patch.object(clip_scraper.twitter_login, "search_async",
                               return_value=future) as search:
            clip_scraper.backend_search(
                "Tokyo station incident", 8, sort="MOST_VIEWED", platforms=["x"])
        self.assertEqual(search.call_args.kwargs["sort"], "MOST_VIEWED")

    def test_download_selection_uses_global_rank_not_forced_platform_balance(self):
        scored = []
        for index in range(8):
            scored.append((10.0 - index, {"id": f"tk{index}"},
                           {"platform": "tiktok"}, f"tk{index}"))
        for index in range(3):
            scored.append((7.0 - index, {"id": f"x{index}"},
                           {"platform": "twitter"}, f"x{index}"))
        scored.sort(key=lambda row: row[0], reverse=True)
        selected = clip_scraper.balanced_platform_candidates(
            scored, 6, {"tiktok", "twitter"})
        self.assertEqual([row[2]["platform"] for row in selected],
                         ["tiktok", "tiktok", "tiktok", "tiktok", "twitter", "tiktok"])

    def test_query_normalization_preserves_complete_phrases(self):
        self.assertEqual(
            clip_scraper.normalize_query_list("Japanese students cleaning classroom"),
            ["Japanese students cleaning classroom"],
        )
        self.assertEqual(
            clip_scraper.normalize_query_list('["night train commuters", "駅 終電"]'),
            ["night train commuters", "駅 終電"],
        )
        self.assertEqual(
            clip_scraper.normalize_query_list("Tokyo vending machine · konbini breakfast"),
            ["Tokyo vending machine", "konbini breakfast"],
        )
        self.assertEqual(
            clip_scraper.normalize_query_list(["Tokyo train", "tokyo train", "Tokyo train"]),
            ["Tokyo train"],
        )

    def test_x_query_validation_rejects_script_fragments_but_accepts_visual_phrases(self):
        self.assertFalse(clip_scraper.is_valid_x_query("First;"))
        self.assertFalse(clip_scraper.is_valid_x_query("for"))
        self.assertTrue(clip_scraper.is_valid_x_query("Japanese students cleaning classroom"))
        self.assertTrue(clip_scraper.is_valid_x_query("終電 サラリーマン"))
        # Bucket terms may be raw narration and must never become a backend query.
        self.assertEqual(
            clip_scraper.x_queries_for_search(
                ["Japanese students cleaning classroom"],
                bucket_terms="First, I will explain why you love it.",
                search_intent="specific_action",
            ),
            ["Japanese students cleaning classroom"],
        )

    def test_five_valid_x_zero_searches_trigger_one_health_check_across_buckets(self):
        clip_scraper.reset_backend_search_health()

    def test_three_empty_instagram_searches_trigger_health_check_and_disable_backend(self):
        import concurrent.futures
        clip_scraper.reset_backend_search_health()

        def empty_future(*_args, **_kwargs):
            future = concurrent.futures.Future()
            future.set_result([])
            return future

        with mock.patch.object(clip_scraper, "instagram_backend_ready", return_value=True), \
             mock.patch.object(clip_scraper, "tiktok_backend_ready", return_value=False), \
             mock.patch.object(clip_scraper, "twitter_backend_ready", return_value=False), \
             mock.patch.object(clip_scraper.instagram_login, "search_async", side_effect=empty_future) as search, \
             mock.patch.object(clip_scraper.instagram_login, "health_check",
                               return_value={"ok": False}) as health:
            for query in ("#名頃かかしの里", "#お見舞いマナー", "#すっぴんメイク"):
                self.assertEqual(clip_scraper.backend_search(query, 5, platforms=["instagram"]), [])
            self.assertEqual(clip_scraper.backend_search("#japan", 5, platforms=["instagram"]), [])
        self.assertEqual(search.call_count, 3)
        health.assert_called_once()
        clip_scraper.reset_backend_search_health()
        health = mock.Mock(return_value={"ok": False})
        common = (
            mock.patch.object(clip_scraper, "backend_active", return_value=True),
            mock.patch.object(clip_scraper, "_ffmpeg_tools", return_value=("ffmpeg", "ffprobe")),
            mock.patch.object(clip_scraper, "backend_search", return_value=[]),
            mock.patch.object(clip_scraper.twitter_login, "health_check", health),
        )
        with common[0], common[1], common[2], common[3]:
            clip_scraper.scrape_bucket(
                Path(tempfile.gettempdir()) / "x-query-test-a",
                ["Japan train delay", "Tokyo station incident", "commuter reaction video"],
                1, platforms=["x"], search_intent="specific_action")
            clip_scraper.scrape_bucket(
                Path(tempfile.gettempdir()) / "x-query-test-b",
                ["students cleaning classroom", "vending machine restocking", "train passenger event"],
                1, platforms=["x"], search_intent="specific_action")
        health.assert_called_once()
        clip_scraper.reset_backend_search_health()

    def test_invalid_x_queries_are_reported_without_backend_calls(self):
        clip_scraper.reset_backend_search_health()
        perf = []
        with mock.patch.object(clip_scraper, "backend_active", return_value=True), \
             mock.patch.object(clip_scraper, "backend_search") as search:
            result = clip_scraper.scrape_bucket(
                Path(tempfile.gettempdir()) / "x-invalid-query-test",
                ["First;", "for"], 1, platforms=["x"], query_perf=perf,
                search_intent="specific_action")
        self.assertEqual(result, [])
        search.assert_not_called()
        self.assertEqual([row["status"] for row in perf],
                         ["invalid_query", "invalid_query"])

    def test_tiktok_and_x_receive_separate_query_sets(self):
        clip_scraper.reset_backend_search_health()
        calls = []
        def search(query, _want, **kwargs):
            calls.append((next(iter(kwargs["platforms"])), query))
            return []
        with mock.patch.object(clip_scraper, "backend_active", return_value=True), \
             mock.patch.object(clip_scraper, "_ffmpeg_tools", return_value=("ffmpeg", "ffprobe")), \
             mock.patch.object(clip_scraper, "backend_search", side_effect=search):
            clip_scraper.scrape_bucket(
                Path(tempfile.gettempdir()) / "separate-social-query-test",
                ["かわいい 東京 日常"], 1, platforms=["tiktok", "x"],
                search_intent="specific_action",
                x_queries=["Tokyo station passenger incident"])
        self.assertIn(("tiktok", "かわいい 東京 日常"), calls)
        self.assertIn(("twitter", "Tokyo station passenger incident"), calls)
        self.assertNotIn(("twitter", "かわいい 東京 日常"), calls)
        clip_scraper.reset_backend_search_health()

    def test_body_has_no_like_floor_but_hook_keeps_twenty_thousand(self):
        self.assertEqual(agent_core.MIN_CLIP_LIKES, 0)
        self.assertEqual(agent_core.HOOK_MIN_LIKES, 20_000)

    def test_timeline_ui_exposes_smaller_player_rework_model_and_search_sort(self):
        self.assertIn("height:clamp(260px,38vh,390px)", app.TIMELINE_SKELETON)
        self.assertIn('id="tl-rw-model"', app.TIMELINE_SKELETON)
        page_html = app.form_page(clear=True)
        self.assertIn('name="scrape_sort"', page_html)
        self.assertIn('value="MOST_VIEWED"', page_html)

    def test_caption_gate_rejects_instead_of_blurring(self):
        self.assertFalse(clip_scraper.is_captioned_candidate(1.5, False))
        # Lots of natural OCR (e.g. vending-machine labels) is not a creator caption.
        self.assertFalse(clip_scraper.is_captioned_candidate(8.0, False))
        self.assertFalse(clip_scraper.is_captioned_candidate(0.3, True))
        self.assertTrue(clip_scraper.is_captioned_candidate(0.8, True))

    def test_legacy_caption_blur_mask_contains_glyphs_not_ocr_rectangle(self):
        if clip_scraper.cv2 is None or clip_scraper.np is None:
            self.skipTest("OpenCV unavailable")
        cv2, np = clip_scraper.cv2, clip_scraper.np
        frame = np.full((180, 320, 3), 55, dtype=np.uint8)
        cv2.putText(frame, "CAPTION", (42, 105), cv2.FONT_HERSHEY_SIMPLEX,
                    1.15, (255, 255, 255), 3, cv2.LINE_AA)
        box = (35, 68, 220, 52)
        mask = clip_scraper._caption_text_stroke_mask(
            [frame], [[box]], allowed_regions=[box])
        self.assertIsNotNone(mask)
        x, y, w, h = box
        coverage = float(np.count_nonzero(mask[y:y + h, x:x + w])) / float(w * h)
        self.assertGreater(coverage, 0.02)
        self.assertLess(coverage, 0.55)  # regression: old implementation masked the full box
        self.assertEqual(int(np.count_nonzero(mask[:40, :])), 0)

    def test_raw_tiktok_duration_is_converted_from_milliseconds(self):
        accepted, reason, meta = clip_scraper.pre_download_candidate_filter({
            "id": "duration-test",
            "video": {"width": 720, "height": 1280, "duration": 47_000},
        })
        self.assertTrue(accepted, reason)
        self.assertEqual(meta["duration"], 47.0)

    def test_minimum_likes_are_hard_gate_and_popularity_still_ranks(self):
        base = {"id": "likes-test", "video": {"width": 720, "height": 1280, "duration": 15_000}}
        popular = dict(base, statistics={"digg_count": 25_000})
        ok_p, reason_p, meta_p = clip_scraper.pre_download_candidate_filter(popular, min_likes=20_000)
        self.assertTrue(ok_p, reason_p)
        self.assertEqual(meta_p["likes"], 25_000)

        medium = dict(base, id="medium", statistics={"digg_count": 21_000})
        ok_m, reason_m, meta_m = clip_scraper.pre_download_candidate_filter(
            medium, min_likes=20_000)
        self.assertTrue(ok_m, reason_m)
        self.assertGreater(meta_p["rank_score"], meta_m["rank_score"])

        weak = dict(base, id="weak", statistics={"digg_count": 12})
        ok_w, reason_w, meta_w = clip_scraper.pre_download_candidate_filter(weak, min_likes=20_000)
        self.assertFalse(ok_w)
        self.assertIn("below minimum likes", reason_w)

        # X uses a quarter of TikTok's absolute floor because its engagement counts are lower.
        x_item = dict(base, id="x-medium", _platform="twitter",
                      stats={"diggCount": 3_000})
        ok_x, reason_x, meta_x = clip_scraper.pre_download_candidate_filter(
            x_item, min_likes=10_000)
        self.assertTrue(ok_x, reason_x)
        self.assertEqual(meta_x["effective_min_likes"], 2_500)
        self.assertEqual(clip_scraper.platform_like_floor(20_000, "twitter"), 5_000)
        self.assertEqual(clip_scraper.platform_like_floor(20_000, "tiktok"), 20_000)
        x_weak = dict(base, id="x-weak", _platform="twitter",
                      stats={"diggCount": 2_499})
        ok_xw, reason_xw, _meta_xw = clip_scraper.pre_download_candidate_filter(
            x_weak, min_likes=10_000)
        self.assertFalse(ok_xw)
        self.assertIn("2,500", reason_xw)

    def test_backend_search_respects_selected_platform_and_deadline(self):
        tk_item = {"id": "tk1", "statistics": {"digg_count": 30_000},
                   "webVideoUrl": "https://www.tiktok.com/@u/video/tk1"}

        with mock.patch.object(clip_scraper, "tiktok_backend_ready", return_value=True), \
             mock.patch.object(clip_scraper, "twitter_backend_ready", return_value=True), \
             mock.patch.object(clip_scraper, "_ensure_tiktok_cookies", return_value=True), \
             mock.patch.object(clip_scraper.tiktok_login, "search_sync", return_value=[tk_item]), \
             mock.patch.object(clip_scraper.twitter_login, "search_async") as x_search:
            items = clip_scraper.backend_search("query", 8, platforms=["tiktok"])
        self.assertEqual([item["id"] for item in items], ["tk1"])
        x_search.assert_not_called()

        expired = clip_scraper.backend_search(
            "query", 8, platforms=["tiktok"], deadline=time.monotonic() - 1)
        self.assertEqual(expired, [])

    def test_fuzzy_metadata_is_penalty_not_reject(self):
        item = {"id": "promo-test", "desc": "sponsored オーディション ニュース",
                "video": {"width": 720, "height": 1280, "duration": 15_000},
                "statistics": {"digg_count": 500}}
        ok, reason, meta = clip_scraper.pre_download_candidate_filter(item)
        self.assertTrue(ok, reason)
        self.assertGreater(meta["penalty"], 0)
        self.assertIn("possible_promo_ad", meta["penalty_flags"])
        # self-labeled AI content stays a HARD reject
        ai = {"id": "ai-test", "desc": "made with ai #aiart",
              "video": {"width": 720, "height": 1280, "duration": 15_000}}
        ok2, reason2, _m = clip_scraper.pre_download_candidate_filter(ai)
        self.assertFalse(ok2)
        self.assertIn("ai-generated", reason2)

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

    def test_media_sidebar_scrape_progress_reports_live_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / "input").mkdir()
            (project / "input" / "run_form.json").write_text(
                '{"clip_source":"scrape"}', encoding="utf-8")
            candidates = project / "seedance 2.0" / "_candidates" / "cafe"
            raw = candidates / "_raw"
            declined = project / "seedance 2.0" / "_declined"
            raw.mkdir(parents=True)
            declined.mkdir(parents=True)
            (candidates / "candidate.mp4").write_bytes(b"video")
            (raw / "download.mp4.part").write_bytes(b"partial")
            (declined / "declined.mp4").write_bytes(b"video")
            html = app.scrape_progress_html(project, {
                "status": "running",
                "logs": [
                    "Built 3 social search bucket(s) + a hook-influencer bucket from the script:",
                    "Bucket cafe: final candidate pool 1 clip(s).",
                    "  [street · exact] searching tiktok, x: 渋谷 夜景",
                ],
            })
            self.assertIn("Search / scrape", html)
            self.assertIn("1 / 4 buckets", html)
            self.assertIn("1 usable", html)
            self.assertIn("1 declined", html)
            self.assertIn("1 processing", html)

    def test_running_media_panel_shows_platform_accepted_and_assigned(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            tk = project / "seedance 2.0" / "_candidates" / "food" / "cand_tk.mp4"
            tw = project / "seedance 2.0" / "_candidates" / "train" / "cand_x.mp4"
            assigned = project / "seedance 2.0" / "scraped_00.mp4"
            for path in (tk, tw, assigned):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"video")
            tk.with_suffix(".json").write_text('{"platform":"tiktok"}', encoding="utf-8")
            tw.with_suffix(".json").write_text('{"platform":"twitter"}', encoding="utf-8")
            items = app.project_media_files(project)
            kinds = {path.name: kind for kind, path in items}
            self.assertEqual(kinds["cand_tk.mp4"], "accepted_tiktok")
            self.assertEqual(kinds["cand_x.mp4"], "accepted_twitter")
            self.assertEqual(kinds["scraped_00.mp4"], "assigned")
            html = app.media_tabs_html(items, removal_job_id="job-1")
            self.assertIn("TikTok accepted", html)
            self.assertIn("X accepted", html)
            self.assertIn("Assigned", html)
            self.assertEqual(html.count('class="media-del media-remove"'), 3)

    def test_manual_assigned_exclusion_gets_replaced_before_render(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            clip_dir = project / "seedance 2.0"
            candidate = clip_dir / "_candidates" / "station" / "cand_new.mp4"
            old_source = clip_dir / "_candidates" / "station" / "cand_old.mp4"
            assigned = clip_dir / "scraped_00.mp4"
            candidate.parent.mkdir(parents=True)
            candidate.write_bytes(b"new-video")
            old_source.write_bytes(b"old-video")
            assigned.write_bytes(b"old-video")
            candidate.with_suffix(".json").write_text(
                '{"platform":"twitter","clip_id":"new-x"}', encoding="utf-8")
            assigned.with_suffix(".json").write_text(json.dumps({
                "source_path": str(old_source.resolve()), "platform": "tiktok",
            }), encoding="utf-8")
            config = {"clip_source": "scrape", "scenes": [{
                "id": "01", "clip": "scraped_00.mp4", "start": 0, "end": 2,
            }]}
            form = {"_media_exclusions": {str(old_source.resolve()).lower()}}
            changed = agent_core.reconcile_manual_scrape_exclusions(config, project, form)
            self.assertEqual(changed, 1)
            self.assertEqual(assigned.read_bytes(), b"new-video")
            self.assertEqual(config["scenes"][0]["scrape_source"], "twitter")
            self.assertEqual(config["scenes"][0]["scrape_clip_id"], "new-x")

    def test_timeline_overlay_move_scale_delete_round_trips_to_renderer(self):
        config = {
            "project_slug": "overlay-test", "smart_overlays": False,
            "scenes": [{"id": "s1", "start": 0.0, "end": 2.0,
                        "overlays": [{"id": "arrow-1", "type": "callout", "shape": "arrow",
                                      "cx": 0.3, "cy": 0.4}]}],
        }
        edits = {"overlays": [{"scene_id": "s1", "items": [{
            "id": "arrow-1", "type": "callout", "shape": "arrow",
            "cx": 0.3, "cy": 0.4, "editor_x": 0.72,
            "editor_y": 0.61, "editor_scale": 1.8, "editor_rotation": 37,
            "arrow_style": "yellow_sticker_arrow", "animation": "bounce",
            "animation_duration": 0.45,
        }]}]}
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(agent_core, "PROJECTS_DIR", Path(tmp)):
            out = agent_core.apply_timeline_edits_to_config(config, edits, "overlay-test")
        saved = out["scenes"][0]["overlays"][0]
        self.assertEqual(saved["editor_x"], 0.72)
        self.assertEqual(saved["editor_y"], 0.61)
        self.assertEqual(saved["editor_scale"], 1.8)
        self.assertEqual(saved["editor_rotation"], 37)
        self.assertEqual(saved["arrow_style"], "yellow_sticker_arrow")
        self.assertEqual(saved["animation"], "bounce")
        self.assertTrue(out["smart_overlays"])
        transformed = pipeline.apply_overlay_editor_transform(saved)
        self.assertEqual(transformed["cx"], 0.72)
        self.assertEqual(transformed["cy"], 0.61)

        # Sending an empty list is the editor's delete operation.
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(agent_core, "PROJECTS_DIR", Path(tmp)):
            deleted = agent_core.apply_timeline_edits_to_config(
                out, {"overlays": [{"scene_id": "s1", "items": []}]}, "overlay-test")
        self.assertEqual(deleted["scenes"][0]["overlays"], [])
        self.assertFalse(deleted["smart_overlays"])
        self.assertTrue(deleted["timeline_overlays_managed"])

    def test_overlay_appearance_sound_is_mixed_at_visual_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            sound = Path(tmp) / "pop.wav"
            sound.write_bytes(b"RIFF-test")
            config = {
                "sfx_enabled": False,
                "scenes": [{"id": "s1", "start": 2.0, "end": 6.0, "overlays": [{
                    "id": "arrow-1", "type": "callout", "start": 0.25,
                    "appear_sfx_path": str(sound), "appear_sfx_volume": 0.31,
                    "appear_sfx_duration": 0.7,
                }]}],
            }
            segments = pipeline.build_sfx_segments(config, has_speech=True)
        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0]["start"], 3.0)
        self.assertEqual(segments[0]["volume"], 0.31)
        self.assertEqual(segments[0]["category"], "overlay_appearance")

    def test_arrow_style_palettes_render_different_pixels(self):
        red, _tip1 = pipeline.build_arrow_sprite(
            140, 18, 0.0, style="default_thick_red_arrow")
        yellow, _tip2 = pipeline.build_arrow_sprite(
            140, 18, 0.0, style="yellow_sticker_arrow")
        self.assertEqual(red.size, yellow.size)
        self.assertNotEqual(red.tobytes(), yellow.tobytes())
        rotated = pipeline.apply_overlay_editor_transform({
            "type": "arrows", "items": [[0.2, 0.5, 0.8, 0.5]],
            "editor_rotation": 90, "editor_scale": 1,
        })["items"][0]
        self.assertAlmostEqual(rotated[0], rotated[2], places=5)
        self.assertLess(rotated[1], rotated[3])

    def test_old_project_arrows_are_recovered_from_agent_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            projects = Path(tmp) / "projects"
            project = projects / "legacy-arrows"
            (project / "config").mkdir(parents=True)
            (project / "review").mkdir()
            (project / "config" / "project.json").write_text(json.dumps({
                "smart_overlays": False,
                "scenes": [
                    {"id": "01", "start": 0.0, "end": 2.0},
                    {"id": "02", "start": 2.0, "end": 4.0},
                ],
            }), encoding="utf-8")
            (project / "review" / "agent_report.json").write_text(json.dumps({
                "scene_visual_fx": [
                    {"scene_id": 0, "callout_enabled": False, "callout_type": "none"},
                    {"scene_id": 1, "callout_enabled": True, "callout_type": "arrow"},
                ],
            }), encoding="utf-8")
            with mock.patch.object(agent_core, "PROJECTS_DIR", projects):
                config = agent_core.load_project_config("legacy-arrows")
            self.assertFalse(config["scenes"][0].get("overlays"))
            self.assertEqual(config["scenes"][1]["overlays"][0]["type"], "callout")
            self.assertTrue(config["smart_overlays"])
            self.assertEqual(config["_legacy_overlays_recovered"], 1)

    def test_timeline_save_survives_generated_config_replacement(self):
        with tempfile.TemporaryDirectory() as tmp:
            projects = Path(tmp) / "projects"
            project = projects / "durable-timeline"
            (project / "config").mkdir(parents=True)
            original = {
                "project_slug": "durable-timeline", "smart_overlays": False,
                "scenes": [{"id": "01", "start": 0.0, "end": 2.0}],
            }
            config_path = project / "config" / "project.json"
            config_path.write_text(json.dumps(original), encoding="utf-8")
            edits = {
                "scenes": [{"id": "01", "duration": 2.0, "speed": 1.5}],
                "order": ["01"],
                "overlays": [{"scene_id": "01", "items": [{
                    "id": "arrow-1", "type": "callout", "shape": "arrow",
                    "editor_x": 0.73, "editor_y": 0.22, "editor_scale": 1.4,
                    "editor_rotation": 42, "animation": "slide",
                }]}],
            }
            with mock.patch.object(agent_core, "PROJECTS_DIR", projects):
                result = agent_core.save_timeline_edits("durable-timeline", edits)
                self.assertTrue(result["saved"])
                self.assertTrue((project / "config" / "timeline_edits.json").exists())
                # Simulate a later agent run replacing its generated project config.
                config_path.write_text(json.dumps(original), encoding="utf-8")
                reloaded = agent_core.load_project_config("durable-timeline")
            scene = reloaded["scenes"][0]
            self.assertEqual(scene["timeline_speed"], 1.5)
            self.assertEqual(scene["overlays"][0]["editor_x"], 0.73)
            self.assertEqual(scene["overlays"][0]["editor_rotation"], 42)

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

    def test_cancelled_run_reuses_prefiltered_candidates_and_hook(self):
        with tempfile.TemporaryDirectory() as tmp:
            projects = Path(tmp) / "projects"
            current = projects / "silent_cafe"
            inputs = current / "input"
            body_bucket = current / "seedance 2.0" / "_candidates" / "silent_cafe_interior"
            hook_bucket = current / "seedance 2.0" / "_candidates" / "hook_influencer"
            inputs.mkdir(parents=True)
            body_bucket.mkdir(parents=True)
            hook_bucket.mkdir(parents=True)
            script = "Japan has silent cafes where customers order with handwritten notes."
            (inputs / "script.txt").write_text(script, encoding="utf-8")
            (inputs / "run_form.json").write_text(json.dumps({
                "script": script, "clip_source": "scrape",
            }), encoding="utf-8")
            (body_bucket / "cand_body_a.mp4").write_bytes(b"b" * 5000)
            (body_bucket / "cand_body_b.mp4").write_bytes(b"b" * 5000)
            (hook_bucket / "cand_hook_a.mp4").write_bytes(b"h" * 5000)
            with mock.patch.object(agent_core, "PROJECTS_DIR", projects), \
                 mock.patch.object(clip_scraper, "text_heaviness_score", return_value=0.0), \
                 mock.patch.object(clip_scraper, "has_burned_captions", return_value=False), \
                 mock.patch.dict(os.environ, {"WAVESPEED_API_KEY": ""}):
                body, hooks, meta, report = agent_core.find_reusable_social_clips(
                    current, "Silent Cafe", script)
            self.assertEqual(len(body), 2)
            self.assertEqual(len(hooks), 1)
            self.assertEqual(meta[str(hooks[0])]["likes"], agent_core.HOOK_MIN_LIKES)
            self.assertEqual(meta[str(body[0])]["bucket_id"], "silent_cafe_interior")
            self.assertEqual(report["reusable_body_clips"], 2)

    def test_reused_candidate_keeps_platform_sidecar(self):
        with tempfile.TemporaryDirectory() as tmp:
            projects = Path(tmp) / "projects"
            current = projects / "x_reuse"
            inputs = current / "input"
            bucket = current / "seedance 2.0" / "_candidates" / "trains"
            inputs.mkdir(parents=True)
            bucket.mkdir(parents=True)
            script = "Japanese trains arrive on time."
            (inputs / "script.txt").write_text(script, encoding="utf-8")
            clip = bucket / "cand_x_train.mp4"
            clip.write_bytes(b"x" * 5000)
            clip.with_suffix(".json").write_text(json.dumps({
                "platform": "twitter", "clip_id": "tweet-1", "likes": 9000,
                "text_heaviness": 0.0,
            }), encoding="utf-8")
            with mock.patch.object(agent_core, "PROJECTS_DIR", projects), \
                 mock.patch.object(clip_scraper, "text_heaviness_score", return_value=0.0), \
                 mock.patch.object(clip_scraper, "has_burned_captions", return_value=False), \
                 mock.patch.dict(os.environ, {"WAVESPEED_API_KEY": ""}):
                body, _hooks, meta, _report = agent_core.find_reusable_social_clips(
                    current, "Japanese Trains", script)
            self.assertEqual(len(body), 1)
            self.assertEqual(meta[str(body[0])]["platform"], "twitter")
            self.assertEqual(meta[str(body[0])]["clip_id"], "tweet-1")

    def test_same_script_fresh_voice_still_reuses_entire_cancelled_scrape(self):
        self.assertTrue(agent_core.should_reuse_same_script_media(
            "same_project", False, {"force_regenerate": "on"}))
        self.assertFalse(agent_core.should_reuse_same_script_media(
            "same_project", False, {"force_rescrape": "on"}))

        with tempfile.TemporaryDirectory() as tmp:
            projects = Path(tmp) / "projects"
            current = projects / "large_cancelled_scrape"
            inputs = current / "input"
            bucket = current / "seedance 2.0" / "_candidates" / "vending_machines"
            inputs.mkdir(parents=True)
            bucket.mkdir(parents=True)
            script = "Japanese vending machines sell drinks and food in cities and rural roads."
            (inputs / "script.txt").write_text(script, encoding="utf-8")
            (inputs / "run_form.json").write_text(json.dumps({
                "script": script, "clip_source": "scrape", "force_regenerate": "on",
            }), encoding="utf-8")
            for index in range(65):
                (bucket / f"cand_saved_{index}.mp4").write_bytes(b"v" * 5000)
            with mock.patch.object(agent_core, "PROJECTS_DIR", projects), \
                 mock.patch.object(clip_scraper, "text_heaviness_score", return_value=0.0), \
                 mock.patch.object(clip_scraper, "has_burned_captions", return_value=False), \
                 mock.patch.dict(os.environ, {"WAVESPEED_API_KEY": ""}):
                self.assertEqual(agent_core.find_matching_scrape_project(script),
                                 "large_cancelled_scrape")
                body, hooks, _meta, report = agent_core.find_reusable_social_clips(
                    current, "Vending Machines", script)
            self.assertEqual(len(body), 65)
            self.assertEqual(hooks, [])
            self.assertEqual(report["reusable_body_clips"], 65)

    def test_clip_scrape_watchdog_can_be_fully_disabled(self):
        with mock.patch.object(agent_core.threading, "Timer") as timer:
            with agent_core.step_watchdog({}, "Clip scrape", limit_s=0):
                pass
        timer.assert_not_called()

    def test_timeline_model_exposes_rendered_sfx_at_zero_seconds(self):
        with tempfile.TemporaryDirectory() as tmp:
            projects = Path(tmp) / "projects"
            project = projects / "timeline-zero-sfx"
            config_dir = project / "config"
            config_dir.mkdir(parents=True)
            sound = project / "intro.wav"
            sound.write_bytes(b"RIFF" + b"x" * 5000)
            (config_dir / "project.json").write_text(json.dumps({
                "title": "Timeline Zero SFX", "duration": 4.0, "clip_source": "scrape",
                "scenes": [{"id": "scene-0", "start": 0.0, "end": 4.0,
                            "script": "Opening line"}],
                "ai_content_sfx": [{"id": "sfx-00", "start": 0.0, "duration": 0.57,
                                    "volume": 0.5, "category": "impact_hit",
                                    "path": str(sound)}],
            }), encoding="utf-8")
            with mock.patch.object(agent_core, "PROJECTS_DIR", projects), \
                 mock.patch.object(app.agent_core, "PROJECTS_DIR", projects):
                model = app.timeline_model("timeline-zero-sfx")
            visible = [item for item in model["sfx"] if item["id"] == "sfx-00"]
            self.assertEqual(len(visible), 1)
            self.assertEqual(visible[0]["start_abs"], 0.0)
            self.assertEqual(visible[0]["scene_id"], "scene-0")
            self.assertTrue(visible[0]["url"])

    def test_timeline_preview_model_uses_render_trim_speed_voice_and_caption_clock(self):
        with tempfile.TemporaryDirectory() as tmp:
            projects = Path(tmp) / "projects"
            project = projects / "synced-preview"
            config_dir = project / "config"
            input_dir = project / "input"
            config_dir.mkdir(parents=True)
            input_dir.mkdir(parents=True)
            voice = input_dir / "custom_voice.wav"
            voice.write_bytes(b"RIFF" + b"v" * 100)
            (input_dir / "audio_analysis.json").write_text(json.dumps({
                "sentence_timestamps": [
                    {"start": 0.0, "end": 2.0, "text": "Synced spoken words"}
                ]
            }), encoding="utf-8")
            (config_dir / "project.json").write_text(json.dumps({
                "title": "Synced Preview", "duration": 2.0,
                "audio_path": str(voice), "seedance_clip_start_trim": 0.75,
                "caption_max_words": 2, "caption_uppercase": True,
                "scenes": [{
                    "id": "a", "start": 0.0, "end": 2.0, "script": "Synced spoken words",
                    "clip": "speed_a_1p5_original.mp4", "timeline_speed_src": "original.mp4",
                    "timeline_speed": 1.5, "seedance": True,
                }],
            }), encoding="utf-8")
            with mock.patch.object(agent_core, "PROJECTS_DIR", projects), \
                 mock.patch.object(app.agent_core, "PROJECTS_DIR", projects), \
                 mock.patch.object(app, "_timeline_media",
                                   return_value=("/poster.jpg", "/clip.mp4", None, None)):
                model = app.timeline_model("synced-preview")
            self.assertEqual(model["scenes"][0]["source_trim"], 0.75)
            self.assertEqual(model["scenes"][0]["source_speed"], 1.5)
            self.assertEqual(model["caption_track"][0]["text"], "Synced spoken words")
            self.assertEqual(model["caption_max_words"], 2)
            self.assertIn("custom_voice.wav", model["voice_url"])
            self.assertIn("var RENDER_MODE = false", app.TIMELINE_ASSETS)
            self.assertIn("function syncPreviewVideo", app.TIMELINE_ASSETS)

    def test_timeline_caption_choice_disables_animated_captions_and_preserves_voice_track(self):
        config = {
            "render_captions": True, "animated_captions": True,
            "scenes": [
                {"id": "a", "start": 0.0, "end": 2.0, "script": "first words",
                 "word_timings": [{"word": "first", "start": 0.0, "end": 0.7}]},
                {"id": "b", "start": 2.0, "end": 4.0, "script": "second words"},
            ],
        }
        edits = {"order": ["b", "a"], "scenes": [{"id": "b", "duration": 2.0},
                                                    {"id": "a", "duration": 2.0}],
                 "captions": False}
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(agent_core, "PROJECTS_DIR", Path(tmp)):
            out = agent_core.apply_timeline_edits_to_config(config, edits, "caption-choice")
        self.assertFalse(out["render_captions"])
        self.assertFalse(pipeline.animated_captions_enabled(out))
        self.assertEqual([row["text"] for row in out["timeline_caption_track"]],
                         ["first words", "second words"])
        self.assertEqual([scene["id"] for scene in out["scenes"]], ["b", "a"])

    def test_timeline_revoice_retimes_clips_captions_and_saved_editor_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            projects = Path(tmp) / "projects"
            project = projects / "revoice-project"
            config_dir = project / "config"
            input_dir = project / "input"
            config_dir.mkdir(parents=True)
            input_dir.mkdir(parents=True)
            voice = input_dir / "voiceover.wav"
            voice.write_bytes(b"RIFF" + b"old" * 100)
            script = "First line here. Second line now."
            (input_dir / "script.txt").write_text(script, encoding="utf-8")
            (input_dir / "run_form.json").write_text(json.dumps({
                "script": script, "speaker_name": "Narrator", "tts_voice": "Charon",
            }), encoding="utf-8")
            (config_dir / "project.json").write_text(json.dumps({
                "title": "Revoice", "duration": 4.0,
                "audio_path": str(voice), "timing_audio_path": str(voice),
                "scenes": [
                    {"id": "a", "start": 0.0, "end": 2.0, "script": "First line here.",
                     "clip": "a.mp4"},
                    {"id": "b", "start": 2.0, "end": 4.0, "script": "Second line now.",
                     "clip": "b.mp4"},
                ],
            }), encoding="utf-8")
            (config_dir / "timeline_edits.json").write_text(json.dumps({
                "order": ["a", "b"],
                "scenes": [{"id": "a", "duration": 2.0},
                           {"id": "b", "duration": 2.0}],
                "captions": True,
            }), encoding="utf-8")
            analysis = {
                "transcript": script, "duration_seconds": 6.0,
                "sentence_timestamps": [
                    {"start": 0.0, "end": 3.0, "text": "First line here."},
                    {"start": 3.0, "end": 6.0, "text": "Second line now."},
                ],
            }
            with mock.patch.object(agent_core, "PROJECTS_DIR", projects), \
                 mock.patch.object(agent_core, "generate_project_voiceover", return_value=voice), \
                 mock.patch.object(agent_core, "probe_audio_duration", return_value=6.0), \
                 mock.patch("voice_align.available", return_value=False), \
                 mock.patch.object(agent_core, "analyze_audio_with_gemini", return_value=analysis):
                result = agent_core.regenerate_timeline_speech("revoice-project", render=False)
                reopened = agent_core.load_project_config("revoice-project")
            self.assertEqual(result["retimed_scenes"], 2)
            self.assertEqual(reopened["duration"], 6.0)
            self.assertEqual([(scene["start"], scene["end"]) for scene in reopened["scenes"]],
                             [(0.0, 3.0), (3.0, 6.0)])
            self.assertEqual([row["text"] for row in reopened["timeline_caption_track"]],
                             ["First line here.", "Second line now."])
            saved_edits = json.loads((config_dir / "timeline_edits.json").read_text(encoding="utf-8"))
            self.assertEqual([row["duration"] for row in saved_edits["scenes"]], [3.0, 3.0])
            self.assertTrue(any((input_dir / "voice_versions").rglob("voiceover.wav")))

    def test_force_regenerate_bypasses_unchanged_script_voice_reuse(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            input_dir = project / "input"
            input_dir.mkdir()
            old_voice = input_dir / "voiceover.wav"
            old_voice.write_bytes(b"old")
            (input_dir / "script.txt").write_text("Same script", encoding="utf-8")
            with mock.patch.object(agent_core.pipeline, "generate_speech_gemini",
                                   return_value=old_voice) as generate, \
                 mock.patch.object(agent_core.pipeline, "apply_voice_postprocess"):
                result = agent_core.generate_project_voiceover(
                    "Same script", project,
                    {"force_regenerate": "on", "speaker_name": "Narrator",
                     "tts_voice": "Charon"})
            self.assertEqual(result, old_voice)
            generate.assert_called_once()

    def test_targeted_scrape_replacement_changes_only_marked_scene(self):
        with tempfile.TemporaryDirectory() as tmp:
            projects = Path(tmp) / "projects"
            project = projects / "targeted-social"
            config_dir = project / "config"
            inputs = project / "input"
            candidate_dir = project / "seedance 2.0" / "_candidates" / "test"
            config_dir.mkdir(parents=True)
            inputs.mkdir(parents=True)
            candidate_dir.mkdir(parents=True)
            candidate = candidate_dir / "cand_new.mp4"
            candidate.write_bytes(b"v" * 5000)
            (inputs / "script_understanding.json").write_text(
                '{"topic":"vending machines"}', encoding="utf-8")
            (config_dir / "project.json").write_text(json.dumps({
                "title": "Vending", "duration": 4.0, "clip_source": "scrape",
                "scrape_platforms": "tiktok,x", "script_relevancy": 85,
                "wavespeed": {"reasoning_model": "test-model"},
                "scenes": [
                    {"id": "a", "start": 0.0, "end": 2.0, "script": "vending machine", "clip": "old_a.mp4"},
                    {"id": "b", "start": 2.0, "end": 4.0, "script": "train", "clip": "old_b.mp4"},
                ],
            }), encoding="utf-8")
            scraped = {"path": candidate, "query": "japanese vending machine",
                       "platform": "twitter", "clip_id": "tweet-2", "likes": 4000,
                       "meta": {"caption": ""}, "text_heaviness": 0.0,
                       "rapid_internal_cut_count": 0}
            fake_output = project / "renders" / "replacement.mp4"
            with mock.patch.object(agent_core, "PROJECTS_DIR", projects), \
                 mock.patch.object(clip_scraper, "backend_active", return_value=True), \
                 mock.patch.object(clip_scraper, "scrape_bucket", return_value=[scraped]), \
                 mock.patch.object(agent_core, "llm_scene_scrape_queries",
                                   return_value=["japanese vending machine"]), \
                 mock.patch.object(agent_core, "assign_clips_to_scenes_by_vision",
                                   return_value=([candidate], [{"match_class": "A_MATCH",
                                                                "script_match_score": 9.0}])), \
                 mock.patch.object(agent_core.pipeline, "render_video", return_value=fake_output):
                result = agent_core.replace_timeline_scrape_scenes("targeted-social", ["a"])
            saved = json.loads((config_dir / "project.json").read_text(encoding="utf-8"))
            self.assertTrue(saved["scenes"][0]["clip"].startswith("timeline_replaced_a_"))
            self.assertEqual(saved["scenes"][0]["scrape_source"], "twitter")
            self.assertEqual(saved["scenes"][1]["clip"], "old_b.mp4")
            self.assertEqual(result["replaced_scenes"], 1)

    def test_candidate_names_do_not_collide_across_tiers(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)

            def fake_search(query, want, status_cb=None, sort="MOST_LIKED",
                            platforms=None, deadline=None):
                item_id = "id-exact" if query == "exact query" else "id-broad"
                return [{
                    "id": item_id,
                    "desc": query,
                    "webVideoUrl": f"https://www.tiktok.com/@user/video/{item_id}",
                    "video": {"width": 720, "height": 1280, "duration": 12},
                }]

            def fake_download(_item, dest, status_cb=None):
                Path(dest).write_bytes(b"x" * 5000)
                return Path(dest)

            def fake_normalize(_raw, final, _ffmpeg, seconds=4.0, start=0.0):
                Path(final).parent.mkdir(parents=True, exist_ok=True)
                Path(final).write_bytes(b"video")
                return Path(final)

            common = (
                # tiktok_login is the only backend now - mock its readiness + search/download.
                mock.patch.object(clip_scraper, "tiktok_backend_ready", return_value=True),
                mock.patch.object(clip_scraper, "backend_search", side_effect=fake_search),
                mock.patch.object(clip_scraper, "backend_download", side_effect=fake_download),
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
            )
            with common[0], common[1], common[2], common[3], common[4], common[5], common[6], common[7], common[8]:
                first = clip_scraper.scrape_bucket(out_dir, ["exact query"], 1,
                                                   bucket_id="workers", tier="exact")
                second = clip_scraper.scrape_bucket(out_dir, ["broad query"], 1,
                                                    bucket_id="workers", tier="broad")

            self.assertNotEqual(first[0]["path"], second[0]["path"])
            self.assertTrue(first[0]["path"].exists())
            self.assertTrue(second[0]["path"].exists())


if __name__ == "__main__":
    unittest.main()
