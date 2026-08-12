"""Offline unit tests for Scrape V2 pure logic + V1 compatibility.

Run: python test_scrape_v2.py   (no network / login / ffmpeg needed)

Covers the acceptance-criteria pieces that do NOT require a live scrape: settings default/validation,
visual-intent query generation + diversity, relevance-first ranking (500-like relevant beats
500k-like irrelevant), segment thresholds + multi-region windows, match floors, near-duplicate
detection, global assignment caps, render-validation-v2, and that Scrape V1 is untouched.
"""

import scrape_v2 as v
import json
import tempfile
from pathlib import Path


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
    check("native 720x1280 accepted", v._is_native_9_16(720, 1280))
    check("landscape rejected by native gate", not v._is_native_9_16(1920, 1080))
    check("4:5 portrait rejected by native gate", not v._is_native_9_16(1080, 1350))


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
    # BROAD-DISCOVERY (2026-07-12): specific multi-word phrases find ~0 results on real
    # TikTok/X search, so every query is hard-trimmed (EN: 3 tokens, JA: 2 tokens). The
    # vision matcher finds the exact matching seconds inside the found videos.
    check("English query trimmed to 3 broad tokens", "strict school drill" in texts)
    check("Japanese query trimmed to 2 broad tokens", "学校 厳しい" in texts)
    check("no query exceeds the broad caps",
          all(len(q.query.split()) <= (2 if q.language == "ja" else 3) for q in queries))
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
    q0 = queries[0].query
    # All three backends collect the same platform result neighbourhood and sort it locally.
    # Re-running the same navigation for likes/views wastes time and returns duplicates.
    check("each raw term fetches each result neighbourhood once",
          calls == [(q0, "RELEVANCE"), (" ".join(q0.split()[:-1]), "RELEVANCE")])
    # A 3-token failure may shed only its final disambiguator; it must retain two anchor tokens.
    check("zero-result retry preserves a 2-token anchor",
          len(calls) == 2 and len(calls[-1][0].split()) == 2)
    check("anchor-preserving retry is counted", state.get("broadened_queries", 0) == 1)
    check("single sort mode is reported", state.get("sort_pass_counts") == {"RELEVANCE": 2})

    calls = []
    try:
        v.clip_scraper.backend_search = fake_search
        v._search_sources(queries[:1], ["tiktok", "x"], None, None, set(), {}, None,
                          coverage_pass=True)
    finally:
        v.clip_scraper.backend_search = original
    check("coverage wave uses relevance before popularity expansion",
          calls and all(mode == "RELEVANCE" for _, mode in calls))


def test_platform_query_sanitizer():
    check("TikTok suffix removed from Japanese platform query",
          v.sanitize_platform_query("アイドル ダンス TikTok") == "アイドル ダンス")
    check("platform word removed without damaging content",
          v.sanitize_platform_query("cute Japan TikTok dance") == "cute Japan dance")
    check("X and Instagram meta terms removed",
          v.sanitize_platform_query("Japan office x.com Instagram") == "Japan office")
    check("scene ID and comma separators removed",
          v.sanitize_platform_query("#03 #女の子, ダンス") == "#女の子 ダンス")


def test_scene_bound_query_plan():
    sleep = v.VisualIntent(
        scene_id=1, scene_text="A salaryman sleeps on a train.", visual_type="concrete",
        subject="salaryman", action="sleeping", location="train",
        english_queries=["office worker asleep train"])
    romance = v.SearchQueryV2(
        query="japan couple", language="en", tier="exact_action",
        visual_intent_id=sleep.intent_id, scene_ids=[1],
        expected_subject="salaryman", expected_action="sleeping", expected_location="train")
    valid, reason, _normalized = v.validate_query_against_intent(romance, sleep)
    check("foreign romance query rejected before search", not valid and "unrelated couple" in reason)
    with tempfile.TemporaryDirectory() as tmp:
        by_scene, audit = v.build_scene_bound_query_plan(
           [sleep], Path(tmp), "A salaryman sleeps on a train.")
        saved = json.loads((Path(tmp) / "review" / "search_plan_audit.json").read_text(encoding="utf-8"))
    check("scene-bound plan retains provenance",
          bool(by_scene[1]) and all(q.scene_ids == [1] for q in by_scene[1]))
    check("search plan audit persists current script hash",
          saved["script_sha256"] == audit["script_sha256"] and saved["coverage_by_scene"]["1"] > 0)

    # A Japanese-context beat must not lose native queries because a producer mislabelled
    # them. The Architect files native strings under its "english" array, and on the tokyo
    # project that discarded 62 usable queries - 渋谷 イルミネーション, 代々木公園, 新宿駅 -
    # before a single search ran. The gate reads the text now, not the label.
    jp_beat = v.VisualIntent(
        scene_id=2, scene_text="Japanese couples keep a gap on Tokyo streets.",
        visual_type="concrete", subject="Japanese couple", action="walking apart",
        location="Tokyo street",
        platform_queries={"tiktok": {"english": ["カップル 距離感", "tokyo couple gap"],
                                     "japanese": ["人混み デート"]}})
    with tempfile.TemporaryDirectory() as tmp:
        by_scene2, audit2 = v.build_scene_bound_query_plan(
            [jp_beat], Path(tmp), "Japanese couples keep a gap on Tokyo streets.")
    kept = {q.query for q in by_scene2.get(2, [])}
    dropped = {r["text"] for r in audit2["rejected_before_search"]}
    check("a native query filed under 'english' survives the Japanese-context gate",
          "カップル 距離感" in kept and "カップル 距離感" not in dropped)
    # A bare place name is still dropped - but for being too general, not for "not being
    # Japanese". The distinction matters: the first is a real editorial rule, the second was
    # a bug that silently deleted native terms.
    bare = v.VisualIntent(
        scene_id=3, scene_text="Japanese couples keep a gap on Tokyo streets.",
        visual_type="concrete", subject="Japanese couple", action="walking apart",
        location="Tokyo street",
        platform_queries={"tiktok": {"english": ["代々木公園"]}})
    with tempfile.TemporaryDirectory() as tmp:
        _by3, audit3 = v.build_scene_bound_query_plan(
            [bare], Path(tmp), "Japanese couples keep a gap on Tokyo streets.")
    why = {r["text"]: r["reason"] for r in audit3["rejected_before_search"]}
    check("a bare place name is dropped for generality, not for its language",
          "too general" in why.get("代々木公園", ""))
    check("a genuinely English query is still dropped for a Japanese-context beat",
          "tokyo couple gap" not in kept)


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


def test_provenance_and_editorial_gates():
    japanese_intent = v.VisualIntent(
        scene_id=0, scene_text="Why do many Japanese women style their bangs in Tokyo?",
        visual_type="concrete", subject="Japanese woman", action="styling bangs", location="Tokyo")
    query = v.SearchQueryV2(
        query="前髪 セット", language="ja", tier="exact_action", visual_intent_id="scene_0",
        expected_subject="woman", expected_action="styling bangs", expected_location="Tokyo",
        requires_japanese_context=True)
    native = v.SourceVideoCandidate(
        platform="tiktok", source_id="native", creator_id="creator", url="", width=720, height=1280,
        caption="前髪をセットする", hashtags=["#前髪"], query=query.query, query_tier=query.tier)
    generic = v.SourceVideoCandidate(
        platform="tiktok", source_id="generic", creator_id="creator", url="", width=720, height=1280,
        caption="hair styling tutorial", hashtags=["#bangs"], query=query.query, query_tier=query.tier)
    ranked = v.rank_metadata_candidates_v2([generic, native], query)
    check("Japan-specific query rejects source without native Japanese provenance",
          [c.source_id for c in ranked] == ["native"])
    good = v.SegmentCandidate(segment_id="jp", source_id="native", platform="tiktok", source_path="",
                              start_time=0, end_time=3, duration=3, query="前髪 セット",
                              japanese_context=True, visual_description={"age_confidence": "adult"})
    check("adult native-source clip clears editorial gate",
          v.editorial_rejection_reason(good, japanese_intent) == "")
    teen = v.SegmentCandidate(**{**good.__dict__, "segment_id": "teen",
                                 "visual_description": {"age_confidence": "teen"}})
    check("teen creator footage rejected", "minor" in v.editorial_rejection_reason(teen, japanese_intent))
    # Burned captions alone no longer disqualify a clip. Measured on 20 clips the scraper
    # had just downloaded for a Japanese topic, 17 carried on-screen text inside the exact
    # window that would be cut - the old blanket rule threw away 85% of the pool, which is
    # why the search kept coming back empty on a topic TikTok is full of. The render blurs
    # a lower third; what it cannot fix is text ON the subject, or a frame that is a slide.
    captions = v.SegmentCandidate(**{**good.__dict__, "segment_id": "caption",
                                     "visual_description": {"age_confidence": "adult", "burned_captions": True}})
    # This assertion was the other way round for one commit, on the theory that the render
    # blurs a lower third. An audit showed the blur fails silently when its OCR finds
    # nothing, cannot handle word-by-word or vertical or coloured captions, and produced zero
    # blurred files on the one real artefact on disk. Captions are fatal again.
    check("burned creator captions are rejected",
          "captions" in v.editorial_rejection_reason(captions, japanese_intent))
    over_subject = v.SegmentCandidate(**{**good.__dict__, "segment_id": "over",
                                         "rejection_reasons": ["burned_caption_over_subject"],
                                         "visual_description": {"age_confidence": "adult"}})
    check("a caption sitting ON the subject is still rejected",
          v.editorial_rejection_reason(over_subject, japanese_intent) != "")
    slide = v.SegmentCandidate(**{**good.__dict__, "segment_id": "slide", "text_heaviness": 6.0,
                                  "visual_description": {"age_confidence": "adult"}})
    check("a frame that is mostly text is still rejected",
          "text-heavy" in v.editorial_rejection_reason(slide, japanese_intent))
    overlay = v.SegmentCandidate(**{**good.__dict__, "segment_id": "pip",
                                    "visual_description": {"age_confidence": "adult",
                                                           "creator_overlay": True}})
    check("a creator reaction overlay is still rejected (it cannot be blurred away)",
          v.editorial_rejection_reason(overlay, japanese_intent) != "")
    no_provenance = v.SegmentCandidate(**{**good.__dict__, "segment_id": "generic",
                                          "japanese_context": False})
    check("Japan scene rejects clip without native provenance",
          "Japanese source" in v.editorial_rejection_reason(no_provenance, japanese_intent))
    declined = v.SegmentCandidate(**{**good.__dict__, "segment_id": "declined",
        "source_path": r"C:\\project\\seedance 2.0\\_declined\\declined_v2_bad.mp4"})
    check("previously declined footage can never re-enter V2 matching",
          "previously rejected" in v.editorial_rejection_reason(declined, japanese_intent))


def test_download_budget_is_spent_once():
    """A round must attempt exactly the downloads it was given, not half of them.

    The concurrent-prefetch rewrite reserved each planned source in _downloaded_ids AND
    added len(planned) to the same guard, so every source counted twice and a budget of 6
    delivered 3. min_downloads_per_scene = 3 quietly became 1.5 per beat.
    """
    calls = []

    def fake_download(raw_item, proxy, status_cb=None):
        calls.append(proxy)
        return None                     # counts as a failed fetch; stops before ffmpeg

    real = v.download_proxy_v2
    v.download_proxy_v2 = fake_download
    try:
        for budget, spent, want in ((6, 0, 6), (3, 0, 3), (36, 30, 6), (5, 5, 0)):
            calls.clear()
            state = {"rejections": {}, "_downloaded_ids": {f"old{i}" for i in range(spent)}}
            sources = [v.SourceVideoCandidate(source_id=f"s{i}", platform="tiktok",
                                              creator_id="c", url="https://x/%d" % i,
                                              query="q", raw_item={})
                       for i in range(40)]
            with tempfile.TemporaryDirectory() as tmp:
                v._download_and_segment(sources, Path(tmp), "ffmpeg", "ffprobe",
                                        None, None, state, None, budget)
            check(f"budget {budget} with {spent} spent attempts {want}", len(calls) == want)
    finally:
        v.download_proxy_v2 = real


def test_uncovered_beats_borrow_motion():
    """Clips only - and the borrow has to survive all the way to the renderer.

    The first version of this test hand-fed the fixture a "source_duration" key and asserted
    against it. No scene dict ever carries that key, so the test passed while the feature did
    nothing: the in-point never shifted, and the borrowed clip was popped again by
    agent_core, which rebuilds every scene from the scene_clips list. Real files and the real
    list now, so a fiction cannot pass twice.
    """
    import subprocess as _sp
    ff = v.clip_scraper._ffmpeg_tools()[0]
    with tempfile.TemporaryDirectory() as tmp:
        made = []
        for name, secs in (("a.mp4", 12), ("b.mp4", 12)):
            out = Path(tmp) / name
            _sp.run([str(ff), "-y", "-loglevel", "error", "-f", "lavfi",
                     "-i", f"testsrc=size=64x64:rate=8:duration={secs}",
                     "-pix_fmt", "yuv420p", str(out)], capture_output=True, timeout=120)
            made.append(out)
        if not all(m.exists() for m in made):
            check("borrow fixture built", False)
            return
        scenes = [
            {"id": 0, "clip": "a.mp4", "asset": "a.mp4", "assignment_type": "exact"},
            {"id": 1, "assignment_type": "uncovered_still", "match_class": "UNMATCHED",
             "scrape_uncovered_reason": "no approved segment"},
            {"id": 2, "assignment_type": "uncovered_still", "match_class": "UNMATCHED",
             "scrape_uncovered_reason": "no approved segment"},
            {"id": 3, "clip": "b.mp4", "asset": "b.mp4", "assignment_type": "exact"},
        ]
        scene_clips = [str(made[0]), None, None, str(made[1])]
        borrowed = v.borrow_motion_for_uncovered(scenes, scene_clips)

    check("both footage-less beats borrow motion", borrowed == 2)
    check("no beat is left on a still",
          not any(sc.get("assignment_type") == "uncovered_still" for sc in scenes))
    check("a borrow takes the NEAREST donor",
          scenes[1]["borrowed_from_scene"] == 0 and scenes[2]["borrowed_from_scene"] == 3)
    check("a borrow is labelled, not disguised as a match",
          all(scenes[i]["match_class"] == "BORROWED" for i in (1, 2)))
    check("the uncovered reason survives the borrow",
          scenes[1]["scrape_uncovered_reason"] == "no approved segment")
    # THE ONE THAT WAS MISSING: agent_core rebuilds each scene from this list and pops the
    # clip of anything absent from it. A borrow that only edits the scene dict is erased.
    check("the borrow is written into the scene_clips list the renderer reads",
          scene_clips[1] == scene_clips[0] and scene_clips[2] == scene_clips[3])
    check("a borrowed beat starts at a different in-point than its donor",
          scenes[1].get("source_trim", 0) > 0)

    only_stills = [{"id": 0, "assignment_type": "uncovered_still"}]
    check("with no footage at all the still fallback survives",
          v.borrow_motion_for_uncovered(only_stills, [None]) == 0
          and only_stills[0]["assignment_type"] == "uncovered_still")


def test_download_budget_is_spent_once():
    """A round must attempt exactly the downloads it was given, not half of them.

    The concurrent-prefetch rewrite reserved each planned source in _downloaded_ids AND
    added len(planned) to the same guard, so every source counted twice and a budget of 6
    delivered 3. min_downloads_per_scene = 3 quietly became 1.5 per beat.
    """
    calls = []

    def fake_download(raw_item, proxy, status_cb=None):
        calls.append(proxy)
        return None                     # counts as a failed fetch; stops before ffmpeg

    real = v.download_proxy_v2
    v.download_proxy_v2 = fake_download
    try:
        for budget, spent, want in ((6, 0, 6), (3, 0, 3), (36, 30, 6), (5, 5, 0)):
            calls.clear()
            state = {"rejections": {}, "_downloaded_ids": {f"old{i}" for i in range(spent)}}
            sources = [v.SourceVideoCandidate(source_id=f"s{i}", platform="tiktok",
                                              creator_id="c", url="https://x/%d" % i,
                                              query="q", raw_item={})
                       for i in range(40)]
            with tempfile.TemporaryDirectory() as tmp:
                v._download_and_segment(sources, Path(tmp), "ffmpeg", "ffprobe",
                                        None, None, state, None, budget)
            check(f"budget {budget} with {spent} spent attempts {want}", len(calls) == want)
    finally:
        v.download_proxy_v2 = real


def test_relationship_scene_queries():
    intent = v.VisualIntent(
        scene_id=4,
        scene_text="Only after the confession will the couple finally hold hands in public.",
        visual_type="concrete", subject="Japanese couple", action="holding hands", location="street",
        platform_queries={"tiktok": {"japanese": ["人 深呼吸"]}})
    queries = v.queries_for_intent(intent)
    texts = [q.query for q in queries]
    joined = " ".join(texts)
    check("dating beat gets concrete native hand-holding searches",
          any(term in joined for term in ("手繋ぎ", "恋人繋ぎ")))
    check("dating seeds are TikTok actions, not platform-name pollution",
          any(q.generated_from == "deterministic_relationship_scene_seed" and q.platforms == ["tiktok"]
              for q in queries) and all("tiktok" not in q.query.casefold() for q in queries))
    check("dating seeds are bound to their own scene",
          all(q.scene_ids == [4] for q in queries))
    # The seed used to be asserted as query[0]. That rule is what starved the Tokyo run:
    # the seeds are identical for every scene (カップル デート vlog and friends), so they
    # took slots 1-4 of all 14 beats and pushed each beat's own term (プリクラ 落書き,
    # ラブホ 自動精算機) to slot 5, which the run never reached. The beat's own term leads
    # now - but the seed must stay right behind it, because an Architect term can be junk
    # like 人 深呼吸 for a hand-holding beat and the coverage wave takes two per scene.
    seed_at = next((i for i, q in enumerate(queries)
                    if q.generated_from == "deterministic_relationship_scene_seed"), None)
    check("dating action seed stays inside the coverage-search slots",
          seed_at is not None and seed_at <= 2)
    inflected_hands = v.VisualIntent(
        scene_id=6, scene_text="The couple holds hands while walking home.",
        subject="Japanese couple", action="holding hands", location="street")
    check("inflected hand-holding narration keeps the hand-holding search seed",
          v.queries_for_intent(inflected_hands)[0].query == "カップル 手繋ぎ")
    confession = v.VisualIntent(
        scene_id=5, scene_text="A formal confession is required before dating.",
        subject="Japanese students", action="confessing feelings", location="after school")
    confession_queries = v.queries_for_intent(confession)
    check("confession opens in the populated high-school confession neighbourhood",
          confession_queries[0].query == "高校生 告白")
    confession_query = confession_queries[0]
    literal = v.SourceVideoCandidate(
        platform="tiktok", source_id="literal", creator_id="a", url="", width=720, height=1280,
        caption="高校生が放課後に告白した瞬間", hashtags=["高校生", "告白"],
        query=confession_query.query, query_tier=confession_query.tier)
    generic = v.SourceVideoCandidate(
        platform="tiktok", source_id="generic", creator_id="b", url="", width=720, height=1280,
        caption="高校生の青春ダンス", hashtags=["高校生", "青春"],
        query=confession_query.query, query_tier=confession_query.tier)
    ranked = v.rank_metadata_candidates_v2([generic, literal], confession_query)
    check("native confession anchors rank literal action above generic school context",
          ranked and ranked[0].source_id == "literal")


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
    # the current quality profile rejects ultra-short snippets below 2.2s
    check("2.2s is a valid segment length",
          cfg["min_segment_seconds"] <= 2.2 <= cfg["max_segment_seconds"])


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
    good = {"voice_speed": 1.10, "influencer_hook": False, "scenes": [
        {"id": 0, "clip": "hook.mp4", "visual_role": "hook_topic", "match_class": "A_MATCH",
         "assignment_type": "exact", "black_bar_score": 0.0, "native_9_16": True},
        {"id": 1, "clip": "b1.mp4", "visual_role": "body", "match_class": "B_MATCH",
         "assignment_type": "exact", "black_bar_score": 1.0, "native_9_16": True}]}
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
    check("unrelated emergency filler is rejected before rendering",
          _raises(lambda: v.validate_scrape_render_v2(mostly_emergency)))
    missing_clip = {"voice_speed": 1.20, "scenes": [dict(good["scenes"][0]),
                    {"id": 1, "visual_role": "body"}]}
    check("scene without clip rejected", _raises(lambda: v.validate_scrape_render_v2(missing_clip)))
    uncovered_still = {"voice_speed": 1.20, "scenes": [dict(good["scenes"][0]),
                       {"id": 1, "visual_role": "body", "assignment_type": "uncovered_still",
                        "match_class": "UNMATCHED"}]}
    check("uncovered V2 scene uses explicit still fallback instead of unrelated footage",
          v.validate_scrape_render_v2(uncovered_still))
    selected_rejected = {"voice_speed": 1.10, "influencer_hook": False, "scenes": [
        {"id": 0, "clip": "selected.mp4", "visual_role": "hook_topic",
         "match_class": "D_REJECTED", "assignment_type": "exact",
         "fallback_level": 0, "black_bar_score": 0.0, "native_9_16": True}]}
    check("semantically rejected footage cannot be relabeled as a fallback",
          _raises(lambda: v.validate_scrape_render_v2(selected_rejected)))
    no_hook = {"voice_speed": 1.20, "scenes": [
        {"id": 0, "clip": "b.mp4", "visual_role": "body", "match_class": "B_MATCH", "assignment_type": "exact"}]}
    check("stale opener role normalized", v.validate_scrape_render_v2(no_hook)
          and no_hook["scenes"][0]["visual_role"] == "hook_topic")


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
              test_platform_query_sanitizer,
              test_scene_bound_query_plan,
              test_ranking_relevance_over_likes, test_provenance_and_editorial_gates,
              test_relationship_scene_queries,
              test_uncovered_beats_borrow_motion,
              test_download_budget_is_spent_once,
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
