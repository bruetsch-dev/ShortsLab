import unittest
import contextlib
import io
from unittest import mock
import tempfile
import json
from pathlib import Path

import scrape_v3 as v3


class LiveProcessingTimelineTests(unittest.TestCase):
    def test_beats_exist_before_media_and_fill_the_same_slot_after_assignment(self):
        with tempfile.TemporaryDirectory() as root:
            chapter = v3.Chapter(
                3, "The reveal", start=2.0, end=5.5,
                subject="a Japanese store", action="an employee responds to a signal")
            path = v3.write_live_processing_timeline(root, [chapter], {}, phase="planned")
            planned = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(planned["phase"], "planned")
            self.assertEqual(planned["beats"][0]["id"], 3)
            self.assertEqual(planned["beats"][0]["assigned"], [])

            clip = Path(root) / "candidate.mp4"
            clip.write_bytes(FAKE_MP4)
            window = v3.ShotWindow(
                3, "source-a", "tiktok", str(clip), 4.0, 6.8,
                match_class="exact")
            v3.write_live_processing_timeline(
                root, [chapter], {3: [window]}, phase="assigning")
            assigned = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(assigned["beats"][0]["assigned"][0]["path"], str(clip))
            self.assertEqual(assigned["beats"][0]["assigned"][0]["match_class"], "exact")


def _scenes(count=12, each=2.6):
    return [{"id": f"{i:02d}", "start": round(i * each, 3), "end": round((i + 1) * each, 3),
             "text": f"line {i}"} for i in range(count)]


# A downloaded file is only accepted when it actually looks like a video (see
# test_v3_download_integrity). Fixtures therefore write a real MP4 box header instead
# of an arbitrary marker string.
FAKE_MP4 = bytes(4) + b"ftypisom" + bytes(48)


class QuerySanitisingTests(unittest.TestCase):
    def test_platform_names_are_stripped_not_the_query(self):
        """A query carrying "tiktok" searches TikTok for the word TikTok."""
        out = v3.sanitize_queries(["tiktok garbage truck", "Instagram reels ゴミ収集車", "  "])
        self.assertEqual(out, ["garbage truck", "ゴミ収集車"])

    def test_duplicates_and_empties_go_and_the_count_is_capped(self):
        # Real subject words, not single letters: "a" and "the" are now stripped as the
        # leftovers of a truncated sentence, which is what "A guest soaking in a" was.
        out = v3.sanitize_queries(["ramen shop", "Ramen Shop", "", None, "train door",
                                   "bento box", "vending machine", "capsule pod", "sauna bench"])
        self.assertEqual(out[0], "ramen shop")
        self.assertNotIn("Ramen Shop", out)
        self.assertLessEqual(len(out), v3.V3_CONFIG["queries_per_chapter"])

    def test_presentation_scaffolding_is_never_a_search_query(self):
        self.assertEqual(v3.sanitize_queries(["Chapter 1", "visual chapter #3"]), [])

    def test_explainer_words_are_removed_so_search_targets_visible_footage(self):
        out = v3.sanitize_queries([
            "Japanese ao blue green dictionary explanation", "青 緑 色の呼び方 解説"
        ])
        self.assertEqual(out, ["Japanese ao blue green", "青 緑 色の呼び方"])

    def test_search_phrases_are_compact_platform_queries_not_prose(self):
        """Trimming prose to five words was the first attempt; the Tokyo-train run showed the
        trimmed remainder still loses. "young Japanese couple walking through" narrates an
        action, and every English query of that shape in that run returned only rejects. The
        compact Japanese phrase is what a real caption looks like, so it is what survives."""
        out = v3.sanitize_queries([
            "young Japanese couple walking through illuminated light tunnel then posing",
            "日本人 カップル 光のトンネル 足を止めて 撮影 動画",
        ])
        self.assertEqual(out[0], "日本人 カップル 光のトンネル 足を止めて")
        self.assertNotIn("walking", " ".join(out))

    def test_store_music_fallback_searches_visible_retail_actions(self):
        music = v3._fallback_queries_from_text("Stores use songs as coded messages.")
        cashier = v3._fallback_queries_from_text("More cashiers are needed.")
        stock = v3._fallback_queries_from_text("It is time to restock or clean an area.")
        self.assertIn("スーパー 店内BGM", music)
        self.assertIn("レジ打ち", cashier)
        self.assertTrue(any("品出し" in query for query in stock))

    def test_commute_story_never_injects_unrelated_konbini_searches(self):
        out = v3._retail_action_queries(
            "Women wait on a Tokyo train platform during the morning commute.")
        self.assertIn("朝 通勤 電車", out)
        self.assertFalse(any("コンビニ" in query for query in out))
        self.assertFalse(any("ATM" in query for query in out))

    def test_generic_cash_and_parcel_story_never_assumes_a_convenience_store(self):
        out = v3._retail_action_queries(
            "A commuter withdraws cash, grabs coffee and collects a parcel before going home.")
        self.assertIn("ATM 引き出し", out)
        self.assertIn("宅配便 受け取り", out)
        self.assertFalse(any("コンビニ" in query for query in out))

    def test_printed_sign_is_not_misread_as_a_printer_search(self):
        out = v3._retail_action_queries("A commuter checks the printed operating-hours sign.")
        self.assertFalse(any("印刷" in query or "コピー" in query for query in out))

    def test_convenience_store_planner_prose_is_preceded_by_native_action_queries(self):
        chapter = v3.Chapter(0, "Late-night convenience store", subject="customers entering a convenience store",
                             action="staff heating a bento or serving coffee",
                             queries=["Customers entering under illuminated convenience-store"])
        out = v3.chapter_search_queries(chapter)
        self.assertIn("コンビニ", out)
        self.assertIn("コンビニ 弁当", out)
        self.assertNotEqual(out[0], "Customers entering under illuminated convenience-store")

    def test_retail_chapter_has_concrete_context_actions_not_generic_filler(self):
        proxies = v3._retail_context_proxies(
            "Need cash at an ATM or print a document at a convenience store copier.")
        self.assertTrue(any("ATM" in row or "copier" in row for row in proxies))
        self.assertTrue(any("cashier" in row for row in proxies))

    def test_irasshaimase_is_not_reduced_to_generic_store_or_atm_searches(self):
        out = v3._fallback_queries_from_text(
            'Workers say "Irasshaimase" as you walk into a convenience store.')
        self.assertIn("いらっしゃいませ コンビニ", out)
        self.assertIn("いらっしゃいませ 店員", out)
        chapter = v3.Chapter(0, "Greeting", subject="Japanese store greeting",
                             action="staff say Irasshaimase as a customer enters",
                             queries=["コンビニ"])
        final = v3.chapter_search_queries(chapter)
        self.assertTrue(final[0].startswith("いらっしゃいませ"))

    def test_women_only_carriage_uses_the_native_anchor_before_commute_filler(self):
        out = v3._fallback_queries_from_text(
            "A woman checks a women-only-car sign before the morning commute.")
        self.assertEqual(out[:3], ["女性専用車両", "女性専用車両 乗車", "女性専用車両 ホーム"])

    def test_three_strong_four_second_shots_cover_a_thirteen_second_chapter(self):
        chapter = v3.Chapter(0, "Train", start=0.0, end=13.0)
        windows = [v3.ShotWindow(0, f"S{i}", "tiktok", f"{i}.mp4", 0.0, 4.0,
                                  match_class="context", reason="women board a commuter train")
                   for i in range(3)]
        self.assertTrue(v3.chapter_search_satisfied(chapter, windows))

    def test_grader_allows_one_tangible_component_of_a_multi_fact_chapter(self):
        self.assertIn("MULTI-FACT CHAPTER RULE", v3.GRADE_PROMPT)
        self.assertIn("ONE concrete, visible component", v3.GRADE_PROMPT)

    def test_internal_source_cut_keeps_the_longest_readable_side(self):
        v3._SOURCE_CUT_CACHE.clear()
        with mock.patch.object(v3.clip_scraper, "hard_cut_times", return_value=[1.2]):
            self.assertEqual(v3.longest_uncut_subwindow("source.mp4", 0.0, 3.4, "ffmpeg"),
                             (1.2, 3.4))

    def test_adaptive_retry_reads_failures_and_never_repeats_a_query(self):
        chapter = v3.Chapter(0, "Bins", subject="public trash cans in Japan",
                             action="tourist searches while carrying a bottle",
                             evidence="a person visibly failing to find a bin",
                             queries=["Japan tourist looking for trash can"])
        answer = {"queries": ["TikTok Japan tourist looking for trash can",
                              "日本 ゴミ箱 探す 外国人", "tourist carrying trash Tokyo POV"]}
        with mock.patch.object(v3.scrape_v2, "_llm_json", return_value=answer):
            out = v3.adaptive_search_queries(
                chapter, ["Japan tourist looking for trash can"],
                [{"reason": "no_visible_action"}], round_index=1)
        self.assertNotIn("Japan tourist looking for trash can", out)
        self.assertIn("日本 ゴミ箱 探す 外国人", out)
        self.assertTrue(all("tiktok" not in query.casefold() for query in out))

    def test_failed_retail_search_widens_to_real_native_platform_tags(self):
        chapter = v3.Chapter(0, "Cashier support", subject="Japanese supermarket cashier",
                             action="more cashiers arrive at the register")
        with mock.patch.object(v3.scrape_v2, "_llm_json", return_value={"queries": []}):
            out = v3.adaptive_search_queries(
                chapter, ["スーパー レジ 店員"], [{"reason": "caption_on_solid_box"}])
        self.assertIn("スーパー店内", out)
        self.assertIn("コンビニ店員", out)
        self.assertIn("レジ打ち", out)

    def test_timer_driven_fallback_keeps_producing_untried_queries(self):
        chapter = v3.Chapter(0, "Store work", subject="Japanese convenience store employee",
                             action="employee restocks shelves",
                             queries=["コンビニ 店員", "Japanese convenience store worker"])
        first = v3.persistent_search_queries(chapter, [], round_index=6)
        second = v3.persistent_search_queries(chapter, first, round_index=7)
        self.assertTrue(first)
        self.assertTrue(second)
        self.assertTrue(set(map(str.casefold, first)).isdisjoint(map(str.casefold, second)))

    def test_rare_school_detail_search_widens_to_real_commute_context(self):
        chapter = v3.Chapter(0, "Yellow covers arrive with first graders",
                             subject="Japanese first graders with yellow randoseru covers",
                             action="children walk to school")
        with mock.patch.object(v3.scrape_v2, "_llm_json", return_value={"queries": []}):
            out = v3.adaptive_search_queries(chapter, [], [{"reason": "unrelated_filler"}])
        self.assertIn("小学生 登校", out)
        self.assertIn("ランドセル 通学", out)


class ChapterPlanTests(unittest.TestCase):
    """V2 fixes twelve scene lengths and then hunts for twelve clips. A chapter is one visual
    idea that may cover several sentences, so the plan is held to 4-6 of them and every voice
    line has to land in exactly one."""

    def test_every_scene_lands_in_exactly_one_chapter(self):
        scenes = _scenes()
        chapters = v3.normalize_chapter_plan(
            [{"title": "Hook", "scene_ids": ["00"]},
             {"title": "Garbage", "scene_ids": ["01", "02", "03"]}], scenes)
        covered = [sid for c in chapters for sid in c.scene_ids]
        self.assertEqual(sorted(covered), [s["id"] for s in scenes])
        self.assertEqual(len(covered), len(set(covered)), "a scene was claimed twice")

    def test_too_few_chapters_are_split_and_too_many_are_merged(self):
        scenes = _scenes()
        few = v3.normalize_chapter_plan([{"title": "All", "scene_ids": [s["id"] for s in scenes]}],
                                        scenes)
        self.assertGreaterEqual(len(few), v3.V3_CONFIG["min_chapters"])
        many = v3.normalize_chapter_plan(
            [{"title": f"C{i}", "scene_ids": [s["id"]]} for i, s in enumerate(scenes)], scenes)
        self.assertLessEqual(len(many), v3.V3_CONFIG["max_chapters"])

    def test_short_voiceover_is_capped_at_three_editorial_chapters(self):
        scenes = _scenes(count=10, each=2.5)  # 25 seconds
        raw = [{"title": f"C{i}", "scene_ids": [scene["id"]]}
               for i, scene in enumerate(scenes)]
        chapters = v3.normalize_chapter_plan(raw, scenes)
        self.assertEqual(len(chapters), 3)

    def test_a_split_chapter_is_not_shown_twice_under_one_name(self):
        chapters = v3.normalize_chapter_plan(
            [{"title": "All", "scene_ids": [s["id"] for s in _scenes()]}], _scenes())
        titles = [c.title for c in chapters]
        self.assertEqual(len(titles), len(set(titles)))

    def test_the_span_follows_the_voice_not_the_plan(self):
        scenes = _scenes(count=8, each=3.0)
        chapters = v3.normalize_chapter_plan(
            [{"title": "A", "scene_ids": ["00", "01"]}, {"title": "B", "scene_ids": ["02", "03"]}],
            scenes)
        self.assertEqual(chapters[0].start, 0.0)
        self.assertEqual(chapters[0].end, 6.0)
        self.assertEqual(chapters[0].target_seconds, 6.0)

    def test_an_empty_script_plans_nothing(self):
        self.assertEqual(v3.normalize_chapter_plan([{"title": "x"}], []), [])

    def test_real_clip_short_scene_fields_feed_the_planner(self):
        scenes = [
            {"id": "0", "start": 0.0, "end": 3.0,
             "exact_voice_text": "Japanese friends meet at a gokon."},
            {"id": "1", "start": 3.0, "end": 6.0,
             "script": "Several men and women sit together at an izakaya."},
            {"id": "2", "start": 6.0, "end": 9.0,
             "voice_line": "Later two people meet in a cafe."},
            {"id": "3", "start": 9.0, "end": 12.0,
             "script": "One person makes a kokuhaku."},
        ]
        planned = {"chapters": [
            {"title": "Gokon", "scene_ids": ["0"], "subject": "gokon", "queries": ["合コン"]},
            {"title": "Izakaya", "scene_ids": ["1"], "subject": "izakaya", "queries": ["居酒屋 男女"]},
            {"title": "Cafe date", "scene_ids": ["2"], "subject": "cafe date", "queries": ["カフェ デート"]},
            {"title": "Kokuhaku", "scene_ids": ["3"], "subject": "kokuhaku", "queries": ["告白"]},
        ]}
        with mock.patch.object(v3.scrape_v2, "_llm_json", return_value=planned) as call:
            chapters = v3.plan_chapters_v3(scenes, "", reasoning_model="test")
        sent = call.call_args.args[0][1]["content"]
        self.assertIn("Japanese friends meet at a gokon", sent)
        self.assertIn("Several men and women sit together", sent)
        self.assertEqual([chapter.title for chapter in chapters],
                         ["Gokon", "Cafe date", "Kokuhaku"])

    def test_planner_failure_uses_script_words_not_chapter_numbers(self):
        scenes = [
            {"id": str(i), "start": i * 3.0, "end": (i + 1) * 3.0,
             "script": text}
            for i, text in enumerate((
                "Friends meet at a gokon.", "Men and women sit at an izakaya.",
                "Two people meet in a cafe.", "One person makes a kokuhaku."))
        ]
        with mock.patch.object(v3.scrape_v2, "_llm_json", return_value={}):
            chapters = v3.plan_chapters_v3(scenes, "")
        self.assertEqual(len(chapters), 3)
        for chapter in chapters:
            self.assertFalse(v3._GENERIC_CHAPTER_QUERY.fullmatch(chapter.title))
            self.assertTrue(v3.chapter_search_queries(chapter))
            self.assertNotIn("Chapter", " ".join(v3.chapter_search_queries(chapter)))

    def test_first_search_round_includes_planned_context_actions(self):
        scenes = _scenes(count=4, each=3.0)
        planned = [{"title": f"C{i}", "scene_ids": [f"0{i}"],
                    "subject": "Japanese school safety cover",
                    "queries": ["黄色いランドセルカバー 配布", "yellow randoseru cover"],
                    "proxies_ok": ["小学生 登校", "ランドセル 通学"]}
                   for i in range(4)]
        chapters = v3.normalize_chapter_plan(planned, scenes)
        self.assertIn("小学生 登校", chapters[0].queries)
        self.assertIn("黄色いランドセルカバー 配布", chapters[0].queries)

    def test_named_retail_ritual_survives_generic_store_fallbacks(self):
        scenes = _scenes(count=2, each=3.0)
        planned = [{"title": "Greeting", "scene_ids": ["00", "01"],
                    "subject": "Japanese shop greeting", "queries": ["コンビニ"],
                    "proxies_ok": ["店員 greeting customer"]}]
        scenes[0]["text"] = 'Workers shout "Irasshaimase" when shoppers enter a store.'
        scenes[1]["text"] = "Tourists do not need to reply."
        chapter = v3.normalize_chapter_plan(planned, scenes)[0]
        self.assertIn("いらっしゃいませ コンビニ", chapter.queries)
        self.assertNotEqual(chapter.queries[0], "コンビニ")


class WindowSelectionTests(unittest.TestCase):
    def setUp(self):
        self.chapter = v3.Chapter(chapter_id=1, title="Garbage", scene_ids=["01"],
                                  start=3.0, end=12.0)

    def _graded(self, windows, source_id="A"):
        return [{"source_id": source_id, "platform": "tiktok", "path": f"{source_id}.mp4",
                 "query": "q", "windows": windows}]

    def test_one_source_gives_at_most_three_explicit_non_overlapping_windows(self):
        graded = self._graded([
            {"start": 0.0, "end": 3.0, "match_class": "exact"},
            {"start": 3.5, "end": 6.0, "match_class": "exact"},      # inside the 2s gap
            {"start": 9.0, "end": 12.0, "match_class": "context"},
            {"start": 20.0, "end": 23.0, "match_class": "context"},
            {"start": 30.0, "end": 33.0, "match_class": "context"},
        ])
        chosen = v3.select_chapter_windows(self.chapter, graded)
        self.assertEqual(len(chosen), v3.V3_CONFIG["max_windows_per_source"])
        self.assertEqual(v3.V3_CONFIG["max_windows_per_source"],
                         v3.V3_CONFIG["max_windows_per_source_total"],
                         "one rule: the per-chapter and whole-Short caps must agree")
        starts = sorted(w.start for w in chosen)
        for earlier, later in zip(starts, starts[1:]):
            self.assertGreaterEqual(later - earlier, v3.V3_CONFIG["min_window_gap_seconds"])

    def test_a_chapter_that_forbids_reuse_takes_one_window_per_source(self):
        self.chapter.allow_multi_window = False
        graded = self._graded([{"start": 0.0, "end": 3.0, "match_class": "exact"},
                               {"start": 10.0, "end": 13.0, "match_class": "exact"}])
        self.assertEqual(len(v3.select_chapter_windows(self.chapter, graded)), 1)

    def test_exact_outranks_context_outranks_proxy(self):
        graded = (self._graded([{"start": 0.0, "end": 3.0, "match_class": "editorial_proxy"}], "A")
                  + self._graded([{"start": 0.0, "end": 3.0, "match_class": "exact"}], "B")
                  + self._graded([{"start": 0.0, "end": 3.0, "match_class": "context"}], "C"))
        chosen = v3.select_chapter_windows(self.chapter, graded)
        self.assertEqual([w.match_class for w in chosen], ["exact", "context", "editorial_proxy"])

    def test_a_proxy_cannot_be_the_only_visual_proof(self):
        graded = self._graded([{"start": 0.0, "end": 3.0, "match_class": "editorial_proxy"}])
        self.assertEqual(v3.select_chapter_windows(self.chapter, graded), [])

    def test_a_long_grounded_source_is_not_automatically_split_into_fake_clips(self):
        self.chapter.start, self.chapter.end = 0.0, 8.5
        graded = self._graded([{
            "start": 0.0, "end": 10.0, "match_class": "context",
            "reason": "A Japanese commuter train car with women-only signage and passengers.",
        }])
        chosen = v3.select_chapter_windows(self.chapter, graded)
        self.assertEqual(len(chosen), 1)
        self.assertTrue(all(w.match_class == "context" for w in chosen))

    def test_same_source_same_visible_content_is_selected_only_once(self):
        graded = self._graded([
            {"start": 0.0, "end": 3.0, "match_class": "exact",
             "reason": "Close-up of the same ceiling speaker grille."},
            {"start": 5.0, "end": 8.0, "match_class": "exact",
             "reason": "Close-up of the same ceiling speaker grille."},
            {"start": 10.0, "end": 13.0, "match_class": "context",
             "reason": "Employee restocking drinks in a store aisle."},
        ])
        chosen = v3.select_chapter_windows(self.chapter, graded)
        self.assertEqual(len(chosen), 2)
        self.assertEqual([window.start for window in chosen], [0.0, 10.0])

    def test_voice_template_uses_exact_timed_words_not_overlapping_display_script(self):
        scenes = [
            {"start": 0.0, "end": 2.0, "exact_voice_text": "first words",
             "script": "first words duplicated tail"},
            {"start": 2.0, "end": 4.0, "exact_voice_text": "duplicated tail",
             "script": "duplicated tail next words"},
        ]
        _base, text = v3._voice_template(scenes, 0.0, 4.0)
        self.assertEqual(text, "first words duplicated tail")

    def test_caption_detection_survives_window_expansion_and_timeline_alignment(self):
        self.chapter.start, self.chapter.end = 0.0, 6.0
        graded = [{"source_id": "A", "platform": "tiktok", "path": "A.mp4", "query": "q",
                   "source_has_captions": True,
                   "windows": [{"start": 0.0, "end": 7.0, "match_class": "exact"}]}]
        chosen = v3.select_chapter_windows(self.chapter, graded)
        self.assertTrue(chosen)
        self.assertTrue(all(window.source_has_captions for window in chosen))
        scenes = v3.align_windows_to_voice([self.chapter], {1: chosen}, 6.0)
        self.assertTrue(all(scene["source_has_captions"] for scene in scenes))

    def test_women_only_train_claim_cannot_accept_a_generic_person(self):
        self.chapter.subject = "Japanese women-only train car"
        self.chapter.evidence = "women-only signage on a commuter train carriage"
        graded = self._graded([{
            "start": 0.0, "end": 5.0, "match_class": "editorial_proxy",
            "reason": "A child waits at a road crossing; no train carriage or women-only sign is visible.",
        }])
        self.assertEqual(v3.select_chapter_windows(self.chapter, graded), [])

    def test_an_unknown_class_becomes_context_not_a_rejection(self):
        self.assertEqual(v3.normalize_match_class("who knows"), "context")
        self.assertEqual(v3.normalize_match_class("proxy"), "editorial_proxy")
        self.assertEqual(v3.normalize_match_class("EXACT"), "exact")

    def test_fortnite_can_never_be_a_proxy_for_real_world_dating(self):
        self.chapter.subject = "Japanese group dating at an izakaya"
        graded = self._graded([{"start": 0.0, "end": 3.0, "match_class": "context",
                                "reason": "Fortnite gameplay shows a character in Chapter 3"}])
        self.assertEqual(v3.select_chapter_windows(self.chapter, graded), [])


class SearchPoolTests(unittest.TestCase):
    def test_legacy_cache_without_query_provenance_is_not_reused(self):
        chapter = v3.Chapter(0, "Women-only train platform", queries=["女性専用車両 乗車"])

        def download(item, destination, _status):
            Path(destination).write_bytes(FAKE_MP4)
            return str(destination)

        with tempfile.TemporaryDirectory() as folder, \
                mock.patch.object(v3.clip_scraper, "backend_search", return_value=[
                    {"id": "fresh", "_platform": "tiktok"}]), \
                mock.patch.object(v3.scrape_v2, "download_proxy_v2", side_effect=download), \
                mock.patch.object(v3.clip_scraper, "_item_meta", return_value={}):
            proxy = Path(folder) / "seedance 2.0" / "_v3_proxies"
            proxy.mkdir(parents=True)
            (proxy / "v3_tiktok_stale.mp4").write_bytes(FAKE_MP4)
            rows = v3.gather_chapter_sources(chapter, ["tiktok"], folder, set())
        self.assertEqual([row["source_id"] for row in rows], ["fresh"])

    def test_download_pool_is_interleaved_across_at_least_three_queries(self):
        chapter = v3.Chapter(0, "Retail", queries=["query one", "query two", "query three"])

        def search(query, *_args, sort=None, **_kwargs):
            return [{"id": f"{query}-{sort}-{index}", "_platform": "tiktok",
                     "title": "Japanese supermarket employee"} for index in range(3)]

        def download(_item, destination, _status):
            Path(destination).write_bytes(FAKE_MP4)
            return str(destination)

        with tempfile.TemporaryDirectory() as folder, \
                mock.patch.object(v3.clip_scraper, "backend_search", side_effect=search), \
                mock.patch.object(v3.scrape_v2, "download_proxy_v2", side_effect=download), \
                mock.patch.object(v3.clip_scraper, "_item_meta", return_value={}):
            rows = v3.gather_chapter_sources(chapter, ["tiktok"], folder, set())
        self.assertEqual(len(rows), v3.V3_CONFIG["max_sources_per_chapter"])
        # Each chapter also searches for the striking version of its own subject (2026-08-30),
        # so the executed set is the three planned queries plus those variants. What this test
        # is about is that the pool is SPREAD, not which exact terms ran.
        used = {row["query"] for row in rows}
        for base in ("query one", "query two", "query three"):
            self.assertTrue(any(q == base or q.startswith(base + " ") for q in used),
                            f"{base} is not represented: {sorted(used)}")
        self.assertGreaterEqual(len({q.split()[1] for q in used if q.split()[1:]}), 3,
                                f"the pool came from too few hypotheses: {sorted(used)}")

    def test_a_failed_download_can_be_retried_in_a_later_round(self):
        chapter = v3.Chapter(0, "Retail", queries=["スーパー買い出し"])
        seen = set()
        with tempfile.TemporaryDirectory() as folder, \
                mock.patch.object(v3.clip_scraper, "backend_search",
                                  return_value=[{"id": "GOOD", "_platform": "tiktok"}]), \
                mock.patch.object(v3.scrape_v2, "download_proxy_v2", return_value=None), \
                mock.patch.object(v3.clip_scraper, "_item_meta", return_value={}):
            rows = v3.gather_chapter_sources(chapter, ["tiktok"], folder, seen)
        self.assertEqual(rows, [])
        self.assertNotIn("GOOD", seen,
                         "a transient yt-dlp failure must not blacklist the source forever")

    def test_tiktok_ugc_is_tried_before_instagram_ads(self):
        chapter = v3.Chapter(0, "Retail", queries=["スーパー買い出し"])

        def search(*_args, **_kwargs):
            return ([{"id": f"ig-{i}", "_platform": "instagram"} for i in range(4)]
                    + [{"id": f"tt-{i}", "_platform": "tiktok"} for i in range(4)])

        def download(_item, destination, _status):
            Path(destination).write_bytes(FAKE_MP4)
            return str(destination)

        with tempfile.TemporaryDirectory() as folder, \
                mock.patch.object(v3.clip_scraper, "backend_search", side_effect=search), \
                mock.patch.object(v3.scrape_v2, "download_proxy_v2", side_effect=download), \
                mock.patch.object(v3.clip_scraper, "_item_meta", return_value={}):
            rows = v3.gather_chapter_sources(chapter, ["tiktok", "instagram"], folder, set())
        self.assertTrue(rows)
        self.assertEqual([row["platform"] for row in rows[:4]], ["tiktok"] * 4)
        self.assertEqual(sum(row["platform"] == "tiktok" for row in rows), 4)


class RejectionTests(unittest.TestCase):
    """Only genuinely wrong or broken material may be rejected. Modest text, few likes and an
    imperfect metadata match are what emptied V2's timelines and are not reasons here."""

    def setUp(self):
        self.chapter = v3.Chapter(chapter_id=0, title="C", forbidden=["cartoon"])

    def test_the_real_reasons_are_caught(self):
        for desc, expected in (
                ({"usable": False}, "no_visible_action"),
                ({"slideshow": True}, "slideshow_static"),
                ({"native_9_16": False}, "not_vertical"),
                ({"repeated_frames": True}, "repeated_frames"),
                ({"captions_cover_subject": True}, "captions_cover_subject"),
                ({"wrong_country": True}, "wrong_country"),
                ({"match_class": "unrelated"}, "unrelated_filler"),
                ({"summary": "a cartoon explainer"}, "unrelated_filler"),
        ):
            self.assertEqual(v3.rejection_reason(desc, self.chapter), expected, desc)

    def test_modest_text_and_low_likes_are_not_reasons(self):
        for desc in ({"burned_captions": True}, {"likes": 12}, {"metadata_relevance": 0.1},
                     {"summary": "a street in Osaka"}, {}):
            self.assertEqual(v3.rejection_reason(desc, self.chapter), "", desc)


class ShotTimingTests(unittest.TestCase):
    def test_shots_stay_readable_and_fill_the_chapter(self):
        lengths = v3.plan_shot_lengths(9.0, 3)
        self.assertEqual(len(lengths), 3)
        self.assertAlmostEqual(sum(lengths), 9.0, places=2)
        for value in lengths:
            self.assertGreaterEqual(value, v3.V3_CONFIG["shot_seconds_min"] - 0.01)
            self.assertLessEqual(value, v3.V3_CONFIG["shot_seconds_max"] + 0.01)

    def test_too_many_windows_for_the_time_means_fewer_shots_not_shorter_ones(self):
        lengths = v3.plan_shot_lengths(9.0, 8)
        self.assertLess(len(lengths), 8)
        for value in lengths:
            self.assertGreaterEqual(value, v3.V3_CONFIG["shot_seconds_min"] - 0.01)

    def test_a_demonstration_may_run_longer(self):
        self.assertGreater(max(v3.plan_shot_lengths(15.0, 3, is_demo=True)),
                           v3.V3_CONFIG["shot_seconds_max"])

    def test_a_short_window_is_slowed_a_little_never_frozen(self):
        window = v3.ShotWindow(1, "A", "tiktok", "a.mp4", 0.0, 3.0)
        self.assertEqual(v3.fit_window(window, 2.5)["speed"], 1.0)
        gentle = v3.fit_window(window, 3.2)
        self.assertGreaterEqual(gentle["speed"], v3.V3_CONFIG["slowdown_floor"])
        self.assertIsNone(v3.fit_window(window, 4.0),
                          "a window that would need a heavy slowdown must be refused")


class TimelineTests(unittest.TestCase):
    def test_the_timeline_is_built_from_the_windows(self):
        chapter = v3.Chapter(chapter_id=0, title="C", scene_ids=["00"], start=0.0, end=9.0)
        windows = [v3.ShotWindow(0, "A", "tiktok", "/x/a.mp4", 0.0, 4.0),
                   v3.ShotWindow(0, "B", "tiktok", "/x/b.mp4", 0.0, 4.0),
                   v3.ShotWindow(0, "C", "tiktok", "/x/c.mp4", 0.0, 4.0)]
        scenes = v3.align_windows_to_voice([chapter], {0: windows}, 9.0)
        self.assertEqual(len(scenes), 3)
        self.assertEqual(scenes[0]["start"], 0.0)
        self.assertEqual(scenes[-1]["end"], 9.0)
        for earlier, later in zip(scenes, scenes[1:]):
            self.assertAlmostEqual(earlier["end"], later["start"], places=2,
                                   msg="the timeline must tile without a hole")
        self.assertEqual([s["clip"] for s in scenes], ["a.mp4", "b.mp4", "c.mp4"])

    def test_a_chapter_with_nothing_is_marked_uncovered_not_filled(self):
        chapter = v3.Chapter(chapter_id=0, title="C", scene_ids=["00"], start=0.0, end=5.0)
        scenes = v3.align_windows_to_voice([chapter], {0: []}, 5.0)
        self.assertEqual(scenes[0]["assignment_type"], "uncovered")
        self.assertIsNone(scenes[0]["clip"])

    def test_a_long_uncovered_chapter_becomes_short_replacement_slots(self):
        chapter = v3.Chapter(chapter_id=0, title="C", scene_ids=["00"], start=0.0, end=8.0)
        scenes = v3.align_windows_to_voice([chapter], {0: []}, 8.0)
        self.assertGreater(len(scenes), 1)
        self.assertAlmostEqual(scenes[0]["start"], 0.0, places=2)
        self.assertAlmostEqual(scenes[-1]["end"], 8.0, places=2)
        self.assertTrue(all(float(scene["end"]) - float(scene["start"])
                            <= v3.V3_CONFIG["shot_seconds_max"] + 0.01 for scene in scenes))

    def test_nearby_voice_and_chapter_boundaries_do_not_make_millisecond_cuts(self):
        chapter = v3.Chapter(chapter_id=0, title="C", scene_ids=["00"],
                             start=6.97, end=20.31)
        voice = [{"start": 7.224, "end": 20.584, "script": "Line."}]
        scenes = v3.align_windows_to_voice([chapter], {0: []}, 20.31, voice_scenes=voice)
        self.assertTrue(all(float(scene["end"]) - float(scene["start"]) >= 1.0
                            for scene in scenes))

    def test_a_partial_window_is_kept_instead_of_discarding_the_whole_chapter(self):
        chapter = v3.Chapter(chapter_id=0, title="C", scene_ids=["00"], start=0.0, end=6.1)
        window = v3.ShotWindow(0, "A", "tiktok", "/x/a.mp4", 0.0, 3.3,
                               match_class="context")
        scenes = v3.align_windows_to_voice([chapter], {0: [window]}, 6.1)
        self.assertEqual(scenes[0]["clip"], "a.mp4")
        self.assertAlmostEqual(scenes[0]["end"], 3.3, places=2)
        self.assertTrue(any(scene["assignment_type"] == "uncovered" for scene in scenes[1:]))
        self.assertAlmostEqual(scenes[-1]["end"], 6.1, places=2)

    def test_same_source_overlapping_part_is_not_reused_across_chapters(self):
        shared = lambda cid: v3.ShotWindow(cid, "SAME", "tiktok", "/x/s.mp4", 0.0, 3.0)
        cleaned = v3.drop_cross_chapter_reuse({0: [shared(0)], 1: [shared(1)]})
        self.assertEqual(len(cleaned[0]), 1)
        self.assertEqual(len(cleaned[1]), 0)
        self.assertEqual(v3.cross_chapter_conflicts(cleaned), [])

    def test_same_source_distinct_parts_are_allowed_across_chapters(self):
        first = v3.ShotWindow(0, "SAME", "tiktok", "/x/s.mp4", 0.0, 3.0,
                              reason="cashier scans a basket at the checkout")
        later = v3.ShotWindow(1, "SAME", "tiktok", "/x/s.mp4", 18.0, 21.0,
                              reason="employee restocks drinks in a back aisle")
        cleaned = v3.drop_cross_chapter_reuse({0: [first], 1: [later]})
        self.assertEqual(len(cleaned[0]), 1)
        self.assertEqual(len(cleaned[1]), 1)
        self.assertEqual(v3.cross_chapter_conflicts(cleaned), [])

    def test_same_source_same_visible_part_is_not_reused_at_new_timestamps(self):
        first = v3.ShotWindow(0, "SAME", "tiktok", "/x/s.mp4", 0.0, 3.0,
                              reason="ceiling speaker above a store aisle")
        later = v3.ShotWindow(1, "SAME", "tiktok", "/x/s.mp4", 20.0, 23.0,
                              reason="ceiling speaker above a store aisle")
        cleaned = v3.drop_cross_chapter_reuse({0: [first], 1: [later]})
        self.assertEqual(len(cleaned[0]), 1)
        self.assertEqual(len(cleaned[1]), 0)

    def test_visual_signature_blocks_reworded_duplicate_moment(self):
        first = v3.ShotWindow(0, "SAME", "tiktok", "/x/s.mp4", 0.0, 3.0,
                              reason="staff handles the music cue",
                              visual_signature="cashier | scans basket | supermarket checkout")
        later = v3.ShotWindow(1, "SAME", "tiktok", "/x/s.mp4", 20.0, 23.0,
                              reason="the hidden retail signal is at work",
                              visual_signature="cashier | scans basket | supermarket checkout")
        cleaned = v3.drop_cross_chapter_reuse({0: [first], 1: [later]})
        self.assertEqual(len(cleaned[0]), 1)
        self.assertEqual(len(cleaned[1]), 0)

    def test_reuse_inside_one_chapter_is_allowed(self):
        windows = [v3.ShotWindow(0, "SAME", "tiktok", "/x/s.mp4", 0.0, 3.0),
                   v3.ShotWindow(0, "SAME", "tiktok", "/x/s.mp4", 8.0, 11.0)]
        cleaned = v3.drop_cross_chapter_reuse({0: windows})
        self.assertEqual(len(cleaned[0]), 2)

    def test_final_audit_replaces_a_bad_first_choice_with_the_next_window(self):
        chapter = v3.Chapter(0, "C", ["00"], 0.0, 3.0)
        windows = {0: [
            v3.ShotWindow(0, "BAD", "tiktok", "/x/bad.mp4", 0.0, 4.0),
            v3.ShotWindow(0, "GOOD", "tiktok", "/x/good.mp4", 0.0, 4.0),
        ]}
        voice = [{"id": "00", "start": 0.0, "end": 3.0, "script": "Line."}]
        with mock.patch.object(v3, "technical_window_issue",
                               side_effect=lambda path, *_a, **_k: "repeated_frames" if "bad" in path else ""):
            scenes, cleaned = v3.build_audited_timeline(
                [chapter], windows, 3.0, voice, "ffmpeg")
        self.assertEqual(scenes[0]["scrape_clip_id"], "GOOD")
        self.assertEqual([window.source_id for window in cleaned[0]], ["GOOD"])


class SceneShapeTests(unittest.TestCase):
    """The first live V3 run died in plan_config on scene['script']. A V3 scene is a SHOT rather
    than a sentence, but everything downstream still reads a scene the way V2 built one, and
    plan_config indexes four keys directly with no .get."""

    def _scenes(self):
        chapter = v3.Chapter(chapter_id=0, title="C", scene_ids=["00", "01"], start=0.0, end=9.0)
        voice = [{"id": "00", "start": 0.0, "end": 4.5, "script": "First line.",
                  "micro_beat": "a", "must_show": ["x"]},
                 {"id": "01", "start": 4.5, "end": 9.0, "script": "Second line.",
                  "micro_beat": "b", "must_show": ["y"]}]
        windows = [v3.ShotWindow(0, sid, "tiktok", f"/x/{sid}.mp4", 0.0, 4.0)
                   for sid in ("A", "B", "C")]
        return v3.align_windows_to_voice([chapter], {0: windows}, 9.0, voice_scenes=voice)

    def test_every_key_plan_config_indexes_directly_is_present(self):
        import re
        import inspect
        import agent_core
        body = inspect.getsource(agent_core.plan_config)
        required = set(re.findall(r"scene\[[\"']([a-z_]+)[\"']\]", body))
        self.assertIn("script", required, "the key the first run died on")
        for scene in self._scenes():
            missing = [key for key in required if key not in scene]
            self.assertEqual(missing, [], f"scene {scene['id']} is missing {missing}")

    def test_a_shot_carries_the_narration_it_sits_over(self):
        scenes = self._scenes()
        self.assertEqual(scenes[0]["script"], "First line.")
        self.assertEqual(scenes[-1]["script"], "Second line.")
        # A shot spanning the boundary carries both lines rather than picking one.
        self.assertEqual(scenes[1]["script"], "First line. Second line.")

    def test_the_voice_scene_s_other_fields_survive(self):
        """Copying the underlying line rather than inventing a dict is what makes every field the
        rest of the pipeline expects present, whatever it happens to be."""
        for scene in self._scenes():
            self.assertIn("micro_beat", scene)
            self.assertIn("must_show", scene)

    def test_an_uncovered_chapter_is_still_a_valid_scene(self):
        chapter = v3.Chapter(chapter_id=0, title="C", scene_ids=["00"], start=0.0, end=5.0)
        voice = [{"id": "00", "start": 0.0, "end": 5.0, "script": "Only line."}]
        scene = v3.align_windows_to_voice([chapter], {0: []}, 5.0, voice_scenes=voice)[0]
        self.assertEqual(scene["assignment_type"], "uncovered")
        self.assertIsNone(scene["clip"])
        self.assertEqual(scene["script"], "Only line.")

    def test_it_survives_having_no_voice_scenes_at_all(self):
        chapter = v3.Chapter(chapter_id=0, title="C", scene_ids=[], start=0.0, end=3.0)
        window = v3.ShotWindow(0, "A", "tiktok", "/x/a.mp4", 0.0, 3.0)
        scene = v3.align_windows_to_voice([chapter], {0: [window]}, 3.0, voice_scenes=[])[0]
        self.assertEqual(scene["script"], "")
        self.assertEqual(scene["clip"], "a.mp4")


class EngineSelectionTests(unittest.TestCase):
    """V4 is the engine a new Clip Short runs on; V2 and V3 stay reachable and unmodified.

    These assertions were written when V3 was the default. V4 landed (scrape_v4.py, Scrape.do
    discovery, refuse-partial contract) and became the default in the shell and in app.py, so the
    old text no longer described the product. What is worth protecting is unchanged: ONE default,
    stated identically everywhere, and the older engines still present rather than half-removed.
    """

    def test_the_run_accepts_every_engine_and_keeps_v2_v3_untouched(self):
        import inspect
        import agent_core
        whole = inspect.getsource(agent_core)
        self.assertIn('if _scrape_engine not in ("v1", "v2", "v3", "v4")', whole)
        self.assertIn("import scrape_v3", whole)
        self.assertIn("scrape_v3.scrape_social_plan_v3", whole)
        # V2 still runs through its own branch and its own module.
        self.assertIn("scrape_v2.scrape_social_plan_v2", whole)
        # ...and V4 through its own.
        self.assertIn("scrape_v4", whole)

    def test_new_clip_shorts_default_to_v4_everywhere(self):
        """Three places state the default. They drifting apart is how a run silently uses an
        engine the screen never showed."""
        from pathlib import Path as _Path
        shell = _Path("static/chat-shell.js").read_text(encoding="utf-8")
        app = _Path("app.py").read_text(encoding="utf-8")
        self.assertIn('if (!v.scraping_engine) v.scraping_engine = "v4";', shell)
        self.assertIn('S.values.scraping_engine = "v4";', shell)
        self.assertIn('"scraping_engine": "v4"', app)

    def test_a_project_with_no_saved_engine_still_resolves_to_something_explicit(self):
        """Changed deliberately 2026-09-02: an unrecorded engine now means V4, not V3.

        This test used to pin the opposite, on the reasoning that a rerun of old work should
        keep its known-good engine. The user's call, in their words: "es soll nich zurückfallen".
        A saved preset or an older project carries no `scraping_engine`, so V3 was what those
        runs actually got - without the measured caption-coverage gate, the vision source review
        or the duplicate policy - while the UI presented them as V4 throughout.
        """
        from pathlib import Path as _Path
        core = _Path("agent_core.py").read_text(encoding="utf-8")
        self.assertIn('or "v4").strip().lower()', core)
        self.assertIn('_scrape_engine = "v4"', core)
        self.assertIn('config["_scrape_engine"] = _scrape_engine', core,
                      "the chosen engine must survive later rework and dedup passes")

    def test_v3_searches_like_an_editor_before_leaving_a_gap(self):
        self.assertGreaterEqual(v3.V3_CONFIG["max_search_rounds"], 5)
        self.assertIn("GENERAL INVISIBLE-CONCEPT RULE", v3.GRADE_PROMPT)
        self.assertIn("search ladder", v3.ADAPTIVE_QUERY_PROMPT)
        self.assertIn("different footage hypothesis", v3.ADAPTIVE_QUERY_PROMPT)

    def test_native_query_status_cannot_kill_a_windows_worker(self):
        import tiktok_login

        class AsciiConsole(io.StringIO):
            encoding = "ascii"

        output = AsciiConsole()
        with contextlib.redirect_stdout(output):
            tiktok_login._status(None, "TikTok search: スーパー 店内BGM")
        self.assertIn("TikTok search:", output.getvalue())

    def test_environmental_sign_text_does_not_enable_caption_removal(self):
        import tempfile
        from pathlib import Path
        import agent_core
        with tempfile.TemporaryDirectory() as folder:
            scene = {"id": "0", "start": 0.0, "end": 3.0, "script": "A road sign.",
                     "clip": "source.mp4", "source_has_captions": False}
            config = agent_core.plan_config(Path(folder), "T", scene["script"], 3.0,
                                            allow_seedance=False, max_seedance=0,
                                            static_gpt_image_count=0, scenes_override=[scene])
        self.assertFalse(config["scenes"][0]["blur_captions"])

    def test_real_creator_captions_do_enable_caption_removal(self):
        import tempfile
        from pathlib import Path
        import agent_core
        with tempfile.TemporaryDirectory() as folder:
            scene = {"id": "0", "start": 0.0, "end": 3.0, "script": "A creator speaks.",
                     "clip": "source.mp4", "source_has_captions": True}
            config = agent_core.plan_config(Path(folder), "T", scene["script"], 3.0,
                                            allow_seedance=False, max_seedance=0,
                                            static_gpt_image_count=0, scenes_override=[scene])
        self.assertTrue(config["scenes"][0]["blur_captions"])


if __name__ == "__main__":
    unittest.main()
