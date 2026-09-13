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

import concurrent.futures
import copy
import hashlib
import json
import math
import re
import shutil
import subprocess
import time
import urllib.request
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

import clip_scraper                      # V1 primitives (no cycle: clip_scraper never imports us)
import pipeline                          # shared render/cadence helpers; pipeline does not import scrape_v2
import reasoning_modes                   # model capability table (vision vs text-only)

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

PROXY_FETCH_WORKERS = 6   # proxy downloads are network wait; see _download_and_segment
V2_ANALYSIS_VERSION = 5   # v5 adds search-context vocabulary plus an explicit footage-vs-caption
                          # contract, so older generic/over-restrictive descriptions are never
                          # reused to reject visually correct clips during a resumed test run

# A beat that clears no floor does not go blank - it replays a neighbour's clip. Below
# this score a segment really is unrelated and a repeat is the lesser evil; above it, the
# clip at least shows what the line is talking about.
NEAR_MISS_FLOOR = 4.5

# A beat may be covered by a clip that is slightly SHORTER than the narration line, played a
# little slower. Measured on the dating run: the only train shot in a 92-segment pool was 2.97s
# against a 3.26s beat - an 8% shortfall - so the beat was dropped to a still image while the
# perfect clip sat unused. 0.88 is 12% short, i.e. 0.88x playback; below that b-roll starts to
# read as slow motion. The renderer already retimes properly (setpts + motion interpolation).
SHORT_CLIP_STRETCH_FLOOR = 0.88

# The score a beat's BEST candidate has to reach before the search is allowed to stop early.
# Measured across runs: the ones that kept looking landed beats at 7.0-9.2, the one that stopped
# as soon as every beat held something came out at 5.05-7.10 having spent a third of its clock.
# Below this the run keeps searching while time and escalation stages remain; it never blocks a
# render, because whatever exists when the clock runs out is still used.
GOOD_ENOUGH_MATCH = 7.0

SCRAPE_V2_CONFIG = {
    # COST (user 2026-07-22: "$2 LLM pro Mini-Run, was soll das"): the old budgets let one
    # 25s mini short download 65 sources and push 174 segments through paid vision. Halved.
    "max_total_source_videos": 120,       # metadata candidates considered overall
    "max_downloaded_analysis_videos": 36,
    # ...but 36 is a cap for a short script. A 14-beat fact short splitting it FIFO gave the
    # first two beats everything and the other twelve nothing, so the pool scales with the
    # beat count and every beat is guaranteed this many downloads of its own.
    # Three downloads per chapter sounds reasonable on paper, but one dead URL plus one intro/
    # slideshow leaves a chapter with a single real option.  The hand-made edits that consistently
    # beat the automated run inspect several uploads before choosing a shot.  Give every chapter a
    # six-source first-look pool; this is still bounded and the search stops once it has real choice.
    "min_downloads_per_scene": 6,
    "min_distinct_matches_per_scene": 3,
    "max_final_segments": 90,
    "max_queries_per_bucket": 12,
    # Manual sourcing consistently wins with three deliberate searches: native object, native
    # action, then one English discovery phrase.  Large per-scene query trees drift into vibes and
    # spend minutes re-sorting the same weak neighbourhood.
    "max_initial_queries_per_chapter": 3,
    "max_queries_per_scene_retry": 6,
    "max_bucket_time_seconds": 900,
    # THE CLOCK MUST NOT BE THE BINDING CONSTRAINT (user 2026-08-20: "die Uhr soll nicht mehr
    # limitieren"). Measured: both goal runs ended at ~2010s against a 1800s budget with beats
    # still empty - the run was stopped mid-recovery, not because it had run out of things to
    # try. The loop already exits as soon as every beat has editorial choice, so a larger budget
    # costs nothing on a script whose footage exists and buys the recovery rounds on one whose
    # footage is scarce. This is a backstop against a runaway, not a pacing target.
    "max_total_scrape_time_seconds": 7200,
    "vision_batch_size": 8,
    "max_segments_per_source": 3,
    # Visual chapters are intentionally longer than narration micro-beats.  Search for a coherent
    # phone-video action that can hold the thought instead of forcing a fresh filler every 2s.
    "target_segment_seconds": 6.5,
    "min_segment_seconds": 2.2,
    "max_segment_seconds": 8.5,
    "proxy_max_height": 720,
    "proxy_max_filesize_mb": 60,
    # assignment diversity / reuse caps
    "max_platform_share": 0.85,
    "max_clips_per_creator": 2,
    "max_clips_per_query": 3,
    "max_near_duplicates_per_visual": 2,
    # STRICT (user rule 2026-07-13): the SAME source TikTok/video may appear AT MOST ONCE in the
    # final render - never a second window/excerpt of it. Everything else (more searching, more
    # query terms) must be tried before a source would be reused. 1 = one clip per source.
    "max_segments_per_source_final": 1,
    # ...but a script can legitimately spend several beats on ONE process: "you order at the
    # machine / you pay / you hand the ticket to the staff" is three beats of the same thing, and
    # the rule above then demands three DIFFERENT uploads of it. A real editor takes one good
    # thirty-second video and cuts three moments out of it - that is continuity, not repetition.
    # A second window is allowed ONLY for a beat about the SAME subject, and only when it is a
    # different, non-overlapping, visually different moment. Unrelated beats still get 1.
    "max_segments_per_source_same_topic": 2,
    # seconds two windows of one source must be apart before they read as different moments
    "same_source_window_gap_seconds": 2.0,
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
# TIGHTENED 2026-07-22 ("material das 0 mit dem voiceover zu tun hat"): the abstract/context
# floors let a key-hook DIY tutorial score 7/10 on "erase their existence overnight" because the
# matcher rewarded the METAPHOR. Floors up; the matcher prompt must score the LITERAL on-screen
# subject, not a poetic connection.
MATCH_THRESHOLDS_V2 = {
    "concrete": {"script_floor": 5.8, "overall": 6.5},
    "context":  {"script_floor": 5.5, "overall": 6.5},
    "abstract": {"script_floor": 5.2, "overall": 6.3},
    # shock/meme scenes: the visual punchline may ignore the script (that IS the joke), but the
    # segment must actually be absurd (absurdity gate enforced in the matcher loop)
    "shock":    {"script_floor": 4.0, "overall": 5.8},
}

# segment quality: only genuinely unusable material is a HARD reject; everything else scores soft.
SEGMENT_HARD_REJECT_BLACKBAR = 6.0        # black_bar_score above this = massive letterbox
SEGMENT_MIN_QUALITY = 6.3                 # preferred quality for final assignment
# Do not discard a technically usable, topic-relevant segment solely because its soft
# quality score is below the preferred bar. Vision matching can still select it when it
# is a better literal match than a polished but unrelated clip.
SEGMENT_SOFT_MIN_QUALITY = 4.5
CONTEXT_FALLBACK_MIN_RELEVANCE = 5.7
CONTEXT_FALLBACK_MIN_QUALITY = 5.8


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
    english_queries: list = field(default_factory=list)
    japanese_queries: list = field(default_factory=list)
    # Explicit executable terms by platform. Shape:
    # {"tiktok": {"japanese": [], "english": []},
    #  "instagram": {"keywords": [], "hashtags": []}}
    platform_queries: dict = field(default_factory=dict)


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
    # Architect Agent output: raw, directly executable platform search strings.
    match_category: str = "vibe"       # literal | vibe | shock
    english_queries: list = field(default_factory=list)
    japanese_queries: list = field(default_factory=list)
    # What the picture contributes to comprehension. This prevents a blanket "never objects"
    # rule from rejecting indispensable proof/demonstration shots such as four sweets or a bow.
    communication_role: str = "human_consequence"  # proof|demonstration|human_consequence|emotion|pattern_interrupt
    story_subject: str = ""       # recurring subject of the WHOLE script
    local_claim: str = ""         # what this exact narration beat says about that subject
    platform_queries: dict = field(default_factory=dict)

    @property
    def intent_id(self) -> str:
        return f"scene_{self.scene_id}"


@dataclass
class SearchQueryV2:
    query: str
    language: str          # "ja" | "en"
    tier: str
    visual_intent_id: str
    scene_ids: list = field(default_factory=list)
    query_type: str = "subject_action_location"
    generated_from: str = "scene_visual_intent"
    reason: str = ""
    expected_subject: str = ""
    expected_action: str = ""
    expected_location: str = ""
    negative_terms: list = field(default_factory=list)
    # Empty = legacy query may run everywhere. Otherwise execute only on these backends.
    platforms: list = field(default_factory=list)
    # Japan-specific stories must be researched from native-language source neighbourhoods.
    # This is provenance, not an attempt to infer nationality from a person's appearance.
    requires_japanese_context: bool = False


def _query_identity(query):
    """Dedupe the same text per backend, not globally across incompatible search surfaces."""
    scoped = tuple(sorted({_canonical_platform(value) for value in (query.platforms or [])}))
    return (scoped or ("*",), sanitize_platform_query(query.query).casefold())


@dataclass
class SourceVideoCandidate:
    platform: str
    source_id: str
    creator_id: str
    url: str
    caption: str = ""
    hashtags: list = field(default_factory=list)
    likes: int = 0
    views: int = 0
    duration: float = 0.0
    width: int = 0
    height: int = 0
    query: str = ""
    query_tier: str = ""
    scene_ids: list = field(default_factory=list)
    metadata_relevance: float = 0.0
    rank_score: float = 0.0
    raw_item: dict = field(default_factory=dict)   # original backend item for yt-dlp download
    japanese_context: bool = False


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
    scene_ids: list = field(default_factory=list)
    # quality (segment-level, soft)
    quality_score: float = 0.0
    raw_footage_score: float = 0.0
    edit_stability_score: float = 0.0
    text_heaviness: float = 0.0
    caption_probability: float = 0.0
    black_bar_score: float = 0.0
    vertical_quality: float = 0.0
    source_width: int = 0
    source_height: int = 0
    native_9_16: bool = False
    frozen_run_seconds: float = 0.0
    motion_score: float = 0.0        # 0 = a held photograph, see _motion_score
    cleaned_path: str = ""           # blurred copy, when the captions were removable
    caption_over_subject: bool = False   # a penalty now, not a disqualifier
    cut_scan_failed: bool = False        # the cut scan gave no answer; NOT "no cuts"
    micro_stutter_count: int = 0
    # semantics (filled by vision)
    semantic_score: float = 0.0
    visual_description: dict = field(default_factory=dict)
    # bookkeeping
    metadata_relevance: float = 0.0
    frame_hash: str = ""
    rejection_reasons: list = field(default_factory=list)
    quality_gate: str = "preferred"  # preferred|soft; hard technical rejects never reach matching
    final_path: str = ""    # high-quality normalized 9:16 clip once accepted
    japanese_context: bool = False


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


def _slog(project_dir, entry):
    """Append one entry to the USER-FACING scrape log (review/scrape_log.json). Written live
    during the run so the Log view can be opened mid-scrape. Best-effort, never raises."""
    try:
        path = Path(project_dir) / "review" / "scrape_log.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = []
        if path.exists():
            try:
                rows = json.loads(path.read_text(encoding="utf-8")) or []
            except Exception:
                rows = []
        entry = dict(entry)
        entry["at"] = time.strftime("%H:%M:%S")
        rows.append(entry)
        path.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass


def _slog_thumbs(seg, project_dir, ffmpeg, n=3):
    """Extract up to n small thumbnails from a segment for the scrape log. Returns rel paths."""
    out = []
    try:
        src = seg.final_path or seg.source_path
        if not src or not Path(src).exists():
            return out
        tdir = Path(project_dir) / "review" / "_scrape_log"
        tdir.mkdir(parents=True, exist_ok=True)
        dur = max(0.4, float(seg.duration or 2.0))
        base = hashlib.sha1(str(seg.segment_id).encode("utf-8", "ignore")).hexdigest()[:10]
        offs = ([dur * 0.15, dur * 0.5, dur * 0.85][:n]
                if not seg.final_path else [dur * (i + 1) / (n + 1) for i in range(n)])
        start = 0.0 if seg.final_path else float(seg.start_time or 0.0)
        for i, off in enumerate(offs):
            dst = tdir / f"{base}_{i}.jpg"
            if not dst.exists():
                subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-ss", f"{start + off:.2f}",
                                "-i", str(src), "-frames:v", "1",
                                "-vf", "scale=180:-2,format=yuvj420p", "-q:v", "6", str(dst)],
                               capture_output=True, timeout=30)
            if dst.exists():
                out.append(str(dst))
    except Exception:
        pass
    return out


def _accepted_poster(seg, project_dir, ffmpeg, start=0.0):
    """One browser-safe JPEG frame of an accepted clip for the live 'last accepted' preview.
    The raw proxies are often HEVC (won't play in Chromium/WebView2), so the UI shows this."""
    try:
        src = seg.final_path or seg.source_path
        if not src or not ffmpeg or not Path(src).exists():
            return ""
        pdir = Path(project_dir) / "review" / "_accepted"
        pdir.mkdir(parents=True, exist_ok=True)
        base = hashlib.sha1(str(seg.segment_id).encode("utf-8", "ignore")).hexdigest()[:10]
        dst = pdir / f"{base}.jpg"
        at = max(0.0, float(start)) + max(0.3, float(seg.duration or 2.0) * 0.4)
        subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-ss", f"{at:.2f}",
                        "-i", str(src), "-frames:v", "1",
                        "-vf", "scale=360:-2,format=yuvj420p", "-q:v", "5", str(dst)],
                       capture_output=True, timeout=30)
        return str(dst) if dst.exists() else ""
    except Exception:
        return ""


def _llm_json(messages, max_tokens=4000, temperature=0.3, reasoning_model=None,
              status_cb=None, label="LLM"):
    """Text-only LLM call returning a parsed JSON dict (or {}).

    NEVER fail silently: a swallowed matcher error used to collapse a whole run into
    emergency fallbacks with no trace (every scene filled with random off-topic clips).

    ALWAYS a dict, even when the model ignores the requested envelope and answers with a
    bare JSON array - every caller here does data.get(...), so a list killed the whole run
    with "'list' object has no attribute 'get'" (it killed one live). A bare array is kept
    under __rows__ so callers that want a list can still find it.
    """
    ac = _ac()
    try:
        data = ac._post_llm_json(reasoning_model or ac.GPT55_MODEL, messages,
                                 max_tokens, temperature)
    except Exception as exc:
        _log(status_cb, "Scrape V2: %s call FAILED (%s: %s) - continuing without its result."
             % (label, exc.__class__.__name__, str(exc)[:160]))
        return {}
    if isinstance(data, dict):
        return data
    if isinstance(data, list):
        return {"__rows__": data}
    return {}


def _scene_candidate_map(data):
    """scene id -> candidate list, out of whatever shape the matcher answered with.

    The matcher is asked for {"scenes": {"1": [...]}} and only that shape was accepted.
    Everything else - a LIST of per-scene objects, the map without its wrapper, a bare
    array - was silently dropped, which set every segment's score to 0 for every scene.
    On a live run that produced "matcher total 0/14 scene(s)" and looked exactly like
    footage that did not fit the script, which is the wrong thing to go and fix.
    """
    if not isinstance(data, (dict, list)):
        return {}

    def _rows(value):
        return [r for r in value if isinstance(r, dict)] if isinstance(value, list) else []

    def _from_list(items):
        """[{"scene": 1, "candidates": [...]}, ...] -> {"1": [...]}"""
        out = {}
        for row in _rows(items):
            sid = None
            for key in ("scene", "scene_id", "id", "scene_number", "index"):
                if row.get(key) is not None:
                    sid = row.get(key)
                    break
            cands = None
            for key in ("candidates", "segments", "matches", "results", "items"):
                if isinstance(row.get(key), list):
                    cands = row[key]
                    break
            if sid is not None and cands is not None:
                out.setdefault(str(sid), []).extend(_rows(cands))
        return out

    if isinstance(data, list):
        return _from_list(data)
    scenes = data.get("scenes")
    if isinstance(scenes, dict):
        return scenes
    if isinstance(scenes, list):
        return _from_list(scenes)
    if isinstance(data.get("__rows__"), list):          # the model answered a bare array
        return _from_list(data["__rows__"])
    # the map itself, without its wrapper: {"0": [...], "1": [...]}
    digitish = {k: v for k, v in data.items()
                if isinstance(v, list) and re.search(r"\d", str(k))}
    if digitish:
        return digitish
    return {}


def _rows_of(data, key):
    """The list a caller asked for, whichever shape the model wrapped it in."""
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if not isinstance(data, dict):
        return []
    for candidate in (data.get(key), data.get("__rows__")):
        if isinstance(candidate, list):
            return [r for r in candidate if isinstance(r, dict)]
    # last resort: the model renamed the key but the payload is unmistakable
    for value in data.values():
        if (isinstance(value, list) and value
                and all(isinstance(r, dict) for r in value)
                and any(("scene_id" in r or "subject" in r) for r in value)):
            return list(value)
    return []


def _vision_json(prompt_text, sheet_path, max_tokens=4000, temperature=0.1, reasoning_model=None):
    """Vision LLM call over one contact sheet, returning a parsed JSON dict (or {})."""
    ac = _ac()
    try:
        data = ac.post_json_url(ac.WAVESPEED_LLM_API, {
            # A text-only pick (DeepSeek) would answer this from imagination, so every clip
            # judgement would be a hallucination; route just the vision call to a model that sees.
            "model": reasoning_modes.vision_model_for(reasoning_model or ac.GPT55_MODEL),
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
    except ac.WaveSpeedBalanceError:
        # An empty account 403s EVERY call. Swallowing that into {} made a discovery run
        # spend 20 minutes rejecting every candidate "by vision review" instead of saying
        # the credit ran out (2026-07-25), so this one error always propagates.
        raise
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


def _relationship_scene_seeds_v2(intent: VisualIntent):
    """Return concrete native searches for a relationship beat, when applicable.

    The Architect is useful for broad ideas, but it repeatedly reduced dating stories to vague
    searches such as ``person breathing deeply``.  Those terms have no reliable relationship
    signal on TikTok.  These are observable actions a human editor would search for instead.
    They supplement, rather than replace, the model's scene-specific plan.
    """
    # THIS beat's own words only. story_subject is the recurring subject of the whole
    # script, so including it made every scene of a couples script look like a couples
    # scene: on a live plan, the beat about photo booths and the beat about love hotels
    # both came out searching "カップル デート vlog", and scenes 1-3 ended up with
    # identical query sets while the word プリクラ never appeared once.
    text = " ".join(str(getattr(intent, key, "") or "") for key in (
        "scene_text", "local_claim", "subject", "action", "location",
    )).casefold()
    relationship_markers = (
        "girlfriend", "boyfriend", "couple", "dating", "date", "romance", "romantic",
        "confession", "kokuhaku", "hold hands", "relationship", "classmates", "恋", "告白",
    )
    def _has_marker(marker):
        # Word boundaries matter for short English markers: "dates and locations" in a card-
        # collecting script is not a dating scene. Native text and multi-word actions still use
        # direct containment because Japanese does not have whitespace word boundaries.
        if re.fullmatch(r"[a-z ]+", marker):
            return bool(re.search(r"(?<![a-z])" + re.escape(marker) + r"(?![a-z])", text))
        return marker in text

    if not any(_has_marker(marker) for marker in relationship_markers):
        return []
    if (("hand" in text and any(marker in text for marker in ("hold", "holding", "holds", "held", "interlock")))
            or "interlocking" in text or "hands in public" in text):
        terms = ["カップル 手繋ぎ", "恋人繋ぎ デート", "手繋ぎ カップル", "放課後 手繋ぎ"]
    elif any(marker in text for marker in ("confession", "kokuhaku", "please go out", "look the other")):
        # TikTok's current search surface has no usable result neighbourhood for the overly
        # specific #告白放課後 combination, while #高校生告白 does.  Start from the proven
        # high-school/confession neighbourhood and let vision reject staged/text-heavy posts.
        terms = ["高校生 告白", "告白", "放課後", "恋愛 あるある"]
    elif any(marker in text for marker in ("social media", "profile status", "sns")):
        terms = ["カップル SNS 公開", "彼氏できた 報告", "恋人 インスタ", "交際報告"]
    elif any(marker in text for marker in ("classmate", "classmates", "classroom", "school")):
        terms = ["放課後 教室 男女", "高校生 放課後", "クラスメイト 男女", "学校 帰り道"]
    elif any(marker in text for marker in ("no physical contact", "strictly friends", "dating limbo", "friends")):
        # #恋人未満デート and #男女カフェ return no items in the production backend; the
        # broader couple-date neighbourhood is populated and can still be vision-matched to the
        # deliberate-distance action.
        terms = ["カップル デート", "恋愛 あるある", "放課後", "カップル 日常"]
    else:
        terms = ["カップル デート vlog", "カップル 日常", "恋人 デート", "放課後 デート"]
    out = []
    for term in terms:
        # Metadata is normally Japanese for native TikTok searches.  Keeping English planner
        # fields here made every #高校生告白 result look nearly identical to the ranker, so it
        # selected engagement/context over the literal action.  Score the two visible Japanese
        # anchors directly, then let vision decide whether the moment is authentic/useful.
        pieces = term.split(maxsplit=1)
        anchor_subject = pieces[0]
        anchor_action = pieces[1] if len(pieces) > 1 else pieces[0]
        out.append(SearchQueryV2(
            query=term, language="ja", tier="relationship_action",
            visual_intent_id=intent.intent_id, scene_ids=[intent.scene_id],
            query_type="relationship_action", generated_from="deterministic_relationship_scene_seed",
            reason="observable Japanese relationship action for this narration beat",
            expected_subject=anchor_subject, expected_action=anchor_action,
            expected_location=intent.location, negative_terms=list(intent.avoid_elements or []),
            platforms=["tiktok"],
        ))
    return out


def queries_for_intent(intent: VisualIntent):
    """Build tiered SearchQueryV2 objects for one visual intent.

    PRIMARY queries are SHORT, native Japanese (1-3 keywords) built from the planner's jp_* fields -
    real TikTok search finds nothing with an 8-word phrase. English phrases are kept only as a low
    backup tier. Every block is length-capped and the relevance-`expected_*` carry the Japanese
    tokens so metadata ranking matches the (Japanese) captions. Diversified per tier."""
    # New Architect plans contain platform-native strings. TikTok, Instagram and X do not index
    # content the same way, so never broadcast an Instagram hashtag or an X proof phrase to every
    # backend. Legacy plans without platform_queries still use the shared arrays below.
    direct = []
    primary_tier = "exact_action" if intent.match_category == "literal" else (
        "semantic_action" if intent.match_category == "vibe" else "creator_style")

    def add_direct(values, language, tier, subject, action, location, platforms=None):
        for raw in values or []:
            native = language == "ja" or _contains_japanese(raw)
            direct.append(SearchQueryV2(
                query=raw, language=language, tier=tier, visual_intent_id=intent.intent_id,
                scene_ids=[intent.scene_id], query_type=tier,
                generated_from="scene_visual_intent",
                reason=f"{subject} / {action} / {location}".strip(" /"),
                # Native captions cannot overlap the Architect's English subject/action labels.
                # Bind metadata ranking to the executable native phrase itself; vision still makes
                # the later semantic decision against the full English intent.
                expected_subject=(raw if native else subject),
                expected_action=(raw if native else action), expected_location=location,
                negative_terms=list(intent.avoid_elements or []), platforms=list(platforms or [])))

    def add_platform_plan(plan, subject, action, location, tier):
        if not isinstance(plan, dict):
            return
        for raw_platform, values in plan.items():
            platform = _canonical_platform(raw_platform)
            if platform not in ("tiktok", "instagram") or not isinstance(values, dict):
                continue
            for language, key in (("ja", "japanese"), ("en", "english")):
                cleaned = _clean_queries_for_platform(platform, values.get(key), language, limit=4)
                for text in cleaned:
                    # The Architect files native strings under its "english" array, so the
                    # key is not evidence of the language. The gate in
                    # build_scene_bound_query_plan reads the text; labelling correctly here
                    # as well keeps the plan audit honest about what it actually sent.
                    add_direct([text], "ja" if _contains_japanese(text) else language,
                               tier, subject, action, location, [platform])
            # Instagram exposes keyword/tag discovery rather than a useful TikTok-style phrase
            # ranking. Keep those two result neighbourhoods explicit.
            if platform == "instagram":
                cleaned = _clean_queries_for_platform(platform, values.get("keywords"), "auto", limit=4)
                add_direct(cleaned, "auto", tier, subject, action, location, [platform])
                tags = _clean_queries_for_platform(platform, values.get("hashtags"), "hashtag", limit=4)
                add_direct(tags, "ja", "hashtag", subject, action, location, [platform])

    # The Architect's own terms go FIRST, the generic relationship seeds behind them.
    #
    # This used to be the other way round, on the reasoning that the coverage wave only runs
    # a scene's first query and so must not spend it on a vague Architect term. The Tokyo
    # run showed the opposite is what happens: the seeds are the vague ones. They are
    # identical for every scene - カップル デート vlog, カップル 日常, 恋人 デート,
    # 放課後 デート - so they filled position 1-4 of all 14 scenes and pushed the terms that
    # actually describe each beat (プリクラ 落書き, ラブホ 自動精算機) to position 5. The run
    # died at query 4. Not one purikura or love-hotel search was ever issued.
    add_platform_plan(intent.platform_queries, intent.subject, intent.action, intent.location,
                      primary_tier)
    direct.extend(_relationship_scene_seeds_v2(intent))
    # Keep obvious, filmable anchors alive even when the Architect turns an intent into
    # abstract dating/attraction language. These are discovery seeds, not translated narration.
    scene_lower = str(intent.scene_text or "").casefold()
    if any(term in scene_lower for term in ("mask", "masks", "face hidden", "above the nose")):
        mask_queries = {
            "tiktok": {
                "japanese": ["マスク 日常", "マスク デート", "マスク 電車", "マスク 学校"],
                "english": ["japan mask", "mask date"],
            },
            "instagram": {
                "keywords": ["日本 マスク", "マスク デート"],
                "hashtags": ["#マスク", "#マスク生活"],
            },
        }
        add_platform_plan(mask_queries, "person wearing a mask", "wearing a mask",
                          "Japan daily life", "anchor_mask")
    for alt in (intent.alternative_visuals or [])[:3]:
        add_platform_plan(alt.platform_queries, alt.subject, alt.action, alt.location,
                          "semantic_action")

    if not direct:
        add_direct(_clean_english_queries(intent.english_queries), "en", primary_tier,
                   intent.subject, intent.action, intent.location)
        add_direct([_cap_tokens(query, 2) for query in _clean_japanese_queries(intent.japanese_queries)], "ja", primary_tier,
                   intent.subject, intent.action, intent.location)
        for alt in (intent.alternative_visuals or [])[:3]:
            add_direct(_clean_english_queries(alt.english_queries, 2), "en", "semantic_action",
                       alt.subject, alt.action, alt.location)
            add_direct([_cap_tokens(query, 2) for query in _clean_japanese_queries(alt.japanese_queries, 2)], "ja", "semantic_action",
                       alt.subject, alt.action, alt.location)
    if direct:
        out, seen = [], set()
        for query in direct:
            key = (tuple(query.platforms), query.language, query.query.casefold())
            if key not in seen:
                seen.add(key)
                out.append(query)
        return out[:SCRAPE_V2_CONFIG["max_queries_per_bucket"]]

    # Backward-compatible fallback for old cached plans that predate raw query arrays.
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
            scene_ids=[intent.scene_id], query_type=tier,
            generated_from="scene_visual_intent",
            reason=f"{exp_s} / {exp_a} / {exp_l}".strip(" /"),
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


_QUERY_CONCEPT_GUARDS = {
    "couple": ("couple", "romance", "dating", "girlfriend", "boyfriend", "カップル", "恋人", "デート"),
    "messaging": ("line", "text message", "texting", "メッセージ", "返信", "既読"),
    "coffee_gift": ("canned coffee", "coffee gift", "缶コーヒー", "差し入れ"),
    "dance_creator": ("dancing", "dance", "idol", "踊ってみた", "ダンス", "アイドル", "女の子"),
}


def _intent_text(intent):
    return " ".join(str(value or "") for value in (
        intent.scene_text, intent.subject, intent.action, intent.location,
        intent.story_subject, intent.local_claim, " ".join(intent.required_elements or []),
        " ".join(intent.optional_elements or []),
        getattr(intent, "jp_subject", ""), getattr(intent, "jp_action", ""),
        getattr(intent, "jp_location", ""),
    )).casefold()


def validate_query_against_intent(query, intent):
    """Reject malformed or foreign-theme searches before they reach a backend.

    This is deliberately deterministic: it protects a run from stale presets and
    planner contamination without charging another LLM call.
    """
    normalized = sanitize_platform_query(query.query)
    if not normalized:
        return False, "empty after normalization", normalized
    # Japanese compounds/hashtags such as 終電難民, 路上寝 or 飲みニケーション are complete,
    # highly specific uploader language even though whitespace tokenisation calls them one word.
    if (query.tier != "hashtag" and len(normalized.split()) < 2
            and not _contains_japanese(normalized)):
        return False, "too general for a scene-bound search", normalized
    if (_contains_japanese(normalized) and len(normalized.split()) < 2
            and any(marker in normalized for marker in ("公園", "駅", "街", "通り", "市", "区"))):
        return False, "too general for a scene-bound search", normalized
    intent_text = _intent_text(intent)
    query_text = normalized.casefold()
    for concept, markers in _QUERY_CONCEPT_GUARDS.items():
        has_query_concept = any(marker in query_text for marker in markers)
        has_intent_concept = any(marker in intent_text for marker in markers)
        if has_query_concept and not has_intent_concept:
            return False, f"unrelated {concept} concept", normalized
    if not _contains_japanese(normalized):
        # English is the expat/viral discovery lane, but it still has to name one tangible part of
        # this chapter. This keeps useful queries such as "Japan manhole card collector" while
        # refusing drift such as "regional Japanese" that contains only a country adjective.
        generic_context = {"japan", "japanese", "tokyo", "osaka", "kyoto", "viral", "tiktok"}
        query_anchors = _tokens(normalized) - generic_context
        intent_anchors = _tokens(" ".join((
            intent.subject or "", intent.action or "", intent.location or "",
            intent.story_subject or "", intent.local_claim or "", intent.scene_text or "")))
        if not (query_anchors & intent_anchors):
            return False, "English query has no tangible scene anchor", normalized
    return True, "", normalized


# A beat's SETTING is load-bearing: the same action in a different place is a different video.
# English location wording -> the native token the platform is actually searched with. Longest
# phrase wins, so "high school cafeteria" anchors on 学食 rather than the generic 高校.
_SETTING_ANCHORS = (
    ("convenience store", "コンビニ"), ("school shop", "購買"), ("school cafeteria", "学食"),
    ("high-school cafeteria", "学食"), ("high school cafeteria", "学食"),
    ("high-school classroom", "教室"), ("high school classroom", "教室"),
    ("classroom", "教室"), ("high-school", "高校"), ("high school", "高校"),
    ("university", "大学"), ("school", "学校"), ("cafeteria", "食堂"),
    ("supermarket", "スーパー"), ("department store", "百貨店"),
    ("station", "駅"), ("platform", "ホーム"), ("train", "電車"),
    ("hospital", "病院"), ("clinic", "病院"), ("office", "オフィス"),
    ("restaurant", "飲食店"), ("cafe", "カフェ"), ("hotel", "ホテル"),
    ("arcade", "ゲームセンター"), ("gym", "体育館"), ("pool", "プール"),
    ("public bathhouse", "銭湯"), ("bathhouse", "銭湯"), ("hot spring", "温泉"), ("onsen", "温泉"),
    ("parking", "駐車場"), ("library", "図書館"), ("museum", "博物館"),
    ("pharmacy", "薬局"), ("post office", "郵便局"), ("bank", "銀行"),
    ("airport", "空港"), ("temple", "寺"), ("shrine", "神社"),
    ("market", "市場"), ("shop", "店"), ("store", "店"),
)


def _setting_anchor_for(location):
    """The native token that names this beat's place, or "" when it names none."""
    text = str(location or "").casefold()
    for phrase, token in _SETTING_ANCHORS:
        if phrase in text:
            return token
    return ""


def _repair_missing_setting_anchor(intents, by_scene, status_cb=None):
    """Put the beat's own setting back into at least one of its queries.

    Measured on the school-lunch run: every beat's location read "Japanese high-school classroom /
    school shop / cafeteria", and the planner emitted 手作り弁当 and おにぎり 昼ごはん with the school
    dropped. To the platform those words mean HOME COOKING, so thirty-five of the sixty-eight
    analysed clips were somebody's kitchen counter and not one was a school. The retries that did
    keep the setting - 高校生 手作り弁当, 教室 弁当 - returned real school footage immediately, but by
    then the download budget was gone. The food is what happens; the place is what makes it the
    right video.
    """
    repaired = 0
    for intent in intents:
        queries = by_scene.get(intent.scene_id) or []
        if not queries:
            continue
        anchor = _setting_anchor_for(getattr(intent, "location", ""))
        if not anchor:
            continue
        if any(anchor in str(query.query or "") for query in queries):
            continue                       # the setting is already in one of this beat's queries
        native = [q for q in queries if _contains_japanese(q.query)]
        if not native:
            continue
        base = native[0]
        text = sanitize_platform_query(f"{anchor} {base.query}")
        if not text or any(text == str(query.query or "") for query in queries):
            continue
        # ADD, DO NOT DISPLACE. Inserting the anchored form at the front pushed the beat's own
        # term out of the budget: 配膳ロボット never ran and 飲食店 配膳ロボット returned four
        # results, starving all four robot beats. Some subjects need the place (手作り弁当 means
        # home cooking without it), some already imply it - so run both and let the results
        # decide, rather than guessing which kind this is.
        anchored = SearchQueryV2(
            query=text, language="ja", tier=base.tier, visual_intent_id=base.visual_intent_id,
            scene_ids=list(base.scene_ids or [intent.scene_id]),
            query_type="setting_anchor", generated_from="deterministic_setting_repair",
            reason="the beat is set in a named place and no query carried it",
            expected_subject=base.expected_subject, expected_action=base.expected_action,
            expected_location=getattr(intent, "location", ""),
            platforms=list(base.platforms or ["tiktok"]),
            requires_japanese_context=True)
        budget = max(1, int(SCRAPE_V2_CONFIG.get("max_initial_queries_per_chapter", 3)))
        if len(queries) < budget:
            queries.append(anchored)
        else:
            queries[-1] = anchored          # the last slot is the weakest discovery variant
        repaired += 1
    if repaired:
        _log(status_cb, "Scrape V2: %d beat(s) named a place their queries had dropped; "
                        "re-anchored them to the setting." % repaired)
    return repaired


def build_scene_bound_query_plan(intents, project_dir, script_text, use_influencer_hook=False,
                                 status_cb=None, active_platforms=None):
    """Create, validate, and persist a fresh search plan for this exact script.

    V2 never reads a saved planner result. The persisted audit is diagnostic
    only and ties every executable query to current scene IDs and its visual
    intent, preventing a global pool from silently crossing scene boundaries.
    """
    script_hash = hashlib.sha256((script_text or "").strip().encode("utf-8")).hexdigest()
    by_scene, accepted, rejected = {}, [], []
    active = ({_canonical_platform(value) for value in (active_platforms or [])}
              if active_platforms is not None else None)
    for intent in intents:
        needs_japanese_context = requires_japanese_context(intent, script_text)
        scene_queries = []
        for query in queries_for_intent(intent):
            query_platforms = {_canonical_platform(value) for value in (query.platforms or [])}
            if active is not None:
                # RE-TARGET, NEVER DISCARD. The platform on a query is the backend the planner
                # had in mind, not a property of the words: "タクシー 自動ドア" finds the same
                # footage on TikTok as on Instagram. Dropping the mismatches deleted 87 of 126
                # planned queries on one run - including EVERY taxi and robot-hotel search - so
                # seven beats were never searched for at all and ended up with a dashcam, coin
                # lockers and a bowl of ramen. Point the query at what the user did enable.
                query.platforms = (sorted(query_platforms & active) if (query_platforms & active)
                                   else sorted(active))
            # Keep the English expat/viral lane. Japan provenance is enforced on every returned
            # source below, so an English query can discover English-captioned footage explicitly
            # tagged Japan/Tokyo without opening the door to generic Western creators.
            ok, reason, normalized = validate_query_against_intent(query, intent)
            if not ok:
                rejected.append({
                    "text": query.query, "scene_ids": [intent.scene_id],
                    "visual_intent_id": intent.intent_id, "reason": reason,
                })
                continue
            query.query = normalized
            query.scene_ids = [intent.scene_id]
            query.requires_japanese_context = needs_japanese_context
            scene_queries.append(query)

        # Guarantee the dual discovery lane even when the Architect emits an empty or abstract
        # English phrase.  Build a compact fallback from the tangible visible subject, never from
        # the whole narration sentence ("regional Japanese" was the useless real-run example).
        has_tangible_english = any(
            not _contains_japanese(query.query) and query.language not in ("ja", "hashtag")
            for query in scene_queries)
        if not has_tangible_english and (active is None or "tiktok" in active):
            subject_words = [word for word in re.findall(
                r"[A-Za-z0-9]+", str(intent.subject or intent.story_subject or ""))
                if len(word) >= 3 and word.casefold() not in _EN_STOP]
            subject_words = subject_words[:3]
            if subject_words:
                fallback_text = "Japan " + " ".join(subject_words)
                fallback = SearchQueryV2(
                    query=fallback_text, language="en", tier="semantic_action",
                    visual_intent_id=intent.intent_id, scene_ids=[intent.scene_id],
                    query_type="english_subject_fallback",
                    generated_from="deterministic_dual_lane",
                    reason="English expat/viral fallback from the chapter's tangible subject",
                    expected_subject=intent.subject or intent.story_subject or "",
                    expected_action=intent.action or "", expected_location="Japan",
                    platforms=["tiktok"], requires_japanese_context=needs_japanese_context)
                ok, reason, normalized = validate_query_against_intent(fallback, intent)
                if ok:
                    fallback.query = normalized
                    scene_queries.append(fallback)
                else:
                    rejected.append({
                        "text": fallback_text, "scene_ids": [intent.scene_id],
                        "visual_intent_id": intent.intent_id, "reason": reason,
                    })

        # Deliberate discovery lanes per visual chapter. Disabled platforms have already been
        # removed above, so TikTok-only runs spend every slot on TikTok instead of silently
        # sacrificing one third of their search budget to X/Instagram queries that cannot run.
        def _priority(item):
            platforms = {_canonical_platform(value) for value in (item.platforms or [])}
            native = item.language == "ja" or _contains_japanese(item.query)
            if "tiktok" in platforms and native:
                lane = 0
            elif "tiktok" in platforms:
                lane = 1
            elif native:
                lane = 2
            else:
                lane = 3
            tier = V2_QUERY_TIERS.index(item.tier) if item.tier in V2_QUERY_TIERS else 9
            return lane, tier

        budget = max(1, int(SCRAPE_V2_CONFIG.get("max_initial_queries_per_chapter", 3)))
        selected, selected_keys = [], set()
        ranked = sorted(scene_queries, key=_priority)
        # The successful hand searches use native TikTok twice (bare object, then visible use)
        # and English once. X/Instagram are fallbacks, not a mandatory third dead-end search.
        # If one lane is unavailable, the later fill loop still uses the strongest remaining one.
        for wanted_lane, lane_slots in ((0, 2), (1, 1), (2, 1)):
            for query in ranked:
                if lane_slots <= 0 or len(selected) >= budget:
                    break
                key = _query_identity(query)
                if key in selected_keys or _priority(query)[0] != wanted_lane:
                    continue
                selected_keys.add(key)
                selected.append(query)
                lane_slots -= 1
            if len(selected) >= budget:
                break
        for query in ranked:
            if len(selected) >= budget:
                break
            key = _query_identity(query)
            if key in selected_keys:
                continue
            selected_keys.add(key)
            selected.append(query)
        for query in scene_queries:
            if query not in selected:
                rejected.append({
                    "text": query.query, "scene_ids": [intent.scene_id],
                    "visual_intent_id": intent.intent_id,
                    "reason": "chapter query budget: lower-priority discovery variant",
                })
        by_scene[intent.scene_id] = selected
        accepted.extend(selected)

    _repair_missing_setting_anchor(intents, by_scene, status_cb)

    coverage = {
        str(scene_id): len(queries)
        for scene_id, queries in sorted(by_scene.items())
        if not (use_influencer_hook and scene_id == 0)
    }
    missing = [scene_id for scene_id, count in coverage.items() if count == 0]
    audit = {
        "engine": "scrape_v2",
        "planner_version": V2_ANALYSIS_VERSION,
        "script_sha256": script_hash,
        "query_count": len(accepted),
        "coverage_by_scene": coverage,
        "queries": [{
            "text": query.query, "source": ",".join(query.platforms) or "all",
            "scene_ids": query.scene_ids, "visual_intent_id": query.visual_intent_id,
            "query_type": query.query_type, "generated_from": query.generated_from,
            "reason": query.reason,
            "requires_japanese_context": query.requires_japanese_context,
        } for query in accepted],
        "rejected_before_search": rejected,
    }
    review_dir = Path(project_dir) / "review"
    review_dir.mkdir(parents=True, exist_ok=True)
    (review_dir / "search_plan_audit.json").write_text(
        json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
    if missing:
        raise RuntimeError("Scrape V2 search plan has no valid queries for scene(s): " + ", ".join(missing))
    _log(status_cb, "Scrape V2 search-plan audit: %d scene-bound query/queries across %d scene(s); "
                    "%d rejected before search." % (len(accepted), len(coverage), len(rejected)))
    return by_scene, audit


# ---------------------------------------------------------------- metadata relevance + ranking (PURE)

_RAW_FOOTAGE_POS = ("vlog", "pov", "日常", "散歩", "歩く", "walk", "routine", "ルーティン",
                    "一人暮らし", "帰り", "通勤", "街", "night", "夜")
_RAW_FOOTAGE_NEG = ("解説", "まとめ", "react", "リアクション", "talking", "commentary", "説明",
                    "テロップ", "字幕", "news", "ニュース")
# What a candidate scores on resolution when its metadata does not say. Mid-scale on purpose:
# a 1080x1920 upload scores ~9.5 here and a 720-wide one ~5.5, so an unknown clip sorts among
# the ordinary rather than below every measured one.
UNKNOWN_SHAPE_SCORE = 5.0

_RISK_TERMS = tuple(list(clip_scraper._ANIME_GAME_TERMS) + list(clip_scraper._AI_CONTENT_TERMS)
                    + list(clip_scraper._LIVE_SCREEN_TERMS) + list(clip_scraper._IDOL_PROMO_TERMS))


_JAPAN_TOPIC_MARKERS = (
    "japan", "japanese", "tokyo", "osaka", "kyoto", "harajuku", "shibuya",
    "日本", "東京", "大阪", "京都", "原宿", "渋谷",
)


def _contains_japanese(text: str) -> bool:
    """Return whether text contains Japanese writing, without making claims about people in video."""
    return bool(re.search(r"[\u3040-\u30ff\u3400-\u9fff]", str(text or "")))


def is_japan_context(text: str) -> bool:
    value = str(text or "").casefold()
    return _contains_japanese(value) or any(marker in value for marker in _JAPAN_TOPIC_MARKERS)


def has_japanese_source_signal(meta: dict) -> bool:
    """Check source provenance via its metadata, never visual ethnicity.

    A native Japanese query alone is useful but can still return globally popular English
    material.  A Japanese caption, hashtag, creator name, or explicit Japan location is the
    minimum source-side evidence for a Japan-specific person or culture story.
    """
    blob = " ".join([
        str(meta.get("caption") or ""), " ".join(meta.get("hashtags") or []),
        str(meta.get("author") or ""), str(meta.get("author_name") or ""),
        str(meta.get("location") or ""),
    ])
    return is_japan_context(blob)


def requires_japanese_context(intent: VisualIntent, script_text: str = "") -> bool:
    return is_japan_context(" ".join((script_text or "", intent.scene_text or "",
                                       intent.subject or "", intent.action or "",
                                       intent.location or "", intent.story_subject or "")))


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
    # X used a quarter of the floor because its like counts ran that much lower; it was removed
    # on 2026-09-03, so every remaining platform shares the tier floor.
    return LIKE_FLOORS_V2.get(tier, 0)


def rank_metadata_candidates_v2(candidates, query: SearchQueryV2, platform_counts=None,
                                creator_counts=None):
    """Rank like the successful manual footage pass: visible relevance first, then clean native
    portrait footage, with popularity as a meaningful tie-breaker rather than a hard gate.
    Applies the tier-aware dynamic like floor. Returns the surviving list sorted best-first.

    rank = relevance*0.48 + specificity*0.10 + raw_footage_prob*0.14 + resolution*0.10
           + engagement*0.10 + diversity*0.08 - risk_penalty
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
        c.japanese_context = has_japanese_source_signal(meta)
        # A culture-specific story cannot silently fall back to generic global footage.
        # Missing metadata is not proof of a bad creator, but it is insufficient provenance
        # when the brief itself requires Japanese context.
        if query.requires_japanese_context and not c.japanese_context:
            continue
        rel = estimate_metadata_relevance(meta, query)
        raw_p = expected_raw_footage_probability(query, meta)
        # An UNKNOWN shape must score neutrally, not zero. The Bright Data payload has no height
        # field, so every record from the paid fallback - the only source left once TikTok hits
        # its daily limit - was landing on 0 of 10 here while a login result scored up to 10. The
        # clip was not worse; its metadata was thinner. Scoring absent evidence as bad evidence
        # quietly ranked the fallback out of every chapter it was supposed to rescue.
        res = UNKNOWN_SHAPE_SCORE
        if c.width and c.height:
            long_side = max(c.width, c.height)
            res = max(0.0, min(10.0, (long_side - 400) / 160.0)) if long_side else 0.0
            if c.height < c.width:               # landscape source -> weak
                res *= 0.4
        like_eng = min(10.0, math.log10(int(c.likes or 0) + 1) * 1.7)
        view_eng = min(10.0, math.log10(int(c.views or 0) + 1) * 1.35)
        eng = like_eng * 0.65 + view_eng * 0.35
        blob = (c.caption + " " + " ".join(c.hashtags)).lower()
        risk = 2.5 if any(t in blob for t in _RISK_TERMS) else 0.0
        # 9:16 output - landscape sources (X is ~all landscape) must never outrank an available
        # portrait clip; they stay usable as fallback but sort behind.
        if c.width and c.height and c.width > c.height:
            risk += 1.2
        # diversity bonus: reward platforms/creators not already dominant this bucket
        div = 5.0
        div -= 1.5 * platform_counts.get(str(c.platform).lower(), 0) / 5.0
        div -= 2.0 * creator_counts.get(str(c.creator_id).lower(), 0)
        div = max(0.0, min(10.0, div))
        c.metadata_relevance = rel
        c.rank_score = round(rel * 0.48 + spec * 10 * 0.10 + raw_p * 0.14 + res * 0.10
                             + eng * 0.10 + div * 0.08 - risk, 3)
        out.append(c)
    out.sort(key=lambda x: x.rank_score, reverse=True)
    return out


def interleave_ranked_sources_by_scene(candidates):
    """Preserve relevance order while giving every searched chapter a download slot.

    A global sort can spend a 12-download coverage budget on one high-like query even though the
    batch intentionally searched four different chapters. Manual sourcing always inspects a few
    results from *each* query. This round-robin reproduces that behaviour without weakening the
    per-query relevance ranking; candidates shared by merged queries are assigned to the least-fed
    scene and every source still appears exactly once.
    """
    rows = list(candidates or [])
    if len(rows) < 2:
        return rows
    buckets = {}
    unscoped = []
    for row in rows:
        scene_ids = list(dict.fromkeys(getattr(row, "scene_ids", None) or []))
        if not scene_ids:
            unscoped.append(row)
            continue
        chosen = min(scene_ids, key=lambda sid: len(buckets.get(sid, [])))
        buckets.setdefault(chosen, []).append(row)
    for bucket in buckets.values():
        bucket.sort(key=lambda item: float(getattr(item, "rank_score", 0.0) or 0.0), reverse=True)
    ordered = []
    scene_order = sorted(buckets)
    depth = 0
    while True:
        added = False
        for scene_id in scene_order:
            bucket = buckets[scene_id]
            if depth < len(bucket):
                ordered.append(bucket[depth])
                added = True
        if not added:
            break
        depth += 1
    unscoped.sort(key=lambda item: float(getattr(item, "rank_score", 0.0) or 0.0), reverse=True)
    return ordered + unscoped


# ---------------------------------------------------------------- planner (LLM)

def _build_social_search_plan_v2_legacy(title, script, scenes, understanding=None, reasoning_model=None,
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
    rows = _rows_of(data, "intents")
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


# latin letters are allowed ONLY as short unit/trend tokens (40kg, 150cm, BMI, GRWM) - a real
# English word (5+ latin chars) still disqualifies the string as a native Japanese query
# 々-〇 are the iteration mark and its neighbours - 々 alone appears in
# 代々木, 人々, 時々, 様々. It sits below the kana block, so leaving it out silently
# rejected whole place names: 代々木公園 failed this fullmatch and was dropped before
# any search on the tokyo project.
_JP_QUERY_ALLOWED_RE = re.compile(r"^[\u3005-\u3007\u3040-\u30ff\u3400-\u9fff\uff66-\uff9f0-9A-Za-z#\s]+$")
_JP_LATIN_WORD_RE = re.compile(r"[A-Za-z]{5,}")


# decorative fillers dropped BEFORE the 3-token trim so the content nouns survive
# ("steaming traditional japanese onsen pov" must become "japanese onsen pov", never
# "steaming traditional japanese" - proven by the onsen run where that query carried 8 clips).
_EN_QUERY_FILLER = {"the", "a", "an", "of", "in", "on", "at", "with", "and", "to", "for",
                    "is", "are", "very", "super", "really", "extremely", "beautiful",
                    "stunning", "gorgeous", "amazing", "breathtaking", "steaming",
                    "traditional",
                    # A NEGATION IS NOT A PICTURE. "no high school lunch" was trimmed to the
                    # search term "no high", and nothing can be filmed of a thing not happening -
                    # the beat wants the bento box that replaces it. Spend these tokens first.
                    "no", "not", "never", "without", "none", "nothing", "cannot", "dont",
                    "doesnt", "isnt", "arent"}

# Platform names are not content.  Appending "TikTok"/"X" while already searching those
# platforms makes their search engines favour meta posts about the platform and, for Japanese
# queries, wastes one of the two useful tokens.  Keep this sanitiser at the final execution
# boundary as well as in the Architect cleanup so custom/legacy/hook pools cannot bypass it.
_PLATFORM_QUERY_TOKENS = {
    "tiktok", "tiktoker", "tik-tok", "tik_tok", "twitter", "x.com", "instagram", "insta",
    "instagramer", "reels", "reel", "ig", "shorts", "youtube",
}


def _repair_mojibake(value):
    """Recover UTF-8 text accidentally decoded as Latin-1/CP1252."""
    text = str(value or "").strip()
    if not text or not any(marker in text for marker in ("Ã", "Â", "â", "å", "æ", "ç", "ð")):
        return text
    for encoding in ("cp1252", "latin1"):
        try:
            repaired = text.encode(encoding).decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
        if re.search(r"[\u3040-\u30ff\u3400-\u9fff\uff66-\uff9f]", repaired):
            return repaired
    return text


def sanitize_platform_query(value):
    text = _repair_mojibake(value)
    # Planner labels such as "#03" are not search terms. Commas are usually a
    # malformed planner separator, not meaningful platform syntax.
    text = re.sub(r"(?<!\S)#\d+\b", " ", text).replace(",", " ")
    tokens = text.split()
    cleaned = [tok for tok in tokens
               if tok.strip("#@.,:;!?()[]{}").casefold() not in _PLATFORM_QUERY_TOKENS]
    return " ".join(cleaned).strip(" \t\r\n.,:;!?")


def _clean_english_queries(values, limit=4):
    """Accept only plain, short raw search strings (never notes/labels/JSON fragments).

    BROAD-DISCOVERY CAP (2026-07-12): real TikTok/X search returns ~0 results for specific
    multi-word phrases, so every query is hard-trimmed to 3 tokens no matter what the model
    produced (fillers dropped first). The vision matcher finds the exact moment inside the
    videos - the query only has to land in the right neighbourhood."""
    out = []
    for value in values if isinstance(values, list) else []:
        query = sanitize_platform_query(value)
        if not query or len(query) > 90 or any(ch in query for ch in "()[]{}:/"):
            continue
        if len(re.findall(r"[A-Za-z0-9]+", query)) < 2:
            continue
        if query.lower().startswith(("english", "query", "search")):
            continue
        toks = query.split()
        if len(toks) > 3:
            kept = [t for t in toks if t.lower() not in _EN_QUERY_FILLER]
            if len(kept) >= 2:
                toks = kept
        query = " ".join(toks[:3])
        if query.lower() not in {item.lower() for item in out}:
            out.append(query)
        if len(out) >= limit:
            break
    return out


def _clean_japanese_queries(values, limit=4):
    """Enforce raw native Japanese strings: no Romaji, translations or annotations can leak.
    Keep compact event phrases intact here; callers that use the broad generic lane apply their
    stricter two-token cap separately. This preserves a visible Japanese action such as
    ``路上 酔っ払い 寝てる`` rather than silently turning it into generic street footage."""
    out = []
    for value in values if isinstance(values, list) else []:
        query = sanitize_platform_query(value)
        if not query or len(query) > 50 or not _JP_QUERY_ALLOWED_RE.fullmatch(query):
            continue
        if _JP_LATIN_WORD_RE.search(query):
            continue
        if not re.search(r"[\u3040-\u30ff\u3400-\u9fff\uff66-\uff9f]", query):
            continue
        query = " ".join(query.split()[:4])
        if query not in out:
            out.append(query)
        if len(out) >= limit:
            break
    return out


def _canonical_platform(value):
    key = str(value or "").strip().lower()
    if key in ("ig", "insta", "instagram", "reels"):
        return "instagram"
    return "tiktok" if key in ("tt", "tiktok") else key


def _clean_queries_for_platform(platform, values, language="auto", limit=4):
    """Validate an Architect query without destroying the platform-specific search grammar.

    The old universal 2-token trim erased X disambiguators (``女性 土俵 救命`` became
    ``女性 土俵``) and broadcast Instagram tags to all sites. TikTok remains compact, X may keep
    one proof disambiguator, and Instagram may use a single native hashtag.
    """
    platform = _canonical_platform(platform)
    out, seen = [], set()
    rows = values if isinstance(values, list) else []
    for value in rows:
        query = sanitize_platform_query(value)
        if not query or len(query) > 120 or any(ch in query for ch in "()[]{}:/"):
            continue
        if language == "hashtag":
            # Exactly one tag. Multiple tags in the keyword box are a weak neighbourhood and the
            # backend already routes a leading # to the platform's tag surface where supported.
            tag = query.split()[0].lstrip("#")
            tag = re.sub(r"[^\w\u3040-\u30ff\u3400-\u9fff\uff66-\uff9f]", "", tag)
            query = "#" + tag if tag else ""
        else:
            has_japanese = bool(re.search(r"[\u3040-\u30ff\u3400-\u9fff\uff66-\uff9f]", query))
            # The declared language names the array the Architect filed the string under. It
            # is not a fact about the string, and the Architect puts native terms in its
            # "english" array constantly. Believing the label deleted 62 usable queries on
            # the tokyo project before any search: \u4ee3\u3005\u6728\u516c\u5712, \u65b0\u5bbf\u99c5 and
            # \u6e0b\u8c37 \u30a4\u30eb\u30df\u30cd\u30fc\u30b7\u30e7\u30f3 have no Latin words at all, so the two-Latin-word rule
            # below threw them out. Read the text.
            effective = "ja" if has_japanese else language
            if effective == "ja" and (not _JP_QUERY_ALLOWED_RE.fullmatch(query)
                                      or _JP_LATIN_WORD_RE.search(query)):
                continue
            if effective == "en" and len(re.findall(r"[A-Za-z0-9]+", query)) < 2:
                continue
            toks = query.split()
            max_tokens = 2 if platform == "tiktok" else (3 if platform == "instagram" else 4)
            if effective == "en" or (effective == "auto" and not has_japanese):
                useful = [token for token in toks if token.casefold() not in _EN_QUERY_FILLER]
                if len(useful) >= 2:
                    toks = useful
                toks = _trim_english_query_tokens(toks, max_tokens)
            query = " ".join(toks[:max_tokens])
        key = query.casefold()
        if query and key not in seen:
            seen.add(key); out.append(query)
        if len(out) >= limit:
            break
    return out


# Geo words carry the least information inside a query that is already Japan-scoped: every
# returned source has its Japan provenance enforced separately.  Under a 2-token cap they are
# the first thing to give up.
_EN_GEO_TOKENS = {"japan", "japanese", "tokyo", "osaka", "kyoto", "nippon", "nihon"}


def _trim_english_query_tokens(toks, max_tokens):
    """Choose WHICH tokens survive an English query's token cap.

    A blind head-trim splits fixed names.  The chocolate short searched TikTok for
    "Japan White" and "Valentine White" - neither is a thing - because "Japan White Day" and
    "Valentine White Day" were cut after two tokens, and the White Day beats found nothing.
    Give up the geo word first, and never cut between two capitalised words that name one
    concept.
    """
    toks = list(toks or [])
    if len(toks) <= max_tokens or max_tokens < 2:
        return toks
    # 1. spend the geo word before a content word, while enough content is left to search on
    lean = [tok for tok in toks if tok.casefold().strip("#") not in _EN_GEO_TOKENS]
    if len(lean) >= 2:
        toks = lean
    if len(toks) <= max_tokens:
        return toks

    # 2. slide the window so a capitalised compound ("White Day") stays whole
    def _proper(tok):
        return bool(re.match(r"^[A-Z][a-z]", tok))

    def _damage(start):
        window = toks[start:start + max_tokens]
        after = toks[start + max_tokens] if start + max_tokens < len(toks) else ""
        cuts_before = start > 0 and _proper(toks[start - 1]) and _proper(window[0])
        cuts_after = bool(after) and _proper(window[-1]) and _proper(after)
        # Splitting a compound and keeping its ORPHAN half ("Valentine White") is worse than
        # dropping a leading modifier and keeping the compound whole ("White Day"), so a cut on
        # the right edge outranks a cut on the left. Earlier windows win ties.
        return (cuts_after, cuts_before, start)

    best = min(range(0, len(toks) - max_tokens + 1), key=_damage)
    return toks[best:best + max_tokens]


def _normalize_platform_query_plan(raw):
    plan = {}
    for raw_platform, values in (raw.items() if isinstance(raw, dict) else []):
        platform = _canonical_platform(raw_platform)
        if platform not in ("tiktok", "instagram") or not isinstance(values, dict):
            continue
        row = {}
        if platform == "instagram":
            row["keywords"] = _clean_queries_for_platform(platform, values.get("keywords"), "auto", 4)
            row["hashtags"] = _clean_queries_for_platform(platform, values.get("hashtags"), "hashtag", 4)
        else:
            row["japanese"] = _clean_queries_for_platform(platform, values.get("japanese"), "ja", 4)
            row["english"] = _clean_queries_for_platform(platform, values.get("english"), "en", 3)
        if any(row.values()):
            plan[platform] = row
    return plan


def _architect_primary_query_plan(row):
    """Put the human-editor searches ahead of optional platform variants.

    The action phrase deliberately precedes the bare noun. A live karaoke regression showed
    why: ``個室カラオケ`` returned caption-adjacent stages, shops and unrelated creators, while
    ``カラオケ 歌ってみた`` immediately returned people singing inside karaoke rooms. Round zero
    executes only the first phrase, so putting the noun first effectively discarded the useful
    query until a much later recovery round.
    """
    row = row if isinstance(row, dict) else {}
    plan = _normalize_platform_query_plan(row.get("platform_queries"))
    tiktok = dict(plan.get("tiktok") or {})
    native = (_clean_japanese_queries([row.get("native_action_query")], 1)
              + _clean_japanese_queries([row.get("native_object_query")], 1))
    english = _clean_english_queries([row.get("english_discovery_query")], 1)
    for key, primary in (("japanese", native), ("english", english)):
        existing = list(tiktok.get(key) or [])
        merged, seen = [], set()
        for value in primary + existing:
            identity = str(value).casefold()
            if identity and identity not in seen:
                seen.add(identity)
                merged.append(value)
        if merged:
            tiktok[key] = merged
    if tiktok:
        plan["tiktok"] = tiktok
    return plan


def build_viral_search_plan_v2(title, script, scenes, understanding=None, reasoning_model=None,
                                status_cb=None):
    """Architect Agent: tangible viral B-roll concepts + raw EN/JA platform search strings."""
    ac = _ac()
    if not (script or "").strip():
        return []
    system = (
        "You are the Architect Agent for fast viral B-roll in the 'Wildest School Rules' style. "
        "Your JSON is executed autonomously by TikTok, Instagram and X Scraper Agents. Never make a boring "
        "sentence translation. Extract the underlying object, action, emotion, awkwardness or shock, "
        "then describe what it physically looks like in authentic phone footage. JSON only.")
    prompt = ac.understanding_brief(understanding) + f"""
For every scene choose exactly one visual_match_category:
- literal: direct visual proof is important.
- vibe: turn an abstract feeling into a concrete scannable action.
- shock: use exaggeration, awkwardness, surprise or meme humor as a retention hook.

Examples: "no free time" -> student asleep at desk or massive textbook pile. "strict discipline"
-> perfectly synchronized drill. "romance banned" -> awkward teenage couple or teacher intervening.
Create one central phone-filmable TOPIC ANCHOR and TWO genuinely different coverage shots. Each
needs a visible subject, visible action and plausible location. A scene can contain several
sentences and actions. In that case, do not compress the entire chapter into one rare micro-action.
Use `subject` and `action` for the common, unmistakable activity at the heart of the chapter, and
put 2-4 separately filmable proof shots in `required_elements`. Example: a karaoke chapter about
ordering food, trading songs and a sleeping friend should use the central activity "friends singing
inside a private karaoke room" and coverage elements such as "person singing with microphone",
"friends seated in karaoke room", "food on karaoke table", "person asleep on sofa". It must NOT
make "presses room-phone buttons" the sole intent merely because that detail appeared first.

SEARCH THE EVENT, NOT AN ADJECTIVE. If narration requires a person doing something, never reduce
it to clothing + a state such as "dirty suit", "morning suit" or "tired student". Those searches
mostly return fashion shops, cleaning advice and portraits. Use the native incident/action people
actually caption instead: passed-out-on-the-street slang, missing-the-last-train language, pouring
beer for a boss, waking up outside, or the equivalent concrete event for this scene. Include the
person class only when it narrows that event; the verb/incident must remain in the query.

BUILD A SEARCH LADDER, NOT FOUR SYNONYMS. The executable terms for every scene must cover four
different ways real uploaders would expose the SAME visible claim:
1. EXACT ANCHOR - the indispensable object/place by its native name.
2. VISIBLE USE - that anchor plus the hand/person action the narration requires.
3. NATIVE POST LANGUAGE - the compact colloquial phrase, review term, product name or あるある
   phrase a local uploader would actually put in the caption.
4. EVIDENCE ALTERNATIVE - a different filmable shot that proves the same local claim or human
   consequence, without merely sharing a broad word.
Do not fill this ladder with translations or near-identical variants. A query is useful only if
its likely result page could visibly contain a required element. For example, a restaurant handbag
basket needs terms for 荷物かご / バッグ置き / the act of placing a bag beneath a restaurant chair;
"container", "restaurant vlog" and "Japan dining" are not recovery queries. A toilet-tank sink
needs 手洗い付きトイレ / tank handwashing or visible water reuse; generic toilet tours are not proof.

For the PRIMARY situation, also return three dedicated search strings. They are not prose:
- native_object_query: the exact native name of the tangible object/place, optionally one context
  word. It must work as a bare TikTok search.
- native_action_query: that same native anchor plus the visible use/action OR the compact phrase
  real uploaders use for filming themselves doing it (for example カラオケ 歌ってみた,
  カラオケ 女子会, カラオケ 飯). This is the FIRST query the scraper executes. Prefer a populated
  native content neighbourhood over a technically exact phrase nobody captions.
- english_discovery_query: a compact 2-3 word English discovery fallback.
These fields are mandatory and must describe the same local claim. Never substitute a mood,
country-wide theme or generic creator type for the named object/action.

SEARCH LIKE AN UPLOADER, NOT A DICTIONARY. Bare nouns are only the fallback. The primary TikTok
query must normally combine the cultural anchor with an uploader-native action, format, group or
result word such as 歌ってみた, 女子会, 開封, 使ってみた, やってみた, ルーティン, 飯, 寝落ち,
あるある. Do not search an incidental prop (telephone, button, receipt) when the narration's
central activity has a much richer native result neighbourhood.

SEARCH THE REMARKABLE VERSION OF THE THING. Native platforms are full of footage of any
subject; almost all of it is a shopfront, a street or someone walking past. What gets
filmed and posted is the extreme of the thing - the fullest hall, the biggest payout, the
loudest moment, the strangest machine, the process at its most satisfying. After the bare
noun, write queries for THAT: the quantity, the record, the mess, the close-up of the
mechanism, the reaction. A correct but empty shot is a wasted beat.

NAME THE THING, THEN SEARCH FOR IT. Before writing queries for a scene, decide what the
ONE concrete noun of that beat is - the object, machine, room or place the sentence is
actually about (a photo booth, a love hotel, a vending machine, a pachinko parlour, a
drain, a lane rope). At least one query per scene MUST be that noun on its own, in the
local language, with nothing else attached. Native speakers tag their footage with the
name of the thing, not with a description of the mood around it.

WHY THIS IS A RULE AND NOT A SUGGESTION: on a live plan, three beats about photo booths,
love hotels and public distance were all given the same searches - "shibuya crowds",
"couple date vlog", "#tokyo" - and the word for photo booth never appeared once. Every
one of those searches returns real footage; none of it shows what the sentence says.
Mood and place words are what you add AFTER the noun, never instead of it.

Also choose one communication_role:
- proof: a named place, event, sign, rule or factual object must be visibly proven.
- demonstration: hands/person demonstrates the concrete object or procedure.
- human_consequence: show what the rule causes a person to do or endure.
- emotion: a physical expression/action communicates an abstract feeling.
- pattern_interrupt: a surprising but still locally meaningful visual punchline.
Objects are REQUIRED when they are the clearest proof or demonstration (four sweets, five cups,
a warning gate, a ribbon being tied). The "prefer humans" rule applies only to emotion/filler.

STORY-THESIS GUARD: First infer the recurring human subject and overall claim from the FULL script.
The opening hook is often only one example, not the overall topic. A hook noun may influence only
the scenes that explicitly mention it. Do not carry it into later intents, alternatives, queries or
global context unless the later narration repeats it. Example: if sumo appears only in the hook of a
story about restrictions on women, sumo is valid only for that hook block; the recurring subject is
women experiencing different restrictions. Each later scene must follow its own local claim.

QUERIES ARE DISCOVERY SEARCHES, NOT TRANSLATED SCENE DESCRIPTIONS. Generate a separate executable
plan for every platform because their discovery grammars differ:
- TikTok: 1-2 compact native words describing a phone-filmable situation. Preserve a named-entity
  anchor when one exists (名頃 かかし), and never append TikTok, Instagram, X, reels or shorts.
- Instagram: 1-2 keyword phrases plus 1-2 single native hashtags. A hashtag is ONE raw string that
  begins with #; no spaces, annotations, translations or multiple tags in one string.
KEEP THE SETTING IN THE QUERY. When the beat's location names a place - a school, a classroom,
a station, a hospital, an office, a shop - at least the first query for that beat must contain it.
Measured on the school-lunch run: every one of its seven beats read "Japanese high-school
classroom / school shop / cafeteria", and the queries came back as 手作り弁当 and おにぎり 昼ごはん
with the school dropped. Those words mean HOME COOKING to the platform, so thirty-five of the
sixty-eight analysed clips were somebody's kitchen counter and not one was a school. 高校生 手作り弁当
and 教室 弁当 - the same food WITH the setting - returned real school footage immediately. The food
is what happens; the place is what makes it the right video.
NAME THE PLACE, DO NOT DESCRIBE IT. When a beat is about a specific real chain, venue, brand,
product, machine, event or system, the FIRST query must be the name locals actually type and tag -
not a descriptive compound you assembled out of the narration. Measured on the Tokyo hotel run:
the beat about the dinosaur-staffed hotel searched 恐竜受付 and returned ZERO results on every
platform, while the name people use - 変なホテル - is tagged on hundreds of uploads. The same
pattern: くら寿司 / ビっくらポン beats 皿投入口, ユニクロ セルフレジ beats RFIDレジ, エコサイクル
beats 地下駐輪場. If you know the real name, the name IS the first query and the descriptive
compound is at best the second. Only invent a description when the thing genuinely has no name.
English is a secondary discovery lane: 2-3 targeted words, only when local-language results may be
insufficient. The downstream vision agent finds the exact seconds, but the query must still land in
the correct subject/action neighbourhood.
VIBE scenes: ONE query may add a single emotion word (疲れた / awkward japan). SHOCK scenes: ONE
query may chase the punchline with a single meme word (ハプニング / japan fail).
INTELLIGENT TERMS: you MAY invent platform-native terms that PROVE the scene's feeling even when
those words never appear in the script - three levers beat literal translation: (1) the NUMBERS
users flex/complain with ("40kg ダイエット" / "シンデレラ体重", never 体重計), (2) the HUMAN
ACTION the topic causes (泣いた / ボディチェック / 失敗), (3) native slang (あるある / 限界 /
密着 / ルーティン).
HARD RULE - PHYSICAL ATTRIBUTES: when a scene is about weight, body shape, height or looks,
NEVER search for measuring OBJECTS (scale/体重計, tape measure, calculator, mirror). ALWAYS
search measurable numbers ("40kg", "150cm") or platform action trends (ボディチェック / body
check, 骨格診断, outfit try on, GRWM) so results show HUMANS, not props. Searching the object
filled half a video with store shelves and suitcases - retention death.
AUDIO-ONLY CLAIMS: narration about voices, pronunciation, politeness, announcements, accents or sound
cannot be proven from silent video frames. Do not invent generic proxy scenes such as random women,
fashion, stations or people laughing. Search for the topic-specific visible setting, speaker,
interaction or object that is actually observable. If no topic-specific visual evidence exists,
mark the intent as context and use clearly related setting footage, never unrelated b-roll.
Use shock/pattern_interrupt only when it strengthens the local narration beat. Never impose a fixed
cadence and never replace factual proof with an unrelated meme merely because several scenes passed.
Japanese strings must be what local users write, including useful native slang such as あるある or
厳しい. ABSOLUTE RULE: every japanese_queries string contains ONLY Kanji, Hiragana, Katakana, spaces,
digits or #. No Romaji, English, translation, parentheses, colons, slashes, labels or notes.
Never translate the full narration sentence. Arrays contain strings only.

SUBJECT/ACTION/LOCATION ARE FACTS THE LENS CAN SEE, NOT SHOT DIRECTION. `subject` names the
thing or person, `action` names what it visibly does, `location` names where it is. No camera
words (close-up, macro, cross-section, slow motion, aerial, POV), no quality adjectives
(high-end, premium, perfect, fluffy, beautiful, satisfying), no editing terms. Those belong in
camera_style and mood, and nowhere else. This is not cosmetic: the matcher has to confirm
subject and action as VISIBLE EVIDENCE in the footage, so "Close-up of the fluffy texture and
perfect cross-section" is unconfirmable by construction and scores every real clip at zero.
Measured on one konbini run: the same 53 segments matched 0 of 4 beats written that way and 2
of 4 when the same beats read "packaged sandwiches / a hand taking a pack from the chiller".
Write what is in front of the camera, and let the camera fields describe the camera.

Scenes:
{{numbered}}

Return exactly:
{{"intents":[{{"scene_id":0,"visual_type":"concrete|context|abstract",
"visual_match_category":"literal|vibe|shock","communication_role":"proof|demonstration|human_consequence|emotion|pattern_interrupt",
"story_subject":"recurring subject of full script","local_claim":"claim of this scene",
"subject":"...","action":"...","location":"...",
"native_object_query":"native object/place search","native_action_query":"native use/action search",
"english_discovery_query":"2-3 word fallback",
"camera_style":"pov|handheld|vlog|static|walking","mood":"...",
"required_elements":["..."],"optional_elements":["..."],"avoid_elements":["..."],
"platform_queries":{{"tiktok":{{"japanese":["日本語検索"],"english":["raw phrase"]}},
"instagram":{{"keywords":["日本語検索"],"hashtags":["#日本語タグ"]}},
}},
"alternatives":[{{"subject":"...","action":"...","location":"...","camera_style":"...",
"mood":"...","platform_queries":{{"tiktok":{{"japanese":["日本語検索"],"english":[]}},
"instagram":{{"keywords":["日本語検索"],"hashtags":["#日本語タグ"]}},
}}}}]}}]}}
"""

    # BATCHED (2026-07-11): one call for ALL scenes truncated on longer scripts - ~400 output
    # tokens per scene x 20+ scenes plus the model's reasoning tokens exceeds any max_tokens,
    # the JSON gets cut mid-array and extract_json_object returns None ("Architect returned no
    # executable plan" although the API worked). 8 scenes per call always fit comfortably.
    ARCH_BATCH = 8

    def _architect_call(idx_list, temperature):
        numbered = "\n".join(
            f"scene {i}: {(ac.scene_text_for_planning(scenes[i]) or scenes[i].get('script', ''))[:180]}"
            for i in idx_list)
        data = _llm_json([{"role": "system", "content": system},
                          {"role": "user", "content": prompt.replace("{numbered}", numbered)}],
                         max_tokens=10000, temperature=temperature,
                         reasoning_model=reasoning_model,
                         status_cb=status_cb, label="architect")
        return _rows_of(data, "intents")

    rows = []
    all_ids = list(range(len(scenes)))
    for b0 in range(0, len(all_ids), ARCH_BATCH):
        batch_ids = all_ids[b0:b0 + ARCH_BATCH]
        got = _architect_call(batch_ids, 0.35)
        if not got:
            _log(status_cb, "Scrape V2 Architect: batch %d-%d returned no plan; retrying once "
                            "with lower variance..." % (batch_ids[0], batch_ids[-1]))
            got = _architect_call(batch_ids, 0.1)
        rows.extend(got or [])
    intents, seen = [], set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            sid = int(row.get("scene_id"))
        except (TypeError, ValueError):
            continue
        if sid in seen or not 0 <= sid < len(scenes):
            continue
        seen.add(sid)
        category = str(row.get("visual_match_category") or "vibe").strip().lower()
        if category not in ("literal", "vibe", "shock"):
            category = "vibe"
        visual_type = str(row.get("visual_type") or "context").strip().lower()
        if visual_type not in ("concrete", "context", "abstract"):
            visual_type = "context"
        communication_role = str(row.get("communication_role") or "human_consequence").strip().lower()
        if communication_role not in ("proof", "demonstration", "human_consequence", "emotion",
                                      "pattern_interrupt"):
            communication_role = "human_consequence"
        alternatives = []
        for alt in (row.get("alternatives") or [])[:3]:
            if isinstance(alt, dict):
                alternatives.append(AlternativeVisualIntent(
                    subject=str(alt.get("subject") or ""), action=str(alt.get("action") or ""),
                    location=str(alt.get("location") or ""),
                    camera_style=str(alt.get("camera_style") or ""), mood=str(alt.get("mood") or ""),
                    english_queries=_clean_english_queries(alt.get("english_queries"), 2),
                    japanese_queries=_clean_japanese_queries(alt.get("japanese_queries"), 2),
                    platform_queries=_normalize_platform_query_plan(alt.get("platform_queries"))))
        intents.append(VisualIntent(
            scene_id=sid, scene_text=(ac.scene_text_for_planning(scenes[sid]) or "")[:200],
            visual_type=visual_type, match_category=category, communication_role=communication_role,
            story_subject=str(row.get("story_subject") or ""),
            local_claim=str(row.get("local_claim") or ""),
            subject=str(row.get("subject") or ""), action=str(row.get("action") or ""),
            location=str(row.get("location") or ""), camera_style=str(row.get("camera_style") or ""),
            mood=str(row.get("mood") or ""), required_elements=list(row.get("required_elements") or []),
            optional_elements=list(row.get("optional_elements") or []),
            avoid_elements=list(row.get("avoid_elements") or []), alternative_visuals=alternatives,
            english_queries=_clean_english_queries(row.get("english_queries")),
            japanese_queries=_clean_japanese_queries(row.get("japanese_queries")),
            platform_queries=_architect_primary_query_plan(row)))
    missing = [sid for sid in range(len(scenes)) if sid not in seen]
    if missing:
        _log(status_cb, f"Scrape V2 Architect omitted {len(missing)} scene(s); using the legacy visual "
                        "planner only for those scenes (never raw narration fragments).")
        legacy = _build_social_search_plan_v2_legacy(
            title, script, scenes, understanding=understanding, reasoning_model=reasoning_model,
            status_cb=status_cb)
        legacy_by_id = {item.scene_id: item for item in legacy
                        if item.subject or item.location or item.jp_subject or item.jp_action}
        for sid in missing:
            if sid in legacy_by_id:
                intents.append(legacy_by_id[sid])
    intents.sort(key=lambda item: item.scene_id)
    if intents:
        _n_shock = sum(1 for x in intents if getattr(x, "match_category", "") == "shock")
        _log(status_cb, f"Scrape V2 Architect: {_n_shock} locally justified shock scene(s) "
                        f"across {len(intents)}; no forced cadence.")
    query_count = sum(len(queries_for_intent(item)) for item in intents)
    _log(status_cb, "Scrape V2 Architect: %d tangible visual intent(s), %d executable "
                    "platform-scoped queries." % (len(intents), query_count))
    if not intents or not any(queries_for_intent(item) for item in intents):
        raise RuntimeError("Scrape V2 Architect could not generate any valid visual search phrases after retry.")
    return intents


def build_social_search_plan_v2(title, script, scenes, understanding=None, reasoning_model=None,
                                status_cb=None):
    """Public V2 planner entry point retained for trainers/callers; now uses the viral Architect."""
    return build_viral_search_plan_v2(
        title, script, scenes, understanding=understanding, reasoning_model=reasoning_model,
        status_cb=status_cb)


# ---------------------------------------------------------------- proxy download + segments

def _download_direct_media(url, dest, status_cb=None, timeout=90):
    """Fetch a signed CDN link straight to disk. Returns the path, or None to fall back.

    Deliberately quiet about failure: a signed link expires, and an expired link is a reason to
    try the normal downloader, not a reason to log an error for a clip that may still arrive.
    """
    dest = Path(dest).with_suffix(".mp4")
    try:
        request = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0", "Accept": "*/*"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if int(getattr(response, "status", 200)) >= 400:
                return None
            cap = int(SCRAPE_V2_CONFIG["proxy_max_filesize_mb"]) * 1024 * 1024
            written = 0
            with open(dest, "wb") as handle:
                while True:
                    chunk = response.read(262144)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > cap:
                        raise ValueError("over the proxy size cap")
                    handle.write(chunk)
    except Exception:                                                   # noqa: BLE001
        try:
            dest.unlink(missing_ok=True)
        except OSError:
            pass
        return None
    # A CDN that answers an expired link with an HTML error page still writes a file.
    if not dest.is_file() or dest.stat().st_size < 40000:
        dest.unlink(missing_ok=True)
        return None
    if status_cb:
        status_cb(f"Fetched {dest.name} directly from the CDN link in the API record.")
    return dest


def download_proxy_v2(item, dest, status_cb=None, fmt=None):
    """Download a LOW-RES analysis proxy of the WHOLE video (<=720p, no audio, size-capped) so we
    can find usable segments anywhere in the timeline - not just the first seconds. Returns path|None.
    `fmt`: override the yt-dlp format (e.g. full-quality+audio for the timeline agent fetch)."""
    url = (item.get("webVideoUrl") or item.get("url") or "") if isinstance(item, dict) else ""
    video_meta = (item.get("video") or {}) if isinstance(item, dict) else {}
    if not url and not video_meta:
        return None
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    # A record from the paid API already carries a direct CDN link to the file. Fetching that is
    # both faster and the whole point of an API-only run: downloading the PAGE instead puts
    # yt-dlp back in front of the logged-out Instagram/TikTok web app, which is the negotiation
    # this mode exists to skip. Falls through to the normal path if the link is dead or expired -
    # CDN links are signed and time-limited.
    direct = str(item.get("_media_url") or "") if isinstance(item, dict) else ""
    if direct.startswith("http"):
        got = _download_direct_media(direct, dest, status_cb=status_cb)
        if got:
            return got
    # TikTok refuses both of the other routes: yt-dlp answers "Unexpected response from webpage
    # request" in about a second, and the signed CDN link answers 403 to every header combination
    # (browser UA, Referer, Origin, Range) - including a fresh request from the logged-in context
    # itself. The one thing that works is not re-requesting at all: the browser has already
    # fetched the media to play it, so the bytes are taken from ITS response. Measured 3/3 where
    # both other routes were 0/3. Instagram is left on the direct link, which works and is faster.
    platform = str(item.get("_platform") or item.get("platform") or "").lower()         if isinstance(item, dict) else ""
    if "tiktok" in (platform or url.lower()):
        try:
            import tiktok_login
            if tiktok_login.is_ready():
                got = tiktok_login.download_sync(url, dest, status_cb=status_cb, timeout_s=90)
                if got and Path(got).is_file():
                    return Path(got)
        except Exception:      # noqa: BLE001 - fall through to yt-dlp rather than lose the clip
            pass
    tmpl = str(dest.with_suffix("")) + ".%(ext)s"
    h = int(SCRAPE_V2_CONFIG["proxy_max_height"])
    fmt = fmt or f"bestvideo[height<={h}]/best[height<={h}]/best"
    opts = {
        "quiet": True, "no_warnings": True, "noprogress": True, "outtmpl": tmpl,
        "format": fmt, "merge_output_format": "mp4",
        "max_filesize": int(SCRAPE_V2_CONFIG["proxy_max_filesize_mb"]) * 1024 * 1024,
        "ignoreerrors": True, "postprocessors": [],
    }
    clip_scraper._apply_cookies(opts)
    # Give this download its OWN copy of the cookie jar.
    #
    # yt-dlp rewrites the cookiefile from scratch when the YoutubeDL context closes - open(
    # path, "w"), no lock, no atomic rename - and it does so even for a failed download.
    # Six of these run at once now, all pointed at the one merged jar, so a reader could
    # land mid-truncate. Measured over repeated open/close cycles: 12-17% of them raised
    # "does not look like a Netscape format cookies file", and one load silently returned
    # 151 of 420 cookies, which is a download running half logged out. The damaged jar is
    # then written BACK, so the shared file shrinks for every later download and the session
    # decays. A private copy costs one file write and makes the whole class impossible.
    jar = opts.get("cookiefile")
    direct_cookiefile = jar
    scratch = None
    if jar and Path(jar).is_file():
        scratch = dest.parent / (".cookies_" + dest.stem + ".txt")
        try:
            shutil.copyfile(jar, scratch)
            opts["cookiefile"] = str(scratch)
        except OSError:
            scratch = None          # fall back to the shared jar rather than losing cookies
    try:
        if yt_dlp is not None and url:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])
    except Exception:
        # TikTok's current webpage endpoint can reject yt-dlp while the logged-in search API
        # has already returned a valid signed CDN playAddr. Fall through to that address below.
        pass
    finally:
        if scratch:
            try:
                scratch.unlink(missing_ok=True)
            except OSError:
                pass
    for cand in sorted(dest.parent.glob(dest.stem + ".*")):
        if cand.suffix.lower() in (".mp4", ".mov", ".mkv", ".webm") and cand.stat().st_size > 8192:
            if cand != dest:
                try:
                    cand.replace(dest)
                    return dest
                except Exception:
                    return cand
            return dest
    # Logged-in TikTok search results carry signed video CDN URLs. This is not a second scraper;
    # it is the direct media address returned with the result. In August 2026 yt-dlp began raising
    # "Unexpected response from webpage request" for these IDs even though the signed URL works
    # with the same session cookies. Use it as the authenticated fallback so found clips are not
    # reported as zero downloads.
    direct_urls = []
    for info in sorted((video_meta.get("bitrateInfo") or []),
                       key=lambda row: float(row.get("Bitrate") or 0), reverse=True):
        addr = info.get("PlayAddr") or {}
        try:
            height = int(addr.get("Height") or 0)
        except (TypeError, ValueError):
            height = 0
        if height and height > h:
            continue
        direct_urls.extend(str(value) for value in (addr.get("UrlList") or []) if value)
    direct_urls.extend(str(video_meta.get(key) or "") for key in ("playAddr", "downloadAddr"))
    cookie_header = ""
    if direct_cookiefile and Path(direct_cookiefile).is_file():
        cookies = []
        try:
            for line in Path(direct_cookiefile).read_text("utf-8", errors="ignore").splitlines():
                fields = line.split("\t")
                if len(fields) >= 7 and fields[5]:
                    cookies.append(fields[5] + "=" + fields[6])
            cookie_header = "; ".join(cookies)
        except OSError:
            pass
    max_bytes = int(SCRAPE_V2_CONFIG["proxy_max_filesize_mb"]) * 1024 * 1024
    for direct_url in dict.fromkeys(value for value in direct_urls if value.startswith("http")):
        try:
            request = urllib.request.Request(direct_url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                              "AppleWebKit/537.36 Chrome/140 Safari/537.36",
                "Referer": "https://www.tiktok.com/", "Cookie": cookie_header})
            with urllib.request.urlopen(request, timeout=60) as response, dest.open("wb") as fh:
                total = 0
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise OSError("direct proxy exceeds configured size cap")
                    fh.write(chunk)
            if dest.stat().st_size > 8192:
                return dest
        except Exception:
            try:
                dest.unlink(missing_ok=True)
            except OSError:
                pass
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
    # Sample across the WHOLE upload. Returning the first three windows looked neutral but meant a
    # long TikTok was only inspected near its opening/middle and the end was never seen. Candidate
    # windows are alternatives (only one source window can ship), so slight overlap is harmless and
    # must not delete an entire probe region before vision has inspected it.
    windows.sort()
    merged = list(dict.fromkeys(windows))
    cap = max(1, int(cfg["max_segments_per_source"]))
    if len(merged) <= cap:
        return merged
    if cap == 1:
        return [merged[len(merged) // 2]]
    # When more windows exist than we can afford, skip the opening-biased first region and spread
    # the budget from the next region through the end (for five regions/cap three: 20%, 50%, 90%).
    indexes = [1 + int(i * (len(merged) - 2) / float(cap - 1)) for i in range(cap)]
    return [merged[index] for index in dict.fromkeys(indexes)]


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
    # Segment discovery only uses cuts to place window boundaries, so an unknown scan
    # costs variety, not correctness.
    cuts = clip_scraper.hard_cut_times(proxy_path, ffmpeg, scan_seconds=scan) or []
    windows = _candidate_windows(duration, cuts, cfg)
    segs = []
    for (start, end) in windows:
        sid = f"{source.platform}_{source.source_id}_{int(start*1000)}_{int(end*1000)}"
        segs.append(SegmentCandidate(
            segment_id=sid, source_id=source.source_id, platform=source.platform,
            source_path=str(proxy_path), start_time=start, end_time=end,
            duration=round(end - start, 3), creator_id=source.creator_id,
            query=source.query, query_tier=source.query_tier,
            scene_ids=list(source.scene_ids or []),
            metadata_relevance=source.metadata_relevance,
            japanese_context=bool(source.japanese_context)))
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


def _is_native_9_16(width, height):
    """True for genuine portrait phone media: 9:16 or TALLER.

    The gate this feeds exists to refuse landscape and fake verticals (a 16:9 clip letterboxed
    into a portrait canvas). A symmetric tolerance around 9:16 also refused the opposite thing -
    a real phone recording. Modern phones film 9:19.5, 9:20 or 9:22, so a 720x1560 upload sits at
    0.462 and was thrown away for being "not native 9:16" when it is more native than 9:16 is:
    it needs a top/bottom crop to reach 1080x1920, which loses nothing and is what the finalizer
    already does. Anything WIDER than 9:16 is still refused - that is where the fake verticals,
    the 3:4 uploads and the landscape live.
    """
    try:
        width, height = int(width), int(height)
    except (TypeError, ValueError):
        return False
    if width <= 0 or height <= width:
        return False
    ratio = width / float(height)
    # 0.40 is about 9:22.5, past any phone aspect in use; the +0.04 keeps the old top tolerance.
    return 0.40 <= ratio <= (9.0 / 16.0) + 0.04


# Measured on the run that produced a motionless hook, with _motion_score below:
#   the dead street sign that shipped   1.02
#   the weakest clip that looked fine   1.72
#   ordinary handheld footage           2.3 - 7.0
# 1.35 sits between the two with room on both sides. It is a thin margin and it is drawn
# from one project, so it is a floor for the OPENING shot only - mid-video the same clip is
# survivable, and rejecting good footage everywhere to fix the hook would be a bad trade.
MOTION_DEAD = 0.45        # a photograph with sensor noise on it; fatal anywhere
MOTION_HOOK_MIN = 1.35    # the opening shot has to move: it is the whole scroll-stop
# Cadence hitches PER SECOND at which a window is refused. 0.8 lets a ~3.8s shot carry
# three of them - measured on a real pool, that keeps ordinary phone footage (a pan easing
# off, a subject pausing) and still refuses a duplicated-frame re-encode, which produces a
# hitch several times a second.
CADENCE_HITCH_RATE = 0.8
# Longest run of byte-identical frames a window may contain. Below this it is a held beat;
# above it the viewer sees a still. _motion_score already refuses footage that never moves.
FROZEN_RUN_FATAL = 0.5


def _motion_score(frames_bgr):
    """0-10: how much of the FRAME changes across the window. 0 is a held photograph.

    Mean pixel difference was the first attempt and it was wrong: compression noise and one
    pedestrian crossing a locked-off shot both raise it, so a motionless street sign scored
    3.3 out of 10 and passed. What a viewer calls "still" is that the picture's AREA does not
    change, so this measures the share of pixels that move by more than noise. A handheld or
    walking shot moves most of the frame; a tripod on a sign moves a few percent of it.
    """
    if np is None or not frames_bgr or len(frames_bgr) < 2:
        return 0.0
    shares = []
    for a, b in zip(frames_bgr, frames_bgr[1:]):
        if a is None or b is None or a.shape != b.shape:
            continue
        diff = np.abs(a.astype("int16") - b.astype("int16")).max(axis=2)
        shares.append(float((diff > 24).mean()))       # 24/255 clears sensor noise
    if not shares:
        return 0.0
    return round(min(10.0, (sum(shares) / len(shares)) * 14.0), 2)


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
    seg.source_width = int(w)
    seg.source_height = int(h)
    seg.native_9_16 = _is_native_9_16(w, h)
    vertical_quality = 0.0
    if h > 0 and w > 0:
        ratio = h / float(w)
        vertical_quality = max(0.0, min(10.0, (ratio - 0.9) * 9.0))
        if max(w, h) < clip_scraper.MIN_LONG_SIDE:
            vertical_quality *= 0.5
    # Sample once, at the exact window.  The prior implementation first decoded five generic
    # frames up to ``end_time`` and immediately threw them away, then invoked ffmpeg five more
    # times for this window.  On resume this turned twelve already-downloaded proxies into
    # hundreds of pointless ffmpeg launches before the actual search could even start.
    frames = _sample_window_frames(src, ffmpeg, seg.start_time, seg.end_time, n=5)
    if not frames:
        seg.rejection_reasons = ["no_frames"]
        return seg

    frozen_run = _detect_duplicate_frame_run(src, seg.start_time, seg.end_time)
    seg.frozen_run_seconds = round(frozen_run, 3)
    # 0.15s is four identical frames at 30fps - every held beat, every editor's freeze on
    # a punchline, every source that repeats a frame across a speed ramp. The live search
    # controller diagnosed its own failures as "high micro-freeze rejections" while the
    # motion gate above already refuses genuinely dead footage. Half a second is the point
    # where a viewer sees a still rather than a hold.
    if frozen_run >= FROZEN_RUN_FATAL:
        reasons.append("frozen_frames")

    # How much does the picture actually MOVE?
    #
    # The duplicate-run check above only catches byte-identical frames, and a camera phone
    # never produces those - sensor noise and compression make every frame differ by a hair
    # while nothing in the shot moves. A real run put a motionless street sign on the hook of
    # a short: 1 of its 92 frames showed any change, it passed every gate, and the opening
    # two seconds read as a photograph. "Not duplicated" is not "moving".
    seg.motion_score = _motion_score(frames)
    # Motion is an editorial preference, not proof that the file is unusable.  A slow close-up of
    # the exact object can be the best explanatory insert in the body; the dedicated hook gate still
    # rejects it at scene 0.  Keep it in the pool and make it lose to moving footage through quality.
    motion_penalty = 1.4 if seg.motion_score < MOTION_DEAD else 0.0
    cadence_hitches = pipeline.micro_stutter_events(src, seg.start_time, seg.end_time)
    seg.micro_stutter_count = len(cadence_hitches)
    # A hitch is one frame whose motion drops below a quarter of its neighbours'. ONE of
    # those is not a defect, it is a pan decelerating or a person pausing, and rejecting on
    # the first one killed 31% of every candidate window measured on a real pool - before
    # anything asked what the footage showed. What is worth refusing is a source whose
    # cadence is broken THROUGHOUT: a re-encode with duplicated frames, or a slideshow.
    # That shows up as a rate, not a single event, so the rule is per second of window.
    _window = max(0.5, float(seg.end_time) - float(seg.start_time))
    cadence_penalty = (1.0 if len(cadence_hitches) / _window >= CADENCE_HITCH_RATE else 0.0)

    fv = clip_scraper.detect_fake_vertical_or_black_bars(src, ffmpeg, seconds=seg.end_time)
    black_bar = float(fv.get("black_bar_score", 0.0))
    if black_bar > SEGMENT_HARD_REJECT_BLACKBAR:
        reasons.append("massive_black_bars")
    if h < w and vertical_quality < 2.0:
        # Landscape (X is ~all 16:9) is CROPPABLE: _finalize_segment_clip center-crops every
        # accepted clip to 1080x1920 anyway, so aspect ratio alone must never reject an
        # otherwise good clip. Only sources whose 9:16 strip would be too small survive-wise
        # (< ~360px wide after crop) are still out.
        if h * 9.0 / 16.0 >= 360.0:
            vertical_quality = 4.0        # usable via crop - still ranks behind native portrait
        else:
            reasons.append("landscape_too_small_to_crop")

    # OCR: text load + AI watermark (hard) using the reusable stats
    # OCR can use the exact visual samples we already paid to decode.  It does not need to spin
    # up another four ffmpeg processes for the same 3–8 second window.
    areas, lines, texts = _ocr_window(src, ffmpeg, seg.start_time, seg.end_time, frames=frames)
    text_heaviness_ocr = clip_scraper._score_from_ocr(areas, lines)
    if any(clip_scraper._AI_WATERMARK_RE.search(t) for t in texts):
        reasons.append("ai_watermark")

    cap_prob, cap_text_heavy, hard_over_subject = _segment_caption_signals(frames)
    # Not a rejection any more - it was the single largest loss (32 of 64 declines) on a
    # topic TikTok is full of. It stays on the segment as a score penalty so a clean clip
    # still wins when there is a choice.
    seg.caption_over_subject = bool(hard_over_subject)

    # internal cuts WITHIN this window
    stab = _window_stability(src, ffmpeg, ffprobe, seg.start_time, seg.end_time)
    edit_stability = 10.0 if stab["stable"] else max(0.0, 8.0 - stab["internal_cut_count"] * 3.0)
    # ONE original cut inside an assigned clip is allowed; two is not.
    #
    # This used to reject on the first cut, and measured on a real run that was the single
    # largest loss: 56% of all candidate windows died here, before anything asked what the
    # footage showed. It is also unmeetable on the material - a window is ~3.8s and social
    # footage cuts about that often, so the rule demanded a continuous take that most
    # uploads simply do not contain. One inherited cut inside a beat reads as pace; two
    # inside four seconds is someone else's edit showing through.
    rapid_cut_penalty = 0.0
    if stab.get("cuts_unknown"):
        # Not a rejection: throwing away good footage because ffmpeg hiccupped is worse
        # than keeping a clip that might carry two cuts. But it is recorded, so a run whose
        # cut scans were all failing is visible in the report instead of looking clean.
        seg.cut_scan_failed = True
    elif stab["internal_cut_count"] >= 2:
        # The finalizer searches for a clean sub-window before export.  Rejecting the whole upload
        # here discarded useful footage elsewhere in the same candidate and was one of the largest
        # losses in real scrape reports.  Rank it down; final cut QA remains authoritative.
        rapid_cut_penalty = min(2.0, 0.55 * float(stab["internal_cut_count"]))

    text_heaviness = max(text_heaviness_ocr, cap_text_heavy)
    # Keep the two signals separate. A large subtitle can have the same numeric coverage as a
    # presentation slide, but only the latter should be rejected; subtitles are removed later.
    seg.ocr_text_heaviness = round(float(text_heaviness_ocr), 2)
    seg.caption_text_heaviness = round(float(cap_text_heavy), 2)
    text_cleanliness = max(0.0, 10.0 - text_heaviness * 2.0)
    # Burned captions are blurred later.  They should make a clean alternative win a tie, not push
    # otherwise exact footage below the pre-vision quality floor before semantic matching sees it.
    clean_frame_score = max(0.0, 10.0 - cap_prob * 2.0)
    resolution_score = max(0.0, min(10.0, (max(w, h) - 400) / 160.0))
    raw_footage_score = max(0.0, min(10.0, 6.0 + (edit_stability - 5.0) * 0.4 - cap_prob * 0.6))
    action_visibility = 6.0    # neutral prior; vision stage refines this

    quality = (vertical_quality * 0.15 + raw_footage_score * 0.20 + edit_stability * 0.20
               + clean_frame_score * 0.15 + text_cleanliness * 0.10 + resolution_score * 0.10
               + action_visibility * 0.10 - motion_penalty - cadence_penalty - rapid_cut_penalty)

    seg.quality_score = round(quality, 2)
    seg.raw_footage_score = round(raw_footage_score, 2)
    seg.edit_stability_score = round(edit_stability, 2)
    seg.text_heaviness = round(text_heaviness, 2)
    seg.caption_probability = cap_prob
    seg.black_bar_score = round(black_bar, 2)
    seg.vertical_quality = round(vertical_quality, 2)
    seg.rejection_reasons = reasons
    return seg


def _detect_duplicate_frame_run(path, start, end, pixel_delta=0.12):
    """Return the longest near-identical frame run inside a source window.

    This catches the tiny 5-6 frame stalls seen in otherwise valid TikToks. Sampling only five
    thumbnails missed them, so QA reads the actual frame cadence at a tiny 160px working size.
    """
    if cv2 is None or np is None:
        return 0.0
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        return 0.0
    try:
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 30.0)
        fps = fps if 5.0 <= fps <= 120.0 else 30.0
        cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, float(start)) * 1000.0)
        max_frames = max(1, int(math.ceil(max(0.0, float(end) - float(start)) * fps)) + 2)
        previous = None
        current = longest = 0
        for _ in range(max_frames):
            ok, frame = cap.read()
            if not ok:
                break
            gray = cv2.cvtColor(cv2.resize(frame, (160, 90)), cv2.COLOR_BGR2GRAY)
            if previous is not None:
                delta = float(np.mean(cv2.absdiff(previous, gray)))
                if delta <= float(pixel_delta):
                    current += 1
                    longest = max(longest, current)
                else:
                    current = 0
            previous = gray
        return longest / fps
    except Exception:
        return 0.0
    finally:
        cap.release()


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


def _ocr_window(path, ffmpeg, start, end, frames=None):
    """OCR frames inside the window; returns (areas, lines, texts) like clip_scraper._ocr_frame_stats."""
    if cv2 is None or np is None:
        return [], [], []
    frames = list(frames or []) or _sample_window_frames(path, ffmpeg, start, end, n=4)
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
    # This is the one caller where "unknown" must not read as "clean" - it decides whether
    # a clip is rejected for carrying someone else's edit. One retry on a shorter scan,
    # then the window is reported as UNKNOWN and the gate leaves it alone rather than
    # passing it as continuous footage.
    cuts = clip_scraper.hard_cut_times(path, ffmpeg, scan_seconds=min(end + 1.0, 90.0))
    if cuts is None:
        cuts = clip_scraper.hard_cut_times(path, ffmpeg, scan_seconds=min(end + 0.5, 30.0))
    if cuts is None:
        return {"internal_cut_count": 0, "stable": False, "cuts_unknown": True}
    inside = [c for c in cuts if start + 0.1 < c < end - 0.1]
    gaps = []
    bounds = [start] + inside + [end]
    for i in range(1, len(bounds)):
        gaps.append(bounds[i] - bounds[i - 1])
    return {"internal_cut_count": len(inside), "cuts_unknown": False,
            "stable": len(inside) == 0 and all(g >= 1.7 for g in (gaps or [end - start]))}


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


def match_thresholds_for_relevancy(visual_type, script_relevancy=70):
    """Return the actual semantic floors for the user's relevance control.

    V2 previously accepted a script_relevancy argument but never used it, so 90% behaved exactly
    like 70%. Keep 70 as the calibrated baseline and tighten/relax smoothly around it.
    """
    base = MATCH_THRESHOLDS_V2.get(visual_type, MATCH_THRESHOLDS_V2["context"])
    try:
        rel = max(0.0, min(100.0, float(script_relevancy)))
    except (TypeError, ValueError):
        rel = 70.0
    boost = (rel - 70.0) * 0.02
    script_floor = float(base["script_floor"]) + boost
    overall = float(base["overall"]) + boost * 0.75
    if str(visual_type).lower() == "shock" and rel >= 80:
        # Shock is a presentation strategy, not permission to use an unrelated meme.
        script_floor = max(script_floor, 4.0 + (rel - 80.0) * 0.03)
    return {"script_floor": round(max(1.5, min(9.0, script_floor)), 3),
            "overall": round(max(3.5, min(9.0, overall)), 3)}


def match_class_for(overall, visual_type, script_relevancy=70):
    th = match_thresholds_for_relevancy(visual_type, script_relevancy)
    if overall >= 8.0:
        return "A_MATCH"
    if overall >= 7.0:
        return "B_MATCH"
    if overall >= th["overall"]:
        return "C_MATCH"
    return "D_REJECTED"


def passes_match_floors(script_match, overall, visual_type, script_relevancy=70):
    th = match_thresholds_for_relevancy(visual_type, script_relevancy)
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
        search_context = "\n".join(
            f'seg_{idx:02d}: search context="{str(seg.query or "")[:80]}"'
            for idx, (seg, _strip) in enumerate(strips))
        prompt = (
            "Describe each numbered segment tile (seg_00, seg_01, ...); each tile is a few "
            "frames sampled across ONE candidate CUT - the exact window that would be used.\n"
            "Judge ONLY what is visible in that tile. Do not judge the upload it came from: "
            "a tile is a few seconds out of a much longer video, and the intro, the title "
            "card and the end screen are not in it.\n"
            "This matters most for burned_captions and visible_text - set them true only "
            "when text is on screen IN THESE FRAMES. Japanese creators caption their "
            "openings by convention, so judging whole uploads threw away nearly every "
            "usable Japanese result even when the middle of the video was clean.\n"
            "`usable` means there is recognizable real footage in the sampled window. Burned-in "
            "captions, subtitles, a username or other removable text NEVER make an otherwise real "
            "shot unusable; the caption is inpainted away later. One kind cannot be: text sitting "
            "on a SOLID PLATE - an opaque coloured or white rectangle, rounded box, banner or "
            "speech bubble drawn behind the letters. Removing those letters leaves the empty "
            "plate stranded on the picture, which looks worse than the caption did. Set "
            "caption_on_solid_box=true ONLY for that: a filled shape behind the text. Text with "
            "just an outline, a drop shadow or a soft glow is NOT a plate.\n"
            "Set usable=false only for slides, screen "
            "recordings/web pages, severe corruption, an unrecognizable frame, or content with no "
            "usable live-action/real-world picture.\n"
            "creator_overlay means a SEPARATE inset window of the person filming reacting to "
            "the footage - a picture-in-picture head, a circular face bubble, a green-screen "
            "commentator pasted over the shot. A person simply being in the scene, filming "
            "themselves, talking to camera or holding the phone is NOT an overlay. This flag "
            "rejects the clip outright, and left undefined it was set on a quarter of an "
            "ordinary Japanese pool - every clip that merely had a face in it.\n"
            "NAME THE SPECIFIC VISIBLE THING. Do not collapse an identifiable cultural object "
            "into a generic category: a card visibly showing a decorated manhole and/or a sign "
            "reading マンホールカード is a manhole card, not merely an 'illustrated card'; a "
            "decorated utility cover embedded in pavement is a decorated manhole cover, not merely "
            "a 'circular plaque'. The search context below is a CLUE for vocabulary, never proof: "
            "use the specific name only when the frames themselves support it through shape, "
            "setting, printed imagery or readable text. If the picture contradicts the query, "
            "describe the picture and ignore the query.\n\n"
            "SEARCH CONTEXT BY TILE:\n" + search_context + "\n\n"
            "Return STRICT JSON keyed by index.\n"
            'For each: {"subjects":[".."],"subject_count":int,"action":"..","location":"..",'
            '"camera_style":"..","shot_size":"..","motion":"low|moderate|high","visible_text":"..",'
            '"creator_overlay":true|false,"burned_captions":true|false,'
            '"caption_on_solid_box":true|false,"raw_footage_score":0-10,'
            '"edit_stability":0-10,"visual_quality":0-10,"action_visibility":0-10,'
            '"age_confidence":"adult|teen|child|unknown","sexualized_content":true|false,'
            '"absurdity":0-10,"intensity":0-10,"striking":0-10,"usable":true|false}\n'
            '(absurdity: how exaggerated/meme-like/bizarre the clip is - 0 mundane, 10 '
            'alien-costume level. intensity: visual energy/craziness, used for escalation '
            'ordering within a topic block.\n'
            'striking: would a scroller STOP for this shot? Not whether it is pretty and '
            'not whether it is on topic - whether it is worth looking at. 8-10: a scale or '
            'quantity that is hard to believe, something genuinely strange, a satisfying '
            'process at its best moment, a detail nobody has seen before. 4-7: clearly '
            'interesting, a real thing happening, but familiar. 0-3: a correct but empty '
            'shot - a street, a shopfront, a wall, someone walking. Two clips can both '
            'show the subject and only one of them is worth a beat of a short.)\n'
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


def _segment_strip(seg: SegmentCandidate, frames_dir, idx, ffmpeg, frames_n=4):
    """Multi-frame horizontal strip built from frames INSIDE the segment window.

    `frames_n` is how much of the clip the vision pass gets to see. V2 inspects a short chosen
    SEGMENT, where four frames is plenty. V3 hands over the WHOLE source and asks whether any
    usable moment exists in it, which is a different question: four frames of a 25-second TikTok
    is one every six seconds, and a three-second relevant shot is more likely missed than seen.
    """
    import subprocess
    frames = _sample_window_frames(seg.source_path, ffmpeg, seg.start_time, seg.end_time,
                                   n=max(2, int(frames_n or 4)))
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


def _captions_are_removable(seg) -> bool:
    """Can the caption blur actually take the text off THIS segment?

    Runs the real glyph blur on a scratch copy of the cut and keeps the result only if it
    found letters. Guessing was tried twice and was wrong both times, in opposite
    directions. The blurred file is kept beside the source so the render can use it.
    """
    src = Path(str(seg.source_path or ""))
    if not src.is_file():
        return False
    cleaned = src.with_name("nocap_" + src.name)
    if cleaned.is_file() and cleaned.stat().st_size > 4096:
        seg.cleaned_path = str(cleaned)
        return True
    info = {}
    try:
        shutil.copyfile(src, cleaned)
        import caption_remover
        found = caption_remover.remove_caption_regions(
            str(cleaned), pipeline.find_ffmpeg(),
            seconds=max(1.0, float(seg.duration or 0) + 0.5), info=info,
            # Explicitly OFF, and it has to stay a written-down decision now that omitting the
            # argument means "clean it". This probe runs per candidate segment during a scrape,
            # and the only backend is a ProPainter fill that takes the whole card for about a
            # minute - the render does the real cleanup once the user has actually picked the
            # clip. What this call still buys is the ``refused`` coverage measurement, which is
            # what decides whether a segment is a text slide.
            allow_gpu=False)
    except Exception as exc:      # noqa: BLE001 - a broken blur must be visible, not silent
        print(f"[nocap] {src.name}: {type(exc).__name__}: {exc}")
        found = 0
    if not found:
        cleaned.unlink(missing_ok=True)
        # "Nothing to blur" and "too much text to blur" are different answers. The first leaves a
        # clean clip; the second leaves one whose captions are perfectly readable, and using it
        # untouched is exactly the failure this pass exists to prevent. Mark it so the editorial
        # gate can drop it - captions on 85% of the pool are tolerated, a frame plastered in
        # someone else's text is not.
        if info.get("refused"):
            seg.captions_unremovable = True
        return False
    seg.cleaned_path = str(cleaned)
    return True


def editorial_rejection_reason(seg: SegmentCandidate, intent: Optional[VisualIntent] = None) -> str:
    """Non-negotiable editorial checks applied before a clip can reach a timeline.

    These rules deliberately use declared source metadata and the vision description rather
    than trying to guess a person's ethnicity from their face.  An uncovered scene is safer
    than a polished but misleading clip.
    """
    source_parts = {part.casefold() for part in Path(str(seg.source_path or "")).parts}
    if "_declined" in source_parts:
        return "clip was previously rejected by the quality gate"
    if getattr(seg, "rejection_reasons", None):
        return "clip failed the technical quality gate"
    if str(getattr(seg, "quality_gate", "") or "").casefold() == "rejected":
        return "clip failed the quality gate"
    if not str(seg.query or "").strip():
        return "missing source query/provenance"
    desc = seg.visual_description or {}
    age = str(desc.get("age_confidence") or "").casefold()
    if age in {"child", "minor", "underage"}:
        return "child footage"
    if bool(desc.get("sexualized_content")):
        return "sexualized creator footage"
    # Non-sexualized school/festival/lifestyle footage is legitimate factual B-roll. The former
    # blanket teen veto made scripts about Japanese students incapable of using the actual subject
    # and forced them into generic adults, empty streets or filler.
    _sid = getattr(intent, "scene_id", None) if intent is not None else None
    # Only judge motion on a segment that was actually measured. analyze_segment_v2 fills
    # source_width, so an unset width means nobody looked - a library clip reused without a
    # fresh analysis would otherwise read as motionless and be barred from every hook.
    _measured = bool(getattr(seg, "source_width", 0))
    if _sid is not None and int(_sid) == 0 and _measured:   # `or -1` would swallow scene 0
        # The opening shot is the scroll-stop. A near-motionless clip there kills the short
        # before the first sentence lands, and a real run put a static street sign on it -
        # technically a video, visually a photograph. Mid-video the same clip is survivable;
        # in the hook it is not.
        if float(getattr(seg, "motion_score", 0) or 0) < MOTION_HOOK_MIN:
            return "opening shot barely moves"
    if bool(desc.get("creator_overlay")):
        # A picture-in-picture of the creator reacting cannot be blurred away - it IS the
        # shot.
        return "creator reaction overlay"
    if bool(desc.get("caption_on_solid_box")):
        # A caption drawn on an opaque plate is the one kind the remover cannot help with. It
        # takes the LETTERS off - deliberately, because painting the whole rectangle is what used
        # to smear away the subject - and that leaves the empty plate stranded on the picture,
        # which looks worse than the caption did. Vision decides this: a morphological test for
        # "uniform area behind the text" was calibrated against real clips and came out inverted,
        # scoring a table seam 9.2 and the one genuine plate 39.3.
        return "caption sits on a solid background box"
    if desc.get("usable") is False:
        # Stage-A is explicitly instructed that captions alone remain usable. This catches actual
        # slides, browser recordings, corrupt/unrecognizable frames and other non-footage.
        return "vision found no usable real footage"
    if bool(desc.get("burned_captions")):
        # Captions do not reject anything any more (user: "Untertitel sind egal").
        #
        # This rule cost more than it protected. 85% of the Japanese pool carries burned-in
        # text, so refusing it left twelve of fourteen beats borrowing one Shibuya clip -
        # a short where the picture matches the narration nowhere is worse than a short with
        # someone else's caption in the corner. The blur still runs, so the text is removed
        # wherever the glyph pass can find it; when there is simply nothing to find, the clip is
        # used as it is. The one exception is below: a clip so covered in text that removing it
        # would smudge a quarter of the frame is not a captioned clip, it is a text slide.
        _captions_are_removable(seg)
        # Checked AFTER the blur attempt, because that call is what sets the flag. "Nothing to
        # blur" and "too much text to blur" are different answers and only the second one has to
        # cost the clip: captions on 85% of the pool are tolerated, a frame plastered in someone
        # else's text is not, and using it untouched puts fully legible captions on screen.
        if bool(getattr(seg, "captions_unremovable", False)):
            return "caption text covers too much of the frame to remove"
    # Only OCR-heavy material that vision did NOT identify as burned captions is slide-like.
    # The former max(OCR, caption detector) gate contradicted the caption policy above and threw
    # away precisely the native TikToks the glyph blur is designed to recover.
    if (float(getattr(seg, "ocr_text_heaviness", seg.text_heaviness) or 0) >= 4.5
            and not bool(desc.get("burned_captions"))):
        return "text-heavy footage (a slide, not footage)"
    if intent is not None and requires_japanese_context(intent):
        if not seg.japanese_context:
            return "no Japanese source-context signal"
    return ""


# Words that only ever appear because a beat NAMES a distinguishing feature. When the narration
# says "blue lights on the platforms", a platform is not the shot - the blue lights are. These are
# the tokens whose absence turns a match into mere context.
_GENERIC_REQUIRED = {
    "japan", "japanese", "tokyo", "people", "person", "man", "woman", "adult", "student",
    "customer", "traveler", "traveller", "someone", "camera", "shot", "view", "scene", "place",
    "area", "indoor", "outdoor", "day", "night", "normal", "first", "second", "third", "some",
    "many", "believe", "designed", "create", "atmosphere", "thing", "things",
}


def distinguishing_elements(intent):
    """The named features that make this beat THIS beat, not its topic in general."""
    out = []
    for value in (list(getattr(intent, "required_elements", None) or [])
                  + [getattr(intent, "subject", ""), getattr(intent, "action", "")]):
        for word in re.findall(r"[A-Za-z]{4,}|[぀-ヿ㐀-鿿]{2,}", str(value or "")):
            key = word.casefold()
            if key in _GENERIC_REQUIRED or key in _EN_STOP or key in out:
                continue
            out.append(key)
    return out[:8]


def names_a_feature_it_cannot_show(intent, evidence, covers, missing):
    """True when vision itself reported a NAMED feature missing and never showed it.

    Measured on the "blue lights on their platforms" beat: the assigned clip is an ordinary
    railway platform with no blue lamp anywhere, and it scored 7.1 because `missing_required`
    only ranked. "Platform" satisfied "blue lights on a platform". The judgement has to come from
    the model's own answer, not from the beat's wording: whatever it listed as missing, if that
    thing is specific and appears nowhere in what it says it SAW, the clip is context for the
    topic and not the shot of the thing. Vibe and context beats are exempt - they never name a
    feature in the first place.
    """
    if str(getattr(intent, "visual_type", "") or "") != "concrete":
        return False
    # A montage beat spells out several phases ("misses the last train, sleeps outside, wakes for
    # work") and is DESIGNED to be covered one phase at a time. Missing the other phases is the
    # point, not a defect.
    if bool(getattr(intent, "allows_partial_coverage", False)):
        return False
    if str(getattr(intent, "match_category", "") or "").lower() not in ("literal", ""):
        return False
    # Scope it to the beat's own SUBJECT and ACTION - the thing the sentence is about. A beat
    # lists several required elements and real footage almost always misses a secondary one
    # ("names on the flags"); that is a ranking signal and must stay one. What cannot be missing
    # is the feature the subject itself names.
    core = " ".join(str(getattr(intent, key, "") or "")
                    for key in ("subject", "action")).casefold()
    seen = " ".join(list(evidence or []) + list(covers or [])).casefold()
    for item in (missing or []):
        for word in re.findall(r"[A-Za-z]{4,}|[぀-ヿ㐀-鿿]{2,}", str(item)):
            key = word.casefold()
            if key in _GENERIC_REQUIRED or key in _EN_STOP:
                continue
            if key in core and key not in seen:
                return True
    return False


def match_segments_to_scenes_v2(intents, segments, reasoning_model=None, status_cb=None,
                               stats=None):
    """Vision stage B (TEXT): compare structured segment descriptions to the visual intents. Returns
    a dict scene_id -> sorted list of {segment, subject_match, action_match, location_match,
    mood_match, script_match, semantic_match, overall_match, match_class} candidates that pass floors.

    BATCHED over scenes (2026-07-11): the response size scales with scenes x 4 candidates; one
    call for 16+ scenes exceeded max_tokens once the model spends reasoning tokens too - the JSON
    was cut mid-array and EVERY scene scored 0 (the all-emergency-fallback failure). 6 scenes per
    call keeps the answer far inside any budget; the full segment list rides along in each call.
    """
    MATCH_SCENE_BATCH = 6
    intents = list(intents or [])
    if len(intents) > MATCH_SCENE_BATCH:
        merged = {}
        matched_total = 0
        for b0 in range(0, len(intents), MATCH_SCENE_BATCH):
            part = match_segments_to_scenes_v2(intents[b0:b0 + MATCH_SCENE_BATCH], segments,
                                               reasoning_model=reasoning_model, status_cb=status_cb,
                                               stats=stats)
            merged.update(part or {})
        matched_total = sum(1 for v in merged.values() if v)
        _log(status_cb, f"Scrape V2: matcher total {matched_total}/{len(intents)} scene(s) "
                        f"across {(len(intents) + MATCH_SCENE_BATCH - 1) // MATCH_SCENE_BATCH} batches.")
        return merged
    ac = _ac()
    described = [s for s in segments if s.visual_description and not editorial_rejection_reason(s)]
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
            f'usable={d.get("usable")}, found_for_scene={s.scene_ids or "any"}')
    intent_lines = []
    for it in intents:
        intent_lines.append(
            f'scene {it.scene_id} [{it.visual_type}/{str(getattr(it, "match_category", "") or "vibe")}]: '
            f'multi_shot_chapter={bool(getattr(it, "allows_partial_coverage", False))}; '
            f'whole-story subject="{it.story_subject}", local claim="{it.local_claim}", '
            f'subject="{it.subject}", action="{it.action}", '
            f'location="{it.location}", mood="{it.mood}", '
            f'required visible elements={it.required_elements}, avoid={it.avoid_elements}; '
            f'text="{it.scene_text[:180]}"')
    prompt = (
        "You match short video SEGMENTS to narration SCENES for a found-footage short, and you "
        "report evidence rather than verdicts: the editor applies its own acceptance thresholds "
        "to your numbers, so a blanket \"reject by default\" simply leaves beats blank. For EACH "
        "scene, "
        "pick the best-fitting segments and score the fit. A segment fits when its subject/action/"
        "location genuinely support the scene's visible intent. `found_for_scene` only records which "
        "search turned the segment up; consider EVERY segment for EVERY scene, because a clip found "
        "while searching one beat is often the best picture for another. Fragments/abstract scenes accept a "
        "topically coherent segment; for those, a topically related Japanese slice-of-life segment is "
        "a VALID candidate (score it honestly rather than returning nothing). Scenes tagged /shock "
        "want the visual PUNCHLINE: absurd, exaggerated, cringe/awkward/fail or meme-like footage, "
        "but it must still show the local subject, action or human consequence. A random viral "
        "reaction is not a match. Only genuinely off-topic "
        "footage gets no candidate.\n"
        "SUBJECT-CLASS GUARD: a segment only matches when its visible SUBJECT CLASS fits the "
        "sentence. A line about human bodies, weight, height or looks can NEVER be matched by an "
        "object-only segment (a scale on a store shelf, a suitcase for 'heavy', a calculator) - "
        "it requires a visible PERSON. Never match on one shared keyword when the surrounding "
        "context differs (that is how a suitcase ended up illustrating body weight).\n"
        "NO METAPHOR SCORING: script_match measures whether the LITERAL on-screen content shows "
        "what the sentence talks about - a DIY key-holder tutorial is NOT a match for 'people "
        "erase their existence' just because keys are 'left behind'. A poetic/metaphorical "
        "connection caps script_match at 3. Tutorials, product demos, anime/avatar/cartoon "
        "footage, gaming overlays and comedy face-filters cap script_match at 2 unless the "
        "sentence is literally about that thing.\n\n"
        "IGNORE SHOT DIRECTION IN THE SCENE: a scene's subject/action may contain camera or "
        "quality words (close-up, macro, cross-section, slow motion, high-end, premium, perfect, "
        "satisfying). They are how the editor would like to FILM it, never something a clip has "
        "to prove. Judge only the thing and what it does: for \"Close-up of the fluffy texture "
        "and perfect cross-section\" of an egg sandwich, the requirement is an egg sandwich on "
        "screen - any framing. Treating the camera words as required evidence scores correct "
        "footage at zero.\n"
        "VISIBLE-EVIDENCE CONTRACT (MANDATORY): use ONLY the supplied subjects/action/location "
        "description. Never infer an object's purpose, ownership, location or use from its broad "
        "category. A container is not a restaurant bag basket. A kitchen is not a restaurant. A "
        "person holding a box is not a customer placing a handbag below a chair. A toilet bowl is "
        "not a toilet-tank sink or proof of water reuse. A cashier/register is not proof of money "
        "being placed in a coin tray. Shared words such as container, bag, cash, toilet, Japan or "
        "restaurant do not establish a match.\n"
        "For each proposed candidate return `visible_evidence`: short concrete things/actions that "
        "the description explicitly confirms, `missing_required`: every required scene element not "
        "explicitly confirmed, and `literal_match`: true only when the central subject AND central "
        "action are visibly proven. If an indispensable object/action is missing, set literal_match "
        "false and name it in missing_required - then SCORE THE REST HONESTLY on the full 0-10 "
        "scale instead of collapsing to a token low number: the right subject in the right place "
        "doing something adjacent is a 5-6, the right topic with the wrong subject is a 2-3, and "
        "unrelated footage is a 0-1. Flattening every imperfect clip to 3 destroys the ranking the "
        "editor needs and leaves beats with no picture at all. If the description is ambiguous, say "
        "so in missing_required and let the numbers carry the doubt; never invent evidence. "
        "`covers` may contain only exact scene requirements that "
        "are directly proven by visible_evidence. For multi_shot_chapter=true, a segment may be a "
        "literal candidate when it visibly proves ONE named required sub-action/object; list the "
        "other requirements under missing_required and put the proven sub-action in covers. Missing "
        "the other shots does not invalidate that segment. Generic setting, clothing, streets, or "
        "topic atmosphere alone still does not count as partial coverage.\n\n"
        "CENTRAL ACTION VS DETAIL ATTRIBUTES: a clip can be literal support when it visibly proves "
        "the central human action even if occupation, relationship, time of day or one wardrobe "
        "detail cannot be verified from frames. A person lying on a city pavement is valid support "
        "for narration about a salaryman passed out outside (list 'salaryman/suit' as missing); "
        "people drinking in an izakaya support an office drinking party (list boss/subordinate as "
        "missing); a suited commuter walking in the morning supports returning to work (list "
        "brushing off dust as missing). Score these as useful 5-7 candidates, not unrelated 0-3. "
        "This never permits a different SUBJECT CLASS: an object cannot stand in for a person.\n\n"
        "MASK SCENES: a visible person wearing a mask, face covering, train mask scene, school mask "
        "scene or masked date is a valid literal/context match for narration about masks becoming "
        "normal, comfort, politeness, or attraction starting above the nose. Do not reject it merely "
        "because the exact dating interaction is not visible.\n\n"
        "AUDIO CLAIMS: frames cannot prove a person's voice, accent, pronunciation or what an "
        "announcement sounds like. For those scenes, score only visible topic-specific evidence "
        "(the actual speaker, customer-service interaction, train announcement setting, or named "
        "cultural practice). Do not accept generic fashion, laughing, walking or unrelated station "
        "clips merely because the narration mentions sound.\n\n"
        "MULTI-SHOT CHAPTER COVERAGE: a scene may be a complete 6-12 second list item. For every "
        "candidate, return `covers` containing only the required visible elements or concrete "
        "sub-actions unmistakably visible in that exact segment. Do not claim that a machine "
        "exterior covers hands pressing its buttons, or that a finished bowl covers purchasing "
        "the ticket. The editor uses this to combine overview -> use -> result shots.\n\n"
        "SEGMENTS:\n" + "\n".join(seg_lines) + "\n\nSCENES:\n" + "\n".join(intent_lines) + "\n\n"
        'Return STRICT JSON: {"scenes": {"1": [{"seg": <seg index>, "subject_match":0-10,'
        '"action_match":0-10,"location_match":0-10,"mood_match":0-10,"script_match":0-10,'
        '"style_match":0-10,"literal_match":true|false,'
        '"visible_evidence":["explicitly observed subject/action"],'
        '"missing_required":["required element not visibly proven"],'
        '"covers":["exact visible element/action"],"reason":"short factual explanation"}], '
        '"2": [...]}} - keys are the BARE scene numbers '
        'shown above (digits only, never "scene 1"); list up to 4 candidates per scene, best first.\n'
        "COVERAGE (read this last, it overrides any urge to be terse): return your 3 BEST segments "
        "for EVERY scene, ranked, even when none of them is a good match - a low score is how you "
        "say \"this is weak\", an omission is not. The editor discards everything under its own "
        "threshold, so an imperfect candidate costs nothing while a missing one leaves that beat "
        "with no picture at all. Judged against 29 real segments this instruction was the "
        "difference between 4 candidates and full coverage. Only return an empty list for a scene "
        "when not one segment shows anything even loosely related to it.")
    data = _llm_json([{"role": "system", "content": (
                          "You are a precise, well-calibrated footage-to-script matcher. Do not treat "
                          "semantic association, keyword overlap or inferred object purpose as proof - "
                          "report what is visibly there and what is missing, and use the whole 0-10 "
                          "range so the editor can rank. The editor, not you, decides what is good "
                          "enough. JSON only.")},
                      {"role": "user", "content": prompt}],
                     max_tokens=6000, temperature=0.1, reasoning_model=reasoning_model,
                     status_cb=status_cb, label="scene matcher")
    smap = _scene_candidate_map(data)
    if not smap:
        # Say WHAT came back. "no usable data" with no shape named is how this sat
        # undiagnosed: every segment scored 0 for every scene, the run reported "matched
        # 0/14", and it looked like footage that did not fit.
        _shape = (f"{type(data).__name__} keys={sorted(data)[:6]}" if isinstance(data, dict)
                  else type(data).__name__)
        _log(status_cb, "Scrape V2: WARNING - scene matcher returned NO usable data for "
                        f"{len(described)} segment(s) (got {_shape}); these segments scored "
                        "0 for every scene.")
    intent_by_id = {it.scene_id: it for it in intents}
    out = {}
    near_misses = {}          # scene_id -> best scores seen, matched or not
    _intent_tokens_by_scene = {}      # concrete nouns per beat, for the near-miss evidence check
    dropped = {}              # why the code threw away an answer the model DID give
    empty_answers = []        # scenes the model explicitly answered with an empty list
    for sid_str, cands in smap.items():
        try:
            # models routinely answer with "scene 3" / "Scene_3" instead of "3" - a strict
            # int() threw ValueError and silently DISCARDED every candidate of that scene
            sid = int(re.sub(r"[^0-9-]", "", str(sid_str)) or "x")
        except (TypeError, ValueError):
            # These two paths had NO counter, so an answer keyed by something unexpected
            # vanished without a trace and the run reported "the matcher judged the segments
            # off-topic" - the one message that is certainly wrong when the model answered.
            dropped[f"unreadable scene key {sid_str!r}"] = (
                dropped.get(f"unreadable scene key {sid_str!r}", 0) + 1)
            continue
        it = intent_by_id.get(sid)
        if it is None:
            dropped[f"answer for scene {sid}, which is not in this batch "
                    f"({sorted(intent_by_id)})"] = 1 + dropped.get(
                        f"answer for scene {sid}, which is not in this batch "
                        f"({sorted(intent_by_id)})", 0)
        elif not isinstance(cands, list):
            dropped[f"scene {sid} value was {type(cands).__name__}, not a list"] = (
                dropped.get(f"scene {sid} value was {type(cands).__name__}, not a list", 0) + 1)
        if it is None or not isinstance(cands, list):
            continue
        if not cands:
            # An explicit empty array is the model USING the escape hatch, not a parse failure and
            # not a discard - the last case in this loop that no counter described, so a run where
            # the model simply declined every scene looked exactly like a broken pipeline.
            empty_answers.append(sid)
        scored = []
        near_rows = []          # below the floor, used only to stop a beat being empty
        for c in cands:
            if not isinstance(c, dict):
                continue
            try:
                seg = seg_by_id[int(c.get("seg"))]
            except (KeyError, TypeError, ValueError):
                dropped["unknown segment index"] = dropped.get("unknown segment index", 0) + 1
                continue
            # scene_ids is PROVENANCE - the search that happened to surface this clip - and every
            # query is scene-bound, so every segment carried exactly one scene id. Enforcing it
            # here meant a perfect vending-machine clip found by beat 2's query could never be
            # offered to beat 1: the model answered, the code deleted the answer, and the run
            # then reported "the matcher judged the segments off-topic". Re-use across beats is
            # already prevented downstream, by the strict one-source-per-scene assigner.
            _reason = editorial_rejection_reason(seg, it)
            # Provenance is a SEARCH-time rule and it already ran there: a Japan-specific query
            # refuses candidates without a Japanese caption before anything is downloaded. Running
            # it again here vetoed the corrective round by construction - those adaptive queries
            # are deliberately English, their results are legitimately English-captioned, they get
            # downloaded and described, and then every one of them is deleted at match time. That
            # is why a corrective round could report "recovered 0/4" after finding good footage.
            # Once vision has SEEN the clip, the picture outranks the caption language; keep it,
            # ranked below an equally good clip with native provenance.
            _weak_provenance = _reason == "no Japanese source-context signal"
            if _reason and not _weak_provenance:
                dropped[_reason] = dropped.get(_reason, 0) + 1
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
            _cat = str(getattr(it, "match_category", "") or "").lower()
            _rel = getattr(it, "script_relevancy", 70)
            _role = str(getattr(it, "communication_role", "") or "").lower()
            _strict_evidence = (float(_rel or 0) >= 85 and
                                (_cat == "literal" or it.visual_type == "concrete"
                                 or _role in ("proof", "demonstration")))
            _literal = c.get("literal_match") is True
            _evidence = [str(value).strip() for value in (c.get("visible_evidence") or [])
                         if str(value).strip()][:8]
            _missing = [str(value).strip() for value in (c.get("missing_required") or [])
                        if str(value).strip()][:8]
            _covers = [str(value).strip() for value in (c.get("covers") or [])
                       if str(value).strip()][:8]
            _allows_partial = bool(getattr(it, "allows_partial_coverage", False))
            # A NAMED FEATURE THE CLIP DOES NOT SHOW IS NOT AN EXACT MATCH. "blue lights on their
            # platforms" was filled with a plain platform at 7.1 because `missing_required` only
            # ranked. Demote to context instead of accepting or deleting: the footage is still
            # about the topic, it is just not the shot of the thing the sentence names.
            if names_a_feature_it_cannot_show(it, _evidence, _covers, _missing):
                _floor_ctx = match_thresholds_for_relevancy(it.visual_type, _rel)["script_floor"]
                script_m = min(script_m, max(NEAR_MISS_FLOOR, _floor_ctx - 0.3))
                overall = min(overall, semantic_match_score(subj, act, loc, mood, script_m))
                _literal = False
            # Gemini's numeric score is not allowed to overrule its own evidence. This catches
            # exactly the bad run where "man holding a food container in a kitchen" received
            # 7.6 for "handbag in a basket under a restaurant chair".
            #
            # But SILENCE IS NOT EVIDENCE OF FAILURE. Demoting a candidate because the answer
            # simply did not carry the contract fields made the whole gate a kill switch: the
            # slider defaults to 90, every concrete beat is therefore "strict", the demotion
            # capped script_match at 3.0 while the concrete floor at 90 is 6.2, and the
            # near-miss rescue below was disabled for exactly these beats. That combination
            # cannot match anything - measured on the coming-of-age run: 83 segments passed
            # quality, 0 passed the matcher, all 6 beats "uncovered". Only judge the contract
            # when the model actually answered it.
            _answered_contract = ("literal_match" in c) or bool(_evidence) or bool(_missing)
            # `missing_required` is a RANKING signal, not a veto. A beat lists several required
            # elements ("flags", "names on the flags", "procession"), so real footage almost
            # always misses one - treating that as failure demoted every candidate of every
            # scene to just under the floor and turned the whole run into WEAK rescues. What
            # actually matters is the model's own verdict on the central subject+action, which
            # is exactly what literal_match reports.
            # A confirmed subject in the confirmed place is a match even when the action is a
            # NEIGHBOUR of the one written down. Measured on the konbini pool: the beat "staff
            # restock the shelves" had 14 fitting segments - including a uniformed shop worker
            # reaching for a box inside a convenience store - and came back empty at 3.0/4.3,
            # because that is not literally "arranging products on shelves". No editor would
            # reject that shot. The original protection ("a handbag under a chair" scoring 7.6)
            # is unaffected: there the SUBJECT is wrong, and a wrong subject still fails below.
            # Missing secondary attributes lower the model's scores but are not a veto. The model's
            # literal_match already answers whether the central visible subject+action are proven.
            # The previous extra `_missing` veto rejected a person lying on a pavement because a
            # still frame could not prove his occupation, and rejected an izakaya drinking scene
            # because it could not prove who was the boss: 61 technical passes, 0 semantic passes.
            # Vision sometimes marks literal_match=false merely because a secondary attribute
            # ("salaryman", "boss", exact relationship) is invisible. Preserve a clearly visible
            # human central action when its own numeric evidence is strong; a wrong object or a
            # different action still fails these floors and cannot leak in as filler.
            # A high numeric score alone cannot turn a loosely associated object into proof
            # (e.g. a food container into a handbag basket). The relaxed branch is only for
            # visible evidence that directly covers at least one required concrete element.
            _central_action_is_clear = (bool(_evidence) and bool(_covers)
                                        and subj >= 5.0 and act >= 6.0)
            _evidence_fails = _answered_contract and (not _evidence or
                                                      (not _literal and not _central_action_is_clear))
            if _strict_evidence and (_evidence_fails or subj < 5.5 or act < 5.0):
                # Demote, do not annihilate: this candidate may no longer be presented as an
                # exact match, but it stays inside near-miss range so a beat that would
                # otherwise be EMPTY can still borrow it instead of replaying a neighbour.
                _floor_now = match_thresholds_for_relevancy(it.visual_type, _rel)["script_floor"]
                script_m = min(script_m, max(NEAR_MISS_FLOOR, _floor_now - 0.3))
                overall = semantic_match_score(subj, act, loc, mood, script_m)
            if _weak_provenance:
                # Native-provenance footage still wins a tie; this one only gets used when the
                # beat has nothing better, which is exactly what the old veto made impossible.
                overall = max(0.0, overall - 0.6)
            # Remember the best REJECTED pair too. "matched 0/6 after floors" is not
            # diagnosable on its own: a run where the best candidate scored 5.4 against a
            # 5.9 floor and a run where it scored 1.8 need opposite fixes, and the report
            # could not tell them apart.
            _near = near_misses.setdefault(sid, {"best_script": 0.0, "best_overall": 0.0,
                                                 "judged": 0})
            _near["judged"] += 1
            _near["best_script"] = max(_near["best_script"], float(script_m or 0))
            _near["best_overall"] = max(_near["best_overall"], float(overall or 0))
            if _cat == "shock":
                th_s = match_thresholds_for_relevancy("shock", _rel)
                try:
                    _absurd = float((seg.visual_description or {}).get("absurdity") or 0)
                except (TypeError, ValueError):
                    _absurd = 0.0
                if not (script_m >= th_s["script_floor"] and overall >= th_s["overall"]
                        and _absurd >= 6.0):
                    continue
            elif not passes_match_floors(script_m, overall, it.visual_type, _rel):
                # KEEP THE NEAR MISS. It is only used if this beat would otherwise end up
                # with nothing, and "nothing" does not mean a blank - it means the beat
                # borrows a neighbour's clip. Measured on the suppin run: 96 segments
                # passed quality, 7 cleared these floors, and twelve beats then replayed
                # two clips. A 6.4 that shows the thing being narrated beats the same
                # phone for the eighth time, so the honest comparison is not
                # "match vs no match" but "weak match vs a repeat".
                # The rescue used to switch itself off at relevancy >= 85, i.e. exactly where
                # the strict demotion above was active - so the strictest runs were the ones
                # left with nothing at all. High relevancy should mean "rank a weak clip last",
                # never "leave the beat blank".
                # A low numerical score may be a useful last-resort neighbour. An explicit
                # evidence contradiction (wrong object/action, literal_match=false) is not a
                # near miss and must never re-enter the timeline through this rescue path.
                # `_evidence_fails` fires whenever vision said literal_match=false, which is
                # also exactly what happens to a beat written as an action nobody filmed. On the
                # Tokyo hotel run the pool held ten shots of dinosaurs inside a dinosaur-themed
                # hotel and the beat asked for the dinosaur to MOVE behind the desk and the guest
                # to SPEAK to it; every one was vetoed here, so the rescue that exists to stop an
                # empty beat was switched off by the very condition that empties it.
                # A wrong SUBJECT is a contradiction and still stays out. The right subject in
                # the right place doing a neighbouring action is the definition of a near miss.
                #
                # The model's own numbers cannot draw that line: it scored subject_match 8.0 for
                # "man holds food container in kitchen" against "handbag in a basket under a
                # restaurant chair", which is the exact association this gate was built to stop.
                # Its written EVIDENCE can. Both cases look identical in the contract fields -
                # every required element missing, covers empty - but the dinosaur evidence names
                # the beat's own nouns ("dinosaur figure at a hotel reception desk") and the
                # food-container evidence names none of them.
                _tokens = _intent_tokens_by_scene.get(sid)
                if _tokens is None:
                    _group = _intent_context_group(it)
                    _tokens = tuple(_group[0]) if _group else ()
                    _intent_tokens_by_scene[sid] = _tokens
                _evidence_text = " ".join(_evidence).casefold()
                _subject_seen = any(str(tok).casefold() in _evidence_text for tok in _tokens)
                _subject_confirmed = subj >= 5.5 and loc >= 4.5 and _subject_seen
                if overall >= NEAR_MISS_FLOOR and (not _evidence_fails or _subject_confirmed):
                    near_rows.append({
                        "segment": seg, "subject_match": subj, "action_match": act,
                        "location_match": loc, "mood_match": mood, "script_match": script_m,
                        "style_match": style_m, "semantic_match": overall,
                        "overall_match": overall, "striking": 0.0,
                        "below_floor": True, "match_class": "WEAK",
                        "reason": "below the floor, kept in case the beat would be empty"})
                continue
            try:
                striking = float((seg.visual_description or {}).get("striking") or 0.0)
            except (TypeError, ValueError):
                striking = 0.0
            scored.append({
                "segment": seg, "subject_match": subj, "action_match": act, "location_match": loc,
                "mood_match": mood, "script_match": script_m, "style_match": style_m,
                "semantic_match": overall, "overall_match": overall, "striking": striking,
                "covers": _covers,
                "visible_evidence": _evidence, "missing_required": _missing,
                "literal_match": _literal,
                "match_class": match_class_for(overall, it.visual_type, _rel),
                "reason": str(c.get("reason", ""))[:160]})
            seg.semantic_score = max(seg.semantic_score, overall)
        # Among clips that all fit the beat, prefer the one worth stopping for. `striking`
        # ranks but never gates: a correct-and-dull shot still beats no shot, and blocking
        # on it would be the caption gate all over again.
        #
        # The match is rounded to a WHOLE point first. Half-point buckets were tried and
        # are useless here - 7.4 and 7.2 fall either side of 7.5, so a two-tenths scoring
        # wobble still outranked a clip that was seven points more watchable, which is the
        # exact situation this tie-break exists for.
        # Someone else's burned-in captions rank LAST among equals. The OCR area score is
        # useless for this - it read 0.00 on a romance_down_to_yen clip with a Japanese
        # subtitle across the middle of frame - but the vision stage already answers
        # burned_captions per segment, and that is the signal used here. It ranks, it never
        # gates: a captioned clip still beats no clip, so this cannot starve a beat.
        def _clean(row):
            d = row["segment"].visual_description or {}
            return 0 if (d.get("burned_captions") or d.get("creator_overlay")) else 1

        scored.sort(key=lambda r: (round(r["overall_match"]),
                                   _clean(r),
                                   r["striking"],
                                   r["segment"].quality_score), reverse=True)
        # A beat with nothing does not go blank - it replays a neighbour's clip. So when
        # nothing cleared the floor, the best near miss is carried forward instead, marked
        # WEAK. It is still a picture of what is being said; a repeat is not.
        if not scored and near_rows:
            near_rows.sort(key=lambda r: (r["overall_match"], r["segment"].quality_score),
                           reverse=True)
            scored = near_rows[:1]
            _log(status_cb, "Scrape V2: scene %s had no clip over the floor; keeping its best "
                            "near miss at %.1f rather than repeating another beat's footage."
                 % (sid, scored[0]["overall_match"]))
        out[sid] = scored

    # The vision matcher is intentionally conservative about relationship details: a close-up of
    # somebody selecting boxed chocolate cannot prove that a girlfriend gives it to *her* partner.
    # It must not, however, erase an otherwise excellent, scene-bound chocolate/gift insert and
    # leave the beat blank.  Recover only directly visible supporting objects/actions from the
    # query that was issued FOR THIS scene.  This is explicitly a context fallback, never an
    # "exact" relationship claim, and does not allow generic people/talking heads back in.
    _context_groups = (
        (("valentine", "chocolate", "white day", "gift", "present", "sweets", "cookie", "confection"),
         ("chocolate", "cocoa", "confection", "candy", "sweet", "cookie", "boxed", "box", "package", "gift"),
         ("バレンタイン", "チョコ", "ホワイトデー", "義理チョコ", "お菓子", "ギフト", "お返し")),
        (("train", "station", "commuter", "platform", "rail"),
         ("train", "station", "platform", "commuter", "carriage", "rail"),
         ("電車", "満員電車", "通勤", "駅", "ホーム")),
        (("christmas", "eve", "lights", "winter"),
         ("christmas", "light", "decoration", "winter", "tree", "ornament"),
         ("クリスマス", "イルミネーション", "冬")),
        (("couple", "dating", "girlfriend", "boyfriend", "romance", "affection"),
         ("couple", "man and woman", "two people", "holding hands", "date"),
         ("カップル", "デート", "彼氏", "彼女", "恋人")),
    )
    for it in intents:
        if out.get(it.scene_id):
            continue
        intent_text = " ".join(str(getattr(it, key, "") or "") for key in
                               ("scene_text", "story_subject", "local_claim", "subject", "action")).casefold()
        hits_needed = [group for group in _context_groups
                       if any(token in intent_text for token in group[0])]
        if not hits_needed:
            # THE TABLE IS A BOOST, NOT THE GATE. Chocolate, trains, Christmas and couples are
            # the four topics that happened to get debugged; every other subject fell straight
            # through to borrowing a neighbour's clip. The Tokyo hotel run is the proof - the
            # pool held dinosaur-hotel, self-checkout and parking-tower footage and not one beat
            # could reach it. Build the same three-part group out of THIS beat's own words.
            _own = _intent_context_group(it)
            if not _own:
                continue
            hits_needed = [_own]
        recovered = []
        for seg in described:
            # First prefer the exact scene-bound search, but do not pretend that a good
            # chocolate/display/train insert becomes unusable merely because it was found for
            # the adjacent sentence. The global allocator still enforces one source per scene.
            # This is the path that prevents ten empty beats from borrowing the same four clips.
            is_scene_bound = it.scene_id in (seg.scene_ids or [])
            source_text = str(getattr(seg, "query", "") or "").casefold()
            source_support = any(any(anchor in source_text for anchor in anchors)
                                 for _intent, _evidence, anchors in hits_needed)
            if not is_scene_bound and not source_support:
                continue
            if editorial_rejection_reason(seg, it):
                continue
            desc = seg.visual_description or {}
            observed = " ".join(str(desc.get(key) or "") for key in
                                ("subjects", "action", "location")).casefold()
            strength = max((sum(token in observed for token in evidence)
                            for _intent, evidence, _anchors in hits_needed), default=0)
            # One exact concrete object is enough for a supporting insert (e.g. chocolate);
            # generic room/people footage still has zero strength and remains rejected.
            if strength < 1:
                continue
            score = min(6.0, 4.75 + 0.55 * strength + 0.08 * float(seg.quality_score or 0.0)
                        - (0.35 if not is_scene_bound else 0.0))
            recovered.append({
                "segment": seg, "subject_match": score, "action_match": score,
                "location_match": 4.5, "mood_match": 5.0, "script_match": score,
                "style_match": 5.0, "semantic_match": score, "overall_match": score,
                "striking": float(desc.get("striking") or 0.0),
                "covers": [], "visible_evidence": [], "missing_required": [],
                "literal_match": False, "match_class": "C_MATCH",
                "assignment_type": "context_fallback",
                "reason": ("scene-bound visible context insert; relationship detail not inferred"
                           if is_scene_bound else
                           "adjacent-topic visible context insert; relationship detail not inferred"),
            })
        if recovered:
            recovered.sort(key=lambda row: (row["overall_match"], row["segment"].quality_score), reverse=True)
            out[it.scene_id] = recovered[:3]
            _log(status_cb, "Scrape V2: scene %s recovered %d concrete scene-bound context "
                            "insert(s) after the literal matcher returned none."
                 % (it.scene_id, len(out[it.scene_id])))
    matched = sum(1 for v in out.values() if v)
    _log(status_cb, f"Scrape V2: matched {matched}/{len(intents)} scene(s) after floors.")
    # Two very different failures look identical in that count, and the near-miss report
    # below can only describe one of them: a scene the matcher OFFERED candidates for that
    # then failed the floor, versus a scene it declined to offer anything for at all. The
    # first is a threshold to argue about, the second is the model saying the footage is
    # off-topic - opposite fixes, so say which happened.
    if dropped:
        # Report discards ALWAYS, not only when a scene ends up completely silent. A beat that
        # kept one candidate and lost twelve looks healthy in the counters and is not.
        _log(status_cb, "Scrape V2: the pipeline discarded %d matcher answer(s): %s."
             % (sum(dropped.values()),
                ", ".join(f"{count}x {reason}" for reason, count
                          in sorted(dropped.items(), key=lambda kv: -kv[1])[:6])))
    silent = [it.scene_id for it in intents if it.scene_id not in near_misses]
    if silent:
        # Only call it "off-topic" when the model really said nothing. If the code deleted the
        # answers, say THAT instead - the two need opposite fixes and the old message always
        # blamed the footage.
        if dropped:
            _log(status_cb, "Scrape V2: %d/%d scene(s) ended with no candidate, but the matcher DID "
                            "answer for them - the pipeline discarded %d candidate(s): %s."
                 % (len(silent), len(intents), sum(dropped.values()),
                    ", ".join(f"{count}x {reason}" for reason, count
                              in sorted(dropped.items(), key=lambda kv: -kv[1])[:5])))
        elif empty_answers:
            _log(status_cb, "Scrape V2: the matcher returned an EMPTY list for "
                            f"{len(empty_answers)}/{len(intents)} scene(s): {empty_answers[:12]} - "
                            f"it read all {len(described)} segment(s) and judged none of them "
                            "related. The pool is the problem here, not a threshold.")
        else:
            _log(status_cb, "Scrape V2: the matcher offered NO candidate at all for "
                            f"{len(silent)}/{len(intents)} scene(s): {silent[:12]} - it judged "
                            "the segments off-topic for them, which no threshold will change.")
    if matched < len(intents) and near_misses:
        # Say HOW FAR the misses were, in the same breath as the count.
        misses = [(sid, n) for sid, n in near_misses.items() if not out.get(sid)]
        if misses:
            worst = sorted(misses, key=lambda kv: -kv[1]["best_overall"])[:4]
            th = match_thresholds_for_relevancy("context",
                                                getattr(intents[0], "script_relevancy", 70))
            _log(status_cb, "Scrape V2 near misses (floor script>=%.1f overall>=%.1f): %s"
                 % (th["script_floor"], th["overall"],
                    "; ".join("scene %s best %.1f/%.1f of %d judged"
                              % (sid, n["best_script"], n["best_overall"], n["judged"])
                              for sid, n in worst)))
    # THE DIAGNOSIS HAS TO LEAVE THIS FUNCTION. "88 quality segments, 5 candidates" is not
    # actionable on its own - a run where the best clip scored 5.4 against a 5.9 floor and a run
    # where the code deleted the model's answer need opposite fixes. Until now both numbers only
    # existed in a status line, and the server log is empty by the time anyone looks.
    if stats is not None:
        _judged = sum(int(n.get("judged") or 0) for n in near_misses.values())
        stats["judged"] = int(stats.get("judged", 0) or 0) + _judged
        stats["kept"] = int(stats.get("kept", 0) or 0) + sum(len(v) for v in out.values())
        for reason, count in dropped.items():
            stats.setdefault("dropped", {})
            stats["dropped"][reason] = stats["dropped"].get(reason, 0) + count
        if empty_answers:
            stats["declined_scenes"] = sorted(
                set(stats.get("declined_scenes") or []) | set(empty_answers))
        _best = stats.setdefault("best_by_scene", {})
        for sid, n in near_misses.items():
            row = _best.setdefault(str(sid), {"best_script": 0.0, "best_overall": 0.0, "judged": 0})
            row["best_script"] = max(row["best_script"], float(n.get("best_script") or 0.0))
            row["best_overall"] = max(row["best_overall"], float(n.get("best_overall") or 0.0))
            row["judged"] = row["judged"] + int(n.get("judged") or 0)
    return out


# ---------------------------------------------------------------- global assignment (PURE-ish)

def assign_segments_globally_v2(intents, scene_candidates, cfg=None, status_cb=None):
    """Global assignment: maximise total relevance while enforcing diversity + reuse caps. Improved
    greedy with backtracking (a scene can steal a segment from a scene that still has alternatives).
    Returns {scene_id: SceneAssignment}. PURE: operates on already-scored candidates."""
    cfg = cfg or SCRAPE_V2_CONFIG
    intent_by_id = {it.scene_id: it for it in intents}
    order = sorted(intent_by_id.keys())
    required_duration = dict(cfg.get("required_duration_by_scene") or {})
    montage_scene_ids = {int(value) for value in (cfg.get("visual_chapter_scene_ids") or [])}

    # edges: (assignment_score, scene_id, cand) best first
    edges = []
    for sid, cands in (scene_candidates or {}).items():
        for c in cands:
            seg = c["segment"]
            # Never assign a discovery window that is shorter than the final narration chapter.
            # The old finalizer silently extended it beyond the inspected window and crossed into
            # the source video's next scene immediately before our cut.
            need = float(required_duration.get(str(sid), required_duration.get(sid, 0.0)) or 0.0)
            if (need and sid not in montage_scene_ids
                    and float(seg.duration or 0.0) + 0.05 < need
                    and float(seg.duration or 0.0) < need * SHORT_CLIP_STRETCH_FLOOR):
                continue
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

    def _same_topic(sid_a, sid_b):
        """Do these two beats describe the same thing? Their own concrete nouns decide."""
        a, b = intent_by_id.get(sid_a), intent_by_id.get(sid_b)
        if a is None or b is None:
            return False
        ga, gb = _intent_context_group(a), _intent_context_group(b)
        if not ga or not gb:
            return False
        return bool(set(ga[0]) & set(gb[0]))

    def _second_window_ok(seg, sid):
        """A second excerpt of an already-used source, only as a genuinely different moment."""
        gap = float(cfg.get("same_source_window_gap_seconds", 2.0) or 0.0)
        for other_sid, other in assignments.items():
            other_seg = getattr(other, "_seg", None)
            if other_seg is None or other_seg.source_id != seg.source_id:
                continue
            if not _same_topic(sid, other_sid):
                return False                      # unrelated beat: the strict rule still applies
            if is_near_duplicate(other_seg, seg, cfg["max_near_duplicate_similarity"]):
                return False
            # the windows must not overlap, and must sit clearly apart in the source
            if not (float(seg.start_time) >= float(other_seg.end_time) + gap
                    or float(other_seg.start_time) >= float(seg.end_time) + gap):
                return False
            if (getattr(seg, "frame_hash", "") and
                    getattr(seg, "frame_hash", "") == getattr(other_seg, "frame_hash", "")):
                return False
        return True

    def _caps_ok(seg, cand_class, sid):
        # ONE scene per segment. One clip per SOURCE video, EXCEPT for a beat about the same
        # subject: there a second, non-overlapping, visually different window is continuity
        # rather than the video repeating itself (see max_segments_per_source_same_topic).
        if seg_use.get(seg.segment_id, 0) >= 1:
            return False
        used = source_use.get(seg.source_id, 0)
        if used >= max(1, int(cfg["max_segments_per_source_final"])):
            if used >= max(1, int(cfg.get("max_segments_per_source_same_topic", 1))):
                return False
            if not _second_window_ok(seg, sid):
                return False
        if creator_use.get(seg.creator_id, 0) >= cfg["max_clips_per_creator"]:
            return False
        return True

    for a_score, sid, c in edges:
        a = assignments[sid]
        if a.segment_id:                                  # scene already filled
            continue
        seg = c["segment"]
        if not _caps_ok(seg, c["match_class"], sid):
            continue
        if _adjacent_dupe(sid, seg):
            continue
        a.segment_id = seg.segment_id
        a.final_path = seg.final_path or seg.source_path
        a.assignment_type = c.get("assignment_type") or "exact"
        a.fallback_level = int(c.get("fallback_level") or 0)
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

def _clip_seconds(path):
    """Real duration of a clip file, or 0.0. Scenes do not carry one.

    The first version of the borrow read scene["source_duration"], which nothing writes onto
    a scene - so the in-point shift never happened and every borrowed beat was the donor's
    identical three seconds. The unit test passed only because its fixture invented the key.
    """
    if not path:
        return 0.0
    try:
        out = subprocess.run(
            [str(clip_scraper._ffmpeg_tools()[1]), "-v", "error", "-show_entries",
             "format=duration", "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True, timeout=30).stdout.strip()
        return max(0.0, float(out))
    except Exception:      # noqa: BLE001 - a missing probe must not break the borrow
        return 0.0


def _intent_context_group(intent):
    """Build a (trigger, evidence, anchors) group from a beat's own subject and location.

    The hand-written `_context_groups` table only covers four topics.  This derives the same
    shape for any beat: the concrete nouns it names become the evidence tokens a recovered
    segment must visibly show, and they double as the query anchors.  Generic words are
    excluded, so a beat whose subject is "person" or "thing" still recovers nothing.
    """
    text = " ".join(str(getattr(intent, key, "") or "") for key in
                    ("subject", "story_subject", "action", "location"))
    generic = {"person", "people", "man", "woman", "adult", "someone", "traveler", "traveller",
               "customer", "shopper", "user", "japan", "japanese", "tokyo", "thing", "object",
               "camera", "scene", "shot", "view", "area", "place", "indoor", "outdoor"}
    latin = [w for w in re.findall(r"[a-z]{4,}", text.casefold())
             if w not in generic and w not in _EN_STOP]
    native = re.findall(r"[぀-ヿ㐀-鿿]{2,}", text)
    tokens = []
    for word in latin + native:
        if word not in tokens:
            tokens.append(word)
    if len(tokens) < 2:
        return None
    tokens = tokens[:6]
    return (tuple(tokens), tuple(tokens), tuple(tokens))


def _publish_scene_clip(scene_clips, idx, value):
    """Write an assigned clip into the parallel path list the caller reads.

    The list and the scene dict are two views of the same decision, and only the scene dict was
    being updated by the borrow/spare paths. agent_core rebuilds the timeline from the LIST, so
    a beat that borrowed successfully arrived with nothing and was reset to a blank scene.
    """
    if scene_clips is None or not value:
        return False
    try:
        while len(scene_clips) <= int(idx):
            scene_clips.append(None)
        scene_clips[int(idx)] = str(value)
        return True
    except (TypeError, ValueError):
        return False


def borrow_motion_for_uncovered(scenes, scene_clips=None, spare_for_scene=None):
    """Give every footage-less beat a moving picture borrowed from its nearest neighbour.

    The user's rule is clips only. The previous behaviour put a still on screen for any beat
    the search could not cover, and on the tokyo_love_goes_private project that was thirteen
    beats out of fourteen - the short played as a slideshow.

    A beat borrows from the nearest beat that has real footage, because in a narration the
    beats either side share the place and the people: the purikura beats are all the same
    booth. The in-point is shifted so two beats are not the same three seconds twice.

    The borrow is never dressed up as a match. match_class becomes BORROWED, the original
    uncovered reason stays on the scene, and the caller still counts the beat as unmatched.
    UNUSED FOOTAGE COMES FIRST. Borrowing means the same source appears twice, and on the
    dating run two pairs of beats ended up on the same TikTok while 62 of the 64 quality-passed
    segments were never touched. `spare_pool` is that leftover: fresh, already-downloaded,
    already-quality-checked material from a source no beat is using. A duplicate is only
    acceptable once that pool is empty.

    Returns the number of beats that borrowed; beats with nothing to borrow from keep their
    still, because a still beats a black frame.
    """

    donors = [(i, sc) for i, sc in enumerate(scenes)
              if sc.get("clip") and str(sc.get("assignment_type") or "") != "uncovered_still"]
    if not donors:
        return 0
    # SPREAD THE BORROWS. Taking the nearest donor every time put ONE clip on eight
    # consecutive beats of romance_down_to_yen - thirty-six seconds of video containing two
    # pictures. Least-used donor first, nearest as the tie-break: locality is kept where it
    # is free, but no single source can carry the whole short.
    def _donor_key(row):
        sc = row[1]
        return str(sc.get("scrape_clip_id") or sc.get("clip") or row[0])

    uses = {}
    for _i, sc in enumerate(scenes):
        if sc.get("clip") and str(sc.get("assignment_type") or "") != "uncovered_still":
            uses[str(sc.get("scrape_clip_id") or sc.get("clip") or _i)] = uses.get(
                str(sc.get("scrape_clip_id") or sc.get("clip") or _i), 0) + 1

    used_sources = {str(sc.get("scrape_clip_id") or "") for sc in scenes
                    if sc.get("clip") and sc.get("scrape_clip_id")}

    borrowed = 0
    for idx, scene in enumerate(scenes):
        if str(scene.get("assignment_type") or "") != "uncovered_still":
            continue
        # A fresh source beats a repeat ONLY when it is also relevant to this exact line, so
        # the caller hands over this beat's own best unused candidate - never a clip picked
        # for looking sharp. No relevant leftover means we borrow, because a repeat of the
        # right picture is worth more than a pristine shot of something else.
        try:
            need = max(0.0, float(scene.get("end") or 0.0) - float(scene.get("start") or 0.0))
        except Exception:
            need = 0.0
        fresh = spare_for_scene(idx) if spare_for_scene else None
        if fresh is not None and str(fresh.get("scrape_clip_id") or "") not in used_sources:
            used_sources.add(str(fresh.get("scrape_clip_id") or ""))
            scene["clip"] = fresh["clip"]
            scene["asset"] = fresh.get("asset") or fresh["clip"]
            scene["assignment_type"] = "spare_clip"
            scene["match_class"] = "SPARE"
            scene["scrape_clip_id"] = fresh.get("scrape_clip_id")
            _publish_scene_clip(scene_clips, idx, fresh.get("path") or fresh.get("clip"))
            _retime_short_scene(scene, _clip_seconds(fresh.get("path") or fresh["clip"]), need)
            borrowed += 1
            continue
        # LENGTH IS PART OF BEING A DONOR. Ranking only by least-used and nearest handed beat 8
        # of the chocolate short the 1.53s hook clip for a 3.68s line: the dancer played for a
        # second and a half and then froze for 2.15 seconds. A donor that cannot fill the beat
        # is considered only once every donor that can has been passed over.
        def _donor_rank(row):
            row_idx = row[0]
            length = _clip_seconds(
                scene_clips[row_idx] if (scene_clips is not None and row_idx < len(scene_clips))
                else row[1].get("clip"))
            too_short = bool(need > 0 and length > 0 and length < need * SHORT_CLIP_STRETCH_FLOOR)
            return (1 if too_short else 0, uses.get(_donor_key(row), 0), abs(row_idx - idx))

        donor_idx, donor = min(donors, key=_donor_rank)
        uses[_donor_key((donor_idx, donor))] = uses.get(_donor_key((donor_idx, donor)), 0) + 1
        scene["clip"] = donor["clip"]
        scene["asset"] = donor.get("asset") or donor["clip"]
        scene["assignment_type"] = "borrowed_clip"
        scene["match_class"] = "BORROWED"
        scene["borrowed_from_scene"] = donor_idx
        # Carry the donor's identity, or nothing downstream can tell these apart. The
        # caption pre-pass renames each scene's clip to capblur_<sid>_<hash>.mp4 - a
        # different filename per scene - and both the duplicate guard and the renderer's
        # continuity offset key off the filename when scrape_clip_id is missing. Without
        # this, a timeline that is one clip fourteen times reports zero repeats and every
        # beat starts at the same frame.
        scene["scrape_clip_id"] = donor.get("scrape_clip_id")
        # the resolved path, not the basename - this is what the renderer reads. When the donor
        # itself has no entry (it was written straight onto the scene), fall back to its name so
        # the caller still has SOMETHING to resolve; an empty entry is read as "no clip" and the
        # whole borrow is thrown away downstream.
        _donor_path = (scene_clips[donor_idx]
                       if (scene_clips is not None and donor_idx < len(scene_clips))
                       else None)
        _publish_scene_clip(scene_clips, idx, _donor_path or donor.get("clip"))
        src_len = _clip_seconds(scene_clips[donor_idx] if (scene_clips is not None
                                                          and donor_idx < len(scene_clips))
                                else donor.get("clip"))
        # Shift the in-point so two beats are not the same seconds twice. The first version
        # required src_len > 4.0 and wrote "source_trim": the donor is cut to roughly its own
        # beat length, so that gate could never be true, and source_trim is an SFX/editor key
        # the renderer never reads. seedance_start_trim is the one it honours.
        if src_len > 1.2:
            # Only shift into footage the beat does not need. The old rule spent up to a second
            # of head on a clip that was already too short, which turned a small hold at the end
            # into a long freeze.
            room = max(0.0, min(src_len - 1.0, src_len - need) if need > 0 else src_len - 1.0)
            if room > 0.1:
                step = max(0.3, min(1.5, room / 3.0))
                scene["seedance_start_trim"] = round((abs(idx - donor_idx) * step) % room, 2)
        _retime_short_scene(scene, src_len - float(scene.get("seedance_start_trim") or 0.0), need)
        borrowed += 1
    return borrowed


def _retime_short_scene(scene, available_seconds, need_seconds):
    """Slow a too-short clip down to cover its beat instead of freezing on the last frame.

    Assignment already does this; the borrow and spare paths wrote the clip straight onto the
    scene and never retimed, so a beat that was 15% under simply held a still. The floor is the
    same 12% as everywhere else - past that it reads as slow motion and the renderer holds the
    remainder, which is the honest outcome for a clip that cannot cover the line."""
    if need_seconds <= 0 or available_seconds <= 0 or available_seconds + 0.05 >= need_seconds:
        return False
    scene["timeline_speed"] = round(max(SHORT_CLIP_STRETCH_FLOOR,
                                        available_seconds / need_seconds), 4)
    scene["timeline_speed_src"] = str(scene.get("clip") or "")
    return True


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
    if not (0.999 <= vs <= 1.601):
        raise RuntimeError(f"Scrape V2 pre-render validation failed: voice_speed {config.get('voice_speed')} "
                           "outside 1.0x-1.6x (default 1.10x, user-tunable at the speech gate)")
    for i, sc in enumerate(scenes):
        for ov in (sc.get("overlays") or []):
            if ov.get("type") in ("arrows", "highlight", "paper", "newspaper", "counter"):
                raise RuntimeError("Scrape V2: untargeted/legacy overlay present")
            if ov.get("type") == "callout" and ov.get("shape") in ("circle", "stamp"):
                raise RuntimeError(f"Scrape V2: circle/stamp overlay on scene {sc.get('id')}")
    influencer_hook = bool(config.get("influencer_hook", False))
    expected_role = "hook_influencer" if influencer_hook else "hook_topic"
    if (scenes[0].get("visual_role") or "") != expected_role:
        # visual_role is descriptive metadata, not evidence that the timeline is
        # misordered. A stale/missing label must not discard an otherwise usable run.
        _log(status_cb, f"Scrape V2 WARNING: normalizing first scene visual_role to {expected_role}.")
        scenes[0]["visual_role"] = expected_role
    emergency_scenes = 0
    for i, sc in enumerate(scenes):
        if not sc.get("clip"):
            if str(sc.get("assignment_type") or "") == "uncovered_still":
                _log(status_cb, f"Scrape V2: scene {i} has no approved social clip; using its planned still-motion fallback.")
                continue
            raise RuntimeError(f"Scrape V2: scene {i} has no segment clip")
        atype = str(sc.get("assignment_type") or "exact")
        if atype == "emergency_fallback":
            emergency_scenes += 1
            raise RuntimeError(f"Scrape V2: scene {i} has an unrelated emergency fallback clip; "
                               "rerun this scene search instead of rendering filler.")
        if str(sc.get("match_class") or "") == "D_REJECTED":
            raise RuntimeError(f"Scrape V2: scene {i} retained footage rejected by semantic review; "
                               "rerun the targeted scene search instead of relabeling it as a match.")
        try:
            bbs = float(sc.get("black_bar_score") or 0)
        except (TypeError, ValueError):
            bbs = 0.0
        if sc.get("is_fake_vertical") or bbs > SEGMENT_HARD_REJECT_BLACKBAR:
            raise RuntimeError(f"Scrape V2: scene {i} massive black bars (score={bbs})")
        # Some platform downloads (notably X) have no reliable native-aspect metadata.
        # The black-bar/fake-vertical gates above are the actual visual safety check; only
        # reject a clip when inspection explicitly identified it as non-native.
        if sc.get("native_9_16") is False:
            raise RuntimeError(f"Scrape V2: scene {i} is not native 9:16 footage")
        if sc.get("native_9_16") is None:
            _log(status_cb, f"Scrape V2: scene {i} has unknown source aspect; "
                            "continuing with the 9:16 crop.")
    # Emergency scenes are a degraded but renderable result. Never turn matcher weakness into a
    # second hard failure after the scraper has already exhausted its search/escalation passes.
    if emergency_scenes * 2 > len(scenes):
        _log(status_cb,
             f"Scrape V2 WARNING: {emergency_scenes}/{len(scenes)} scenes use emergency "
             "fallback material; continuing with the best available clips.")
    config["pre_render_validation_passed"] = True
    _log(status_cb, "Scrape V2 pre-render validation passed.")
    return True


# ---------------------------------------------------------------- debug report

def warn_if_topic_is_unfilmable(state, status_cb=None):
    """Say early when a script's subject exists as a service but not as footage.

    Measured on two runs of this pipeline. The konbini/ecbo/iKasa script produced 519 raw search
    results - the best of any run - and not one of its twelve beats scored above 5.8 out of 10,
    because printing a certificate, booking luggage storage and unlocking an umbrella are posted
    as explainer videos with text cards, never as filmed action. Half of everything downloaded
    was a slideshow. A search-result COUNT cannot see that; the share of downloads thrown out for
    having no motion can, and it can see it within the first analysis round instead of after
    thirty-three minutes. This warns; it never aborts - the choice to keep going is the user's.
    """
    if state.get("_unfilmable_warned"):
        return False
    discovered = int(state.get("segments_discovered", 0) or 0)
    if discovered < 12:
        return False
    rejections = state.get("rejections") or {}
    # TWO DIFFERENT PROBLEMS, NOT ONE. `micro_freezes` is a slideshow or an explainer with text
    # cards - what a subject looks like when it exists as a service but nobody films it.
    # `black_bars` is landscape footage reposted into a portrait canvas, which says the topic is
    # newsworthy, not that it is unfilmable: the Shinkansen-cleaning run tripped a combined
    # threshold at 29% while its own footage was fine. Only the freeze share is evidence about
    # the SUBJECT.
    freezes = int(rejections.get("micro_freezes", 0) or 0)
    bars = int(rejections.get("black_bars", 0) or 0)
    # 25% is drawn from measured runs, so a first cut rather than a law:
    #   chocolate 13%  school lunch 6%  "3 things" 3%   <- all produced usable footage
    #   konbini/ecbo/iKasa 29% freezes                  <- produced none at all
    if freezes * 100 < discovered * 25:
        if bars * 100 >= discovered * 25:
            state["_unfilmable_warned"] = True
            _log(status_cb, "Scrape V2: %d of %d discovered segment(s) were landscape footage "
                            "letterboxed into a portrait canvas - reposted broadcast material "
                            "rather than someone's own phone video. The subject is filmed; its "
                            "best clips may simply not be native vertical."
                 % (bars, discovered))
        return False
    state["_unfilmable_warned"] = True
    _log(status_cb, "Scrape V2 WARNING: %d of %d discovered segment(s) were slideshows or "
                    "explainer videos with text cards rather than filmed action. That is what a "
                    "subject looks like when it exists as a service but nobody records it. The "
                    "search is working; the topic may not be filmable. Consider a fact whose core "
                    "is a machine somebody operates or an object changing hands."
         % (freezes, discovered))
    return True


def build_debug_report(state: dict) -> dict:
    """Assemble the structured V2 run report from the accumulated counters + per-scene assignments."""
    return {
        "engine": "scrape_v2",
        "analysis_version": V2_ANALYSIS_VERSION,
        "queries_executed": state.get("queries_executed", 0),
        "query_texts": list(state.get("query_texts") or []),
        "query_provenance": list(state.get("query_provenance") or []),
        "search_plan_audit": state.get("search_plan_audit") or {},
        "matcher": state.get("matcher_stats") or {},
        "downloads_by_scene": state.get("downloads_by_scene") or {},
        "sort_pass_counts": dict(state.get("sort_pass_counts") or {}),
        "raw_results": state.get("raw_results", 0),
        "raw_results_by_platform": dict(state.get("raw_results_by_platform") or {}),
        "metadata_candidates": state.get("metadata_candidates", 0),
        "downloaded_sources": state.get("downloaded_sources", 0),
        "segments_discovered": state.get("segments_discovered", 0),
        "segments_quality_passed": state.get("segments_quality_passed", 0),
        "segments_soft_quality_kept": state.get("segments_soft_quality_kept", 0),
        "scenes_borrowed_motion": state.get("scenes_borrowed_motion", 0),
        "scenes_still_fallback": state.get("scenes_still_fallback", 0),
        "segments_soft_artifact_kept": state.get("segments_soft_artifact_kept", 0),
        "segments_semantic_passed": state.get("segments_semantic_passed", 0),
        "scenes_exact_matched": state.get("scenes_exact_matched", 0),
        "scenes_alternative_matched": state.get("scenes_alternative_matched", 0),
        "scenes_context_fallback": state.get("scenes_context_fallback", 0),
        "scenes_unmatched": state.get("scenes_unmatched", 0),
        "scene_retries": state.get("scene_retries", 0),
        "rejections": state.get("rejections", {}),
        "topic_looks_unfilmable": bool(state.get("_unfilmable_warned")),
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
        likes=int(m.get("likes") or 0), views=int(m.get("views") or 0),
        duration=float(m.get("duration") or 0.0),
        width=int(m.get("w") or 0), height=int(m.get("h") or 0),
        query=query.query, query_tier=query.tier, scene_ids=list(query.scene_ids or []),
        japanese_context=has_japanese_source_signal({
            "caption": m.get("caption"), "hashtags": m.get("hashtags"),
            "author": m.get("author"), "author_name": m.get("author_name"),
            "location": m.get("location"),
        }), raw_item=item)


def _hook_presenter_queries_v2(hook_intent=None):
    """Dedicated OPENING-HOOK search queries: a young-adult Japanese female creator dancing /
    playfully acting cute to camera (idol energy). Topic-agnostic - the universal scroll-stopper.
    Reuses the same query pool as the V1 hook finder (agent_core.HOOK_PRESENTER_QUERIES)."""
    # A cute dance is a valid generic hook, but it becomes irrelevant for relationship scripts.
    # In that case search for a young Japanese woman *inside the subject matter* instead: couple
    # styling, date vlog or a couple photo. This preserves the human hook without misleading the
    # viewer before the first spoken claim.
    intent_text = " ".join(str(getattr(hook_intent, key, "") or "") for key in
                           ("story_subject", "local_claim", "subject", "action", "location")).casefold()
    relationship_cues = ("couple", "dating", "date", "girlfriend", "boyfriend", "romance",
                         "relationship", "matching", "osoroi", "christmas")
    if any(re.search(r"(?<![a-z])" + re.escape(cue) + r"(?![a-z])", intent_text)
           for cue in relationship_cues):
        themed = (("カップルコーデ", "ja"), ("カップル デート vlog", "ja"),
                  ("カップル 写真", "ja"), ("Japanese couple matching outfits", "en"),
                  ("Japanese couple date vlog", "en"))
        return [SearchQueryV2(
            query=q, language=lang, tier="exact_action", visual_intent_id="hook_influencer",
            scene_ids=[0], query_type="hook_presenter", generated_from="hook_relationship_preset",
            reason="young Japanese woman/couple relevant to the relationship hook",
            expected_subject="young Japanese woman with her partner",
            expected_action="couple styling, date activity or affectionate candid moment",
            expected_location="Japan", requires_japanese_context=True) for q, lang in themed]

    pools = getattr(_ac(), "HOOK_PRESENTER_QUERIES", {}) or {}
    out = []
    # A handful of strong terms is enough. The old pool repeatedly navigated the same query for
    # local relevance/likes/views sorts and could spend 15 minutes on the hook alone. Birth-year
    # tags ("#0X #女の子", user-provided) go FIRST: they surface exactly the
    # young-Japanese-creator demographic on TikTok AND Instagram, and their surplus segments
    # double as generic Japanese-style fill for the body/context-fallback pool. Rotate through
    # the years so consecutive runs don't hammer the same two tags.
    limits = {"birthyear": 2, "exact": 2, "social": 1, "english": 0, "hashtag": 1}
    by = list(pools.get("birthyear") or [])
    if by:
        rot = int(time.time() // 3600) % len(by)      # hourly rotation start
        pools = dict(pools)
        pools["birthyear"] = by[rot:] + by[:rot]
    for group, lang in (("birthyear", "ja"), ("exact", "ja"), ("social", "ja"),
                        ("english", "en"), ("hashtag", "ja")):
        for q in (pools.get(group) or [])[:limits[group]]:
            # defensive: a preset query must never carry a platform NAME (e.g. "... TikTok"),
            # or IG/X literally searches for the word "tiktok" and returns cross-posts/junk
            q = sanitize_platform_query(q)
            if not q:
                continue
            out.append(SearchQueryV2(
                query=q, language=lang, tier="exact_action", visual_intent_id="hook_influencer",
                scene_ids=[0], query_type="hook_presenter",
                generated_from="hook_presenter_preset",
                reason="optional presenter hook only",
                expected_subject="young adult Japanese female creator",
                expected_action="dancing or playfully acting cute to camera", expected_location="",
                requires_japanese_context=True))
    return out


def lateral_filler_queries_v2(title, script, understanding=None, reasoning_model=None,
                              status_cb=None):
    """Lateral Search Architect (user spec): ONE LLM call -> 15-25 broad, diverse, native
    queries for a GLOBAL thematic B-roll pool. Philosophy: intelligent tangibles over literal
    objects - (1) the specific NUMBERS/extremes users flex or complain with, (2) the HUMAN
    ACTIONS the topic causes, (3) native platform SLANG. The pool keeps the segment supply
    rich so weak scenes fall back to ON-THEME human footage instead of emergency reuse."""
    ac = _ac()
    sys_p = (
        "You are the Lateral Search Architect for a high-retention short-form scraping "
        "pipeline. You translate a script's THEME into native TikTok search queries that "
        "guarantee AUTHENTIC HUMAN footage. CORE PHILOSOPHY - intelligent tangibles over "
        "literal objects: NEVER search lifeless objects; translate every concept into (1) the "
        "specific numbers/extremes users post to flex or complain (40kg, 3時間睡眠, 月3万円), "
        "(2) the human behavior the topic causes (crying, struggling, routine, body check, "
        "fail), (3) native platform slang (あるある, 限界, 失敗, ルーティン, 密着). JSON only.")
    user_p = (ac.understanding_brief(understanding) if understanding else "") + f"""
TOPIC: {title}
SCRIPT:
{(script or "")[:900]}

Generate 15-25 broad, DIVERSE, lateral search queries for a GLOBAL thematic B-roll pool.
Rules: mostly 1-2-word native Japanese (latin unit tokens like 40kg / 150cm are fine), plus at
most 4 English queries of 2-3 words; every query must promise visible PEOPLE in motion or an
extreme platform trend tied to the theme; no two queries covering the same angle. Infer the global
theme from the FULL script, not its first hook example. Do not include a hook-specific object/place
in this pool unless it recurs later in the script.
Return exactly: {{"global_filler_queries": ["...", "..."]}}"""
    try:
        data = _llm_json([{"role": "system", "content": sys_p},
                          {"role": "user", "content": user_p}],
                         max_tokens=1200, temperature=0.8, reasoning_model=reasoning_model,
                         status_cb=status_cb, label="lateral filler")
    except Exception:
        return []
    raw = data.get("global_filler_queries") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        return []
    out, seen = [], set()
    for text, lang in ([(q, "ja") for q in _clean_japanese_queries(raw, limit=25)]
                       + [(q, "en") for q in _clean_english_queries(raw, limit=4)]):
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        # expected_subject = the query itself so metadata relevance still rewards captions
        # that echo the theme term (filler has no per-scene expectations).
        out.append(SearchQueryV2(query=text, language=lang, tier="global_filler",
                                 visual_intent_id="global_filler", scene_ids=[],
                                 query_type="global_filler", generated_from="lateral_search_architect",
                                 reason="optional global thematic filler",
                                 expected_subject=text,
                                 expected_action="", expected_location="", platforms=["tiktok"]))
    return out[:25]


def _reserve_for_correction(now, deadline, share=0.28, minimum=240.0):
    """The deadline the first-look analysis rounds may use, keeping a share for the correction.

    A beat whose single coverage query returned nothing can only be rescued by a corrective
    search. Handing the analysis rounds the whole clock meant that stage never ran: two runs in
    a row executed zero adaptive queries and simply borrowed a neighbour's clip for every empty
    beat. The reserve is skipped when there is too little time left for it to buy anything.
    """
    left = max(0.0, float(deadline) - float(now))
    if left <= minimum:
        return float(deadline)
    return float(deadline) - max(0.0, min(left - minimum, left * float(share)))


def _fair_round_deadline(now, deadline, rounds_left, minimum=90.0):
    """The sub-deadline one analysis round may use, so later rounds still get a turn.

    The coverage wave's first round used to be handed the run deadline and simply downloaded and
    described until it was gone; beats 5-10 of the chocolate short had their sources found and
    then never fetched.  Splitting the REMAINING time by the rounds that are still owed keeps the
    slices even as the clock drains, and ``minimum`` guarantees a round is never handed a slice
    too small to fetch anything at all.
    """
    left = max(0.0, float(deadline) - float(now))
    share = left / max(1, int(rounds_left or 1))
    return min(float(deadline), float(now) + max(float(minimum), share))


def _search_sources(queries, platforms, cancel_check, deadline, seen_source_ids, state, status_cb,
                    sort="RELEVANCE", coverage_pass=False):
    """Run a batch of SearchQueryV2 through the dual backend, dedupe by source_id, relevance-rank.

    ``ALL`` is treated as a controller preference rather than four browser searches: coverage
    uses relevance, while later recovery uses most-liked. This preserves both signals without
    spending most of the run repeating the same term four times."""
    ranked_all = []
    plat_counts, creator_counts = {}, {}
    preferred = str(sort or "RELEVANCE").upper()
    if preferred not in ("RELEVANCE", "MOST_LIKED", "MOST_VIEWED", "MOST_RECENT", "ALL"):
        preferred = "MOST_LIKED"
    # backend_search can expand ALL internally, but that means ONE query becomes FOUR searches:
    # most-liked, relevance,
    # most-viewed, most-recent, run in sequence. That is depth on a single term, and the
    # coverage wave is the one round whose whole job is BREADTH - one search for every beat
    # before any beat gets a second. Spending four searches per term there is why 162
    # planned queries became 16 executed in a 30-minute deadline, with beats 12-15 never
    # searched at all. The coverage pass now takes one sort order; the other three stay
    # available to the later rounds, which target beats that are still empty.
    #
    # coverage_pass was already a parameter here and had simply never been read.
    if preferred == "ALL":
        preferred = "RELEVANCE" if coverage_pass else "MOST_LIKED"
    sort_passes = [preferred]
    selected_platforms = clip_scraper.normalize_platforms(platforms)

    def _run_pass(q, text, sort_mode, target_platforms):
        """One backend search for one text under one sort order. Returns raw item count."""
        if status_cb:
            _log(status_cb, f'Scrape V2 query [{sort_mode}]: "{text}"')
        text = sanitize_platform_query(text)
        if not text:
            return 0
        # DEEP SCROLL (user rule 2026-07-13): pull ~18 (page 2-3), not the top-10. The first
        # page is caption-heavy tutorials; the organic POV clips live deeper, and strict
        # no-reuse needs a wide pool of DISTINCT sources per term.
        items = clip_scraper.backend_search(text, 18, status_cb=status_cb, sort=sort_mode,
                                            platforms=target_platforms, deadline=deadline) or []
        state["queries_executed"] = state.get("queries_executed", 0) + 1
        qt = state.setdefault("query_texts", [])
        if text not in qt:
            qt.append(text)
        state.setdefault("sort_pass_counts", {})[sort_mode] = (
            state.setdefault("sort_pass_counts", {}).get(sort_mode, 0) + 1)
        state["raw_results"] = state.get("raw_results", 0) + len(items)
        raw_by_platform = state.setdefault("raw_results_by_platform", {})
        for item in items:
            platform = _canonical_platform((clip_scraper._item_meta(item) or {}).get("platform") or "tiktok")
            raw_by_platform[platform] = raw_by_platform.get(platform, 0) + 1
        provenance = state.setdefault("query_provenance", [])
        provenance.append({
            "text": text, "source": target_platforms,
            "scene_ids": list(q.scene_ids or []), "visual_intent_id": q.visual_intent_id,
            "query_type": q.query_type, "generated_from": q.generated_from,
            "reason": q.reason, "sort": sort_mode, "raw_results": len(items),
        })
        fresh = []
        for it in items:
            src = _item_to_source(it, q)
            if src is None or src.source_id in seen_source_ids:
                continue
            seen_source_ids.add(src.source_id)
            fresh.append(src)
        state["metadata_candidates"] = state.get("metadata_candidates", 0) + len(fresh)
        ranked = rank_metadata_candidates_v2(fresh, q, plat_counts, creator_counts)
        for source in ranked:
            plat_counts[source.platform.lower()] = plat_counts.get(source.platform.lower(), 0) + 1
            creator_counts[source.creator_id.lower()] = creator_counts.get(source.creator_id.lower(), 0) + 1
        ranked_all.extend(ranked)
        return len(items)

    for q in queries:
        if (cancel_check and cancel_check()) or (deadline and time.monotonic() >= deadline):
            break
        scoped = {_canonical_platform(value) for value in (q.platforms or [])}
        target_platforms = sorted(selected_platforms & scoped) if scoped else sorted(selected_platforms)
        if not target_platforms:
            continue
        raw_total = 0
        for sort_mode in sort_passes:
            if (cancel_check and cancel_check()) or (deadline and time.monotonic() >= deadline):
                break
            raw_total += _run_pass(q, q.query, sort_mode, target_platforms)
        # Preserve named-entity anchors. The previous first-token fallback changed ``名頃 かかし``
        # into generic ``名頃``/``かかし`` neighbourhoods and flooded the pool with unrelated media.
        # Only a 3+ token query may shed its final disambiguator here; two-token failures are left
        # for the live adaptive controller, which can change the physical situation intelligently.
        toks = q.query.split()
        if (raw_total == 0 and len(toks) >= 3
                and not (cancel_check and cancel_check())
                and not (deadline and time.monotonic() >= deadline)):
            broader = " ".join(toks[:-1])
            _log(status_cb, f'Scrape V2: 0 results for "{q.query}" -> preserved anchor and retried "{broader}"')
            state["broadened_queries"] = state.get("broadened_queries", 0) + 1
            _run_pass(q, broader, sort_passes[0], target_platforms)
    ranked_all.sort(key=lambda s: s.rank_score, reverse=True)
    return interleave_ranked_sources_by_scene(ranked_all)


def _download_and_segment(sources, project_dir, ffmpeg, ffprobe, cancel_check, deadline, state,
                          status_cb, download_budget):
    """Proxy-download top-ranked sources, discover multi-region segments, score segment quality.
    Returns quality-passed SegmentCandidate list. Records rejection tallies in state."""
    cand_root = Path(project_dir) / "seedance 2.0" / "_v2_proxies"
    cand_root.mkdir(parents=True, exist_ok=True)
    rej = state.setdefault("rejections", {})
    passed = []

    def _archive_declined(path, src, reason, segment=None):
        """Persist a watchable source for every V2 post-download rejection.

        V2 previously deleted proxies as soon as a segment failed quality, leaving the user with
        a log entry but no way to inspect or manually reuse the footage.  Keep one source copy
        per clip (not one copy per rejected segment) plus the exact reason in a sidecar.
        """
        try:
            source = Path(path)
            if not source.exists():
                return
            root = Path(project_dir) / "seedance 2.0" / "_declined"
            root.mkdir(parents=True, exist_ok=True)
            key = hashlib.sha1(str(src.source_id).encode("utf-8", "ignore")).hexdigest()[:12]
            dest = root / f"declined_v2_{key}.mp4"
            if not dest.exists():
                shutil.copy2(source, dest)
            sidecar = dest.with_suffix(".json")
            prior = json.loads(sidecar.read_text("utf-8")) if sidecar.exists() else {}
            reasons = list(prior.get("reasons") or [])
            if reason not in reasons:
                reasons.append(str(reason)[:180])
            sidecar.write_text(json.dumps({
                "status": "rejected", "platform": src.platform, "clip_id": src.source_id,
                "query": src.query, "reasons": reasons,
                "segment": ({"start": round(float(segment.start_time or 0), 3),
                             "end": round(float(segment.end_time or 0), 3)} if segment else None),
            }, indent=2, ensure_ascii=False), "utf-8")
        except Exception:
            pass
    # Fetch the proxies concurrently, then analyse them one at a time.
    #
    # Downloading was the single most expensive thing the scraper did and it was strictly
    # serial: on the Tokyo project 35 proxies arrived at a median 34.6s apart, 1211 of the
    # run's 1978 seconds, and the 25-minute deadline then cut the search off after four
    # queries. Fetching is network wait, so it parallelises almost perfectly; segment
    # discovery and vision stay sequential because they are CPU and paid-API work.
    planned = []
    for src in sources:
        # Check before PLANNING, not only before analysing. The serial loop tested both per
        # source and broke before spending anything; batching moved the download ahead of
        # the old checks, so a cancelled or timed-out run could still fetch a whole batch.
        if (cancel_check and cancel_check()) or (deadline and time.monotonic() >= deadline):
            break
        # TWO DIFFERENT QUESTIONS, TWO DIFFERENT SETS. _downloaded_ids answers "do we already
        # have this source?" and is seeded on resume with every proxy the project ever fetched.
        # _fetched_ids answers "how much has this RUN spent?" and starts empty. They used to be
        # the same set, so a second run on an existing project began with the spend counter
        # already at the number of proxies on disk: the Zauo re-run started at 184 against a cap
        # of 45 and broke out of this loop on the first source of every round. 897 ranked
        # candidates, 11 downloads, 8 of 13 beats uncovered, and it gave up with more than an
        # hour of its budget unspent.
        #
        # The id is reserved below, so len() already counts what this loop has planned. Adding
        # len(planned) as well counted every source twice and delivered half the intended
        # budget - min_downloads_per_scene 3 became 1.5.
        if len(state.setdefault("_fetched_ids", set())) >= download_budget:
            break
        if src.source_id in state.setdefault("_downloaded_ids", set()):
            continue
        state["_downloaded_ids"].add(src.source_id)
        state["_fetched_ids"].add(src.source_id)
        planned.append((src, cand_root / ("proxy_" + src.platform + "_"
                                          + re.sub(r"[^A-Za-z0-9]+", "_", src.source_id)[:24]
                                          + ".mp4")))
    fetched = {}
    if planned:
        workers = min(PROXY_FETCH_WORKERS, len(planned))
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(download_proxy_v2, src.raw_item, proxy, None): src.source_id
                       for src, proxy in planned}
            for fut in concurrent.futures.as_completed(futures):
                try:
                    fetched[futures[fut]] = fut.result()
                except Exception:      # a dead link must not take the batch down with it
                    fetched[futures[fut]] = None
    for src, _proxy in planned:
        if (cancel_check and cancel_check()) or (deadline and time.monotonic() >= deadline):
            break
        got = fetched.get(src.source_id)
        if not got:
            rej["download_failed"] = rej.get("download_failed", 0) + 1
            continue
        state["downloaded_sources"] = state.get("downloaded_sources", 0) + 1
        if state.get("native_vertical_only"):
            dims = clip_scraper._probe_dims(got, ffprobe)
            if not dims or not _is_native_9_16(*dims):
                shown = f"{dims[0]}x{dims[1]}" if dims else "unknown dimensions"
                rej["not_native_9_16"] = rej.get("not_native_9_16", 0) + 1
                _log(status_cb, f"Scrape V2 Mini Story: rejected {src.platform} clip "
                                f"{src.source_id} ({shown}); native 9:16 is required.")
                _archive_declined(got, src, f"native 9:16 required ({shown})")
                try:
                    Path(got).unlink(missing_ok=True)
                except Exception:
                    pass
                continue
        segs = discover_segments_v2(src, got, ffmpeg, ffprobe, status_cb=status_cb)
        state["segments_discovered"] = state.get("segments_discovered", 0) + len(segs)
        kept = 0
        for seg in segs:
            if kept >= SCRAPE_V2_CONFIG["max_segments_per_source"]:
                break
            analyze_segment_v2(seg, ffmpeg, ffprobe, status_cb=status_cb)
            # Never knowingly render a source hitch, frozen run or internal source edit. These
            # used to survive as soft warnings and became visible micro-lags/double-cuts later.
            hard_reasons = list(seg.rejection_reasons)
            if hard_reasons:
                for r in hard_reasons:
                    key = {"massive_black_bars": "black_bars", "ai_watermark": "ai_content",
                           "burned_caption_over_subject": "burned_captions",
                           "no_motion": "no_motion",
                            "rapid_internal_cuts": "rapid_edits",
                            "frozen_frames": "micro_freezes",
                            "cadence_stutter": "micro_stutters"}.get(r, r)
                    rej[key] = rej.get(key, 0) + 1
                _archive_declined(got, src, "; ".join(hard_reasons), seg)
                continue
            if seg.rejection_reasons:
                # Timing defects can often be avoided by trimming the selected window.
                # Keep these clips available so semantic relevance, not one technical
                # imperfection, decides whether they are useful.
                seg.quality_gate = "soft"
                state["segments_soft_artifact_kept"] = state.get("segments_soft_artifact_kept", 0) + 1
            if seg.quality_score < SEGMENT_SOFT_MIN_QUALITY:
                rej["low_quality"] = rej.get("low_quality", 0) + 1
                _archive_declined(got, src, f"low quality score {seg.quality_score:.1f}", seg)
                continue
            if seg.quality_score < SEGMENT_MIN_QUALITY:
                seg.quality_gate = "soft"
                state["segments_soft_quality_kept"] = state.get("segments_soft_quality_kept", 0) + 1
            passed.append(seg)
            kept += 1
        state["segments_quality_passed"] = state.get("segments_quality_passed", 0) + kept
    return passed


def _clean_window_start(source_path, ffmpeg, lo, hi, want, edge=0.22):
    """Start time of a cut-free `want`-second window for a candidate whose window straddles a cut.

    The final audit rejects an exported clip that contains a source scene change. Throwing the
    whole candidate away for that was measurably wasteful: on the real pool, 13 of 14 sources
    hold at least one clean window, so the shot the matcher approved is usually still there - a
    second or two along. Slide, do not discard.

    The slide may leave the discovery window but never leaves the SHOT: it searches the maximal
    cut-free interval containing the middle of the inspected window. No cut between the two means
    it is the same continuous take, so the picture vision described is the picture exported -
    which is the actual rule the "never extend" comment was protecting.
    """
    lo, hi, want = float(lo), float(hi), float(want)
    duration = float(clip_scraper._probe_duration(
        source_path, clip_scraper._ffmpeg_tools()[1]) or 0.0)
    if duration <= 0.0 or duration + 0.05 < want:
        return None
    cuts = sorted(float(c) for c in (clip_scraper.hard_cut_times(
        source_path, ffmpeg, scan_seconds=min(duration + 0.1, 180.0)) or []))
    if not cuts:
        return None                      # nothing to avoid: either no cuts or the scan failed
    # Every shot of the source, then only those the inspected window actually touches - the take
    # it was cut from and the one immediately after the straddled cut. Anchoring on a single
    # midpoint was worse than useless: a window straddling a cut anchors into the SHORT side.
    bounds = [0.0] + cuts + [duration]
    shots = [(bounds[i] + edge, bounds[i + 1] - edge) for i in range(len(bounds) - 1)]
    reach_lo, reach_hi = lo - want, hi + want
    best = None
    for shot_lo, shot_hi in shots:
        if shot_hi <= reach_lo or shot_lo >= reach_hi:
            continue
        if shot_hi - shot_lo + 0.05 < want:
            continue                     # this take is shorter than the beat needs
        # closest legal start inside this shot to the window vision actually looked at
        candidate = min(max(lo, shot_lo), shot_hi - want)
        if best is None or abs(candidate - lo) < abs(best - lo):
            best = candidate
    return best


def _finalize_segment_clip(seg, project_dir, ffmpeg, min_seconds=None):
    """Normalize the chosen segment window into a clean 9:16 1080x1920 clip. Returns the path.

    `min_seconds` is the exact narration chapter length.  The inspected discovery window must
    already contain that much continuous footage; the finalizer never reaches beyond its end.
    Otherwise semantic review and cut detection would describe a different picture than the one
    the renderer receives.
    """
    want = max(0.1, float(min_seconds or seg.duration or 0))
    # Finalization may trim an inspected clean window, but must never EXTEND beyond it.  Extending
    # was the exact double-cut bug: semantic review saw one shot, while the render included the
    # source's next shot for the final few frames.
    if float(seg.duration or 0.0) + 0.05 < want:
        return ""
    prev = getattr(seg, "_final_secs", 0.0)
    if seg.final_path and prev >= want - 0.01:
        return seg.final_path
    out_dir = Path(project_dir) / "seedance 2.0"
    key = hashlib.sha1(seg.segment_id.encode("utf-8", "ignore")).hexdigest()[:10]
    dest = out_dir / ("v2seg_" + key + ".mp4")
    ffprobe = clip_scraper._ffmpeg_tools()[1]
    source_duration = float(clip_scraper._probe_duration(seg.source_path, ffprobe) or 0.0)
    start = float(seg.start_time or 0.0)
    if start + want > float(seg.end_time or 0.0) + 0.05:
        return ""
    if source_duration > 0:
        if source_duration + 0.05 < want:
            return ""
    # Cut from the de-captioned copy when the blur managed to make one, so the accepted
    # clip is the clean version rather than the one with someone else's text on it.
    cut_from = str(getattr(seg, "cleaned_path", "") or seg.source_path)
    if cut_from != seg.source_path and not Path(cut_from).is_file():
        cut_from = seg.source_path
    final = clip_scraper.normalize_clip(cut_from, dest, ffmpeg, seconds=want, start=start)
    seg.final_path = str(final) if final else ""
    if final:
        final_duration = float(clip_scraper._probe_duration(final, ffprobe) or 0.0)
        if final_duration + 0.08 < want:
            Path(final).unlink(missing_ok=True)
            seg.final_path = ""
            return ""
        # FINAL PICTURE AUDIT: judge the exact exported bytes, not only the proxy window.  A source
        # scene change within 0.22s of either boundary is effectively the chosen in/out point;
        # anything deeper inside the usable
        # picture creates a visible double-cut and invalidates this candidate.
        final_cuts = clip_scraper.hard_cut_times(
            final, ffmpeg, scan_seconds=min(final_duration + 0.1, 90.0))
        inside = ([cut for cut in (final_cuts or [])
                   if 0.22 < float(cut) < final_duration - 0.22]
                  if final_cuts is not None else [])
        if inside:
            Path(final).unlink(missing_ok=True)
            seg.final_path = ""
            # SLIDE BEFORE DISCARDING. This audit used to end the candidate outright, and it did
            # so silently: the beat then reported "selected segment failed the final editorial
            # gate" and borrowed a neighbour's picture instead - a semantic match of 5.95 thrown
            # away over a scene change that a one-second shift avoids entirely (measured on the
            # konbini run, beat 1).
            slid = _clean_window_start(cut_from, ffmpeg, start, float(seg.end_time or 0.0), want)
            if slid is not None and abs(slid - start) > 0.05:
                final = clip_scraper.normalize_clip(cut_from, dest, ffmpeg, seconds=want,
                                                    start=slid)
                if final:
                    final_duration = float(clip_scraper._probe_duration(final, ffprobe) or 0.0)
                    recut = clip_scraper.hard_cut_times(
                        final, ffmpeg, scan_seconds=min(final_duration + 0.1, 90.0))
                    still_inside = [cut for cut in (recut or [])
                                    if 0.22 < float(cut) < final_duration - 0.22]
                    if final_duration + 0.08 >= want and not still_inside:
                        seg.start_time, seg.end_time = slid, slid + want
                        seg.final_path = str(final)
                        seg._final_secs = want
                        return seg.final_path
                    Path(final).unlink(missing_ok=True)
            seg.final_path = ""
            seg.rejection_reasons = list(seg.rejection_reasons or []) + ["final_internal_cut"]
            return ""
        seg._final_secs = want
    return seg.final_path


def plan_chapter_montage_rows(primary_seg, candidate_rows, scene_need, scene_intent,
                               source_owner=None, scene_idx=0, max_shots=4):
    """Pure editorial planner for a multi-shot visual chapter.

    Returns ``[(candidate_row, seconds), ...]``. It maximises coverage of the chapter's concrete
    visible elements, permits non-overlapping windows from one source inside the same topic, and
    guarantees that real source duration covers the narration without a frozen tail.
    """
    scene_need = float(scene_need or 0.0)
    if primary_seg is None or scene_need <= 0.0:
        return []
    source_owner = dict(source_owner or {})
    required = [str(value).strip() for value in
                (getattr(scene_intent, "required_elements", None) or [])
                if str(value).strip()]
    rows = list(candidate_rows or [])
    if not any(row.get("segment") is primary_seg for row in rows):
        rows.insert(0, {"segment": primary_seg, "overall_match": primary_seg.semantic_score,
                        "covers": [], "match_class": "A_MATCH"})

    def visible_tokens(row):
        seg0 = row.get("segment")
        desc = (seg0.visual_description or {}) if seg0 is not None else {}
        values = list(row.get("covers") or []) + list(desc.get("subjects") or []) + [
            desc.get("action") or "", desc.get("location") or ""]
        return _tokens(" ".join(str(value) for value in values if value))

    wanted_shots = max(1, min(int(max_shots), int(math.ceil(scene_need / 4.0))))
    # A single continuous source that genuinely covers the complete chapter is already the ideal
    # result. Multi-shot assembly is a recovery/editorial enhancement, not a reason to reject a
    # coherent 8-second demonstration just because no second TikTok is needed.
    single_can_cover = (float(primary_seg.duration or 0.0) + 0.05 >= scene_need
                        and not editorial_rejection_reason(primary_seg, scene_intent))
    minimum_shots = 1 if single_can_cover else wanted_shots
    selected, selected_ids, selected_windows, covered = [], set(), {}, set()
    while len(selected) < int(max_shots):
        best = None
        for row in rows:
            candidate = row.get("segment")
            if candidate is None or candidate.segment_id in selected_ids:
                continue
            owner = source_owner.get(candidate.source_id)
            if owner is not None and owner != scene_idx:
                continue
            if editorial_rejection_reason(candidate, scene_intent):
                continue
            overlaps = any(not (candidate.end_time <= lo + 0.08
                                or candidate.start_time >= hi - 0.08)
                           for lo, hi in selected_windows.get(candidate.source_id, []))
            if overlaps:
                continue
            visible = visible_tokens(row)
            new_visible = len(visible - covered)
            required_gain = 0
            for element in required:
                element_tokens = _tokens(element)
                if element_tokens and element_tokens & visible and not element_tokens <= covered:
                    required_gain += 1
            diversity = 0.4 if candidate.source_id not in selected_windows else 0.0
            score = (float(row.get("overall_match") or candidate.semantic_score or 0.0)
                     + required_gain * 1.6 + min(4, new_visible) * 0.18 + diversity)
            if candidate is primary_seg and not selected:
                score += 0.8
            if best is None or score > best[0]:
                best = (score, row, visible)
        if best is None:
            break
        _score, chosen_row, visible = best
        chosen = chosen_row["segment"]
        selected.append(chosen_row)
        selected_ids.add(chosen.segment_id)
        selected_windows.setdefault(chosen.source_id, []).append(
            (float(chosen.start_time), float(chosen.end_time)))
        covered.update(visible)
        capacity = sum(float(row["segment"].duration or 0.0) for row in selected)
        if len(selected) >= wanted_shots and capacity + 0.05 >= scene_need:
            break
    if (len(selected) < minimum_shots
            or sum(float(row["segment"].duration or 0.0) for row in selected) + 0.05 < scene_need):
        return []

    def story_order(row):
        visible = visible_tokens(row)
        first = len(required) + 1
        for index, element in enumerate(required):
            if _tokens(element) & visible:
                first = index
                break
        return first, float(row["segment"].start_time or 0.0)

    selected.sort(key=story_order)
    capacities = [float(row["segment"].duration or 0.0) for row in selected]
    allocations = [0.0] * len(selected)
    remaining, active = scene_need, set(range(len(selected)))
    while active and remaining > 0.001:
        share = remaining / len(active)
        saturated = [index for index in active if capacities[index] <= share + 0.001]
        if not saturated:
            for index in active:
                allocations[index] += share
            remaining = 0.0
            break
        for index in saturated:
            allocations[index] = capacities[index]
            remaining -= allocations[index]
            active.remove(index)
    if remaining > 0.05 or any(value < 1.25 for value in allocations):
        return []
    return list(zip(selected, allocations))


def render_chapter_montage(plan, scene_idx, scene_need, project_dir, ffmpeg, ffprobe,
                            status_cb=None):
    """Render a planned chapter montage from exact, independently audited source windows."""
    if not plan:
        return "", []
    parts = []
    for index, (row, seconds) in enumerate(plan):
        source_seg = row["segment"]
        part_seg = copy.copy(source_seg)
        part_seg.segment_id = f"{source_seg.segment_id}_chapter_{scene_idx}_{index}"
        part_seg.final_path = ""
        if hasattr(part_seg, "_final_secs"):
            delattr(part_seg, "_final_secs")
        part = _finalize_segment_clip(part_seg, project_dir, ffmpeg,
                                      min_seconds=round(float(seconds), 3))
        if not part:
            return "", []
        parts.append((part, source_seg, round(float(seconds), 3)))

    chapter_dir = Path(project_dir) / "seedance 2.0"
    digest = hashlib.sha1((str(scene_idx) + "|" + "|".join(
        item[1].segment_id for item in parts)).encode("utf-8", "ignore")).hexdigest()[:10]
    destination = chapter_dir / f"v2chapter_{scene_idx:02d}_{digest}.mp4"
    concat_file = chapter_dir / f"_v2chapter_{scene_idx:02d}_{digest}.txt"
    concat_file.write_text("\n".join(
        "file '" + Path(path).resolve().as_posix().replace("'", "'\\''") + "'"
        for path, _source, _seconds in parts), encoding="utf-8")
    command = [ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", str(concat_file),
               "-t", f"{float(scene_need):.3f}", "-an", "-c:v", "libx264",
               "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-r", "30",
               "-movflags", "+faststart", str(destination)]
    try:
        subprocess.run(command, check=True, capture_output=True, timeout=240)
    except Exception as exc:
        _log(status_cb, f"Fact Short chapter montage render failed: {exc.__class__.__name__}.")
        return "", []
    finally:
        concat_file.unlink(missing_ok=True)
    duration = float(clip_scraper._probe_duration(destination, ffprobe) or 0.0)
    if duration + 0.08 < float(scene_need):
        destination.unlink(missing_ok=True)
        return "", []
    boundaries, cursor = [], 0.0
    for _path, _source, seconds in parts[:-1]:
        cursor += float(seconds)
        boundaries.append(cursor)
    cuts = [float(value) for value in (clip_scraper.hard_cut_times(
        destination, ffmpeg, scan_seconds=min(duration + 0.1, 90.0)) or [])]
    # Every detected picture change must be the edit we intentionally created. Two changes around
    # one boundary mean the incoming source switches again a few frames later - the exact
    # "scene changes just before/after our cut" defect the hand audit found.
    if not chapter_montage_cuts_are_clean(cuts, boundaries, duration):
        destination.unlink(missing_ok=True)
        _log(status_cb, "Fact Short chapter montage rejected: hidden source cut beside the "
                        "editorial boundary.")
        return "", []
    return str(destination), parts


def chapter_montage_cuts_are_clean(cuts, boundaries, duration, tolerance=0.22):
    """Accept only the deliberately authored chapter boundaries, one detected cut each."""
    cuts = [float(value) for value in (cuts or [])]
    boundaries = [float(value) for value in (boundaries or [])]
    duration = float(duration or 0.0)
    unexpected = [cut for cut in cuts
                  if tolerance < cut < duration - tolerance
                  and not any(abs(cut - boundary) <= tolerance for boundary in boundaries)]
    double_cuts = [boundary for boundary in boundaries
                   if len([cut for cut in cuts if abs(cut - boundary) <= tolerance]) > 1]
    return not unexpected and not double_cuts


# ---------------------------------------------------------------- hook V2

def hook_pool_without_burned_captions(segs, status_cb=None):
    """Drop hook candidates that carry somebody else's burned-in text.

    A stolen caption ranks low everywhere in the video; on the OPENING shot it is fatal, because
    it is the first thing on screen and it says "reposted" before a word is spoken. Returns the
    clean candidates, or the original list when every candidate is captioned - a captioned opener
    still beats no opener, but the caller is told about it.
    """
    segs = list(segs or [])
    clean = [seg for seg in segs
             if not bool((seg.visual_description or {}).get("burned_captions"))
             and not bool((seg.visual_description or {}).get("creator_overlay"))
             and float(getattr(seg, "caption_probability", 0.0) or 0.0) < 0.35]
    if not clean:
        if segs:
            _log(status_cb, "Scrape V2 hook WARNING: every candidate carries burned-in text, so "
                            "the opening shot will show somebody else's captions.")
        return segs
    if len(clean) < len(segs):
        _log(status_cb, "Scrape V2 hook: %d of %d candidate(s) carry burned-in captions; the "
                        "opening shot is chosen from the %d clean one(s)."
             % (len(segs) - len(clean), len(segs), len(clean)))
    return clean


def score_hook_candidates_v2(hook_segments, hook_intent, project_dir, ffmpeg, reasoning_model=None,
                             status_cb=None):
    """Topic-relevant hook caster: presenter energy + TOPIC relevance + visible action + clean frame.
    Returns the best hook SegmentCandidate (or None)."""
    ac = _ac()
    segs = [s for s in (hook_segments or []) if Path(s.source_path).exists()]
    if not segs:
        return None
    describe_segments_v2(segs, project_dir, ffmpeg, reasoning_model=reasoning_model, status_cb=status_cb)
    segs = hook_pool_without_burned_captions(segs, status_cb=status_cb)
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
        "Cast the OPENING HOOK of a vertical Japanese social short. The strongest hooks are a young-"
        "adult Japanese female creator DANCING or playfully acting cute directly to camera (idol "
        "energy) - this is a great scroll-stopper on its own, topic relevance is a bonus not a "
        "requirement. Topic (bonus only): " + topic + ".\nScore EACH tile 0-10: face_visibility, "
        "eye_contact, expression_energy, "
        "topic_relevance, visible_action, clean_frame, vertical_quality, plus sexualization_penalty "
        "(0 wholesome..10 body-bait), text_penalty (0 clean..10 text over face) and filter_penalty "
        "(0 the real face..10 drawn doodles, stickers, cat ears, sparkles or a heavy beauty filter "
        "covering the face). Set passed=false for: "
        "minor/child, sexualized body-bait, anime/VTuber/CGI, slideshow, livestream, heavy text over "
        "the face, a face buried under drawn stickers or doodles, or no clear main subject.\n"
        'Return STRICT JSON: {"clips": {"0": {"face_visibility":n,"eye_contact":n,"expression_energy":n,'
        '"topic_relevance":n,"visible_action":n,"clean_frame":n,"vertical_quality":n,'
        '"sexualization_penalty":n,"text_penalty":n,"filter_penalty":n,"passed":true|false}, ...}}')
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
        # Presenter/dance energy dominates; topic relevance is a small bonus (female-creator hook).
        score = (g("face_visibility") * 0.15 + g("eye_contact") * 0.15 + g("expression_energy") * 0.20
                 + g("topic_relevance") * 0.10 + g("visible_action") * 0.20 + g("clean_frame") * 0.10
                 + g("vertical_quality") * 0.10 - g("sexualization_penalty") * 0.6 - g("text_penalty") * 0.4
                 # A doodle filter is not text, so text_penalty never saw it: the capsule-hotel
                 # short opened on a creator whose face was covered in drawn white scribbles while
                 # eleven clean topical clips sat unused in the same candidate pool. The opening
                 # frame is the first thing on screen and this reads as cheaply as a stolen caption.
                 - g("filter_penalty") * 0.4)
        if score > best_score:
            best, best_score = seg, score
    if best is not None:
        best.semantic_score = round(best_score, 2)
        _log(status_cb, "Scrape V2 hook: selected " + best.segment_id + (" (score %.1f)." % best_score))
    return best


# ---------------------------------------------------------------- retry weak scenes

def relationship_recovery_queries_v2(uncovered):
    """Deterministic rescue terms for relationship scripts.

    The general lateral agent can collapse a whole dating story into generic terms such as
    ``woman reservation``. These are the native, visibly filmable clusters a human editor would
    actually try next. They are only used after ordinary per-scene and lateral recovery failed.
    """
    out, seen = [], set()
    for it in uncovered or []:
        text = " ".join(str(getattr(it, key, "") or "") for key in
                        ("scene_text", "story_subject", "local_claim", "subject", "action")).casefold()
        if not any(re.search(r"(?<![a-z])" + re.escape(k) + r"(?![a-z])", text)
                   for k in ("couple", "dating", "date", "girlfriend", "boyfriend",
                             "romance", "matching", "osoroi", "christmas", "gift")):
            continue
        if any(k in text for k in ("matching", "outfit", "osoroi", "fashion", "sync")):
            terms = [("カップルコーデ", "ja"), ("ペアルック", "ja"),
                     ("カップル お揃い", "ja"), ("Japanese couple matching outfit", "en")]
        elif any(k in text for k in ("christmas", "hotel", "dinner", "booking", "reservation")):
            terms = [("クリスマスデート", "ja"), ("カップル デート vlog", "ja"),
                     ("カップル レストラン", "ja"), ("Japanese Christmas date", "en")]
        elif any(k in text for k in ("gift", "okaeshi", "spending", "price")):
            terms = [("カップル プレゼント交換", "ja"), ("恋人 プレゼント", "ja"),
                     ("カップル ギフト vlog", "ja"), ("Japanese couple gift exchange", "en")]
        else:
            terms = [("カップル デート vlog", "ja"), ("カップル 日常", "ja"),
                     ("カップル 写真", "ja"), ("Japanese couple vlog", "en")]
        for query, lang in terms:
            key = (query.casefold(), it.scene_id)
            if key in seen:
                continue
            seen.add(key)
            out.append(SearchQueryV2(
                query=query, language=lang, tier="semantic_action", visual_intent_id=it.intent_id,
                scene_ids=[it.scene_id], query_type="relationship_recovery",
                generated_from="relationship_recovery", reason="specific native relationship footage rescue",
                expected_subject="young Japanese couple", expected_action=it.action,
                expected_location=it.location, platforms=["tiktok"]))
    return out

def retry_unmatched_scenes_v2(weak_intents, platforms, project_dir, ffmpeg, ffprobe, cancel_check,
                              deadline, state, seen_source_ids, reasoning_model=None,
                              status_cb=None, download_budget=None):
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
    # CREATIVE LATERAL RETRY (user rule): the literal approach already failed for these
    # scenes - ask for lateral queries that PROVE each scene's point visually WITHOUT its
    # literal words. ROLE-AWARE (research 2026-07-13, gift-taboo finding): for proof/
    # demonstration beats the factual OBJECT is the proof (4個 和菓子, 蝶結び ラッピング) and
    # must NOT be banned; only emotion/vibe beats avoid lifeless objects.
    try:
        lines = "\n".join(
            f'scene {it.scene_id} [{getattr(it, "communication_role", "human_consequence")}]: '
            f'"{(it.scene_text or "")[:120]}"' for it in weak_intents[:8])
        data = _llm_json([
            {"role": "system", "content":
             "You are the Evidence-Gap Search Architect. Previous searches for these scenes "
             "returned footage that failed visual verification. Do NOT merely loosen relevance "
             "or abandon the required object/action. Repair the gap with a search ladder: exact "
             "native object/place name, visible use/action, native uploader wording, and a genuinely "
             "different shot that visibly proves the same local claim or human consequence. "
             "OBJECT RULE by the [role] tag: proof/"
             "demonstration beats SHOULD search the factual object itself (hands counting four "
             "sweets, tying a bow); emotion/human_consequence/pattern_interrupt beats must "
             "show the affected person/action. Never replace a missing specific object with a broad "
             "category, setting or keyword association. JSON only."},
            {"role": "user", "content":
             f"FAILED SCENES:\n{lines}\n\nFor EACH scene return 4-6 distinct native queries "
             "(normally 1-3 Japanese terms; exact product/practice names may be longer). Include "
             "the indispensable noun, a visible action, native post wording, and one evidence-based "
             "alternative. Latin unit tokens like 40kg are allowed.\n"
             'Return exactly: {"scenes": {"<scene number>": ["query", ...]}}'}],
            max_tokens=1500, temperature=0.9, reasoning_model=reasoning_model,
            status_cb=status_cb, label="lateral retry")
        by_id = {it.scene_id: it for it in weak_intents}
        lat_added = 0
        seen_q = {_query_identity(q) for q in new_queries}
        for sid_str, qs_raw in ((data.get("scenes") or {}) if isinstance(data, dict) else {}).items():
            try:
                sid = int(re.sub(r"[^0-9-]", "", str(sid_str)) or "x")
            except ValueError:
                continue
            it = by_id.get(sid)
            if it is None or not isinstance(qs_raw, list):
                continue
            for text, lang in ([(q, "ja") for q in _clean_japanese_queries(qs_raw, limit=5)]
                               + [(q, "en") for q in _clean_english_queries(qs_raw, limit=2)]):
                candidate = SearchQueryV2(
                    query=text, language=lang, tier="semantic_action",
                    visual_intent_id=it.intent_id, scene_ids=[it.scene_id],
                    query_type="retry_lateral", generated_from="current_scene_retry",
                    reason="retry after no valid current-scene match", expected_subject=text,
                    expected_action=it.action, expected_location="", platforms=["tiktok"])
                valid, _reason, normalized = validate_query_against_intent(candidate, it)
                if not valid:
                    continue
                candidate.query = normalized
                if _query_identity(candidate) in seen_q:
                    continue
                seen_q.add(_query_identity(candidate))
                new_queries.append(candidate)
                lat_added += 1
        if lat_added:
            _log(status_cb, f"Scrape V2: +{lat_added} lateral retry queries for the failed scene(s).")
    except Exception:
        pass
    sources = _search_sources(new_queries, platforms, cancel_check, deadline, seen_source_ids,
                              state, status_cb)
    segs = _download_and_segment(sources, project_dir, ffmpeg, ffprobe, cancel_check, deadline, state,
                                 status_cb,
                                 download_budget=(download_budget
                                                  or SCRAPE_V2_CONFIG["max_downloaded_analysis_videos"]))
    describe_segments_v2(segs, project_dir, ffmpeg, reasoning_model=reasoning_model, status_cb=status_cb)
    return match_segments_to_scenes_v2(weak_intents, segs, reasoning_model=reasoning_model,
                                       status_cb=status_cb)


def escalate_platform_pivot_v2(uncovered, platforms, project_dir, ffmpeg, ffprobe, cancel_check,
                               deadline, state, seen_source_ids, reasoning_model=None,
                               status_cb=None, download_budget=None):
    """ESCALATION STAGE 1 - cross-platform pivot: re-send each uncovered scene's OWN terms to
    ALL connected platforms. X's Media tab is strong for proof/news, Instagram for hashtag reach,
    so a term that only ran on TikTok now also hits X and IG. Returns new scene->candidate map."""
    import copy as _c
    qs, seen = [], set()
    for it in uncovered:
        for q in queries_for_intent(it)[:6]:
            q2 = _c.copy(q)
            q2.platforms = list(platforms)          # force the full connected set
            key = (q2.query.casefold(), q2.visual_intent_id)
            if key in seen:
                continue
            seen.add(key)
            qs.append(q2)
    if not qs:
        return {}
    state["escalation_stage1"] = state.get("escalation_stage1", 0) + 1
    sources = _search_sources(qs, platforms, cancel_check, deadline, seen_source_ids, state, status_cb)
    segs = _download_and_segment(sources, project_dir, ffmpeg, ffprobe, cancel_check, deadline, state,
                                 status_cb,
                                 download_budget=(download_budget
                                                  or SCRAPE_V2_CONFIG["max_downloaded_analysis_videos"]))
    if not segs:
        return {}
    describe_segments_v2(segs, project_dir, ffmpeg, reasoning_model=reasoning_model, status_cb=status_cb)
    return match_segments_to_scenes_v2(uncovered, segs, reasoning_model=reasoning_model, status_cb=status_cb)


def adapt_queries_from_live_round_v2(weak_intents, searched_queries, recent_segments,
                                     reasoning_model=None, status_cb=None, limit=8,
                                     rejection_summary=None):
    """Inspect one completed search round and create a small corrective query set.

    This is deliberately called *between* rounds, while the run is active.  It gives the Search
    Agent the evidence the static Architect did not have: what the platforms actually returned,
    what vision saw, and why those clips failed.  It is bounded so a bad platform cannot trigger an
    endless LLM/search loop.
    """
    if not weak_intents:
        return []
    observations = []
    for seg in (recent_segments or [])[:18]:
        vd = seg.visual_description or {}
        observations.append({
            "query": seg.query or "", "subjects": vd.get("subjects") or [],
            "action": vd.get("action") or "", "location": vd.get("location") or "",
            "visible_text": vd.get("visible_text") or "",
            "burned_captions": bool(vd.get("burned_captions")),
            "quality": round(float(seg.quality_score or 0), 1),
        })
    failed = [{"scene_id": it.scene_id, "line": it.scene_text,
               "wanted_subject": it.subject, "wanted_action": it.action,
               "wanted_location": it.location, "communication_role": it.communication_role,
               "match_category": it.match_category,
               "required_visible_elements": list(it.required_elements or []),
               "avoid_elements": list(it.avoid_elements or []),
               "story_subject": it.story_subject, "local_claim": it.local_claim}
              for it in weak_intents[:8]]
    searched_rows = []
    for searched in searched_queries or []:
        if isinstance(searched, SearchQueryV2):
            searched_rows.append({"query": searched.query, "platforms": searched.platforms or ["all"]})
        else:
            searched_rows.append({"query": str(searched or ""), "platforms": ["all"]})
    prompt = (
        "You are the live Search Controller for a TikTok/Instagram/X footage scraper. A search round just "
        "finished and the listed scenes STILL have no verified visual match. Inspect the searched "
        "terms and what vision actually saw. For each scene explicitly compare the REQUIRED VISIBLE "
        "ELEMENTS with the observed subjects/actions/locations and identify the missing evidence. "
        "Then change strategy using a search ladder: exact native object/place name, visible use or "
        "action, native uploader wording, or a different shot that proves the same local claim. "
        "Search like an uploader, not a dictionary: combine the cultural anchor with a populated "
        "UGC action/format term such as 歌ってみた, 女子会, 開封, 使ってみた, やってみた, "
        "ルーティン, 飯, 寝落ち or あるある when appropriate. If a chapter contains several "
        "actions, target its central recognisable activity and separate coverage shots; do not "
        "lock the retry to a rare incidental prop just because it appeared in the narration. A "
        "failed karaoke search for an internal telephone should pivot to people singing in the "
        "karaoke room, friends at the karaoke table, or karaoke food—not another telephone synonym. "
        "Use rejection counts: repeated captions/black bars means leave tutorial/news-repost "
        "neighbourhoods for clean UGC actions; repeated semantic mismatch means change the visible "
        "subject/action, not merely a synonym. Never respond to a failed specific search by widening "
        "to a broad category such as container, restaurant vlog, toilet, Japan lifestyle or city. "
        "Do not repeat or lightly reword a failed query. Do not add platform names such as TikTok "
        "or Instagram. Create platform-specific terms: TikTok uses one compact native "
        "anchor plus one uploader-action/format phrase; "
        "Instagram uses one keyword phrase or one #hashtag. "
        "Preserve named-entity anchors. Return at most two new terms per "
        "scene total PER PLATFORM; each term must target a distinct result neighbourhood. JSON only.\n\n"
        f"UNMATCHED SCENES:\n{json.dumps(failed, ensure_ascii=False)}\n\n"
        f"SEARCHED THIS ROUND:\n{json.dumps(searched_rows, ensure_ascii=False)}\n\n"
        f"VISION OBSERVATIONS / LIVE LOG:\n{json.dumps(observations, ensure_ascii=False)}\n\n"
        f"CUMULATIVE REJECTION COUNTS:\n{json.dumps(rejection_summary or {}, ensure_ascii=False)}\n\n"
        'Return {"diagnosis":"short reason","queries":{"<scene_id>":'
        '{"tiktok":["raw query"],"instagram":["#rawtag"]}}}')
    try:
        data = _llm_json([
            {"role": "system", "content": "Diagnose live search failures and return executable corrective queries."},
            {"role": "user", "content": prompt}], max_tokens=1200, temperature=0.65,
            reasoning_model=reasoning_model, status_cb=status_cb, label="live search controller")
    except Exception as exc:
        _log(status_cb, f"Search Controller: adaptation failed ({exc.__class__.__name__}).")
        return []
    diagnosis = str(data.get("diagnosis") or "strategy changed after weak search results").strip()
    if diagnosis:
        _log(status_cb, "Search Controller: " + diagnosis[:300])
    by_id = {it.scene_id: it for it in weak_intents}
    out, seen = [], {(tuple(sorted(row["platforms"])), sanitize_platform_query(row["query"]).casefold())
                     for row in searched_rows}
    rows = data.get("queries") if isinstance(data, dict) else {}
    # ONE PER BEAT BEFORE ANY BEAT GETS A SECOND. This used to append until the limit was full
    # while walking the answer scene by scene, so the first beat in the dict drained the whole
    # allowance: measured on the school-lunch run, beat 1 took EIGHT retries, beat 5 took five,
    # and beats 3, 4 and 7 - all of them still empty - got none at all. Same starvation shape as
    # the coverage wave had, in a different loop.
    per_scene = {}
    for sid_raw, values in (rows.items() if isinstance(rows, dict) else []):
        try:
            sid = int(re.sub(r"[^0-9-]", "", str(sid_raw)))
        except (TypeError, ValueError):
            continue
        it = by_id.get(sid)
        if it is None or not isinstance(values, dict):
            continue
        for raw_platform, raw_queries in values.items():
            platform = _canonical_platform(raw_platform)
            if platform not in ("tiktok", "instagram"):
                continue
            language = "hashtag" if (platform == "instagram" and any(
                str(v or "").strip().startswith("#") for v in (raw_queries or []))) else "auto"
            cleaned = _clean_queries_for_platform(platform, raw_queries, language, limit=2)
            for text in cleaned:
                key = ((platform,), text.casefold())
                if not text or key in seen:
                    continue
                seen.add(key)
                lang = "ja" if re.search(r"[\u3040-\u30ff\u3400-\u9fff]", text) else "en"
                per_scene.setdefault(sid, []).append(SearchQueryV2(
                    query=text, language=lang, tier="semantic_action", platforms=[platform],
                    visual_intent_id=it.intent_id, scene_ids=[it.scene_id],
                    query_type="retry_adaptive", generated_from="current_scene_retry",
                    reason="adapted after weak current-scene results", expected_subject=it.subject,
                    expected_action=it.action, expected_location=it.location))
    return _interleave_by_scene(per_scene, limit)


def _interleave_by_scene(per_scene, limit):
    """Round-robin: every beat's first query before any beat's second, up to `limit`."""
    order = sorted(per_scene)
    out, depth = [], 0
    while order and len(out) < max(1, int(limit)):
        progressed = False
        for sid in order:
            rows = per_scene.get(sid) or []
            if depth >= len(rows):
                continue
            out.append(rows[depth])
            progressed = True
            if len(out) >= max(1, int(limit)):
                break
        if not progressed:
            break
        depth += 1
    return out


_LIBRARY_TOPIC_STOP = {
    "japan", "japanese", "short", "video", "people", "thing", "things", "this", "that",
    "with", "from", "into", "they", "their", "about", "first", "next", "then", "but",
}


def existing_fact_short_segments_v2(project_dir, script_text, ffmpeg, ffprobe,
                                    status_cb=None, max_projects=3, max_segments=18):
    """Load stable native-portrait clips from strongly related earlier projects.

    This is a cheap, local first pass.  It never trusts filenames or old scene positions: project
    scripts establish topic overlap, then the current vision matcher decides whether each clip
    actually fits a current narration beat.
    """
    project_dir = Path(project_dir)
    wanted = _tokens(script_text) - _LIBRARY_TOPIC_STOP
    if not wanted or not project_dir.parent.exists():
        return []
    related = []
    for candidate in project_dir.parent.iterdir():
        if not candidate.is_dir() or candidate.resolve() == project_dir.resolve():
            continue
        script_path = candidate / "input" / "script.txt"
        media_dir = candidate / "seedance 2.0"
        if not script_path.exists() or not media_dir.exists():
            continue
        try:
            old_script = script_path.read_text("utf-8", errors="replace")[:5000]
        except Exception:
            continue
        old_terms = _tokens(old_script) - _LIBRARY_TOPIC_STOP
        overlap = len(wanted & old_terms) / float(max(1, min(len(wanted), len(old_terms))))
        if overlap >= 0.42:
            related.append((overlap, candidate, media_dir))
    related.sort(key=lambda row: (row[0], row[1].stat().st_mtime), reverse=True)

    segments = []
    for overlap, candidate, media_dir in related[:max(1, int(max_projects))]:
        paths = sorted(media_dir.glob("scraped_*.mp4"),
                       key=lambda path: path.stat().st_mtime, reverse=True)
        for path in paths:
            if len(segments) >= max(1, int(max_segments)):
                break
            dims = clip_scraper._probe_dims(path, ffprobe)
            if not dims or not _is_native_9_16(*dims):
                continue
            duration = float(clip_scraper._probe_duration(path, ffprobe) or 0.0)
            if duration < 2.1:
                continue
            # Preserve enough source headroom for a 3.2s timeline beat without a freeze frame.
            end = min(duration, SCRAPE_V2_CONFIG["max_segment_seconds"])
            segment = SegmentCandidate(
                segment_id="library_" + hashlib.sha1(str(path.resolve()).encode(
                    "utf-8", "ignore")).hexdigest()[:16],
                source_id=f"library:{candidate.name}:{path.name}", platform="library",
                source_path=str(path), start_time=0.0, end_time=end, duration=end,
                creator_id=candidate.name, query=f"existing project: {candidate.name}",
                query_tier="existing_project", source_width=dims[0], source_height=dims[1],
                native_9_16=True, metadata_relevance=round(overlap * 10.0, 2))
            analyze_segment_v2(segment, ffmpeg, ffprobe, status_cb=status_cb)
            if segment.rejection_reasons or segment.quality_score < SEGMENT_SOFT_MIN_QUALITY:
                continue
            segments.append(segment)
        if len(segments) >= max(1, int(max_segments)):
            break
    if related:
        _log(status_cb, "Fact Short library-first: checked %d related project(s), kept %d stable "
             "portrait clip(s) for current-scene vision matching."
             % (min(len(related), max_projects), len(segments)))
    return segments


def resume_project_proxy_segments_v2(project_dir, ffmpeg, ffprobe, status_cb=None):
    """Re-open already downloaded V2 proxies from THIS interrupted project.

    A resume used to search the project library only for *other* projects, then start with an
    empty source-id set.  It therefore paid to download the same TikToks again while the original
    proxies were sitting in ``_v2_proxies``.  Re-discovering their windows is also valuable after a
    quality-analysis upgrade because the good moment may be later than the old inspection window.
    """
    root = Path(project_dir) / "seedance 2.0" / "_v2_proxies"
    if not root.is_dir():
        return [], set()
    segments, source_ids = [], set()
    paths = sorted(root.glob("proxy_*.*"), key=lambda path: path.stat().st_mtime)
    for path in paths:
        if path.suffix.casefold() not in {".mp4", ".mov", ".mkv", ".webm"}:
            continue
        # proxy_tiktok_123456.mp4 -> platform=tiktok/source_id=123456. Keep the stable
        # filename identity for non-numeric source IDs too; it still prevents a second fetch.
        match = re.match(r"^proxy_([^_]+)_(.+)$", path.stem, flags=re.I)
        if not match:
            continue
        platform, source_id = match.group(1).casefold(), match.group(2)
        if source_id in source_ids:
            continue
        source = SourceVideoCandidate(
            platform=platform, source_id=source_id, creator_id="resumed_source", url="",
            query="resumed project proxy", query_tier="resume_local",
            scene_ids=[], japanese_context=True)
        fresh = discover_segments_v2(source, path, ffmpeg, ffprobe, status_cb=status_cb)
        # These files already passed proxy download validation in the interrupted run.  A full
        # segment-quality pass here used to run OCR, cut scans and cadence decoding for every
        # window of every source before *any* vision matching.  Apart from making Continue take
        # several minutes, that duplicated the final render QA (which remains authoritative).
        # Keep representative early/late windows for semantic vision review now; the chosen clip
        # still receives the exhaustive trim/freeze/cut validation before it can render.
        if len(fresh) > 2:
            fresh = [fresh[0], fresh[-1]]
        dims = clip_scraper._probe_dims(path, ffprobe)
        for segment in fresh:
            if not dims:
                # Let the normal analyser make the rejection decision when probing fails.  This
                # also keeps the helper usable with an injected/test proxy reader.
                analyze_segment_v2(segment, ffmpeg, ffprobe, status_cb=status_cb)
                if segment.rejection_reasons or segment.quality_score < SEGMENT_SOFT_MIN_QUALITY:
                    continue
                segments.append(segment)
                continue
            if not _is_native_9_16(*dims):
                continue
            segment.source_width, segment.source_height = map(int, dims)
            segment.native_9_16 = True
            segment.quality_score = SEGMENT_MIN_QUALITY
            segment.quality_gate = "resume_fast_vision"
            segments.append(segment)
        source_ids.add(source_id)
    if segments:
        _log(status_cb, "Scrape V2 resume: reopened %d local proxy source(s), kept %d segment(s) "
                        "for the current search/match pass."
             % (len(source_ids), len(segments)))
    return segments, source_ids


# ---------------------------------------------------------------- orchestrator

def scrape_social_plan_v2(config, scenes, project_dir, platforms, per_clip_seconds, script_relevancy,
                          cookies, cancel_check, understanding=None, reasoning_model=None,
                          status_cb=None, script_text=""):
    """Relevance-first, segment-based orchestrator. Writes scene['clip'] etc. directly and returns the
    V1-compatible 7-tuple. Stashes assignments + debug report on config['_scrape_v2']. Loops until
    enough final usable segments exist for every scene or a budget is hit."""
    cfg = SCRAPE_V2_CONFIG
    # USER-SET SEARCH TIME. The run form carries scrape_time_budget in seconds; how long a topic
    # is worth searching for is an editorial call, not a constant. Clamped to the range the UI
    # offers so a malformed form cannot hand the scrape an unbounded or useless deadline.
    _budget = config.get("scrape_time_budget") if isinstance(config, dict) else None
    try:
        _budget = int(float(_budget)) if str(_budget or "").strip() else 0
    except (TypeError, ValueError):
        _budget = 0
    if _budget:
        cfg = dict(cfg)
        cfg["max_total_scrape_time_seconds"] = max(1800, min(7200, _budget))
    try:
        import scrape_browser_preview
        scrape_browser_preview.clear()
        scrape_browser_preview.set_owner(Path(project_dir).name)
    except Exception:
        pass
    # Backend result order chosen by the user (default RELEVANCE = TikTok's topical order, which
    # returns far more on-topic Japanese footage than MOST_LIKED's Western viral bias).
    sort_mode = str(config.get("scrape_sort") or config.get("search_sort") or "RELEVANCE").upper()
    if sort_mode not in ("RELEVANCE", "MOST_LIKED", "MOST_VIEWED", "MOST_RECENT", "ALL"):
        sort_mode = "RELEVANCE"
    t0 = time.monotonic()
    deadline = t0 + cfg["max_total_scrape_time_seconds"]
    clip_scraper.set_cookies(cookies)
    try:
        clip_scraper.reset_backend_search_health()
    except Exception:
        pass
    ffmpeg, ffprobe = clip_scraper._ffmpeg_tools()
    state = {"rejections": {}, "scene_reports": [], "_downloaded_ids": set(), "_fetched_ids": set()}
    # Declared with `state`, not beside the coverage wave: the resume path matches long before
    # that line runs, and referencing it there died with "cannot access local variable".
    _matcher_stats = state.setdefault("matcher_stats", {})
    chapter_mode = any(bool(scene.get("visual_chapter")) for scene in (scenes or []))

    # The strongest reference edits use native portrait footage. Cropping landscape X clips often
    # made the subject unrecognisable; all Clip Shorts now reject those before vision matching.
    state["native_vertical_only"] = True
    _log(status_cb, "Scrape V2 quality profile: native 9:16 footage only.")
    if str(config.get("clip_short_format") or "").lower() == "mini_story":
        understanding = dict(understanding or {})
        understanding.setdefault(
            "editorial_format",
            "Mini Story: one continuous real event with setup, escalation and payoff; never a listicle")
        understanding.setdefault(
            "footage_strategy",
            "Prefer one source video or one tightly related event/location cluster for every scene; "
            "generic topical filler is not acceptable. Use only native 9:16 source footage; never "
            "crop, stack or pad landscape footage into a portrait canvas.")
        _log(status_cb, "Scrape V2 Mini Story: coherent source clusters + native 9:16 only.")

    _log(status_cb, "Scrape V2: Visual scenes planning...")
    intents = build_viral_search_plan_v2(config.get("title") or "", script_text or "", scenes,
                                         understanding=understanding, reasoning_model=reasoning_model,
                                         status_cb=status_cb)
    try:
        _relevancy = max(0, min(100, int(float(script_relevancy))))
    except (TypeError, ValueError):
        _relevancy = 90
    for _intent in intents:
        # Kept on the intent so every normal, retry and rescue matcher call uses the same floor.
        _intent.script_relevancy = _relevancy
    _ref_rules = str(config.get("pipeline_version") or "v0.2") != "v0.1"
    if _ref_rules:
        # v0.2 MULTI-CLIP PER SENTENCE: sub-beats split from one sentence (scene["beat_group"])
        # share ONE search intent - identical candidate lists + the 1-scene-per-segment cap make
        # the global assignment spread the matcher's top candidates across the sub-beats
        # ("mass proves the thesis") without any extra searching.
        _by_id = {it.scene_id: it for it in intents}
        _groups = {}
        for _i, _sc in enumerate(scenes):
            _bg = str(_sc.get("beat_group") or "")
            if _bg:
                _groups.setdefault(_bg, []).append(_i)
        _prop = 0
        import copy as _copy
        for _bg, _ids in _groups.items():
            _lead = next((_by_id[i] for i in _ids if i in _by_id), None)
            if _lead is None:
                continue
            for _i in _ids:
                if _i == _lead.scene_id or _i not in _by_id:
                    continue
                _clone = _copy.copy(_lead)
                _clone.scene_id = _i
                _clone.scene_text = _by_id[_i].scene_text
                _by_id[_i] = _clone
                _prop += 1
        if _prop:
            intents = sorted(_by_id.values(), key=lambda x: x.scene_id)
            _log(status_cb, f"v0.2 pacing: {_prop} sub-beat(s) share their sentence's intent "
                            "(top candidates will spread across them).")
    use_influencer_hook = bool(config.get("influencer_hook", False))
    body_intents = ([it for it in intents if it.scene_id != 0]
                    if use_influencer_hook else list(intents))
    hook_intent = (next((it for it in intents if it.scene_id == 0), None)
                   if use_influencer_hook else None)
    cfg = dict(cfg)
    cfg["required_duration_by_scene"] = {
        str(index): max(0.0, float(scene.get("end", 0.0) or 0.0)
                        - float(scene.get("start", 0.0) or 0.0))
        for index, scene in enumerate(scenes)
    }
    cfg["visual_chapter_scene_ids"] = [
        index for index, scene in enumerate(scenes) if bool(scene.get("visual_chapter"))
    ]
    _visual_chapters = set(cfg["visual_chapter_scene_ids"])
    for _intent in intents:
        # A long chapter is deliberately edited as overview -> action -> result. Each source
        # segment must prove its own sub-action, but no single four-second TikTok window has to
        # prove every sentence in a ten-second chapter by itself.
        _intent.allows_partial_coverage = _intent.scene_id in _visual_chapters
    # The download cap has to know how many beats it is feeding. A 14-scene fact short with a
    # 36-download pool starves by arithmetic: the first scenes take what they need and the
    # rest get an empty list back from every later call. Guarantee a per-scene share, and
    # raise the pool when the script has more beats than the flat cap was written for.
    scene_count = max(1, len(body_intents))
    per_scene_downloads = int(cfg.get("min_downloads_per_scene", 3))
    if scene_count * per_scene_downloads > cfg["max_downloaded_analysis_videos"]:
        cfg = dict(cfg)
        # +6 headroom for the opening-hook gather, which runs before any beat and used to
        # take a quarter of the pool off the top - beats 12 and 13 then downloaded nothing
        # on their first and only search.
        cfg["max_downloaded_analysis_videos"] = scene_count * per_scene_downloads + 6
        _log(status_cb, "Scrape V2: %d beats -> download pool raised to %d so every beat can "
                        "reach footage." % (scene_count, cfg["max_downloaded_analysis_videos"]))

    def _round_budget(scenes_in_round):
        """Downloads this round may ADD, so early scenes cannot drain the whole pool.

        _download_and_segment compares the global downloaded-id count against whatever it is
        handed, so handing it "already spent + this round's share" caps the round instead of
        the run. On the Tokyo project every call was handed the global cap, chunk 0 (scenes 0
        and 1) consumed all 36, and the remaining twelve scenes could not download at all -
        no matter how much time or how many queries were left.
        """
        spent = len(state.get("_fetched_ids") or ())
        share = per_scene_downloads * max(1, int(scenes_in_round))
        return min(cfg["max_downloaded_analysis_videos"], spent + share)

    queries_by_scene, search_plan_audit = build_scene_bound_query_plan(
        intents, project_dir, script_text, use_influencer_hook=use_influencer_hook,
        status_cb=status_cb, active_platforms=platforms)
    state["search_plan_audit"] = search_plan_audit
    config["search_plan_script_sha256"] = search_plan_audit["script_sha256"]
    _log(status_cb, "Scrape V2 hook mode: " + (
        "optional cute/dance presenter (20K+ likes)." if use_influencer_hook
        else "topic-matched footage; no presenter search."))

    scene_candidates = {}
    seen_source_ids = set()
    _intent_by_scene = {intent.scene_id: intent for intent in body_intents}
    _chapter_scene_ids = {int(value) for value in cfg.get("visual_chapter_scene_ids", [])}

    def _has_a_match(scene_id):
        """A chapter is solved only when approved shots cover its complete real duration."""
        rows = [row for row in (scene_candidates.get(scene_id) or [])
                if str(row.get("match_class") or "") == "A_MATCH"]
        if not rows:
            return False
        if scene_id not in _chapter_scene_ids:
            return True
        need = float((cfg.get("required_duration_by_scene") or {}).get(
            str(scene_id), (cfg.get("required_duration_by_scene") or {}).get(scene_id, 0.0)) or 0.0)
        intent = _intent_by_scene.get(scene_id)
        return bool(plan_chapter_montage_rows(
            rows[0].get("segment"), rows, need, intent,
            source_owner={}, scene_idx=scene_id, max_shots=4))

    def _has_editorial_choice(scene_id):
        """Return true only when a scene has multiple independently sourced usable options."""
        rows = list(scene_candidates.get(scene_id) or [])
        eligible = [
            row for row in rows
            if row.get("segment") is not None
            and str(row.get("match_class") or "") in {"A_MATCH", "B_MATCH", "C_MATCH"}
        ]
        distinct = {str(row["segment"].source_id) for row in eligible}
        minimum = max(2, int(cfg.get("min_distinct_matches_per_scene", 3) or 3))
        if len(distinct) < minimum:
            return False
        if scene_id not in _chapter_scene_ids:
            return True
        need = float((cfg.get("required_duration_by_scene") or {}).get(str(scene_id), 0.0) or 0.0)
        intent = _intent_by_scene.get(scene_id)
        return bool(plan_chapter_montage_rows(
            eligible[0].get("segment"), eligible, need, intent,
            source_owner={}, scene_idx=scene_id, max_shots=4))

    def _beat_is_strong(scene_id):
        """Does this beat have a clip worth stopping the search for?

        Coverage and quality are different questions and only the first was being asked. The
        Zauo run had the best raw offering of any run - 1113 search results - and stopped after
        43 minutes of a 120-minute budget with 28 downloads, because every beat held three
        mediocre C-matches and that satisfied "has editorial choice". Its beats came out at 5.05
        to 7.10. The Shinkansen run, which kept going because beats were still EMPTY, reached
        9.15 with 173 downloads. The difference was not the footage available; it was being
        allowed to keep looking.
        """
        rows = scene_candidates.get(scene_id) or []
        best = max((float(row.get("overall_match") or 0.0) for row in rows), default=0.0)
        return best >= GOOD_ENOUGH_MATCH

    query_queue = []
    coverage_queries = []
    coverage_by_scene = []
    # Round zero is deliberately chapter-fair: execute exactly ONE highest-priority relevance
    # query for every chapter.  Only chapters that fail that pass reach their second/third query.
    # This prevents a strong result from still triggering two minutes of redundant searches.
    # Previously tier sorting put all sumo variants first, so the 25-minute deadline expired
    # before heels, glasses, Mount Omine or the royal-family searches were even attempted.
    for it in body_intents:
        intent_queries = queries_by_scene.get(it.scene_id, [])
        if intent_queries:
            chosen = intent_queries[:1]
            coverage_by_scene.append(chosen)
            query_queue.extend(q for q in intent_queries if q not in chosen)

    # Round-robin, not scene block by scene block. The queue used to run scene 0's two
    # queries, then scene 1's, and so on, and the executor takes it four at a time - so the
    # first chunk WAS scenes 0 and 1. When the clock ran out there, scenes 2-13 had never
    # been searched once. Interleaving means every beat gets its first search before any
    # beat gets its second, and a run that is cut short is cut short evenly.
    coverage_queries = [chosen[depth] for depth in range(max((len(c) for c in coverage_by_scene),
                                                            default=0))
                        for chosen in coverage_by_scene if len(chosen) > depth]
    # The coverage wave was NOT deduplicated, only the queue behind it was. One browser
    # search costs about two minutes, and the run is deadline-bound: on the suppin short
    # six of the sixteen searches that fit were repeats ("身だしなみ チェック" three times,
    # for scenes 9, 10 and 11), while scenes 12-15 were never searched at all. The matcher
    # assigns whatever a text finds to EVERY scene it fits, so searching it once is not a
    # loss of coverage - it is the same pool for a third of the price.
    _cov_by_key, _cov = {}, []
    for q in coverage_queries:
        key = _query_identity(q)
        if key in _cov_by_key:
            prior = _cov_by_key[key]
            prior.scene_ids = list(dict.fromkeys(list(prior.scene_ids or [])
                                                  + list(q.scene_ids or [])))
            continue
        _cov_by_key[key] = q
        _cov.append(q)
    if len(_cov) != len(coverage_queries):
        _log(status_cb, "Scrape V2: coverage wave %d -> %d searches after dropping repeated "
                        "terms (each one costs a full search)." % (len(coverage_queries), len(_cov)))
    coverage_queries = _cov
    query_queue.sort(key=lambda q: V2_QUERY_TIERS.index(q.tier) if q.tier in V2_QUERY_TIERS else 9)
    # BROAD-DISCOVERY DEDUPE (2026-07-12): after the 2/3-token trim many intents collapse onto
    # the same broad query (東京 / 日本 学校 ...). Search each text ONCE globally - the vision
    # matcher assigns the found segments to every scene anyway, so per-intent repeats only
    # burn browser time.
    _seen_qtext = {}
    _dq = []
    for q in query_queue:
        key = _query_identity(q)
        if key in _seen_qtext:
            prior = _seen_qtext[key]
            prior.scene_ids = list(dict.fromkeys(list(prior.scene_ids or [])
                                                  + list(q.scene_ids or [])))
            continue
        _seen_qtext[key] = q
        _dq.append(q)
    if len(_dq) != len(query_queue):
        _log(status_cb, "Scrape V2: %d broad queries after global dedupe (was %d)."
             % (len(_dq), len(query_queue)))
    query_queue = _dq
    # Never append a global filler pool. Every body query must remain attributable
    # to an active scene and visual intent from this exact script.

    # Resume the current project's own downloaded proxies before looking at older related projects
    # or hitting the network.  Seed the attempted-id budget too, so backend results cannot trigger
    # an accidental second download of a source we already have locally.
    resumed_segments, resumed_source_ids = resume_project_proxy_segments_v2(
        project_dir, ffmpeg, ffprobe, status_cb=status_cb)
    all_segments = list(resumed_segments)
    if resumed_source_ids:
        state["_downloaded_ids"].update(resumed_source_ids)
        state["resumed_proxy_sources"] = len(resumed_source_ids)
        state["segments_resumed"] = len(resumed_segments)
        resumed_matches = match_segments_to_scenes_v2(
            body_intents, resumed_segments, reasoning_model=reasoning_model, status_cb=status_cb, stats=_matcher_stats)
        for sid, candidates in resumed_matches.items():
            scene_candidates.setdefault(sid, []).extend(candidates)
            scene_candidates[sid].sort(key=lambda row: row["overall_match"], reverse=True)
            scene_candidates[sid] = scene_candidates[sid][:6]
        state["segments_semantic_passed"] = sum(len(v) for v in scene_candidates.values())
    # LOCAL LIBRARY FIRST: re-check earlier same-topic clips before spending time on a fresh
    # browser search. The current vision matcher, not the old filename/timing, decides placement.
    library_segments = existing_fact_short_segments_v2(
        project_dir, script_text, ffmpeg, ffprobe, status_cb=status_cb)
    if library_segments:
        all_segments.extend(library_segments)
        describe_segments_v2(library_segments, project_dir, ffmpeg,
                             reasoning_model=reasoning_model, status_cb=status_cb)
        library_matches = match_segments_to_scenes_v2(
            body_intents, library_segments, reasoning_model=reasoning_model,
            status_cb=status_cb, stats=_matcher_stats)
        for sid, candidates in library_matches.items():
            scene_candidates.setdefault(sid, []).extend(candidates)
            scene_candidates[sid].sort(key=lambda row: row["overall_match"], reverse=True)
            scene_candidates[sid] = scene_candidates[sid][:6]
        covered_from_library = {sid for sid in scene_candidates if _has_a_match(sid)}
        # A library clip can only be USED ONCE - the final assignment never puts the same
        # footage on two beats. Coverage, though, was counted per scene independently, so
        # three library clips that each matched nine beats cancelled the search for all
        # nine. Six of them then reached the end with nothing of their own and borrowed
        # from a neighbour, having never been searched even once. (Measured on
        # romance_down_to_yen: 124 queries planned, 8 executed, 10 of 12 beats borrowed.)
        # So claim a scene as covered only while a DISTINCT clip is left for it, scarcest
        # scenes first.
        if covered_from_library:
            taken, truly_covered = set(), set()
            for sid in sorted(covered_from_library,
                              key=lambda s: len(scene_candidates.get(s) or [])):
                rows = [row for row in (scene_candidates.get(sid) or [])
                        if str(row.get("match_class") or "") == "A_MATCH"]
                need = float((cfg.get("required_duration_by_scene") or {}).get(str(sid), 0.0) or 0.0)
                if sid in _chapter_scene_ids:
                    plan = plan_chapter_montage_rows(
                        rows[0].get("segment") if rows else None, rows, need,
                        _intent_by_scene.get(sid), source_owner={}, scene_idx=sid)
                    needed = {row["segment"].source_id for row, _seconds in plan}
                else:
                    needed = ({rows[0]["segment"].source_id} if rows else set())
                if not needed or needed & taken:
                    continue
                taken.update(needed)
                truly_covered.add(sid)
            starved = len(covered_from_library) - len(truly_covered)
            covered_from_library = truly_covered
            coverage_queries = [query for query in coverage_queries
                                if not any(sid in covered_from_library
                                           for sid in (query.scene_ids or []))]
            _log(status_cb, "Fact Short library-first: %d scene(s) covered by a clip of their "
                 "own; scraping the remaining visual beats.%s"
                 % (len(covered_from_library),
                    (" %d more matched the same clips and are still searched, because that "
                     "footage can only be used once." % starved) if starved else ""))
    # --- OPENING HOOK gathering (RE-ADDED 2026-07-11): a dedicated search for a young-adult
    # Japanese female creator dancing / idol / playfully acting cute to camera. This is the
    # universal scroll-stopper for scene 0 and is gathered separately from the topical body pool.
    # BREADTH BEFORE DEPTH (2026-08-18, chocolate project): the coverage wave used to search
    # four beats, then download/describe/match them, then search the next four.  The first
    # batch's ANALYSIS ate the entire clock: of 33 planned queries only 10 ever ran, six of
    # them the hook preset, and beats 5-10 - honmei choco, White Day, the return gift - were
    # never searched at all.  Their footage could not exist in the pool, so the last third of
    # the video was borrowed duplicates and a freeze frame, and three of the five body clips
    # came from one single query.  Searching is the CHEAP half of the wave; do all of it first
    # so every beat is represented in the pool, then analyse.
    _cov_batches, _pending_corrections = [], []
    if coverage_queries and not (cancel_check and cancel_check()) and time.monotonic() < deadline:
        _sweep_deadline = min(deadline, time.monotonic()
                              + max(150.0, (deadline - time.monotonic()) * 0.4))
        _log(status_cb, "Scrape V2: breadth sweep - one search for each of %d body scene "
                        "concept(s) before any download or vision call."
             % len(coverage_queries))
        _swept = True
        for _cov_start in range(0, len(coverage_queries), 4):
            _cb = coverage_queries[_cov_start:_cov_start + 4]
            if not _swept:
                _cov_batches.append((_cb, None))      # searched later, if the clock allows
                continue
            if (cancel_check and cancel_check()) or time.monotonic() >= _sweep_deadline:
                _swept = False
                _log(status_cb, "Scrape V2: breadth sweep hit its time share with %d beat "
                                "concept(s) left; they are searched in the analysis rounds."
                     % len(coverage_queries[_cov_start:]))
                _cov_batches.append((_cb, None))
                continue
            _cov_batches.append((_cb, _search_sources(
                _cb, platforms, cancel_check, _sweep_deadline, seen_source_ids, state,
                status_cb, sort="RELEVANCE", coverage_pass=True) or []))
        _log(status_cb, "Scrape V2: breadth sweep found %d source(s) across %d beat concept(s)."
             % (sum(len(src or ()) for _q, src in _cov_batches), len(coverage_queries)))
    hook_presenter_segments = []
    if hook_intent is not None and not (cancel_check and cancel_check()) and time.monotonic() < deadline:
        # ONE opener needs one clip.  Six preset searches ran before any beat was
        # searched and were 60% of everything the chocolate run managed to execute.
        # Two birth-year presets sanitise down to the same "#女の子", and the Tokyo hotel run
        # spent two of its three hook searches on the identical term. Dedupe, then cap.
        hq, _hq_seen = [], set()
        for _q in (_hook_presenter_queries_v2(hook_intent) or []):
            _key = sanitize_platform_query(_q.query).casefold()
            if not _key or _key in _hq_seen:
                continue
            _hq_seen.add(_key)
            hq.append(_q)
            if len(hq) >= 3:
                break
        if hq:
            _log(status_cb, "Scrape V2: gathering a topic-aware young Japanese creator hook...")
            try:
                hook_platforms = list(platforms)
                hook_deadline = min(deadline, time.monotonic()
                                    + max(120.0, (deadline - time.monotonic()) * 0.2))
                hook_sources = _search_sources(hq, hook_platforms or ["tiktok"], cancel_check,
                                               hook_deadline, seen_source_ids,
                                               state, status_cb, sort="MOST_LIKED")
            except Exception:
                hook_sources = []
            hi_likes = int(getattr(_ac(), "HOOK_MIN_LIKES", 20000) or 20000)
            filt = [s for s in hook_sources if int(getattr(s, "likes", 0) or 0) >= hi_likes]
            # Measured: the hook gathered 30 clips in each of two runs - about a third of the
            # whole download budget, for ONE beat out of twelve. Five sources is plenty for an
            # opener and hands the rest back to the body.
            hook_sources = filt[:5]
            if not hook_sources:
                _log(status_cb, f"Scrape V2 hook: no candidate met the strict {hi_likes:,}-like floor; "
                                "the app will not substitute a low-engagement cute/dance clip.")
            if hook_sources:
                # The hook gather runs BEFORE any beat and used to be handed the whole
                # pool, taking a quarter of it off the top; beats 12 and 13 then had nothing
                # left to download with on their only search. One beat's share is plenty for
                # an opener.
                hook_presenter_segments = _download_and_segment(
                    hook_sources, project_dir, ffmpeg, ffprobe, cancel_check, deadline, state,
                    status_cb, _round_budget(1)) or []
                if hook_presenter_segments:
                    _log(status_cb, "Scrape V2: gathered %d hook-presenter clip(s)." % len(hook_presenter_segments))
    # SCENE-COVERAGE WAVE: search one topical query for every body intent under relevance before
    # any secondary query or popularity sort. Hook footage is intentionally kept out of the body
    # pool; cute/dance clips must never become emergency B-roll later in the video.
    if coverage_queries and not (cancel_check and cancel_check()) and time.monotonic() < deadline:
        _log(status_cb, "Scrape V2: coverage wave across %d body scene concept(s)..." % len(coverage_queries))
        intent_by_key = {it.intent_id: it for it in body_intents}
        # Small chunks are intentional: after four searches the controller can inspect real
        # outcomes and redirect the next round instead of discovering 20 searches later that the
        # platform interpreted every term incorrectly.
        # RESERVE THE CORRECTION. The analysis rounds used to be handed the run deadline, so the
        # last batch could run right up to it and the corrective phase - the only stage that can
        # rescue a beat whose one query returned nothing - got no clock at all. Two runs in a row
        # ended with zero adaptive queries executed. If no beat turns out to need correcting the
        # time is not lost: the secondary query queue below still uses the full deadline.
        _analysis_deadline = _reserve_for_correction(time.monotonic(), deadline)
        for _bi, (cov_batch, coverage_sources) in enumerate(_cov_batches):
            if (cancel_check and cancel_check()) or time.monotonic() >= _analysis_deadline:
                _log(status_cb, "Scrape V2: analysis rounds stopped at their time share with "
                                "%d batch(es) left, so the empty beats can still be corrected."
                     % (len(_cov_batches) - _bi))
                break
            before_raw = int(state.get("raw_results", 0) or 0)
            before_quality = int(state.get("segments_quality_passed", 0) or 0)
            # FAIR SHARE OF THE CLOCK: the first batch used to download and describe until the
            # deadline, so the later beats had their sources found and then never fetched.
            # Each remaining batch gets an equal slice of whatever time is left.
            _slice = _fair_round_deadline(time.monotonic(), _analysis_deadline,
                                          len(_cov_batches) - _bi)
            # Relevance always gets first look. Likes/views may rank a later pass, but never choose
            # the initial neighbourhood; that was the source of polished unrelated viral clips.
            if coverage_sources is None:
                coverage_sources = _search_sources(
                    cov_batch, platforms, cancel_check, _slice, seen_source_ids, state,
                    status_cb, sort="RELEVANCE", coverage_pass=True)
            coverage_segments = _download_and_segment(
                coverage_sources, project_dir, ffmpeg, ffprobe, cancel_check, _slice, state,
                status_cb, _round_budget(len({sid for q in cov_batch
                                              for sid in (q.scene_ids or [])})))
            warn_if_topic_is_unfilmable(state, status_cb)
            if coverage_segments:
                all_segments.extend(coverage_segments)
                describe_segments_v2(coverage_segments, project_dir, ffmpeg,
                                     reasoning_model=reasoning_model, status_cb=status_cb)
                coverage_matches = match_segments_to_scenes_v2(
                    body_intents, coverage_segments, reasoning_model=reasoning_model,
                    status_cb=status_cb, stats=_matcher_stats)
                for sid, cands in coverage_matches.items():
                    scene_candidates.setdefault(sid, []).extend(cands)
                    scene_candidates[sid].sort(key=lambda r: r["overall_match"], reverse=True)
                    scene_candidates[sid] = scene_candidates[sid][:6]
                state["segments_semantic_passed"] = sum(len(v) for v in scene_candidates.values())
            batch_intents, _batch_seen = [], set()
            for q in cov_batch:
                if q.visual_intent_id in intent_by_key and q.visual_intent_id not in _batch_seen:
                    _batch_seen.add(q.visual_intent_id)
                    batch_intents.append(intent_by_key[q.visual_intent_id])
            weak_now = [it for it in batch_intents if not _has_a_match(it.scene_id)]
            raw_delta = int(state.get("raw_results", 0) or 0) - before_raw
            quality_delta = int(state.get("segments_quality_passed", 0) or 0) - before_quality
            _log(status_cb, "Search Controller round: %d raw result(s), %d usable segment(s), "
                            "%d/%d scene concept(s) still unmatched."
                 % (raw_delta, quality_delta, len(weak_now), len(batch_intents)))
            # Adapt only when the round demonstrably failed. Every unresolved scene gets one
            # evidence-driven correction of its own; the old global cap of two meant later
            # chapters never learned from their failed results at all.
            adapted_scene_ids = {int(value) for value in
                                 (state.get("live_adapted_scene_ids") or [])}
            adaptable_now = [it for it in weak_now if it.scene_id not in adapted_scene_ids]
            # CORRECTION IS DEPTH, COVERAGE IS BREADTH. Correcting inline let the first batch's
            # four weak beats emit twelve corrective searches, and those searches plus their
            # downloads ended the run: on the Tokyo project beats 0 and 1 took ELEVEN corrective
            # queries while beats 2-14 got none, and the sushi footage that three queries had
            # already found - 18 results each - was never fetched. Bank the evidence and correct
            # every still-weak beat together, after every beat has been analysed once.
            if adaptable_now:
                _pending_corrections.append((adaptable_now, cov_batch, coverage_segments))

    # DEFERRED CORRECTION ROUND: now that every beat has been searched and analysed once, spend
    # what is left of the clock on the beats that are still empty - all of them, not whichever
    # ones happened to be in the first batch.
    if (_pending_corrections and config.get("use_llm_search", False)
            and not (cancel_check and cancel_check()) and time.monotonic() < deadline):
        _still_weak = [(intents, batch, segments) for intents, batch, segments in _pending_corrections
                       if any(not _has_a_match(it.scene_id) for it in intents)]
        _weak_total = sum(len([it for it in intents if not _has_a_match(it.scene_id)])
                          for intents, _b, _s in _still_weak)
        if _still_weak:
            _log(status_cb, "Search Controller: %d beat(s) are still empty; correcting every one "
                            "of them before any beat gets a third look." % _weak_total)
        # WORST FIRST. The groups arrive in coverage-batch order, so the corrective round spent
        # all eight of its queries on beats already scoring 7.2-7.75 while the robot beats at
        # 2.85 got none and the clock ran out. Correct where the hole is deepest.
        _still_weak.sort(key=lambda row: -sum(
            1 for it in row[0] if not _has_a_match(it.scene_id)))
        for _ci, (_intents, _batch, _segments) in enumerate(_still_weak):
            if (cancel_check and cancel_check()) or time.monotonic() >= deadline:
                _log(status_cb, "Search Controller: out of time with %d group(s) uncorrected."
                     % (len(_still_weak) - _ci))
                break
            _open = [it for it in _intents if not _has_a_match(it.scene_id)]
            if not _open:
                continue
            _cslice = _fair_round_deadline(time.monotonic(), deadline, len(_still_weak) - _ci)
            # Two corrective terms per empty beat. The old formula allowed twelve for a group of
            # four, which is a re-search of one chapter dressed up as a correction.
            adaptive = adapt_queries_from_live_round_v2(
                _open, _batch, _segments, reasoning_model=reasoning_model, status_cb=status_cb,
                limit=max(2, len(_open) * 2), rejection_summary=state.get("rejections") or {})
            fresh_adaptive = []
            for q in adaptive:
                key = _query_identity(q)
                if key in _seen_qtext:
                    continue
                # `_seen_qtext` is intentionally a mapping, not a set: earlier query
                # deduplication merges scene IDs into the original query object. Adaptive terms
                # must register in that same mapping or the next dedupe path either crashes
                # (`dict` has no .add) or loses provenance.
                _seen_qtext[key] = q
                fresh_adaptive.append(q)
            if not fresh_adaptive:
                continue
            _adapted = {int(value) for value in (state.get("live_adapted_scene_ids") or [])}
            _adapted.update(sid for query in fresh_adaptive for sid in (query.scene_ids or []))
            state["live_adapted_scene_ids"] = sorted(_adapted)
            state["live_adaptation_rounds"] = int(state.get("live_adaptation_rounds", 0) or 0) + 1
            state["live_adaptive_queries"] = state.get("live_adaptive_queries", 0) + len(fresh_adaptive)
            _log(status_cb, "Search Controller: executing %d changed-strategy query/queries for "
                            "%d empty beat(s)." % (len(fresh_adaptive), len(_open)))
            adaptive_sources = _search_sources(
                fresh_adaptive, platforms, cancel_check, _cslice, seen_source_ids, state,
                status_cb, sort=sort_mode, coverage_pass=True)
            adaptive_segments = _download_and_segment(
                adaptive_sources, project_dir, ffmpeg, ffprobe, cancel_check, _cslice,
                state, status_cb, _round_budget(len(_open)))
            if not adaptive_segments:
                continue
            all_segments.extend(adaptive_segments)
            describe_segments_v2(adaptive_segments, project_dir, ffmpeg,
                                 reasoning_model=reasoning_model, status_cb=status_cb)
            adaptive_matches = match_segments_to_scenes_v2(
                _open, adaptive_segments, reasoning_model=reasoning_model, status_cb=status_cb,
                stats=_matcher_stats)
            for sid, cands in adaptive_matches.items():
                scene_candidates.setdefault(sid, []).extend(cands)
                scene_candidates[sid].sort(key=lambda r: r["overall_match"], reverse=True)
                scene_candidates[sid] = scene_candidates[sid][:6]
            recovered = sum(1 for it in _open if _has_a_match(it.scene_id))
            _log(status_cb, "Search Controller: recovered %d/%d empty beat(s) in the corrective "
                            "round." % (recovered, len(_open)))
    BATCH = 4
    while query_queue:
        if (cancel_check and cancel_check()) or time.monotonic() >= deadline:
            break
        # Stop searching a chapter only after there are several independent matching uploads.
        # One A-match is coverage, but it is not editorial choice and cannot survive a later cut-QA
        # rejection without falling back to filler.
        before_prune = len(query_queue)
        query_queue = [query for query in query_queue
                       if not query.scene_ids or not all(_has_editorial_choice(sid)
                                                        for sid in query.scene_ids)]
        if len(query_queue) != before_prune:
            state["queries_skipped_after_a_match"] = (
                state.get("queries_skipped_after_a_match", 0) + before_prune - len(query_queue))
            _log(status_cb, "Search Controller: skipped %d queued search(es); their chapter "
                            "already has an A-match." % (before_prune - len(query_queue)))
        if not query_queue:
            break
        matched_scenes = sum(1 for sid, c in scene_candidates.items() if c)
        # STRICT no-reuse: every scene needs its OWN source, so keep searching until the pool
        # holds at least as many DISTINCT sources as there are scenes (many candidates share
        # the same TikTok). Only then is it possible to fill the render without reusing a source.
        distinct_sources = len({c["segment"].source_id
                                for cands in scene_candidates.values() for c in cands})
        if (all(_has_editorial_choice(it.scene_id) for it in body_intents)
                and all(_beat_is_strong(it.scene_id) for it in body_intents)
                and matched_scenes >= len(body_intents)
                and distinct_sources >= len(body_intents) * 2
                and len(all_segments) >= len(body_intents) * 3):
            break
        uncovered_now = [it for it in body_intents if not _has_editorial_choice(it.scene_id)]
        # Compare ATTEMPTS against the cap, because attempts are what _round_budget spends.
        # downloaded_sources counts only successful fetches; with any dead links the two
        # diverge permanently, the raise below never fires, and every later round plans zero
        # downloads in silence while the queue and the clock still have room. Attempts means
        # this run's fetches - proxies resumed from disk cost nothing and must not count.
        if len(state.get("_fetched_ids") or ()) >= cfg["max_downloaded_analysis_videos"]:
            if not uncovered_now:
                break
            # A chapter with only one surviving option is still under-sourced. Reserve a smaller
            # corrective allowance for every chapter that lacks editorial choice.
            cfg = dict(cfg)
            cfg["max_downloaded_analysis_videos"] += max(2, per_scene_downloads // 2) * len(uncovered_now)
            _log(status_cb, "Scrape V2: %d chapter(s) still lack enough independent footage; extending the "
                            "download pool to %d rather than falling back to stills."
                 % (len(uncovered_now), cfg["max_downloaded_analysis_videos"]))
        batch_q = query_queue[:BATCH]
        query_queue = query_queue[BATCH:]
        _log(status_cb, "Scrape V2: searching %s ... (%d queries queued)" % (batch_q[0].tier, len(query_queue)))
        secondary_sort = "MOST_LIKED" if sort_mode == "RELEVANCE" else sort_mode
        sources = _search_sources(batch_q, platforms, cancel_check, deadline, seen_source_ids, state,
                                  status_cb, sort=secondary_sort)
        _log(status_cb, "Scrape V2: ranking %d source video(s) by relevance..." % len(sources))
        segs = _download_and_segment(sources, project_dir, ffmpeg, ffprobe, cancel_check, deadline,
                                     state, status_cb,
                                     _round_budget(max(1, len(uncovered_now)) if uncovered_now
                                                   else len(batch_q)))
        if not segs:
            continue
        all_segments.extend(segs)
        _log(status_cb, "Scrape V2: describing clips (%d new segment(s))..." % len(segs))
        describe_segments_v2(segs, project_dir, ffmpeg, reasoning_model=reasoning_model, status_cb=status_cb)
        _log(status_cb, "Scrape V2: matching clips to scenes...")
        new_matches = match_segments_to_scenes_v2(body_intents, segs, reasoning_model=reasoning_model,
                                                  status_cb=status_cb, stats=_matcher_stats)
        # USER-FACING SCRAPE LOG: one entry per evaluated clip - thumbnails, search term,
        # what the vision AI saw, per-scene scores + accept/reject verdict with reason.
        intent_text = {it.scene_id: (it.scene_text or "")[:160] for it in body_intents}
        for seg in segs:
            vd = seg.visual_description or {}
            best = None
            scored_scenes = []
            for sid, cands in (new_matches or {}).items():
                for c in cands:
                    if c["segment"].segment_id == seg.segment_id:
                        row = {"scene": sid, "scene_text": intent_text.get(sid, ""),
                               "overall": c["overall_match"], "script_match": c["script_match"],
                               "match_class": c["match_class"], "reason": c.get("reason", "")}
                        scored_scenes.append(row)
                        if best is None or row["overall"] > best["overall"]:
                            best = row
            if scored_scenes:
                verdict = "candidate"
                why = (f"passed match floors for scene(s) "
                       f"{', '.join(str(r['scene']) for r in scored_scenes)}"
                       + (f" - best: {best['reason']}" if best and best.get("reason") else ""))
            else:
                verdict = "rejected"
                why = ("below the semantic match floors for every scene "
                       "(subject/action/location did not support any narration line strongly enough)")
                # Semantic rejections are still real, downloaded footage. Preserve a single
                # source copy in the Declined library instead of making the user hunt through
                # transient V2 proxies after the run.
                try:
                    source = Path(seg.source_path)
                    if source.exists():
                        droot = Path(project_dir) / "seedance 2.0" / "_declined"
                        droot.mkdir(parents=True, exist_ok=True)
                        key = hashlib.sha1(str(seg.source_id).encode("utf-8", "ignore")).hexdigest()[:12]
                        target = droot / f"declined_v2_{key}.mp4"
                        if not target.exists():
                            shutil.copy2(source, target)
                        target.with_suffix(".json").write_text(json.dumps({
                            "status": "rejected_semantic", "platform": seg.platform,
                            "clip_id": seg.source_id, "query": seg.query or "", "reason": why,
                        }, indent=2, ensure_ascii=False), "utf-8")
                except Exception:
                    pass
            _slog(project_dir, {
                "type": "clip", "segment_id": seg.segment_id, "platform": seg.platform,
                "clip_id": seg.source_id, "query": seg.query or "",
                "thumbs": _slog_thumbs(seg, project_dir, ffmpeg),
                "vision": {k: vd.get(k) for k in ("subjects", "action", "location",
                                                  "camera_style", "visible_text",
                                                  "burned_captions", "raw_footage_score",
                                                  "visual_quality") if k in vd},
                "quality_score": round(float(seg.quality_score or 0), 1),
                "verdict": verdict, "why": why, "scenes": scored_scenes[:4],
            })
            if scored_scenes:
                try:
                    import scrape_browser_preview
                    _pstart = 0.0 if seg.final_path else float(seg.start_time or 0.0)
                    _poster = _accepted_poster(seg, project_dir, ffmpeg, _pstart)
                    scrape_browser_preview.mark_accepted(
                        seg.final_path or seg.source_path,
                        platform=seg.platform, query=seg.query or "", clip_id=seg.source_id,
                        start=_pstart, poster=_poster)
                except Exception:
                    pass
        for sid, cands in new_matches.items():
            scene_candidates.setdefault(sid, [])
            scene_candidates[sid].extend(cands)
            scene_candidates[sid].sort(key=lambda r: r["overall_match"], reverse=True)
            scene_candidates[sid] = scene_candidates[sid][:6]
        state["segments_semantic_passed"] = sum(len(v) for v in scene_candidates.values())

    # RESCUE RE-MATCH: if the semantic matcher produced ZERO candidates across ALL scenes
    # (single silent LLM failure, truncated JSON, timeout...), one more attempt over the best
    # described segments - this is cheap next to the scrape and prevents the whole run from
    # degrading into random emergency-fallback clips.
    if config.get("use_llm_search", False) and not any(
            scene_candidates.get(it.scene_id) for it in body_intents):
        described_all = [s for s in all_segments if s.visual_description]
        if described_all:
            _log(status_cb, "Scrape V2: matcher yielded 0 candidates for every scene - "
                            "running a rescue re-match over %d described segment(s)..."
                 % len(described_all))
            best = sorted(described_all, key=lambda s: s.quality_score, reverse=True)[:40]
            rescue = match_segments_to_scenes_v2(body_intents, best,
                                                 reasoning_model=reasoning_model, status_cb=status_cb, stats=_matcher_stats)
            for sid, cands in rescue.items():
                scene_candidates.setdefault(sid, [])
                scene_candidates[sid].extend(cands)
                scene_candidates[sid].sort(key=lambda r: r["overall_match"], reverse=True)
                scene_candidates[sid] = scene_candidates[sid][:6]
            state["segments_semantic_passed"] = sum(len(v) for v in scene_candidates.values())

    # ---- STRICT 3-STAGE ESCALATION (user rule 2026-07-13) ----
    # A scene may only drop to a filler/fallback clip AFTER it has bled through, in order:
    #   1) cross-platform pivot (its terms re-sent to X Media + Instagram),
    #   2) a fresh lateral-agent query set (numbers / cringe / trend terms),
    #   3) the global thematic filler pool.
    # Re-assign after every stage: a scene that lands a UNIQUE clip drops out of the escalation,
    # so later stages only work the scenes that are still genuinely uncovered.
    def _merge_matches(new_matches):
        for sid, cands in (new_matches or {}).items():
            scene_candidates.setdefault(sid, []).extend(cands)
            scene_candidates[sid].sort(key=lambda r: r["overall_match"], reverse=True)
            scene_candidates[sid] = scene_candidates[sid][:6]

    def _uncovered_intents():
        # a scene is "covered" only if the strict-source assigner can give it its OWN clip
        asg = assign_segments_globally_v2(body_intents, scene_candidates, cfg)
        owners = {}
        for sid, assignment in asg.items():
            segment = getattr(assignment, "_seg", None)
            if segment is not None:
                owners[segment.source_id] = sid
        uncovered_rows = []
        for it in body_intents:
            assignment = asg.get(it.scene_id)
            primary = getattr(assignment, "_seg", None) if assignment else None
            if primary is None:
                uncovered_rows.append(it)
                continue
            if it.scene_id in _chapter_scene_ids:
                need = float((cfg.get("required_duration_by_scene") or {}).get(
                    str(it.scene_id), 0.0) or 0.0)
                rows = [row for row in (scene_candidates.get(it.scene_id) or [])
                        if str(row.get("match_class") or "") == "A_MATCH"]
                if not plan_chapter_montage_rows(
                        primary, rows, need, it, source_owner=owners,
                        scene_idx=it.scene_id, max_shots=4):
                    uncovered_rows.append(it)
        return uncovered_rows

    uncovered = _uncovered_intents()
    if (config.get("use_llm_search", False) and uncovered
            and time.monotonic() < deadline):
        _log(status_cb, "Escalation 1/3 (cross-platform pivot): %d scene(s) still without a "
                        "unique clip - re-sending their terms to X + Instagram..." % len(uncovered))
        _merge_matches(escalate_platform_pivot_v2(
            uncovered, platforms, project_dir, ffmpeg, ffprobe, cancel_check, deadline, state,
            seen_source_ids, reasoning_model=reasoning_model, status_cb=status_cb,
            download_budget=_round_budget(len(uncovered))))
        uncovered = _uncovered_intents()

    if (config.get("use_llm_search", False) and uncovered
            and time.monotonic() < deadline):
        _log(status_cb, "Escalation 2/3 (lateral-agent): %d scene(s) still uncovered - generating "
                        "fresh creative queries (numbers / cringe / trends)..." % len(uncovered))
        _merge_matches(retry_unmatched_scenes_v2(
            uncovered, platforms, project_dir, ffmpeg, ffprobe, cancel_check, deadline, state,
            seen_source_ids, reasoning_model=reasoning_model, status_cb=status_cb,
            download_budget=_round_budget(len(uncovered))))
        uncovered = _uncovered_intents()

    if (config.get("use_llm_search", False) and uncovered and not chapter_mode
            and time.monotonic() < deadline):
        # STAGE 3 - global thematic filler pool: match the still-uncovered scenes against the
        # broad on-theme B-roll already gathered (lateral filler + surplus hook clips), relaxed.
        described_pool = sorted((s for s in all_segments if s.visual_description),
                                key=lambda s: s.quality_score, reverse=True)[:60]
        if described_pool:
            _log(status_cb, "Escalation 3/3 (global filler bucket): %d scene(s) - matching the "
                            "thematic B-roll pool..." % len(uncovered))
            state["escalation_stage3"] = state.get("escalation_stage3", 0) + 1
            _merge_matches(match_segments_to_scenes_v2(
                uncovered, described_pool, reasoning_model=reasoning_model, status_cb=status_cb,
                stats=_matcher_stats))
            uncovered = _uncovered_intents()
    elif uncovered and chapter_mode:
        _log(status_cb, "Fact Short chapters: skipped the global filler bucket; only scene-bound "
                        "or concrete recovery searches may fill an uncovered chapter.")
    # FULL-POOL SECOND LOOK. The matcher is called once per download batch, so it judges 4-19
    # segments at a time and never sees the pool as a whole - a batch of four can only answer
    # "none of these fit", which is exactly what a run reports while a fitting clip from an
    # earlier batch sits unused. Measured on one konbini pool: the same segments and the same
    # floors gave 1 of 5 beats a match batch-by-batch and 4 of 5 when judged together.
    #
    # This is NOT the filler bucket: no relaxation, the same floors decide, and it only looks at
    # beats that are still empty. That is why it may run in chapter mode too, where the filler
    # bucket is rightly forbidden.
    if config.get("use_llm_search", False) and uncovered and time.monotonic() < deadline:
        whole_pool = [s for s in all_segments if s.visual_description]
        if len(whole_pool) > 12:
            _log(status_cb, "Scrape V2 full-pool second look: re-judging %d described segment(s) "
                            "against %d still-empty beat(s) - the batch matcher never saw them "
                            "side by side." % (len(whole_pool), len(uncovered)))
            state["full_pool_second_look"] = state.get("full_pool_second_look", 0) + 1
            _merge_matches(match_segments_to_scenes_v2(
                uncovered, whole_pool, reasoning_model=reasoning_model, status_cb=status_cb,
                stats=_matcher_stats))
            recovered_now = len(uncovered) - len(_uncovered_intents())
            _log(status_cb, "Scrape V2 full-pool second look: recovered %d beat(s)." % recovered_now)
            uncovered = _uncovered_intents()
    # Stage 4 is a deterministic, topic-specific recovery after the LLM's broad queries were
    # exhausted. It matters most for dating scripts: "woman reservation" is a syntactically valid
    # query but does not remotely prove matching outfits, Christmas dates or gift exchanges.
    if uncovered and time.monotonic() < deadline:
        relationship_queries = relationship_recovery_queries_v2(uncovered)
        if relationship_queries:
            _log(status_cb, "Escalation 4/4 (relationship recovery): %d concrete native query/queries "
                            "for %d uncovered scene(s)..." %
                 (len(relationship_queries), len(uncovered)))
            state["escalation_stage4"] = state.get("escalation_stage4", 0) + 1
            relationship_sources = _search_sources(
                relationship_queries, platforms, cancel_check, deadline, seen_source_ids, state,
                status_cb, sort="RELEVANCE", coverage_pass=True)
            relationship_segments = _download_and_segment(
                relationship_sources, project_dir, ffmpeg, ffprobe, cancel_check, deadline, state,
                status_cb, cfg["max_downloaded_analysis_videos"])
            if relationship_segments:
                all_segments.extend(relationship_segments)
                describe_segments_v2(relationship_segments, project_dir, ffmpeg,
                                     reasoning_model=reasoning_model, status_cb=status_cb)
                _merge_matches(match_segments_to_scenes_v2(
                    uncovered, relationship_segments, reasoning_model=reasoning_model,
                    status_cb=status_cb, stats=_matcher_stats))
                uncovered = _uncovered_intents()
    if uncovered:
        _log(status_cb, "Escalation exhausted: %d scene(s) still have no unique clip after all 4 "
                        "stages. The run continues into targeted recovery; it will never render "
                        "unrelated emergency filler."
             % len(uncovered))

    # Deterministic rescue is deliberately AFTER every search stage. Previously it ran first and
    # promoted a merely topically adjacent clip to an "exact" match, which prevented the targeted
    # cross-platform and lateral searches from ever running. A rescue also needs evidence for a
    # concrete action, location, or required visual element -- generic subjects such as "people"
    # or "couple" are not enough to illustrate a specific narrated beat.
    described_all = [s for s in all_segments if s.visual_description]
    for it in body_intents:
        if scene_candidates.get(it.scene_id) or not described_all:
            continue
        want = _tokens(" ".join((
            it.scene_text, it.subject, it.action, it.location, it.story_subject,
            " ".join(it.required_elements or []),
        )))
        concrete = _tokens(" ".join((
            it.action, it.location, " ".join(it.required_elements or []),
        )))
        ranked = []
        for seg in described_all:
            # A scene-bound search result may never be rescued into another scene just because it
            # shares one vague word.  Only deliberately global/library material can serve as an
            # evidence-based contextual fallback.
            if seg.scene_ids and it.scene_id not in seg.scene_ids:
                continue
            vd = seg.visual_description or {}
            have = _tokens(" ".join((
                str(vd.get("subjects") or ""), str(vd.get("action") or ""),
                str(vd.get("location") or ""), str(vd.get("visible_text") or ""),
                str(seg.query or ""),
            )))
            concrete_overlap = concrete & have
            overlap = len(want & have) / float(max(1, len(want)))
            if not concrete_overlap or overlap < 0.12:
                continue
            score = round(min(6.0, 3.0 + overlap * 4.0), 2)
            if score < CONTEXT_FALLBACK_MIN_RELEVANCE:
                continue
            ranked.append((overlap, float(seg.quality_score or 0.0), seg, score))
        ranked.sort(key=lambda row: (row[0], row[1]), reverse=True)
        if ranked:
            _overlap, _quality, seg, score = ranked[0]
            scene_candidates[it.scene_id] = [{
                "segment": seg, "subject_match": score, "action_match": score,
                "location_match": score, "mood_match": score, "script_match": score,
                "style_match": 3.0, "semantic_match": score, "overall_match": score,
                "match_class": "C_MATCH", "assignment_type": "context_fallback",
                "fallback_level": 4,
                "reason": "deterministic visual-anchor fallback after all search stages",
            }]
            seg.semantic_score = max(seg.semantic_score, score)

    # TOTAL-COLLAPSE: the matcher can fail while downloads and visual descriptions are usable.
    # Emergency assignments above keep the run alive; only report the condition instead of
    # turning it into a hard job failure.
    if (not any(scene_candidates.get(it.scene_id) for it in body_intents)
            and any(s.visual_description for s in all_segments)):
        _log(status_cb,
             "Scrape V2 WARNING: the clip-to-scene matcher returned zero matches across "
             "%d described segments; emergency assignments will use the best available material."
             % sum(1 for s in all_segments if s.visual_description))

    _log(status_cb, "Scrape V2: final assignment...")
    assignments = assign_segments_globally_v2(body_intents, scene_candidates, cfg, status_cb=status_cb)

    if _ref_rules:
        # v0.2 ESCALATION LADDER: within one sentence's sub-beats, order the assigned clips
        # normal -> crazy (ascending vision "intensity") so every topic block builds up.
        _seg_by_id2 = {sg.segment_id: sg for sg in all_segments}
        for _bg, _ids in (_groups or {}).items():
            _assigned = [(i, assignments[i]) for i in _ids
                         if i in assignments and assignments[i].segment_id]
            if len(_assigned) < 2:
                continue
            def _inten(a):
                sg = _seg_by_id2.get(a.segment_id)
                try:
                    return float(((sg.visual_description or {}) if sg else {}).get("intensity") or 5.0)
                except (TypeError, ValueError):
                    return 5.0
            _slots = sorted(i for i, _a in _assigned)
            _order = sorted((_a for _i, _a in _assigned), key=_inten)
            for _slot, _a in zip(_slots, _order):
                _a.scene_id = _slot
                assignments[_slot] = _a

    used_seg_ids = {a.segment_id for a in assignments.values() if a.segment_id}
    # For filling unmatched scenes, rank spares by RELEVANCE to THIS scene, not by raw quality:
    # a topically-closest Japanese clip beats a shiny but off-topic filler. Relevance is computed
    # per-intent below; keep a quality-sorted copy only as the final tiebreaker source.
    spare = [s for s in all_segments if s.segment_id not in used_seg_ids]

    def _spare_relevance(seg, intent):
        # best available signal that this specific segment relates to this specific scene
        if seg.scene_ids and intent.scene_id not in seg.scene_ids:
            return float("-inf")
        base = max(getattr(seg, "semantic_score", 0.0), getattr(seg, "metadata_relevance", 0.0))
        vd = getattr(seg, "visual_description", None) or {}
        want = " ".join([intent.story_subject or "", intent.local_claim or "",
                         intent.subject or "", intent.action or "", intent.location or "",
                         intent.jp_subject or "", intent.jp_action or "", intent.jp_location or ""])
        have = " ".join([str(vd.get("subjects") or ""), str(vd.get("action") or ""),
                         str(vd.get("location") or ""), str(seg.query or "")])
        wt, ht = _tokens(want), _tokens(have)
        overlap = (len(wt & ht) / float(len(wt))) * 10.0 if wt else 0.0
        return base * 0.6 + overlap * 0.4

    # Same-source cap for the fallback filler too: the global assigner already enforces
    # max_segments_per_source_final, but this filler used to ignore it - one source video
    # could fill 3+ scenes (the "same clip 5x" complaint). Count existing uses first.
    _src_use = {}
    for a in assignments.values():
        seg0 = getattr(a, "_seg", None)
        if a.segment_id and seg0 is not None:
            _src_use[seg0.source_id] = _src_use.get(seg0.source_id, 0) + 1
    _src_cap = max(1, int(cfg.get("max_segments_per_source_final", 1) or 1))

    for it in body_intents:
        a = assignments.get(it.scene_id)
        if a and a.segment_id:
            continue
        chosen = None
        atype, flevel = "context_fallback", 4
        # rank the remaining pool by relevance to THIS scene
        ranked_spare = sorted(spare, key=lambda s: _spare_relevance(s, it), reverse=True)
        fresh = [s for s in ranked_spare if _src_use.get(s.source_id, 0) < _src_cap]
        fallback_floor = max(
            CONTEXT_FALLBACK_MIN_RELEVANCE,
            match_thresholds_for_relevancy(it.visual_type, getattr(it, "script_relevancy", 70))["overall"] - 0.1,
        )
        for s in fresh:
            rel = _spare_relevance(s, it)
            if rel >= fallback_floor and s.quality_score >= CONTEXT_FALLBACK_MIN_QUALITY:
                chosen = s
                break
        # Never turn a rejected/zero-score clip into rendered footage merely to fill a slot.
        # Leaving the scene unmatched activates the existing retry/relevancy handling; assigning
        # it here was the exact path that spread sumo and hook-dance footage through whole videos.
        if chosen is not None:
            _src_use[chosen.source_id] = _src_use.get(chosen.source_id, 0) + 1
            spare.remove(chosen)
            na = SceneAssignment(scene_id=it.scene_id, segment_id=chosen.segment_id,
                                 assignment_type=atype, fallback_level=flevel,
                                 semantic_score=chosen.semantic_score,
                                 quality_score=chosen.quality_score, match_class="C_MATCH")
            na._seg = chosen
            assignments[it.scene_id] = na

    # Never force an arbitrary downloaded clip into an unmatched scene. This used to turn a
    # relationship Short into repeated unrelated booking/dance footage because the forced pool
    # ignored per-scene semantic score. An uncovered claim can trigger a targeted retry; unrelated
    # filler cannot be repaired after rendering.
    unresolved = [it.scene_id for it in body_intents
                  if not (assignments.get(it.scene_id) and assignments[it.scene_id].segment_id)]
    if unresolved:
        _log(status_cb, "Scrape V2: leaving %d scene(s) unmatched after relevance filtering: %s. "
                        "No unrelated emergency footage will be rendered." %
                        (len(unresolved), ", ".join(str(i) for i in unresolved)))

    hook_seg = None
    if hook_intent is not None:
        # THE TOPICAL POOL GETS TO COMPETE. The presenter clips used to be the only candidates
        # whenever that search returned anything at all, so the opener was a dancing creator no
        # matter what the video was about: on the Shinkansen short Gemini named "the first shot is
        # completely unrelated to the video's topic" as the whole video's worst problem, and the
        # reference Short opens on its own subject instead. The presenter clips are still in the
        # pool and the influencer-role scorer still favours them - they simply have to beat a
        # topical shot rather than run unopposed.
        _by_quality = lambda rows: sorted(rows, key=lambda s: s.quality_score, reverse=True)
        _hook_seen, hook_pool_segs = set(), []
        for _cand in _by_quality(hook_presenter_segments)[:12] + _by_quality(all_segments)[:12]:
            if _cand.source_id in _hook_seen:
                continue
            _hook_seen.add(_cand.source_id)
            hook_pool_segs.append(_cand)
        hook_pool_segs = hook_pool_segs[:16]
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

    _intent_cat = {it.scene_id: getattr(it, "match_category", "") for it in intents}

    # A visual chapter is one searchable topic but may need several 2-4 second detail shots.
    # Reserve source videos across chapters (reusing different windows inside the SAME chapter is
    # useful and mirrors the hand edit; repeating that source in another topic is not).
    chapter_source_owner = {}
    for _sid, _assignment in assignments.items():
        _assigned_seg = getattr(_assignment, "_seg", None)
        if _assigned_seg is not None:
            chapter_source_owner[_assigned_seg.source_id] = _sid

    def _chapter_montage(scene_idx, primary_seg, scene_need, scene_intent):
        """Build overview -> interaction -> result from approved segments of one topic."""
        if scene_need <= 0.0:
            return "", []
        rows = list(scene_candidates.get(scene_idx) or [])
        planned = plan_chapter_montage_rows(
            primary_seg, rows, scene_need, scene_intent,
            source_owner=chapter_source_owner, scene_idx=scene_idx, max_shots=4)
        if not planned:
            return "", []
        destination, parts = render_chapter_montage(
            planned, scene_idx, scene_need, project_dir, ffmpeg, ffprobe,
            status_cb=status_cb)
        if not destination:
            return "", []
        for _part, source_seg, _seconds in parts:
            chapter_source_owner[source_seg.source_id] = scene_idx
        primary_seg.final_path = str(destination)
        primary_seg._final_secs = scene_need
        _log(status_cb, "Fact Short chapter %s: assembled %d relevant detail shot(s) for %.1fs "
                        "instead of stretching one TikTok." % (scene_idx, len(parts), scene_need))
        return str(destination), parts

    def _write_scene(scene_idx, seg, atype, flevel, sem, mclass, visual_role=None):
        sc0 = scenes[scene_idx]
        scene_intent = (hook_intent if scene_idx == 0 and hook_intent is not None else
                        next((it for it in body_intents if it.scene_id == scene_idx), None))
        editorial_reason = editorial_rejection_reason(seg, scene_intent)
        if editorial_reason:
            _log(status_cb, f"Scrape V2: rejected timeline candidate for scene {scene_idx}: {editorial_reason}.")
            return False
        # v0.2 boom rule keys on this: shock scenes get the sub-bass on frame 1
        if _intent_cat.get(scene_idx):
            sc0["visual_match_category"] = _intent_cat[scene_idx]
        try:
            scene_need = max(0.0, float(sc0.get("end", 0) or 0) - float(sc0.get("start", 0) or 0))
        except (TypeError, ValueError):
            scene_need = 0.0
        # Cut exactly the visual chapter.  Assignment already guarantees the inspected stable
        # window is long enough, so no unreviewed source frames are appended as "headroom".
        montage_parts = []
        if bool(sc0.get("visual_chapter")) and scene_need >= 5.2:
            final, montage_parts = _chapter_montage(
                scene_idx, seg, scene_need, scene_intent)
        else:
            # A clip a little shorter than the line is played a little slower instead of being
            # thrown away: export everything the inspected window holds, then let the renderer's
            # existing retime cover the rest. Without this the beat loses its only fitting shot
            # over a fraction of a second and falls back to a still.
            _avail = float(seg.duration or 0.0)
            _stretch = bool(scene_need and _avail + 0.05 < scene_need
                            and _avail >= scene_need * SHORT_CLIP_STRETCH_FLOOR)
            final = _finalize_segment_clip(
                seg, project_dir, ffmpeg,
                min_seconds=(_avail if _stretch else (scene_need if scene_need else None)))
        if not final:
            # Say WHY. This was the only silent failure left in the chain: a beat with an
            # approved match reported nothing at all, the report said "failed the final
            # editorial gate", and no line anywhere named the export as the culprit.
            _log(status_cb, "Scrape V2: scene %s lost its approved clip at export (%s), needed "
                            "%.1fs from a %.1fs window."
                 % (scene_idx, ", ".join(seg.rejection_reasons or ["window too short"]),
                    scene_need, float(seg.duration or 0.0)))
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
        sc["source_width"] = seg.source_width
        sc["source_height"] = seg.source_height
        sc["native_9_16"] = seg.native_9_16
        sc["assignment_type"] = atype
        sc["fallback_level"] = flevel
        # DID VISION ACTUALLY SEE A CAPTION? The caption pass used to be switched on for every
        # source clip regardless, so on one festival project it ran over eight clips of which
        # exactly one carried text - seven fills of GPU time for nothing, each of them touching
        # pixels nobody asked to have touched. Two heuristics were tried and both failed against
        # real footage: scoring the uniformity behind the text ranked a table seam best and the
        # one genuine plate worst, and requiring the caption to be temporally frozen rejected the
        # true positive too, because handheld footage freezes nothing. The vision pass already
        # answers this per segment, so its verdict is what the scene carries.
        sc["source_has_captions"] = bool((seg.visual_description or {}).get("burned_captions"))
        if _stretch and scene_need > 0:
            # speed < 1 = slower = longer (setpts=PTS/speed), so this is exactly the factor that
            # makes the available footage cover the narration line.
            sc["timeline_speed"] = round(max(SHORT_CLIP_STRETCH_FLOOR, _avail / scene_need), 4)
            sc["timeline_speed_src"] = name
            _log(status_cb, "Scrape V2: scene %s covers a %.2fs line with %.2fs of footage at "
                            "%.2fx rather than dropping its only fitting clip."
                 % (scene_idx, scene_need, _avail, sc["timeline_speed"]))
        if montage_parts:
            sc["chapter_montage"] = [{
                "segment_id": source.segment_id, "source_id": source.source_id,
                "start": round(float(source.start_time), 3),
                "duration": seconds, "query": source.query or "",
            } for _path, source, seconds in montage_parts]
        if visual_role:
            sc["visual_role"] = visual_role
        elif scene_idx == 0:
            sc["visual_role"] = "hook_topic"
        scene_clips_out[scene_idx] = final
        _slog(project_dir, {
            "type": "assignment", "segment_id": seg.segment_id, "platform": seg.platform,
            "clip_id": seg.source_id, "query": seg.query or "",
            "scene": scene_idx,
            "scene_text": str(sc.get("exact_voice_text") or sc.get("voice_line")
                             or sc.get("script") or "")[:160],
            "assignment_type": atype, "match_class": mclass,
            "script_match_score": round(sem, 1),
            "why": ("verified literal visual-evidence match" if
                    atype == "exact" and mclass in ("A_MATCH", "B_MATCH") else
                    "unverified candidate; not represented as an exact match" if atype == "exact" else
                    "relevance-ranked fallback (no exact match passed the floors for this scene)"
                    if atype in ("context_fallback",) else atype),
            "thumbs": _slog_thumbs(seg, project_dir, ffmpeg, n=1),
        })
        clip_decision_log[scene_idx] = {
            "scene": scene_idx, "chosen_clip": name, "match_class": mclass,
            "script_match_score": round(sem, 1), "assignment_type": atype,
            "fallback_needed": False, "adaptive_fallback": flevel >= 4,
            "reason": atype, "passes_acceptance_test": flevel == 0}
        pool.append(final)
        clip_meta[final] = {"bucket_id": scene_bucket.get(scene_idx, ""), "tier": "v2_segment",
                            "platform": seg.platform, "clip_id": seg.source_id,
                            "source_query": seg.query, "likes": 0,
                            "creator_id": seg.creator_id, "query_tier": seg.query_tier,
                            "japanese_context": seg.japanese_context,
                            "black_bar_score": seg.black_bar_score, "text_heaviness": seg.text_heaviness,
                            "semantic_score": sem, "assignment_type": atype,
                            "source_width": seg.source_width, "source_height": seg.source_height,
                            "native_9_16": seg.native_9_16}
        candidate_statuses.append({"clip_id": seg.source_id, "bucket_id": scene_bucket.get(scene_idx, ""),
                                   "source_query": seg.query, "tier": "v2_segment",
                                   "status": "assigned_to_scene", "reason": atype,
                                   "shown_in_media_panel": False})
        try:
            Path(final).with_suffix(".json").write_text(json.dumps({
                "status": "accepted", "platform": seg.platform, "clip_id": seg.source_id,
                "creator_id": seg.creator_id, "query": seg.query, "query_tier": seg.query_tier,
                "japanese_context": seg.japanese_context,
                "assignment_type": atype, "semantic_score": sem, "captioned": False}, indent=2), "utf-8")
        except Exception:
            pass
        return True

    if use_influencer_hook and hook_intent is not None and hook_seg is not None:
        if _write_scene(0, hook_seg, "exact", 0, hook_seg.semantic_score, "A_MATCH",
                        visual_role="hook_influencer"):
            hook_pool.append(hook_seg.final_path)
    elif use_influencer_hook and scenes:
        scenes[0]["visual_role"] = "hook_influencer"

    def _borrow_clip(scene_idx, reason):
        """Last resort: a DIFFERENT part of the nearest already-accepted clip. Never a still.

        A frozen photo in the middle of a short reads as a broken video, so a beat that
        found nothing of its own borrows motion rather than going still. It borrows from the
        neighbouring beat, because in a narration the beat before and after share the place
        and the people - the purikura booth beats are all the same booth. A fresh in/out
        point keeps it from looking like a literal repeat of the shot just seen.

        The borrow is never dressed up as a match: match_class stays BORROWED, the reason
        travels into the report, and the run still counts the beat as unmatched.
        """
        used = [(i, sc) for i, sc in enumerate(scenes)
                if sc.get("clip") and str(sc.get("assignment_type") or "") not in
                ("uncovered_still", "borrowed_clip")]
        if not used:
            return False
        donor_idx, donor = min(used, key=lambda row: abs(row[0] - scene_idx))
        scene = scenes[scene_idx]
        scene["clip"] = donor["clip"]
        scene["asset"] = donor.get("asset") or donor["clip"]
        scene["assignment_type"] = "borrowed_clip"
        scene["match_class"] = "BORROWED"
        scene["borrowed_from_scene"] = donor_idx
        scene["scrape_uncovered_reason"] = reason
        # a different window of the same source, so two beats are not the same three seconds
        try:
            src_len = float(donor.get("source_duration") or 0)
        except (TypeError, ValueError):
            src_len = 0.0
        if src_len > 4.0:
            step = max(1.0, min(3.0, src_len / 4.0))
            scene["source_trim"] = round(((scene_idx - donor_idx) * step) % max(1.0, src_len - 2.0), 2)
        if scene_idx and not scene.get("visual_role"):
            scene["visual_role"] = "body"
        return True

    def _mark_uncovered(scene_idx, rep, reason):
        """Record that this beat found nothing of its own. The picture is decided later.

        Borrowing has to wait until every real assignment exists - beat 1 may want motion
        from beat 8, which has not been written yet while this loop is running.
        """
        scene = scenes[scene_idx]
        scene.pop("clip", None)
        scene.pop("asset", None)
        scene["assignment_type"] = "uncovered_still"
        scene["match_class"] = "UNMATCHED"
        scene["scrape_uncovered_reason"] = reason
        if scene_idx and not scene.get("visual_role"):
            scene["visual_role"] = "body"
        rep["assignment_type"] = "uncovered_still"
        rep["uncovered_reason"] = reason

    reserved_sources = {
        getattr(a._seg, "source_id", "")
        for sid, a in assignments.items()
        if getattr(a, "_seg", None) is not None
    }
    for it in body_intents:
        a = assignments.get(it.scene_id)
        rep = {"scene_id": it.scene_id, "scene_text": it.scene_text,
               "visual_intent": "%s / %s / %s" % (it.subject, it.action, it.location),
               "queries_used": [q.query for q in queries_for_intent(it)][:4]}
        if a and getattr(a, "_seg", None) is not None:
            seg = a._seg
            ok = _write_scene(it.scene_id, seg, a.assignment_type, a.fallback_level,
                              a.semantic_score, a.match_class)
            chosen_type, chosen_sem, chosen_quality = (a.assignment_type, a.semantic_score,
                                                        a.quality_score)
            # The final byte-level cut audit can discover a transition that proxy analysis missed.
            # Try another semantically approved candidate from a different, unreserved source
            # before declaring the chapter uncovered and borrowing unrelated neighbour motion.
            if not ok:
                for row in scene_candidates.get(it.scene_id, []):
                    alternative = row.get("segment")
                    if (alternative is None or alternative.segment_id == seg.segment_id
                            or alternative.source_id in reserved_sources):
                        continue
                    alternative_type = str(row.get("assignment_type") or "retry_exact")
                    alternative_sem = float(row.get("overall_match") or 0.0)
                    if _write_scene(it.scene_id, alternative, alternative_type, 0,
                                    alternative_sem, str(row.get("match_class") or "A_MATCH")):
                        seg, ok = alternative, True
                        chosen_type, chosen_sem = alternative_type, alternative_sem
                        chosen_quality = float(alternative.quality_score or 0.0)
                        reserved_sources.add(alternative.source_id)
                        _log(status_cb, "Scrape V2: scene %s used its next approved candidate "
                                        "after the first failed the exact final-cut audit."
                             % it.scene_id)
                        break
            rep.update({"selected_segment": seg.segment_id, "assignment_type": chosen_type,
                        "semantic_score": chosen_sem, "quality_score": chosen_quality})
            if ok and chosen_type == "exact":
                counters["scenes_exact_matched"] += 1
            elif ok and chosen_type in ("alternative_visual", "retry_exact"):
                counters["scenes_alternative_matched"] += 1
            elif ok and chosen_type in ("context_fallback", "emergency_fallback"):
                counters["scenes_context_fallback"] += 1
            if not ok:
                counters["scenes_unmatched"] += 1
                _mark_uncovered(it.scene_id, rep, "selected segment failed the final editorial gate")
        else:
            counters["scenes_unmatched"] += 1
            _mark_uncovered(it.scene_id, rep, "no approved semantically matching social segment after recovery")
        state["scene_reports"].append(rep)

    # The optional presenter hook is searched separately from body intents.  Give it the same
    # explicit still fallback when no approved creator clip survives, rather than letting a later
    # generic fallback copy unrelated footage into the opener.
    for scene_idx, scene in enumerate(scenes):
        if not scene.get("clip") and str(scene.get("assignment_type") or "") != "uncovered_still":
            rep = {"scene_id": scene_idx,
                   "scene_text": str(scene.get("exact_voice_text") or scene.get("voice_line") or "")}
            _mark_uncovered(scene_idx, rep, "no approved social clip for this scene")
            if not any(row.get("scene_id") == scene_idx for row in state["scene_reports"]):
                state["scene_reports"].append(rep)
            counters["scenes_unmatched"] += 1

    # CLIPS ONLY: a beat with no footage of its own shows borrowed motion, never a still.
    #
    # scene_clips_out is the list agent_core rebuilds the timeline from; a scene missing
    # from it has its clip popped again just before the render. Writing only the scene dict
    # made this whole feature invisible - and worse than the still it replaced, because the
    # pre-render gate lets an uncovered_still through and does not know borrowed_clip.
    # Hand the borrow step everything the run already paid for and nobody used. On the dating
    # project 62 of 64 quality-passed segments sat idle while two sources were duplicated onto
    # four beats; each spare here is one duplicate that never has to happen.
    # A FRESH CLIP IS ONLY BETTER THAN A REPEAT IF IT IS ALSO RELEVANT.
    #
    # The first version of this took the highest QUALITY unused segments, and quality says
    # nothing about what is on screen: the machines run filled seven beats with a news
    # screenshot, a pink Mercari text card, a dashcam road, coin lockers, an English-speaking
    # creator and a bowl of ramen. Every one of those is worse than repeating a clip that
    # actually shows the thing being narrated, and several should never have passed the
    # editorial gate at all.
    #
    # So a spare must now be a segment the matcher SCORED FOR THAT EXACT BEAT - its own best
    # leftover candidate, relevance first - and it must clear the same editorial gate as any
    # other clip. Nothing else is offered; a beat with no relevant leftover borrows instead.
    _spare_by_scene, _on_timeline = {}, {
        str(sc.get("scrape_clip_id") or "") for sc in scenes if sc.get("clip")}
    _intent_by_id = {it.scene_id: it for it in body_intents}
    for _sid_scene, _rows in (scene_candidates or {}).items():
        _intent = _intent_by_id.get(_sid_scene)
        _ranked = sorted(_rows or [], key=lambda r: -float(r.get("overall_match") or 0.0))
        for _row in _ranked:
            _seg = _row.get("segment")
            if _seg is None:
                continue
            _src = str(_seg.source_id or "")
            if not _src or _src in _on_timeline:
                continue
            if editorial_rejection_reason(_seg, _intent):
                continue
            _spare_by_scene.setdefault(int(_sid_scene), []).append(
                {"segment": _seg, "score": float(_row.get("overall_match") or 0.0)})
    if _spare_by_scene:
        _log(status_cb, "Scrape V2: %d beat(s) have an unused candidate of their own, so an empty "
                        "beat can take relevant fresh footage instead of repeating a neighbour."
             % len(_spare_by_scene))

    def _prepare_spare(scene_idx):
        """Cut this beat's best unused candidate, or None. Relevance decides, not quality."""
        for row in _spare_by_scene.get(int(scene_idx), []):
            seg = row["segment"]
            src = str(seg.source_id or "")
            if src in _on_timeline:
                continue
            final = _finalize_segment_clip(seg, project_dir, ffmpeg)
            if not final:
                continue
            _on_timeline.add(src)
            return {"clip": Path(final).name, "asset": Path(final).name,
                    "scrape_clip_id": src, "path": str(final), "score": row["score"]}
        return None

    borrowed = borrow_motion_for_uncovered(scenes, scene_clips_out,
                                           spare_for_scene=_prepare_spare)
    # Counted separately from scenes_unmatched, which still includes these. A run where ten
    # beats borrow is a run whose search failed ten times, and the report has to say so even
    # though the video plays as motion throughout.
    counters["scenes_borrowed_motion"] = borrowed
    counters["scenes_still_fallback"] = sum(
        1 for sc in scenes if str(sc.get("assignment_type") or "") == "uncovered_still")
    if borrowed:
        for row in state["scene_reports"]:
            sc = scenes[row["scene_id"]] if 0 <= int(row.get("scene_id", -1)) < len(scenes) else {}
            if str(sc.get("assignment_type") or "") == "borrowed_clip":
                row["assignment_type"] = "borrowed_clip"
                row["borrowed_from_scene"] = sc.get("borrowed_from_scene")
        _log(status_cb, "Scrape V2: %d beat(s) had no footage of their own and borrowed motion "
                        "from a neighbouring beat; none of them is a still." % borrowed)
    elif any(str(sc.get("assignment_type") or "") == "uncovered_still" for sc in scenes):
        _log(status_cb, "Scrape V2 WARNING: no usable footage at all, so there is nothing to "
                        "borrow from and the scenes fall back to stills.")

    # WHAT EACH BEAT ACTUALLY HAD TO CHOOSE FROM. The report recorded only the segment that WON,
    # so a beat that ended up with filler looked identical to one whose search returned nothing -
    # and the footage that was downloaded, judged and passed over could not be reviewed at all.
    # The timeline's per-beat downloads button reads this.
    _picked = {str(getattr(a, "segment_id", "") or "") for a in assignments.values()}
    _downloads = {}
    for _seg in all_segments:
        _row = {
            "segment_id": _seg.segment_id,
            "source_id": _seg.source_id,
            "platform": _seg.platform,
            "query": getattr(_seg, "query", ""),
            "start": round(float(_seg.start_time or 0.0), 2),
            "end": round(float(_seg.end_time or 0.0), 2),
            "quality": round(float(getattr(_seg, "quality_score", 0.0) or 0.0), 2),
            "match": round(float(getattr(_seg, "semantic_score", 0.0) or 0.0), 2),
            "clip": Path(_seg.final_path).name if getattr(_seg, "final_path", "") else "",
            "proxy": Path(_seg.source_path).name if getattr(_seg, "source_path", "") else "",
            "used": _seg.segment_id in _picked,
            "reason": "; ".join(str(r) for r in (getattr(_seg, "rejection_reasons", None) or []))[:120],
        }
        for _sid in (getattr(_seg, "scene_ids", None) or []):
            try:
                _downloads.setdefault(int(_sid), []).append(_row)
            except (TypeError, ValueError):
                continue
    for _rows in _downloads.values():
        _rows.sort(key=lambda r: (-r["used"], -r["match"], -r["quality"]))
    state["downloads_by_scene"] = {str(k): v[:40] for k, v in _downloads.items()}

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
