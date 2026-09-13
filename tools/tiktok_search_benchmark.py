"""TikTok Search Lab - a REAL, systematic benchmark of TikTok search query structures for our
Japan-shorts niche. It measures which query STRUCTURES yield the most relevant + technically
usable clips - not the most results.

It is fully isolated from production: it imports and REUSES the real logged-in TikTok session
(tiktok_login), the real metadata parser (clip_scraper), and the real Scrape-V2 download / segment
discovery / quality / vision primitives (scrape_v2). It NEVER changes V1 or V2 production logic.
No mocks, no fabricated numbers - every metric comes from a real search + real download + real
segment analysis + real vision judgment.

Pipeline per query (controlled, equal conditions for every query type):
  1. real TikTok search via the logged-in session (search order preserved, NO like-gate)
  2. dedupe by id/url, log every raw result + metadata
  3. download the top-N sources as low-res proxies (scrape_v2.download_proxy_v2)
  4. discover usable SEGMENTS across the whole video (scrape_v2.discover_segments_v2)
  5. segment-level quality (scrape_v2.analyze_segment_v2) - vertical / caption / stability / black-bar
  6. Vision: describe segments + match to the ORIGINAL visual intent (scrape_v2 two-stage)
  7. compute Relevant@10, Usable@10, Final Yield, caption rate, raw-footage rate, diversity, ...

Resumable: every fully-analysed query is cached; a re-run skips it.
Reports: JSON + CSV + HTML under benchmark_runs/<run_id>/.

CLI:
    python -m tools.tiktok_search_benchmark --pilot       # small real slice (fast, cheap)
    python -m tools.tiktok_search_benchmark --full        # every intent x every query type
    python -m tools.tiktok_search_benchmark --intent school_cleaning_01
    python -m tools.tiktok_search_benchmark --resume <run_id>
    python -m tools.tiktok_search_benchmark --report <run_id>   # rebuild reports from cache only
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
import statistics
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path

# repo root on path so `import clip_scraper` works when run as `python -m tools.tiktok_search_benchmark`
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Windows consoles default to cp1252 and CRASH on printing Japanese query text. Force utf-8.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import clip_scraper                       # real metadata parser + backend
import scrape_v2                          # real download / segment / quality / vision primitives

try:
    import tiktok_login
except Exception:                         # pragma: no cover
    tiktok_login = None

BENCH_DIR = ROOT / "benchmark_runs"
BENCH_ANALYSIS_VERSION = 1


# --------------------------------------------------------------- benchmark config

@dataclass
class BenchConfig:
    max_scrolls_per_query: int = 8
    max_raw_results_per_query: int = 50
    max_downloads_per_query: int = 12
    max_segments_per_source: int = 3
    vision_batch_size: int = 6
    search_timeout_s: float = 60.0
    top_k: int = 10                       # Relevant@K / Usable@K window
    # match thresholds for "relevant" (graded overall match) vs "usable" (a quality-passing segment)
    relevant_overall_floor: float = 6.0     # B_MATCH+ = clearly topically relevant
    usable_quality_floor: float = 5.5


PILOT_CONFIG = BenchConfig(max_downloads_per_query=5, max_raw_results_per_query=30)


# --------------------------------------------------------------- visual intent dataset (>=30)

@dataclass
class BenchIntent:
    intent_id: str
    category: str
    visual_type: str                      # concrete | context | proof | hook
    subject: str                          # English (for the report + vision intent)
    action: str
    location: str
    jp_subject: str = ""                  # Japanese building blocks for controlled queries
    jp_action: str = ""
    jp_location: str = ""
    must_show: list = field(default_factory=list)
    avoid: list = field(default_factory=list)


def _mk(iid, cat, vt, subj, act, loc, js, ja, jl, must, avoid):
    return BenchIntent(iid, cat, vt, subj, act, loc, js, ja, jl, list(must), list(avoid))


_AVOID_BROLL = ["talking head only", "anime", "news", "quiz", "large subtitles", "photo slideshow"]
_AVOID_HOOK = ["anime", "vtuber", "cgi", "slideshow", "livestream", "heavy text over face", "no clear subject"]

INTENTS = [
    # --- school ---
    _mk("school_cleaning_01", "school", "concrete", "Japanese students", "cleaning classroom",
        "Japanese school classroom", "学生", "教室 掃除", "学校", ["students", "cleaning action", "classroom"], _AVOID_BROLL),
    _mk("school_lunch_02", "school", "concrete", "students", "eating school lunch", "classroom",
        "学生", "給食", "教室", ["students", "school lunch", "classroom"], _AVOID_BROLL),
    _mk("school_shoes_03", "school", "concrete", "students", "changing shoes at entrance", "school entrance",
        "学生", "上履き 履き替え", "昇降口", ["students", "changing shoes", "entrance"], _AVOID_BROLL),
    _mk("school_classroom_04", "school", "context", "japanese classroom", "empty classroom", "japanese school",
        "", "教室", "日本の学校", ["classroom interior"], _AVOID_BROLL),
    _mk("school_uniform_05", "school", "context", "students", "walking to school in uniform", "street",
        "高校生", "登校", "制服", ["uniform", "walking to school"], _AVOID_BROLL),
    _mk("school_afterclass_06", "school", "context", "students", "after-school activities", "school",
        "高校生", "放課後", "学校", ["students", "after school"], _AVOID_BROLL),
    _mk("school_empty_07", "school", "context", "school building", "empty school building", "japan",
        "", "誰もいない 学校", "校舎", ["empty school"], _AVOID_BROLL),
    _mk("school_teacher_08", "school", "concrete", "teacher", "teacher in classroom", "classroom",
        "先生", "授業", "教室", ["teacher", "classroom"], _AVOID_BROLL),
    # --- convenience store ---
    _mk("konbini_buy_01", "convenience_store", "concrete", "person", "buying food at konbini", "convenience store",
        "", "コンビニ 買い物", "コンビニ", ["person", "buying", "konbini"], _AVOID_BROLL),
    _mk("konbini_shelf_02", "convenience_store", "context", "konbini shelf", "store shelf", "convenience store",
        "", "コンビニ 商品棚", "店内", ["shelf", "products"], _AVOID_BROLL),
    _mk("konbini_restock_03", "convenience_store", "concrete", "clerk", "restocking shelves", "convenience store",
        "店員", "品出し", "コンビニ", ["clerk", "restocking"], _AVOID_BROLL),
    _mk("konbini_checkout_04", "convenience_store", "concrete", "person", "self checkout", "convenience store",
        "", "セルフレジ", "コンビニ", ["self checkout"], _AVOID_BROLL),
    _mk("konbini_hotsnack_05", "convenience_store", "concrete", "hot snacks", "warm snacks at register", "convenience store",
        "", "ホットスナック", "レジ", ["hot food", "register"], _AVOID_BROLL),
    _mk("konbini_night_06", "convenience_store", "context", "convenience store", "night convenience store", "japan",
        "", "深夜 コンビニ", "夜", ["konbini", "night"], _AVOID_BROLL),
    _mk("konbini_unusual_07", "convenience_store", "proof", "product", "unusual konbini product", "convenience store",
        "", "変わった コンビニ 商品", "コンビニ", ["unusual product"], _AVOID_BROLL),
    # --- vending machines ---
    _mk("vending_use_01", "vending_machine", "concrete", "person", "using a japanese vending machine", "street",
        "", "自動販売機 使う", "日本", ["person", "vending machine"], _AVOID_BROLL),
    _mk("vending_unusual_02", "vending_machine", "proof", "vending machine", "unusual vending machine", "japan",
        "", "変わった 自動販売機", "日本", ["unusual vending machine"], _AVOID_BROLL),
    _mk("vending_many_03", "vending_machine", "context", "vending machines", "many machines in a row", "street",
        "", "自動販売機 たくさん", "並ぶ", ["many vending machines"], _AVOID_BROLL),
    _mk("vending_hot_04", "vending_machine", "concrete", "hot drinks", "hot drinks from machine", "vending machine",
        "", "自販機 あったかい 飲み物", "自動販売機", ["hot drink", "vending machine"], _AVOID_BROLL),
    _mk("vending_food_05", "vending_machine", "proof", "food vending machine", "food vending machine", "japan",
        "", "食品 自動販売機", "日本", ["food vending machine"], _AVOID_BROLL),
    _mk("vending_night_06", "vending_machine", "context", "vending machine", "vending machine at night", "japan",
        "", "自動販売機 夜", "夜", ["vending machine", "night"], _AVOID_BROLL),
    _mk("vending_rural_07", "vending_machine", "context", "vending machine", "vending machine in the countryside", "rural japan",
        "", "田舎 自動販売機", "田舎", ["vending machine", "rural"], _AVOID_BROLL),
    # --- trains / commuting ---
    _mk("train_sleep_01", "train", "concrete", "salaryman", "sleeping on the train", "commuter train",
        "サラリーマン", "電車 寝る", "通勤電車", ["person sleeping", "train"], _AVOID_BROLL),
    _mk("train_crowded_02", "train", "context", "commuters", "crowded commuter train", "tokyo",
        "", "満員電車", "通勤", ["crowded train"], _AVOID_BROLL),
    _mk("train_empty_03", "train", "context", "empty train", "empty train late at night", "japan",
        "", "終電 ガラガラ", "夜", ["empty train", "night"], _AVOID_BROLL),
    _mk("train_wait_04", "train", "context", "person", "waiting for a train", "platform",
        "", "電車 待つ", "駅 ホーム", ["waiting", "platform"], _AVOID_BROLL),
    _mk("train_platform_05", "train", "context", "platform", "tokyo train platform", "tokyo",
        "", "駅 ホーム", "東京", ["platform", "tokyo"], _AVOID_BROLL),
    _mk("train_lasttrain_06", "train", "context", "person", "last train", "station",
        "", "終電", "駅", ["last train"], _AVOID_BROLL),
    _mk("train_shinkansen_07", "train", "concrete", "shinkansen interior", "shinkansen interior", "japan",
        "", "新幹線 車内", "新幹線", ["shinkansen interior"], _AVOID_BROLL),
    # --- daily life ---
    _mk("daily_ramen_01", "daily_life", "concrete", "person", "eating ramen alone", "ramen shop",
        "", "一人 ラーメン", "ラーメン屋", ["person eating", "ramen"], _AVOID_BROLL),
    _mk("daily_apartment_02", "daily_life", "context", "small apartment", "small japanese apartment", "japan",
        "", "狭い 部屋", "一人暮らし", ["small apartment interior"], _AVOID_BROLL),
    _mk("daily_home_03", "daily_life", "context", "person", "coming home after work", "apartment",
        "", "仕事帰り 帰宅", "家", ["coming home"], _AVOID_BROLL),
    _mk("daily_supermarket_04", "daily_life", "concrete", "person", "shopping at japanese supermarket", "supermarket",
        "", "スーパー 買い物", "日本", ["supermarket shopping"], _AVOID_BROLL),
    _mk("daily_morning_05", "daily_life", "context", "person", "japanese morning routine", "apartment",
        "", "朝 ルーティン", "日本", ["morning routine"], _AVOID_BROLL),
    _mk("daily_walk_06", "daily_life", "context", "person", "walking through tokyo at night", "tokyo",
        "", "東京 夜 一人歩き", "夜", ["walking", "tokyo night"], _AVOID_BROLL),
    _mk("daily_street_07", "daily_life", "context", "residential street", "japanese residential street", "japan",
        "", "住宅街", "日本", ["residential street"], _AVOID_BROLL),
    _mk("daily_cook_08", "daily_life", "concrete", "person", "cooking in small apartment", "kitchen",
        "", "自炊 一人暮らし", "狭い キッチン", ["cooking", "small kitchen"], _AVOID_BROLL),
    # --- work culture ---
    _mk("work_leave_01", "work", "context", "salaryman", "leaving office at night", "office building",
        "サラリーマン", "残業帰り", "オフィス", ["leaving office", "night"], _AVOID_BROLL),
    _mk("work_desksleep_02", "work", "concrete", "office worker", "sleeping at desk", "office",
        "会社員", "デスク 居眠り", "オフィス", ["sleeping at desk"], _AVOID_BROLL),
    _mk("work_office_03", "work", "context", "office", "crowded japanese office", "office",
        "", "日本 オフィス", "会社", ["office interior"], _AVOID_BROLL),
    _mk("work_nightoffice_04", "work", "context", "office", "office lit at night", "japan",
        "", "夜 オフィス 明かり", "夜", ["office at night"], _AVOID_BROLL),
    _mk("work_commuter_05", "work", "context", "commuters", "commuters after work", "street",
        "", "仕事帰り 通勤", "街", ["commuters", "after work"], _AVOID_BROLL),
    _mk("work_lunch_06", "work", "concrete", "office workers", "office workers at lunch", "restaurant",
        "会社員", "ランチ", "オフィス街", ["office workers", "lunch"], _AVOID_BROLL),
    # --- unusual japan / proof ---
    _mk("unusual_product_01", "unusual_japan", "proof", "product", "unusual japanese product", "shop",
        "", "変わった 日本 商品", "店", ["unusual product"], _AVOID_BROLL),
    _mk("unusual_robot_02", "unusual_japan", "proof", "robot restaurant", "robot restaurant", "japan",
        "", "ロボット レストラン", "日本", ["robot restaurant"], _AVOID_BROLL),
    _mk("unusual_futurestore_03", "unusual_japan", "proof", "futuristic shop", "futuristic store", "japan",
        "", "未来的 店", "日本", ["futuristic store"], _AVOID_BROLL),
    _mk("unusual_rule_04", "unusual_japan", "proof", "sign", "curious japanese rule", "japan",
        "", "変わった 日本 ルール", "日本", ["rule sign"], _AVOID_BROLL),
    _mk("unusual_smallshop_05", "unusual_japan", "proof", "tiny shop", "extremely small shop", "japan",
        "", "極小 店", "日本", ["tiny shop"], _AVOID_BROLL),
    # --- hook presenters (tested separately) ---
    _mk("hook_surprise_01", "hook_presenter", "hook", "japanese woman", "reacting surprised", "convenience store",
        "日本人女子", "リアクション 驚く", "コンビニ", ["woman", "clear reaction", "japan"], _AVOID_HOOK),
    _mk("hook_pointproduct_02", "hook_presenter", "hook", "japanese woman", "pointing at a product", "shop",
        "日本人女性", "商品 紹介", "店", ["woman", "pointing at product"], _AVOID_HOOK),
    _mk("hook_konbini_03", "hook_presenter", "hook", "presenter", "presenter in convenience store", "convenience store",
        "日本人女子", "コンビニ 紹介", "コンビニ", ["presenter", "konbini"], _AVOID_HOOK),
    _mk("hook_vending_04", "hook_presenter", "hook", "presenter", "presenter at vending machine", "street",
        "日本人女子", "自動販売機 紹介", "街頭", ["presenter", "vending machine"], _AVOID_HOOK),
    _mk("hook_street_05", "hook_presenter", "hook", "presenter", "presenter on a japanese street", "street",
        "日本人女子", "街頭", "日本の街", ["presenter", "street"], _AVOID_HOOK),
]


# --------------------------------------------------------------- query taxonomy (12 types)

# Native TikTok format words + colloquial + hashtags (fixed control vocab from the spec).
FORMAT_WORDS = ["密着", "ルーティン", "日常", "vlog", "POV", "一日", "仕事帰り", "学校生活", "購入品", "店内", "使ってみた", "行ってみた"]
CATEGORY_COLLOQUIAL = {
    "school": ["放課後", "朝の通学", "学校生活"],
    "convenience_store": ["コンビニ飯", "コンビニ 購入品"],
    "vending_machine": ["自販機", "自販機 巡り"],
    "train": ["終電帰り", "満員電車", "通勤"],
    "daily_life": ["一人飯", "ぼっち飯", "一人暮らし"],
    "work": ["残業帰り", "夜勤明け", "社畜"],
    "unusual_japan": ["日本でしか見ないもの", "変わった"],
    "hook_presenter": ["リアクション", "紹介"],
}
CATEGORY_HASHTAGS = {
    "school": ["#学校生活", "#高校生", "#放課後"],
    "convenience_store": ["#コンビニ", "#コンビニ飯"],
    "vending_machine": ["#自動販売機", "#自販機"],
    "train": ["#通勤", "#満員電車", "#終電"],
    "daily_life": ["#一人暮らし", "#東京の日常", "#一人飯"],
    "work": ["#サラリーマン", "#社畜", "#仕事帰り"],
    "unusual_japan": ["#日本の変わったもの", "#面白い"],
    "hook_presenter": ["#日本人女子", "#紹介"],
}
CATEGORY_QUESTION = {
    "school": "日本の学校ってどんな感じ",
    "convenience_store": "日本のコンビニ 面白い",
    "vending_machine": "変わった自動販売機",
    "train": "日本の満員電車 やばい",
    "daily_life": "日本でしか見ないもの",
    "work": "日本の働き方 やばい",
    "unusual_japan": "日本でしか見ないもの",
    "hook_presenter": "日本のコンビニ 面白い",
}
CATEGORY_BROAD = {
    "school": "日本の学校", "convenience_store": "日本のコンビニ", "vending_machine": "自動販売機",
    "train": "東京生活", "daily_life": "日本の日常", "work": "日本の仕事",
    "unusual_japan": "日本の文化", "hook_presenter": "日本の日常",
}

QUERY_TYPES = ["simple_noun", "subject_action", "action_location", "subject_action_location",
               "native_vlog", "format_word", "question", "colloquial", "hashtag",
               "broad_topic", "english", "presenter_style"]


def _nz(*parts):
    return " ".join(p for p in parts if p and str(p).strip()).strip()


def _cap(x, n):
    """Keep only the first n whitespace tokens - real TikTok search uses 1-3 keywords, so a
    verbose intent block ('ブース 選ぶ パソコン 椅子 マット 入る') is trimmed to its head."""
    return " ".join(str(x or "").split()[:n]).strip()


def build_queries(intent: BenchIntent, max_per_type=3):
    """Deterministic, CONTROLLED queries per type from the intent's JP building blocks + fixed
    control vocab. Returns {query_type: [queries]}. Diversified so a type is not a synonym of itself."""
    # Cap each block so concatenated queries stay short & searchable (TikTok wants 1-3 keywords).
    js, ja, jl = _cap(intent.jp_subject, 2), _cap(intent.jp_action, 2), _cap(intent.jp_location, 2)
    js1, ja1, jl1 = _cap(intent.jp_subject, 1), _cap(intent.jp_action, 1), _cap(intent.jp_location, 1)
    cat = intent.category
    out = {t: [] for t in QUERY_TYPES}
    out["simple_noun"] = [x for x in [jl1, ja1, CATEGORY_BROAD.get(cat)] if x]
    out["subject_action"] = [_nz(js1, ja1)] if js else [_nz(ja)]
    out["action_location"] = [_nz(ja1, jl1), _nz(ja1, "日本")]
    out["subject_action_location"] = [_nz(js1, ja1, jl1)] if js else [_nz(ja1, jl1)]
    out["native_vlog"] = [_nz(jl1, "vlog"), _nz(ja1, "vlog"), _nz(jl1, "日常 vlog")]
    out["format_word"] = [_nz(jl1 or ja1, "密着"), _nz(jl1 or ja1, "ルーティン"), _nz(ja1, "使ってみた")]
    out["question"] = [CATEGORY_QUESTION.get(cat, "")]
    out["colloquial"] = list(CATEGORY_COLLOQUIAL.get(cat, []))
    out["hashtag"] = list(CATEGORY_HASHTAGS.get(cat, []))
    out["broad_topic"] = [CATEGORY_BROAD.get(cat, "")]
    out["english"] = [_nz(intent.subject, intent.action, "japan"), _nz(intent.action, intent.location)]
    if cat == "hook_presenter":
        out["presenter_style"] = [_nz(js1, ja1), _nz(js1, "街頭"), _nz(ja1, "女性")]
    # clean + dedupe within type
    for t in out:
        seen, kept = set(), []
        for q in out[t]:
            q = (q or "").strip()
            if q and q.lower() not in seen:
                seen.add(q.lower())
                kept.append(q)
        out[t] = kept[:max_per_type]
    return out


def normalize_query(q):
    return " ".join(str(q or "").split()).strip().lower()


# --------------------------------------------------------------- real search + analysis

def _real_search(query, cfg: BenchConfig, status_cb=None):
    """One REAL TikTok search via the logged-in session. Preserves TikTok's search ORDER (sort !=
    MOST_LIKED) and applies NO like gate. Returns (raw_items, elapsed_s, login_wall)."""
    if tiktok_login is None or not tiktok_login.is_ready():
        return [], 0.0, True
    t0 = time.monotonic()
    before = tiktok_login.search_stats().get("login_wall", 0)
    items = tiktok_login.search_sync(query, want=cfg.max_raw_results_per_query, status_cb=status_cb,
                                     sort="RELEVANCE", timeout_s=cfg.search_timeout_s) or []
    after = tiktok_login.search_stats().get("login_wall", 0)
    return items, round(time.monotonic() - t0, 2), (after > before)


def _bench_match(intent, segments, reasoning_model=None):
    """Benchmark relevance scoring - GRADED, not the strict V2 production floors (which reject almost
    everything and make every query type look 0% relevant). Scores EVERY described segment 0-10 and
    keeps the score. Returns {segment_id: {overall_match, subject/action/location/script_match}}."""
    described = [s for s in segments if s.visual_description]
    if not described:
        return {}
    idx_to_seg, lines = {}, []
    for i, s in enumerate(described):
        d = s.visual_description
        idx_to_seg[i] = s
        lines.append('seg %d: subjects=%s, action="%s", location="%s", motion=%s'
                     % (i, d.get("subjects"), d.get("action"), d.get("location"), d.get("motion")))
    prompt = (
        "Grade how well each SEGMENT matches the target visual for a found-footage clip. Be GRADED, "
        "not binary: off-topic = 0-3; shows the subject or place but not the exact action = 4-6; "
        "clearly shows the target subject + action + place = 8-10.\n"
        'TARGET: subject="%s", action="%s", location="%s"\n\n' % (intent.subject, intent.action, intent.location)
        + "SEGMENTS:\n" + "\n".join(lines) + "\n\n"
        'Return STRICT JSON: {"seg": {"0": {"subject_match":0-10,"action_match":0-10,'
        '"location_match":0-10,"mood_match":0-10,"script_match":0-10}, "1": {...}}}')
    data = scrape_v2._llm_json([{"role": "user", "content": prompt}], max_tokens=3000,
                              temperature=0.1, reasoning_model=reasoning_model)
    smap = data.get("seg") if isinstance(data.get("seg"), dict) else {}
    if not smap:                              # one retry against a transient empty response
        time.sleep(1.5)
        data = scrape_v2._llm_json([{"role": "user", "content": prompt}], max_tokens=3000,
                                  temperature=0.1, reasoning_model=reasoning_model)
        smap = data.get("seg") if isinstance(data.get("seg"), dict) else {}
    out = {}
    for k, sc in smap.items():
        try:
            seg = idx_to_seg[int(k)]
        except (KeyError, ValueError, TypeError):
            continue
        if not isinstance(sc, dict):
            continue
        def g(name):
            try:
                return max(0.0, min(10.0, float(sc.get(name, 0))))
            except (TypeError, ValueError):
                return 0.0
        subj, act, loc = g("subject_match"), g("action_match"), g("location_match")
        overall = scrape_v2.semantic_match_score(subj, act, loc, g("mood_match"), g("script_match"))
        out[seg.segment_id] = {"overall_match": round(overall, 2), "subject_match": subj,
                               "action_match": act, "location_match": loc, "script_match": g("script_match")}
    return out


def _bench_match_class(overall):
    return ("A_MATCH" if overall >= 7.5 else "B_MATCH" if overall >= 6.0 else
            "C_CONTEXT" if overall >= 4.5 else "D_REJECTED")


def _analyze_query(intent: BenchIntent, query, query_type, cfg: BenchConfig, work_dir: Path,
                   ffmpeg, ffprobe, reasoning_model=None, status_cb=None):
    """Full REAL analysis of one query. Returns a dict with raw results, per-source segment analysis,
    vision matches and computed metrics. No fabricated values."""
    items, elapsed, login_wall = _real_search(query, cfg, status_cb=status_cb)
    # dedupe by id/url, preserve search order, record raw metadata
    seen_ids, raw = set(), []
    for pos, it in enumerate(items):
        m = clip_scraper._item_meta(it)
        sid = m.get("id") or m.get("url")
        if not sid or sid in seen_ids:
            continue
        seen_ids.add(sid)
        raw.append({
            "result_position": len(raw) + 1, "platform_id": str(sid), "creator_id": m.get("author", ""),
            "caption": (m.get("caption") or "")[:200], "hashtags": (m.get("hashtags") or [])[:10],
            "likes": int(m.get("likes") or 0), "duration": float(m.get("duration") or 0.0),
            "width": int(m.get("w") or 0), "height": int(m.get("h") or 0),
            "is_slideshow": bool(m.get("is_image_post")), "_item": it, "_meta": m,
        })
    duplicate_rate = round(1.0 - (len(raw) / max(1, len(items))), 3) if items else 0.0

    # download top-N proxies (search order), discover + quality-score segments
    v_intent = scrape_v2.VisualIntent(
        scene_id=0, scene_text=f"{intent.subject} {intent.action} {intent.location}",
        visual_type=("concrete" if intent.visual_type in ("concrete", "proof") else
                     "context" if intent.visual_type == "context" else "concrete"),
        subject=intent.subject, action=intent.action, location=intent.location,
        avoid_elements=intent.avoid)
    all_segs, per_source, dl_ok, dl_try = [], [], 0, 0
    for r in raw[:cfg.max_downloads_per_query]:
        dl_try += 1
        proxy = work_dir / f"src_{r['platform_id'][:20]}.mp4"
        got = scrape_v2.download_proxy_v2(r["_item"], proxy)
        r["download_success"] = bool(got)
        if not got:
            continue
        dl_ok += 1
        src = scrape_v2.SourceVideoCandidate(
            platform="tiktok", source_id=r["platform_id"], creator_id=r["creator_id"], url="",
            caption=r["caption"], hashtags=r["hashtags"], likes=r["likes"], duration=r["duration"],
            width=r["width"], height=r["height"], query=query)
        segs = scrape_v2.discover_segments_v2(src, got, ffmpeg, ffprobe)[:cfg.max_segments_per_source]
        best = None
        for seg in segs:
            scrape_v2.analyze_segment_v2(seg, ffmpeg, ffprobe)
            all_segs.append(seg)
            if best is None or seg.quality_score > best.quality_score:
                best = seg
        r["segments"] = len(segs)
        r["best_quality"] = round(best.quality_score, 2) if best else 0.0
        r["has_usable_segment"] = bool(best and not best.rejection_reasons
                                       and best.quality_score >= cfg.usable_quality_floor)
        r["_best_seg"] = best
        per_source.append(r)

    # Vision: describe every discovered segment, then GRADE-match it to THIS intent (unfiltered
    # scores - not V2's strict production floors, so query types stay distinguishable).
    if all_segs:
        scrape_v2.describe_segments_v2(all_segs, work_dir, ffmpeg, reasoning_model=reasoning_model)
        scores = _bench_match(v_intent, all_segs, reasoning_model=reasoning_model)
        for r in per_source:
            seg = r.get("_best_seg")
            sc = scores.get(seg.segment_id) if seg else None
            if sc:
                r["overall_match"] = sc["overall_match"]
                r["action_match"] = sc["action_match"]
                r["subject_match"] = sc["subject_match"]
                r["location_match"] = sc["location_match"]
                r["match_class"] = _bench_match_class(sc["overall_match"])
            else:
                r["overall_match"] = 0.0
                r["match_class"] = "D_REJECTED"

    metrics = _compute_query_metrics(raw, per_source, elapsed, cfg)
    metrics.update({"login_wall": login_wall, "duplicate_rate": duplicate_rate,
                    "raw_results": len(items), "unique_results": len(raw)})
    # strip non-serializable handles before returning
    for r in raw:
        r.pop("_item", None); r.pop("_meta", None); r.pop("_best_seg", None)
    return {"intent_id": intent.intent_id, "category": intent.category, "query": query,
            "query_type": query_type, "query_language": ("en" if query_type == "english" else "ja"),
            "elapsed_s": elapsed, "raw": raw, "metrics": metrics}


def _compute_query_metrics(raw, per_source, elapsed, cfg: BenchConfig):
    n_raw = len(raw)
    top = raw[:cfg.top_k]
    analysed = {r["platform_id"]: r for r in per_source}
    def is_relevant(r):
        a = analysed.get(r["platform_id"])
        return bool(a and float(a.get("overall_match", 0)) >= cfg.relevant_overall_floor)
    def is_usable(r):
        a = analysed.get(r["platform_id"])
        return bool(a and a.get("has_usable_segment"))
    def is_exact(r):
        a = analysed.get(r["platform_id"])
        return bool(a and float(a.get("action_match", 0)) >= 7 and float(a.get("subject_match", 0)) >= 6
                    and float(a.get("location_match", 0)) >= 6 and str(a.get("match_class")) == "A_MATCH")
    relevant_at_10 = round(sum(1 for r in top if is_relevant(r)) / max(1, len(top)), 3)
    usable_at_10 = round(sum(1 for r in top if is_usable(r)) / max(1, len(top)), 3)
    usable_segments = sum(1 for r in per_source if r.get("has_usable_segment"))
    relevant_usable = sum(1 for r in per_source if r.get("has_usable_segment")
                          and float(r.get("overall_match", 0)) >= cfg.relevant_overall_floor)
    final_yield = round(relevant_usable / max(1, n_raw), 3)
    dl_try = sum(1 for r in raw[:cfg.max_downloads_per_query])
    dl_ok = sum(1 for r in per_source)
    verticals = sum(1 for r in raw if r["height"] and r["width"] and r["height"] >= r["width"] * 1.2)
    caption_free = sum(1 for r in per_source if _caption_free(r))
    likes = [r["likes"] for r in raw] or [0]
    creators = {r["creator_id"] for r in raw if r["creator_id"]}
    scored_overall = [float(r.get("overall_match", 0)) for r in per_source if "overall_match" in r]
    avg_overall_match = round(statistics.mean(scored_overall), 2) if scored_overall else 0.0
    return {
        "download_success_rate": round(dl_ok / max(1, dl_try), 3),
        "vertical_rate": round(verticals / max(1, n_raw), 3),
        "usable_segment_rate": round(usable_segments / max(1, dl_ok), 3) if dl_ok else 0.0,
        "avg_overall_match": avg_overall_match,       # continuous signal (distinguishes query types)
        "relevant_at_10": relevant_at_10, "usable_at_10": usable_at_10,
        "exact_match_rate": round(sum(1 for r in top if is_exact(r)) / max(1, len(top)), 3),
        "final_yield": final_yield,
        "search_efficiency": round(relevant_usable / max(0.1, elapsed), 3),
        "caption_free_rate": round(caption_free / max(1, dl_ok), 3) if dl_ok else 0.0,
        "creator_diversity": round(len(creators) / max(1, n_raw), 3),
        "average_likes": int(statistics.mean(likes)), "median_likes": int(statistics.median(likes)),
        "average_search_time": elapsed,
    }


def _caption_free(r):
    seg = r.get("_best_seg")
    if seg is None:
        return False
    return float(getattr(seg, "caption_probability", 1.0)) < 0.5 and "burned_caption_over_subject" not in (seg.rejection_reasons or [])


# --------------------------------------------------------------- run + resume + reports

def run_tiktok_search_benchmark(intents=None, query_types=None, cfg=None, run_id=None,
                                shuffle=True, status_cb=None, cancel_check=None):
    """Execute the REAL benchmark. Resumable: caches every completed query and skips it on re-run.
    Returns the run_id."""
    cfg = cfg or BenchConfig()
    intents = intents or INTENTS
    run_id = run_id or ("bench_" + time.strftime("%Y%m%d_%H%M%S"))
    run_dir = BENCH_DIR / run_id
    cache_dir = run_dir / "cache"
    proxies = run_dir / "_proxies"
    for d in (cache_dir, proxies):
        d.mkdir(parents=True, exist_ok=True)
    ffmpeg, ffprobe = clip_scraper._ffmpeg_tools()
    reasoning_model = os.environ.get("BENCH_REASONING_MODEL") or None

    def log(m):
        line = "[bench] " + m
        try:
            with (run_dir / "benchmark.log").open("a", encoding="utf-8") as _f:
                _f.write(line + "\n")
        except Exception:
            pass
        if status_cb:
            try:
                status_cb(line)
            except Exception:
                pass
        else:
            try:
                print(line, flush=True)
            except Exception:                 # never let a console-encoding hiccup kill the run
                try:
                    sys.stdout.buffer.write((line + "\n").encode("utf-8", "replace"))
                    sys.stdout.flush()
                except Exception:
                    pass

    if tiktok_login is None or not tiktok_login.is_ready():
        log("TikTok session is NOT connected - connect TikTok first, then resume with --resume " + run_id)
        return run_id

    # build the full query worklist (intent x query_type x variant), randomised per intent
    worklist = []
    for intent in intents:
        qmap = build_queries(intent)
        types = query_types or QUERY_TYPES
        items = [(qt, q) for qt in types for q in qmap.get(qt, [])]
        if shuffle:
            random.shuffle(items)           # randomise order so session/cache state can't bias a type
        for qt, q in items:
            worklist.append((intent, qt, q))
    log(f"worklist: {len(worklist)} real queries across {len(intents)} intents.")

    done = 0
    for intent, qt, q in worklist:
        if cancel_check and cancel_check():
            log("cancelled."); break
        key = scrape_v2._cache_key("tiktok", normalize_query(q), "bench") + "_" + intent.intent_id
        cache_file = cache_dir / (key + ".json")
        if cache_file.exists():
            done += 1
            continue
        try:
            res = _analyze_query(intent, q, qt, cfg, proxies, ffmpeg, ffprobe,
                                 reasoning_model=reasoning_model, status_cb=None)
        except Exception as exc:            # never let one query kill the whole benchmark
            log(f"query failed [{intent.intent_id}/{qt}] {q!r}: {exc.__class__.__name__}: {exc}")
            res = {"intent_id": intent.intent_id, "category": intent.category, "query": q,
                   "query_type": qt, "error": f"{exc.__class__.__name__}: {exc}",
                   "raw": [], "metrics": {}}
        cache_file.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
        done += 1
        m = res.get("metrics", {})
        log(f"[{done}/{len(worklist)}] {intent.category}/{qt} {q!r} -> raw={m.get('raw_results', 0)} "
            f"rel@10={m.get('relevant_at_10', 0)} usable@10={m.get('usable_at_10', 0)} "
            f"yield={m.get('final_yield', 0)}")
        if m.get("login_wall"):
            log("LOGIN WALL / captcha detected. Progress is cached. Reconnect TikTok, then: "
                f"python -m tools.tiktok_search_benchmark --resume {run_id}")
            break
    # cleanup proxy scratch (cache holds the metrics; proxies are large)
    try:
        for f in proxies.glob("*"):
            f.unlink()
    except Exception:
        pass
    build_reports(run_id)
    return run_id


def _load_cache(run_id):
    cache_dir = BENCH_DIR / run_id / "cache"
    rows = []
    for p in sorted(cache_dir.glob("*.json")):
        try:
            rows.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            pass
    return rows


def _aggregate(rows):
    """Aggregate per query_type and per (category, query_type). Cross-query duplicates too."""
    by_type, by_cat_type = {}, {}
    seen_clip_to_queries = {}
    for r in rows:
        m = r.get("metrics") or {}
        if not m:
            continue
        qt = r.get("query_type", "?")
        by_type.setdefault(qt, []).append(m)
        by_cat_type.setdefault((r.get("category", "?"), qt), []).append(m)
        for raw in (r.get("raw") or []):
            seen_clip_to_queries.setdefault(raw["platform_id"], set()).add(r.get("query", ""))
    def summarise(ms):
        keys = ["relevant_at_10", "usable_at_10", "exact_match_rate", "final_yield", "search_efficiency",
                "caption_free_rate", "vertical_rate", "usable_segment_rate", "creator_diversity",
                "download_success_rate", "duplicate_rate", "average_search_time"]
        out = {"n_queries": len(ms)}
        for k in keys:
            vals = [float(x.get(k, 0)) for x in ms if k in x]
            out[k] = round(statistics.mean(vals), 3) if vals else 0.0
        return out
    type_summary = {t: summarise(ms) for t, ms in by_type.items()}
    cat_type_summary = {f"{c}|{t}": summarise(ms) for (c, t), ms in by_cat_type.items()}
    cross_dup = sum(1 for v in seen_clip_to_queries.values() if len(v) > 1)
    cross_query_duplicate_rate = round(cross_dup / max(1, len(seen_clip_to_queries)), 3)
    return type_summary, cat_type_summary, cross_query_duplicate_rate


def build_reports(run_id):
    run_dir = BENCH_DIR / run_id
    rows = _load_cache(run_id)
    type_summary, cat_type_summary, cross_dup = _aggregate(rows)
    ranking = sorted(type_summary.items(), key=lambda kv: kv[1].get("final_yield", 0), reverse=True)
    report = {"run_id": run_id, "analysis_version": BENCH_ANALYSIS_VERSION,
              "queries_analysed": len(rows), "cross_query_duplicate_rate": cross_dup,
              "query_type_ranking": [{"query_type": t, **s} for t, s in ranking],
              "by_category_query_type": cat_type_summary,
              "recommendations": _derive_recommendations(rows, type_summary, cat_type_summary)}
    (run_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    # CSV
    with (run_dir / "report.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["query_type", "n_queries", "relevant_at_10", "usable_at_10", "exact_match_rate",
                    "final_yield", "caption_free_rate", "vertical_rate", "creator_diversity",
                    "average_search_time"])
        for t, s in ranking:
            w.writerow([t, s["n_queries"], s["relevant_at_10"], s["usable_at_10"], s["exact_match_rate"],
                        s["final_yield"], s["caption_free_rate"], s["vertical_rate"],
                        s["creator_diversity"], s["average_search_time"]])
    (run_dir / "report.html").write_text(_html_report(report), encoding="utf-8")
    return report


def _derive_recommendations(rows, type_summary, cat_type_summary):
    """Only real, data-derived rules. Best query types by final_yield, per category, worst types."""
    ranked = sorted(type_summary.items(), key=lambda kv: kv[1].get("final_yield", 0), reverse=True)
    best_overall = [t for t, _ in ranked[:3]]
    worst_overall = [t for t, _ in ranked[-3:]]
    per_cat = {}
    cats = sorted({r.get("category") for r in rows if r.get("category")})
    for c in cats:
        cs = [(t.split("|")[1], s) for t, s in cat_type_summary.items() if t.startswith(c + "|")]
        cs.sort(key=lambda kv: kv[1].get("final_yield", 0), reverse=True)
        per_cat[c] = {"preferred_query_types": [t for t, _ in cs[:3]],
                      "avoid_query_types": [t for t, _ in cs[-2:]] if len(cs) > 3 else []}
    return {"best_overall_query_types": best_overall, "worst_overall_query_types": worst_overall,
            "per_category": per_cat,
            "note": "Data-derived from real runs only. Apply to Scrape V2 query tiers AFTER review; "
                    "keep weak types as late fallback tiers, do not spend primary budget on them."}


def _html_report(report):
    rows = report["query_type_ranking"]
    trs = "".join(
        f"<tr><td>{r['query_type']}</td><td>{r['n_queries']}</td><td>{r['relevant_at_10']}</td>"
        f"<td>{r['usable_at_10']}</td><td>{r['exact_match_rate']}</td><td>{r['final_yield']}</td>"
        f"<td>{r['caption_free_rate']}</td><td>{r['creator_diversity']}</td>"
        f"<td>{r['average_search_time']}s</td></tr>" for r in rows)
    rec = report["recommendations"]
    return f"""<!doctype html><meta charset=utf-8><title>TikTok Search Lab - {report['run_id']}</title>
<style>body{{font-family:system-ui,Segoe UI,sans-serif;max-width:1000px;margin:24px auto;padding:0 16px}}
table{{border-collapse:collapse;width:100%;margin:16px 0}}th,td{{border:1px solid #ccc;padding:6px 10px;text-align:right}}
th:first-child,td:first-child{{text-align:left}}th{{background:#f3f0e6}}h1,h2{{font-family:inherit}}
code{{background:#f3f0e6;padding:2px 5px;border-radius:4px}}</style>
<h1>TikTok Search Lab</h1>
<p>Run <code>{report['run_id']}</code> &middot; {report['queries_analysed']} real queries analysed &middot;
cross-query duplicate rate <b>{report['cross_query_duplicate_rate']}</b></p>
<h2>Query-type ranking (by final yield = relevant+usable segments / raw results)</h2>
<table><tr><th>Query type</th><th>n</th><th>Relevant@10</th><th>Usable@10</th><th>Exact match</th>
<th>Final yield</th><th>Caption-free</th><th>Creator div.</th><th>Search time</th></tr>{trs}</table>
<h2>Recommendations (data-derived)</h2>
<p>Best overall: <b>{', '.join(rec['best_overall_query_types'])}</b> &middot;
weakest: <b>{', '.join(rec['worst_overall_query_types'])}</b></p>
<pre>{json.dumps(rec['per_category'], ensure_ascii=False, indent=2)}</pre>
<p style="color:#666">{rec['note']}</p>"""


# --------------------------------------------------------------- CLI

def _main(argv=None):
    ap = argparse.ArgumentParser(description="Real TikTok search-structure benchmark for the Japan niche.")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--full", action="store_true", help="every intent x every query type")
    g.add_argument("--pilot", action="store_true", help="small real slice (fast, cheap)")
    g.add_argument("--slice", action="store_true",
                   help="corrected 4-category x 3-query-type validation slice (school, konbini, "
                        "unusual japan, hook presenter; subject_action, subject_action_location, native_vlog)")
    g.add_argument("--intent", help="run a single intent_id")
    g.add_argument("--resume", help="resume a run_id (skips cached queries)")
    g.add_argument("--report", help="rebuild reports from an existing run's cache only")
    args = ap.parse_args(argv)

    if args.report:
        rep = build_reports(args.report)
        print(json.dumps(rep["query_type_ranking"], ensure_ascii=False, indent=2))
        return
    if args.resume:
        run_tiktok_search_benchmark(run_id=args.resume)
        return
    if args.intent:
        it = next((i for i in INTENTS if i.intent_id == args.intent), None)
        if not it:
            print("unknown intent_id"); return
        run_tiktok_search_benchmark(intents=[it])
        return
    if args.slice:
        # corrected validation slice: 4 representative categories (incl. the two the first pilot
        # missed) x 3 core query types, one intent each. Fresh run_id = new vision evals only.
        cats = ["school", "convenience_store", "unusual_japan", "hook_presenter"]
        picks = [next(i for i in INTENTS if i.category == c) for c in cats]
        qtypes = ["subject_action", "subject_action_location", "native_vlog"]
        run_tiktok_search_benchmark(intents=picks, query_types=qtypes, cfg=PILOT_CONFIG)
        return
    if args.pilot:
        # a small but REAL slice: one intent from a few core categories, core query types
        cats = ["school", "convenience_store", "vending_machine", "train"]
        picks = [next(i for i in INTENTS if i.category == c) for c in cats]
        qtypes = ["subject_action_location", "action_location", "native_vlog", "format_word",
                  "hashtag", "broad_topic", "english"]
        run_tiktok_search_benchmark(intents=picks, query_types=qtypes, cfg=PILOT_CONFIG)
        return
    # default = full
    run_tiktok_search_benchmark()


if __name__ == "__main__":
    _main()
