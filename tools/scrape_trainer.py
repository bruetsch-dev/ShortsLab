"""Human-in-the-loop Scrape Trainer for the Scrape-V2 pipeline.

The app scrapes REAL TikTok footage; this tool lets YOU rate every found clip against the exact
script part, stores your ratings as ground truth in SQLite, and learns which query TYPES / terms /
modifiers actually find good clips for your Japan-shorts - per category. Your manual rating always
wins; vision is optional pre-sorting only and NEVER removes a result before you review it.

It is fully isolated from production and REUSES the existing infrastructure (the same TikTok session,
metadata parser, Scrape-V2 download / segment / quality primitives, and the benchmark's curated
Visual Intents + query taxonomy) - it does NOT touch Scrape V1 or V2.

Two entry paths, ONE shared review / DB / analysis / strategy pipeline:
    start --intents [cats...]   -> scenes from the 53 curated Visual Intents (no LLM, $0)
    start --script <file>       -> scenes from a real voice script via ONE Scrape-V2 planner call

Commands:
    python -m tools.scrape_trainer start --intents school convenience_store [--vision]
    python -m tools.scrape_trainer start --script path/to/script.txt [--vision]
    python -m tools.scrape_trainer resume <run_id>
    python -m tools.scrape_trainer serve <run_id> [--port 7870]     # review UI
    python -m tools.scrape_trainer analyze <run_id>
    python -m tools.scrape_trainer propose-strategy <run_id>
    python -m tools.scrape_trainer compare v001 v002
    python -m tools.scrape_trainer activate v002
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import clip_scraper
import scrape_v2
from tools import tiktok_search_benchmark as bench   # reuse intents, query taxonomy, real search

try:
    import tiktok_login
except Exception:                                     # pragma: no cover
    tiktok_login = None

TRAINER_DIR = ROOT / "scrape_training"
RUNS_DIR = TRAINER_DIR / "runs"
STRATEGY_DIR = ROOT / "config" / "scrape_training"
TRAINER_ANALYSIS_VERSION = 1

# your rating vocabulary (section 4). Positive labels rank higher than backups than context.
LABELS = ["best_match", "good_match", "good_backup", "context_only",
          "relevant_unusable", "wrong_subject", "wrong_action", "wrong_location",
          "too_generic", "caption_heavy", "bad_quality", "duplicate",
          "completely_irrelevant", "not_sure"]
POSITIVE_LABELS = {"best_match", "good_match", "good_backup", "context_only"}
STRONG_LABELS = {"best_match", "good_match"}
LABEL_SCORE = {"best_match": 1.0, "good_match": 0.8, "good_backup": 0.55, "context_only": 0.35}


# ---------------------------------------------------------------- data model

@dataclass
class TrainScene:
    scene_id: int
    script_text: str
    category: str
    visual_type: str
    subject: str
    action: str
    location: str
    jp_subject: str = ""
    jp_action: str = ""
    jp_location: str = ""

    def to_benchintent(self):
        # reuse the benchmark's controlled query taxonomy unchanged
        return bench.BenchIntent(
            intent_id=f"scene_{self.scene_id}", category=self.category, visual_type=self.visual_type,
            subject=self.subject, action=self.action, location=self.location,
            jp_subject=self.jp_subject, jp_action=self.jp_action, jp_location=self.jp_location,
            must_show=[], avoid=[])


# ---------------------------------------------------------------- database (SQLite, durable)

def _db_path(run_id):
    return RUNS_DIR / run_id / "trainer.db"


def db_connect(run_id):
    p = _db_path(run_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(p), timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript("""
    CREATE TABLE IF NOT EXISTS runs(
        run_id TEXT PRIMARY KEY, created_at TEXT, mode TEXT, script_text TEXT,
        vision_on INTEGER, strategy_version TEXT);
    CREATE TABLE IF NOT EXISTS scenes(
        run_id TEXT, scene_id INTEGER, script_text TEXT, category TEXT, visual_type TEXT,
        subject TEXT, action TEXT, location TEXT, jp_subject TEXT, jp_action TEXT, jp_location TEXT,
        queries_json TEXT, PRIMARY KEY(run_id, scene_id));
    CREATE TABLE IF NOT EXISTS results(
        run_id TEXT, scene_id INTEGER, source_id TEXT, creator_id TEXT, caption TEXT,
        hashtags_json TEXT, likes INTEGER, duration REAL, width INTEGER, height INTEGER,
        is_slideshow INTEGER, search_position INTEGER, proxy_path TEXT, seg_start REAL, seg_end REAL,
        tech_scores_json TEXT, vision_scores_json TEXT, reject_reasons_json TEXT,
        found_by_queries_json TEXT, PRIMARY KEY(run_id, scene_id, source_id));
    CREATE TABLE IF NOT EXISTS ratings(
        run_id TEXT, scene_id INTEGER, source_id TEXT, label TEXT, is_primary INTEGER,
        is_backup INTEGER, seg_start REAL, seg_end REAL, comment TEXT, better_query TEXT,
        strategy_version TEXT, review_ts TEXT, PRIMARY KEY(run_id, scene_id, source_id));
    """)
    con.commit()
    return con


def db_insert_scene(con, run_id, scene: TrainScene, qmap):
    con.execute("INSERT OR REPLACE INTO scenes VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, scene.scene_id, scene.script_text, scene.category, scene.visual_type,
                 scene.subject, scene.action, scene.location, scene.jp_subject, scene.jp_action,
                 scene.jp_location, json.dumps(qmap, ensure_ascii=False)))
    con.commit()


def db_upsert_result(con, run_id, scene_id, r, query, query_type):
    """Insert a result, or append this query to found_by_queries if the source already exists for
    this scene (a clip found by several queries is ONE row with all its queries)."""
    row = con.execute("SELECT found_by_queries_json FROM results WHERE run_id=? AND scene_id=? AND source_id=?",
                      (run_id, scene_id, r["source_id"])).fetchone()
    fb = [{"query": query, "query_type": query_type}]
    if row:
        try:
            existing = json.loads(row["found_by_queries_json"] or "[]")
        except Exception:
            existing = []
        if not any(e.get("query") == query for e in existing):
            existing.append({"query": query, "query_type": query_type})
        con.execute("UPDATE results SET found_by_queries_json=? WHERE run_id=? AND scene_id=? AND source_id=?",
                    (json.dumps(existing, ensure_ascii=False), run_id, scene_id, r["source_id"]))
        con.commit()
        return
    con.execute("INSERT INTO results VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, scene_id, r["source_id"], r["creator_id"], r["caption"],
                 json.dumps(r["hashtags"], ensure_ascii=False), r["likes"], r["duration"],
                 r["width"], r["height"], int(r["is_slideshow"]), r["search_position"],
                 r.get("proxy_path", ""), r.get("seg_start"), r.get("seg_end"),
                 json.dumps(r.get("tech_scores", {}), ensure_ascii=False),
                 json.dumps(r.get("vision_scores", {}), ensure_ascii=False),
                 json.dumps(r.get("reject_reasons", []), ensure_ascii=False),
                 json.dumps(fb, ensure_ascii=False)))
    con.commit()


def db_save_rating(con, run_id, scene_id, source_id, **kw):
    con.execute("INSERT OR REPLACE INTO ratings VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, scene_id, source_id, kw.get("label"), int(bool(kw.get("is_primary"))),
                 int(bool(kw.get("is_backup"))), kw.get("seg_start"), kw.get("seg_end"),
                 kw.get("comment", ""), kw.get("better_query", ""),
                 kw.get("strategy_version", "v000"), time.strftime("%Y-%m-%d %H:%M:%S")))
    con.commit()


# ---------------------------------------------------------------- playable previews

def ensure_preview(proxy_path, ffmpeg=None):
    """Return a BROWSER-PLAYABLE preview mp4 for a proxy video, creating it once (cached).

    TikTok proxies are often HEVC (bytevc1) - Chrome/Edge can't decode that, so the review UI
    showed a dead black player. Transcode once to H.264 + faststart (<=576px height, silent -
    the analysis proxies are video-only anyway); h264 sources are only remuxed with faststart."""
    src = Path(proxy_path)
    if not src.exists():
        return None
    prev = src.with_name(src.stem + ".prev.mp4")
    if prev.exists() and prev.stat().st_size > 4096:
        return prev
    if ffmpeg is None:
        ffmpeg, _ = clip_scraper._ffmpeg_tools()
    if not ffmpeg:
        return src
    import subprocess
    try:
        codec = ""
        ffprobe = clip_scraper._ffmpeg_tools()[1]
        if ffprobe:
            codec = subprocess.run(
                [ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
                 "stream=codec_name", "-of", "csv=p=0", str(src)],
                capture_output=True, text=True, timeout=20).stdout.strip().lower()
        if codec == "h264":
            cmd = [ffmpeg, "-y", "-i", str(src), "-c", "copy", "-movflags", "+faststart", str(prev)]
        else:
            cmd = [ffmpeg, "-y", "-i", str(src), "-vf", "scale=-2:'min(576,ih)'",
                   "-c:v", "libx264", "-preset", "veryfast", "-crf", "26",
                   "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-an", str(prev)]
        subprocess.run(cmd, capture_output=True, timeout=180)
    except Exception:
        return src
    return prev if prev.exists() and prev.stat().st_size > 4096 else src


# ---------------------------------------------------------------- scene planning (two paths)

def plan_from_intents(categories=None):
    """Scenes from the 53 curated Visual Intents (no LLM, $0). One scene per intent, optionally
    filtered to categories."""
    scenes, sid = [], 0
    for it in bench.INTENTS:
        if categories and it.category not in categories:
            continue
        scenes.append(TrainScene(
            scene_id=sid, script_text=f"{it.subject} {it.action} in {it.location}",
            category=it.category, visual_type=it.visual_type, subject=it.subject, action=it.action,
            location=it.location, jp_subject=it.jp_subject, jp_action=it.jp_action,
            jp_location=it.jp_location))
        sid += 1
    return scenes


def plan_from_script(script, reasoning_model=None, status_cb=None):
    """Scenes from a real voice script via ONE LLM call - returns each scene with an observable
    English intent AND japanese query building blocks (so build_queries works). Its own prompt (does
    NOT modify scrape_v2). Costs ~1 planner call."""
    import re
    ac = scrape_v2._ac()
    raw = str(script or "").replace("\r", " ").strip()
    lines = [l.strip() for l in raw.split("\n") if l.strip()]
    # Paragraph-style scripts (one/few big lines) -> split into sentences so each spoken beat
    # becomes its own scene, instead of one 160-char-truncated blob.
    if len(lines) <= 2:
        lines = [s.strip() for s in re.split(r"(?<=[.!?])\s+", raw) if len(s.strip()) > 3]
    if not lines:
        return []
    numbered = "\n".join(f"{i}: {l[:220]}" for i, l in enumerate(lines))
    sys_p = ("You plan REAL found-footage for a Japanese social short. For each narration line, give a "
             "concrete, camera-observable situation (visible subject + action + place) AND SHORT "
             "Japanese SEARCH KEYWORDS. NEVER abstract themes. Return JSON only.")
    user_p = (
        "For EACH line output a scene. Classify category as one of: school, convenience_store, "
        "vending_machine, train, daily_life, work, unusual_japan, hook_presenter.\n\n"
        "CRITICAL - the jp_* fields are TikTok SEARCH KEYWORDS, not descriptions. Real people search "
        "with 1-2 words. Keep each jp field SHORT and searchable:\n"
        "  jp_subject = 1 word (the thing/person), e.g. 漫画喫茶, ガチャガチャ, ゆるキャラ\n"
        "  jp_action  = 1-2 words (the verb or key object), e.g. 個室, 漫画を読む, カプセル, ダンス\n"
        "  jp_location = 1 word (the place), e.g. 漫画喫茶, ゲームセンター, 駅\n"
        "NEVER list multiple objects in one field. NEVER a full sentence. A good search is like "
        "'漫画喫茶 個室' or 'ガチャガチャ カプセル' - 2 tokens, not 8. Prefer the single most iconic, "
        "commonly-searched Japanese term for the thing on screen.\n\n"
        f"Lines:\n{numbered}\n\n"
        'Return STRICT JSON: {"scenes":[{"line":int,"category":"...","visual_type":"concrete|context|abstract",'
        '"subject":"english","action":"english","location":"english","jp_subject":"1 word","jp_action":"1-2 words",'
        '"jp_location":"1 word"}]}')
    data = scrape_v2._llm_json([{"role": "system", "content": sys_p}, {"role": "user", "content": user_p}],
                              max_tokens=6000, temperature=0.3, reasoning_model=reasoning_model)
    rows = data.get("scenes") if isinstance(data.get("scenes"), list) else []
    scenes = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        try:
            ln = int(r.get("line"))
        except (TypeError, ValueError):
            ln = len(scenes)
        cat = str(r.get("category", "daily_life"))
        vt = str(r.get("visual_type", "context")).lower()
        if vt not in ("concrete", "context", "abstract"):
            vt = "context"
        scenes.append(TrainScene(
            scene_id=len(scenes), script_text=lines[ln] if 0 <= ln < len(lines) else "",
            category=cat, visual_type=vt, subject=str(r.get("subject", "")), action=str(r.get("action", "")),
            location=str(r.get("location", "")), jp_subject=str(r.get("jp_subject", "")),
            jp_action=str(r.get("jp_action", "")), jp_location=str(r.get("jp_location", ""))))
    return scenes


# ---------------------------------------------------------------- autonomous scrape runner (shared)

def run_training_scrape(run_id, scenes, vision=False, cfg=None, status_cb=None, cancel_check=None,
                        reasoning_model=None, query_types=None):
    """Autonomously scrape every scene x query, download proxies, discover the best segment, and
    fill the review queue (results table). Vision OFF by default ($0). Never waits for the user."""
    cfg = cfg or bench.BenchConfig(max_downloads_per_query=6, max_raw_results_per_query=40)
    ffmpeg, ffprobe = clip_scraper._ffmpeg_tools()
    con = db_connect(run_id)
    proxies = RUNS_DIR / run_id / "proxies"
    proxies.mkdir(parents=True, exist_ok=True)

    def log(m):
        line = "[trainer] " + m
        try:
            with (RUNS_DIR / run_id / "trainer.log").open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass
        (status_cb(line) if status_cb else print(line, flush=True))

    if tiktok_login is None or not tiktok_login.is_ready():
        log("TikTok session not connected - connect TikTok, then: resume " + run_id)
        return run_id
    scene_by_id = {s.scene_id for s in scenes}
    done_scenes = {row["scene_id"] for row in con.execute(
        "SELECT DISTINCT scene_id FROM results WHERE run_id=?", (run_id,)).fetchall()}
    for scene in scenes:
        if cancel_check and cancel_check():
            log("cancelled."); break
        if scene.scene_id in done_scenes:
            continue                                   # resume: skip already-scraped scenes
        qmap = bench.build_queries(scene.to_benchintent())
        if query_types:
            qmap = {k: v for k, v in qmap.items() if k in query_types}
        db_insert_scene(con, run_id, scene, qmap)
        v_intent = scrape_v2.VisualIntent(
            scene_id=0, scene_text=scene.script_text,
            visual_type=("concrete" if scene.visual_type in ("concrete",) else "context"),
            subject=scene.subject, action=scene.action, location=scene.location)
        seen_sources = set()
        all_segs = []
        for qt, qs in qmap.items():
            for q in qs:
                if cancel_check and cancel_check():
                    break
                items = bench._real_search(q, cfg)[0]
                pos = 0
                dl = 0
                for it in items:
                    m = clip_scraper._item_meta(it)
                    sid = m.get("id") or m.get("url")
                    if not sid:
                        continue
                    pos += 1
                    if sid in seen_sources:
                        db_upsert_result(con, run_id, scene.scene_id, {"source_id": str(sid)}, q, qt)
                        continue
                    if dl >= cfg.max_downloads_per_query:
                        continue                       # keep raw record only? we record downloaded ones
                    seen_sources.add(sid)
                    proxy = proxies / f"s{scene.scene_id}_{str(sid)[:18]}.mp4"
                    got = scrape_v2.download_proxy_v2(it, proxy)
                    dl += 1
                    # NOTE: the browser-playable H.264 preview is created lazily by the review
                    # server (ensure_preview) on first play + cached - NOT eagerly here, since
                    # transcoding every full proxy up front dominated the scrape time.
                    tech, seg_s, seg_e, rejects, seg = {}, None, None, [], None
                    if got:
                        src = scrape_v2.SourceVideoCandidate(
                            platform="tiktok", source_id=str(sid), creator_id=m.get("author", ""),
                            url="", caption=m.get("caption", ""), likes=int(m.get("likes") or 0),
                            duration=float(m.get("duration") or 0), width=int(m.get("w") or 0),
                            height=int(m.get("h") or 0), query=q)
                        segs = scrape_v2.discover_segments_v2(src, got, ffmpeg, ffprobe)[:3]
                        best = None
                        for s2 in segs:
                            scrape_v2.analyze_segment_v2(s2, ffmpeg, ffprobe)
                            if best is None or s2.quality_score > best.quality_score:
                                best = s2
                        if best is not None:
                            seg_s, seg_e = best.start_time, best.end_time
                            rejects = list(best.rejection_reasons)
                            tech = {"quality": best.quality_score, "raw_footage": best.raw_footage_score,
                                    "edit_stability": best.edit_stability_score,
                                    "text_heaviness": best.text_heaviness,
                                    "caption_probability": best.caption_probability,
                                    "black_bar": best.black_bar_score, "vertical": best.vertical_quality}
                            all_segs.append(best)
                    db_upsert_result(con, run_id, scene.scene_id, {
                        "source_id": str(sid), "creator_id": m.get("author", ""),
                        "caption": (m.get("caption") or "")[:300], "hashtags": (m.get("hashtags") or [])[:12],
                        "likes": int(m.get("likes") or 0), "duration": float(m.get("duration") or 0),
                        "width": int(m.get("w") or 0), "height": int(m.get("h") or 0),
                        "is_slideshow": bool(m.get("is_image_post")), "search_position": pos,
                        "proxy_path": str(got) if got else "", "seg_start": seg_s, "seg_end": seg_e,
                        "tech_scores": tech, "reject_reasons": rejects, "vision_scores": {}}, q, qt)
        # optional vision pre-sort (NOT ground truth, never removes): describe + grade, store scores
        if vision and all_segs:
            scrape_v2.describe_segments_v2(all_segs, RUNS_DIR / run_id, ffmpeg, reasoning_model=reasoning_model)
            scores = bench._bench_match(v_intent, all_segs, reasoning_model=reasoning_model)
            for seg in all_segs:
                sc = scores.get(seg.segment_id)
                if sc:
                    con.execute("UPDATE results SET vision_scores_json=? WHERE run_id=? AND scene_id=? AND source_id=?",
                                (json.dumps(sc, ensure_ascii=False), run_id, scene.scene_id, seg.source_id))
            con.commit()
        n = con.execute("SELECT COUNT(*) c FROM results WHERE run_id=? AND scene_id=?",
                        (run_id, scene.scene_id)).fetchone()["c"]
        log(f"scene {scene.scene_id} [{scene.category}] '{scene.action}' -> {n} unique result(s) in review queue")
    con.close()
    log(f"done. Review: python -m tools.scrape_trainer serve {run_id}")
    return run_id


def start_run(mode, categories=None, script=None, vision=False, reasoning_model=None, status_cb=None):
    run_id = "train_" + time.strftime("%Y%m%d_%H%M%S")
    if mode == "script":
        scenes = plan_from_script(script or "", reasoning_model=reasoning_model, status_cb=status_cb)
    else:
        scenes = plan_from_intents(categories)
    con = db_connect(run_id)
    con.execute("INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?,?)",
                (run_id, time.strftime("%Y-%m-%d %H:%M:%S"), mode, (script or "")[:4000],
                 int(bool(vision)), "v000"))
    con.commit(); con.close()
    if not scenes:
        (status_cb or print)("[trainer] no scenes planned.")
        return run_id
    return run_training_scrape(run_id, scenes, vision=vision, reasoning_model=reasoning_model,
                               status_cb=status_cb)


def resume_run(run_id, status_cb=None):
    con = db_connect(run_id)
    r = con.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    scenes = [TrainScene(row["scene_id"], row["script_text"], row["category"], row["visual_type"],
                         row["subject"], row["action"], row["location"], row["jp_subject"],
                         row["jp_action"], row["jp_location"])
              for row in con.execute("SELECT * FROM scenes WHERE run_id=? ORDER BY scene_id", (run_id,))]
    vision = bool(r["vision_on"]) if r else False
    con.close()
    if not scenes:
        # scenes weren't persisted (crashed before scraping) - re-plan from the stored mode
        mode = r["mode"] if r else "intents"
        scenes = plan_from_script(r["script_text"]) if mode == "script" else plan_from_intents()
    return run_training_scrape(run_id, scenes, vision=vision, status_cb=status_cb)


# ---------------------------------------------------------------- learning from ratings

def analyze(run_id):
    """Per query-type AND per category: best/good/backup/wrong/usable/caption/dup rates, avg chosen
    position, results-to-first-good. Ground truth = your ratings. Writes analysis.json, returns it."""
    con = db_connect(run_id)
    results = {(row["scene_id"], row["source_id"]): row for row in
               con.execute("SELECT * FROM results WHERE run_id=?", (run_id,))}
    ratings = {(row["scene_id"], row["source_id"]): row for row in
               con.execute("SELECT * FROM ratings WHERE run_id=?", (run_id,))}
    scene_cat = {row["scene_id"]: row["category"] for row in
                 con.execute("SELECT scene_id, category FROM scenes WHERE run_id=?", (run_id,))}
    con.close()

    # attribute each RATING to every query type that found that clip
    def qtypes_for(row):
        try:
            return sorted({e.get("query_type") for e in json.loads(row["found_by_queries_json"] or "[]")})
        except Exception:
            return []

    by_qt, by_cat_qt = {}, {}
    for (sid, src), rrow in results.items():
        rat = ratings.get((sid, src))
        if not rat:
            continue                                    # only rated clips inform the strategy
        cat = scene_cat.get(sid, "?")
        for qt in qtypes_for(rrow) or ["?"]:
            for bucket, key in ((by_qt, qt), (by_cat_qt, f"{cat}|{qt}")):
                b = bucket.setdefault(key, {"rated": 0, "best": 0, "good": 0, "backup": 0, "wrong": 0,
                                            "usable": 0, "caption_heavy": 0, "duplicate": 0,
                                            "chosen_positions": []})
                b["rated"] += 1
                lab = rat["label"]
                if lab == "best_match":
                    b["best"] += 1
                if lab in ("best_match", "good_match"):
                    b["good"] += 1
                if lab == "good_backup":
                    b["backup"] += 1
                if lab in ("wrong_subject", "wrong_action", "wrong_location", "too_generic",
                           "completely_irrelevant"):
                    b["wrong"] += 1
                if lab in POSITIVE_LABELS and lab != "context_only":
                    b["usable"] += 1
                if lab == "caption_heavy":
                    b["caption_heavy"] += 1
                if lab == "duplicate":
                    b["duplicate"] += 1
                if rat["is_primary"] or lab in STRONG_LABELS:
                    b["chosen_positions"].append(int(rrow["search_position"] or 0))

    def rates(b):
        n = max(1, b["rated"])
        return {"rated": b["rated"], "best_rate": round(b["best"] / n, 3),
                "good_rate": round(b["good"] / n, 3), "backup_rate": round(b["backup"] / n, 3),
                "wrong_rate": round(b["wrong"] / n, 3), "usable_rate": round(b["usable"] / n, 3),
                "caption_rate": round(b["caption_heavy"] / n, 3),
                "duplicate_rate": round(b["duplicate"] / n, 3),
                "avg_chosen_position": round(sum(b["chosen_positions"]) / len(b["chosen_positions"]), 2)
                if b["chosen_positions"] else None}

    analysis = {"run_id": run_id, "analysis_version": TRAINER_ANALYSIS_VERSION,
                "total_ratings": len(ratings),
                "by_query_type": {k: rates(v) for k, v in by_qt.items()},
                "by_category_query_type": {k: rates(v) for k, v in by_cat_qt.items()}}
    (RUNS_DIR / run_id / "analysis.json").write_text(json.dumps(analysis, ensure_ascii=False, indent=2),
                                                     encoding="utf-8")
    return analysis


def _next_strategy_version():
    STRATEGY_DIR.mkdir(parents=True, exist_ok=True)
    existing = sorted(STRATEGY_DIR.glob("query_strategy_v*.json"))
    n = 0
    for p in existing:
        try:
            n = max(n, int(p.stem.split("_v")[-1]))
        except ValueError:
            pass
    return n + 1


def propose_strategy(run_id):
    """Turn your ratings into a versioned strategy config: per category, preferred query types +
    weights derived from good_rate (down-weight wrong-heavy types). Does NOT activate it."""
    a = analyze(run_id)
    per_cat = {}
    for key, r in a["by_category_query_type"].items():
        cat, qt = key.split("|", 1)
        per_cat.setdefault(cat, {})[qt] = r
    strategy = {}
    for cat, qts in per_cat.items():
        ranked = sorted(qts.items(), key=lambda kv: (kv[1]["good_rate"], -kv[1]["wrong_rate"]), reverse=True)
        weights = {}
        for qt, r in qts.items():
            # weight rises with good_rate, falls with wrong_rate; clamped to a sane range
            weights[qt] = round(max(0.2, min(1.6, 0.6 + r["good_rate"] * 1.3 - r["wrong_rate"] * 0.8)), 2)
        strategy[cat] = {
            "preferred_query_types": [qt for qt, _ in ranked[:3] if qts[qt]["good_rate"] > 0],
            "avoid_query_types": [qt for qt, r in ranked if r["wrong_rate"] >= 0.6],
            "query_type_weights": weights,
            "n_ratings": sum(r["rated"] for r in qts.values())}
    ver = f"v{_next_strategy_version():03d}"
    out = {"version": ver, "source_run": run_id, "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
           "categories": strategy,
           "note": "Derived from manual ratings (ground truth). NOT active until 'activate'. Compare "
                   "on a holdout before activating."}
    STRATEGY_DIR.mkdir(parents=True, exist_ok=True)
    (STRATEGY_DIR / f"query_strategy_{ver}.json").write_text(json.dumps(out, ensure_ascii=False, indent=2),
                                                             encoding="utf-8")
    return out


def _load_strategy(ver):
    p = STRATEGY_DIR / f"query_strategy_{ver}.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def _score_strategy_on(rows_by_scene, strategy):
    """Rank each scene's rated clips by the strategy's query-type weights; score by MRR of the first
    strong-positive clip + Precision@5. rows = [(query_types, label, position)] per scene."""
    mrr, p5, scenes = 0.0, 0.0, 0
    for scene, rows in rows_by_scene.items():
        cat = scene[1]
        weights = (strategy.get("categories", {}).get(cat, {}) or {}).get("query_type_weights", {})
        ranked = sorted(rows, key=lambda r: max([weights.get(qt, 0.5) for qt in r[0]] or [0.5]), reverse=True)
        scenes += 1
        rr = 0.0
        for i, (qts, label, pos) in enumerate(ranked, 1):
            if label in STRONG_LABELS:
                rr = 1.0 / i
                break
        mrr += rr
        top5 = ranked[:5]
        p5 += sum(1 for (_q, lab, _p) in top5 if lab in POSITIVE_LABELS) / max(1, len(top5))
    return {"scenes": scenes, "mrr": round(mrr / max(1, scenes), 3), "precision_at_5": round(p5 / max(1, scenes), 3)}


def compare(run_id, ver_a, ver_b, holdout_frac=0.4):
    """Compare two strategies on a HELD-OUT slice of the rated data. Recommend ver_b only if it beats
    ver_a on the holdout (MRR + precision@5)."""
    a, b = _load_strategy(ver_a), _load_strategy(ver_b)
    if not a or not b:
        return {"error": "missing strategy version(s)"}
    con = db_connect(run_id)
    results = {(row["scene_id"], row["source_id"]): row for row in
               con.execute("SELECT * FROM results WHERE run_id=?", (run_id,))}
    ratings = {(row["scene_id"], row["source_id"]): row for row in
               con.execute("SELECT * FROM ratings WHERE run_id=?", (run_id,))}
    scene_cat = {row["scene_id"]: row["category"] for row in
                 con.execute("SELECT scene_id, category FROM scenes WHERE run_id=?", (run_id,))}
    con.close()
    rows_by_scene = {}
    for (sid, src), rrow in results.items():
        rat = ratings.get((sid, src))
        if not rat:
            continue
        try:
            qts = [e.get("query_type") for e in json.loads(rrow["found_by_queries_json"] or "[]")]
        except Exception:
            qts = []
        rows_by_scene.setdefault((sid, scene_cat.get(sid, "?")), []).append(
            (qts, rat["label"], int(rrow["search_position"] or 0)))
    scenes = sorted(rows_by_scene.keys())
    cut = int(len(scenes) * (1 - holdout_frac))
    holdout = {s: rows_by_scene[s] for s in scenes[cut:]} or rows_by_scene
    sa, sb = _score_strategy_on(holdout, a), _score_strategy_on(holdout, b)
    better = (sb["mrr"] > sa["mrr"]) or (sb["mrr"] == sa["mrr"] and sb["precision_at_5"] > sa["precision_at_5"])
    return {"holdout_scenes": len(holdout), ver_a: sa, ver_b: sb,
            "recommend": ver_b if better else ver_a,
            "note": f"{ver_b} recommended only if it beats {ver_a} on the holdout."}


def activate(ver):
    """Mark a strategy version active (production V2 can later read this). Requires explicit call -
    never automatic."""
    if not _load_strategy(ver):
        return {"ok": False, "error": f"strategy {ver} not found"}
    STRATEGY_DIR.mkdir(parents=True, exist_ok=True)
    (STRATEGY_DIR / "active.json").write_text(json.dumps(
        {"active_version": ver, "activated_at": time.strftime("%Y-%m-%d %H:%M:%S")}, indent=2), encoding="utf-8")
    return {"ok": True, "active_version": ver}


# ---------------------------------------------------------------- CLI

def _main(argv=None):
    ap = argparse.ArgumentParser(description="Human-in-the-loop Scrape Trainer for Scrape V2.")
    sub = ap.add_subparsers(dest="cmd")
    ps = sub.add_parser("start"); ps.add_argument("--intents", nargs="*")
    ps.add_argument("--script"); ps.add_argument("--vision", action="store_true")
    sub.add_parser("resume").add_argument("run_id")
    pv = sub.add_parser("serve"); pv.add_argument("run_id"); pv.add_argument("--port", type=int, default=7870)
    sub.add_parser("analyze").add_argument("run_id")
    sub.add_parser("propose-strategy").add_argument("run_id")
    pc = sub.add_parser("compare"); pc.add_argument("run_id"); pc.add_argument("ver_a"); pc.add_argument("ver_b")
    sub.add_parser("activate").add_argument("ver")
    args = ap.parse_args(argv)

    if args.cmd == "start":
        if args.script:
            script = Path(args.script).read_text(encoding="utf-8") if Path(args.script).exists() else args.script
            rid = start_run("script", script=script, vision=args.vision)
        else:
            rid = start_run("intents", categories=(args.intents or None), vision=args.vision)
        print("run:", rid)
    elif args.cmd == "resume":
        print("run:", resume_run(args.run_id))
    elif args.cmd == "serve":
        from tools.scrape_trainer_serve import serve
        serve(args.run_id, port=args.port)
    elif args.cmd == "analyze":
        print(json.dumps(analyze(args.run_id), ensure_ascii=False, indent=2))
    elif args.cmd == "propose-strategy":
        print(json.dumps(propose_strategy(args.run_id), ensure_ascii=False, indent=2))
    elif args.cmd == "compare":
        print(json.dumps(compare(args.run_id, args.ver_a, args.ver_b), ensure_ascii=False, indent=2))
    elif args.cmd == "activate":
        print(json.dumps(activate(args.ver), ensure_ascii=False, indent=2))
    else:
        ap.print_help()


if __name__ == "__main__":
    _main()
