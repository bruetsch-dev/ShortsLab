"""Offline unit tests for Scrape V2 pure logic + V1 compatibility.

Run: python test_scrape_v2.py   (no network / login / ffmpeg needed)

Covers the acceptance-criteria pieces that do NOT require a live scrape: settings default/validation,
visual-intent query generation + diversity, relevance-first ranking (500-like relevant beats
500k-like irrelevant), segment thresholds + multi-region windows, match floors, near-duplicate
detection, global assignment caps, render-validation-v2, and that Scrape V1 is untouched.
"""

import scrape_v2 as v


_failures = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        _failures.append(name)


# ---- settings -------------------------------------------------------------
def test_settings():
    check("engine default is v2", v.normalize_engine(None) == "v2")
    check("engine default on empty", v.normalize_engine("") == "v2")
    check("v1 selectable", v.normalize_engine("v1") == "v1")
    check("v2 selectable", v.normalize_engine("V2") == "v2")
    check("invalid value falls back to v2", v.normalize_engine("garbage") == "v2")
    import app
    check("app default ui state = v2", app.normalize_ui_state({})["scraping_engine"] == "v2")
    check("app keeps saved v1", app.normalize_ui_state({"scraping_engine": "v1"})["scraping_engine"] == "v1")
    check("old config without engine does not crash",
          isinstance(app.normalize_ui_state({"clip_source": "scrape"}), dict))
    check("scraping_engine in preset fields (persisted)",
          '"scraping_engine"' in open("app.py", encoding="utf-8").read())


# ---- planner query generation + diversity ---------------------------------
def test_query_diversity():
    intent = v.VisualIntent(
        scene_id=3, scene_text="people feel lonely in the city", visual_type="context",
        subject="office worker", action="asleep on late train", location="Tokyo train at night",
        camera_style="handheld", mood="lonely", required_elements=["train"],
        alternative_visuals=[
            v.AlternativeVisualIntent(subject="woman", action="eating alone at ramen counter",
                                      location="ramen shop"),
            v.AlternativeVisualIntent(subject="salaryman", action="leaving office at night",
                                      location="office building")])
    qs = v.queries_for_intent(intent)
    check("queries generated", len(qs) >= 4)
    tiers = {q.tier for q in qs}
    check("multiple query tiers", len(tiers) >= 3)
    texts = [q.query for q in qs]
    # not merely synonyms of one phrase: alternative actions must appear
    joined = " ".join(texts)
    check("alternative actions present (different actions)",
          ("ramen" in joined) or ("eating" in joined) or ("leaving" in joined))
    # diversity: no two kept queries are near-identical
    kept = v.diversify_queries(texts, threshold=0.62)
    dupes = any(v.query_similarity(a, b) >= 0.9 for i, a in enumerate(kept) for b in kept[i + 1:])
    check("no near-identical queries after diversify", not dupes)
    check("diversify drops a pure synonym swap",
          v.diversify_queries(["一人ご飯 vlog", "一人ご飯 vlog", "一人ご飯　vlog"]) == ["一人ご飯 vlog"])


def test_architect_raw_queries_and_multi_sort():
    intent = v.VisualIntent(
        scene_id=1, scene_text="strict school discipline", match_category="shock",
        subject="students", action="synchronized drill", location="school gym",
        english_queries=["strict school drill caught", "synchronized students fail"],
        japanese_queries=["学校 厳しい あるある", "体育 一斉行動", "school 厳しい", "日本語（訳）"])
    queries = v.queries_for_intent(intent)
    texts = [q.query for q in queries]
    check("Architect English strings pass through unchanged", "strict school drill caught" in texts)
    check("native Japanese string passes through unchanged", "学校 厳しい あるある" in texts)
    check("Romaji/English is rejected from Japanese queries", "school 厳しい" not in texts)
    check("annotated Japanese is rejected", "日本語（訳）" not in texts)

    calls = []
    original = v.clip_scraper.backend_search
    try:
        def fake_search(query, limit, status_cb=None, sort=None, platforms=None, deadline=None):
            calls.append((query, sort))
            return []
        v.clip_scraper.backend_search = fake_search
        state = {}
        v._search_sources(queries[:1], ["tiktok", "x"], None, None, set(), state, None,
                          sort="RELEVANCE")
    finally:
        v.clip_scraper.backend_search = original
    check("each raw term searches relevance + likes + views",
          [mode for _, mode in calls] == ["RELEVANCE", "MOST_LIKED", "MOST_VIEWED"])
    check("sort passes are reported", state.get("sort_pass_counts") == {
          "RELEVANCE": 1, "MOST_LIKED": 1, "MOST_VIEWED": 1})


# ---- relevance-first ranking ----------------------------------------------
def test_ranking_relevance_over_likes():
    q = v.SearchQueryV2(query="office worker asleep train", language="en", tier="exact_action",
                        visual_intent_id="scene_1", expected_subject="office worker",
                        expected_action="asleep on train", expected_location="train")
    relevant_low_likes = v.SourceVideoCandidate(
        platform="tiktok", source_id="A", creator_id="a", url="", likes=500, width=720, height=1280,
        caption="tired office worker asleep on the last train home", hashtags=["train", "commute"],
        query=q.query, query_tier="exact_action")
    irrelevant_high_likes = v.SourceVideoCandidate(
        platform="tiktok", source_id="B", creator_id="b", url="", likes=500000, width=720, height=1280,
        caption="cute puppy compilation funny animals", hashtags=["dog", "funny"],
        query=q.query, query_tier="exact_action")
    ranked = v.rank_metadata_candidates_v2([irrelevant_high_likes, relevant_low_likes], q)
    check("relevant 500-like beats irrelevant 500k-like", ranked and ranked[0].source_id == "A")
    # V2 now gates on RELEVANCE, not likes: every tier's like floor is 0 (a like-gate skewed
    # results toward big Western viral clips even for Japanese queries).
    check("exact_action tier keeps 0-like clips (no like floor)",
          v._dynamic_like_floor_v2("exact_action", "tiktok") == 0)
    check("no tier has a like floor anymore",
          all(v._dynamic_like_floor_v2(t, "tiktok") == 0 for t in v.LIKE_FLOORS_V2))


# ---- segment discovery windows --------------------------------------------
def test_segment_windows():
    cfg = v.SCRAPE_V2_CONFIG
    windows = v._candidate_windows(30.0, cuts=[], cfg=cfg)
    check("multiple regions across a 30s video", len(windows) >= 3)
    check("first window is NOT forced to start at 0", windows[0][0] > 0.0)
    check("a later-region window exists (not just the intro)",
          any(w[0] > 10.0 for w in windows))
    for (s, e) in windows:
        d = e - s
        check("window %.1f-%.1f within [min,max]" % (s, e),
              cfg["min_segment_seconds"] - 0.01 <= d <= cfg["max_segment_seconds"] + 0.01)
    # a hard cut inside a region shrinks/moves the window off the cut
    w2 = v._candidate_windows(30.0, cuts=[6.2], cfg=cfg)
    check("windows still produced around a cut", len(w2) >= 3)
    # a 2.1s segment is acceptable
    check("2.1s is a valid segment length",
          cfg["min_segment_seconds"] <= 2.1 <= cfg["max_segment_seconds"])


# ---- match formula + floors -----------------------------------------------
def test_match_floors():
    # a high bucket-ish score with a weak literal script match must NOT pass concrete floors
    weak_script = v.semantic_match_score(subject_match=6, action_match=3, location_match=6,
                                         mood_match=6, script_match=3)
    check("weak script match fails concrete floor",
          not v.passes_match_floors(script_match=3, overall=weak_script, visual_type="concrete"))
    strong = v.semantic_match_score(subject_match=8, action_match=9, location_match=8,
                                    mood_match=8, script_match=8)
    check("strong concrete action passes", v.passes_match_floors(8, strong, "concrete"))
    check("abstract floor is lower than concrete",
          v.MATCH_THRESHOLDS_V2["abstract"]["script_floor"] < v.MATCH_THRESHOLDS_V2["concrete"]["script_floor"])
    check("action weighted highest in semantic score",
          v.semantic_match_score(0, 10, 0, 0, 0) > v.semantic_match_score(10, 0, 0, 0, 0))
    check("A_MATCH for very high overall", v.match_class_for(8.5, "concrete") == "A_MATCH")
    check("D_REJECTED below overall floor", v.match_class_for(4.0, "concrete") == "D_REJECTED")


# ---- near-duplicate --------------------------------------------------------
def test_near_duplicate():
    check("identical hashes are duplicates", v.hash_similarity("ffff0000ffff0000", "ffff0000ffff0000") == 1.0)
    check("very different hashes are not", v.hash_similarity("ffffffffffffffff", "0000000000000000") < 0.5)
    a = v.SegmentCandidate(segment_id="s1", source_id="v1", platform="tiktok", source_path="", start_time=0,
                           end_time=2.3, duration=2.3, creator_id="creatorX", frame_hash="ffff0000ffff0000")
    b = v.SegmentCandidate(segment_id="s2", source_id="v2", platform="tiktok", source_path="", start_time=0,
                           end_time=2.3, duration=2.3, creator_id="creatorX", frame_hash="ffff0000ffff0000")
    check("same-creator same-duration flagged near-dup", v.is_near_duplicate(a, b))


# ---- global assignment -----------------------------------------------------
def _seg(sid, source, creator, q=8.0):
    return v.SegmentCandidate(segment_id=sid, source_id=source, platform="tiktok", source_path="/x.mp4",
                              start_time=0, end_time=2.3, duration=2.3, creator_id=creator,
                              quality_score=q, frame_hash=sid.ljust(16, "0"))


def test_global_assignment():
    intents = [v.VisualIntent(scene_id=i, scene_text="s%d" % i, visual_type="concrete") for i in range(1, 4)]
    def cand(seg, overall):
        return {"segment": seg, "overall_match": overall, "style_match": 7.0,
                "match_class": "A_MATCH", "semantic_match": overall}
    scene_candidates = {
        1: [cand(_seg("s1", "src1", "cr1"), 9.0), cand(_seg("s2", "src2", "cr2"), 7.0)],
        2: [cand(_seg("s2", "src2", "cr2"), 8.5), cand(_seg("s3", "src3", "cr3"), 7.5)],
        3: [cand(_seg("s3", "src3", "cr3"), 8.0)],
    }
    asg = v.assign_segments_globally_v2(intents, scene_candidates)
    picks = {sid: a.segment_id for sid, a in asg.items() if a.segment_id}
    check("every scene assigned", len(picks) == 3)
    check("scene 1 got its best segment", picks.get(1) == "s1")
    check("no segment used more than twice", all(list(picks.values()).count(x) <= 2 for x in picks.values()))
    # creator cap
    creators = [_seg_creator(scene_candidates, sid, seg) for sid, seg in picks.items()]
    from collections import Counter
    check("creator cap respected (<=2)", all(c <= v.SCRAPE_V2_CONFIG["max_clips_per_creator"]
                                             for c in Counter(creators).values()))


def _seg_creator(scene_candidates, sid, seg_id):
    for c in scene_candidates[sid]:
        if c["segment"].segment_id == seg_id:
            return c["segment"].creator_id
    return None


# ---- render validation v2 --------------------------------------------------
def test_render_validation_v2():
    good = {"voice_speed": 1.20, "scenes": [
        {"id": 0, "clip": "hook.mp4", "visual_role": "hook_influencer", "match_class": "A_MATCH",
         "assignment_type": "exact", "black_bar_score": 0.0},
        {"id": 1, "clip": "b1.mp4", "visual_role": "body", "match_class": "B_MATCH",
         "assignment_type": "exact", "black_bar_score": 1.0}]}
    try:
        v.validate_scrape_render_v2(dict(good))
        check("valid v2 timeline passes", True)
    except Exception as e:
        check("valid v2 timeline passes (%s)" % e, False)
    # 1.0x is now a legitimate user choice at the speech gate; only out-of-range speeds fail
    bad_speed = dict(good); bad_speed["voice_speed"] = 0.5
    check("wrong speed rejected", _raises(lambda: v.validate_scrape_render_v2(bad_speed)))
    mostly_emergency = {"voice_speed": 1.20, "scenes": [
        dict(good["scenes"][0]),
        {"id": 1, "clip": "b1.mp4", "visual_role": "body", "assignment_type": "emergency_fallback"},
        {"id": 2, "clip": "b2.mp4", "visual_role": "body", "assignment_type": "emergency_fallback"}]}
    check("mostly-emergency timeline rejected",
          _raises(lambda: v.validate_scrape_render_v2(mostly_emergency)))
    missing_clip = {"voice_speed": 1.20, "scenes": [dict(good["scenes"][0]),
                    {"id": 1, "visual_role": "body"}]}
    check("scene without clip rejected", _raises(lambda: v.validate_scrape_render_v2(missing_clip)))
    no_hook = {"voice_speed": 1.20, "scenes": [
        {"id": 0, "clip": "b.mp4", "visual_role": "body", "match_class": "B_MATCH", "assignment_type": "exact"}]}
    check("hook-not-first rejected", _raises(lambda: v.validate_scrape_render_v2(no_hook)))


def _raises(fn):
    try:
        fn()
        return False
    except Exception:
        return True


# ---- V1 compatibility ------------------------------------------------------
def test_v1_untouched():
    import agent_core
    check("V1 scrape_social_plan still exists", hasattr(agent_core, "scrape_social_plan"))
    check("V1 validate_scrape_render still exists", hasattr(agent_core, "validate_scrape_render"))
    check("V1 build_social_search_plan still exists", hasattr(agent_core, "build_social_search_plan"))
    check("V1 score_hook_candidates still exists", hasattr(agent_core, "score_hook_candidates"))
    import clip_scraper
    check("V1 scrape_bucket still exists", hasattr(clip_scraper, "scrape_bucket"))


if __name__ == "__main__":
    for t in (test_settings, test_query_diversity, test_architect_raw_queries_and_multi_sort,
              test_ranking_relevance_over_likes,
              test_segment_windows, test_match_floors, test_near_duplicate,
              test_global_assignment, test_render_validation_v2, test_v1_untouched):
        print("\n== %s ==" % t.__name__)
        try:
            t()
        except Exception as exc:
            import traceback
            traceback.print_exc()
            _failures.append(t.__name__ + " raised " + repr(exc))
    print("\n" + ("ALL PASSED" if not _failures else "FAILURES: %d -> %s" % (len(_failures), _failures)))
    raise SystemExit(1 if _failures else 0)
