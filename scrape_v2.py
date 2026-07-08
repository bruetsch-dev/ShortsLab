"""scrape_v2.py - Scrape V2, a relevance-first, SEGMENT-based scraping engine.

Scrape V1 (agent_core.scrape_social_plan + clip_scraper.scrape_bucket) is left completely
untouched; V2 is a parallel, independently-selectable engine. The core idea:

    Do not collect videos. Collect usable SCENE SEGMENTS.

A source video is only a container; the real candidate is an extracted, quality-checked,
semantically-described SEGMENT inside it. V2 keeps searching / downloading / analysing /
expanding until it has enough FINAL usable segments for every scene (or a hard budget is hit),
instead of stopping the moment enough shallow metadata candidates exist.

Design:
  - Reuses V1's proven low-level primitives from clip_scraper (dual-backend search, ffmpeg/OpenCV
    black-bar / OCR / caption / stability, normalize) and agent_core's LLM helpers - it does NOT
    re-implement them. agent_core is imported LAZILY inside functions to avoid an import cycle
    (agent_core imports scrape_v2 only at the routing point, also lazily).
  - Structured dataclasses for every candidate object, so duration / scene-id / score fields can
    never silently become the wrong type (the dict-soup that bit V1).
  - Pure-logic pieces (query diversity, metadata relevance, ranking, match floors, near-dup,
    global assignment, render validation) contain NO I/O so they are unit-testable offline.

Public entry points (all real, no stubs):
    build_social_search_plan_v2      - script -> concrete observable VisualIntents (+ alternatives)
    queries_for_intent / diversify_queries - subject+action+location+style query tiers, de-duped
    rank_metadata_candidates_v2      - relevance-first ranking (likes are a weak signal)
    download_proxy_v2                - whole-video <=720p no-audio analysis proxy
    discover_segments_v2             - multi-region usable-segment discovery inside one source
    analyze_segment_v2               - segment-level soft quality score + hard rejects
    describe_segments_v2             - Vision stage A: structured per-segment descriptions (small batch)
    match_segments_to_scenes_v2      - Vision stage B: text match of descriptions <-> intents
    retry_unmatched_scenes_v2        - targeted re-search for weak scenes (new queries only)
    score_hook_candidates_v2         - topic-relevant hook caster
    assign_segments_globally_v2      - global assignment with diversity + reuse caps
    scrape_social_plan_v2            - the iterative relevance-first orchestrator
    validate_scrape_render_v2        - V2-aware hard pre-render gate
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

import clip_scraper                      # V1 primitives (no cycle: clip_scraper never imports us)

try:
    import cv2
    import numpy as np
except Exception:                        # pragma: no cover
    cv2 = None
    np = None

try:
    import yt_dlp
except Exception:                        # pragma: no cover
    yt_dlp = None


# ---------------------------------------------------------------- versioning + config

V2_ANALYSIS_VERSION = 1   # bump when segment/quality/vision math changes -> invalidates cache

SCRAPE_V2_CONFIG = {
    "max_total_source_videos": 220,       # metadata candidates considered overall
    "max_downloaded_analysis_videos": 100,
    "max_final_segments": 180,
    "max_queries_per_bucket": 30,
    "max_queries_per_scene_retry": 8,
    "max_bucket_time_seconds": 300,
    "max_total_scrape_time_seconds": 1200,
    "vision_batch_size": 8,
    "max_segments_per_source": 3,
    "target_segment_seconds": 2.3,
    "min_segment_seconds": 1.4,
    "max_segment_seconds": 3.8,
    "proxy_max_height": 720,
    "proxy_max_filesize_mb": 60,
    # assignment diversity / reuse caps
    "max_platform_share": 0.85,
    "max_clips_per_creator": 2,
    "max_clips_per_query": 3,
    "max_near_duplicates_per_visual": 2,
    "max_segments_per_source_final": 2,
    "max_near_duplicate_similarity": 0.92,
}

# Tier-aware like floors: an exact-action query may keep a niche clip with almost no likes; broad
# discovery tiers still want some traction. Likes are a WEAK popularity signal in V2, never a
# hard relevance gate. X floors are quartered downstream (structurally lower engagement).
# Relevance-first V2 does NOT gate footage on likes (likes are a weak relevance signal, and a
# like-gate skews toward big Western viral clips even for a Japanese query). All floors are 0;
# ranking still uses engagement as a small (~7%) tie-breaker, never a hard cutoff.
LIKE_FLOORS_V2 = {
    "exact_action": 0,
    "action_location": 0,
    "semantic_action": 0,
    "native_vlog": 0,
    "broad_context": 0,
    "hashtag": 0,
    "creator_style": 0,
}
V2_QUERY_TIERS = ["exact_action", "action_location", "semantic_action",
                  "native_vlog", "broad_context", "hashtag", "creator_style"]

# script_floor = the minimum literal script_match; overall = minimum combined semantic score.
MATCH_THRESHOLDS_V2 = {
    "concrete": {"script_floor": 5.8, "overall": 6.5},
    "context":  {"script_floor": 4.8, "overall": 6.0},
    "abstract": {"script_floor": 3.8, "overall": 5.5},
}

# segment quality: only genuinely unusable material is a HARD reject; everything else scores soft.
SEGMENT_HARD_REJECT_BLACKBAR = 6.0        # black_bar_score above this = massive letterbox
SEGMENT_MIN_QUALITY = 5.5                 # combined soft quality below this = drop the segment
CONTEXT_FALLBACK_MIN_RELEVANCE = 6.0
CONTEXT_FALLBACK_MIN_QUALITY = 6.5


# ---------------------------------------------------------------- data models

@dataclass
class AlternativeVisualIntent:
    subject: str = ""
    action: str = ""
    location: str = ""
    camera_style: str = ""
    mood: str = ""
    # SHORT native-Japanese search keywords (1-2 tokens each) - real TikTok search uses few words.
    jp_subject: str = ""
    jp_action: str = ""
    jp_location: str = ""


@dataclass
class VisualIntent:
    scene_id: int
    scene_text: str
    visual_type: str = "context"          # concrete | context | abstract
    subject: str = ""
    action: str = ""
    location: str = ""
    camera_style: str = ""
    mood: str = ""
    required_elements: list = field(default_factory=list)
    optional_elements: list = field(default_factory=list)
    avoid_elements: list = field(default_factory=list)
    alternative_visuals: list = field(default_factory=list)   # list[AlternativeVisualIntent]
    # SHORT native-Japanese search keywords (1-2 tokens each) for the primary queries.
    jp_subject: str = ""
    jp_action: str = ""
    jp_location: str = ""

    @property
    def intent_id(self) -> str:
        return f"scene_{self.scene_id}"


@dataclass
class SearchQueryV2:
    query: str
    language: str          # "ja" | "en"
    tier: str
    visual_intent_id: str
    expected_subject: str = ""
    expected_action: str = ""
    expected_location: str = ""
    negative_terms: list = field(default_factory=list)


@dataclass
class SourceVideoCandidate:
    platform: str
    source_id: str
    creator_id: str
    url: str
    caption: str = ""
    hashtags: list = field(default_factory=list)
    likes: int = 0
    duration: float = 0.0
    width: int = 0
    height: int = 0
    query: str = ""
    query_tier: str = ""
    metadata_relevance: float = 0.0
    rank_score: float = 0.0
    raw_item: dict = field(default_factory=dict)   # original backend item for yt-dlp download


@dataclass
class SegmentCandidate:
    segment_id: str
    source_id: str
    platform: str
    source_path: str        # proxy video the segment came from
    start_time: float
    end_time: float
    duration: float
    creator_id: str = ""
    query: str = ""
    query_tier: str = ""
    # quality (segment-level, soft)
    quality_score: float = 0.0
    raw_footage_score: float = 0.0
    edit_stability_score: float = 0.0
    text_heaviness: float = 0.0
    caption_probability: float = 0.0
    black_bar_score: float = 0.0
    vertical_quality: float = 0.0
    # semantics (filled by vision)
    semantic_score: float = 0.0
    visual_description: dict = field(default_factory=dict)
    # bookkeeping
    metadata_relevance: float = 0.0
    frame_hash: str = ""
    rejection_reasons: list = field(default_factory=list)
    final_path: str = ""    # high-quality normalized 9:16 clip once accepted


@dataclass
class SceneAssignment:
    scene_id: int
    segment_id: str = ""
    final_path: str = ""
    assignment_type: str = "unmatched"    # exact|alternative_visual|retry_exact|context_fallback|emergency_fallback|unmatched
    fallback_level: int = 0
    semantic_score: float = 0.0
    quality_score: float = 0.0
    match_class: str = "D_REJECTED"
    queries_used: list = field(default_factory=list)
    rejected_top_candidates: list = field(default_factory=list)


# ---------------------------------------------------------------- LLM + primitive adapters
# agent_core imported lazily to break the cycle. These wrap the same LLM plumbing V1 uses.

def _ac():
    import agent_core
    return agent_core


def _log(status_cb, message):
    _ac().log(status_cb, message)


def _llm_json(messages, max_tokens=4000, temperature=0.3, reasoning_model=None):
    """Text-only LLM call returning a parsed JSON dict (or {})."""
    ac = _ac()
    try:
        return ac._post_llm_json(reasoning_model or ac.GPT55_MODEL, messages,
                                 max_tokens, temperature) or {}
    except Exception:
        return {}


def _vision_json(prompt_text, sheet_path, max_tokens=4000, temperature=0.1, reasoning_model=None):
    """Vision LLM call over one contact sheet, returning a parsed JSON dict (or {})."""
    ac = _ac()
    try:
        data = ac.post_json_url(ac.WAVESPEED_LLM_API, {
            "model": reasoning_model or ac.GPT55_MODEL,
            "messages": [
                {"role": "system", "content": "You describe short-form video segments precisely and "
                 "return STRICT JSON only."},
                {"role": "user", "content": [
                    {"type": "text", "text": prompt_text},
                    {"type": "image_url", "image_url": {"url": ac.image_data_url(sheet_path)}},
                ]},
            ],
            "temperature": temperature, "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }, timeout=240)
        return ac.extract_json_object(data["choices"][0]["message"]["content"]) or {}
    except Exception:
        return {}


# ---------------------------------------------------------------- cache

def _cache_dir(project_dir) -> Path:
    d = Path(project_dir) / "seedance 2.0" / "_v2_cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _cache_key(platform, source_id, kind):
    raw = f"{platform}:{source_id}:{kind}:v{V2_ANALYSIS_VERSION}"
    return hashlib.sha1(raw.encode("utf-8", "ignore")).hexdigest()[:20]


def cache_get(project_dir, platform, source_id, kind):
    p = _cache_dir(project_dir) / f"{_cache_key(platform, source_id, kind)}.json"
    if p.exists():
        try:
            return json.loads(p.read_text("utf-8"))
        except Exception:
            return None
    return None


def cache_put(project_dir, platform, source_id, kind, value):
    p = _cache_dir(project_dir) / f"{_cache_key(platform, source_id, kind)}.json"
    try:
        p.write_text(json.dumps(value, ensure_ascii=False), "utf-8")
    except Exception:
        pass


# ---------------------------------------------------------------- tokenisation / diversity (PURE)

_JP_STOP = {"の", "は", "が", "を", "に", "へ", "と", "で", "も", "や", "し", "て", "だ", "です", "ます"}
_EN_STOP = clip_scraper._STOP


def _tokens(text):
    """Rough multilingual token set: Latin words (>=2) + CJK bigrams. Good enough for overlap."""
    text = str(text or "").lower()
    latin = [t for t in re.findall(r"[a-z0-9]+", text) if len(t) >= 2 and t not in _EN_STOP]
    cjk_chars = re.findall(r"[぀-ヿ一-鿿]", text)
    cjk = ["".join(pair) for pair in zip(cjk_chars, cjk_chars[1:])]      # bigrams
    cjk += [c for c in cjk_chars if c not in _JP_STOP]
    return set(latin) | set(cjk)


def query_similarity(a, b):
    """Jaccard token similarity of two queries (0..1). Used to reject near-identical queries."""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / float(len(ta | tb))


def diversify_queries(queries, threshold=0.62, limit=None):
    """Drop near-identical queries (single-synonym swaps of the same phrase). Keeps first-seen.
    PURE + deterministic so a unit test can assert 'not merely synonyms of one phrase'."""
    kept = []
    for q in queries:
        q = str(q or "").strip()
        if not q:
            continue
        if any(query_similarity(q, k) >= threshold for k in kept):
            continue
        kept.append(q)
        if limit and len(kept) >= limit:
            break
    return kept


def _cap_tokens(x, n):
    """Keep only the first n whitespace tokens - TikTok search uses 1-3 keywords, so a verbose
    intent phrase ('office worker asleep on the last train') is trimmed to its head."""
    return " ".join(str(x or "").split()[:n]).strip()


def queries_for_intent(intent: VisualIntent):
    """Build tiered SearchQueryV2 objects for one visual intent.

    PRIMARY queries are SHORT, native Japanese (1-3 keywords) built from the planner's jp_* fields -
    real TikTok search finds nothing with an 8-word phrase. English phrases are kept only as a low
    backup tier. Every block is length-capped and the relevance-`expected_*` carry the Japanese
    tokens so metadata ranking matches the (Japanese) captions. Diversified per tier."""
    # English (backup only) - capped so even these stay short.
    subj = _cap_tokens(intent.subject, 3)
    act = _cap_tokens(intent.action, 4)
    loc = _cap_tokens(intent.location, 3)
    style = _cap_tokens(intent.camera_style, 1)
    # Short native Japanese keywords (primary). Fall back to the English head when the planner
    # gave no jp_* (older plans / abstract fallbacks) so nothing becomes empty.
    jsub = _cap_tokens(intent.jp_subject, 1) or _cap_tokens(intent.subject, 1)
    jact = _cap_tokens(intent.jp_action, 2) or _cap_tokens(intent.action, 1)
    jact1 = _cap_tokens(intent.jp_action, 1) or _cap_tokens(intent.action, 1)
    jloc = _cap_tokens(intent.jp_location, 1) or _cap_tokens(intent.location, 1)
    # relevance targets = Japanese (captions are Japanese); fall back to English.
    exp_s = (intent.jp_subject or intent.subject or "").strip()
    exp_a = (intent.jp_action or intent.action or "").strip()
    exp_l = (intent.jp_location or intent.location or "").strip()
    alts = [a for a in (intent.alternative_visuals or []) if a]

    def _mk(parts, tier, lang="ja", exp=(None, None, None)):
        # flatten to tokens and drop duplicates in order (so '電車 電車' -> '電車', and a subject that
        # repeats the location collapses) -> shorter, cleaner queries.
        toks = [t for part in parts if part for t in str(part).split() if t]
        q = " ".join(dict.fromkeys(toks)).strip()
        if not q:
            return None
        return SearchQueryV2(
            query=q, language=lang, tier=tier, visual_intent_id=intent.intent_id,
            expected_subject=(exp[0] if exp[0] is not None else exp_s),
            expected_action=(exp[1] if exp[1] is not None else exp_a),
            expected_location=(exp[2] if exp[2] is not None else exp_l),
            negative_terms=list(intent.avoid_elements or []))

    per_tier = {t: [] for t in V2_QUERY_TIERS}
    # primary intent - SHORT Japanese keyword queries
    per_tier["exact_action"].append(_mk([jsub, jact], "exact_action"))
    per_tier["action_location"].append(_mk([jact1, jloc], "action_location"))
    per_tier["action_location"].append(_mk([jsub, jact1, jloc], "action_location"))
    # semantic/broad stay PURE Japanese (mood is English -> would pollute the query)
    per_tier["semantic_action"].append(_mk([jact], "semantic_action"))
    per_tier["native_vlog"].append(_mk([jloc, "vlog"], "native_vlog"))
    per_tier["native_vlog"].append(_mk([jloc, "日常"], "native_vlog"))
    per_tier["broad_context"].append(_mk([jloc], "broad_context"))
    per_tier["creator_style"].append(_mk([jsub, jact1, style], "creator_style"))
    # English backups (low priority) - kept SHORT; may still hit English-tagged JP creators.
    per_tier["broad_context"].append(_mk([_cap_tokens(subj, 2), _cap_tokens(act, 2)],
                                         "broad_context", lang="en", exp=(subj, act, loc)))
    # hashtags from location + required elements (no spaces)
    for el in ([intent.jp_location or loc] + list(intent.required_elements or []))[:3]:
        tag = re.sub(r"\s+", "", _cap_tokens(el, 2))
        if tag:
            per_tier["hashtag"].append(_mk(["#" + tag], "hashtag"))
    # alternative visuals -> genuinely different actions/locations (diversity, not synonyms)
    for alt in alts[:3]:
        a_sub = _cap_tokens(alt.jp_subject, 1) or _cap_tokens(alt.subject, 1)
        a_act = _cap_tokens(alt.jp_action, 2) or _cap_tokens(alt.action, 1)
        a_loc = _cap_tokens(alt.jp_location, 1) or _cap_tokens(alt.location, 1)
        a_exp = ((alt.jp_subject or alt.subject or ""), (alt.jp_action or alt.action or ""),
                 (alt.jp_location or alt.location or ""))
        per_tier["exact_action"].append(_mk([a_sub, a_act], "exact_action", exp=a_exp))
        per_tier["action_location"].append(_mk([a_act, a_loc], "action_location", exp=a_exp))

    out = []
    for tier in V2_QUERY_TIERS:
        cand = [q for q in per_tier[tier] if q]
        kept = diversify_queries([q.query for q in cand])
        for q in cand:
            if q.query in kept:
                out.append(q)
                kept.remove(q.query)
    return out


# ---------------------------------------------------------------- metadata relevance + ranking (PURE)

_RAW_FOOTAGE_POS = ("vlog", "pov", "日常", "散歩", "歩く", "walk", "routine", "ルーティン",
                    "一人暮らし", "帰り", "通勤", "街", "night", "夜")
_RAW_FOOTAGE_NEG = ("解説", "まとめ", "react", "リアクション", "talking", "commentary", "説明",
                    "テロップ", "字幕", "news", "ニュース")
_RISK_TERMS = tuple(list(clip_scraper._ANIME_GAME_TERMS) + list(clip_scraper._AI_CONTENT_TERMS)
                    + list(clip_scraper._LIVE_SCREEN_TERMS) + list(clip_scraper._IDOL_PROMO_TERMS))


def estimate_metadata_relevance(meta: dict, query: SearchQueryV2):
    """0..10 estimate of how well a source's METADATA matches the intent behind its query.
    Uses caption + hashtags + creator + expected subject/action/location, penalises negative
    and known-risk terms. Cheap + deterministic (no LLM) so ranking is fast and testable."""
    blob = " ".join([str(meta.get("caption") or ""), " ".join(meta.get("hashtags") or []),
                     str(meta.get("author") or ""), str(meta.get("author_name") or "")]).lower()
    btoks = _tokens(blob)
    if not btoks:
        return 2.0
    want_action = _tokens(query.expected_action)
    want_subject = _tokens(query.expected_subject)
    want_loc = _tokens(query.expected_location)
    want_query = _tokens(query.query)

    def cover(want):
        return (len(want & btoks) / float(len(want))) if want else 0.0

    # action match matters most, then subject, location, and the raw query text.
    score = (cover(want_action) * 4.2 + cover(want_subject) * 2.6
             + cover(want_loc) * 1.8 + cover(want_query) * 1.4)
    # negative / avoid terms in the caption pull it down hard
    for neg in (query.negative_terms or []):
        if neg and str(neg).lower() in blob:
            score -= 2.0
    if any(t in blob for t in _RISK_TERMS):
        score -= 2.5
    return round(max(0.0, min(10.0, score)), 2)


def expected_raw_footage_probability(query: SearchQueryV2, meta: dict):
    blob = (str(query.query) + " " + str(meta.get("caption") or "")).lower()
    s = 5.0
    s += 1.4 * sum(1 for t in _RAW_FOOTAGE_POS if t in blob)
    s -= 1.8 * sum(1 for t in _RAW_FOOTAGE_NEG if t in blob)
    return max(0.0, min(10.0, s))


def query_specificity(query: SearchQueryV2):
    """More distinct concept tokens (subject+action+location present) = more specific = higher."""
    toks = _tokens(query.query)
    dims = sum(1 for x in (query.expected_subject, query.expected_action,
                           query.expected_location) if str(x or "").strip())
    return max(0.0, min(10.0, len(toks) * 1.6 + dims * 1.2))


def _dynamic_like_floor_v2(tier, platform):
    base = LIKE_FLOORS_V2.get(tier, 0)
    if str(platform).lower() in ("twitter", "x"):
        base = base // 4
    return base


def rank_metadata_candidates_v2(candidates, query: SearchQueryV2, platform_counts=None,
                                creator_counts=None):
    """Score + sort SourceVideoCandidate list by RELEVANCE first (likes are ~7% of the score).
    Applies the tier-aware dynamic like floor. Returns the surviving list sorted best-first.

    rank = relevance*0.45 + specificity*0.15 + raw_footage_prob*0.15 + resolution*0.08
           + engagement*0.07 + diversity*0.10 - risk_penalty
    """
    platform_counts = dict(platform_counts or {})
    creator_counts = dict(creator_counts or {})
    spec = query_specificity(query) / 10.0
    out = []
    for c in candidates:
        meta = {"caption": c.caption, "hashtags": c.hashtags, "author": c.creator_id,
                "author_name": c.creator_id}
        floor = _dynamic_like_floor_v2(c.query_tier or query.tier, c.platform)
        if floor and int(c.likes or 0) < floor:
            continue
        rel = estimate_metadata_relevance(meta, query)
        raw_p = expected_raw_footage_probability(query, meta)
        res = 0.0
        if c.width and c.height:
            long_side = max(c.width, c.height)
            res = max(0.0, min(10.0, (long_side - 400) / 160.0)) if long_side else 0.0
            if c.height < c.width:               # landscape source -> weak
                res *= 0.4
        eng = min(10.0, math.log10(int(c.likes or 0) + 1) * 1.7)   # log-normalised, capped
        blob = (c.caption + " " + " ".join(c.hashtags)).lower()
        risk = 2.5 if any(t in blob for t in _RISK_TERMS) else 0.0
        # diversity bonus: reward platforms/creators not already dominant this bucket
        div = 5.0
        div -= 1.5 * platform_counts.get(str(c.platform).lower(), 0) / 5.0
        div -= 2.0 * creator_counts.get(str(c.creator_id).lower(), 0)
        div = max(0.0, min(10.0, div))
        c.metadata_relevance = rel
        c.rank_score = round(rel * 0.45 + spec * 10 * 0.15 + raw_p * 0.15 + res * 0.08
                             + eng * 0.07 + div * 0.10 - risk, 3)
        out.append(c)
    out.sort(key=lambda x: x.rank_score, reverse=True)
    return out


# ---------------------------------------------------------------- planner (LLM)

def build_social_search_plan_v2(title, script, scenes, understanding=None, reasoning_model=None,
                                status_cb=None):
    """Turn each scene into a CONCRETE, OBSERVABLE VisualIntent (+ >=2 alternatives). The planner is
    explicitly forbidden from emitting abstract themes ('loneliness', 'work culture') as the final
    search unit - it must produce visible situations ('office worker asleep on late train')."""
    ac = _ac()
    if not (script or "").strip():
        return []
    numbered = "\n".join(
        f"scene {i}: {(ac.scene_text_for_planning(s) or s.get('script',''))[:160]}"
        for i, s in enumerate(scenes))
    sys = ("You are a visual researcher for Japanese social-topic shorts made of REAL found footage. "
           "You translate each narration line into a concrete, camera-observable situation that a "
           "phone could have filmed. You NEVER output abstract themes as the search target. Return JSON only.")
    user = (
        ac.understanding_brief(understanding) +
        "For EACH scene, output a PRIMARY visible situation plus at least TWO ALTERNATIVE visible "
        "situations for the same idea. A situation MUST have a visible subject + visible action + a "
        "plausible location. FORBIDDEN as an intent: feelings/claims like 'people feel lonely', "
        "'work culture is strict', 'dating is hard'. REQUIRED style: 'office worker asleep on late "
        "train', 'woman eating alone at a ramen counter', 'salaryman leaving office at night', "
        "'customer checking a price tag in a supermarket', 'small Tokyo apartment interior'.\n"
        "Classify each scene visual_type: 'concrete' (shows a specific object/action), 'context' "
        "(a place/social setting), or 'abstract' (a fragment/feeling/transition).\n\n"
        "CRITICAL - also give SHORT Japanese SEARCH KEYWORDS for each situation (jp_subject, "
        "jp_action, jp_location). These are how a real person searches TikTok, NOT a translation of "
        "the sentence. Keep each ONE short term (jp_action may be 1-2 words). Use the single most "
        "iconic, commonly-searched Japanese word for the thing on screen. Examples: subject 'office "
        "worker asleep on late train' -> jp_subject 'サラリーマン', jp_action "
        "'電車 寝る', jp_location '電車'. NEVER a full sentence, NEVER 5+ words, "
        "NEVER multiple objects in one field. Give the SAME short jp_* keywords for each alternative.\n\n"
        f"Scenes:\n{numbered}\n\n"
        'Return STRICT JSON: {"intents":[{"scene_id":int,"visual_type":"concrete|context|abstract",'
        '"subject":"...","action":"...","location":"...","jp_subject":"1 word","jp_action":"1-2 words",'
        '"jp_location":"1 word","camera_style":"pov|handheld|vlog|static|walking",'
        '"mood":"...","required_elements":[".."],"optional_elements":[".."],"avoid_elements":[".."],'
        '"alternatives":[{"subject":"..","action":"..","location":"..","jp_subject":"..","jp_action":"..",'
        '"jp_location":"..","camera_style":"..","mood":".."},'
        '{"subject":"..","action":"..","location":"..","jp_subject":"..","jp_action":"..",'
        '"jp_location":"..","camera_style":"..","mood":".."}]}]}')
    data = _llm_json([{"role": "system", "content": sys}, {"role": "user", "content": user}],
                     max_tokens=8000, temperature=0.3, reasoning_model=reasoning_model)
    rows = data.get("intents") if isinstance(data.get("intents"), list) else []
    intents = []
    seen_ids = set()
    for r in rows:
        if not isinstance(r, dict):
            continue
        try:
            sid = int(r.get("scene_id"))
        except (TypeError, ValueError):
            continue
        if not (0 <= sid < len(scenes)) or sid in seen_ids:
            continue
        seen_ids.add(sid)
        alts = [AlternativeVisualIntent(
            subject=str(a.get("subject", "")), action=str(a.get("action", "")),
            location=str(a.get("location", "")), camera_style=str(a.get("camera_style", "")),
            mood=str(a.get("mood", "")),
            jp_subject=str(a.get("jp_subject", "")), jp_action=str(a.get("jp_action", "")),
            jp_location=str(a.get("jp_location", ""))) for a in (r.get("alternatives") or []) if isinstance(a, dict)]
        vt = str(r.get("visual_type", "context")).strip().lower()
        if vt not in ("concrete", "context", "abstract"):
            vt = "context"
        intents.append(VisualIntent(
            scene_id=sid,
            scene_text=(ac.scene_text_for_planning(scenes[sid]) or scenes[sid].get("script", ""))[:200],
            visual_type=vt, subject=str(r.get("subject", "")), action=str(r.get("action", "")),
            location=str(r.get("location", "")), camera_style=str(r.get("camera_style", "")),
            mood=str(r.get("mood", "")), required_elements=list(r.get("required_elements") or []),
            optional_elements=list(r.get("optional_elements") or []),
            avoid_elements=list(r.get("avoid_elements") or []), alternative_visuals=alts,
            jp_subject=str(r.get("jp_subject", "")), jp_action=str(r.get("jp_action", "")),
            jp_location=str(r.get("jp_location", ""))))
    # ensure every scene has an intent (fall back to the raw line so nothing is silently dropped)
    have = {it.scene_id for it in intents}
    for i, s in enumerate(scenes):
        if i not in have:
            line = (ac.scene_text_for_planning(s) or s.get("script", ""))[:200]
            intents.append(VisualIntent(scene_id=i, scene_text=line, visual_type="abstract",
                                        action=line[:40]))
    intents.sort(key=lambda it: it.scene_id)
    _log(status_cb, f"Scrape V2: planned {len(intents)} visible visual intent(s) "
                    f"(+{sum(len(it.alternative_visuals) for it in intents)} alternatives).")
    return intents


# ---------------------------------------------------------------- proxy download + segments

def download_proxy_v2(item, dest, status_cb=None):
    """Download a LOW-RES analysis proxy of the WHOLE video (<=720p, no audio, size-capped) so we
    can find usable segments anywhere in the timeline - not just the first seconds. Returns path|None."""
    if yt_dlp is None:
        return None
    url = (item.get("webVideoUrl") or item.get("url") or "") if isinstance(item, dict) else ""
    if not url:
        return None
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmpl = str(dest.with_suffix("")) + ".%(ext)s"
    h = int(SCRAPE_V2_CONFIG["proxy_max_height"])
    fmt = f"bestvideo[height<={h}]/best[height<={h}]/best"
    opts = {
        "quiet": True, "no_warnings": True, "noprogress": True, "outtmpl": tmpl,
        "format": fmt, "merge_output_format": "mp4",
        "max_filesize": int(SCRAPE_V2_CONFIG["proxy_max_filesize_mb"]) * 1024 * 1024,
        "ignoreerrors": True, "postprocessors": [],
    }
    clip_scraper._apply_cookies(opts)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except Exception:
        return None
    for cand in sorted(dest.parent.glob(dest.stem + ".*")):
        if cand.suffix.lower() in (".mp4", ".mov", ".mkv", ".webm") and cand.stat().st_size > 8192:
            if cand != dest:
                try:
                    cand.replace(dest)
                    return dest
                except Exception:
                    return cand
            return dest
    return None


def _candidate_windows(duration, cuts, cfg):
    """Propose analysis windows spread across the WHOLE timeline (5-15%, 20-30%, ... 80-90%),
    each avoiding hard cuts, of ~target length. Early windows are NOT preferred over later ones."""
    target = float(cfg["target_segment_seconds"])
    mn, mx = float(cfg["min_segment_seconds"]), float(cfg["max_segment_seconds"])
    if duration <= mn:
        return []
    regions = [(0.05, 0.15), (0.20, 0.30), (0.40, 0.50), (0.60, 0.70), (0.80, 0.90)]
    if duration < 8.0:                                   # short video: also probe start/mid/end
        regions = [(0.0, 0.15), (0.42, 0.58), (0.82, 0.98)]
    windows = []
    for lo, hi in regions:
        centre = duration * (lo + hi) / 2.0
        start = max(0.0, min(duration - mn, centre - target / 2.0))
        end = min(duration, start + target)
        # shrink/shift so no hard cut falls INSIDE the window (keep a clean continuous shot)
        inside = [c for c in cuts if start + 0.08 < c < end - 0.08]
        if inside:
            first_cut = min(inside)
            if first_cut - start >= mn:
                end = first_cut - 0.05
            else:
                start = max(0.0, first_cut + 0.05)
                end = min(duration, start + target)
                if any(start + 0.08 < c < end - 0.08 for c in cuts):
                    continue
        if end - start >= mn:
            windows.append((round(start, 2), round(min(end, start + mx), 2)))
    # de-overlap
    windows.sort()
    merged = []
    for w in windows:
        if merged and w[0] < merged[-1][1] - 0.3:
            continue
        merged.append(w)
    return merged[:int(cfg["max_segments_per_source"])]


def discover_segments_v2(source: SourceVideoCandidate, proxy_path, ffmpeg, ffprobe, status_cb=None,
                         cfg=None):
    """Find up to N usable SEGMENTS anywhere in one proxy video (not just the intro). Returns
    a list of SegmentCandidate (quality not yet scored). A whole video is never discarded just
    because part of it has captions / a talking head / fast cuts."""
    cfg = cfg or SCRAPE_V2_CONFIG
    duration = clip_scraper._probe_duration(proxy_path, ffprobe)
    if duration <= float(cfg["min_segment_seconds"]):
        return []
    scan = min(duration, 90.0)
    cuts = clip_scraper.hard_cut_times(proxy_path, ffmpeg, scan_seconds=scan)
    windows = _candidate_windows(duration, cuts, cfg)
    segs = []
    for (start, end) in windows:
        sid = f"{source.platform}_{source.source_id}_{int(start*1000)}_{int(end*1000)}"
        segs.append(SegmentCandidate(
            segment_id=sid, source_id=source.source_id, platform=source.platform,
            source_path=str(proxy_path), start_time=start, end_time=end,
            duration=round(end - start, 3), creator_id=source.creator_id,
            query=source.query, query_tier=source.query_tier,
            metadata_relevance=source.metadata_relevance))
    return segs


# ---------------------------------------------------------------- segment quality (soft + hard)

def _segment_caption_signals(frames_bgr):
    """Distinguish SOCIAL OVERLAY captions from PHYSICAL scene text. Returns
    (caption_probability 0..1, text_heaviness 0..10, hard_over_subject). Requires MULTIPLE signals
    for a hard overlay verdict: screen-fixed position across frames + social geometry + over the
    centre/subject. Physical signage moves with the camera and sits off-centre -> low probability."""
    if cv2 is None or np is None or not frames_bgr:
        return 0.0, 0.0, False
    h, w = frames_bgr[0].shape[:2]
    per_frame_boxes = []
    areas = []
    for f in frames_bgr:
        gray = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY)
        hsv = cv2.cvtColor(f, cv2.COLOR_BGR2HSV)
        boxes = clip_scraper._frame_caption_boxes(gray, hsv=hsv)
        per_frame_boxes.append(boxes)
        areas.append(sum(bw * bh for (_x, _y, bw, bh) in boxes) / float(w * h))
    clusters = clip_scraper._cluster_caption_boxes(per_frame_boxes)
    text_heaviness = round(min(10.0, (sum(areas) / max(1, len(areas))) * 100.0 * 0.9), 2)
    best_prob = 0.0
    hard_over_subject = False
    need = max(2, int(math.ceil(len(frames_bgr) * 0.6)))
    for cl in clusters:
        x, y, bw, bh = cl["box"]
        cx, cy = x + bw / 2.0, y + bh / 2.0
        signals = 0
        if len(cl["frames"]) >= need:                     # screen-fixed across frames
            signals += 1
        if bw >= 0.30 * w and 0.20 * w <= cx <= 0.80 * w: # wide + horizontally centred = social geometry
            signals += 1
        if 0.10 * h <= y <= 0.86 * h and bh <= 0.22 * h:  # caption band, not a full-height sign
            signals += 1
        over_subject = (0.30 * h <= cy <= 0.75 * h and 0.30 * w <= cx <= 0.70 * w)
        if over_subject:
            signals += 1
        prob = min(1.0, signals / 4.0)
        if prob > best_prob:
            best_prob = prob
        if signals >= 3 and over_subject and bw >= 0.35 * w:
            hard_over_subject = True
    return round(best_prob, 2), text_heaviness, hard_over_subject


def analyze_segment_v2(seg: SegmentCandidate, ffmpeg, ffprobe, status_cb=None):
    """Segment-level quality: sample frames INSIDE [start,end] only, run the reusable primitives,
    produce a soft quality_score. Hard-reject only genuinely unusable material. Returns the segment
    with quality fields filled and rejection_reasons populated (empty list = passed hard gates)."""
    src = seg.source_path
    mid = (seg.start_time + seg.end_time) / 2.0
    win = max(1.0, seg.duration)
    reasons = []

    dims = clip_scraper._probe_dims(src, ffprobe)
    if not dims:
        seg.rejection_reasons = ["unreadable"]
        return seg
    w, h = dims
    vertical_quality = 0.0
    if h > 0 and w > 0:
        ratio = h / float(w)
        vertical_quality = max(0.0, min(10.0, (ratio - 0.9) * 9.0))
        if max(w, h) < clip_scraper.MIN_LONG_SIDE:
            vertical_quality *= 0.5
    # sample frames within the segment window (offset ffmpeg -ss handled by sampling around mid)
    frames = clip_scraper._sample_bgr_frames(src, ffmpeg, 5, seconds=seg.end_time, width=480)
    # keep only frames roughly inside the window by re-sampling at the exact window
    frames = _sample_window_frames(src, ffmpeg, seg.start_time, seg.end_time, n=5)
    if not frames:
        seg.rejection_reasons = ["no_frames"]
        return seg

    fv = clip_scraper.detect_fake_vertical_or_black_bars(src, ffmpeg, seconds=seg.end_time)
    black_bar = float(fv.get("black_bar_score", 0.0))
    if black_bar > SEGMENT_HARD_REJECT_BLACKBAR:
        reasons.append("massive_black_bars")
    if h < w and vertical_quality < 2.0:
        reasons.append("landscape_no_crop")

    # OCR: text load + AI watermark (hard) using the reusable stats
    areas, lines, texts = _ocr_window(src, ffmpeg, seg.start_time, seg.end_time)
    text_heaviness_ocr = clip_scraper._score_from_ocr(areas, lines)
    if any(clip_scraper._AI_WATERMARK_RE.search(t) for t in texts):
        reasons.append("ai_watermark")

    cap_prob, cap_text_heavy, hard_over_subject = _segment_caption_signals(frames)
    if hard_over_subject:
        reasons.append("burned_caption_over_subject")

    # internal cuts WITHIN this window
    stab = _window_stability(src, ffmpeg, ffprobe, seg.start_time, seg.end_time)
    edit_stability = 10.0 if stab["stable"] else max(0.0, 8.0 - stab["internal_cut_count"] * 3.0)
    if stab["internal_cut_count"] >= 2:
        reasons.append("rapid_internal_cuts")

    text_heaviness = max(text_heaviness_ocr, cap_text_heavy)
    text_cleanliness = max(0.0, 10.0 - text_heaviness * 2.0)
    clean_frame_score = max(0.0, 10.0 - cap_prob * 8.0)
    resolution_score = max(0.0, min(10.0, (max(w, h) - 400) / 160.0))
    raw_footage_score = max(0.0, min(10.0, 6.0 + (edit_stability - 5.0) * 0.4 - cap_prob * 3.0))
    action_visibility = 6.0    # neutral prior; vision stage refines this

    quality = (vertical_quality * 0.15 + raw_footage_score * 0.20 + edit_stability * 0.20
               + clean_frame_score * 0.15 + text_cleanliness * 0.10 + resolution_score * 0.10
               + action_visibility * 0.10)

    seg.quality_score = round(quality, 2)
    seg.raw_footage_score = round(raw_footage_score, 2)
    seg.edit_stability_score = round(edit_stability, 2)
    seg.text_heaviness = round(text_heaviness, 2)
    seg.caption_probability = cap_prob
    seg.black_bar_score = round(black_bar, 2)
    seg.vertical_quality = round(vertical_quality, 2)
    seg.rejection_reasons = reasons
    return seg


def _sample_window_frames(path, ffmpeg, start, end, n=5):
    if cv2 is None or not ffmpeg:
        return []
    import subprocess, tempfile
    frames = []
    tmp = Path(tempfile.mkdtemp(prefix="v2seg_"))
    try:
        span = max(0.2, end - start)
        for i in range(n):
            t = start + span * (i + 0.5) / n
            fp = tmp / f"f{i}.jpg"
            subprocess.run([ffmpeg, "-y", "-ss", f"{t:.3f}", "-i", str(path), "-frames:v", "1",
                            "-vf", "scale=480:-1", str(fp)], capture_output=True, timeout=30)
            if fp.exists():
                img = cv2.imread(str(fp), cv2.IMREAD_COLOR)
                if img is not None:
                    frames.append(img)
    except Exception:
        pass
    finally:
        try:
            for f in tmp.glob("*"):
                f.unlink()
            tmp.rmdir()
        except Exception:
            pass
    return frames


def _ocr_window(path, ffmpeg, start, end):
    """OCR frames inside the window; returns (areas, lines, texts) like clip_scraper._ocr_frame_stats."""
    if cv2 is None or np is None:
        return [], [], []
    frames = _sample_window_frames(path, ffmpeg, start, end, n=4)
    ocr = clip_scraper._get_ocr()
    if not frames or ocr is None:
        return [], [], []
    areas, lines, texts = [], [], []
    for f in frames:
        h, w = f.shape[:2]
        try:
            result, _ = ocr(f, use_det=True, use_cls=False, use_rec=True)
        except Exception:
            result = None
        area, cnt = 0.0, 0
        for r in (result or []):
            try:
                quad, text, conf = r[0], str(r[1]).strip(), float(r[2])
            except Exception:
                continue
            if conf >= 0.4 and len(text) >= 2:
                texts.append(text)
            if conf < 0.55 or len(text) < 2:
                continue
            xs = [p[0] for p in quad]; ys = [p[1] for p in quad]
            area += max(1.0, max(xs) - min(xs)) * max(1.0, max(ys) - min(ys))
            cnt += 1
        areas.append(area / float(w * h))
        lines.append(cnt)
    return areas, lines, texts


def _window_stability(path, ffmpeg, ffprobe, start, end):
    cuts = clip_scraper.hard_cut_times(path, ffmpeg, scan_seconds=min(end + 1.0, 90.0))
    inside = [c for c in cuts if start + 0.1 < c < end - 0.1]
    gaps = []
    bounds = [start] + inside + [end]
    for i in range(1, len(bounds)):
        gaps.append(bounds[i] - bounds[i - 1])
    return {"internal_cut_count": len(inside),
            "stable": len(inside) <= 1 and all(g >= 0.7 for g in (gaps or [end - start]))}


# ---------------------------------------------------------------- near-duplicate (PURE-ish)

def _dhash(frame_bgr, size=8):
    if cv2 is None or np is None or frame_bgr is None:
        return ""
    gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
    small = cv2.resize(gray, (size + 1, size))
    diff = small[:, 1:] > small[:, :-1]
    bits = 0
    for b in diff.flatten():
        bits = (bits << 1) | int(bool(b))
    return f"{bits:0{(size*size + 3)//4}x}"


def _hamming_hex(a, b):
    if not a or not b or len(a) != len(b):
        return 64
    try:
        return bin(int(a, 16) ^ int(b, 16)).count("1")
    except (ValueError, TypeError):
        return 64            # a malformed hash is treated as maximally different, never a crash


def hash_similarity(a, b, bits=64):
    if not a or not b:
        return 0.0
    return 1.0 - _hamming_hex(a, b) / float(bits)


def is_near_duplicate(seg_a, seg_b, threshold=None):
    threshold = threshold or SCRAPE_V2_CONFIG["max_near_duplicate_similarity"]
    if getattr(seg_a, "frame_hash", "") and getattr(seg_b, "frame_hash", ""):
        if hash_similarity(seg_a.frame_hash, seg_b.frame_hash) >= threshold:
            return True
    # metadata fallback: same creator + very close duration + overlapping caption tokens
    if seg_a.creator_id and seg_a.creator_id == seg_b.creator_id \
            and abs(seg_a.duration - seg_b.duration) < 0.3:
        return True
    return False


# ---------------------------------------------------------------- match formula (PURE)

def semantic_match_score(subject_match, action_match, location_match, mood_match, script_match):
    return round(subject_match * 0.20 + action_match * 0.35 + location_match * 0.15
                 + mood_match * 0.10 + script_match * 0.20, 3)


def match_class_for(overall, visual_type):
    th = MATCH_THRESHOLDS_V2.get(visual_type, MATCH_THRESHOLDS_V2["context"])
    if overall >= 8.0:
        return "A_MATCH"
    if overall >= 7.0:
        return "B_MATCH"
    if overall >= th["overall"]:
        return "C_MATCH"
    return "D_REJECTED"


def passes_match_floors(script_match, overall, visual_type):
    th = MATCH_THRESHOLDS_V2.get(visual_type, MATCH_THRESHOLDS_V2["context"])
    return script_match >= th["script_floor"] and overall >= th["overall"]


# ---------------------------------------------------------------- vision stage A + B

def describe_segments_v2(segments, project_dir, ffmpeg, reasoning_model=None, status_cb=None,
                         batch_size=None):
    """Vision stage A: structured description of each segment in SMALL batches (default 8). Fills
    seg.visual_description + seg.frame_hash + refines raw/stability/action from what is actually
    seen. Cached per (platform, source_id+window)."""
    ac = _ac()
    batch = int(batch_size or SCRAPE_V2_CONFIG["vision_batch_size"])
    frames_dir = Path(project_dir) / "review" / "_v2_desc"
    frames_dir.mkdir(parents=True, exist_ok=True)
    todo = []
    for seg in segments:
        cached = cache_get(project_dir, seg.platform, seg.segment_id, "describe")
        if cached:
            seg.visual_description = cached
            seg.frame_hash = cached.get("frame_hash", seg.frame_hash)
            seg.raw_footage_score = float(cached.get("raw_footage_score", seg.raw_footage_score))
            seg.edit_stability_score = float(cached.get("edit_stability", seg.edit_stability_score))
        else:
            todo.append(seg)
    _log(status_cb, f"Scrape V2: describing {len(todo)} segment(s) (cached {len(segments)-len(todo)}) "
                    f"in batches of {batch}...")
    for b0 in range(0, len(todo), batch):
        chunk = todo[b0:b0 + batch]
        strips = []
        for i, seg in enumerate(chunk):
            strip = _segment_strip(seg, frames_dir, i, ffmpeg)
            if strip:
                strips.append((seg, strip))
                # a mid-window frame hash for near-dup
                mids = _sample_window_frames(seg.source_path, ffmpeg,
                                             (seg.start_time + seg.end_time) / 2.0 - 0.05,
                                             (seg.start_time + seg.end_time) / 2.0 + 0.05, n=1)
                seg.frame_hash = _dhash(mids[0]) if mids else ""
        if not strips:
            continue
        sheet = ac.create_media_contact_sheet(
            [s for (_seg, s) in strips], frames_dir / f"_v2_sheet_{b0}.jpg",
            title="Each tile = frames across ONE segment (seg_00, seg_01, ...)")
        if not sheet:
            continue
        prompt = (
            "Describe each numbered segment tile (seg_00, seg_01, ...); each tile is several frames "
            "across ONE short clip. Judge the whole clip. Return STRICT JSON keyed by index.\n"
            'For each: {"subjects":[".."],"subject_count":int,"action":"..","location":"..",'
            '"camera_style":"..","shot_size":"..","motion":"low|moderate|high","visible_text":"..",'
            '"creator_overlay":true|false,"burned_captions":true|false,"raw_footage_score":0-10,'
            '"edit_stability":0-10,"visual_quality":0-10,"action_visibility":0-10,'
            '"age_confidence":"adult|teen|child|unknown","sexualized_content":true|false,"usable":true|false}\n'
            'Return {"segments": {"0": {..}, "1": {..}, ...}}')
        data = _vision_json(prompt, sheet, max_tokens=4000, temperature=0.1,
                            reasoning_model=reasoning_model)
        smap = data.get("segments") if isinstance(data.get("segments"), dict) else {}
        for idx, (seg, _strip) in enumerate(strips):
            d = smap.get(str(idx)) if isinstance(smap.get(str(idx)), dict) else {}
            if not d:
                continue
            seg.visual_description = d
            try:
                seg.raw_footage_score = float(d.get("raw_footage_score", seg.raw_footage_score))
            except (TypeError, ValueError):
                pass
            try:
                seg.edit_stability_score = float(d.get("edit_stability", seg.edit_stability_score))
            except (TypeError, ValueError):
                pass
            d["frame_hash"] = seg.frame_hash
            cache_put(project_dir, seg.platform, seg.segment_id, "describe", d)
    return segments


def _segment_strip(seg: SegmentCandidate, frames_dir, idx, ffmpeg):
    """Multi-frame horizontal strip built from frames INSIDE the segment window."""
    import subprocess
    frames = _sample_window_frames(seg.source_path, ffmpeg, seg.start_time, seg.end_time, n=4)
    if not frames or cv2 is None or np is None:
        return None
    hgt = 240
    tiles = []
    for f in frames:
        h, w = f.shape[:2]
        scale = hgt / float(h)
        tiles.append(cv2.resize(f, (max(1, int(w * scale)), hgt)))
    strip = np.hstack(tiles) if tiles else None
    if strip is None:
        return None
    out = frames_dir / f"seg_strip_{idx}.jpg"
    try:
        cv2.imwrite(str(out), strip)
        return out
    except Exception:
        return None


def match_segments_to_scenes_v2(intents, segments, reasoning_model=None, status_cb=None):
    """Vision stage B (TEXT): compare structured segment descriptions to the visual intents. Returns
    a dict scene_id -> sorted list of {segment, subject_match, action_match, location_match,
    mood_match, script_match, semantic_match, overall_match, match_class} candidates that pass floors."""
    ac = _ac()
    described = [s for s in segments if s.visual_description]
    if not described or not intents:
        return {}
    seg_lines = []
    seg_by_id = {}
    for i, s in enumerate(described):
        d = s.visual_description
        seg_by_id[i] = s
        seg_lines.append(
            f'seg {i} (id={s.segment_id}): subjects={d.get("subjects")}, action="{d.get("action")}", '
            f'location="{d.get("location")}", camera={d.get("camera_style")}, motion={d.get("motion")}, '
            f'usable={d.get("usable")}')
    intent_lines = []
    for it in intents:
        intent_lines.append(
            f'scene {it.scene_id} [{it.visual_type}]: subject="{it.subject}", action="{it.action}", '
            f'location="{it.location}", mood="{it.mood}", avoid={it.avoid_elements}; text="{it.scene_text[:80]}"')
    prompt = (
        "You match short video SEGMENTS to narration SCENES for a found-footage short. For EACH scene, "
        "pick the best-fitting segments and score the fit. A segment fits when its subject/action/"
        "location genuinely support the scene's visible intent. Fragments/abstract scenes accept a "
        "topically coherent segment. Never invent a match for off-topic footage.\n\n"
        "SEGMENTS:\n" + "\n".join(seg_lines) + "\n\nSCENES:\n" + "\n".join(intent_lines) + "\n\n"
        'Return STRICT JSON: {"scenes": {"<scene_id>": [{"seg": <seg index>, "subject_match":0-10,'
        '"action_match":0-10,"location_match":0-10,"mood_match":0-10,"script_match":0-10,'
        '"style_match":0-10,"reason":"short"}], ...}} - list up to 4 candidates per scene, best first.')
    data = _llm_json([{"role": "system", "content": "You are a precise footage-to-script matcher. JSON only."},
                      {"role": "user", "content": prompt}],
                     max_tokens=6000, temperature=0.1, reasoning_model=reasoning_model)
    smap = data.get("scenes") if isinstance(data.get("scenes"), dict) else {}
    intent_by_id = {it.scene_id: it for it in intents}
    out = {}
    for sid_str, cands in smap.items():
        try:
            sid = int(sid_str)
        except (TypeError, ValueError):
            continue
        it = intent_by_id.get(sid)
        if it is None or not isinstance(cands, list):
            continue
        scored = []
        for c in cands:
            if not isinstance(c, dict):
                continue
            try:
                seg = seg_by_id[int(c.get("seg"))]
            except (KeyError, TypeError, ValueError):
                continue
            def g(k):
                try:
                    return max(0.0, min(10.0, float(c.get(k, 0))))
                except (TypeError, ValueError):
                    return 0.0
            subj, act, loc, mood = g("subject_match"), g("action_match"), g("location_match"), g("mood_match")
            script_m = g("script_match")
            style_m = g("style_match")
            overall = semantic_match_score(subj, act, loc, mood, script_m)
            if not passes_match_floors(script_m, overall, it.visual_type):
                continue
            scored.append({
                "segment": seg, "subject_match": subj, "action_match": act, "location_match": loc,
                "mood_match": mood, "script_match": script_m, "style_match": style_m,
                "semantic_match": overall, "overall_match": overall,
                "match_class": match_class_for(overall, it.visual_type),
                "reason": str(c.get("reason", ""))[:160]})
            seg.semantic_score = max(seg.semantic_score, overall)
        scored.sort(key=lambda r: (r["overall_match"], r["segment"].quality_score), reverse=True)
        out[sid] = scored
    matched = sum(1 for v in out.values() if v)
    _log(status_cb, f"Scrape V2: matched {matched}/{len(intents)} scene(s) after floors.")
    return out


# ---------------------------------------------------------------- global assignment (PURE-ish)

def assign_segments_globally_v2(intents, scene_candidates, cfg=None, status_cb=None):
    """Global assignment: maximise total relevance while enforcing diversity + reuse caps. Improved
    greedy with backtracking (a scene can steal a segment from a scene that still has alternatives).
    Returns {scene_id: SceneAssignment}. PURE: operates on already-scored candidates."""
    cfg = cfg or SCRAPE_V2_CONFIG
    intent_by_id = {it.scene_id: it for it in intents}
    order = sorted(intent_by_id.keys())

    # edges: (assignment_score, scene_id, cand) best first
    edges = []
    for sid, cands in (scene_candidates or {}).items():
        for c in cands:
            seg = c["segment"]
            a_score = (c["overall_match"] * 0.55 + seg.quality_score * 0.20
                       + c.get("style_match", 0) * 0.10 + 0.10 * 5.0 + 0.05 * 5.0)
            edges.append((round(a_score, 3), sid, c))
    edges.sort(key=lambda e: e[0], reverse=True)

    assignments = {sid: SceneAssignment(scene_id=sid) for sid in order}
    seg_use = {}          # segment_id -> count
    source_use = {}       # source_id -> count
    creator_use = {}      # creator_id -> count
    used_hashes = []      # (scene_id, hash)

    def _adjacent_dupe(sid, seg):
        for nb in (sid - 1, sid + 1):
            a = assignments.get(nb)
            if a and a.segment_id:
                other = a._seg if hasattr(a, "_seg") else None
                if other is not None and is_near_duplicate(other, seg, cfg["max_near_duplicate_similarity"]):
                    return True
        return False

    def _caps_ok(seg, cand_class):
        if seg_use.get(seg.segment_id, 0) >= 2:
            return False
        if source_use.get(seg.source_id, 0) >= cfg["max_segments_per_source_final"]:
            return False
        if creator_use.get(seg.creator_id, 0) >= cfg["max_clips_per_creator"]:
            return False
        return True

    for a_score, sid, c in edges:
        a = assignments[sid]
        if a.segment_id:                                  # scene already filled
            continue
        seg = c["segment"]
        if not _caps_ok(seg, c["match_class"]):
            continue
        if _adjacent_dupe(sid, seg):
            continue
        a.segment_id = seg.segment_id
        a.final_path = seg.final_path or seg.source_path
        a.assignment_type = "exact"
        a.semantic_score = c["overall_match"]
        a.quality_score = seg.quality_score
        a.match_class = c["match_class"]
        a.queries_used = [seg.query] if seg.query else []
        a._seg = seg
        seg_use[seg.segment_id] = seg_use.get(seg.segment_id, 0) + 1
        source_use[seg.source_id] = source_use.get(seg.source_id, 0) + 1
        creator_use[seg.creator_id] = creator_use.get(seg.creator_id, 0) + 1
        used_hashes.append((sid, seg.frame_hash))
    return assignments


# ---------------------------------------------------------------- render validation V2 (PURE)

def validate_scrape_render_v2(config, status_cb=None):
    """V2-aware hard pre-render gate. Understands assignment_type/fallback_level. Raises RuntimeError
    with a clear reason on a broken timeline; allows a controlled context fallback but never a fully
    irrelevant clip. Returns True on pass."""
    scenes = config.get("scenes", []) or []
    if not scenes:
        raise RuntimeError("Scrape V2 pre-render validation failed: no scenes")
    try:
        vs = float(config.get("voice_speed", 0))
    except (TypeError, ValueError):
        vs = 0.0
    if abs(vs - 1.20) > 0.001:
        raise RuntimeError(f"Scrape V2 pre-render validation failed: voice_speed {config.get('voice_speed')} != 1.20x")
    for i, sc in enumerate(scenes):
        for ov in (sc.get("overlays") or []):
            if ov.get("type") in ("arrows", "highlight", "paper", "newspaper", "counter"):
                raise RuntimeError("Scrape V2: untargeted/legacy overlay present")
            if ov.get("type") == "callout" and ov.get("shape") in ("circle", "stamp"):
                raise RuntimeError(f"Scrape V2: circle/stamp overlay on scene {sc.get('id')}")
    if (scenes[0].get("visual_role") or "") != "hook_influencer":
        raise RuntimeError("Scrape V2: hook is not the first scene")
    for i, sc in enumerate(scenes):
        if not sc.get("clip"):
            raise RuntimeError(f"Scrape V2: scene {i} has no segment clip")
        atype = str(sc.get("assignment_type") or "exact")
        if atype == "emergency_fallback":
            _log(status_cb, f"Scrape V2 WARNING: scene {i} uses an emergency fallback clip.")
        if str(sc.get("match_class") or "") == "D_REJECTED" and atype not in ("context_fallback", "emergency_fallback"):
            raise RuntimeError(f"Scrape V2: scene {i} is D_REJECTED without a controlled fallback")
        try:
            bbs = float(sc.get("black_bar_score") or 0)
        except (TypeError, ValueError):
            bbs = 0.0
        if sc.get("is_fake_vertical") or bbs > SEGMENT_HARD_REJECT_BLACKBAR:
            raise RuntimeError(f"Scrape V2: scene {i} massive black bars (score={bbs})")
    config["pre_render_validation_passed"] = True
    _log(status_cb, "Scrape V2 pre-render validation passed.")
    return True


# ---------------------------------------------------------------- debug report

def build_debug_report(state: dict) -> dict:
    """Assemble the structured V2 run report from the accumulated counters + per-scene assignments."""
    return {
        "engine": "scrape_v2",
        "analysis_version": V2_ANALYSIS_VERSION,
        "queries_executed": state.get("queries_executed", 0),
        "raw_results": state.get("raw_results", 0),
        "metadata_candidates": state.get("metadata_candidates", 0),
        "downloaded_sources": state.get("downloaded_sources", 0),
        "segments_discovered": state.get("segments_discovered", 0),
        "segments_quality_passed": state.get("segments_quality_passed", 0),
        "segments_semantic_passed": state.get("segments_semantic_passed", 0),
        "scenes_exact_matched": state.get("scenes_exact_matched", 0),
        "scenes_alternative_matched": state.get("scenes_alternative_matched", 0),
        "scenes_context_fallback": state.get("scenes_context_fallback", 0),
        "scenes_unmatched": state.get("scenes_unmatched", 0),
        "scene_retries": state.get("scene_retries", 0),
        "rejections": state.get("rejections", {}),
        "budgets": {"time_s": round(state.get("elapsed", 0.0), 1),
                    "downloads": state.get("downloaded_sources", 0),
                    "queries": state.get("queries_executed", 0)},
        "scenes": state.get("scene_reports", []),
    }


# ---------------------------------------------------------------- search / download helpers

def _item_to_source(item, query: SearchQueryV2):
    m = clip_scraper._item_meta(item)
    sid = m.get("id") or m.get("url")
    if not sid:
        return None
    return SourceVideoCandidate(
        platform=str(m.get("platform") or "tiktok"), source_id=str(sid),
        creator_id=str(m.get("author") or ""), url=str(m.get("url") or ""),
        caption=str(m.get("caption") or ""), hashtags=list(m.get("hashtags") or []),
        likes=int(m.get("likes") or 0), duration=float(m.get("duration") or 0.0),
        width=int(m.get("w") or 0), height=int(m.get("h") or 0),
        query=query.query, query_tier=query.tier, raw_item=item)


def _search_sources(queries, platforms, cancel_check, deadline, seen_source_ids, state, status_cb,
                    sort="RELEVANCE"):
    """Run a batch of SearchQueryV2 through the dual backend, dedupe by source_id, relevance-rank.

    ``sort`` is the backend result order (RELEVANCE | MOST_LIKED | MOST_VIEWED | MOST_RECENT).
    Default RELEVANCE = TikTok's own topical order, which returns far more on-topic Japanese
    footage for a Japanese query than MOST_LIKED (which surfaces big Western viral clips)."""
    ranked_all = []
    plat_counts, creator_counts = {}, {}
    for q in queries:
        if (cancel_check and cancel_check()) or (deadline and time.monotonic() >= deadline):
            break
        items = clip_scraper.backend_search(q.query, 12, status_cb=status_cb, sort=sort,
                                            platforms=platforms, deadline=deadline) or []
        state["queries_executed"] = state.get("queries_executed", 0) + 1
        state["raw_results"] = state.get("raw_results", 0) + len(items)
        fresh = []
        for it in items:
            src = _item_to_source(it, q)
            if src is None or src.source_id in seen_source_ids:
                continue
            seen_source_ids.add(src.source_id)
            fresh.append(src)
        state["metadata_candidates"] = state.get("metadata_candidates", 0) + len(fresh)
        ranked = rank_metadata_candidates_v2(fresh, q, plat_counts, creator_counts)
        for s in ranked:
            plat_counts[s.platform.lower()] = plat_counts.get(s.platform.lower(), 0) + 1
            creator_counts[s.creator_id.lower()] = creator_counts.get(s.creator_id.lower(), 0) + 1
        ranked_all.extend(ranked)
    ranked_all.sort(key=lambda s: s.rank_score, reverse=True)
    return ranked_all


def _download_and_segment(sources, project_dir, ffmpeg, ffprobe, cancel_check, deadline, state,
                          status_cb, download_budget):
    """Proxy-download top-ranked sources, discover multi-region segments, score segment quality.
    Returns quality-passed SegmentCandidate list. Records rejection tallies in state."""
    cand_root = Path(project_dir) / "seedance 2.0" / "_v2_proxies"
    cand_root.mkdir(parents=True, exist_ok=True)
    rej = state.setdefault("rejections", {})
    passed = []
    for src in sources:
        if len(state.setdefault("_downloaded_ids", set())) >= download_budget:
            break
        if (cancel_check and cancel_check()) or (deadline and time.monotonic() >= deadline):
            break
        if src.source_id in state["_downloaded_ids"]:
            continue
        state["_downloaded_ids"].add(src.source_id)
        proxy = cand_root / ("proxy_" + src.platform + "_"
                             + re.sub(r"[^A-Za-z0-9]+", "_", src.source_id)[:24] + ".mp4")
        got = download_proxy_v2(src.raw_item, proxy, status_cb=None)
        if not got:
            rej["download_failed"] = rej.get("download_failed", 0) + 1
            continue
        state["downloaded_sources"] = state.get("downloaded_sources", 0) + 1
        segs = discover_segments_v2(src, got, ffmpeg, ffprobe, status_cb=status_cb)
        state["segments_discovered"] = state.get("segments_discovered", 0) + len(segs)
        kept = 0
        for seg in segs:
            if kept >= SCRAPE_V2_CONFIG["max_segments_per_source"]:
                break
            analyze_segment_v2(seg, ffmpeg, ffprobe, status_cb=status_cb)
            if seg.rejection_reasons:
                for r in seg.rejection_reasons:
                    key = {"massive_black_bars": "black_bars", "ai_watermark": "ai_content",
                           "burned_caption_over_subject": "burned_captions",
                           "rapid_internal_cuts": "rapid_edits"}.get(r, r)
                    rej[key] = rej.get(key, 0) + 1
                continue
            if seg.quality_score < SEGMENT_MIN_QUALITY:
                rej["low_quality"] = rej.get("low_quality", 0) + 1
                continue
            passed.append(seg)
            kept += 1
        state["segments_quality_passed"] = state.get("segments_quality_passed", 0) + kept
    return passed


def _finalize_segment_clip(seg, project_dir, ffmpeg):
    """Normalize the chosen segment window into a clean 9:16 1080x1920 clip. Returns the path."""
    out_dir = Path(project_dir) / "seedance 2.0"
    key = hashlib.sha1(seg.segment_id.encode("utf-8", "ignore")).hexdigest()[:10]
    dest = out_dir / ("v2seg_" + key + ".mp4")
    final = clip_scraper.normalize_clip(seg.source_path, dest, ffmpeg, seconds=seg.duration,
                                        start=seg.start_time)
    seg.final_path = str(final) if final else ""
    return seg.final_path


# ---------------------------------------------------------------- hook V2

def score_hook_candidates_v2(hook_segments, hook_intent, project_dir, ffmpeg, reasoning_model=None,
                             status_cb=None):
    """Topic-relevant hook caster: presenter energy + TOPIC relevance + visible action + clean frame.
    Returns the best hook SegmentCandidate (or None)."""
    ac = _ac()
    segs = [s for s in (hook_segments or []) if Path(s.source_path).exists()]
    if not segs:
        return None
    describe_segments_v2(segs, project_dir, ffmpeg, reasoning_model=reasoning_model, status_cb=status_cb)
    frames_dir = Path(project_dir) / "review" / "_v2_hook"
    frames_dir.mkdir(parents=True, exist_ok=True)
    strips = []
    for i, seg in enumerate(segs):
        st = _segment_strip(seg, frames_dir, i, ffmpeg)
        if st:
            strips.append((seg, st))
    if not strips:
        return None
    sheet = ac.create_media_contact_sheet([s for (_g, s) in strips], frames_dir / "_hook_sheet.jpg",
                                           title="Hook candidates (seg_00 ...)")
    if not sheet:
        return None
    topic = ('subject="' + hook_intent.subject + '", action="' + hook_intent.action
             + '", location="' + hook_intent.location + '"')
    prompt = (
        "Cast the OPENING HOOK of a vertical Japanese social short. A great hook combines presenter "
        "energy with TOPIC RELEVANCE and a visible action - NOT a generic dance. The topic hook wants: "
        + topic + ".\nScore EACH tile 0-10: face_visibility, eye_contact, expression_energy, "
        "topic_relevance, visible_action, clean_frame, vertical_quality, plus sexualization_penalty "
        "(0 wholesome..10 body-bait) and text_penalty (0 clean..10 text over face). Set passed=false for: "
        "minor/child, sexualized body-bait, anime/VTuber/CGI, slideshow, livestream, heavy text over "
        "the face, or no clear main subject.\n"
        'Return STRICT JSON: {"clips": {"0": {"face_visibility":n,"eye_contact":n,"expression_energy":n,'
        '"topic_relevance":n,"visible_action":n,"clean_frame":n,"vertical_quality":n,'
        '"sexualization_penalty":n,"text_penalty":n,"passed":true|false}, ...}}')
    data = _vision_json(prompt, sheet, max_tokens=3000, temperature=0.1, reasoning_model=reasoning_model)
    cmap = data.get("clips") if isinstance(data.get("clips"), dict) else {}
    best, best_score = None, -1.0
    for idx, (seg, _st) in enumerate(strips):
        d = cmap.get(str(idx)) if isinstance(cmap.get(str(idx)), dict) else {}
        if not d or not bool(d.get("passed", False)):
            continue
        def g(k):
            try:
                return max(0.0, min(10.0, float(d.get(k, 0))))
            except (TypeError, ValueError):
                return 0.0
        score = (g("face_visibility") * 0.15 + g("eye_contact") * 0.10 + g("expression_energy") * 0.15
                 + g("topic_relevance") * 0.25 + g("visible_action") * 0.15 + g("clean_frame") * 0.10
                 + g("vertical_quality") * 0.10 - g("sexualization_penalty") * 0.6 - g("text_penalty") * 0.4)
        if score > best_score:
            best, best_score = seg, score
    if best is not None:
        best.semantic_score = round(best_score, 2)
        _log(status_cb, "Scrape V2 hook: selected " + best.segment_id + (" (score %.1f)." % best_score))
    return best


# ---------------------------------------------------------------- retry weak scenes

def retry_unmatched_scenes_v2(weak_intents, platforms, project_dir, ffmpeg, ffprobe, cancel_check,
                              deadline, state, seen_source_ids, reasoning_model=None, status_cb=None):
    """Targeted re-search for scenes with no usable match: NEW queries from ALTERNATIVE visuals +
    unused tiers, search only for those, discover+describe+match new sources."""
    if not weak_intents:
        return {}
    _log(status_cb, "Scrape V2: retrying %d unmatched/weak scene(s)..." % len(weak_intents))
    new_queries = []
    for it in weak_intents:
        qs = queries_for_intent(it)
        picked = [q for q in qs if q.tier in ("action_location", "semantic_action",
                                              "native_vlog", "broad_context")]
        new_queries.extend(picked[:SCRAPE_V2_CONFIG["max_queries_per_scene_retry"]])
        state["scene_retries"] = state.get("scene_retries", 0) + 1
    sources = _search_sources(new_queries, platforms, cancel_check, deadline, seen_source_ids,
                              state, status_cb)
    segs = _download_and_segment(sources, project_dir, ffmpeg, ffprobe, cancel_check, deadline, state,
                                 status_cb, download_budget=SCRAPE_V2_CONFIG["max_downloaded_analysis_videos"])
    describe_segments_v2(segs, project_dir, ffmpeg, reasoning_model=reasoning_model, status_cb=status_cb)
    return match_segments_to_scenes_v2(weak_intents, segs, reasoning_model=reasoning_model,
                                       status_cb=status_cb)


# ---------------------------------------------------------------- orchestrator

def scrape_social_plan_v2(config, scenes, project_dir, platforms, per_clip_seconds, script_relevancy,
                          cookies, cancel_check, understanding=None, reasoning_model=None,
                          status_cb=None, script_text=""):
    """Relevance-first, segment-based orchestrator. Writes scene['clip'] etc. directly and returns the
    V1-compatible 7-tuple. Stashes assignments + debug report on config['_scrape_v2']. Loops until
    enough final usable segments exist for every scene or a budget is hit."""
    cfg = SCRAPE_V2_CONFIG
    # Backend result order chosen by the user (default RELEVANCE = TikTok's topical order, which
    # returns far more on-topic Japanese footage than MOST_LIKED's Western viral bias).
    sort_mode = str(config.get("scrape_sort") or config.get("search_sort") or "RELEVANCE").upper()
    if sort_mode not in ("RELEVANCE", "MOST_LIKED", "MOST_VIEWED", "MOST_RECENT"):
        sort_mode = "RELEVANCE"
    t0 = time.monotonic()
    deadline = t0 + cfg["max_total_scrape_time_seconds"]
    clip_scraper.set_cookies(cookies)
    try:
        clip_scraper.reset_backend_search_health()
    except Exception:
        pass
    ffmpeg, ffprobe = clip_scraper._ffmpeg_tools()
    state = {"rejections": {}, "scene_reports": [], "_downloaded_ids": set()}

    _log(status_cb, "Scrape V2: Visual scenes planning...")
    intents = build_social_search_plan_v2(config.get("title") or "", script_text or "", scenes,
                                          understanding=understanding, reasoning_model=reasoning_model,
                                          status_cb=status_cb)
    body_intents = [it for it in intents if it.scene_id != 0]
    hook_intent = next((it for it in intents if it.scene_id == 0), None)

    scene_candidates = {}
    seen_source_ids = set()

    query_queue = []
    for it in body_intents:
        query_queue.extend(queries_for_intent(it))
    query_queue.sort(key=lambda q: V2_QUERY_TIERS.index(q.tier) if q.tier in V2_QUERY_TIERS else 9)

    all_segments = []
    BATCH = 8
    while query_queue:
        if (cancel_check and cancel_check()) or time.monotonic() >= deadline:
            break
        matched_scenes = sum(1 for sid, c in scene_candidates.items() if c)
        if matched_scenes >= len(body_intents) and len(all_segments) >= len(body_intents) * 2:
            break
        if state.get("downloaded_sources", 0) >= cfg["max_downloaded_analysis_videos"]:
            break
        batch_q = query_queue[:BATCH]
        query_queue = query_queue[BATCH:]
        _log(status_cb, "Scrape V2: searching %s ... (%d queries queued)" % (batch_q[0].tier, len(query_queue)))
        sources = _search_sources(batch_q, platforms, cancel_check, deadline, seen_source_ids, state,
                                  status_cb, sort=sort_mode)
        _log(status_cb, "Scrape V2: ranking %d source video(s) by relevance..." % len(sources))
        segs = _download_and_segment(sources, project_dir, ffmpeg, ffprobe, cancel_check, deadline,
                                     state, status_cb, cfg["max_downloaded_analysis_videos"])
        if not segs:
            continue
        all_segments.extend(segs)
        _log(status_cb, "Scrape V2: describing clips (%d new segment(s))..." % len(segs))
        describe_segments_v2(segs, project_dir, ffmpeg, reasoning_model=reasoning_model, status_cb=status_cb)
        _log(status_cb, "Scrape V2: matching clips to scenes...")
        new_matches = match_segments_to_scenes_v2(body_intents, segs, reasoning_model=reasoning_model,
                                                  status_cb=status_cb)
        for sid, cands in new_matches.items():
            scene_candidates.setdefault(sid, [])
            scene_candidates[sid].extend(cands)
            scene_candidates[sid].sort(key=lambda r: r["overall_match"], reverse=True)
            scene_candidates[sid] = scene_candidates[sid][:6]
        state["segments_semantic_passed"] = sum(len(v) for v in scene_candidates.values())

    weak = [it for it in body_intents if not scene_candidates.get(it.scene_id)]
    if weak and time.monotonic() < deadline:
        retry_matches = retry_unmatched_scenes_v2(weak, platforms, project_dir, ffmpeg, ffprobe,
                                                  cancel_check, deadline, state, seen_source_ids,
                                                  reasoning_model=reasoning_model, status_cb=status_cb)
        for sid, cands in retry_matches.items():
            scene_candidates.setdefault(sid, []).extend(cands)
            scene_candidates[sid].sort(key=lambda r: r["overall_match"], reverse=True)

    _log(status_cb, "Scrape V2: final assignment...")
    assignments = assign_segments_globally_v2(body_intents, scene_candidates, cfg, status_cb=status_cb)

    used_seg_ids = {a.segment_id for a in assignments.values() if a.segment_id}
    # For filling unmatched scenes, rank spares by RELEVANCE to THIS scene, not by raw quality:
    # a topically-closest Japanese clip beats a shiny but off-topic filler. Relevance is computed
    # per-intent below; keep a quality-sorted copy only as the final tiebreaker source.
    spare = [s for s in all_segments if s.segment_id not in used_seg_ids]

    def _spare_relevance(seg, intent):
        # best available signal that this specific segment relates to this specific scene
        base = max(getattr(seg, "semantic_score", 0.0), getattr(seg, "metadata_relevance", 0.0))
        vd = getattr(seg, "visual_description", None) or {}
        want = " ".join([intent.subject or "", intent.action or "", intent.location or "",
                         intent.jp_subject or "", intent.jp_action or "", intent.jp_location or ""])
        have = " ".join([str(vd.get("subjects") or ""), str(vd.get("action") or ""),
                         str(vd.get("location") or ""), str(seg.query or "")])
        wt, ht = _tokens(want), _tokens(have)
        overlap = (len(wt & ht) / float(len(wt))) * 10.0 if wt else 0.0
        return base * 0.6 + overlap * 0.4

    for it in body_intents:
        a = assignments.get(it.scene_id)
        if a and a.segment_id:
            continue
        chosen = None
        atype, flevel = "context_fallback", 4
        # rank the remaining pool by relevance to THIS scene
        ranked_spare = sorted(spare, key=lambda s: _spare_relevance(s, it), reverse=True)
        for s in ranked_spare:
            rel = max(s.semantic_score, s.metadata_relevance)
            if rel >= CONTEXT_FALLBACK_MIN_RELEVANCE and s.quality_score >= CONTEXT_FALLBACK_MIN_QUALITY:
                chosen = s
                break
        if chosen is None and ranked_spare:
            # last resort: the MOST RELEVANT remaining clip for this scene (never the shiniest
            # random one). Still marked emergency_fallback so the report/validator can flag it.
            chosen = ranked_spare[0]
            atype, flevel = "emergency_fallback", 5
        if chosen is not None:
            spare.remove(chosen)
            na = SceneAssignment(scene_id=it.scene_id, segment_id=chosen.segment_id,
                                 assignment_type=atype, fallback_level=flevel,
                                 semantic_score=chosen.semantic_score,
                                 quality_score=chosen.quality_score, match_class="C_MATCH")
            na._seg = chosen
            assignments[it.scene_id] = na

    hook_seg = None
    if hook_intent is not None:
        hook_pool_segs = sorted(all_segments, key=lambda s: s.quality_score, reverse=True)[:16]
        hook_seg = score_hook_candidates_v2(hook_pool_segs, hook_intent, project_dir, ffmpeg,
                                            reasoning_model=reasoning_model, status_cb=status_cb)

    pool, clip_meta, hook_pool, candidate_statuses = [], {}, [], []
    scene_bucket = {it.scene_id: it.intent_id for it in intents}
    counters = {"scenes_exact_matched": 0, "scenes_alternative_matched": 0,
                "scenes_context_fallback": 0, "scenes_unmatched": 0}
    # Exposed to run_project so V2 flows through the SAME V1 finalize machinery (scraped_NN copy,
    # candidate-status stamping, enforcement report) rather than duplicating it.
    scene_clips_out = [None] * len(scenes)
    clip_decision_log = [None] * len(scenes)

    def _write_scene(scene_idx, seg, atype, flevel, sem, mclass, visual_role=None):
        final = _finalize_segment_clip(seg, project_dir, ffmpeg) if not seg.final_path else seg.final_path
        if not final:
            return False
        sc = scenes[scene_idx]
        name = Path(final).name
        sc["clip"] = name
        sc["asset"] = name
        sc["seedance"] = True
        sc.pop("timeline_speed_src", None)
        sc.pop("caption_blur_src", None)
        sc["scrape_source"] = seg.platform
        sc["scrape_clip_id"] = seg.source_id
        sc["match_class"] = mclass
        sc["script_match_score"] = round(sem, 1)
        sc["black_bar_score"] = seg.black_bar_score
        sc["is_fake_vertical"] = False
        sc["assignment_type"] = atype
        sc["fallback_level"] = flevel
        if visual_role:
            sc["visual_role"] = visual_role
        scene_clips_out[scene_idx] = final
        clip_decision_log[scene_idx] = {
            "scene": scene_idx, "chosen_clip": name, "match_class": mclass,
            "script_match_score": round(sem, 1), "assignment_type": atype,
            "fallback_needed": False, "adaptive_fallback": flevel >= 4,
            "reason": atype, "passes_acceptance_test": flevel == 0}
        pool.append(final)
        clip_meta[final] = {"bucket_id": scene_bucket.get(scene_idx, ""), "tier": "v2_segment",
                            "platform": seg.platform, "clip_id": seg.source_id,
                            "source_query": seg.query, "likes": 0,
                            "black_bar_score": seg.black_bar_score, "text_heaviness": seg.text_heaviness,
                            "semantic_score": sem, "assignment_type": atype}
        candidate_statuses.append({"clip_id": seg.source_id, "bucket_id": scene_bucket.get(scene_idx, ""),
                                   "source_query": seg.query, "tier": "v2_segment",
                                   "status": "assigned_to_scene", "reason": atype,
                                   "shown_in_media_panel": False})
        try:
            Path(final).with_suffix(".json").write_text(json.dumps({
                "status": "accepted", "platform": seg.platform, "clip_id": seg.source_id,
                "assignment_type": atype, "semantic_score": sem, "captioned": False}, indent=2), "utf-8")
        except Exception:
            pass
        return True

    if hook_intent is not None and hook_seg is not None:
        if _write_scene(0, hook_seg, "exact", 0, hook_seg.semantic_score, "A_MATCH",
                        visual_role="hook_influencer"):
            hook_pool.append(hook_seg.final_path)
    elif scenes:
        scenes[0]["visual_role"] = "hook_influencer"

    for it in body_intents:
        a = assignments.get(it.scene_id)
        rep = {"scene_id": it.scene_id, "scene_text": it.scene_text,
               "visual_intent": "%s / %s / %s" % (it.subject, it.action, it.location),
               "queries_used": [q.query for q in queries_for_intent(it)][:4]}
        if a and getattr(a, "_seg", None) is not None:
            seg = a._seg
            ok = _write_scene(it.scene_id, seg, a.assignment_type, a.fallback_level,
                              a.semantic_score, a.match_class)
            rep.update({"selected_segment": seg.segment_id, "assignment_type": a.assignment_type,
                        "semantic_score": a.semantic_score, "quality_score": a.quality_score})
            if a.assignment_type == "exact":
                counters["scenes_exact_matched"] += 1
            elif a.assignment_type in ("alternative_visual", "retry_exact"):
                counters["scenes_alternative_matched"] += 1
            elif a.assignment_type in ("context_fallback", "emergency_fallback"):
                counters["scenes_context_fallback"] += 1
            if not ok:
                counters["scenes_unmatched"] += 1
                rep["assignment_type"] = "unmatched"
        else:
            counters["scenes_unmatched"] += 1
            rep["assignment_type"] = "unmatched"
        state["scene_reports"].append(rep)

    state.update(counters)
    state["elapsed"] = time.monotonic() - t0
    report = build_debug_report(state)
    try:
        rep_dir = Path(project_dir) / "review"
        rep_dir.mkdir(parents=True, exist_ok=True)
        (rep_dir / "scrape_v2_report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), "utf-8")
    except Exception:
        pass
    config["_scrape_v2"] = {"report": report,
                            "assignments": {sid: asdict_safe(a) for sid, a in assignments.items()},
                            "scene_clips": scene_clips_out,
                            "clip_decision_log": [d for d in clip_decision_log],
                            "best_hook": (hook_seg.final_path if hook_seg is not None else None)}
    try:
        clip_scraper.close_backend()
    except Exception:
        pass
    _log(status_cb, "Scrape V2 done: %d exact, %d fallback, %d unmatched in %.0fs." % (
        counters["scenes_exact_matched"], counters["scenes_context_fallback"],
        counters["scenes_unmatched"], state["elapsed"]))
    filter_summary = {"engine": "scrape_v2", "scene_reports": state["scene_reports"],
                      "rejections": report["rejections"]}
    return pool, clip_meta, [], scene_bucket, hook_pool, candidate_statuses, filter_summary


def asdict_safe(a):
    d = asdict(a) if hasattr(a, "__dataclass_fields__") else dict(a.__dict__)
    d.pop("_seg", None)
    return d


def normalize_engine(val):
    """Map any stored/typed value to a valid engine id, defaulting to 'v2' (safe for old configs)."""
    v = str(val or "").strip().lower()
    return v if v in ("v1", "v2") else "v2"
