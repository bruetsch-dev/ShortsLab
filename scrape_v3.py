"""Clip Short V3 - source footage like an editor, then cut to the voice.

V2 treats a script as a list of sentences and tries to prove each one with its own perfect,
unique clip. When it cannot, the beat borrows, falls back to a still, or comes back empty. V3
starts from the other end: a short is a handful of visual IDEAS, each of which may cover several
spoken sentences, and an editor picks the strongest available sequence for each idea. A clearly
relevant shot beats an empty scene, and a labelled contextual shot beats unrelated filler.

The differences from V2 that matter, all deliberate:

- Chapters, not micro-scenes. Four to six for a half-minute script; sentences group into one idea.
- Footage is chosen BEFORE the timeline is cut. V2 fixes twelve scene lengths and then hunts for
  twelve clips; V3 finds the shots first and lays the voice over them.
- Three match classes, all valid: `exact` proves the words, `context` shows the same real system,
  `editorial_proxy` is understandable related footage. Only genuinely wrong or broken material is
  rejected, and every rejection carries one concrete reason.
- A source may return only genuinely different, non-overlapping moments. A long TikTok is not
  automatically sliced into three fake "clips"; multiple moments are allowed only when vision
  identifies different visible actions/parts. The renderer never freezes a frame to fill time.
- Nothing from an older project. A new script gets fresh footage.

V2 is untouched; this module shares only the low-level primitives - search, download, segment
discovery, quality analysis and the vision call - which are plumbing, not editorial judgement.
"""
import copy
import json
import math
import os
import re
import shutil
import subprocess
import time
import concurrent.futures
from dataclasses import dataclass, field
from pathlib import Path

import clip_scraper
import pipeline
import scrape_v2

# Every result order is worth a look: relevance finds the literal object, likes find the clip
# people actually watched, views find the one that travelled, recent finds this season's version.


V3_CONFIG = {
    # A 20-30 second Short needs three coherent visual worlds. Two made an entry ritual, the
    # customer response, and the final reversal collapse into one giant generic retail query.
    "min_chapters": 3,
    "max_chapters": 6,
    # Preserve several independent visual hypotheses. Five slots was enough for generic retail
    # fallbacks to silently push the rare ritual/object out of a chapter entirely.
    "queries_per_chapter": 8,
    # The paid API loses individual keywords on the provider side: measured across 12 batched
    # jobs, 30 keywords submitted and only 12 came back with material - a 40% hit rate, with the
    # rest dying as `dead_page` without ever being searched. Eight keywords therefore buy about
    # three real searches, which is not enough to cover a chapter. In API-only mode the net is
    # cast wider so the surviving share is still a usable pool. Records are capped by
    # BRIGHTDATA_RECORD_BUDGET either way, so this widens coverage rather than the bill.
    "queries_per_chapter_api": 16,
    # A chapter needs a real editorial pool before it may conclude that its visual idea is
    # unavailable. Eight first-look clips was too small for specific real-world facts.
    "min_sources_per_chapter": 8,
    "max_sources_per_chapter": 14,
    # Two, not three. Three shots from one upload is the same footage a third of a 30-second
    # Short, and that is exactly what a delivered Clip Short looked like. The per-chapter and
    # whole-Short numbers are deliberately the same so there is ONE rule to reason about.
    "max_windows_per_source": 2,
    # ...per CHAPTER. Across the whole Short a source may supply at most this many shots.
    # Measured on a delivered Clip Short: nine scenes, six source videos - one TikTok carried
    # three of the nine scenes and another two, so the viewer saw the same footage again and
    # again ("etwa 3 verschiedene clips auf die ganze timeline verteilt, nur duplikate"). The
    # per-chapter cap held; nothing counted across chapters, so three chapters each spent their
    # allowance on the same upload.
    "max_windows_per_source_total": 2,
    # Different parts of one source are valid; automatic subdivision of one visually identical
    # region is not. Selection also rejects overlapping windows and duplicate visual reasons.
    "min_window_gap_seconds": 0.5,
    "shot_seconds_min": 2.0,
    # Four seconds is still brisk for a Short, but it stops a 10–14 second real-world chapter
    # being rejected solely because it has three strong shots instead of four micro-cuts.
    "shot_seconds_max": 4.25,
    "demo_shot_seconds_max": 5.0,
    # When a source is a little shorter than the narration, 0.80x is still natural for real
    # phone footage and is far preferable to repeating a source part or holding a frame.
    "slowdown_floor": 0.76,
    "hook_min_likes": 20_000,
    # Five genuinely different search waves are enough for literal proof -> visible action ->
    # native creator phrase -> setting -> broad context. A 1000-round safety cap turned a failed
    # idea into a two-hour loop that re-downloaded the same kind of junk.
    "max_search_rounds": 5,
    # A chapter may not call itself covered while resting on one or two uploads. Three is
    # the point where a viewer stops recognising the same video; below it the edit reads as
    # a loop. Capped by the number of beats, so a one-beat chapter still needs only one.
    "min_distinct_sources": 3,
    "retry_queries_per_chapter": 5,
    "vision_concurrency": 4,
    # How many frames of a SOURCE the vision pass sees. Four was inherited from V2,
    # which inspects an already-chosen segment; V3 asks "is there anything usable in
    # this whole video". Checked by hand against a delivered run: of eight sources
    # rejected as "visually unrelated" for a capsule-hotel chapter, at least three
    # plainly showed capsule hotels - an airport pod, a hotel corridor and a "can I fit
    # in a capsule hotel" clip. Their relevant frames simply were not among the four.
    "vision_frames_per_source": 8,
    "max_stall_seconds": 0.25,
}

# Result orderings, in the order V3 uses them: every query is asked for RELEVANCE first, and the
# rest are only brought in when that first breadth pass produced too little choice. The names are
# the ones clip_scraper/tiktok_login already understand - anything not MOST_* means pure
# relevance there. Both loops below referenced this tuple, but it was never defined, so every V3
# run died with NameError on its first search: the engine could not scrape at all.
#
# The paid Bright Data fallback cannot order results server-side. It does not need to: these
# sorts are applied locally, on the relevance score and the item's own like/view/date metadata.
# A chapter's clock is split before anything runs: searching gets this share, fetching keeps the
# rest. Without the split, searching was allowed to spend the ENTIRE chapter budget and routinely
# did - two slow provider calls at up to 600s each inside a 150s chapter - so the results arrived
# with no time left to download them and were discarded unseen.
SEARCH_SHARE = 0.65

# What is left after the search share is gone, purely to save results already found. This is the
# backstop for a search phase that overruns anyway; the split above is the actual fix.
DOWNLOAD_GRACE_S = 120.0

V3_SORT_MODES = ("RELEVANCE", "MOST_LIKED", "MOST_VIEWED", "MOST_RECENT")

# The ONLY reasons a candidate may be discarded. Anything not on this list - modest on-screen
# text, few likes, an imperfect metadata match, an inability to prove every word - is not a
# reason, and rejecting on those is what emptied V2's timelines.
V3_REJECTIONS = {
    "wrong_country": "wrong country or context",
    "unrelated_filler": "visually unrelated to the chapter",
    "slideshow_static": "slideshow or static image, not footage",
    "not_vertical": "unusable non-vertical framing",
    "captions_cover_subject": "burned-in captions cover the key visual",
    "caption_on_solid_box": "caption sits on an opaque plate that cannot be removed cleanly",
    # These two were learned from a finished short: a phone screen recording of an app was
    # accepted as konbini footage, and clips whose creator text covers ~10% of the frame went
    # into the edit and then had their cleaning REFUSED at render time - far too late to pick
    # something else. Both are now decided while there are still alternatives on the table.
    "screen_recording": "a screen or app recording, not filmed footage",
    "text_dominates": "creator text covers too much of the frame to be removed cleanly",
    # Both learned from a delivered Short about umbrella lockers: it opened on a news-studio
    # talking head and ran a duet with a stranger's face pasted in the corner.
    "talking_head": "someone talking to camera or a news presentation, not b-roll",
    "duet_or_stitch": "a reaction duet or stitch with someone else's video inset",
    "black_frames": "the chosen range fades to or holds on black",
    "repeated_frames": "repeated frames or a broken clip",
    "hidden_source_cut": "an unplanned source cut inside the selected window",
    "no_visible_action": "no useful visible action",
}

MATCH_CLASSES = ("exact", "context", "editorial_proxy")

# Platform names never belong in a search box - they are where you search, not what you search for.
_PLATFORM_WORDS = re.compile(r"\b(tiktok|tik tok|instagram|insta|reels?|twitter|x\.com|youtube|shorts)\b",
                             re.IGNORECASE)
_SEARCH_META_WORDS = re.compile(
    r"\b(explanation|explainer|meaning|definition|dictionary|infographic|diagram)\b|"
    r"解説|意味|辞書|図解|由来", re.IGNORECASE)
_GENERIC_CHAPTER_QUERY = re.compile(r"^(?:visual\s+)?chapter\s*#?\s*\d+(?:\s*[-:–].*)?$", re.IGNORECASE)
_GAME_TERMS = re.compile(
    r"\b(fortnite|minecraft|roblox|valorant|apex|videogame|video game|gameplay|gaming|hud)\b|"
    r"フォートナイト|マイクラ|ゲーム実況|ゲームプレイ", re.IGNORECASE)
_CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]")
_GENERIC_RETAIL_QUERY = re.compile(
    r"^(?:コンビニ|コンビニ\s+(?:買い物|ルーティン)|japanese\s+convenience\s+store|"
    r"japan(?:ese)?\s+store\s+(?:shopping|ambience)|スーパー買い物vlog)$", re.IGNORECASE)


def _scene_voice_text(scene):
    """Return the words actually spoken for any scene shape used by the app.

    V3 originally read only ``text``/``caption`` while the real Clip Short scenes store their
    narration in ``exact_voice_text``/``script``.  Every line therefore became empty, the planner
    fell back to titles named "Chapter 1", and TikTok correctly returned Fortnite Chapter 1.
    Keep this accessor in one place so a future scene-schema change cannot silently erase the
    search topic again.
    """
    for key in ("exact_voice_text", "script", "voice_line", "text", "caption", "narration"):
        value = str((scene or {}).get(key) or "").strip()
        if value:
            return value
    return ""


def _chapter_allows_games(chapter):
    blob = " ".join([
        str(getattr(chapter, "title", "") or ""),
        str(getattr(chapter, "subject", "") or ""),
        str(getattr(chapter, "action", "") or ""),
        str(getattr(chapter, "evidence", "") or ""),
    ])
    return bool(_GAME_TERMS.search(blob))


def _log(status_cb, message):
    if status_cb:
        try:
            status_cb(message)
        except Exception:      # noqa: BLE001 - a logging failure must not kill a run
            pass


@dataclass
class Chapter:
    """One visual idea. It owns the voice lines it covers and the brief for finding footage."""
    chapter_id: int
    title: str
    scene_ids: list = field(default_factory=list)
    start: float = 0.0
    end: float = 0.0
    subject: str = ""
    action: str = ""
    evidence: str = ""                       # what the shot must actually show
    proxies_ok: list = field(default_factory=list)
    forbidden: list = field(default_factory=list)
    # Terms the creator supplied in the UI.  They are a deliberate search direction and must
    # precede locally-derived fallbacks, which are merely a safety net.
    explicit_queries: list = field(default_factory=list)
    queries: list = field(default_factory=list)
    allow_multi_window: bool = True
    is_hook: bool = False

    @property
    def target_seconds(self):
        return max(0.5, round(float(self.end) - float(self.start), 3))

    def brief(self):
        parts = [self.subject or self.title]
        if self.action:
            parts.append(self.action)
        if self.evidence:
            parts.append("must show: " + self.evidence)
        return " / ".join(p for p in parts if p)


@dataclass
class ShotWindow:
    """A concrete piece of a concrete file, chosen for a concrete reason."""
    chapter_id: int
    source_id: str
    platform: str
    path: str
    start: float
    end: float
    match_class: str = "context"
    reason: str = ""
    # Compact identity of what is on screen (subject + action + place), supplied by the
    # vision review.  It is deliberately separate from `reason`: the latter explains why a
    # shot fits the narration and can vary in wording for the same visual moment.
    visual_signature: str = ""
    # 0-10, from the vision pass: how watchable this moment is, asked SEPARATELY from whether
    # it fits. Default 5 so a source graded before the field existed ranks as ordinary rather
    # than as dead footage. See the ranking in select_chapter_windows.
    visual_interest: float = 5.0
    query: str = ""
    source_has_captions: bool = False

    @property
    def duration(self):
        return max(0.0, round(float(self.end) - float(self.start), 3))

    def overlaps(self, other, gap):
        """Two windows of one source are only distinct when a real gap separates them."""
        return not (self.end + gap <= other.start or other.end + gap <= self.start)


def _interest_band(score):
    """Coarse watchability band: 2 = worth stopping for, 1 = ordinary, 0 = inert.

    Banded on purpose. Sorting on the raw score would let a 7.4 outrank a 7.0 that is a second
    longer, which is noise - the vision pass cannot tell those apart reliably. The bands are the
    three the prompt actually describes.
    """
    try:
        value = float(score)
    except (TypeError, ValueError):
        return 1
    if value >= 8.0:
        return 2
    if value >= 5.0:
        return 1
    return 0


def _interest_of(window):
    """The vision pass's 0-10 watchability score, clamped; 5 when it did not answer.

    Neutral rather than zero on a missing value: an unscored window must not be pushed below
    every scored one, or a single malformed reply silently drops good footage.
    """
    try:
        value = float(window.get("visual_interest"))
    except (TypeError, ValueError):
        return 5.0
    if value != value:                                  # NaN
        return 5.0
    return max(0.0, min(10.0, value))


def visual_identity(window):
    """Stable comparison key for repeated-picture protection within one source."""
    raw = str(getattr(window, "visual_signature", "") or getattr(window, "reason", "") or "")
    return re.sub(r"[^a-z0-9\u3040-\u30ff\u3400-\u9fff]+", " ", raw.casefold()).strip()


# --------------------------------------------------------------------------- chapter planning
# Words a truncated phrase may end on. A query that stops here is a sentence that was cut, not
# a subject: "A guest soaking in a" searches for nothing, and TikTok answers with anything.
_DANGLING_TAIL_WORDS = {
    "a", "an", "the", "and", "or", "of", "to", "in", "on", "at", "by", "for", "from", "with",
    "into", "onto", "over", "under", "their", "its", "his", "her", "your", "my", "our", "this",
    "that", "these", "those", "who", "whom", "which", "while", "when", "where", "as", "is",
    "are", "was", "were", "be", "been", "being", "it", "they", "he", "she", "you", "we",
}


# Camera language is not caption language. Measured on the Tokyo-train run (2026-08-29): the
# planner spent real search slots on "close shot", "POV follows friends from train", "filmed
# from inside train doorway" and "candid train arrival friends step". Nobody captions a clip
# with the framing they used, so these match nothing and the download budget is gone.
_CAMERA_WORDS = re.compile(
    r"\b(close[- ]?up|closeup|wide shot|medium shot|establishing|pov|"
    r"b[- ]?roll|broll|footage|filmed|filming|shot|shots|angle|framing|handheld|camera|"
    r"cutaway|montage|sequence|candid|timelapse|slow motion|slowmo)\b",
    re.IGNORECASE)

# The same run, split by outcome. EVERY English query that produced an accepted source was a
# bare noun phrase - "Japanese young adult dance", "Tokyo train carriage courtesy campaign".
# EVERY English query that produced only rejects narrated an action: "Passengers squeezing
# aboard during morning", "Riders holding straps and looking", "commuters leave carriage and
# gesture", "Passengers exiting and clearing". A caption names the thing; it does not narrate it.
#
# So an English query carrying an action word is a description, and a description is a wasted
# slot. Nouns that merely END in -ing are common and must survive, hence the exception list.
_NOUNS_ENDING_ING = {
    "morning", "evening", "building", "buildings", "training", "shopping", "clothing",
    "crossing", "crossings", "railing", "railings", "ceiling", "meeting", "meetings",
    "wedding", "weddings", "painting", "paintings", "parking", "seating", "lighting",
    "spring", "string", "king", "ring", "thing", "something", "everything", "during",
    "opening", "openings", "greeting", "greetings", "boarding", "vending",
}
_ACTION_VERBS = {
    "leave", "leaves", "step", "steps", "walk", "walks", "stand", "stands", "sit", "sits",
    "hold", "holds", "open", "opens", "close", "closes", "enter", "enters", "exit", "exits",
    "turn", "turns", "look", "looks", "talk", "talks", "speak", "speaks", "laugh", "laughs",
    "gesture", "gestures", "ride", "rides", "wait", "waits", "board", "boards", "push",
    "pushes", "pull", "pulls", "show", "shows", "make", "makes", "take", "takes", "keep",
    "keeps", "read", "reads", "sleep", "sleeps", "stare", "stares", "clear", "clears",
    "follow", "follows", "face", "faces", "squeeze", "squeezes", "press", "presses",
    "save", "saves", "move", "moves", "exchange", "exchanges", "point", "points",
}

# A query left starting on a preposition is the tail of a sentence, not a subject. These appear
# after a camera word is removed ("filmed from inside train doorway" -> "from inside train
# doorway"), and they search for nothing.
_LEADING_PREPOSITIONS = {
    "from", "inside", "outside", "with", "during", "beside", "against", "through", "across",
    "behind", "between", "among", "near", "toward", "towards", "into", "onto", "over",
    "under", "above", "below", "after", "before", "while", "and", "or", "of", "in", "on",
    "at", "by", "for", "to",
}


def _is_shot_description(text):
    """True when an English query narrates an action instead of naming a subject."""
    if _CJK_RE.search(text):
        return False                    # Japanese queries are already compact noun strings
    for word in re.findall(r"[A-Za-z][A-Za-z'-]*", text):
        folded = word.casefold()
        if folded in _NOUNS_ENDING_ING:
            continue
        if folded.endswith("ing") and len(folded) > 4:
            return True
        if folded in _ACTION_VERBS:
            return True
    return False


def _dedupe_words(text):
    """'Friends shoulder-to-shoulder rush hour Friends' - the planner repeats its own subject."""
    seen, kept = set(), []
    for word in text.split():
        folded = word.casefold().strip(",.")
        if folded and folded in seen:
            continue
        seen.add(folded)
        kept.append(word)
    return " ".join(kept)


def _topic_anchors(chapter, attempted):
    """The words this chapter has already proven are on-topic.

    English comes from the brief; Japanese cannot, so it is taken from the queries the planner
    wrote in the first round - those were derived from the brief before any drift set in.
    """
    english, cjk = set(), set()
    for field in (getattr(chapter, "subject", ""), getattr(chapter, "title", ""),
                  getattr(chapter, "action", ""), getattr(chapter, "evidence", "")):
        for word in re.findall(r"[A-Za-z][A-Za-z'-]{2,}", str(field or "")):
            folded = word.casefold()
            if folded not in _DANGLING_TAIL_WORDS and folded not in _ACTION_VERBS:
                english.add(folded)
    for query in (attempted or [])[:_queries_per_chapter()]:
        for word in re.findall(r"[A-Za-z][A-Za-z'-]{2,}", str(query or "")):
            english.add(word.casefold())
        for run in re.findall(r"[぀-ヿ㐀-鿿]{2,}", str(query or "")):
            cjk.add(run)
    return {"english": english, "cjk": cjk}


def _query_drifted(text, anchors):
    """True when a query shares nothing with the chapter's own proven vocabulary."""
    english = anchors.get("english") or set()
    cjk = anchors.get("cjk") or set()
    if not english and not cjk:
        return False                       # nothing to compare against - never block
    if _CJK_RE.search(text):
        if not cjk:
            return False
        # Substring both ways: 電車 anchors 電車ドア, and 満員電車 is anchored by 電車.
        return not any(run in text or text in run or
                       any(run[i:i + 2] in text for i in range(len(run) - 1))
                       for run in cjk)
    words = {w.casefold() for w in re.findall(r"[A-Za-z][A-Za-z'-]{2,}", text)}
    return not (words & english) if english else False


def sanitize_queries(raw_queries, limit=None):
    """Clean a chapter's query list: no platform words, no duplicates, no empties.

    A query carrying "tiktok" searches TikTok for the word TikTok. V2 measured this as pages of
    unrelated meta-content; here the word is simply removed rather than the query discarded.
    """
    limit = limit or _queries_per_chapter()
    out = []
    for entry in (raw_queries or []):
        text = _PLATFORM_WORDS.sub(" ", str(entry or ""))
        # Search for the visible thing, not content ABOUT the thing. These modifiers overwhelmingly
        # return talking heads, slideshows and caption cards, exactly what the vision gate rejects.
        text = _SEARCH_META_WORDS.sub(" ", text)
        # Framing words describe the shot, not the subject - they match nothing.
        text = _CAMERA_WORDS.sub(" ", text)
        text = _dedupe_words(text)
        text = re.sub(r"\s+", " ", text).strip(" \t\r\n-_,")
        if not text or len(text) < 2:
            continue
        # "Chapter 1" is presentation scaffolding, never a visual subject. Searching it produced
        # an entire Fortnite edit for a narration about Japanese group dating.
        if _GENERIC_CHAPTER_QUERY.fullmatch(text):
            continue
        # TikTok/X search boxes work on compact discovery phrases, not prose. The failed
        # Christmas-Eve run sent ten-word descriptions and received German advertisements and
        # random product videos. Keep the tangible subject/action terms and discard the tail.
        parts = text.split()
        max_terms = 4 if _CJK_RE.search(text) else 5
        if len(parts) > max_terms:
            text = " ".join(parts[:max_terms])
        # Truncating prose at a fixed word count leaves a fragment, and a fragment searches for
        # nothing. Measured on a delivered Clip Short: 14 of 71 executed queries were cut
        # mid-sentence - "A guest soaking in a", "Steam, sauna benches, or a", "A hand plugging
        # a phone" - and 32 of 61 rejected downloads came back "visually unrelated". Drop the
        # function words the cut left dangling, and discard what is left if it is no longer a
        # subject. A missing query costs nothing; a nonsense query spends a real search.
        if not _CJK_RE.search(text):
            words = text.split()
            while words and words[-1].casefold().strip(",.") in _DANGLING_TAIL_WORDS:
                words.pop()
            text = " ".join(words)
            # A leading article marks a DESCRIPTION, not a search phrase. Every query in the
            # measured run that came back "visually unrelated" began this way ("A guest
            # soaking...", "A hand plugging..."); every query that worked was a bare noun
            # phrase ("capsule hotel pod bed TV"). Strip it and search for the subject.
            words = text.split()
            while words and words[0].casefold() in ({"a", "an", "the"} | _LEADING_PREPOSITIONS):
                words.pop(0)
            text = " ".join(words)
            content = [w for w in text.split()
                       if w.casefold().strip(",.") not in _DANGLING_TAIL_WORDS]
            # Two content words is the floor for a search that means anything.
            if len(content) < 2 or len(text) < 3:
                continue
        # A missing query costs nothing; a description spends a real download budget.
        if _is_shot_description(text):
            continue
        if text.casefold() in {q.casefold() for q in out}:
            continue
        out.append(text)
        if len(out) >= limit:
            break
    return out


def _fallback_queries_from_text(text):
    """Small deterministic safety net for when the chapter-planning model is unavailable.

    This is intentionally conservative: it searches the actual nouns in the narration.  It is not
    meant to beat the planner, only to guarantee that a failed planning call can never turn into a
    generic query such as "Chapter 3".
    """
    original = re.sub(r"\s+", " ", str(text or "")).strip()
    lowered = original.casefold()
    # Some subjects have a precise native search anchor that must be attempted before broad
    # contextual footage.  Keep this separate from the generic fallback list so a morning
    # commute cannot consume every first-round slot ahead of the actual women-only-car query.
    priority = []
    if any(term in lowered for term in ("women-only", "women only", "women-only-car",
                                        "women-only car", "女性専用")):
        priority.extend(["女性専用車両", "女性専用車両 乗車", "女性専用車両 ホーム"])
    queries = list(_retail_action_queries(original))

    cultural = (
        (("gōkon", "gokon", "group date"), ["合コン", "合コン 居酒屋", "Japanese group date"]),
        (("kokuhaku", "confession"), ["告白", "告白 カップル", "Japanese love confession"]),
        (("izakaya",), ["居酒屋 男女", "居酒屋 デート", "Japanese izakaya friends"]),
        (("garbage truck", "trash truck"), ["ゴミ収集車", "ゴミ収集車 走行", "Japan garbage truck"]),
        (("sorting", "sort your trash", "garbage rules"), ["ゴミ分別", "ゴミ分別 やり方", "Japan trash sorting"]),
        (("address", "street name"), ["日本 住所", "住所 表示 日本", "Japan address system"]),
        # Never key this off the generic word "moving": school-lunch chapters such as
        # "students moving food containers" were silently turned into apartment searches.
        (("apartment", "moving into", "move into", "new neighbor", "new neighbours"),
         ["引っ越し 挨拶", "引っ越し 手土産", "Japan moving apartment"]),
        (("school lunch", "lunch duty", "serve classmates", "itadakimasu"),
         ["給食 当番", "学校給食 配膳", "小学校 給食", "Japanese school lunch students"]),
        (("café", "cafe"), ["カフェ デート", "カップル カフェ", "Japanese cafe date"]),
        # A rare school-safety detail may only appear in one source.  Keep its literal search,
        # but deliberately widen later searches to the surrounding, still-relevant commute so
        # a full edit can use distinct real moments rather than repeat the lone proof clip.
        (("randoseru", "first grader", "first graders", "elementary school", "yellow cover"),
         ["小学生 登校", "小学生 通学路", "ランドセル 通学", "小学生 駅 登校",
          "Japanese schoolchildren commute"]),
        # Retail music is visually indirect: literal sentence translation returns music videos
        # and talking heads. Search the phone-camera actions that can actually illustrate each
        # beat: store ambience, a cashier support call, shelf restocking and staff cleaning.
        (("coded message", "coded messages", "songs or jingles", "store music",
          "employees through music", "stores are secretly talking"),
         ["スーパー 店内BGM", "店内放送 隠語 スーパー", "Japanese supermarket staff music"]),
        (("more cashiers", "cashiers are needed", "cashier help"),
         ["スーパー レジ応援 店員", "レジ応援 店内放送", "Japan supermarket cashier rush"]),
        (("restock", "restocking", "clean an area"),
         ["スーパー 品出し 店員", "コンビニ 品出し 清掃", "Japanese store employee restocking"]),
        (("background music", "strange little tune", "customers never notice"),
         ["日本 スーパー 店内 買い物", "スーパー 店内音楽", "Japan store shopping ambience"]),
        # The actual word tourists hear is the visual research anchor. Without this, the
        # currently failed Irasshaimase project degraded every chapter into generic ATM/checkout
        # searches and never even attempted the greeting ritual.
        (("irasshaimase", "いらっしゃいませ", "welcome shouted", "staff might start shouting",
          "store greeting", "workers as you walk past"),
         ["いらっしゃいませ コンビニ", "いらっしゃいませ 店員", "コンビニ 入店 店員",
          "Japanese store irasshaimase"]),
    )
    def has_concept(needle):
        """Match Latin concepts as complete words/phrases, not accidental substrings.

        ``cafe`` used to match ``cafeteria`` and sent school-lunch runs into Japanese dating
        searches. CJK search anchors do not use Latin word boundaries, so substring matching is
        still appropriate for them.
        """
        needle = str(needle or "").casefold().strip()
        if not needle:
            return False
        if _CJK_RE.search(needle):
            return needle in lowered
        return re.search(r"(?<!\w)" + re.escape(needle) + r"(?!\w)", lowered) is not None

    for needles, additions in cultural:
        if any(has_concept(needle) for needle in needles):
            queries.extend(additions)

    japanese = re.findall(r"[\u3040-\u30ff\u3400-\u9fff]{2,}", original)
    queries.extend(japanese[:2])
    # This list decides what a fallback query is MADE of, and the gaps in it were visible in a
    # delivered run: "has", "where" and "you" were missing, so the chapter about capsule hotels
    # searched TikTok for the literal phrase "Japan has hotels where you" and got back anything
    # at all. Auxiliaries, pronouns, question words and degree words are never the visible
    # subject of a shot, so none of them may occupy one of the five slots.
    stop = {"the", "a", "an", "and", "or", "but", "with", "without", "in", "on", "at",
            "to", "from", "of", "for", "is", "are", "was", "were", "it", "this", "that",
            "some", "may", "can", "after", "before", "only", "not", "their", "your",
            "has", "have", "had", "been", "being", "does", "did", "will", "would", "could",
            "should", "must", "might", "who", "whom", "whose", "which", "what", "when",
            "where", "why", "how", "you", "they", "them", "its", "his", "her", "our", "her",
            "than", "then", "there", "here", "into", "onto", "about", "just", "even", "also",
            "very", "really", "most", "more", "each", "every", "much", "many", "while",
            "because", "since", "such", "other", "another", "same", "own", "one", "two",
            "get", "gets", "got", "use", "uses", "used", "using", "make", "makes", "made"}
    words = [w for w in re.findall(r"[A-Za-zÀ-ž0-9'-]+", original)
             if len(w) > 2 and w.casefold() not in stop]
    if words:
        queries.append(" ".join(words[:5]))
    return sanitize_queries(priority + queries, limit=_queries_per_chapter())


def _retail_action_queries(text):
    """Native, phone-video-shaped searches for the visible actions behind retail narration."""
    lowered = str(text or "").casefold()
    groups = []
    # These terms occur outside convenience stores all the time (a commuter can carry cash,
    # coffee or a parcel).  Treat Konbini as an explicit visual world, never as a default
    # association.  Otherwise a word such as "cash" silently turns a train, banking or home
    # story into a convenience-store scrape.
    retail_context = any(term in lowered for term in (
        "convenience store", "convenience stores", "konbini", "コンビニ", "supermarket",
        "super market", "grocery store", "shop", "shopping", "cashier", "checkout",
        "register", "store music", "store greeting", "store employee", "retail"))
    if any(term in lowered for term in
           ("bento", "hot meal", "hot food", "snack", "弁当", "おにぎり")):
        groups.append((["コンビニ 弁当", "コンビニ おにぎり", "コンビニ ごはん"]
                       if retail_context else ["弁当", "おにぎり", "日本 お弁当"]))
    if any(term in lowered for term in ("coffee", "iced coffee", "コーヒー")):
        groups.append((["コンビニ コーヒー", "コンビニ アイスコーヒー"]
                       if retail_context else ["日本 コーヒー", "アイスコーヒー"]))
    if any(term in lowered for term in
           ("package pickup", "package", "parcel", "delivery pickup", "荷物")):
        groups.append((["コンビニ 荷物受け取り", "コンビニ 宅配便"]
                       if retail_context else ["荷物 受け取り", "宅配便 受け取り"]))
    if any(term in lowered for term in ("atm", "cash", "withdraw")):
        groups.append((["コンビニ ATM", "コンビニ 現金"]
                       if retail_context else ["ATM 引き出し", "現金 引き出し"]))
    # ``print`` must be a word, not a substring: a platform sign described as "printed"
    # previously triggered copier searches in a women-only train story.
    if (re.search(r"\b(?:print|printer|copier)\b", lowered)
            or "copy machine" in lowered or "コピー" in lowered):
        groups.append((["コンビニ コピー", "コンビニ 印刷"]
                       if retail_context else ["コピー機 印刷", "プリンター 印刷"]))
    if any(term in lowered for term in
           ("breakfast", "before trains", "commute", "commuter", "morning")):
        # A commute is not evidence of a convenience store. This old mapping contaminated the
        # women-only-train run with Konbini/ATM searches and squandered its entire budget.
        groups.append(["朝 通勤 電車", "駅ホーム 通勤", "通勤ラッシュ 電車"])
    if any(term in lowered for term in (
            "coded message", "coded messages", "songs or jingles", "store music",
            "employees through music", "stores are secretly talking", "background music")):
        groups.append(["スーパー 店内BGM", "コンビニ 店内BGM", "店内放送 隠語"])
    if any(term in lowered for term in
           ("more cashiers", "cashiers are needed", "cashier help", "cashier")):
        groups.append(["レジ打ち", "スーパー レジ 店員", "コンビニ店員 レジ"])
    if any(term in lowered for term in ("restock", "restocking", "clean an area", "cleaning")):
        school_context = any(term in lowered for term in (
            "school", "classroom", "student", "students", "corridor", "o-soji", "osoji"))
        if retail_context:
            groups.append(["品出し 店員", "コンビニ 品出し", "スーパー 清掃 店員"])
        elif school_context:
            groups.append(["学校 掃除 生徒", "教室 掃除", "小学校 掃除時間"])
        else:
            # ``品出し`` means stocking shelves, not generic cleaning.  It contaminated
            # school and office stories merely because their narration contained "cleaning".
            groups.append(["清掃 作業", "掃除 ルーティン", "日本 清掃"])
    if any(term in lowered for term in
           ("customers never notice", "shopping in japan", "strange little tune")):
        groups.append(["スーパー買い出し", "コンビニ買い物", "スーパー買い物vlog"])
    # Broad retail footage is useful only after distinct actions/rituals. Leading with this
    # group made different chapters share the identical generic pool.
    if any(term in lowered for term in
           ("convenience store", "convenience stores", "konbini", "コンビニ")):
        groups.append(["コンビニ", "コンビニ 買い物", "コンビニ ルーティン"])
    # Interleave concepts. If one chapter mentions music, cashiers and restocking, its five-slot
    # budget must contain all three actions instead of spending the first three slots on music.
    rows = []
    for index in range(max((len(group) for group in groups), default=0)):
        for group in groups:
            if index < len(group):
                rows.append(group[index])
    return rows


def _retail_context_proxies(text):
    """Concrete store actions that are honest cover for a multi-fact retail beat.

    Planner prose routinely turns a useful process into an isolated impossible requirement such
    as ``ATM plus copier``.  These are not generic filler: each is a visible use of the same
    Japanese convenience-store system and gives the vision reviewer an explicit, bounded
    contextual alternative.
    """
    lowered = str(text or "").casefold()
    if not any(term in lowered for term in
               ("convenience store", "convenience stores", "konbini", "コンビニ")):
        return []
    out = [
        "a shopper choosing prepared food in a Japanese convenience store",
        "a cashier scanning a basket at a Japanese convenience-store checkout",
        "a customer using the coffee machine or self-service counter",
        "a worker stocking food shelves in a Japanese convenience store",
    ]
    if any(term in lowered for term in ("atm", "cash", "withdraw", "print", "printer", "copier")):
        out.insert(0, "a customer using an ATM, copier, printer or payment counter inside a Japanese convenience store")
    if any(term in lowered for term in ("package", "parcel", "delivery pickup")):
        out.insert(1, "a customer collecting or handing over a parcel at a Japanese convenience-store counter")
    return out[:6]


def chapter_limits(scenes):
    """Editorial chapter count from spoken duration, not arbitrary sentence count."""
    rows = list(scenes or [])
    duration = max((float(row.get("end") or 0.0) for row in rows), default=0.0)
    # Short-form edits need enough distinct worlds to move, but not a bespoke search problem for
    # every sentence. The 26-second production test was fragmented into an unusable ATM chapter.
    if duration <= 30.0:
        hi = 3
    elif duration <= 42.0:
        hi = 4
    elif duration <= 58.0:
        hi = 5
    else:
        hi = V3_CONFIG["max_chapters"]
    hi = max(1, min(int(hi), len(rows) or 1))
    return min(V3_CONFIG["min_chapters"], hi), hi


def _scene_key(scene, index):
    """Return a stable, unique key even when upstream beats carry ``id: None``.

    The voice planner creates some interim scene dicts with an explicit null id.  Converting that
    value to ``"None"`` made every beat share one identity, so V3 could produce overlapping
    chapters such as a 0-31s bento scene on top of later 4-16s scenes.  Fall back to the local
    ordinal whenever the upstream id is blank.
    """
    value = scene.get("id") if isinstance(scene, dict) else None
    return str(value) if value not in (None, "") else f"scene-{int(index)}"


def _deterministic_chapter_plan(scenes):
    """Evenly group real voice lines and derive every fallback field from their words."""
    scenes = list(scenes or [])
    if not scenes:
        return []
    lo, hi = chapter_limits(scenes)
    count = min(hi, max(lo, min(len(scenes), 5)))
    groups = []
    for chapter_index in range(count):
        start = round(chapter_index * len(scenes) / count)
        end = round((chapter_index + 1) * len(scenes) / count)
        rows = scenes[start:end]
        if not rows:
            continue
        text = " ".join(filter(None, (_scene_voice_text(row) for row in rows))).strip()
        words = text.split()
        title = " ".join(words[:8]).strip(" .,!?:;-") or "Spoken topic"
        groups.append({
            "title": title,
            "scene_ids": [_scene_key(row, start + offset) for offset, row in enumerate(rows)],
            "subject": text,
            "action": text,
            "evidence": text,
            "proxies_ok": [],
            "forbidden": ["video games", "animation", "unrelated generic footage"],
            "queries": _fallback_queries_from_text(text),
            "allow_multi_window": True,
        })
    return groups


def normalize_chapter_plan(raw_plan, scenes, status_cb=None):
    """Turn a plan into duration-appropriate chapters that cover every voice line.

    The plan is advice; the voice timeline is fact. Chapters are clamped to the real scene list,
    every scene ends up in exactly one chapter, and the count is forced into range - a planner
    that returns two chapters for a 35-second script has not planned a short, and one that
    returns twelve has just renamed the sentences.
    """
    scenes = list(scenes or [])
    if not scenes:
        return []
    ids = [_scene_key(scene, index) for index, scene in enumerate(scenes)]
    by_id = {sid: scenes[i] for i, sid in enumerate(ids)}
    lo, hi = chapter_limits(scenes)

    # A missing/invalid model response must still be ABOUT THE SCRIPT. The old empty-plan branch
    # created four blank entries titled "Chapter 1"..."Chapter 4"; those strings became literal
    # TikTok queries. Build an honest local plan from the voice instead.
    plan_rows = list(raw_plan or [])
    if not plan_rows:
        plan_rows = _deterministic_chapter_plan(scenes)

    groups, seen = [], set()
    for entry in plan_rows:
        if not isinstance(entry, dict):
            continue
        wanted = [str(x) for x in (entry.get("scene_ids") or []) if str(x) in by_id]
        wanted = [x for x in wanted if x not in seen]
        if not wanted:
            continue
        seen.update(wanted)
        groups.append((entry, wanted))

    leftover = [sid for sid in ids if sid not in seen]
    if leftover:
        if groups:
            # Attach each orphan to the chapter it sits next to in time, not to the last one.
            for sid in leftover:
                index = ids.index(sid)
                best = min(range(len(groups)),
                           key=lambda g: min(abs(index - ids.index(x)) for x in groups[g][1]))
                groups[best][1].append(sid)
                groups[best][1].sort(key=ids.index)
        else:
            groups = [({}, list(ids))]

    # A planner can understand the right visual ideas yet list their scene ids out of narration
    # order (for example ``[hook, final payoff]`` in one chapter and the middle beats in another).
    # Each id was unique, but using its first/last timestamp made the chapter spans overlap and
    # produced an impossible timeline.  Project every planned ownership label onto the actual
    # voice order and clamp it forward: chapters are always contiguous time ranges, never a
    # collection of disjoint moments spread across the whole Short.
    owner = {}
    for group_index, (_, members) in enumerate(groups):
        for sid in members:
            owner[str(sid)] = group_index
    ordered_group_ids = []
    for sid in ids:
        group_index = owner.get(sid, 0)
        if group_index not in ordered_group_ids:
            ordered_group_ids.append(group_index)
    rank_for_group = {group_index: rank for rank, group_index in enumerate(ordered_group_ids)}
    last_rank = 0
    contiguous = [[] for _ in ordered_group_ids]
    for sid in ids:
        rank = rank_for_group.get(owner.get(sid, 0), 0)
        rank = max(last_rank, rank)
        contiguous[rank].append(sid)
        last_rank = rank
    groups = [(groups[group_index][0], members)
              for group_index, members in zip(ordered_group_ids, contiguous) if members]

    if len(groups) > hi:
        # Merge the shortest neighbours until the count fits.
        while len(groups) > hi:
            spans = [sum(_scene_seconds(by_id[x]) for x in g[1]) for g in groups]
            i = spans.index(min(spans))
            j = i - 1 if i == len(groups) - 1 else i + 1
            merged = sorted(groups[i][1] + groups[j][1], key=ids.index)
            entry = groups[min(i, j)][0]
            groups[min(i, j)] = (entry, merged)
            groups.pop(max(i, j))
    if len(groups) < lo and len(ids) >= lo:
        # Split the longest chapters until there are enough ideas to cut between.
        while len(groups) < lo and any(len(g[1]) > 1 for g in groups):
            spans = [sum(_scene_seconds(by_id[x]) for x in g[1]) if len(g[1]) > 1 else -1.0
                     for g in groups]
            i = spans.index(max(spans))
            entry, members = groups[i]
            half = max(1, len(members) // 2)
            tail = dict(entry)
            # A split is one idea shown twice, not two ideas. Saying so keeps the report honest
            # and stops two chapters appearing under one identical name.
            if entry.get("title"):
                tail["title"] = f"{entry['title']} (continued)"
            groups[i] = (entry, members[:half])
            groups.insert(i + 1, (tail, members[half:]))

    chapters = []
    for index, (entry, members) in enumerate(groups):
        members = sorted(members, key=ids.index)
        first, last = by_id[members[0]], by_id[members[-1]]
        voice_text = " ".join(_scene_voice_text(by_id[sid]) for sid in members).strip()
        entry_title = str(entry.get("title") or "").strip()
        if not entry_title or _GENERIC_CHAPTER_QUERY.fullmatch(entry_title):
            entry_title = " ".join(voice_text.split()[:8]).strip(" .,!?:;-") or "Spoken topic"
        entry_subject = str(entry.get("subject") or "").strip() or voice_text
        entry_action = str(entry.get("action") or "").strip() or voice_text
        entry_evidence = str(entry.get("evidence") or "").strip() or voice_text
        # Preserve a rare named ritual/object from the words actually spoken before widening to
        # ordinary store actions.  The old order let "コンビニ" / ATM consume all five slots for
        # an Irasshaimase story, so the scraper never searched its central visible action.
        literal_queries = (_fallback_queries_from_text(voice_text)
                           + list(entry.get("queries") or [])
                           + _retail_action_queries(voice_text))
        entry_proxies = list(entry.get("proxies_ok") or []) + _retail_context_proxies(voice_text)
        proxy_queries = entry_proxies
        interleaved_queries = []
        for slot in range(max(len(literal_queries), len(proxy_queries), 1)):
            if slot < len(literal_queries):
                interleaved_queries.append(literal_queries[slot])
            if slot < len(proxy_queries):
                interleaved_queries.append(proxy_queries[slot])
        specific = [query for query in interleaved_queries
                    if not _GENERIC_RETAIL_QUERY.fullmatch(str(query or "").strip())]
        generic = [query for query in interleaved_queries
                   if _GENERIC_RETAIL_QUERY.fullmatch(str(query or "").strip())]
        entry_queries = (sanitize_queries(specific + generic)
                         or _fallback_queries_from_text(voice_text))
        chapter = Chapter(
            chapter_id=index,
            title=entry_title[:80],
            scene_ids=members,
            start=float(first.get("start", 0.0) or 0.0),
            end=float(last.get("end", 0.0) or 0.0),
            subject=entry_subject,
            action=entry_action,
            evidence=entry_evidence,
            proxies_ok=[str(x) for x in entry_proxies][:6],
            forbidden=[str(x) for x in (entry.get("forbidden") or [])][:6],
            queries=entry_queries,
            allow_multi_window=bool(entry.get("allow_multi_window", True)),
            is_hook=bool(entry.get("is_hook")) or index == 0,
        )
        chapters.append(chapter)
    # Splitting a long chapter twice produced two identical "(continued)" titles, which reads in
    # the report as one idea shown twice under one name. Number them instead.
    counts = {}
    for chapter in chapters:
        counts[chapter.title] = counts.get(chapter.title, 0) + 1
    used = {}
    for chapter in chapters:
        if counts.get(chapter.title, 0) < 2:
            continue
        used[chapter.title] = used.get(chapter.title, 0) + 1
        chapter.title = f"{chapter.title} {used[chapter.title]}"
    _log(status_cb, "Scrape V3: %d chapter(s) planned across %d voice line(s)."
         % (len(chapters), len(ids)))
    return chapters


def _scene_seconds(scene):
    try:
        return max(0.0, float(scene.get("end", 0) or 0) - float(scene.get("start", 0) or 0))
    except (TypeError, ValueError):
        return 0.0


CHAPTER_PROMPT = """You are the editor of a vertical short. You are NOT labelling sentences.

Group the narration below into {lo}-{hi} VISUAL CHAPTERS. A chapter is ONE tangible idea that a
viewer could watch as a continuous piece of footage. Several sentences belong in one chapter when
they describe the same real-world thing: "garbage trucks play melodies" and "sorting rules change
by city" are both the garbage system, so they are ONE chapter, not two.

CRITICAL EDITORIAL RULE: do not search for an abstract sentence, explanation, dictionary entry,
word definition, infographic, text card or talking-head explanation. Translate abstract narration
into a PHONE-CAMERA ACTION, emotion, visual comparison or surprising human behaviour. A language
history line about blue versus green should search for people pointing at blue-green signals,
green objects that Japanese speakers call ao, street reactions, or a clearly filmed color contrast
in the real world — not "ao dictionary explanation". The footage should remain interesting with
all audio and platform captions removed.

For each chapter give a brief a researcher could search with:
- subject: the concrete thing on screen (a named place, object, machine, or the people using it)
- action: what visibly happens
- evidence: what the shot MUST show for the idea to land. ONE observable condition, not a
  sequence of them. Measured on a delivered Short (2026-08-29): a chapter that asked for
  "One continuous sequence must show the doors opening, the friends leaving the train, turning
  toward one another, and visibly talking together on the platform" accepted 0 of 20 downloads,
  because that is a scripted narrative shot and nobody posts one. The same 20 clips re-graded
  against "a station platform with passengers who have left, or are leaving, a train" accepted
  three times as many. Name the ONE thing that must be visible; never chain conditions with
  "and then", "followed by" or "one continuous sequence".
- proxies_ok: 2-4 concrete, watchable context actions from the SAME world/process that can cover
  the narration after one proof shot is found. These must be real things a phone camera can show,
  not vague words such as "culture" or "atmosphere". Example: a rare backpack safety cover can
  use children walking their school route, waiting at a crossing, or entering a station as context.
- forbidden: visuals that would be wrong or misleading here
- queries: 3-5 searches, in this order where they apply:
    1. the native-language phrase for the object or place, as locals type it
    2. the native-language object + action phrase
    3. a compact English discovery query
    4. optional: a creator/vlog phrasing
  Every query must be a compact 1-5 keyword search phrase, never a sentence.
  NEVER put a platform name (TikTok, Instagram, X, Reels, Shorts) in a query.
  NEVER use search words such as explanation, meaning, dictionary, definition, history, infographic,
  diagram or 解説 unless the narration is specifically about watching somebody perform that action.
- allow_multi_window: true when several moments from ONE video could carry this chapter

NARRATION LINES (id: text, seconds):
{lines}

Return STRICT JSON:
{{"chapters":[{{"title":"...","scene_ids":["..."],"subject":"...","action":"...",
"evidence":"...","proxies_ok":["..."],"forbidden":["..."],"queries":["..."],
"allow_multi_window":true}}]}}"""


# Where the topic happens, in the fewest words that still make a search. The dance hook is
# deliberately non-literal, but a hook filmed in the video's own world ("dancing on a Tokyo
# platform" over a Short about train silence) reads as part of the video instead of as a
# stranger bolted to the front. Requested 2026-08-29: topical "wenn moeglich", generic
# otherwise - so this only steers the SEARCH. Acceptance stays generic, and a plain dance is
# still a valid hook when the topical search comes back empty.
_HOOK_PLACE_STOP = {
    "a", "an", "the", "and", "or", "of", "in", "on", "at", "with", "real", "their", "his",
    "her", "its", "one", "two", "some", "any", "who", "that", "this", "these", "those",
    "man", "men", "woman", "women", "person", "people", "passenger", "passengers", "rider",
    "riders", "friend", "friends", "commuter", "commuters", "worker", "workers", "guest",
    "guests", "visitor", "visitors", "customer", "customers", "young", "adult", "japanese",
}


def _hook_place_hint(chapters):
    """Two or three words naming the topic's setting, or "" when nothing is clear enough."""
    words = []
    for chapter in chapters or []:
        for field in (getattr(chapter, "subject", ""), getattr(chapter, "title", "")):
            for word in re.findall(r"[A-Za-z][A-Za-z-]+", str(field or "")):
                folded = word.casefold()
                if folded in _HOOK_PLACE_STOP or len(folded) < 3:
                    continue
                if folded not in words:
                    words.append(folded)
        if len(words) >= 3:
            break
    return " ".join(words[:2])


def _hook_queries_for(place):
    """Topical dance searches first, the proven generic ones after."""
    generic = ["日本 女性 ダンス", "可愛い ダンス",
               "Japanese young adult dance", "Japanese dance vlog"]
    if not place:
        return generic
    topical = [f"{place} dance", f"Japanese {place} dance"]
    return topical + generic


def _inject_influencer_hook_v3(chapters, scenes, status_cb=None):
    """Reserve the spoken opener for the optional retention-dance hook.

    V3 normally plans one continuous visual world for the first chapter.  That is right for a
    factual opener, but it also meant the existing ``influencer_hook`` option was silently
    ignored in V3 while it worked in the older scraper.  Split only the first timed voice beat
    out into a compact, clearly labelled *non-literal* hook chapter.  The remaining chapters keep
    their topic-matched editorial brief, so a dance never gets treated as proof of the fact.
    """
    rows = list(scenes or [])
    planned = list(chapters or [])
    if not rows or not planned:
        return planned
    first = rows[0]
    first_id = _scene_key(first, 0)
    hook_start = float(first.get("start", 0.0) or 0.0)
    hook_end = float(first.get("end", hook_start) or hook_start)
    if hook_end - hook_start < 0.5:
        return planned

    remainder = []
    removed = False
    for chapter in planned:
        members = [str(member) for member in (chapter.scene_ids or [])]
        if first_id not in members:
            remainder.append(chapter)
            continue
        kept = [member for member in members if member != first_id]
        if not kept:
            removed = True
            continue
        chapter.scene_ids = kept
        # The first remaining narration beat begins this topic chapter.  Keeping the old start
        # would make the dance scene consume topic footage time during final fitting.
        next_scene = next((scene for index, scene in enumerate(rows)
                           if _scene_key(scene, index) == kept[0]), None)
        if next_scene is not None:
            chapter.start = float(next_scene.get("start", hook_end) or hook_end)
        else:
            chapter.start = hook_end
        remainder.append(chapter)
        removed = True

    if not removed:
        return planned

    hook = Chapter(
        chapter_id=0,
        title="Cute dance retention hook",
        scene_ids=[first_id],
        start=hook_start,
        end=hook_end,
        subject="a young adult Japanese woman",
        action="doing a playful short dance or expressive movement directly in a real everyday setting",
        evidence="one clearly visible young adult woman actively dancing or moving playfully; no talking-to-camera explanation",
        proxies_ok=["a young adult woman smiling while doing a short dance", "a playful movement in a real Japanese street, cafe or home setting"],
        forbidden=["talking-head explanation", "children", "slideshow", "generic stock footage"],
        queries=_hook_queries_for(_hook_place_hint(remainder)),
        allow_multi_window=False,
        is_hook=True,
    )
    planned = [hook] + remainder
    for index, chapter in enumerate(planned):
        chapter.chapter_id = index
        chapter.is_hook = bool(chapter.is_hook) or index == 0
    _place = _hook_place_hint(remainder)
    _log(status_cb, "Scrape V3: cute dance retention hook enabled; the first %.1fs is searched "
         "as a separate non-literal opener%s." % (
             hook.target_seconds,
             (", preferring a dance filmed in the video's own world (%s)" % _place)
             if _place else " (no clear setting in the script - generic dance search)"))
    return planned


def plan_chapters_v3(scenes, script_text, reasoning_model=None, status_cb=None,
                     influencer_hook=False):
    """Ask the planner for chapters, then hold it to the voice timeline."""
    lines = []
    for index, scene in enumerate(scenes or []):
        sid = _scene_key(scene, index)
        text = _scene_voice_text(scene)
        lines.append(f'{sid}: "{text}" ({_scene_seconds(scene):.1f}s)')
    lo, hi = chapter_limits(scenes)
    prompt = CHAPTER_PROMPT.format(lo=lo, hi=hi,
                                   lines="\n".join(lines) or script_text[:1200])
    raw = []
    try:
        # V2 exposes _llm_json, not _reason_json. The old hasattr branch was therefore false on
        # every run and V3 never called its chapter planner at all.
        data = scrape_v2._llm_json(
            [{"role": "system", "content": "Plan searchable visual chapters from real narration."},
             {"role": "user", "content": prompt}],
            max_tokens=2400, temperature=0.2, reasoning_model=reasoning_model,
            status_cb=status_cb, label="Clip Short V3 chapter planner")
        if isinstance(data, dict):
            raw = data.get("chapters") or []
    except Exception as exc:      # noqa: BLE001 - planning must degrade, never abort the run
        _log(status_cb, f"Scrape V3: chapter planner unavailable ({type(exc).__name__}); "
                        "falling back to an even split of the narration.")
    chapters = normalize_chapter_plan(raw, scenes, status_cb=status_cb)
    if influencer_hook:
        chapters = _inject_influencer_hook_v3(chapters, scenes, status_cb=status_cb)
    return chapters


INSTAGRAM_ACCOUNTS_PROMPT = """Name real Instagram accounts whose reels would show this footage.

TOPIC: {topic}

WHAT THE SHORT NEEDS TO SHOW:
{beats}

Rules:
- Real, currently active accounts only. If you are not confident an account exists, leave it out -
  a wrong handle costs a paid API call and returns nothing.
- Pick accounts whose ORDINARY posts are this subject matter, not accounts that mentioned it once.
- RAW FOOTAGE, NOT PUBLICITY. Never name the official account of the company, operator, museum or
  city involved: they post polished adverts, announcements and staff portraits. Measured on a
  train-pushers short, the official operator accounts returned tea commercials, boots and a beach,
  and the vision pass rejected every one. Name the people who FILM the thing: POV vloggers, daily
  commuters, hobbyists, documentary channels, tourists who post unedited phone clips.
- Prefer accounts whose posts are handheld and unedited over ones that are colour-graded and
  captioned like a brand reel.
- Handles only, no URLs, no @.

Return JSON: {{"accounts": ["handle", ...]}} - at most {limit}."""


def instagram_accounts_for(script_text, scenes, limit=8, reasoning_model=None, status_cb=None):
    """Which Instagram creators this run should pull reels from.

    Instagram offers no keyword or hashtag discovery on this Bright Data account - only reels by
    profile - so a topic has to be turned into a list of people who film it. This is the step that
    does that, and it runs ONCE per project: the pull it feeds is billed per record.
    """
    beats = []
    for index, scene in enumerate(scenes or []):
        text = _scene_voice_text(scene)
        if text:
            beats.append(f"- {text}")
        if len(beats) >= 12:
            break
    prompt = INSTAGRAM_ACCOUNTS_PROMPT.format(
        topic=(script_text or "")[:400],
        beats=chr(10).join(beats) or "(no beats)", limit=int(limit))
    try:
        data = scrape_v2._llm_json(
            [{"role": "system",
              "content": "Name real Instagram accounts that post a given kind of footage."},
             {"role": "user", "content": prompt}],
            max_tokens=400, temperature=0.3, reasoning_model=reasoning_model,
            status_cb=status_cb, label="Clip Short V3 Instagram accounts")
    except Exception as exc:      # noqa: BLE001 - no accounts is a thinner run, not a failed one
        _log(status_cb, f"Scrape V3: could not pick Instagram accounts "
                        f"({type(exc).__name__}); this run will use TikTok only.")
        return []
    raw = (data or {}).get("accounts") if isinstance(data, dict) else None
    out, seen = [], set()
    for entry in raw or ():
        handle = str(entry or "").strip().lstrip("@").strip("/")
        # A model asked for handles will sometimes answer with URLs anyway.
        if "instagram.com/" in handle:
            handle = handle.split("instagram.com/", 1)[1].split("/")[0].split("?")[0]
        if not re.fullmatch(r"[A-Za-z0-9._]{1,30}", handle or ""):
            continue
        if handle.lower() in seen:
            continue
        seen.add(handle.lower())
        out.append(handle)
        if len(out) >= int(limit):
            break
    if out:
        _log(status_cb, f"Scrape V3: Instagram reels will come from {', '.join(out)}.")
    return out


# --------------------------------------------------------------------------- search & download
def _queries_per_chapter():
    """How many search terms one chapter gets - wider when the paid API is the only source."""
    if getattr(clip_scraper, "BRIGHTDATA_ONLY", False):
        return int(V3_CONFIG.get("queries_per_chapter_api")
                   or V3_CONFIG["queries_per_chapter"])
    return int(V3_CONFIG["queries_per_chapter"])


# Searching the CATEGORY returns the category. Measured on the Tokyo-train run (2026-08-30):
# the chapter searched 朝 通勤 電車 / 通勤ラッシュ 電車 - "morning commute train" - and the pool
# came back as eleven near-identical shots of people boarding. Re-scored with the watchability
# prompt, ten of them sat between 4 and 7 and exactly ONE reached 9: a station attendant
# physically pushing commuters into a carriage. Ranking cannot invent that clip; only the search
# can go looking for it.
#
# So each chapter also searches for the EXTREME version of its own subject. The intensifiers are
# words creators actually caption with, in the language of the subject term - a Japanese subject
# gets Japanese intensifiers, because that is where the footage is captioned. The subject term is
# kept verbatim, so the topic anchor still matches and `_query_drifted` lets these through.
_STRIKING_JA = ("ヤバい", "衝撃", "限界", "密着")
_STRIKING_EN = ("insane", "extreme", "chaos", "close up")


def _striking_queries(chapter, base_queries, limit=3):
    """Searches aimed at the most extreme version of the chapter's subject."""
    out = []
    for index, query in enumerate(list(base_queries or [])[:3]):
        query = str(query or "").strip()
        if not query:
            continue
        words = _STRIKING_JA if _CJK_RE.search(query) else _STRIKING_EN
        # Rotate the intensifier: three searches all ending in the same word are one search
        # repeated, and they would spend three slots to look in the same place.
        candidate = f"{query} {words[index % len(words)]}"
        if candidate not in out:
            out.append(candidate)
        if len(out) >= limit:
            break
    return out


def chapter_search_queries(chapter):
    """Return compact discovery terms with native/action fallbacks ahead of planner prose."""
    # Planner output is useful, but must never be the sole search input. Models occasionally
    # return a beautiful visual sentence that no creator would type into TikTok. Deterministic
    # action terms from the actual chapter keep the search executable.
    context = " ".join(str(part or "") for part in (
        chapter.title, chapter.subject, chapter.action, chapter.evidence, *chapter.proxies_ok))
    fallback = _fallback_queries_from_text(context)
    # Do not let a fallback such as the bare title "Retail" displace a caller's deliberately
    # supplied queries. Native/domain action terms and multi-hypothesis fallbacks do go first.
    useful_fallback = (len(fallback) > 1
                       or any(_CJK_RE.search(query) for query in fallback)
                       or bool(_retail_action_queries(context)))
    # A retention hook is intentionally non-literal. Its carefully authored dance/presenter
    # queries must win over broad semantic fallbacks ("cafe date" was otherwise derived from
    # "young woman in an everyday setting" and silently replaced the selected hook style).
    if chapter.is_hook:
        combined = (list(chapter.explicit_queries or [])
                    + list(chapter.queries or [])
                    + (fallback if useful_fallback else []))
    else:
        combined = (list(chapter.explicit_queries or [])
                    + (fallback if useful_fallback else [])
                    + list(chapter.queries or []))
    # The normalized plan already ranked its specific ritual/object terms. Keep that ordering on
    # the final search boundary too; otherwise `_fallback_queries_from_text` reintroduced bare
    # "コンビニ" before "いらっしゃいませ" and undid the planner's work.
    specific = [query for query in combined
                if not _GENERIC_RETAIL_QUERY.fullmatch(str(query or "").strip())]
    generic = [query for query in combined
               if _GENERIC_RETAIL_QUERY.fullmatch(str(query or "").strip())]
    queries = sanitize_queries(specific + generic)
    if queries:
        # Placed after the first two proven terms rather than appended: the per-chapter budget
        # would otherwise cut them off before they ever ran.
        if not chapter.is_hook:
            striking = [q for q in _striking_queries(chapter, queries, limit=2)
                        if q not in queries]
            if striking:
                # ADDITIVE, never displacing. Fitting them inside the existing budget pushed
                # proven terms off the end - the bare "コンビニ" among them, which an earlier
                # measurement had already shown must not be dropped to save a slot. Two extra
                # searches per chapter is the deliberate price for looking for the striking
                # version at all.
                queries = sanitize_queries(queries[:2] + striking + queries[2:],
                                           limit=_queries_per_chapter() + len(striking))
        return queries
    seed = " ".join(x for x in (chapter.subject, chapter.action) if x).strip()
    seed = seed or chapter.evidence
    queries = sanitize_queries([seed, chapter.subject, chapter.evidence], limit=3)
    if not queries:
        queries = _fallback_queries_from_text(chapter.brief())
    return queries


ADAPTIVE_QUERY_PROMPT = """You are correcting a failed social-video search for a vertical short.

The footage must show a tangible ACTION, reaction, surprising behaviour or concrete proof. Do not
translate the narration sentence. Search for what a phone camera would physically capture. Prefer
watchable user-generated footage over talking heads and generic establishing shots.

CHAPTER: {title}
SUBJECT/ACTION/EVIDENCE: {brief}
ALREADY TRIED: {attempted}
WHAT FAILED: {failures}
WHICH SEARCHES WASTED THE SLOTS: {dead_queries}

Return 4-6 NEW raw search strings. Include native Japanese queries written only in native
characters where useful, plus focused English queries. Do not add translations, Romaji, notes,
hashtags, years, result-order words, or platform names. Do not repeat an attempted query.
Native Japanese queries must look like short creator tags (normally 1-3 compact concepts), not a
translated sentence. Prefer forms such as スーパー買い出し, スーパー購入品, コンビニ買い物 or 買い物vlog
over prose such as "a person walking through an aisle carrying a shopping basket".

Treat WHICH SEARCHES WASTED THE SLOTS as the most important line: those exact hypotheses are
spent. Do not return a rephrasing of any of them - change what the camera is pointed at.

Behave like a human editor changing tactics, not a synonym generator. Use this search ladder:
- after literal proof fails, search the visible action caused by the claim;
- then search a person using the same object/place/system;
- then use short native creator/vlog phrases locals actually post;
- finally search the broad real-world setting plus a concrete action.
Never keep adding adjectives to the same failed phrase. Every new query must represent a visibly
different footage hypothesis while remaining understandable beside the narration.

STRICT JSON: {{"queries":["...","..."]}}"""


def adaptive_search_queries(chapter, attempted, rejected, round_index=1,
                            reasoning_model=None, status_cb=None):
    """Create the next search round from what the previous visual inspection actually rejected.

    This is the difference between a static query list and an editor searching: zero useful
    action widens toward demonstrations/POVs, caption-heavy results move toward raw local footage,
    and unrelated results tighten back onto the concrete subject. The model gets the rejection
    log, but a deterministic fallback keeps the retry working when that call is unavailable.
    """
    attempted = sanitize_queries(attempted or [], limit=100)
    attempted_folded = {q.casefold() for q in attempted}
    counts = {}
    for row in (rejected or []):
        reason = str((row or {}).get("reason") or "unknown")
        counts[reason] = counts.get(reason, 0) + 1
    failure_text = ", ".join(f"{key} x{value}" for key, value in sorted(counts.items())) or "no usable windows"
    # Per-QUERY yield, not just a tally of reasons. "unrelated_filler x10" tells the model that
    # something went wrong; it does not say that 朝 通勤 電車, 駅ホーム 通勤 and 通勤ラッシュ 電車
    # were the three hypotheses that produced it, so the next round could propose the same shape
    # again. An editor changing tactics looks at which search wasted the slots.
    per_query = {}
    for row in (rejected or []):
        query = str((row or {}).get("query") or "").strip()
        if not query:
            continue
        bucket = per_query.setdefault(query, {})
        reason = str((row or {}).get("reason") or "unknown")
        bucket[reason] = bucket.get(reason, 0) + 1
    dead_queries = "; ".join(
        "%s -> %s" % (query, ", ".join("%s x%d" % (r, n) for r, n in sorted(reasons.items())))
        for query, reasons in sorted(per_query.items(), key=lambda kv: -sum(kv[1].values()))[:8]
    ) or "not attributed"
    raw = []
    prompt = ADAPTIVE_QUERY_PROMPT.format(
        title=chapter.title, brief=chapter.brief(),
        attempted=" | ".join(attempted[-20:]) or "none", failures=failure_text,
        dead_queries=dead_queries)
    try:
        data = scrape_v2._llm_json(
            [{"role": "system", "content": "Repair weak social-footage searches using visible action."},
             {"role": "user", "content": prompt}],
            max_tokens=900, temperature=0.35, reasoning_model=reasoning_model,
            status_cb=status_cb, label=f"Clip Short V3 adaptive search round {round_index + 1}")
        if isinstance(data, dict):
            raw = data.get("queries") or []
    except Exception as exc:  # noqa: BLE001 - a query retry must have a local fallback
        _log(status_cb, f"Scrape V3: adaptive query agent unavailable ({type(exc).__name__}); "
                        "using action-query fallbacks.")

    # When a precise retail phrase fails, widen to tags that people genuinely post under. The
    # previous retry merely invented another 3-5 word phrase and TikTok returned zero forever.
    retail_blob = " ".join((chapter.title, chapter.brief())).casefold()
    broad = []
    if any(term in retail_blob for term in
           ("store", "retail", "supermarket", "cashier", "shopper", "shopping", "customer",
            "コンビニ", "スーパー", "レジ", "買い物")):
        broad.extend(["スーパー店内", "コンビニ店内", "スーパー店員", "コンビニ店員"])
    if any(term in retail_blob for term in
           ("shopper", "shopping", "customer", "browse", "aisle", "買い物")):
        # These were validated against live TikTok results: short creator tags surface real
        # in-store behaviour, while translated action sentences return zero or advertising.
        broad.extend(["スーパー買い出し", "スーパー買い物vlog",
                      "コンビニ買い物", "コンビニルーティン"])
    if any(term in retail_blob for term in ("cashier", "register", "checkout", "レジ")):
        broad.extend(["レジ打ち", "レジ混雑"])
    if any(term in retail_blob for term in
           ("restock", "shelf", "clean", "stock", "品出し", "清掃")):
        broad.extend(["品出し", "商品補充", "コンビニ品出し"])
    if any(term in retail_blob for term in
           ("randoseru", "first grader", "first graders", "elementary school", "yellow cover",
            "schoolchildren", "schoolchildren")):
        # Search both the rare safety detail and the surrounding commute. The latter is valid
        # context once the edit contains a literal proof shot, and supplies genuinely different
        # moments instead of repeating the one clip where the yellow cover is close to camera.
        broad.extend(["小学生 登校", "小学生 通学路", "ランドセル 通学", "小学生 駅 登校",
                      "登校班 歩く", "Japanese schoolchildren commute"])

    # A small deterministic action expansion. It deliberately stays search-like rather than
    # attaching every modifier to every phrase, which previously produced pages of SEO content.
    bases = chapter_search_queries(chapter)
    fallback = []
    for query in bases[:3]:
        if re.search(r"[\u3040-\u30ff\u3400-\u9fff]", query):
            suffix = (" 実演", " やってみた", " 現場")[(round_index - 1) % 3]
        else:
            suffix = (" POV", " in action", " caught on camera")[(round_index - 1) % 3]
        fallback.append(query + suffix)
    raw = broad + list(raw) + fallback
    out = [q for q in sanitize_queries(raw, limit=V3_CONFIG["retry_queries_per_chapter"] * 2)
           if q.casefold() not in attempted_folded]
    # A retry may change the hypothesis; it may not change the SUBJECT. Measured on the
    # Tokyo-train run: later rounds searched "引っ越し 挨拶" (moving-house greetings) three times
    # and "カフェ デート" / "カップル カフェ" / "Japanese cafe date" once each - six downloads
    # spent on cafes and house moves for a chapter about a train. The words already proven to be
    # on-topic are the ones the planner itself used before it started drifting.
    anchors = _topic_anchors(chapter, attempted)
    kept = [q for q in out if not _query_drifted(q, anchors)]
    if kept:
        out = kept
    return out[:V3_CONFIG["retry_queries_per_chapter"]]


def persistent_search_queries(chapter, attempted, round_index):
    """Create another genuinely different search hypothesis when the model has no new term.

    A timer-based search must not silently become a busy-loop after five query rounds. These
    modifiers mirror phrases real creators use and deliberately rotate action, routine, POV and
    behind-the-scenes discovery. They are only a fallback; the adaptive agent still goes first.
    """
    attempted_folded = {str(query).casefold() for query in (attempted or [])}
    native_suffixes = ("日常", "仕事", "ルーティン", "密着", "vlog", "現場", "裏側")
    english_suffixes = ("day in the life", "at work", "behind the scenes", "POV",
                        "routine", "caught on camera", "customer reaction")
    suffixes = native_suffixes if any(_CJK_RE.search(q) for q in chapter_search_queries(chapter)) \
        else english_suffixes
    offset = max(0, int(round_index) - 1) % len(suffixes)
    candidates = []
    for index, base in enumerate(chapter_search_queries(chapter)):
        suffix_pool = native_suffixes if _CJK_RE.search(base) else english_suffixes
        suffix = suffix_pool[(offset + index) % len(suffix_pool)]
        candidates.append(f"{base} {suffix}")
    return [query for query in sanitize_queries(candidates, limit=5)
            if query.casefold() not in attempted_folded]


def chapter_search_satisfied(chapter, windows):
    """Enough clean, relevant picture to cover the voice without padding or frozen tails.

    This is intentionally an editorial bar, not a demand that every 3.5 seconds receive a new
    source.  A few longer, genuinely relevant shots cut better than a failed run or a sequence
    of arbitrary micro-clips.
    """
    windows = list(windows or [])
    target = max(0.5, chapter.target_seconds)
    per_shot = V3_CONFIG["demo_shot_seconds_max"] if "demonstrat" in chapter.evidence.casefold() \
        else V3_CONFIG["shot_seconds_max"]
    # Account for the small natural slowdown the fitter can use.  The previous calculation
    # required four sources for a 13-second chapter even when three distinct 4-second moments
    # could honestly cover it at 0.76x.
    required_shots = max(1, int(math.ceil(target / (per_shot / V3_CONFIG["slowdown_floor"]))))
    # ...but never fewer than the chapter has BEATS to fill. This check counted SECONDS while the
    # assignment hands out one window per beat, so a 9.2s chapter with four beats was declared
    # covered by three windows and then failed the final audit with an uncovered beat - after all
    # the searching was over. The audit's own duplicate rule is what makes one-window-per-beat a
    # hard fact: the same source range used twice is rejected as a repeat.
    required_shots = max(required_shots, len(chapter.scene_ids or []))
    useful = sum(min(window.duration / V3_CONFIG["slowdown_floor"],
                     per_shot / V3_CONFIG["slowdown_floor"])
                 for window in windows)
    # A chapter needs REAL proof, not one loosely-related shot. Accepting a single "context"
    # window as grounding is how a whole chapter ended up resting on one weak clip: the prompt
    # deliberately encourages context matches for invisible concepts, so they are plentiful and
    # cheap. One exact match proves the chapter; failing that, two context windows from DIFFERENT
    # sources have to agree before the chapter counts as covered.
    exact = [w for w in windows if w.match_class == "exact"]
    context_sources = {w.source_id for w in windows if w.match_class == "context"}
    grounded = bool(exact) or len(context_sources) >= 2
    # SOURCE variety, not just window count. This check used to be satisfied by slicing the few
    # surviving uploads into enough windows, which is how a delivered Short ended up with nine
    # scenes cut from six videos - one TikTok carrying three of them. Worse, "satisfied" ends the
    # search: every chapter of that run reported zero retry rounds while sitting on two or three
    # sources against a minimum of eight, so the thin pool was never given a chance to grow.
    # Asking for a third source before a chapter may stop is what turns that into another round.
    distinct_sources = {w.source_id for w in windows}
    wanted_sources = min(len(chapter.scene_ids or []) or 1, V3_CONFIG["min_distinct_sources"])
    varied = len(distinct_sources) >= wanted_sources
    return (len(windows) >= required_shots and useful + 0.05 >= target
            and grounded and varied)


_WINDOW_TECH_CACHE = {}
_SOURCE_CUT_CACHE = {}


def longest_uncut_subwindow(path, start, end, ffmpeg=None):
    """Keep the strongest continuous portion of a vision-selected source range.

    TikToks often contain a good 2-second action immediately followed by their creator's next
    shot. Rejecting the whole source for that later cut starves the edit; passing it through makes
    the app cut *inside* a source. Preserve the longest clean side instead. ``None`` means the
    selected range was already continuous.
    """
    path = str(path or "")
    start, end = float(start or 0.0), float(end or 0.0)
    if not path or end - start < 0.8:
        return None
    key = (path, round(start, 3), round(end, 3))
    if key in _SOURCE_CUT_CACHE:
        return _SOURCE_CUT_CACHE[key]
    try:
        cuts = clip_scraper.hard_cut_times(
            path, ffmpeg or pipeline.find_ffmpeg(),
            scan_seconds=min(max(end + 0.35, 1.0), 180.0)) or []
        inner = [float(cut) for cut in cuts if start + 0.22 < float(cut) < end - 0.22]
        if not inner:
            result = None
        else:
            edges = [start] + inner + [end]
            parts = [(edges[i], edges[i + 1]) for i in range(len(edges) - 1)]
            best = max(parts, key=lambda part: part[1] - part[0])
            # A fragment under one second is too short to become a readable timeline shot.
            result = (round(best[0], 3), round(best[1], 3)) if best[1] - best[0] >= 1.0 else None
    except Exception:  # noqa: BLE001 - the normal technical check remains the fallback
        result = None
    _SOURCE_CUT_CACHE[key] = result
    return result


def technical_window_issue(path, start, end, ffmpeg=None):
    """Return why an exact source window would look broken, or an empty string when clean."""
    path = str(path or "")
    start, end = float(start or 0.0), float(end or 0.0)
    key = (path, round(start, 3), round(end, 3))
    if key in _WINDOW_TECH_CACHE:
        return _WINDOW_TECH_CACHE[key]
    issue = ""
    if not path or not Path(path).is_file() or end - start < 0.55:
        issue = "repeated_frames"
    else:
        try:
            stall = float(pipeline.repeated_frame_stall_seconds(path, start, end) or 0.0)
            hitches = list(pipeline.micro_stutter_events(path, start, end) or [])
            # A single near-duplicate decode frame occurs routinely in otherwise healthy
            # TikTok footage. Treat it as a quality signal, not an automatic veto. A sustained
            # hold or several cadence faults in the chosen 2-4s edit window is what the viewer
            # actually notices as a lag.
            if stall > V3_CONFIG["max_stall_seconds"] or len(hitches) >= 3:
                issue = "repeated_frames"
        except Exception:  # noqa: BLE001 - failure to measure must not reject healthy footage
            pass
        if not issue:
            try:
                ffmpeg = ffmpeg or pipeline.find_ffmpeg()
                cuts = clip_scraper.hard_cut_times(
                    path, ffmpeg, scan_seconds=min(max(end + 0.35, 1.0), 180.0)) or []
                if any(start + 0.22 < float(cut) < end - 0.22 for cut in cuts):
                    issue = "hidden_source_cut"
            except Exception:  # noqa: BLE001
                pass
        if not issue:
            # A delivered Short went fully black for half a second. The longform path proves its
            # render has pixels before publishing; nothing checked a scraped window, so a fade to
            # black inside the chosen range travelled straight into the edit.
            try:
                ffmpeg = ffmpeg or pipeline.find_ffmpeg()
                probe = subprocess.run(
                    [ffmpeg, "-hide_banner", "-v", "info",
                     "-ss", f"{max(0.0, start):.3f}", "-t", f"{max(0.2, end - start):.3f}",
                     "-i", path, "-vf", "blackdetect=d=0.12:pix_th=0.10:pic_th=0.98",
                     "-an", "-f", "null", os.devnull],
                    capture_output=True, text=True, timeout=90,
                    encoding="utf-8", errors="replace")
                if "black_start" in (probe.stderr or ""):
                    issue = "black_frames"
            # Deliberately NOT `except Exception`. This block first shipped with subprocess
            # unimported; the resulting NameError was swallowed and the check silently did
            # nothing, exactly like the two other missing-name bugs found tonight. Only failures
            # to MEASURE are tolerated here - a broken call has to be heard.
            except (OSError, subprocess.SubprocessError):
                pass
    _WINDOW_TECH_CACHE[key] = issue
    return issue


def tiktok_daily_limited():
    """Is this run carried by the paid API rather than the browser?

    Two ways in. The browser reports TikTok's daily ceiling - or the run was configured to skip
    the browser entirely. The second case was missing and it cost the first API-only run its
    TikTok half: with no session to report a limit this returned False, the chapter kept the
    short 150s browser budget, and an async API call needs more than that to even start.
    """
    if getattr(clip_scraper, "BRIGHTDATA_ONLY", False):
        return True
    try:
        return bool((clip_scraper.tiktok_login.search_stats() or {}).get("daily_limit"))
    except Exception:                                                   # noqa: BLE001
        return False


# An MP4/MOV begins with a 4-byte size and then the ASCII box type; `ftyp` is the very first
# box of any file ffmpeg will open. A container check costs a 12-byte read, where the duration
# probe that used to catch this costs a subprocess and ran far too late to matter.
_VIDEO_MAGIC = (b"ftyp", b"moov", b"mdat", b"free", b"skip", b"wide")


def _is_playable_video(path, ffprobe=None):
    """Cheap "is this actually a video file" check, done the moment it lands.

    Deliberately shallow: it must be fast enough to run on every download. Anything that passes
    here is still probed properly before it is used - this only catches the case where the CDN
    handed back something that is not a video at all.
    """
    try:
        with open(path, "rb") as handle:
            head = handle.read(32)
    except OSError:
        return False
    if len(head) < 12:
        return False
    if any(magic in head[:16] for magic in _VIDEO_MAGIC):
        return True
    # A few containers do not start with an MP4 box. Recognise them rather than
    # reject them. Written as byte values so no escape can be mangled on the way in.
    return head[:4] in (bytes([0x1A, 0x45, 0xDF, 0xA3]),      # Matroska / WebM
                        b"RIFF", b"OggS",
                        bytes([0x46, 0x4C, 0x56, 0x01]),      # FLV
                        bytes([0x00, 0x00, 0x01, 0xBA]))      # MPEG program stream


def gather_chapter_sources(chapter, platforms, project_dir, seen_source_ids,
                           cancel_check=None, deadline=None, status_cb=None, state=None):
    """Build a deliberately diverse first-look pool for one visual chapter.

    Ranking metadata is not looking at footage. The pool is downloaded so the vision pass can
    judge what is on screen, which is the only thing that decides whether a shot is usable.
    """
    # Split the clock BEFORE the first query: searching may not spend the fetch reserve.
    search_deadline = (None if deadline is None
                       else time.monotonic() + max(0.0, deadline - time.monotonic()) * SEARCH_SHARE)
    queries = chapter_search_queries(chapter)
    want_lo = V3_CONFIG["min_sources_per_chapter"]
    want_hi = V3_CONFIG["max_sources_per_chapter"]
    proxies = Path(project_dir) / "seedance 2.0" / "_v3_proxies"
    proxies.mkdir(parents=True, exist_ok=True)
    manifest_path = proxies / "_v3_candidate_manifest.json"
    try:
        cache_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(cache_manifest, dict):
            cache_manifest = {}
    except Exception:  # noqa: BLE001 - old projects have no provenance manifest
        cache_manifest = {}

    # A failed V3 run may already have a valuable local pool.  The old code left it on disk but
    # never inspected it again, so Continue project paid for another scrape and repeated the
    # same narrow rejection logic. Re-grade it under the current chapter/query strategy first.
    downloaded = []
    # Recovery is useful on Continue project, but it cannot be allowed to fill the entire pool.
    # The old behaviour re-graded the fourteen newest files from *all* earlier chapter searches,
    # then skipped fresh discovery entirely.  That is how an old generic store clip could become
    # the candidate pool for a train-platform chapter.
    recovery_cap = max(2, min(4, want_hi // 3))
    chapter_key = re.sub(r"\s+", " ", str(chapter.title or "").casefold()).strip()
    for cached in sorted(proxies.glob("v3_*.mp4"), key=lambda path: path.stat().st_mtime,
                         reverse=True):
        if len(downloaded) >= recovery_cap:
            break
        if cached.stat().st_size < 4096:
            continue
        # A filename only records platform + id, not why the clip was found.  Never recover
        # legacy anonymous candidates: the last run proved that they fill every chapter with the
        # same irrelevant twenty sources. New cache files carry an explicit chapter key and can
        # only ever return to that exact visual chapter on Continue project.
        provenance = cache_manifest.get(cached.name) or {}
        if str(provenance.get("chapter_key") or "") != chapter_key:
            continue
        match = re.match(r"^v3_([^_]+)_(.+)$", cached.stem)
        if not match:
            continue
        platform, source_id = match.group(1), match.group(2)
        if not source_id or source_id in seen_source_ids:
            continue
        seen_source_ids.add(source_id)
        downloaded.append({"source_id": source_id, "platform": platform, "path": str(cached),
                           "query": "saved candidate recovery", "item": {}})
    if downloaded:
        _log(status_cb, "Scrape V3 chapter %d: re-checking %d previously downloaded candidate(s) "
                        "before searching for new footage."
             % (chapter.chapter_id, len(downloaded)))

    raw_total, query_buckets = 0, []
    queued_ids = set()
    buckets_by_query = {}
    skipped_out_of_time = 0

    def collect(query, sort_mode):
        """Collect a small, query-labelled result bucket without downloading it yet."""
        nonlocal raw_total, skipped_out_of_time
        bucket = buckets_by_query.setdefault(query, [])
        if cancel_check and cancel_check():
            skipped_out_of_time += 1
            return
        # The deadline gates NETWORK work. Reading the batched job's results costs nothing - and
        # the job itself routinely spends most of the search clock, so gating the read on the
        # same clock threw the results away: one run received 50 paid records in a single job
        # and the very next line reported "0 raw results, 16 searches never ran".
        if search_deadline and time.monotonic() >= search_deadline and api_results is None:
            # Returning silently made an exhausted time budget look identical to a chapter the
            # platforms had nothing for: the report said "0 raw results, nothing usable was
            # found" and sent you hunting for a scraping fault that was never there.
            skipped_out_of_time += 1
            return
        try:
            # A real callback also prevents the login worker from falling back to a legacy
            # Windows console print. That print used to crash every native Japanese TikTok
            # query with UnicodeEncodeError before the browser result could be returned.
            if api_results is not None:
                # The batched job already paid for this keyword; asking again would bill twice
                # and wait another minute for the same answer. Sort variants add nothing here -
                # the API has no server-side ordering.
                if sort_mode != "RELEVANCE":
                    return
                items = list(api_results.get(query) or [])
                # The pooled-reel supplement is a lookup after the pool's first fetch, but that
                # first fetch IS network - so it alone keeps honouring the clock.
                if ("instagram" in {str(p).lower() for p in (platforms or ())}
                        and not (search_deadline and time.monotonic() >= search_deadline)):
                    items += clip_scraper.backend_search(
                        query, want_hi, status_cb=(status_cb or (lambda _m: None)),
                        sort="RELEVANCE", platforms={"instagram"},
                        deadline=search_deadline) or []
            else:
                items = clip_scraper.backend_search(query, want_hi,
                                                    status_cb=(status_cb or (lambda _msg: None)),
                                                    sort=sort_mode, platforms=platforms,
                                                    deadline=search_deadline) or []
        except Exception as exc:      # noqa: BLE001 - one dead sort must not end the chapter
            _log(status_cb, f"Scrape V3: search '{query}' [{sort_mode}] failed "
                            f"({type(exc).__name__}).")
            return
        raw_total += len(items)
        added_this_sort = 0
        # A result from each query is worth more than several ranking variants of the first one.
        per_sort_cap = max(1, int(math.ceil(want_hi / len(V3_SORT_MODES))))
        # Prefer native UGC platforms before Instagram/X advertising when relevance is tied.
        # This is stable, so each platform's own popularity/relevance order remains intact.
        platform_rank = {"tiktok": 0, "instagram": 1}
        items = sorted(items, key=lambda item: platform_rank.get(
            str(item.get("_platform") or item.get("platform") or "tiktok").lower(), 3))
        for item in items:
            source_id = str(item.get("id") or item.get("videoId") or "")
            if not source_id or source_id in seen_source_ids or source_id in queued_ids:
                continue
            # Do not spend a download/vision slot on an explicitly labelled game result when
            # the script is real-world. Search engines can inject popular Fortnite clips even
            # under a good query; metadata is sufficient for this narrow negative guard.
            if not _chapter_allows_games(chapter):
                meta = clip_scraper._item_meta(item)
                metadata_blob = " ".join(str(meta.get(key) or "") for key in
                                         ("caption", "title", "author", "author_name"))
                if _GAME_TERMS.search(metadata_blob):
                    continue
            # Reserve it within this discovery pass, but do NOT mark it globally seen yet.
            # TikTok often rejects a first yt-dlp request and succeeds after the session/cookie
            # refresh on a later query. Marking before download permanently discarded those
            # good clips; the manual editor succeeded precisely because it retried them.
            queued_ids.add(source_id)
            bucket.append((query, item))
            added_this_sort += 1
            if added_this_sort >= per_sort_cap:
                break

    # In API-only mode the whole chapter goes to Bright Data as ONE batched job: a discover job
    # spends 60-110s queued regardless of keyword count, so eight serial calls spent the entire
    # chapter clock on two queries. `collect` then reads each keyword's bucket from this map
    # instead of issuing its own call.
    api_results = None
    if getattr(clip_scraper, "BRIGHTDATA_ONLY", False):
        try:
            budget_left = (None if search_deadline is None
                           else max(60.0, search_deadline - time.monotonic()))
            # A chapter may spend only its share of the record budget. Measured on the sushi
            # run: chapter 0 took 70 of 60 records and chapters 1-2 searched with nothing left -
            # 2 and 1 raw results against its 60. Same fairness rule the time budget follows.
            chapters_left = max(1, int(getattr(chapter, "chapters_remaining", 1) or 1))
            share = max(10, (clip_scraper.brightdata_tiktok.BRIGHTDATA_RECORD_BUDGET
                             - clip_scraper.brightdata_tiktok.records_used()) // chapters_left)
            api_results = clip_scraper.brightdata_tiktok.search_many(
                queries, per_query=max(4, min(10, want_hi)),
                status_cb=status_cb, timeout_s=budget_left or 420,
                max_records=share)
        except Exception as exc:      # noqa: BLE001 - fall back to per-query calls
            _log(status_cb, f"Scrape V3: batched keyword job failed "
                            f"({type(exc).__name__}); falling back to single calls.")
            api_results = None

    # First pass is breadth: run every independent visual hypothesis once.  The former nested
    # loop searched all four sorts for query 1, 2 and 3 and then stopped, so the precise ideas
    # at positions 4–8 literally never reached TikTok on a healthy result page.
    for query in queries:
        collect(query, "RELEVANCE")

    # Only widen the result ordering if broad first-pass discovery genuinely produced too little
    # choice.  This retains popularity/recent fallbacks without multiplying browser work for
    # already healthy chapters.
    if sum(len(rows) for rows in buckets_by_query.values()) < want_hi * 2:
        for sort_mode in tuple(mode for mode in V3_SORT_MODES if mode != "RELEVANCE"):
            for query in queries:
                if (cancel_check and cancel_check()) or (
                        search_deadline and time.monotonic() >= search_deadline):
                    break
                collect(query, sort_mode)
            if sum(len(rows) for rows in buckets_by_query.values()) >= want_hi * 2:
                break

    # Keep the query order explicit for round-robin selection, including queries with zero hits.
    query_buckets = [buckets_by_query.get(query, []) for query in queries]

    # Round-robin the query pools so download slots represent different visual hypotheses and
    # different result orderings instead of the first query monopolising the chapter.
    candidates = []
    for offset in range(max((len(rows) for rows in query_buckets), default=0)):
        for bucket in query_buckets:
            if offset < len(bucket):
                candidates.append(bucket[offset])

    # Native UGC before Instagram/X advertising, across the WHOLE pool. The same preference is
    # applied inside each search call, but that only held the guarantee while a chapter had a
    # single query: with two, the round-robin above interleaved the buckets and an Instagram ad
    # entered the download pool ahead of TikTok footage that had already been found. Measured
    # 2026-08-30 while adding a second query per chapter: ['tiktok','instagram','tiktok',...]
    # where one query gave four TikToks first.
    #
    # Stable, so the round-robin's query rotation survives inside each platform.
    _platform_rank = {"tiktok": 0, "instagram": 1}
    candidates.sort(key=lambda pair: _platform_rank.get(
        str((pair[1] or {}).get("_platform") or (pair[1] or {}).get("platform")
            or "tiktok").lower(), 3))

    # The deadline guards SEARCHING, not fetching what searching already paid for. A chapter that
    # found ten candidates and downloaded none spent its entire budget for nothing - and when the
    # candidates came from the Bright Data fallback it spent real money for nothing too. Once the
    # clock is out, keep fetching until SOMETHING is in hand, bounded so it cannot run away.
    download_grace = None if deadline is None else deadline + DOWNLOAD_GRACE_S
    for query, item in candidates:
        if len(downloaded) >= want_hi:
            break
        if cancel_check and cancel_check():
            break
        if deadline and time.monotonic() >= deadline:
            if downloaded or (download_grace and time.monotonic() >= download_grace):
                break
        source_id = str(item.get("id") or item.get("videoId") or "")
        # Backend items use ``_platform``. Reading only ``platform`` labelled every Instagram/X
        # result as TikTok, selected the wrong downloader/session and made the run report useless.
        platform = str(item.get("_platform") or item.get("platform") or "tiktok")
        dest = proxies / f"v3_{platform}_{re.sub(r'[^A-Za-z0-9]+', '_', source_id)[:24]}.mp4"
        try:
            got = scrape_v2.download_proxy_v2(item, dest, None)
        except Exception:      # noqa: BLE001 - a dead link is one candidate, not a failure
            got = None
        if not got or not Path(dest).is_file():
            continue
        # Existing and non-empty is not the same as playable. Measured on a delivered run: 9 of
        # 70 "successful" downloads were multi-megabyte files with no MP4 header at all - random
        # bytes where `ftyp` belongs - and every one of them survived as a source, occupied a
        # slot in the chapter's pool, and was only unmasked much later by a duration probe that
        # reported them as "unreadable or empty". By then the record was paid for and the
        # chapter had stopped looking. Catch it here, where the slot can still be given to the
        # next candidate.
        if not _is_playable_video(dest):
            _log(status_cb, "Scrape V3 chapter %d: %s downloaded %d KB that is not a video; "
                            "discarding it and taking the next candidate."
                 % (chapter.chapter_id, source_id, Path(dest).stat().st_size // 1024))
            try:
                Path(dest).unlink()
            except OSError:
                pass
            continue
        seen_source_ids.add(source_id)
        downloaded.append({"source_id": source_id, "platform": platform, "path": str(dest),
                           "query": query, "item": item})
        cache_manifest[dest.name] = {
            "chapter_key": chapter_key,
            "query": query,
            "saved_at": int(time.time()),
        }

    if cache_manifest:
        try:
            manifest_path.write_text(json.dumps(cache_manifest, ensure_ascii=False, indent=2),
                                     encoding="utf-8")
        except OSError:
            pass

    if state is not None:
        info = state.setdefault("chapters", {}).setdefault(str(chapter.chapter_id), {
            "title": chapter.title, "queries": [], "raw_results": 0,
            "downloaded": 0, "rejected": [], "selected": [], "rounds": [],
        })
        info["queries"] = sanitize_queries(list(info.get("queries") or []) + queries, limit=100)
        info["raw_results"] = int(info.get("raw_results") or 0) + raw_total
        info["downloaded"] = int(info.get("downloaded") or 0) + len(downloaded)
        info.setdefault("rounds", []).append({"queries": queries, "raw_results": raw_total,
                                               "downloaded": len(downloaded)})
    if skipped_out_of_time:
        info["skipped_out_of_time"] = int(info.get("skipped_out_of_time") or 0) + skipped_out_of_time
    _log(status_cb, "Scrape V3 chapter %d (%s): %d quer%s, %d raw result(s), %d source(s) "
                    "downloaded for inspection.%s"
         % (chapter.chapter_id, chapter.title, len(queries),
            "y" if len(queries) == 1 else "ies", raw_total, len(downloaded),
            (" %d search(es) never ran - the scrape time budget was already spent."
             % skipped_out_of_time) if skipped_out_of_time else ""))
    # Results found but not fetched is the most expensive outcome there is, and the old summary
    # reported it as a flat "0 source(s)" that read like a scraping fault. Name it.
    if raw_total and not downloaded:
        _log(status_cb, "Scrape V3 chapter %d: %d result(s) were found but none could be fetched "
                        "before the clock ran out - the searches were paid for and discarded."
             % (chapter.chapter_id, raw_total))
    if len(downloaded) < want_lo:
        _log(status_cb, "Scrape V3 chapter %d: only %d source(s) - below the %d this chapter "
                        "wants; it will be cut from what is here."
             % (chapter.chapter_id, len(downloaded), want_lo))
    return downloaded


# --------------------------------------------------------------------------- window selection
def rejection_reason(description, chapter):
    """The one concrete reason this source cannot be used, or "" when it can.

    Deliberately short. Everything absent from V3_REJECTIONS - modest text, low likes, a partial
    metadata match, an inability to prove every word - is not grounds for rejection.
    """
    desc = description or {}
    if not bool(desc.get("usable", True)):
        return "no_visible_action"
    if bool(desc.get("slideshow")) or bool(desc.get("static_image")):
        return "slideshow_static"
    if desc.get("native_9_16") is False:
        return "not_vertical"
    if bool(desc.get("repeated_frames")) or bool(desc.get("broken")):
        return "repeated_frames"
    if bool(desc.get("captions_cover_subject")):
        return "captions_cover_subject"
    if bool(desc.get("caption_on_solid_box")):
        # The remover takes the LETTERS off and leaves the plate behind, which looks worse than
        # the caption did. Vision decides this: a morphological test for "uniform area behind the
        # text" was calibrated on real clips and came out inverted - a table seam scored best and
        # the one genuine plate scored worst.
        return "caption_on_solid_box"
    if bool(desc.get("wrong_country")):
        return "wrong_country"
    if str(desc.get("match_class") or "").strip().lower() == "unrelated":
        return "unrelated_filler"
    for term in (chapter.forbidden or []):
        if term and term.casefold() in str(desc.get("summary") or "").casefold():
            return "unrelated_filler"
    return ""


def normalize_match_class(value):
    text = str(value or "").strip().lower().replace(" ", "_")
    if text in MATCH_CLASSES:
        return text
    if text in ("proxy", "related", "editorial"):
        return "editorial_proxy"
    if text in ("exact_match", "literal"):
        return "exact"
    return "context"


def _chapter_requires_train_women_proof(chapter):
    """Whether this is a women-only train-car chapter with a non-negotiable visual anchor."""
    brief = " ".join((chapter.title, chapter.subject, chapter.action, chapter.evidence)).casefold()
    return ("women-only" in brief or "women only" in brief or "女性専用" in brief) and any(
        token in brief for token in ("train", "carriage", "train car", "commuter", "電車", "車両"))


def _window_semantically_safe(chapter, window):
    """Reject a vision label that contradicts the visible subject.

    The model is deliberately permissive about minor imperfections, but it may not turn a child
    at a crossing or an indoor vlog into a women-only train car merely because the search query
    was relevant.  This is a narrow defence for the high-impact claims that need literal proof;
    it is not a metadata relevance filter.
    """
    visible = str(window.reason or "").casefold()
    if not visible:
        # Older reports and unit-level callers do not include a reason.  Keep generic grounded
        # classes compatible; a women-only train claim is too specific to trust without proof.
        return not _chapter_requires_train_women_proof(chapter)
    # A grader occasionally explains why the material *doesn't* fit and still emits a proxy.
    denial = re.compile(r"\b(?:no|not|without|lacks?|missing|doesn['’]t|does not)\b.{0,70}"
                        r"(?:women|female|女性|train|carriage|commuter|車両|電車)", re.IGNORECASE)
    if denial.search(visible):
        return False
    if _chapter_requires_train_women_proof(chapter):
        women = bool(re.search(r"women|woman|female|女性", visible, re.IGNORECASE))
        rail = bool(re.search(r"train|carriage|commuter|platform|rail|車両|電車|ホーム", visible,
                              re.IGNORECASE))
        return women and rail
    return True


def _expand_long_source_window(window, chapter):
    """Return one honest candidate for one source.

    Older V3 expanded a ten-second region into as many as three timeline shots. That made the
    coverage counter green while the viewer watched the same speaker, shopper or employee again
    and again. Keep the grader's strongest region intact; selection may use this source once.
    """
    return [window]


def _wkey(window):
    """Identify one offered window so a skip reason can be matched back to it."""
    return "%s@%.2f-%.2f" % (window.source_id, window.start, window.end)


def select_chapter_windows(chapter, graded, status_cb=None, state=None):
    """Pick the sequence for one chapter, applying the reuse rules.

    ``graded`` is a list of {source_id, platform, path, windows:[{start,end,match_class,reason}]}
    already judged by the vision pass. Ordering is by match class first and then by the grader's
    own confidence, because an exact shot is worth more than a confident proxy - but a proxy is
    worth far more than an empty chapter.
    """
    rank = {"exact": 0, "context": 1, "editorial_proxy": 2}
    gap = V3_CONFIG["min_window_gap_seconds"]
    per_source = V3_CONFIG["max_windows_per_source"] if chapter.allow_multi_window else 1

    flat = []
    for source in (graded or []):
        for window in (source.get("windows") or []):
            candidate = ShotWindow(
                chapter_id=chapter.chapter_id,
                source_id=str(source.get("source_id") or ""),
                platform=str(source.get("platform") or ""),
                path=str(source.get("path") or ""),
                start=float(window.get("start") or 0.0),
                end=float(window.get("end") or 0.0),
                match_class=normalize_match_class(window.get("match_class")),
                reason=str(window.get("reason") or "").strip()[:180],
                visual_signature=str(window.get("visual_signature") or "").strip()[:120],
                visual_interest=_interest_of(window),
                query=str(source.get("query") or ""),
                source_has_captions=bool(source.get("source_has_captions")),
            )
            if not _window_semantically_safe(chapter, candidate):
                continue
            flat.extend(_expand_long_source_window(candidate, chapter))
    flat = [w for w in flat if w.duration >= 0.6]
    if not _chapter_allows_games(chapter):
        # Defence in depth for a grader that ignored the explicit subject rule. The broken live
        # run literally justified windows with "Fortnite gameplay" while the chapter concerned
        # Japanese dating; such a reason can never be normalized into contextual footage.
        flat = [w for w in flat if not _GAME_TERMS.search(" ".join((w.reason, w.query)))]
    # Interest beats length. The old order was (match_class, -duration), and that tiebreaker
    # actively rewarded dead footage: a locked-off, uneventful shot yields ONE long usable
    # window, while a lively clip is cut into several short ones - so the boring take won every
    # tie. Reported as "die meisten sind low quality clips und uninteressant" (2026-08-30).
    #
    # Interest is banded rather than used raw, so a 7.4 does not beat a 7.0 that is a second
    # longer; within a band the longer window still wins, because a longer window needs fewer
    # cuts to cover a beat.
    flat.sort(key=lambda w: (rank.get(w.match_class, 3),
                             -_interest_band(w.visual_interest),
                             -float(w.duration)))

    # A proxy is never allowed to become the sole visual proof of a chapter.  It can only add
    # variety after at least one grounded exact/context shot is already selected.
    # Measuring is cheap ONLY because it runs on the range that would actually ship, and only on
    # windows that are about to be chosen - not on every downloaded candidate. Asking a vision
    # model "can this caption be removed?" produced captions on solid plates and full-height
    # vertical text in delivered Shorts; the remover's own measurement does not guess.
    coverage_cache = {}
    try:
        import caption_remover as _cr
        CAPTION_CEILING = float(_cr.MAX_COVERAGE)
    except Exception:                                                   # noqa: BLE001
        CAPTION_CEILING = 0.08

    def caption_too_heavy(window):
        key = (str(window.path), round(window.start, 2), round(window.end, 2))
        if key not in coverage_cache:
            try:
                import caption_remover
                ffmpeg = pipeline.find_ffmpeg()
                span = max(0.8, min(2.0, float(window.duration)))
                value = caption_remover.caption_coverage(
                    window.path, ffmpeg, seconds=span, start=float(window.start))
                ceiling = float(caption_remover.MAX_COVERAGE)
            except Exception:                                           # noqa: BLE001
                value, ceiling = -1.0, 1.0
            # -1 means it could not be measured; that is not evidence of a problem.
            coverage_cache[key] = (value > ceiling, value)
        return coverage_cache[key]

    def pick(across_sources, allow_captioned=False, ignore_global_budget=False):
        """Choose windows. `across_sources` also refuses a picture already used by a DIFFERENT
        post - three uploads of the same departure board are three sources but one shot as far
        as a viewer is concerned, and a delivered Short opened on exactly that.

        `allow_captioned` lets through footage the remover cannot fully clean. Japanese TikTok is
        saturated with burned-in text, so refusing all of it outright emptied whole chapters and
        failed runs that had perfectly watchable material - a caption is worse than clean footage
        and better than no footage."""
        chosen, used_per_source, used_visuals = [], {}, {}
        skipped = {}
        # Carried ACROSS chapters. A chapter cannot see what the previous ones already spent,
        # so without this the same upload supplies a third of the finished Short.
        spent_globally = (state if isinstance(state, dict) else {}).setdefault("source_spend", {})
        for window in flat:
            if window.match_class == "editorial_proxy" and not any(
                    item.match_class in ("exact", "context") for item in chosen):
                skipped[_wkey(window)] = "a proxy may not be the chapter's only grounding"
                continue
            count = used_per_source.get(window.source_id, 0)
            if count >= per_source:
                skipped[_wkey(window)] = ("this chapter already uses %d shot(s) from this upload"
                                          % count)
                continue
            # Whole-Short budget, relaxed only in the last-resort pass: a repeat is bad, an
            # uncovered beat is worse, so this must be the last thing given up, not the first.
            if not ignore_global_budget and (
                    spent_globally.get(window.source_id, 0) + count
                    >= V3_CONFIG["max_windows_per_source_total"]):
                skipped[_wkey(window)] = ("this upload already carries %d shot(s) elsewhere in "
                                          "the Short" % spent_globally.get(window.source_id, 0))
                continue
            if any(other.source_id == window.source_id and other.overlaps(window, gap)
                   for other in chosen):
                skipped[_wkey(window)] = "overlaps a range already taken from this upload"
                continue
            # The same post may contain a cashier, a restocking shot and a shopper reaction. It
            # may not contribute three shots of the same ceiling speaker merely because their
            # timestamps differ. The vision reason is the visible-content label for this guard.
            visual_key = visual_identity(window)
            scope = "*" if across_sources else window.source_id
            if visual_key and visual_key in used_visuals.get(scope, set()):
                skipped[_wkey(window)] = ("the same picture is already on screen: %s"
                                          % (visual_key[:70] or "identical shot"))
                continue
            heavy, share = caption_too_heavy(window)
            # Past twice the ceiling the picture IS the text - no amount of need makes that
            # usable, so it is dropped in both passes.
            if share > 2 * CAPTION_CEILING:
                skipped[_wkey(window)] = ("creator text covers %.0f%% of the frame" % (share * 100))
                continue
            # Restricting the caption compromise to exact matches was measured and reverted:
            # the three topics attempted under that rule ALL failed, against two deliveries in
            # the two runs immediately before it. In this corpus most usable footage is a context
            # match carrying some text, so the rule sealed off nearly everything.
            #
            # The bad clip that motivated it - a yukata news card used for umbrella lockers - was
            # wrong on RELEVANCE, not on caption load. Relevance is defended by the chapter
            # grounding rule above; pulling on the caption lever to solve it only starved the
            # editor.
            if heavy and not allow_captioned:
                skipped[_wkey(window)] = ("carries creator text the remover cannot fully lift "
                                          "(%.0f%% of the frame)" % (share * 100))
                continue
            chosen.append(window)
            used_per_source[window.source_id] = count + 1
            if visual_key:
                used_visuals.setdefault(scope, set()).add(visual_key)
        return chosen, skipped

    # Variety first. Falling back to per-source uniqueness only when the strict pass cannot fill
    # the chapter keeps a repeat preferable to a hole - the audit refuses an uncovered beat.
    # Best first, then give ground one constraint at a time: clean pictures that never repeat,
    # then repeats, then footage carrying text the remover cannot fully lift. Each step is only
    # taken when the chapter still has beats with nothing in them.
    needed = len(chapter.scene_ids or [])
    chosen, skipped = pick(across_sources=True)
    # (across_sources, allow_captioned, ignore_global_budget) - the whole-Short source budget is
    # the LAST thing surrendered, after picture variety and after caption tolerance.
    for across, captioned_ok, spend_budget in ((False, False, False), (True, True, False),
                                               (False, True, False), (False, True, True)):
        if len(chosen) >= needed:
            break
        candidate, candidate_skips = pick(across_sources=across, allow_captioned=captioned_ok,
                                          ignore_global_budget=spend_budget)
        if len(candidate) > len(chosen):
            chosen, skipped = candidate, candidate_skips
    captioned = sum(1 for w in chosen if caption_too_heavy(w)[0])
    if captioned:
        _log(status_cb, "Scrape V3 chapter %d: %d of %d shot(s) still carry creator text the "
                        "remover cannot fully lift - nothing cleaner was found."
             % (chapter.chapter_id, captioned, len(chosen)))

    # Remember what this chapter spent, so the NEXT chapter's budget check can see it. Without
    # this the cap above is per chapter again and the same upload carries a third of the Short.
    if isinstance(state, dict):
        spend = state.setdefault("source_spend", {})
        # Only what this chapter will actually SHOW. `chosen` deliberately over-collects so the
        # fitter has options; charging the budget for all of it emptied every source's allowance
        # in the first chapter and made the cap useless from chapter two onwards (measured: with
        # twelve usable sources the most-used one still appeared eight times).
        for window in chosen[:max(1, needed)]:
            spend[window.source_id] = spend.get(window.source_id, 0) + 1
    if state is not None:
        row = state.setdefault("chapters", {}).setdefault(str(chapter.chapter_id), {})
        # Why an offered window did NOT make the cut. Without it the report can say what the
        # vision found and what ended up on screen, but not what happened in between.
        row["windows_passed_over"] = [
            {"source_id": w.source_id, "window": [w.start, w.end],
             "match_class": w.match_class, "saw": w.reason,
             "passed_over_because": skipped.get(_wkey(w), "")}
            for w in flat if _wkey(w) in skipped]
        row["selected"] = [{"source_id": w.source_id, "start": w.start, "end": w.end,
                            "match_class": w.match_class, "reason": w.reason,
                            "visual_signature": w.visual_signature} for w in chosen]
    if chosen:
        counts = {name: sum(1 for w in chosen if w.match_class == name) for name in MATCH_CLASSES}
        _log(status_cb, "Scrape V3 chapter %d (%s): %d source(s) inspected, %d exact / %d "
                        "contextual / %d proxy option(s); %d window(s) selected."
             % (chapter.chapter_id, chapter.title, len(graded or []),
                sum(1 for w in flat if w.match_class == "exact"),
                sum(1 for w in flat if w.match_class == "context"),
                sum(1 for w in flat if w.match_class == "editorial_proxy"),
                len(chosen)) + (" (%s)" % ", ".join(f"{k}:{v}" for k, v in counts.items() if v)))
    else:
        _log(status_cb, "Scrape V3 chapter %d (%s): nothing usable was found."
             % (chapter.chapter_id, chapter.title))
    return chosen


# --------------------------------------------------------------------------- cut to the voice
def plan_shot_lengths(total_seconds, count, is_demo=False):
    """Split a chapter's voice span across its windows, keeping shots readable.

    Shots stay in the 2.0-3.5s band wherever the arithmetic allows: shorter and the viewer cannot
    read the picture, longer and a static-ish shot outstays its welcome. A demonstration may run
    longer because the point of it is watching something happen.
    """
    count = max(1, int(count))
    total = max(0.1, float(total_seconds))
    lo = V3_CONFIG["shot_seconds_min"]
    hi = V3_CONFIG["demo_shot_seconds_max"] if is_demo else V3_CONFIG["shot_seconds_max"]
    even = total / count
    if even < lo:
        # Too many windows for the time: use fewer, each long enough to read.
        count = max(1, int(total // lo)) or 1
        even = total / count
    lengths = [min(hi, even)] * count
    spare = total - sum(lengths)
    index = 0
    while spare > 0.001 and lengths:
        lengths[index % len(lengths)] += spare / len(lengths)
        spare = total - sum(lengths)
        index += 1
        if index > len(lengths) * 3:
            break
    lengths[-1] += max(0.0, total - sum(lengths))
    return [round(x, 3) for x in lengths]


def fit_window(window, seconds):
    """Trim or gently slow ONE window to fill ``seconds`` - never freeze it.

    A frame held to fill time is the single most obvious sign of a machine-made edit. When a
    window is a little short a slowdown to 0.88x is invisible; below that the answer is a
    different window, so the caller is told the fit failed.
    """
    available = window.duration
    if available <= 0.05 or seconds <= 0.0:
        return None
    if available >= seconds:
        return {"start": window.start, "end": round(window.start + seconds, 3), "speed": 1.0}
    rate = available / seconds
    if rate >= V3_CONFIG["slowdown_floor"]:
        return {"start": window.start, "end": window.end, "speed": round(rate, 4)}
    return None


def _voice_template(voice_scenes, start, end):
    """The narration a shot sits over: the line it overlaps most, plus every line it touches.

    A V3 scene is a SHOT, not a sentence, but everything downstream still reads a scene the way
    V2 built one - plan_config indexes scene['script'] directly, and the first V3 run died on
    exactly that. Copying the underlying voice line rather than inventing a fresh dict means every
    field the rest of the pipeline expects is present, whatever it happens to be.
    """
    if not voice_scenes:
        return {}, ""
    best, best_overlap = None, -1.0
    texts = []
    for scene in voice_scenes:
        try:
            s0, s1 = float(scene.get("start", 0) or 0), float(scene.get("end", 0) or 0)
        except (TypeError, ValueError):
            continue
        overlap = min(end, s1) - max(start, s0)
        if overlap > best_overlap:
            best, best_overlap = scene, overlap
        if overlap > 0.05:
            # `script` may be an older editor display string that already contains words from
            # the next beat. `exact_voice_text` is the timed speech truth and prevents those
            # overlapping tails from being concatenated twice in the rebuilt timeline.
            line = _scene_voice_text(scene)
            if line and line not in texts:
                texts.append(line)
    template = dict(best or voice_scenes[0])
    if not texts:
        fallback = _scene_voice_text(template)
        texts = [fallback] if fallback else []
    return template, " ".join(texts)


def fit_chapter_sequence(windows, span, is_demo=False):
    """Choose a subset whose real source duration tiles the whole chapter without padding."""
    windows = list(windows or [])
    if not windows:
        return []
    per_shot = V3_CONFIG["demo_shot_seconds_max"] if is_demo else V3_CONFIG["shot_seconds_max"]
    ideal = min(len(windows), max(1, int(math.ceil(float(span) / per_shot))))
    # More shots make each required source window shorter. Start editorially calm, then add a shot
    # only when that is what makes full real-motion coverage possible.
    for count in range(ideal, len(windows) + 1):
        lengths = plan_shot_lengths(span, count, is_demo=is_demo)
        remaining = list(windows)
        fitted = []
        for seconds in lengths:
            picked = None
            for index, window in enumerate(remaining):
                fit = fit_window(window, seconds)
                if fit is not None:
                    picked = (index, window, fit, seconds)
                    break
            if picked is None:
                fitted = []
                break
            index, window, fit, seconds = picked
            remaining.pop(index)
            fitted.append((window, fit, seconds))
        if fitted and abs(sum(row[2] for row in fitted) - float(span)) < 0.08:
            return fitted
    return []


def fit_partial_chapter_sequence(windows, span, is_demo=False):
    """Use every honest second we found even when it cannot fill the whole chapter.

    Previously a 3.3-second relevant clip for a 6.1-second chapter became *zero* assigned clips:
    the full-span fitter failed and discarded it. Partial coverage is still useful editing work;
    the uncovered remainder stays explicit and editable instead of erasing the good shot.
    """
    remaining = max(0.0, float(span))
    hi = V3_CONFIG["demo_shot_seconds_max"] if is_demo else V3_CONFIG["shot_seconds_max"]
    fitted = []
    for window in (windows or []):
        if remaining < 0.55:
            break
        # This is partial coverage, so there is no reason to slow the footage merely to steal a
        # few tenths from the explicit replacement slot. Preserve the source cadence here.
        seconds = min(remaining, hi, window.duration)
        if seconds < 0.55:
            continue
        fit = fit_window(window, seconds)
        if fit is None:
            continue
        fitted.append((window, fit, seconds))
        remaining -= seconds
    return fitted


def uncovered_voice_beats(chapter, voice_scenes, start, end, id_offset=0):
    """Represent missing footage as short original voice beats, never one giant empty chapter."""
    start, end = float(start), float(end)
    if end - start <= 0.01:
        return []
    boundaries = {start, end}
    for scene in (voice_scenes or []):
        try:
            s0, s1 = float(scene.get("start", 0) or 0), float(scene.get("end", 0) or 0)
        except (TypeError, ValueError):
            continue
        if start + 0.01 < s0 < end - 0.01:
            boundaries.add(s0)
        if start + 0.01 < s1 < end - 0.01:
            boundaries.add(s1)
    # A source voice scene can itself be long. Split it so the editor never receives a 6-13s
    # empty tile that suggests one equally long visual should be found.
    ordered = sorted(boundaries)
    # Speech re-timing and chapter planning can disagree by a few frames (the failed project had
    # 6.970 vs 7.224). Keeping both boundaries creates a useless 254ms timeline tile. Drop any
    # interior boundary that would leave a sub-second fragment at either side.
    min_empty = 1.0
    filtered = [ordered[0]]
    for target in ordered[1:-1]:
        if target - filtered[-1] < min_empty or ordered[-1] - target < min_empty:
            continue
        filtered.append(target)
    filtered.append(ordered[-1])
    capped = [filtered[0]]
    max_empty = V3_CONFIG["shot_seconds_max"]
    for target in filtered[1:]:
        span = target - capped[-1]
        pieces = max(1, int(math.ceil(span / max_empty)))
        step = span / pieces
        for _ in range(1, pieces):
            capped.append(round(capped[-1] + step, 3))
        capped.append(target)
    rows = []
    for part, (left, right) in enumerate(zip(capped, capped[1:])):
        if right - left < 0.05:
            continue
        base, text = _voice_template(voice_scenes, left, right)
        base.update({"id": f"c{chapter.chapter_id}u{id_offset + part}",
                     "chapter_id": chapter.chapter_id,
                     "start": round(left, 3), "end": round(right, 3),
                     "clip": None, "asset": None, "seedance": False,
                     "assignment_type": "uncovered", "match_class": "",
                     "chapter_title": chapter.title})
        if text:
            base["script"] = text
        base.setdefault("script", "")
        rows.append(base)
    return rows


def align_windows_to_voice(chapters, windows_by_chapter, duration, voice_scenes=None):
    """Lay the chosen footage under the real voice timeline.

    This is the step V2 does backwards. There, twelve scene lengths are fixed first and the
    search has to satisfy them; here the shots exist already and the timeline is built to fit
    them, so a chapter with three good windows gets three cuts and one with a single strong shot
    gets one.
    """
    scenes = []
    for chapter in chapters:
        chosen = list(windows_by_chapter.get(chapter.chapter_id) or [])
        span = max(0.1, float(chapter.end) - float(chapter.start))
        if not chosen:
            scenes.extend(uncovered_voice_beats(
                chapter, voice_scenes, chapter.start, chapter.end, id_offset=len(scenes)))
            continue
        is_demo = "demonstrat" in (chapter.evidence or "").casefold()
        fitted_sequence = fit_chapter_sequence(chosen, span, is_demo=is_demo)
        if not fitted_sequence:
            fitted_sequence = fit_partial_chapter_sequence(chosen, span, is_demo=is_demo)
        cursor = float(chapter.start)
        for window, fit, seconds in fitted_sequence:
            base, text = _voice_template(voice_scenes, cursor, cursor + seconds)
            base.update({
                "id": f"c{chapter.chapter_id}s{len(scenes)}",
                "chapter_id": chapter.chapter_id,
                "chapter_title": chapter.title,
                "start": round(cursor, 3),
                "end": round(cursor + seconds, 3),
                "clip": Path(window.path).name,
                "asset": Path(window.path).name,
                "seedance": True,
                "seedance_start_trim": round(float(fit["start"]), 3),
                "timeline_speed": float(fit.get("speed") or 1.0),
                # A reserved retention hook is intentionally non-literal, but it is still a
                # first-class editorial role.  Keeping this metadata prevents the renderer
                # from treating an otherwise valid V3 dance hook as an ordinary body shot.
                "visual_role": "hook_influencer" if bool(getattr(chapter, "is_hook", False)) else "body",
                "is_hook": bool(getattr(chapter, "is_hook", False)),
                "assignment_type": window.match_class,
                "match_class": window.match_class,
                "scrape_clip_id": window.source_id,
                "selection_reason": window.reason,
                "visual_signature": window.visual_signature,
                "source_has_captions": bool(window.source_has_captions),
                "blur_captions": bool(window.source_has_captions),
                "source_window": [round(float(fit["start"]), 3), round(float(fit["end"]), 3)],
            })
            if text:
                base["script"] = text
            base.setdefault("script", "")
            scenes.append(base)
            cursor += seconds
        if cursor < float(chapter.end) - 0.01:
            scenes.extend(uncovered_voice_beats(
                chapter, voice_scenes, cursor, chapter.end, id_offset=len(scenes)))
    # Never stretch the final picture to make up a timeline arithmetic error. Every chapter above
    # either tiles its entire voice span with real frames or is explicitly marked uncovered.
    return scenes


def audit_timeline_scenes(scenes, windows_by_chapter, ffmpeg=None):
    """Audit the exact source bytes the renderer will use, not just the grader's wider window."""
    lookup = {}
    for windows in (windows_by_chapter or {}).values():
        for window in windows:
            lookup[(int(window.chapter_id), str(window.source_id))] = window
    bad = []
    for scene in (scenes or []):
        if not scene.get("clip"):
            continue
        key = (int(scene.get("chapter_id") or 0), str(scene.get("scrape_clip_id") or ""))
        window = lookup.get(key)
        source_window = scene.get("source_window") or []
        if not window or len(source_window) != 2:
            bad.append((key[0], key[1], "repeated_frames"))
            continue
        issue = technical_window_issue(window.path, source_window[0], source_window[1], ffmpeg=ffmpeg)
        if issue:
            bad.append((key[0], key[1], issue))
    return bad


def build_audited_timeline(chapters, windows_by_chapter, duration, voice_scenes, ffmpeg,
                           status_cb=None):
    """Rebuild with the next-best clean shot whenever the final fitted window fails its audit."""
    windows_by_chapter = {key: list(value or []) for key, value in (windows_by_chapter or {}).items()}
    scenes = []
    for attempt in range(3):
        scenes = align_windows_to_voice(chapters, windows_by_chapter, duration,
                                        voice_scenes=voice_scenes)
        bad = audit_timeline_scenes(scenes, windows_by_chapter, ffmpeg=ffmpeg)
        if not bad:
            return scenes, windows_by_chapter
        removed = 0
        for chapter_id, source_id, issue in bad:
            before = list(windows_by_chapter.get(chapter_id) or [])
            # Remove only the failing source window from this chapter. Other inspected windows
            # remain ordered behind it and automatically become the replacement on the rebuild.
            after = [window for window in before if window.source_id != source_id]
            if len(after) != len(before):
                windows_by_chapter[chapter_id] = after
                removed += len(before) - len(after)
                _log(status_cb, f"Scrape V3 final audit: replaced source {source_id} in chapter "
                                f"{chapter_id} ({V3_REJECTIONS.get(issue, issue)}).")
        if not removed:
            break
    return scenes, windows_by_chapter


def chapter_report(chapters, windows_by_chapter, state):
    """What the user is shown and what the project keeps: why every shot is on screen."""
    rows = []
    for chapter in chapters:
        info = (state.get("chapters") or {}).get(str(chapter.chapter_id), {})
        chosen = list(windows_by_chapter.get(chapter.chapter_id) or [])
        rows.append({
            "chapter_id": chapter.chapter_id,
            "title": chapter.title,
            "voice_span": [round(chapter.start, 3), round(chapter.end, 3)],
            "scene_ids": list(chapter.scene_ids),
            "brief": chapter.brief(),
            "queries": info.get("queries") or chapter.queries,
            "raw_results": info.get("raw_results", 0),
            "sources_downloaded": info.get("downloaded", 0),
            "sources_rejected": info.get("rejected") or [],
            # What was KEPT and why. Previously the report explained only the refusals.
            "sources_accepted": info.get("accepted") or [],
            "windows_passed_over": info.get("windows_passed_over") or [],
            # The per-round log was kept in `state` and never written here, so a finished run
            # could not answer "did this chapter search again, and why did it stop?". Reading
            # the absent field as a measured zero is a mistake this report invited.
            "rounds": info.get("rounds") or [],
            "skipped_out_of_time": info.get("skipped_out_of_time", 0),
            "stopped_because": info.get("stopped_because", ""),
            "windows": [{"source_id": w.source_id, "platform": w.platform,
                         "window": [w.start, w.end], "match_class": w.match_class,
                         "why": w.reason,
                         "visual_signature": w.visual_signature,
                         "source_has_captions": bool(w.source_has_captions)} for w in chosen],
        })
    return rows


def cross_chapter_conflicts(windows_by_chapter):
    """Source ids whose selected time ranges overlap across chapters."""
    seen, clashes = {}, set()
    for chapter_id, windows in (windows_by_chapter or {}).items():
        for window in windows:
            for previous_chapter, previous in seen.get(window.source_id, []):
                if previous_chapter != chapter_id and previous.overlaps(
                        window, V3_CONFIG["min_window_gap_seconds"]):
                    clashes.add(window.source_id)
            seen.setdefault(window.source_id, []).append((chapter_id, window))
    return sorted(clashes)


def drop_cross_chapter_reuse(windows_by_chapter, status_cb=None):
    """Drop only repeated/overlapping parts; distinct parts of one source remain valid."""
    selected, cleaned, dropped = {}, {}, 0
    for chapter_id in sorted(windows_by_chapter or {}):
        keep = []
        for window in windows_by_chapter[chapter_id]:
            prior = selected.get(window.source_id, [])
            if any(other.overlaps(window, V3_CONFIG["min_window_gap_seconds"])
                   for other in prior):
                dropped += 1
                continue
            # Identical visible-content descriptions from one source are also repetition even
            # when the timestamps do not overlap (the failed run's three ceiling-speaker shots).
            visual_key = visual_identity(window)
            if visual_key and any(
                    visual_identity(other) == visual_key
                    for other in prior):
                dropped += 1
                continue
            keep.append(window)
            selected.setdefault(window.source_id, []).append(window)
        cleaned[chapter_id] = keep
    if dropped:
        _log(status_cb, "Scrape V3: dropped %d repeated/overlapping source window(s); distinct "
                        "parts of the same source remain available."
             % dropped)
    return cleaned


def write_report(project_dir, chapters, windows_by_chapter, state):
    """Persist the diagnostics beside the project so a run can be read after the fact."""
    review = Path(project_dir) / "review"
    review.mkdir(parents=True, exist_ok=True)
    payload = {
        "engine": "scrape_v3",
        "chapters": chapter_report(chapters, windows_by_chapter, state),
        "totals": {
            "chapters": len(chapters),
            "windows": sum(len(v) for v in (windows_by_chapter or {}).values()),
            "sources": sum((state.get("chapters") or {}).get(str(c.chapter_id), {})
                           .get("downloaded", 0) for c in chapters),
        },
    }
    path = review / "scrape_v3_report.json"
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def write_live_processing_timeline(project_dir, chapters, windows_by_chapter=None,
                                   phase="assigning"):
    """Publish the V3 edit map for the processing UI.

    This is deliberately a tiny view of the editor state rather than the full scrape report. The
    browser polls it while the run is active: chapters appear as soon as the director has decided
    the beats, and accepted windows then populate their real beat without inferring timing from
    ``scraped_XX`` filenames.
    """
    windows_by_chapter = windows_by_chapter or {}
    beats = []
    for chapter in chapters or []:
        assigned = []
        for window in windows_by_chapter.get(chapter.chapter_id, []) or []:
            assigned.append({
                "path": str(window.path or ""),
                "source_id": str(window.source_id or ""),
                "platform": str(window.platform or ""),
                "source_start": round(float(window.start or 0), 3),
                "source_end": round(float(window.end or 0), 3),
                "match_class": str(window.match_class or "context"),
            })
        beats.append({
            "id": int(chapter.chapter_id),
            "start": round(float(chapter.start or 0), 3),
            "end": round(float(chapter.end or 0), 3),
            "title": str(chapter.title or f"Beat {chapter.chapter_id}"),
            "voice": str(getattr(chapter, "voice_text", "") or chapter.brief())[:260],
            "assigned": assigned,
        })
    duration = max((float(beat["end"]) for beat in beats), default=0.0)
    payload = {
        "version": 1,
        "engine": "scrape_v3",
        "phase": str(phase or "assigning"),
        "duration": round(duration, 3),
        "updated_at": time.time(),
        "beats": beats,
    }
    review = Path(project_dir) / "review"
    review.mkdir(parents=True, exist_ok=True)
    path = review / "live_processing_timeline.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)
    return path


# --------------------------------------------------------------------------- vision grading
GRADE_PROMPT = """You are an editor choosing shots for one chapter of a vertical short.

CHAPTER: {title}
WHAT IT IS ABOUT: {brief}
ACCEPTABLE RELATED FOOTAGE: {proxies}
WRONG FOR THIS CHAPTER: {forbidden}

The strip below is frames sampled across ONE source video, left to right, covering {dur:.1f}
seconds. Judge what you can SEE, not the caption or the title.

OBJECT IDENTITY IS NON-NEGOTIABLE. Do not infer the requested object merely because the search
query or surrounding street is relevant. For traffic-light chapters, a real vehicle signal must
visibly have the conventional signal housing/lenses (or a recognizable pedestrian signal). An LED
text board, warning sign, direction display, railway sign or glowing Japanese lettering is NOT a
traffic light and must be unrelated_filler.

Pick up to {maxw} moments from this video that would work in this chapter. Multiple moments are
allowed ONLY when they show visibly different actions, subjects, locations or stages of an action.
Three timestamp ranges of the same speaker, person, aisle or repeated activity count as ONE moment.
Never subdivide one long region just to fill the requested count. For each valid moment, give the
start and end second WITHIN this video and one of:
  exact           - it visibly proves the chapter's claim
  context         - it clearly shows the same real place, object, system or action
  editorial_proxy - a SECONDARY cut that still visibly shows the same real-world subject;
                    never use this for a generic crowd, random person, room, street or emotion

For every window also return `visual_signature`: a compact literal identity in the form
"subject | action | place". It describes the picture, not why the picture fits the narration.
If two ranges show the same person doing the same activity in the same place, they MUST have the
same signature and only one may be returned.

Reject the WHOLE source only for one of these, and name which:
  wrong_country, unrelated_filler, slideshow_static, not_vertical, captions_cover_subject,
  caption_on_solid_box, screen_recording, text_dominates, talking_head, duet_or_stitch,
  repeated_frames, no_visible_action

talking_head means the shot is a person addressing the camera - a vlog piece to camera, a news
presenter, a reaction video, an explainer filmed at a desk. The Short already has a narrator; a
second talking face is not footage of anything. A person who happens to look at the camera while
DOING the thing is normal footage and must not be rejected for it.

duet_or_stitch means someone else's video is inset into the frame - a reaction panel in a corner,
a split screen, a "source: @name" credit over borrowed footage. The inset belongs to a different
video and cannot be cut around.

screen_recording means the frame IS a screen: a phone or computer UI being captured - app
windows, chat threads, browser pages, camera viewfinder overlays, on-screen buttons and status
bars. Filmed footage that happens to CONTAIN a screen (someone holding a phone, a shop monitor on
a wall) is normal footage and must not be rejected for it.

text_dominates means creator-added text is so large or so dense that it covers roughly a tenth or
more of the picture - big multi-line headline captions filling the upper or middle third. Those
cannot be cleaned off: removing that much rebuilds the picture instead of the letters. Ordinary
one- or two-line subtitles are fine and must NOT be rejected for this.

caption_on_solid_box means the caption is drawn on a SOLID PLATE - an opaque coloured or white
rectangle, rounded box, banner or speech bubble behind the letters. Those cannot be cleaned: the
letters come off and the empty plate stays. Text with only an outline, a drop shadow or a soft
glow is NOT a plate and is fine.

STRICT BANNER RULE: a creator promo strip is always a solid-box rejection, even if it only uses
a small part of the frame. This includes opaque lower-thirds or top banners saying things such as
"full video", "YouTube", "follow", "subscribe", a channel name, or a URL. Do not call these
ordinary subtitles and do not offer a window from that source: removing the letters leaves the
advertising plate on screen.

Also report has_burned_captions. This means creator-added subtitles or overlay text repeated in the
same screen region across frames. It does NOT mean real writing photographed in the environment:
road signs, shop signs, labels, signal displays and lettering physically attached to an object are
scene content and must never be removed.

Do NOT reject it for having some on-screen text, being unpopular, or failing to prove every word.
A related, watchable shot is worth more than nothing.

MULTI-FACT CHAPTER RULE: narration often groups several examples into one visual chapter. A video
is relevant when it clearly shows ONE concrete, visible component of that chapter; it does NOT
need to show every listed example at once. A chapter about a convenience store solving daily
problems may validly use a person choosing prepared food, a cashier interaction, a working ATM,
a copier, a shelf-restocking action, or a clearly Japanese store interior as separate cuts.
Likewise, a chapter about a school safety practice can use distinct shots of the proof item,
children using the route, and the relevant crossing/station context. Reject only when the source
does not visibly belong to ANY tangible component of the chapter.

ABSTRACT CAUSE RULE: some claims describe an invisible cause followed by a visible employee
response. If the chapter says music secretly signals a cashier, restock or cleaning task, clean
footage of a Japanese retail employee actually cashiering, restocking or cleaning IS a valid
context match even when the music signal itself cannot be seen. Do not reject that physical
action merely because the internal code is invisible.

GENERAL INVISIBLE-CONCEPT RULE: policies, customs, expectations, codes, social rules, sounds and
internal systems are often impossible to photograph directly. In those cases, footage of a real
person visibly interacting with the SAME place, object or process is a context match. Examples:
shoppers moving through the Japanese store for an invisible store-music code; residents carrying
or sorting trash for a social responsibility rule; commuters using the relevant carriage for a
train policy. It does not have to prove the invisible premise. It does have to show the same
specific real-world setting/process in the correct cultural context; a generic crowd or street is
still unrelated.

FACT-DETAIL COVERAGE RULE: A chapter may contain one rare proof detail (a particular colour,
label, protective cover, sign, rule or ceremony) plus the wider real-world process around it.
Use an exact shot when that rare detail is visibly present. After at least one exact/context shot
has established the topic, other clean shots of the SAME real process are valid context even if
the rare detail is out of frame. For example, after a yellow randoseru cover is shown, Japanese
first graders with randoseru walking to school, waiting at a crossing, or entering a station are
valid context. Do not turn this into random children, a generic crowd, or another country.

SUBJECT-IDENTITY RULE: exact/context/editorial_proxy must still depict the real-world subject in
WHAT IT IS ABOUT. A videogame, Fortnite, Minecraft, Roblox, anime or CGI world is unrelated_filler
unless WHAT IT IS ABOUT explicitly asks for that game/animation. Similar colours, landscapes,
emotions or generic actions do not make a different subject an editorial proxy.

WOMEN-ONLY TRAIN RULE: at least one early proof shot must visibly show the women-only sign or
the marked carriage. Once that proof is present, a watchable shot of women boarding/riding a
Japanese commuter train, a crowded platform, or doors opening on the same train process is valid
context footage. A generic indoor group, unrelated commuter, child, road crossing or ordinary
train with no women is unrelated_filler. Do not require every later cut to show the sign: that
overconstraint discarded the actual boarding, crowd and door footage needed to make an edit.

ONE GOOD MOMENT IS ENOUGH. You are not rating the video, you are asking whether anything in it
can be cut into this chapter. A travel vlog that spends most of its length elsewhere but shows
the subject in two frames is NOT unrelated_filler - return that moment and ignore the rest.
unrelated_filler means NO part of the strip shows the chapter's subject at all. Checked by hand
against a delivered run: an airport capsule pod, a capsule-hotel corridor and a "can I fit in a
capsule hotel" clip were all rejected as unrelated for a capsule-hotel chapter, because most of
each clip was something else. Those three rejections cost the edit a third of its footage and
the finished Short then played the same TikTok for 41% of its length.

RATE HOW WATCHABLE EACH MOMENT IS, separately from whether it fits. Fitting the chapter and
being worth watching are different questions, and until now only the first was asked - so the
edit filled up with correct, dead footage: a static platform, a locked-off carriage, a wide
street with nothing happening in it. Give every window a `visual_interest` from 0 to 10.

  8-10  something a person would stop scrolling for: a striking or strange sight, a visible
        reaction on a face, an unusual object or ritual, real physical effort, a crowd doing
        one thing together, a moment with a clear before/after
  5-7   ordinary competent footage of the subject - a person doing the thing, close enough to
        read, camera steady, something changing on screen
  2-4   correct but inert: a wide static view, an empty room or platform, a slow pan over
        nothing in particular, a shot where the subject is small and far away
  0-1   unwatchable: dark, blurred, shaking beyond use, heavily compressed, or an almost
        frozen frame

Judge the PICTURE, not the popularity, and never raise the score because the clip fits the
chapter well - that is what match_class already says. A perfectly on-topic empty platform is a
3. A strange or funny moment that is only loosely related is still an 8.

CALIBRATE AGAINST THE MIDDLE. Measured on a real run: asked without this paragraph, the model
put 12 of 13 windows between 5 and 7 and gave "an empty platform" a 6 - a score that sorts
nothing. MOST FOOTAGE IS A 5. Before writing 7 or more you must be able to name the specific
thing that happens, and it must be in your `reason`: someone pushing commuters into a carriage,
a face reacting, an object doing something unexpected. "People are present and the shot is
competent" is a 5, not a 7. If the only true description is a category - passengers riding,
people walking, a street with traffic - the score is 4 or below, however well it fits.

Return STRICT JSON:
{{"reject":"" or "<reason>","is_game_or_animation":false,"has_burned_captions":false,
"windows":[{{"start":0.0,"end":3.0,"match_class":"exact","visual_interest":7,
"reason":"what is visible and why it fits",
"visual_signature":"cashier | scans basket | supermarket checkout"}}]}}"""


# A brief that chains conditions cannot be satisfied by footage that exists. Measured on the
# Tokyo-train run (2026-08-29): chapter 3 asked for "One continuous sequence must show the doors
# opening, the friends leaving the train, turning toward one another, and visibly talking
# together on the platform" and accepted 0 of 20 downloads. The SAME twenty strips, re-graded
# with the same vision model against "a station platform with passengers who have left, or are
# leaving, a train", accepted three times as many.
#
# The planner is now told to write one condition (see CHAPTER_PROMPT). This is the net for when
# it does it anyway: rather than deliver an empty beat, keep the FIRST condition and re-judge
# the footage already on disk. No new searches, no new downloads - only a second opinion.
_CONDITION_SPLIT = re.compile(
    r",\s*(?:and\s+)?(?:then\s+)?|\s+and\s+then\s+|\s+followed\s+by\s+|\s+;\s*", re.IGNORECASE)
_SEQUENCE_LEAD = re.compile(
    r"^\s*(?:one\s+)?(?:single\s+)?(?:continuous\s+)?(?:shot|sequence|clip|take)\s+must\s+show\s+",
    re.IGNORECASE)


def _is_compound_evidence(text):
    """True when the shot brief asks for a sequence of things instead of one visible thing."""
    text = str(text or "")
    if not text.strip():
        return False
    if re.search(r"\bone continuous\b|\bfollowed by\b|\band then\b", text, re.IGNORECASE):
        return True
    return len([part for part in _CONDITION_SPLIT.split(text) if part.strip()]) > 2


def simplify_evidence(text):
    """Reduce a chained shot brief to its first observable condition."""
    text = _SEQUENCE_LEAD.sub("", str(text or "").strip())
    parts = [part.strip() for part in _CONDITION_SPLIT.split(text) if part.strip()]
    if not parts:
        return text
    first = parts[0]
    # A leading "the doors opening" is a moment, not a subject; keep the next clause too when
    # the first is very short, so the brief still names something searchable.
    if len(first.split()) < 4 and len(parts) > 1:
        first = f"{first}, {parts[1]}"
    return first.rstrip(" .;,")


def rescue_empty_chapter(chapter, project_dir, ffmpeg, seen_source_ids=None,
                         reasoning_model=None, status_cb=None, state=None):
    """Re-grade a chapter's OWN already-downloaded footage against a simplified brief.

    Returns the windows found, or [] when there is nothing on disk or nothing to relax.
    """
    if not _is_compound_evidence(chapter.evidence):
        return []
    proxies = Path(project_dir) / "seedance 2.0" / "_v3_proxies"
    manifest_path = proxies / "_v3_candidate_manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:                                   # noqa: BLE001
        manifest = {}
    chapter_key = re.sub(r"\s+", " ", str(chapter.title or "").casefold()).strip()
    pool = []
    for cached in sorted(proxies.glob("v3_*.mp4")):
        if str((manifest.get(cached.name) or {}).get("chapter_key") or "") != chapter_key:
            continue
        if cached.stat().st_size < 4096:
            continue
        match = re.match(r"^v3_([^_]+)_(.+)$", cached.stem)
        if not match:
            continue
        pool.append({"source_id": match.group(2), "platform": match.group(1),
                     "path": str(cached), "query": "relaxed re-grade", "item": {}})
    if not pool:
        return []
    relaxed = copy.copy(chapter)
    relaxed.evidence = simplify_evidence(chapter.evidence)
    _log(status_cb, "Scrape V3 chapter %d: nothing passed the brief, so its %d downloaded clip(s) "
                    "are being re-judged against one condition instead of a sequence - %r."
         % (chapter.chapter_id, len(pool), relaxed.evidence[:90]))
    try:
        graded = grade_sources_v3(relaxed, pool, project_dir, ffmpeg,
                                  reasoning_model=reasoning_model, status_cb=status_cb,
                                  state=state)
    except Exception as exc:                            # noqa: BLE001 - a rescue may never abort
        _log(status_cb, "Scrape V3 chapter %d: relaxed re-grade unavailable (%s)."
             % (chapter.chapter_id, type(exc).__name__))
        return []
    chosen = select_chapter_windows(relaxed, graded, state=state)
    if chosen:
        _log(status_cb, "Scrape V3 chapter %d: the relaxed brief recovered %d usable window(s) "
                        "from footage that was already paid for."
             % (chapter.chapter_id, len(chosen)))
    return chosen


def grade_sources_v3(chapter, downloaded, project_dir, ffmpeg, reasoning_model=None,
                     status_cb=None, state=None):
    """Look at each downloaded source and pick the moments that belong in this chapter.

    One vision call per source, against a frame strip. The call is asked for WINDOWS rather than
    a yes/no, because a source that is wrong for its first three seconds is often exactly right
    ten seconds later - V2 threw those away.
    """
    frames_dir = Path(project_dir) / "review" / "_v3_strips" / f"chapter_{chapter.chapter_id}"
    frames_dir.mkdir(parents=True, exist_ok=True)
    _, ffprobe = clip_scraper._ffmpeg_tools()
    graded, rejected, prepared = [], [], []

    # Decode strips first, then run the independent vision reviews concurrently. The old serial
    # loop made 81 reviews take 1.5 hours even though every request spent most of its life waiting
    # on the remote model. Four workers keeps load modest and cuts that wall time dramatically.
    for index, row in enumerate(downloaded or []):
        path = Path(row["path"])
        if not path.is_file():
            continue
        try:
            duration = float(clip_scraper._probe_duration(path, ffprobe) or 0.0)
        except Exception:      # noqa: BLE001 - an unreadable file is one source, not a failure
            duration = 0.0
        if duration <= 0.5:
            rejected.append({"source_id": row["source_id"], "reason": "repeated_frames",
                             "query": row.get("query", ""),
                             "detail": "unreadable or empty file"})
            continue
        seg = scrape_v2.SegmentCandidate(
            segment_id=f"v3_{chapter.chapter_id}_{index}", source_id=row["source_id"],
            platform=row["platform"], source_path=str(path),
            start_time=0.0, end_time=duration, duration=duration, query=row.get("query", ""))
        safe_id = re.sub(r"[^A-Za-z0-9_-]+", "_", str(row["source_id"]))[:32]
        strip = scrape_v2._segment_strip(seg, frames_dir, safe_id, ffmpeg,
                                         frames_n=V3_CONFIG["vision_frames_per_source"])
        if not strip:
            rejected.append({"source_id": row["source_id"], "reason": "repeated_frames",
                             "query": row.get("query", ""),
                             "detail": "no frames could be read"})
            continue
        prepared.append((row, path, duration, strip))

    def inspect_source(task):
        row, path, duration, strip = task
        prompt = GRADE_PROMPT.format(
            title=chapter.title, brief=chapter.brief(),
            proxies=", ".join(chapter.proxies_ok) or "anything showing the same subject",
            forbidden=", ".join(chapter.forbidden) or "unrelated subjects",
            dur=duration, maxw=V3_CONFIG["max_windows_per_source"])
        try:
            data = scrape_v2._vision_json(prompt, strip, max_tokens=1200, temperature=0.1,
                                          reasoning_model=reasoning_model) or {}
        except Exception as exc:      # noqa: BLE001 - one bad call is one source, not the chapter
            _log(status_cb, f"Scrape V3: vision failed on {row['source_id']} "
                            f"({type(exc).__name__}).")
            return None, None
        reason = str(data.get("reject") or "").strip().lower()
        if bool(data.get("is_game_or_animation")) and not _chapter_allows_games(chapter):
            reason = "unrelated_filler"
        if reason in V3_REJECTIONS:
            return None, {"source_id": row["source_id"], "reason": reason,
                          "query": row.get("query", ""),
                          "detail": V3_REJECTIONS[reason]}
        windows = []
        technical_rejections = []
        for window in (data.get("windows") or [])[:V3_CONFIG["max_windows_per_source"]]:
            try:
                start = max(0.0, float(window.get("start") or 0.0))
                end = min(duration, float(window.get("end") or 0.0))
            except (TypeError, ValueError):
                continue
            if end - start < 0.6:
                continue
            # Cut the source window *around* a creator's internal edit where possible. This
            # retains an honest, watchable part instead of throwing away the only relevant food,
            # cashier or commuter action from an otherwise edited TikTok.
            clean_part = longest_uncut_subwindow(path, start, end, ffmpeg=ffmpeg)
            if clean_part is not None:
                start, end = clean_part
            technical = technical_window_issue(path, start, end, ffmpeg=ffmpeg)
            if technical:
                technical_rejections.append(technical)
                continue
            windows.append({"start": round(start, 3), "end": round(end, 3),
                            "match_class": normalize_match_class(window.get("match_class")),
                            # Carried explicitly. This dict is rebuilt field by field, so a new
                            # key added anywhere upstream is silently dropped here: the
                            # watchability score reached the model and the report, and every
                            # window still arrived at the ranking as the neutral default 5.0.
                            # Measured on a whole live run - 11 of 11 windows at exactly 5.0.
                            "visual_interest": _interest_of(window),
                            "reason": str(window.get("reason") or "")[:180],
                            "visual_signature": str(window.get("visual_signature") or "")[:120]})
        if not windows:
            technical = technical_rejections[0] if technical_rejections else "no_visible_action"
            return None, {"source_id": row["source_id"], "reason": technical,
                          # The query is what the retry has to reason about: a rejection reason
                          # alone ("unrelated_filler x10") cannot tell the next round WHICH
                          # hypothesis produced the rubbish, so it could propose the same shape
                          # again. Measured: three commute queries returned 8 unrelated sources
                          # and the retry was never told which three.
                          "query": row.get("query", ""),
                          "detail": V3_REJECTIONS.get(technical,
                             "no usable moment was identified")}
        return ({"source_id": row["source_id"], "platform": row["platform"],
                 "path": str(path), "query": row.get("query", ""), "windows": windows,
                 "source_has_captions": bool(data.get("has_burned_captions"))}, None)

    workers = max(1, min(int(V3_CONFIG.get("vision_concurrency") or 1), len(prepared) or 1))
    if prepared:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers,
                                                   thread_name_prefix="scrape-v3-vision") as pool:
            for accepted, declined in pool.map(inspect_source, prepared):
                if accepted:
                    graded.append(accepted)
                if declined:
                    rejected.append(declined)

    if state is not None:
        row = state.setdefault("chapters", {}).setdefault(str(chapter.chapter_id), {})
        row["rejected"] = list(row.get("rejected") or []) + rejected
        # What the vision pass ACCEPTED, and what it said it saw. The report used to explain
        # only the refusals: a finished Short could tell you why 61 sources were thrown away and
        # nothing about why the nine survivors were kept, or which search found them.
        row["accepted"] = list(row.get("accepted") or []) + [
            {"source_id": item["source_id"], "platform": item["platform"],
             "query": item.get("query", ""),
             "has_burned_captions": bool(item.get("source_has_captions")),
             "windows_offered": [
                 {"window": [w["start"], w["end"]], "match_class": w["match_class"],
                  "visual_interest": _interest_of(w),
                  "saw": w.get("reason", ""), "visual_signature": w.get("visual_signature", "")}
                 for w in (item.get("windows") or [])]}
            for item in graded]
    if rejected:
        counts = {}
        for entry in rejected:
            counts[entry["reason"]] = counts.get(entry["reason"], 0) + 1
        _log(status_cb, "Scrape V3 chapter %d: rejected %d source(s) - %s."
             % (chapter.chapter_id, len(rejected),
                ", ".join(f"{V3_REJECTIONS.get(k, k)} x{v}" for k, v in counts.items())))
    return graded


# --------------------------------------------------------------------------- orchestrator
def _as_bucket_candidate(row, chapter, ffmpeg=None):
    """One V3 download in the shape the older bucket scraper returns.

    The timeline's targeted replacement, the vision matcher and the ranking all read that shape
    already, so translating here means only the SEARCH changes and nothing downstream has to
    learn about chapters.
    """
    item = row.get("item") or {}
    stats = item.get("stats") or {}
    path = str(row.get("path") or "")
    black, text = 0.0, 0.0
    if ffmpeg and path:
        # The two measurements that actually gate quality downstream. Reporting a flat 0.0 for
        # them would tell the ranker "no letterboxing, no burnt-in captions" about footage
        # nobody has looked at, which is exactly what those gates exist to catch.
        try:
            black = float((clip_scraper.detect_fake_vertical_or_black_bars(path, ffmpeg) or {})
                          .get("black_bar_score") or 0.0)
        except Exception:                                               # noqa: BLE001
            black = 0.0
        try:
            text = float(clip_scraper.text_heaviness_score(path, ffmpeg) or 0.0)
        except Exception:                                               # noqa: BLE001
            text = 0.0
    try:
        likes = int(float(stats.get("diggCount") or 0))
    except (TypeError, ValueError):
        likes = 0
    return {
        "path": path,
        "query": str(row.get("query") or ""),
        "platform": str(row.get("platform") or "tiktok"),
        "clip_id": str(row.get("source_id") or item.get("id") or Path(path).stem),
        "likes": likes,
        "meta": {"caption": str(item.get("desc") or "")[:300],
                 "chapter": getattr(chapter, "title", ""),
                 "chapter_id": getattr(chapter, "chapter_id", 0)},
        "black_bar_score": black,
        "text_heaviness": text,
        # Left to the window scan: V3 judges rapid cutting per CHOSEN window
        # (technical_window_issue), not per file, and a whole-file number here would be a guess.
        "rapid_internal_cut_count": 0,
        "tier": "v3_rescrape",
    }


def gather_candidates_for_scenes(config, scenes, project_dir, platforms, script_text="",
                                 reasoning_model=None, status_cb=None, cancel_check=None,
                                 time_budget=900.0, measure=True):
    """V3's chapter search, run for a SUBSET of a timeline's scenes.

    ``scrape_social_plan_v3`` cannot be used for this: it returns a new scene list built around
    whatever it found, which is right for a fresh run and wrong for a re-scrape, where the
    timeline's shape is the thing being kept. So the chapter planning and the source gathering
    are reused and the fitting is left to the caller, which already knows the scenes to fill.

    Returns bucket-shaped candidates, newest search first.
    """
    project_dir = Path(project_dir)
    scenes = [dict(scene) for scene in (scenes or [])]
    if not scenes:
        return []
    state = {"chapters": {}}
    chapters = plan_chapters_v3(
        scenes, script_text or "", reasoning_model=reasoning_model, status_cb=status_cb,
        influencer_hook=False,
    )
    if not chapters:
        _log(status_cb, "Scrape V3 rescrape: the selected clips produced no visual chapter.")
        return []
    explicit = sanitize_queries(re.split(r"[,\n]", str(config.get("scrape_terms") or "")),
                               limit=_queries_per_chapter())
    if explicit and chapters:
        chapters[0].explicit_queries = explicit
    deadline = time.monotonic() + max(180.0, float(time_budget or 900.0))
    ffmpeg = pipeline.find_ffmpeg() if measure else None
    seen_source_ids, out = set(), []
    _log(status_cb, "Scrape V3 rescrape: %d chapter(s) planned for %d selected clip(s) - %s"
         % (len(chapters), len(scenes), ", ".join(c.title for c in chapters)))
    for chapter in chapters:
        if cancel_check and cancel_check():
            break
        rows = gather_chapter_sources(chapter, platforms, project_dir, seen_source_ids,
                                      cancel_check=cancel_check, deadline=deadline,
                                      status_cb=status_cb, state=state) or []
        _log(status_cb, "Scrape V3 rescrape: '%s' downloaded %d candidate(s)."
             % (chapter.title, len(rows)))
        for row in rows:
            if row.get("path"):
                out.append(_as_bucket_candidate(row, chapter, ffmpeg))
    _log(status_cb, f"Scrape V3 rescrape: {len(out)} candidate(s) for the vision matcher.")
    return out


def scrape_social_plan_v3(config, scenes, project_dir, platforms, per_clip_seconds=None,
                          script_relevancy=None, cookies=None, cancel_check=None,
                          understanding=None, reasoning_model=None, status_cb=None,
                          script_text=""):
    """Run the V3 pipeline and hand back a new scene list built around the footage.

    Returns (scenes, scene_clips). The caller replaces its voice-derived scene list with this
    one: in V3 the timeline is a consequence of what was found, not a constraint on the search.
    """
    project_dir = Path(project_dir)
    state = {"chapters": {}}
    budget = float(config.get("scrape_time_budget") or 3600)
    deadline = time.monotonic() + max(300.0, budget)
    ffmpeg = pipeline.find_ffmpeg()

    # A continued/restarted run may reuse the project directory. Never show yesterday's edit map
    # during today's planning gap.
    try:
        (project_dir / "review" / "live_processing_timeline.json").unlink(missing_ok=True)
    except OSError:
        pass

    _log(status_cb, "Scrape V3 (editorial chapters) engine selected.")
    chapters = plan_chapters_v3(
        scenes, script_text or "", reasoning_model=reasoning_model, status_cb=status_cb,
        influencer_hook=bool(config.get("influencer_hook")),
    )
    # Unlike the old bucket scraper, V3 owns the query ladder inside each editorial chapter.
    # Feed the creator's optional terms into that ladder explicitly; previously they were only
    # displayed in the run log and could be displaced by an LLM fallback before any search ran.
    raw_terms = re.split(r"[,\n]", str(config.get("scrape_terms") or ""))
    explicit_terms = sanitize_queries(raw_terms, limit=_queries_per_chapter())
    if explicit_terms:
        body_chapter = next((chapter for chapter in chapters if not chapter.is_hook),
                            chapters[0] if chapters else None)
        if body_chapter is not None:
            body_chapter.explicit_queries = explicit_terms
            _log(status_cb, "Scrape V3: prioritizing %d creator-supplied search term(s) in '%s'."
                 % (len(explicit_terms), body_chapter.title))
    # Instagram has no keyword search on this API account, so the topic is turned into a list of
    # creators ONCE per project and the reel pull is served from that for every query. Skipped
    # entirely unless Instagram is actually one of the selected platforms.
    if "instagram" in {str(p).strip().lower() for p in (platforms or ())}:
        try:
            clip_scraper.set_instagram_accounts(
                instagram_accounts_for(script_text or "", scenes,
                                       reasoning_model=reasoning_model, status_cb=status_cb))
        except Exception as exc:      # noqa: BLE001 - a thinner run, never a failed one
            _log(status_cb, f"Scrape V3: Instagram accounts unavailable "
                            f"({type(exc).__name__}); continuing without them.")
    if not chapters:
        return list(scenes or []), []

    windows_by_chapter = {chapter.chapter_id: [] for chapter in chapters}
    # Draw the complete voice timeline before the first provider result arrives. Assignments are
    # written back into these same slots after every search pass.
    write_live_processing_timeline(project_dir, chapters, windows_by_chapter, phase="planned")
    work = {chapter.chapter_id: {
        "original_queries": list(chapter.queries), "attempted": [], "seen": set(),
        "graded": [], "done": False,
    } for chapter in chapters}

    # Search round-robin: every visual idea gets a first attempt before one difficult abstract
    # chapter is allowed to consume three retry rounds. The old chapter-at-a-time loop spent the
    # whole run rejecting dictionary/explainer results for chapter 1; chapters 3 and 4 then logged
    # zero results and the timeline was padded with the hook clip.
    round_index = 0
    while (round_index < V3_CONFIG["max_search_rounds"]
           and time.monotonic() < deadline
           and not all(row["done"] for row in work.values())):
        for chapter_index, chapter in enumerate(chapters):
            row = work[chapter.chapter_id]
            if row["done"] or (cancel_check and cancel_check()) or time.monotonic() >= deadline:
                continue
            if round_index:
                rejected = ((state.get("chapters") or {}).get(str(chapter.chapter_id), {})
                            .get("rejected") or [])
                fresh_queries = adaptive_search_queries(
                    chapter, row["attempted"], rejected, round_index=round_index,
                    reasoning_model=reasoning_model, status_cb=status_cb)
                if not fresh_queries:
                    fresh_queries = persistent_search_queries(
                        chapter, row["attempted"], round_index=round_index)
                if not fresh_queries:
                    # Never erase ``attempted`` and start the same ladder again. That was the
                    # mechanism behind two-hour runs with 100+ downloads yet no new visual idea.
                    # Mark this chapter exhausted; the final report is immediate and honest,
                    # while its already-downloaded candidates remain available for continuation.
                    row["done"] = True
                    row["exhausted"] = True
                    (state.setdefault("chapters", {}).setdefault(str(chapter.chapter_id), {})
                     )["stopped_because"] = "every search hypothesis was tried"
                    _log(status_cb, "Scrape V3 chapter %d: every distinct search hypothesis was "
                                    "tried; stopping retries instead of repeating the same pool."
                         % chapter.chapter_id)
                    continue
                chapter.queries = fresh_queries
                _log(status_cb, "Scrape V3 chapter %d: visual pool was not strong enough; "
                                "adaptive search round %d uses %s."
                     % (chapter.chapter_id, round_index + 1, " | ".join(fresh_queries)))
            row["attempted"].extend(chapter_search_queries(chapter))
            # A later chapter may use a genuinely different part of the same source. Exclude only
            # clips already inspected for THIS chapter; the final overlap/content audit prevents
            # repeated parts across chapters.
            exclusion = set(row["seen"])
            # Reserve time for the untouched chapters in this round. A provider that keeps
            # returning weak results for one idea may not starve the rest of the Short.
            untouched = max(1, len(chapters) - chapter_index)
            chapter.chapters_remaining = untouched
            # A single failed visual hypothesis never earns five minutes of browser work. With
            # the bounded five-wave ladder this keeps a bad three-chapter script below roughly
            # 35-40 minutes instead of burning a two-hour user budget on repeated rejects.
            # 150s per chapter is right when TikTok's own search is answering: a failed visual
            # hypothesis never earns five minutes of browser work. Once TikTok reports its daily
            # limit the run is carried entirely by Bright Data, which is an ASYNC job service -
            # one measured discover call took 111 seconds to go from submitted to ready. Holding
            # a chapter to 150s in that mode kills every call just before it returns, and the
            # whole run reports "0 usable record(s)" from an API that is working fine.
            chapter_cap = 420.0 if tiktok_daily_limited() else 150.0
            fair_seconds = max(60.0, min(chapter_cap,
                                         (deadline - time.monotonic()) / untouched))
            downloaded = gather_chapter_sources(
                chapter, platforms, project_dir, exclusion,
                cancel_check=cancel_check,
                deadline=min(deadline, time.monotonic() + fair_seconds),
                status_cb=status_cb, state=state)
            row["seen"].update(str(item.get("source_id") or "") for item in downloaded)
            graded = grade_sources_v3(chapter, downloaded, project_dir, ffmpeg,
                                      reasoning_model=reasoning_model, status_cb=status_cb,
                                      state=state)
            row["graded"].extend(graded)
            chosen = select_chapter_windows(
                chapter, row["graded"], status_cb=status_cb, state=state)
            windows_by_chapter[chapter.chapter_id] = chosen
            write_live_processing_timeline(
                project_dir, chapters, windows_by_chapter, phase="assigning")
            info = (state.get("chapters") or {}).get(str(chapter.chapter_id), {})
            if info.get("rounds"):
                info["rounds"][-1]["usable_sources"] = len(graded)
                info["rounds"][-1]["usable_windows_total"] = len(chosen)
            if chapter_search_satisfied(chapter, chosen):
                row["done"] = True
                (state.setdefault("chapters", {}).setdefault(str(chapter.chapter_id), {})
                 )["stopped_because"] = "enough coverage after round %d" % (round_index + 1)
                _log(status_cb, f"Scrape V3 chapter {chapter.chapter_id}: enough clean visual "
                                f"coverage after round {round_index + 1}.")
        round_index += 1

    for chapter in chapters:
        # Anything still not done when the loop exits ran out of ROUNDS or CLOCK, which is a
        # different failure from "tried everything" and must not read the same in the report.
        if not work[chapter.chapter_id]["done"]:
            reason = ("the scrape time budget ran out"
                      if time.monotonic() >= deadline
                      else "the round limit (%d) was reached" % V3_CONFIG["max_search_rounds"])
            (state.setdefault("chapters", {}).setdefault(str(chapter.chapter_id), {})
             )["stopped_because"] = reason
        chapter.queries = work[chapter.chapter_id]["original_queries"]
    # A beat with NO footage is the worst outcome the scraper can produce, and an over-specified
    # brief is a cause it can still do something about without spending another download.
    for chapter in chapters:
        if windows_by_chapter.get(chapter.chapter_id):
            continue
        recovered = rescue_empty_chapter(chapter, project_dir, ffmpeg,
                                         reasoning_model=reasoning_model,
                                         status_cb=status_cb, state=state)
        if recovered:
            windows_by_chapter[chapter.chapter_id] = recovered
            (state.setdefault("chapters", {}).setdefault(str(chapter.chapter_id), {})
             )["stopped_because"] = "recovered by relaxing the brief to one condition"
    windows_by_chapter = drop_cross_chapter_reuse(windows_by_chapter, status_cb=status_cb)
    write_live_processing_timeline(
        project_dir, chapters, windows_by_chapter, phase="assigning")
    incomplete = [chapter for chapter in chapters
                  if not chapter_search_satisfied(
                      chapter, windows_by_chapter.get(chapter.chapter_id, []))]
    if incomplete:
        # Deliver what WAS found instead of throwing the run away. A chapter the platforms could
        # not fill still has real footage in its other beats, and a person can fix one gap in the
        # editor in seconds - which they cannot do with an exception. The beats that had to be
        # filled are flagged below and drawn with a red border there.
        report = write_report(project_dir, chapters, windows_by_chapter, state)
        names = ", ".join(chapter.title for chapter in incomplete)
        exhausted = [chapter for chapter in incomplete
                     if work.get(chapter.chapter_id, {}).get("exhausted")]
        exhausted_note = (" Every distinct query hypothesis was tried for: "
                          + ", ".join(chapter.title for chapter in exhausted) + "."
                          if exhausted else "")
        _log(status_cb, f"Scrape V3: short of clean footage for: {names}.{exhausted_note} "
                        f"Building the edit from what was found and marking the gaps. "
                        f"Candidates: {report}")
    duration = max((float(s.get("end", 0) or 0) for s in (scenes or [])), default=0.0)
    new_scenes, windows_by_chapter = build_audited_timeline(
        chapters, windows_by_chapter, duration, list(scenes or []), ffmpeg,
        status_cb=status_cb)
    empty = [scene for scene in new_scenes if not scene.get("clip")]
    repeated = []
    used_ranges = {}
    for scene in new_scenes:
        source_id = str(scene.get("scrape_clip_id") or "")
        bounds = scene.get("source_window") or []
        if not source_id or len(bounds) != 2:
            continue
        start, end = float(bounds[0]), float(bounds[1])
        if any(min(end, prior_end) - max(start, prior_start) > 0.05
               for prior_start, prior_end in used_ranges.get(source_id, [])):
            repeated.append(f"{source_id}@{start:.2f}-{end:.2f}")
        used_ranges.setdefault(source_id, []).append((start, end))
    # A beat with nothing in it cannot render, so it borrows the nearest usable shot and is
    # FLAGGED. The flag is what the timeline editor draws a red border from: the user sees
    # exactly which pictures were improvised and can swap them, instead of losing the whole run.
    if empty:
        pool = [w for windows in windows_by_chapter.values() for w in windows
                if Path(w.path).is_file()]
        for scene in empty:
            # Flag FIRST. An earlier version only marked a beat it could actually fill, so a run
            # that found nothing at all delivered a wall of silent placeholder cards with no red
            # borders anywhere - the one case where the user most needed to see the problem.
            scene["coverage_gap"] = True
            scene["coverage_gap_reason"] = "no footage was found for this beat"
            if pool:
                stand_in = pool[0]
                scene["clip"] = Path(stand_in.path).name
                scene["scrape_clip_id"] = str(stand_in.source_id or "")
                scene["source_window"] = [float(stand_in.start), float(stand_in.end)]
    for scene in new_scenes:
        bounds = scene.get("source_window") or []
        if len(bounds) == 2 and f"{scene.get('scrape_clip_id')}@{float(bounds[0]):.2f}-"                                 f"{float(bounds[1]):.2f}" in repeated:
            scene["coverage_gap"] = True
            scene.setdefault("coverage_gap_reason", "this shot is used more than once")
    # Showing what was found beats throwing a run away - but only when something WAS found.
    # With an empty pool every beat renders as a placeholder card, and a slate video that says
    # "SCENE 01" thirty times is not a short: it is a failure wearing a delivered filename.
    covered = [s for s in new_scenes if s.get("clip")]
    if not covered:
        raise RuntimeError(
            f"Scrape V3 found no usable footage at all for this script ({len(new_scenes)} "
            f"beats, 0 covered). Nothing was delivered because every beat would have rendered "
            f"as a placeholder card. The searches are in the log - this script's subject may "
            f"simply not exist on the platforms.")
    flagged = [s for s in new_scenes if s.get("coverage_gap")]
    if flagged:
        write_report(project_dir, chapters, windows_by_chapter, state)
        _log(status_cb, f"Scrape V3: {len(flagged)} beat(s) could not be covered cleanly and are "
                        "marked in the timeline editor - swap them there before rendering.")

    clip_dir = project_dir / "seedance 2.0"
    clip_dir.mkdir(parents=True, exist_ok=True)
    scene_clips = []
    for scene in new_scenes:
        name = str(scene.get("clip") or "")
        if not name:
            scene_clips.append(None)
            continue
        source = next((w.path for windows in windows_by_chapter.values() for w in windows
                       if Path(w.path).name == name), "")
        if source and Path(source).is_file():
            dest = clip_dir / name
            if not dest.exists():
                try:
                    shutil.copy2(source, dest)
                except OSError:
                    pass
            scene_clips.append(str(dest))
        else:
            scene_clips.append(None)

    report = write_report(project_dir, chapters, windows_by_chapter, state)
    write_live_processing_timeline(project_dir, chapters, windows_by_chapter, phase="ready")
    covered = sum(1 for s in new_scenes if s.get("clip"))
    _log(status_cb, "Scrape V3: %d chapter(s), %d shot(s) on the timeline, %d covered. Report: %s"
         % (len(chapters), len(new_scenes), covered, report.name))
    config["_scrape_v3"] = {"scenes": new_scenes, "scene_clips": scene_clips,
                            "chapters": chapter_report(chapters, windows_by_chapter, state),
                            "report_path": str(report)}
    return new_scenes, scene_clips
