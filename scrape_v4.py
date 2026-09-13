"""Rendered-search Clip Short V4.

This is deliberately a new scraper, not a V2/V3 fork. V4 discovers canonical
TikTok posts with a rendered search provider, then uses one immutable candidate
manifest and a deterministic editorial gate before anything reaches the timeline.
It never performs local keyword search, never uses cached project footage and
never fills an uncovered beat by repeating another beat.
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import time
import base64
import threading
from itertools import combinations
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict, replace
from pathlib import Path

import clip_scraper
import scrapedo_tiktok
import editorial


def _balance_error():
    """The empty-balance exception class, resolved late.

    `agent_core` is imported per call site in this module, never at the top, so naming
    `agent_core.WaveSpeedBalanceError` in an `except` clause reads an unbound local whenever the
    exception is raised before that import line runs - and an `except` clause that raises is a
    handler that silently does not catch. Resolved through the module table instead.
    """
    import agent_core
    return agent_core.WaveSpeedBalanceError


V4_NAME = "rendered_tiktok_editorial_v4"
# A source post may supply several possible windows during review, but exactly
# one is normally assigned. Declared sequences may continue with distinct windows.
# Different TikTok IDs alone do not make a montage feel diverse.
MAX_PER_SOURCE = 1
# Fact Shorts must visibly progress; a seven-second hold is never an acceptable
# fallback.  2.3s is the intended cadence, with a small readable range around it.
MIN_SHOT, MAX_SHOT = 1.45, 2.75
# Grey levels of change between the thirds of a window, below which the shot is a photograph with
# a caption on it. 9.0 sits under the tenth percentile of what was actually assigned in two
# finished Shorts and above the still end of the pool they were chosen from (the measurement is
# written out at Candidate.motion).
MIN_WINDOW_MOTION = 9.0
# A clip that is a little short is SLOWED to cover its beat; a frozen last frame is worse than
# a slightly slower one. Below this ratio it reads as slow motion, so the renderer holds the
# remainder instead - the honest outcome for a clip that genuinely cannot cover the line.
# 0.84 was chosen by the user against real renders; V2's equivalent floor is 0.88.
V4_STRETCH_FLOOR = 0.84

DEFAULT_V4_VISION_MODEL = "google/gemini-3.7-flash"
# V4's Clip Short preset deliberately defaults to the quality narrator path.
DEFAULT_V4_TTS_MODEL = "pro"
DEFAULT_V4_TTS_VOICE = "Laomedeia"
DEFAULT_V4_TTS_SPEAKER = "Narrator"
_TIKTOK_LOGIN_RECOVERY_LOCK = threading.Lock()
_BAD = re.compile(r"\b(tiktok|instagram|reels?|youtube|shorts|fortnite|minecraft|roblox|"
                  r"podcast|explainer|reaction|news)\b", re.I)
_WORDS = re.compile(r"[A-Za-z]{3,}|[\u3040-\u30ff\u3400-\u9fff]{2,}")
_CJK = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]")


def _log(cb, text):
    if callable(cb):
        cb(text)


def _text(scene):
    for key in ("exact_voice_text", "script", "voice_line", "text", "caption", "narration"):
        value = str((scene or {}).get(key) or "").strip()
        if value:
            return value
    return ""


def _intent(scene):
    """Use the director's existing visual brief, never a synthetic chapter label.

    `required_action` FIRST, and it was missing entirely. It is the one field that describes a
    filmable thing - "Drone flight over a giant classic European roundabout packed with
    circulating cars" - while `must_show` holds the narration with its stopwords removed:
    ['while', "they're", 'everywhere', 'europe', 'japan']. The roundabouts run (2026-09-09)
    searched the second kind and nothing else, and its thirteen queries read like this:

        never noticed almost roundabouts / while they everywhere europe / more them after /
        relies traffic lights even / one europe most normal

    Nobody captions a video with those words, so almost nothing came back that fitted any beat,
    and 14 of 16 scenes ended up filled with whatever was left - an operating theatre under a
    line about roundabouts.

    The topic word is no longer dropped either. "japan" and "japanese" were on the stoplist, so a
    Short about Japan asked thirteen questions and none of them said Japan. The intent behind the
    stoplist was to stop every query collapsing onto the topic; the order below does that job
    better, because the specific words now arrive first and the cap keeps them.
    """
    parts = []
    for key in ("required_action", "visual_subject", "visual_action",
                "required_visual_information", "must_show"):
        value = (scene or {}).get(key)
        if isinstance(value, list):
            parts.extend(str(x) for x in value if str(x).strip())
        elif value:
            parts.append(str(value))
    parts.append(_text(scene))
    raw = _CAMERA_WORDS.sub(" ", " ".join(parts))
    # A contract sentence NAMES a thing and then narrates what it does: "a giant classic European
    # roundabout PACKED with CIRCULATING cars". A caption names the thing. `_tidy_phrase` exists
    # for this but it REJECTS anything carrying a participle, which is every contract sentence, so
    # the narration words are dropped here instead and the subject is kept.
    noise = {"people", "things", "there", "their", "that", "this", "only", "very", "into",
             "onto", "while", "when", "then", "than", "them", "they", "over", "under"}
    words = []
    for word in _WORDS.findall(raw):
        low = word.lower()
        if low in words or low in noise or low in _LEAD_DROP:
            continue
        if low in _ACTION_VERBS:
            continue
        if low.endswith("ing") and len(low) > 4 and low not in _NOUN_ING:
            continue
        words.append(low)
    return words[:8] or ["Japan"]


# Words that describe the SHOT rather than the subject. A caption never says them, so a query
# carrying one retrieves nothing. Measured on a delivered Short: "close shot" and
# "POV follows friends from train" were each spent as real search slots.
_CAMERA_WORDS = re.compile(
    r"\b(close[- ]?up|closeup|wide shot|medium shot|establishing|pov|b[- ]?roll|broll|footage|"
    r"filmed|filming|shot|shots|angle|framing|handheld|camera|cutaway|montage|sequence|candid|"
    r"timelapse|slow motion|slowmo)\b", re.I)
# A leading article marks a description ("a hand buying a hot can"); a caption names the thing.
_LEAD_DROP = {"a", "an", "the", "this", "that", "these", "those", "some", "his", "her", "its",
              "their", "my", "your", "our", "of", "in", "on", "at", "by", "for", "from", "with",
              "into", "onto", "and", "or", "to"}
# Present participles are the tell of a narrated action. These nouns merely end in -ing.
# Finite verbs, not just participles: "POV follows the buyer" lost its camera word and came
# through as "follows the buyer" - still a narrated action, still a caption nobody writes.
_ACTION_VERBS = {
    "follows", "follow", "walks", "walk", "stands", "stand", "sits", "sit", "holds", "hold",
    "opens", "open", "closes", "close", "enters", "enter", "exits", "exit", "turns", "turn",
    "looks", "look", "talks", "talk", "speaks", "speak", "laughs", "laugh", "buys", "buy",
    "presses", "press", "drops", "drop", "shows", "show", "makes", "make", "takes", "take",
    "puts", "put", "pulls", "pull", "pushes", "push", "eats", "eat", "drinks", "drink",
    "sells", "sell", "waits", "wait", "reacts", "react", "steps", "step", "leaves", "leave",
}
_NOUN_ING = {"morning", "evening", "building", "buildings", "training", "shopping", "clothing",
             "crossing", "crossings", "railing", "ceiling", "meeting", "wedding", "painting",
             "parking", "seating", "lighting", "spring", "string", "ring", "thing", "during",
             "opening", "greeting", "boarding", "vending"}


def _tidy_phrase(text):
    """One search phrase, or "" when what is left is not a subject.

    Keeps CJK untouched: it has no articles to strip and no participles to detect, and it is
    what actually retrieves Japanese footage.
    """
    phrase = _BAD.sub(" ", str(text or ""))
    phrase = _CAMERA_WORDS.sub(" ", phrase)
    phrase = re.sub(r"[^\w\s\u3040-\u30ff\u3400-\u9fff-]", " ", phrase)
    phrase = re.sub(r"\s+", " ", phrase).strip()
    if not phrase:
        return ""
    if _CJK.search(phrase):
        return phrase[:60]
    words = [w for w in phrase.split() if w]
    while words and words[0].casefold() in _LEAD_DROP:
        words.pop(0)
    while words and words[-1].casefold() in _LEAD_DROP:
        words.pop()
    if not words:
        return ""
    # A participle anywhere means the phrase narrates rather than names.
    for word in words:
        low = word.casefold()
        if low in _NOUN_ING:
            continue
        if low in _ACTION_VERBS:
            return ""
        if low.endswith("ing") and len(low) > 4:
            return ""
    if len(words) < 2:
        return ""
    return " ".join(words[:5])


def _dedupe(phrases, limit=None):
    """Drop repeats, and drop a phrase that is only the opening words of one we already have.

    Exact-match dedupe let "outsiders expect hot" and "outsiders expect hot coffee" both through
    in a measured run. Every rendered search costs the same whatever it returns, so that pair
    spent ten credits to ask one question - and both came back with nothing. Keeping the longer,
    more specific wording is free.
    """
    out = []
    for phrase in phrases:
        value = str(phrase or "").strip()
        if not value:
            continue
        folded = value.casefold()
        if any(folded == x.casefold() for x in out):
            continue
        # A prefix only counts when it ends on a word boundary, so "hot can" does not swallow
        # "hot cans of soup" by accident of spelling.
        shorter = [x for x in out if folded.startswith(x.casefold() + " ")]
        if shorter:
            for x in shorter:
                out[out.index(x)] = value
            continue
        if any(x.casefold().startswith(folded + " ") for x in out):
            continue
        out.append(value)
        if limit and len(out) >= limit:
            break
    return out[:limit] if limit else out


# Camera and manner language, matched as SPANS so the words around them stay in one piece.
# "Aerial drone shot descending over a complex multi-way Japanese intersection" is one camera
# clause followed by the motif; taking the clause out leaves the motif whole, where taking the
# first four words of it left "aerial drone complex multi".
_CAM_ADJ = (r"wide|tight|extreme|low|high|eye[- ]?level|street[- ]?level|ground[- ]?level|aerial|"
            r"overhead|dramatic|cinematic|handheld|static|slow[- ]?motion|real|fast|quick|slow")
# UNAMBIGUOUS: these words are only ever the camera. "Close-up of a road sign" is a road sign;
# nobody sells a close-up. They may be stripped with nothing but a preposition behind them.
_CAM_STRICT = (r"close[- ]?ups?|closeups?|pov|pullback|timelapse|b[- ]?roll|establishing|"
               r"footage|glimpse")
# AMBIGUOUS: each of these is also an ordinary subject - an espresso shot, a river view, a flight
# to Haneda, a pan on a stove, a camera shop. Stripping one of these on the strength of a
# following preposition deleted all five of those searches, so here it takes an adjective in
# front or a camera word behind.
_CAM_NOUN = r"drone|camera|shots?|views?|angles?|clips?|flight|zoom|pan|tilt"
# A PREPOSITION DOES NOT MAKE A CAMERA PHRASE. These two lists were one, so any camera noun
# followed by any preposition counted as camera language and was cut out: "shot of espresso" and
# "views across Tokyo" became nothing, "flight at Haneda airport" lost its flight, "pan on stove
# fried rice" lost its pan. A bare noun now needs a camera WORD behind it - "drone flying over",
# "camera panning" - and prepositions may only follow once that is established.
_CAM_WORD = (r"shot|view|descending|rising|following|showing|revealing|moving|flying|panning|"
             r"tracking|pullback|zooming|circling")
_CAM_PREP = r"of|over|on|at|through|into|down|up|across|onto"
# A camera noun ON ITS OWN is not camera language. "flight attendant", "river view apartment",
# "camera shop in Akihabara" are subjects that happen to contain one of these words, and stripping
# it would delete the thing being searched for. So the noun only counts when the phrase around it
# is a camera phrase: an adjective in front of it ("wide shot"), or a connective behind it
# ("drone flying over", "camera panning over", "view of"). Bare, it is left alone.
# Every alternative ends at a word boundary. Without the trailing \b the tail "at" matched the
# first two letters of "attendant", so "flight attendant serving passengers" was cut to
# "tendant serving passengers" - the regex ate half a word and left a fragment that means nothing.
_CAMERA_CLAUSE = re.compile(
    r"\b(?:"
    # a word that is only ever the camera, with whatever leads into it: "Close-up of",
    # "Dramatic real footage of", "Fast glimpse of"
    rf"(?:(?:{_CAM_ADJ})\b\s+)*(?:{_CAM_STRICT})\b(?:\s+(?:{_CAM_WORD}|{_CAM_PREP})\b)*"
    # led by a camera adjective: "wide shot of", "low angle of", "aerial drone"
    rf"|(?:(?:{_CAM_ADJ})\b\s+)+(?:(?:{_CAM_NOUN})\b\s*)+(?:\s*(?:{_CAM_WORD}|{_CAM_PREP})\b)*"
    # or TWO camera nouns in a row - "drone flight", "camera shot", "drone footage". One is a
    # subject ("camera shop", "river view apartment"); two in sequence is the camera talking.
    rf"|(?:(?:{_CAM_NOUN})\b\s+)+(?:{_CAM_NOUN})\b(?:\s+(?:{_CAM_WORD}|{_CAM_PREP})\b)*"
    # or a bare camera noun followed by a camera WORD: "drone flying over", "camera panning".
    # A preposition alone is not enough - that is what deleted "shot of espresso".
    rf"|(?:{_CAM_NOUN})\b(?:\s+(?:{_CAM_WORD})\b)(?:\s+(?:{_CAM_WORD}|{_CAM_PREP})\b)*"
    r")", re.I)
_MANNER = re.compile(r"\b(?:slowly|quickly|smoothly|continuously|carefully|clearly|visibly|"
                     r"deliberately|instantly|obviously|quietly|dramatically)\b", re.I)
_ARTICLE = re.compile(r"\b(?:a|an|the)\b", re.I)
# Removing a manner word can strand the conjunction that joined it: "operating smoothly and
# continuously without needing electricity" becomes "operating and without needing electricity".
_STRANDED = re.compile(r"\b(?:and|or)\s+(?=(?:with|without|into|onto|over|under|through|by|at|"
                       r"of|in|on|from|to|and|or)\b)", re.I)
# NO WORD IS REFUSED ON ITS OWN. A list here held "pushing", "cutting", "moving", "flying" and
# "circling" and threw away every search that named what happens in the picture: "Japan train
# staff pushing passengers", "chef cutting fish at market" and "cars moving through Japanese
# roundabout" all came back empty. That was the third filter in this file written as "reject
# anything containing X" - `_tidy_phrase` refuses every -ing word, an earlier draft of this one
# used a positive list of meaningful verbs - and all three delete real searches. Camera language
# is only camera language as a connected expression, so `_CAMERA_CLAUSE` takes the expression out
# and whatever was being filmed stays.


def motif_phrase(text):
    """A contract sentence with the camera taken out of it, and nothing else moved.

    Not a summary and not a truncation: every word that describes the SHOT stays, in the order it
    was written. `_intent` gives a bag of words and `_queries` used to take the first three or
    four of it, which on a contract sentence is the camera half - measured on the roundabouts run,
    where all sixteen beats searched things like "aerial drone complex multi" and "street level
    view tiny" while "Japanese intersection" and "traffic lights" sat unused at the end.
    """
    raw = str(text or "").strip()
    if not raw:
        return ""
    raw = _CAMERA_CLAUSE.sub(" ", raw)
    raw = _MANNER.sub(" ", raw)
    raw = _ARTICLE.sub(" ", raw)
    raw = _STRANDED.sub(" ", raw)
    raw = re.sub(r"[^\w\s'\-]", " ", raw)
    return re.sub(r"\s+", " ", raw).strip()


def _tidy_search_phrase(text):
    """Clean a written search phrase WITHOUT shortening it or deleting its verb.

    `_tidy_phrase` refuses any phrase carrying an -ing word or a word in `_ACTION_VERBS`, then
    cuts what survives to five words. That is right for a phrase lifted out of narration and
    wrong for one that was written to be searched: it deleted "customer handing wrappers back to
    the vendor" outright, and cut "blacked-out dead traffic lights power outage" to
    "...lights power". A phrase the planner wrote, or a motif taken from a contract, is used whole.
    """
    phrase = _BAD.sub(" ", str(text or ""))
    # The camera EXPRESSION comes out; the subject it was filming stays. Nothing is rejected.
    phrase = _CAMERA_CLAUSE.sub(" ", phrase)
    phrase = re.sub(r"[^\w\s぀-ヿ㐀-鿿-]", " ", phrase)
    phrase = re.sub(r"\s+", " ", phrase).strip()
    if not phrase:
        return ""
    if _CJK.search(phrase):
        return phrase[:60]
    words = [w for w in phrase.split() if w]
    while words and words[0].casefold() in _LEAD_DROP:
        words.pop(0)
    while words and words[-1].casefold() in _LEAD_DROP:
        words.pop()
    # One word is still a search. "low angle of temple" is a temple; requiring two words threw the
    # whole query away because the camera clause had taken the rest.
    return " ".join(words)


def _queries(scene, title, native_terms=None):
    """Retrieval hypotheses for ONE beat, ordered by how well the shape retrieves.

    Native phrases first when the project has them: on the measured runs they are what returned
    usable footage. Then compact noun phrases naming the subject and the object. Nothing here
    narrates an action, and no platform name is ever put into a search.
    """
    subject = str((scene or {}).get("visual_subject") or "").strip()
    action = str((scene or {}).get("visual_action") or "").strip()
    explicit = (scene or {}).get("search_queries") or (scene or {}).get("queries") or []
    terms = _intent(scene)

    ordered = []
    # `explicit` is handled below by the cleaner that keeps it whole; adding it here as well
    # would send the same phrase through the strict one and get the shortened copy back.
    native_clean = [str(x) for x in (native_terms or []) if str(x).strip()]
    ordered += native_clean
    ordered.append(subject)
    # The sliding window over the brief's words produces things like "before opening commuter"
    # and "sip but the unopened" - word soup that spent real search slots on the failed
    # 2026-09-02 run. It is a last resort, not a peer of a written phrase: when the project has
    # native terms (which measurably retrieve) it is not needed at all.
    #
    # AND NOT WHEN THE BEAT HAS A WRITTEN PHRASE EITHER. On a contract sentence the first three
    # or four of these words are the CAMERA half - "aerial drone complex multi", "street level
    # view tiny", "wide cinematic drone pullback". Counted on the roundabouts run: 12 of the 35
    # queries in the first round were that soup and 11 more were the soup with "insane" glued on,
    # at 125 Scrape.do credits a run. A beat that wrote its own search, or that has a contract to
    # take a motif from, does not need it.
    if not native_clean and not explicit and not motif_phrase(
            (scene or {}).get("required_action")):
        ordered += [" ".join(terms[:3]), " ".join(terms[:4])]
    # The action is kept only as the NOUNS inside it - "a warm can dropping into the tray"
    # becomes "warm can tray", which is a caption, not a sentence.
    if action:
        nouns = [w for w in _WORDS.findall(action)
                 if w.casefold() not in _LEAD_DROP
                 and w.casefold() not in _ACTION_VERBS
                 and not (w.casefold().endswith("ing") and w.casefold() not in _NOUN_ING)]
        if len(nouns) >= 2:
            ordered.append(" ".join(nouns[:4]))

    cleaned = []
    # WRITTEN PHRASES ARE USED AS WRITTEN. A phrase the planner composed to be searched, and the
    # motif taken from the beat's own contract, go through the cleaner that does not shorten them
    # and does not delete their verb. Everything else is still a phrase lifted out of narration
    # and still gets the strict treatment.
    for phrase in explicit:
        tidy = _tidy_search_phrase(phrase)
        if tidy:
            cleaned.append(tidy)
    motif = motif_phrase((scene or {}).get("required_action"))
    if motif:
        tidy = _tidy_search_phrase(motif)
        if tidy:
            cleaned.append(tidy)
    # THE CONTEXT NARROWS A SEARCH, IT NEVER BECOMES EVIDENCE. A year, a cause or a legal status
    # cannot be read off a clip - a roundabout with cars on it looks the same in 2010 and in 2012 -
    # so `search_context` is kept out of `required_action` and out of everything
    # `editorial.evidence_fits` reads. Here it earns its keep: ONE extra narrowed variant beside
    # the unnarrowed phrase, so a search that is too specific cannot take the beat's only query
    # with it. Stored and never read was the previous state, which narrowed nothing at all.
    context = _tidy_search_phrase((scene or {}).get("search_context"))
    for phrase in ordered:
        tidy = _tidy_phrase(phrase)
        if tidy:
            cleaned.append(tidy)
    out = _dedupe(cleaned, limit=6)
    # AFTER the dedupe, and this is the whole point of doing it here. `_dedupe` drops a phrase
    # that is only the opening words of a longer one, so a narrowed variant built by appending the
    # context swallowed the phrase it was narrowing: the beat lost "Japanese roundabout under
    # construction" and kept only "…under construction post-2011 reconstruction zone". Both are
    # wanted - the narrow one to find the exact thing, the wide one because a search that is too
    # specific finds nothing at all.
    if context and out:
        narrowed = _tidy_search_phrase(f"{out[0]} {context}")
        if narrowed and narrowed.casefold() not in {q.casefold() for q in out}:
            out.append(narrowed)
    return out


def _recovery_queries(scene, title, used, native_terms=None):
    """GENUINELY different hypotheses for a beat the first round could not cover.

    The old recovery round returned the first round's phrases with "Japan " glued on the front -
    the same search, paid for twice. These change the ANGLE instead: the place without the
    action, the object alone, and the native terms the first round did not spend.
    """
    spent = {str(x).casefold() for x in (used or ())}
    terms = _intent(scene)
    subject = str((scene or {}).get("visual_subject") or "").strip()
    subject_nouns = [w for w in _WORDS.findall(subject) if w.casefold() not in _LEAD_DROP]

    angles = []
    # 1. unspent native terms - the axis that actually retrieved on every measured run
    angles += [str(x) for x in (native_terms or []) if str(x).strip()]
    # 2. the place/object on its own, without whatever action failed
    if len(subject_nouns) >= 2:
        angles.append(" ".join(subject_nouns[-2:]))
    # NO MORE WORD PAIRS LIFTED OUT OF THE ENGLISH NARRATION. `terms` is the beat's own wording,
    # and gluing two of its nouns together produced searches like "one building" and "tried fit".
    # "one building" is what fetched a tour of Hong Kong's Monster Building into a Short about
    # Japanese convenience stores. A phrase nobody would type into TikTok cannot find footage;
    # it can only find something that happens to share a noun.
    if _CJK.search(" ".join(terms[:4])):
        # Japanese terms are what creators actually tag, so a pair of those is still a search.
        if len(terms) >= 2:
            angles.append(" ".join(terms[-2:]))
        if len(terms) >= 3:
            angles.append(" ".join(terms[:2]))
    out = []
    for phrase in angles:
        tidy = _tidy_phrase(phrase)
        if tidy and tidy.casefold() not in spent:
            out.append(tidy)
    return _dedupe(out, limit=4)


NATIVE_TERMS_PROMPT = """You write TikTok SEARCH PHRASES in the local language of the footage.

TOPIC: {title}
BEATS (one line each, numbered):
{beats}

For every beat write 4-5 search phrases a LOCAL CREATOR would put in their own caption.

RULES, measured on delivered runs:
- Write in the language spoken where the footage is filmed. For anything Japanese, write
  Japanese. Do not transliterate and do not translate to English.
- Name the SUBJECT, the PLACE and the visible OBJECT. A phrase is a caption, never a sentence:
  no verbs of motion, no "a"/"the", no camera words.
- 2 to 5 words. Every winning phrase on the measured runs looked like
  通勤ラッシュ 電車 / 日本 高校生 掃除 教室 / カプセルホテル 大浴場.
- Never write a platform name (TikTok, Reels, Shorts) or the words explainer, reaction, news.
- The phrases for one beat must differ from each other by ANGLE, not by wording.

Return STRICT JSON only:
{{"beats": [{{"beat": 1, "phrases": ["...", "..."]}}]}}"""


def _native_terms_agent(config, scenes, status_cb=None):
    """Per-beat native search phrases, one LLM call per project. [] on any failure."""
    supplied = (config or {}).get("v4_native_terms")
    if isinstance(supplied, list) and supplied:
        return [list(x or []) for x in supplied]
    if not os.environ.get("WAVESPEED_API_KEY"):
        return []
    lines = []
    for index, scene in enumerate(scenes or []):
        brief = " / ".join(x for x in (str((scene or {}).get("visual_subject") or "").strip(),
                                       str((scene or {}).get("visual_action") or "").strip(),
                                       _text(scene)) if x)
        lines.append(f"{index + 1}. {brief[:220]}")
    if not lines:
        return []
    prompt = NATIVE_TERMS_PROMPT.format(title=str((config or {}).get("title") or "")[:120],
                                        beats="\n".join(lines))
    payload = {"model": str((config or {}).get("reasoning_model")
                            or (config or {}).get("wavespeed", {}).get("reasoning_model")
                            or "openai/gpt-5.6-luna"),
               "messages": [{"role": "system",
                             "content": "You write native-language search phrases. JSON only."},
                            {"role": "user", "content": prompt}],
               "temperature": 0.2,
               # One beat costs about 110 tokens of JSON: three native phrases, the quoting and
               # the punctuation. A flat 900 was enough for the two-beat case it was written
               # against and truncated every real project - measured on a 12-beat run, where the
               # reply was cut mid-array, parsed to nothing, and the whole search fell back to
               # searching TikTok for English narration fragments like "thirty seconds before".
               "max_tokens": max(1500, 360 * len(lines)),
               "response_format": {"type": "json_object"}}
    # RETRY, because losing this call costs the whole run its language. post_json_url only
    # retries 429/502/503; a 500, a 504 or a dropped connection raised on the first attempt and
    # the scrape then searched TikTok in English - measured at 72 post URLs against 476 with the
    # native phrases. One failed call is not evidence that the model cannot answer.
    import urllib.error
    parsed, finish, last_error = None, "", None
    for attempt in range(3):
        try:
            import agent_core
            data = agent_core.post_json_url(agent_core.WAVESPEED_LLM_API, payload, timeout=180)
            parsed = agent_core.extract_json_object(data["choices"][0]["message"]["content"])
            finish = str((data.get("choices") or [{}])[0].get("finish_reason") or "")
            last_error = None
            break
        except (OSError, urllib.error.URLError) as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
        except Exception as exc:                                        # noqa: BLE001
            last_error = exc
            break
    if last_error is not None:
        _log(status_cb, f"V4 native search terms unavailable after {attempt + 1} attempt(s) "
                        f"({type(last_error).__name__}); continuing with the brief's own wording.")
        return []
    if finish and finish != "stop":
        # Say why rather than reporting an empty result as if the model had no ideas. A silent
        # "the model returned none" is what hid the truncation above for a whole run.
        _log(status_cb, f"V4 native search terms: the reply stopped early ({finish}).")
    out = [[] for _ in (scenes or [])]
    for row in (parsed or {}).get("beats") or []:
        try:
            index = int(row.get("beat")) - 1
        except (TypeError, ValueError):
            continue
        if not (0 <= index < len(out)):
            continue
        phrases = []
        for phrase in row.get("phrases") or []:
            tidy = _tidy_phrase(phrase)
            if tidy and tidy.casefold() not in {x.casefold() for x in phrases}:
                phrases.append(tidy)
        # Five, not three. A rendered search returns a hard ~12 results whatever the scroll
        # depth - measured here, and confirmed by Scrape.do's own testing (3 scrolls ~14 videos
        # in 28s, 6 scrolls the same ~14 in 38s, 10 scrolls a timeout). So coverage can only come
        # from asking DIFFERENT questions, and the account now has the request budget for them.
        out[index] = phrases[:5]
    found = sum(len(x) for x in out)
    _log(status_cb, f"V4 native search terms: {found} phrase(s) across {len(out)} beat(s)."
         if found else "V4 native search terms: the model returned none; using the brief's wording.")
    return out

_STRIKING_JA = ("ヤバい", "衝撃", "限界", "密着")
_STRIKING_EN = ("insane", "extreme", "chaos", "close up")


def _striking_queries(queries, limit=2):
    """Searches aimed at the most extreme version of a beat's own subject.

    Ranking cannot invent a clip that was never searched for. Measured on a convenience-store run:
    the beats searched their category (`朝 通勤 電車`, `通勤ラッシュ 電車`) and got eleven
    near-identical boarding shots; exactly ONE window in the whole pool was genuinely arresting -
    a station attendant physically pushing commuters into a carriage - and no query had asked for
    anything like it. Appending a native-language intensifier to the beat's OWN term asks for the
    extreme version of the same subject rather than a different subject.

    These are ADDITIVE. Fitting them inside the existing per-beat budget pushed proven terms off
    the end, which an earlier measurement had already shown to be harmful.
    """
    out = []
    for position, query in enumerate(list(queries or [])[:3]):
        query = str(query or "").strip()
        if not query:
            continue
        words = _STRIKING_JA if _CJK.search(query) else _STRIKING_EN
        # Rotate the intensifier: three searches ending in the same word are one search repeated,
        # and they would spend three slots looking in the same place.
        candidate = f"{query} {words[position % len(words)]}"
        if candidate not in out and candidate not in queries:
            out.append(candidate)
        if len(out) >= max(1, int(limit)):
            break
    return out


def _initial_query_round(tasks, per_beat=3):
    """Return the first *diverse* Bright hypotheses for every narrated beat.

    A paid keyword result is not a search-engine page with an endless result
    list.  Asking six near-synonymous phrases for one action and taking the
    first three results from each mostly buys the same handful of explainer
    posts six times.  The first pass therefore gives every beat a subject,
    action and native/explicit hypothesis.  The remaining hypotheses are kept
    for a targeted recovery pass if that beat genuinely has no verified clip.
    """
    per_beat = max(1, int(per_beat or 1))
    first, deferred, seen = [], [], set()
    for _index, _scene, queries in tasks:
        unique = []
        for query in queries:
            key = str(query or "").casefold()
            if key and key not in {x.casefold() for x in unique}:
                unique.append(str(query))
        for pos, query in enumerate(unique):
            # A phrase appearing in two scene briefs is still searched once;
            # it remains mapped to both beats during assignment.
            key = query.casefold()
            if key in seen:
                continue
            seen.add(key)
            (first if pos < per_beat else deferred).append(query)
    return first, deferred


def _merge_bright_grouped(records, grouped, seen_ids):
    """Append unique Bright records and return how many real posts were added."""
    added = 0
    for query, items in (grouped or {}).items():
        for item in items or []:
            platform = str(item.get("_platform") or "tiktok").lower()
            ident = str(item.get("id") or item.get("aweme_id") or "")
            key = (platform, ident)
            if not ident or key in seen_ids:
                continue
            seen_ids.add(key)
            records.append((str(query), item))
            added += 1
    return added


def _clean_instagram_accounts(raw, limit=8):
    """Accept the V4 Instagram collector's profile plan, never URLs from a stale browser."""
    if isinstance(raw, str):
        entries = re.split(r"[,\n]", raw)
    else:
        entries = raw.values() if isinstance(raw, dict) else (raw or ())
    out, seen = [], set()
    for entry in entries:
        handle = str(entry.get("handle") if isinstance(entry, dict) else entry or "").strip()
        if "instagram.com/" in handle:
            handle = handle.split("instagram.com/", 1)[1].split("/", 1)[0].split("?", 1)[0]
        handle = handle.lstrip("@").strip("/ ")
        if not re.fullmatch(r"[A-Za-z0-9._]{1,30}", handle):
            continue
        if handle.casefold() not in seen:
            seen.add(handle.casefold())
            out.append(handle)
        if len(out) >= limit:
            break
    return out


def _authenticated_tiktok_records(requested, status_cb=None):
    """Collect from the user's existing TikTok session without treating Bright as a proxy.

    This is an explicit, temporary V4 source mode.  It never receives a password,
    never attempts to solve a challenge, and makes no attempt to rotate or evade a
    rate limit: an unavailable/limited local session simply returns no records.
    """
    if not getattr(clip_scraper, "tiktok_backend_ready", lambda: False)():
        _log(status_cb, "V4 TikTok session: not connected; Bright discovery remains available.")
        return []
    records, seen = [], set()
    for query in list(requested or [])[:24]:
        try:
            found = clip_scraper.backend_search(query, want=5, status_cb=status_cb,
                                                sort="MOST_LIKED", platforms=["tiktok"])
        except Exception as exc:
            _log(status_cb, f"V4 TikTok session: search failed for {query!r} ({type(exc).__name__}).")
            continue
        for item in found or []:
            if not isinstance(item, dict):
                continue
            entry = dict(item)
            entry["_platform"] = "tiktok"
            entry["_source"] = "authenticated_tiktok"
            sid = str(entry.get("id") or entry.get("aweme_id") or entry.get("webVideoUrl") or "")
            if sid and sid not in seen:
                seen.add(sid); records.append((query, entry))
    _log(status_cb, f"V4 TikTok session: {len(records)} unique locally-authenticated candidates collected.")
    return records


def _instagram_accounts_agent(config, scenes, status_cb=None):
    """Plan a separate Bright Instagram ``url_all_reels`` collection once per project.

    Instagram's Bright Reels dataset cannot search a keyword.  It can only collect all
    reels for an account URL, so this is intentionally a separate source-planning agent
    rather than a fake Instagram keyword search or an old logged-in-browser fallback.
    """
    supplied = _clean_instagram_accounts((config or {}).get("v4_instagram_accounts") or [])
    if supplied:
        _log(status_cb, "V4 Instagram collector: using supplied reel accounts: " + ", ".join(supplied))
        return supplied
    try:
        import agent_core
        beats = []
        for number, scene in enumerate(scenes or [], 1):
            text = _text(scene)
            if text:
                beats.append(f"{number}. {text[:220]}")
            if len(beats) >= 10:
                break
        prompt = (
            "Name up to 6 REAL active Instagram creators whose ordinary REELS contain raw, "
            "vertical phone footage that can visually support this Japanese/Asian factual short. "
            "Do not name official brands, cities, railways, tourism boards, news outlets or celebrities. "
            "Use a handle only when you are confident it exists. The Bright collector will fetch all reels "
            "from each profile, not search Instagram by keyword. Return JSON only: "
            '{"accounts":["handle"]}.\n\nNeeded narration beats:\n' + "\n".join(beats)
        )
        model = str((config or {}).get("v4_vision_model") or DEFAULT_V4_VISION_MODEL)
        data = agent_core.post_json_url(agent_core.WAVESPEED_LLM_API, {
            "model": model,
            "messages": [{"role": "system", "content": "Return only valid JSON."},
                         {"role": "user", "content": prompt}],
            "temperature": 0.0, "max_tokens": 240,
        }, timeout=120)
        raw = str(data["choices"][0]["message"].get("content") or "")
        parsed = agent_core.extract_json_object(raw) or {}
        accounts = _clean_instagram_accounts(parsed.get("accounts") if isinstance(parsed, dict) else [])
        if accounts:
            _log(status_cb, "V4 Instagram collector: planned reel accounts: " + ", ".join(accounts))
        else:
            _log(status_cb, "V4 Instagram collector: no high-confidence accounts; keeping TikTok collection primary.")
        return accounts
    except Exception as exc:
        _log(status_cb, f"V4 Instagram account planning unavailable ({type(exc).__name__}); continuing with TikTok.")
        return []


@dataclass
class Candidate:
    source_id: str
    query: str
    path: str
    start: float
    end: float
    score: float
    reason: str
    status: str
    rejection: str = ""
    source_text: str = ""
    vision: dict | None = None
    fingerprint: str = ""
    # Set when a beat with no query match of its own took this clip on the strength of its
    # vision verdict, so the report says so rather than presenting it as a direct hit.
    borrowed_from: str = ""
    # Share of the frame that measures as burned-in text, or -1.0 when it could not be measured.
    # The vision model's `caption_severity` is an impression; this is the same measurement the
    # removal pass makes, so it decides both rejection and whether the clip needs cleaning.
    caption_share: float = -1.0
    # Share of the window that is flat, near-black filler - a "reply to this comment" layout, a
    # screen recording pasted onto black, a caption slab. Measured from the review frames, which
    # are decoded anyway. 0.0 when it could not be measured, so an unmeasured clip is not punished.
    dead_share: float = 0.0
    # The AI tool mark read off the frames, or "" - a generator watermark is proof the clip was
    # made rather than filmed, and no verdict about the picture can override it.
    synthetic_mark: str = ""
    # A 256-bit signature of the WINDOW's own middle frame, so two moments of one post can be
    # told apart. "" when it could not be measured.
    window_fingerprint: str = ""
    # How much the picture actually MOVES across this window: the smaller of the two differences
    # between the review strip's thirds, in grey levels. A locked-off shelf, a product held still
    # in front of the lens or a frozen screen recording measure near zero and read on screen as a
    # photograph with a caption on it - what the owner keeps calling "Standbilder".
    # Measured on two finished Shorts (2026-09-05): the ten assigned windows of the konbini Short
    # ran 26.5 to 73.1, median 48.1; the couples Short 8.2 to 43.4, median 24.6; and the pool they
    # were chosen from reached down to 4.0. -1.0 when it could not be measured, so an unmeasured
    # window is never punished for it.
    motion: float = -1.0
    # 0-10 from the vision pass: how watchable this moment is, asked SEPARATELY from whether it
    # fits the line. Fit and interest are different questions, and a run that only asks about fit
    # fills a correct video with inert footage. 5 by default so an unscored clip ranks as ordinary
    # rather than as dead. V3 has had this since 2026-08-30; V4 shipped without it.
    visual_interest: float = 5.0
    # Where the source's next hard cut is, in the source's own seconds - the end of the clean
    # stretch this window was slid into. The window is chosen to hold ONE shot; the fit pass that
    # runs later may extend it to cover a longer beat, and without this it would extend straight
    # through the cut the choosing was careful to miss. 0.0 when the cuts were not measured.
    clean_until: float = 0.0


def text_penalty(candidate):
    """How much a clip's burned-in text costs it when two candidates are close.

    The removal pass does not always succeed: measured on a finished Short (2026-09-03), three of
    ten scenes still showed Japanese creator text because the inpainting failed and the original
    was restored - one came out WORSE than it went in, 2.8% to 5.2%. A run with a surplus can
    simply pick a clean clip instead. Capped at 1.2 against a metadata match worth up to 1.5, so a
    clean but weakly-matching clip never beats a strong one; this only decides near-ties.
    """
    share = float(getattr(candidate, "caption_share", -1.0) or 0.0)
    return 0.0 if share <= 0.015 else min(1.2, 0.6 + share * 4.0)


def fill_rank(candidate):
    # OUR verdict outranks the model's. `accept` is what the reviewer claimed about the
    # picture; `status` is what this run concluded after also MEASURING the burned-in
    # text. Ranking on the claim put a window carrying 18% creator text - rejected for
    # exactly that - above two approved windows of the same post at 8% and 2%, and it
    # went into the couples Short (2026-09-04). A clip we rejected is the last resort,
    # never the first choice.
    verdict = candidate.vision if isinstance(candidate.vision, dict) else {}
    share = float(getattr(candidate, "caption_share", -1.0) or 0.0)
    try:
        ceiling = float(__import__("caption_remover").MAX_COVERAGE)
    except Exception:                                           # noqa: BLE001
        ceiling = 0.12
    return (1 if candidate.status == "available" else 0,
            0 if share > ceiling else 1,
            _interest_band(getattr(candidate, "visual_interest", 5.0)),
            float(verdict.get("relevance") or 0),
            -share,
            float(candidate.score or 0))


# Tool marks that AI video generators burn into their output. A clip carrying one is not
# footage somebody filmed, whatever it depicts.
_SYNTHETIC_MARKS = (
    "sora", "runway", "kling", "veo", "pika", "midjourney", "luma", "hailuo",
    "vidu", "seedance", "dreamina", "haiper", "genmo", "pixverse",
    "ai generated", "generated by ai", "made with ai",
)


def synthetic_watermark(frames):
    """The tool mark found in these frames, or "" - evidence a clip was generated, not filmed.

    The reviewer is already told to reject AI-generated imagery and it still accepted, at
    relevance 10, a sushi-train clip with a visible "Sora" watermark (2026-09-04). A watermark is
    not a matter of judgement: it is text on the screen, and the OCR that finds burned-in captions
    can read it.
    """
    try:
        import numpy as np
        if clip_scraper._get_ocr() is None:
            return ""
        for image in frames or ():
            frame = np.asarray(image.convert("RGB"))[:, :, ::-1]
            result, _ = clip_scraper._get_ocr()(frame, use_det=True, use_cls=False, use_rec=True)
            for row in (result or []):
                try:
                    text = str(row[1]).casefold()
                    confidence = float(row[2])
                except (IndexError, TypeError, ValueError):
                    continue
                if confidence < 0.5:
                    continue
                for mark in _SYNTHETIC_MARKS:
                    if mark in text:
                        return str(row[1])[:40]
    except Exception:                                                   # noqa: BLE001
        return ""
    return ""


def dead_frame_share(path, start=None, seconds=2.2):
    """How much of the frame is flat, near-black filler rather than picture.

    Not letterboxing - that is a matte at BOTH edges and `measure_letterbox` handles it. This
    catches the other thing creators do: a "reply to this comment" layout, a screen recording
    pasted onto black, a tall caption slab. Measured on the couples Short (2026-09-04): the hook
    opened on a clip whose top half was black with a Japanese caption bar and the couple squeezed
    into a strip. It is not a matte (the top rows are bright), so nothing measured it, and the
    reviewer accepted it at relevance 8 even though its own prompt lists text cards and screen
    recordings as disqualifiers.
    """
    try:
        import cv2
        import numpy as np
        cap = cv2.VideoCapture(str(path))
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 0) or 30.0
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        first = max(0, int(float(start or 0.0) * fps))
        span = max(1, min(max(0, total - first), int(float(seconds) * fps)))
        shares = []
        for i in range(4):
            cap.set(cv2.CAP_PROP_POS_FRAMES, first + int(span * i / 4))
            ok, frame = cap.read()
            if not ok:
                continue
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            rows = gray.mean(axis=1)
            spread = gray.std(axis=1)
            # A row is dead when it is dark AND flat: a genuinely dark night row still has
            # highlights in it, a filler row does not.
            shares.append(float(((rows < 26) & (spread < 12)).mean()))
        cap.release()
        return float(np.median(shares)) if shares else 0.0
    except Exception:                                                   # noqa: BLE001
        return 0.0


def assignment_rank(score, candidate, is_hook=False):
    """Order candidates for one beat: how well it fits, then whether it is worth watching.

    Nothing in V4 used to ask whether a clip was interesting - only whether it fitted - so a run
    could be entirely correct and entirely inert. The score is banded, so a 7.4 never outranks a
    7.0 that matches the line better, and a dull clip is still taken over leaving the beat empty.

    The hook is a different job from the rest of the video: it has about a second to stop a thumb,
    and a clip that merely fits is a wasted opening. Measured: the train-pushers timeline opened on
    a selfie of two girls while a guard shoving a man into a carriage sat unused in the same pool.
    So beat 0 weighs interest above everything else; later beats use it as a tiebreaker worth about
    as much as a strong metadata match.
    """
    band = _interest_band(getattr(candidate, "visual_interest", 5.0))
    # Filler around the picture costs more in the hook than anywhere else: it is the one shot
    # that has to fill the screen instantly.
    dead = float(getattr(candidate, "dead_share", 0.0) or 0.0)
    # HOW WELL IT CARRIES THE LINE IS THE POINT, and it was not in this sum at all. `score` is
    # 5.0 plus a popularity term plus a metadata bonus; the reviewer's relevance was used only as
    # a gate at >= 7 and then forgotten, so a popular 7 beat a 10. Measured on the school-rules
    # Short (2026-09-04): the payoff line "your final subject is literally cleaning the floor"
    # took a clip of a girl turning on a corridor tap at relevance 7, while SEVENTEEN clips of
    # students actually cleaning the floor sat available - three of them at relevance 10, one
    # being "students kneel on the floor to wipe it clean". Above the gate, each extra point is
    # worth about as much as half a metadata match, so a 10 clears a 7 outright.
    relevance = 0.0
    try:
        relevance = float((candidate.vision or {}).get("relevance") or 0.0)
    except (AttributeError, TypeError, ValueError):
        relevance = 0.0
    # And between two windows that fit equally well, the one where something happens wins. The
    # gate below MIN_WINDOW_MOTION has already thrown out the photographs; this is the difference
    # between a shelf someone walks past and a shelf. Capped, so it can never outweigh relevance:
    # a full point of this is worth about one point of relevance above the gate.
    motion = float(getattr(candidate, "motion", -1.0) or -1.0)
    liveliness = 0.0 if motion < 0 else min(1.0, motion / 45.0)
    return (float(score) - text_penalty(candidate)
            - dead * (6.0 if is_hook else 2.0)
            + max(0.0, relevance - 7.0) * 0.8
            + liveliness * (1.6 if is_hook else 0.8)
            + band * (3.0 if is_hook else 0.8))


def _interest_band(score):
    """Coarse watchability band: 2 = worth stopping for, 1 = ordinary, 0 = inert.

    Banded deliberately. Sorting on the raw score would let a 7.4 outrank a 7.0 that matches the
    line better, and the vision pass cannot tell those apart. Band 0 sorts last but is never
    discarded: a hole in the video is worse than a dull shot.
    """
    try:
        value = float(score)
    except (TypeError, ValueError):
        return 1
    if value != value:                                                  # NaN
        return 1
    if value >= 8.0:
        return 2
    if value >= 5.0:
        return 1
    return 0


def _interest_of(decision):
    """The vision pass's 0-10 watchability score, clamped; 5 when it did not answer."""
    try:
        value = float((decision or {}).get("visual_interest"))
    except (TypeError, ValueError):
        return 5.0
    if value != value:
        return 5.0
    return max(0.0, min(10.0, value))


def _unlocker_video_bytes(response, item):
    """Normalize Bright's raw-byte and documented JSON-envelope response shapes."""
    payload = response.content
    try:
        envelope = response.json()
    except ValueError:
        envelope = None
    if not isinstance(envelope, dict) or "body" not in envelope:
        return payload
    target_status = int(envelope.get("status_code") or 0)
    target_headers = envelope.get("headers") if isinstance(envelope.get("headers"), dict) else {}
    target_type = str(target_headers.get("content-type") or "").lower()
    body = envelope.get("body")
    if isinstance(body, str):
        # Binary data may arrive base64-wrapped; only use that decoding when it
        # contains a recognizable MP4 box. Otherwise preserve byte identity.
        decoded = b""
        try:
            candidate = base64.b64decode(body, validate=True)
            if b"ftyp" in candidate[:64]:
                decoded = candidate
        except Exception:
            pass
        payload = decoded or body.encode("latin-1", errors="ignore")
    elif isinstance(body, list):
        payload = bytes(body)
    elif body is None:
        payload = b""
    if target_status and not (200 <= target_status < 300):
        item["_bright_download_error"] = f"target returned HTTP {target_status}"
        return b""
    if target_type and "video" not in target_type and payload:
        item["_bright_download_error"] = f"target returned {target_type}, not video"
        return b""
    return payload


def _opencv_probe(path):
    """Read geometry and duration by decoding, with no external process involved."""
    try:
        import cv2
        cap = cv2.VideoCapture(str(path))
        try:
            width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
            height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
            fps = float(cap.get(cv2.CAP_PROP_FPS) or 0)
            frames = float(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
            return width, height, (frames / fps if fps > 0 else 0.0)
        finally:
            cap.release()
    except Exception:
        return 0, 0, 0.0


def _probe(path, ffprobe=None):
    """(width, height, duration), plus the reason the fast path gave up.

    ffprobe used to be the ONLY answer whenever a path to it existed, and every failure of it -
    a timeout, a non-zero exit, an unparsable body - collapsed to (0, 0, 0.0). validate_media then
    reported "no readable video stream", which reads like a verdict about the FILE. Measured on
    the train-pushers run 2026-09-03: 126 of 190 downloads were thrown away with that reason, and
    every one of the 16 re-checked afterwards was a perfectly good vertical video. A whole run
    found nothing because one subprocess call misbehaved.

    So ffprobe is now the fast path, not the authority. If it does not answer, the decoder that
    the motion gate uses anyway gets to decide, and the ffprobe failure is carried out as text so
    the rejection can name what actually happened instead of blaming the download.
    """
    note = ""
    if ffprobe:
        try:
            # stdin is inherited otherwise, and this process is often started with its stdin
            # already closed or consumed - a child that reads it can then block until the timeout.
            raw = subprocess.check_output(
                [ffprobe, "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=width,height,duration", "-of", "json", str(path)],
                text=True, timeout=30, stdin=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL)
            stream = (json.loads(raw).get("streams") or [{}])[0]
            width = int(stream.get("width") or 0)
            height = int(stream.get("height") or 0)
            duration = float(stream.get("duration") or 0)
            if width > 0 and height > 0:
                return width, height, duration
            note = "ffprobe reported no video stream"
        except subprocess.TimeoutExpired:
            note = "ffprobe timed out"
        except subprocess.CalledProcessError as exc:
            note = f"ffprobe exited {exc.returncode}"
        except Exception as exc:                                            # noqa: BLE001
            note = f"ffprobe failed ({type(exc).__name__})"
    width, height, duration = _opencv_probe(path)
    if width > 0 and height > 0 and note:
        _PROBE_FALLBACKS.append(note)
    return width, height, duration


# A rejection that describes the FETCH, not the footage. These are worth another attempt; a
# vertical-geometry, motion or vision rejection is a judgement about the post and is final.
_TECHNICAL_FAILURES = (
    "could not be downloaded", "could not deliver usable media", "corrupt download",
    "incomplete download", "download produced no file", "unreadable download",
    "local inspection failed", "parallel media gate failed", "denied direct download",
)

# Every time the decoder rescues a file ffprobe would have discarded, the run should be able to
# say so afterwards rather than leaving it invisible.
_PROBE_FALLBACKS = []


def validate_media(path, ffprobe=None, min_bytes=64 * 1024):
    """Is this file a real, decodable video? Returns (ok, reason, width, height, duration).

    `_probe` collapses EVERY failure to (0, 0, 0.0), and the caller's next line asks
    `h < w or h < 800` - so a truncated file, an HTML error body saved as .mp4, or a download
    with no video stream all came back as "not native vertical". That reading is wrong and it
    hid the real failure across the V4 smoke runs.

    The checks run cheapest-first: size, then container/stream metadata, then an actual decode.
    A file that fails any of them is not footage and must never reach vision, timeline or render.
    """
    try:
        file_path = Path(path)
        if not file_path.is_file():
            return False, "download produced no file", 0, 0, 0.0
        size = file_path.stat().st_size
        if size < int(min_bytes):
            return False, f"incomplete download ({size} bytes)", 0, 0, 0.0
        with open(file_path, "rb") as handle:
            head = handle.read(4096)
        lowered = head.lower()
        if b"<html" in lowered or b"<!doctype" in lowered or lowered[:1] == b"{":
            # An error page or a JSON body saved under a .mp4 name. Nothing to probe.
            return False, "corrupt download (server sent a page, not a video)", 0, 0, 0.0
        # 64 bytes was too small a window and MP4 is not the only container a CDN serves: 16% of
        # the downloads in a measured run were called "no MP4 container header" and thrown away
        # without anyone asking ffprobe. A missing signature is now a suspicion, not a verdict -
        # if the file decodes, it is footage. `header_unknown` only changes the wording of the
        # rejection if the probe below also finds nothing.
        header_unknown = not any(sig in head for sig in
                                 (b"ftyp", b"moov", b"mdat", b"\x1aE\xdf\xa3", b"FLV", b"RIFF"))
    except OSError as exc:
        return False, f"unreadable download ({type(exc).__name__})", 0, 0, 0.0

    width, height, duration = _probe(path, ffprobe)
    # `<= 0`, not `not width`: without ffprobe the OpenCV path returns -1 for an unreadable
    # file, and `not -1` is False - so a truthiness test let that straight through.
    if width <= 0 or height <= 0:
        # Say what actually arrived. "no MP4 container header" told us nothing about the 16% of
        # downloads it was rejecting, so the first bytes go into the reason.
        if header_unknown:
            return False, f"corrupt download (unknown container: {head[:8].hex()})", 0, 0, 0.0
        return False, "corrupt download (no readable video stream)", 0, 0, 0.0
    if duration <= 0.2:
        return False, f"corrupt download (no real duration: {duration:.2f}s)", width, height, duration

    # Metadata can survive in a file whose frames do not. Decode a couple to be sure.
    try:
        import cv2
        capture = cv2.VideoCapture(str(path))
        try:
            decoded = 0
            for _ in range(3):
                ok, frame = capture.read()
                if not ok or frame is None:
                    break
                decoded += 1
        finally:
            capture.release()
        if decoded < 2:
            return False, "corrupt download (frames do not decode)", width, height, duration
    except Exception:                                                   # noqa: BLE001
        # A missing/broken OpenCV must not fail an otherwise sound file; the metadata checks
        # above already rejected the shapes that actually appeared in the smoke runs.
        pass
    return True, "", width, height, duration


def _frame_matte(frame, tighten=False):
    """Measured (top, bottom) black-bar depth of one grayscale frame, or None when it is
    not letterboxed.

    Split out of _motion_and_black so the repair pass rebuilds exactly the matte the gate
    measured - a second, differently-tuned detector would crop a different number of rows
    than the one that rejected the clip.
    """
    height = frame.shape[0]
    rows = frame.mean(axis=1)
    dark = rows < 18
    if not bool(dark[0]) or not bool(dark[-1]):
        return None
    # Creators print a caption ON the matte, and the first bright row is then the text,
    # not the picture. Walk inward for as long as the strip stays overwhelmingly dark so
    # a line of type cannot make a 300px bar measure 50.
    def _matte(flags):
        limit, deepest, seen = int(height * .45), 0, 0
        for index in range(limit):
            if flags[index]:
                seen += 1
            if seen >= (index + 1) * .85:
                deepest = index + 1
        return deepest

    top, bottom = _matte(dark), _matte(dark[::-1])
    floor = max(6, int(height * .03))
    if top < floor or bottom < floor or top + bottom >= height:
        return None
    if float(rows[top:height - bottom].mean()) <= 32:
        return None
    if tighten:
        # The caption-tolerant walk deliberately overshoots: it keeps going while 85% of the
        # rows so far are dark, so a 200px bar over a bright picture measures 235. That is the
        # right bias for a yes/no verdict and the wrong one for a crop - it would eat 35 rows
        # of real picture. Pull each edge back to the last row that is actually dark.
        def _tighten(flags, depth):
            while depth > 0 and not bool(flags[depth - 1]):
                depth -= 1
            return depth

        top, bottom = _tighten(dark, top), _tighten(dark[::-1], bottom)
    return top, bottom


def _sampled_gray_frames(path, count=6, start=None, end=None):
    """Evenly spaced grayscale frames - the sampling both the gate and the repair use.

    `start`/`end` restrict the sampling to the SECONDS the edit will actually show. A matte is
    not always a property of the whole post: a montage can be full-bleed for ten seconds and then
    sit a 16:9 insert inside black bars, and measuring the whole file reports no matte at all.
    Measured on the couples Short (2026-09-04): the hook rendered with thick bands top and bottom
    while measure_letterbox() answered None, because the bars live only in the window that was cut.
    """
    import cv2
    cap = cv2.VideoCapture(str(path))
    frames = []
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 0) or 30.0
    first = 0
    last = max(0, total - 1)
    if start is not None:
        first = max(0, min(last, int(float(start) * fps)))
    if end is not None:
        last = max(first, min(last, int(float(end) * fps)))
    span = max(1, last - first)
    for i in range(count):
        cap.set(cv2.CAP_PROP_POS_FRAMES, first + int(span * i / count)); ok, frame = cap.read()
        if ok: frames.append(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY))
    cap.release()
    return frames


def measure_letterbox(path, start=None, end=None):
    """(top, bottom, height, width) of a persistent matte, or None when the clip has none.

    Pass `start`/`end` to ask about the window the edit will show rather than the whole post.

    Same frames, same per-frame measurement and same "most frames agree" consensus the
    rejection in _motion_and_black uses; the depths reported are the shallowest each edge
    measured across the sampled frames, so a caption that appears halfway through cannot
    make the crop eat picture that is visible in the other frames.
    """
    try:
        frames = _sampled_gray_frames(path, start=start, end=end)
        if len(frames) < 3:
            return None
        mattes = [(frame.shape[0], frame.shape[1], _frame_matte(frame, tighten=True))
                  for frame in frames]
        seen = [(h, w, m) for h, w, m in mattes if m]
        if len(seen) < max(3, len(frames) - 1):
            return None
        top = min(m[0] for _h, _w, m in seen)
        bottom = min(m[1] for _h, _w, m in seen)
        height, width = seen[0][0], seen[0][1]
        return top, bottom, height, width
    except Exception:                                                   # noqa: BLE001
        return None


def measure_dead_bands(path, start=None, end=None, count=6):
    """(top, bottom, height, width) of flat black bands at EITHER edge, or None when there are none.

    `measure_letterbox` deliberately requires a matte at BOTH edges - that is what letterboxing is.
    Creators also ship one-sided bands: a caption slab above the picture, a UI strip below it, a
    16:9 insert pushed to one side. Measured in the finished couples Short (2026-09-04): a 463px
    band along the bottom at 3.5s and a 477px band along the TOP at 12s, each a quarter of a
    1920-high frame. Neither is letterboxing, so nothing measured them, and a quarter of the
    screen rendered dead.

    A band counts only when it is dark AND flat across the sampled frames: a dark night sky has
    highlights in it and varies from frame to frame, a filler band does not.
    """
    try:
        import numpy as np
        frames = _sampled_gray_frames(path, count=count, start=start, end=end)
        if len(frames) < 3:
            return None
        tops, bottoms = [], []
        for frame in frames:
            rows = frame.mean(axis=1)
            spread = frame.std(axis=1)
            dead = (rows < 26) & (spread < 12)
            limit = int(len(rows) * .45)
            top = 0
            while top < limit and dead[top]:
                top += 1
            bottom = 0
            while bottom < limit and dead[len(rows) - 1 - bottom]:
                bottom += 1
            tops.append(top)
            bottoms.append(bottom)
        height, width = frames[0].shape[0], frames[0].shape[1]
        floor = max(6, int(height * .04))

        def settle(depths):
            """The depth to crop, or 0 when this edge has no band worth calling one.

            Taking the minimum across every frame was too strict: a band that is absent from a
            single sampled frame - a flash, a cut inside the window - collapsed the answer to
            zero, and a quarter-height bar went to screen unmeasured. A band counts when MOST
            frames show one, and then the shallowest of those frames decides the crop, so a
            deeper bar elsewhere can never eat picture that some frame actually shows.
            """
            real = [d for d in depths if d >= floor]
            if len(real) < max(3, int(len(depths) * .6)):
                return 0
            return min(real)

        top = settle(tops)
        bottom = settle(bottoms)
        if not (top or bottom):
            return None
        return top, bottom, height, width
    except Exception:                                                   # noqa: BLE001
        return None


def unletterbox_clip(path, ffmpeg=None, min_content_share=0.40, status_cb=None,
                     start=None, end=None):
    """Rebuild a letterboxed clip as a full-frame 9:16 file: sharp cropped content centred
    on a blurred, zoomed copy of itself - the standard social treatment.

    Measured on the fish run 2026-09-03: 49 of 300 downloaded sources were discarded as
    letterboxed. The picture inside those bars is fine; only the container is wrong, so
    rebuilding them is footage supply we already paid to download.

    Returns (repaired_path, note). repaired_path is None when the clip is not letterboxed,
    when too little of the frame is real picture to be worth rebuilding, or when ffmpeg fails.
    """
    src = Path(path)
    # Both edges first (classic letterboxing), then either edge on its own.
    measured = measure_letterbox(src, start=start, end=end) or         measure_dead_bands(src, start=start, end=end)
    if not measured:
        return None, "no dead band measured"
    top, bottom, height, width = measured
    content = height - top - bottom
    share = content / float(height or 1)
    if share < min_content_share:
        # Mostly bar: what is left is a thin strip that has to be blown up past the point
        # where it reads as picture, so keep rejecting these instead of shipping a smear.
        return None, (f"content is only {share:.0%} of the frame "
                      f"(bars {top}px/{bottom}px of {height}px) - not worth rebuilding")
    if ffmpeg is None:
        ffmpeg, _probe = clip_scraper._ffmpeg_tools()
    if not ffmpeg:
        return None, "no ffmpeg available to rebuild the frame"
    # Distinct, collision-proof name next to the proxy: this repo has shipped renders that
    # read the wrong file because two derived clips agreed on a basename (speed_/capblur_/
    # replaced_ all hash for the same reason).
    import hashlib
    key = hashlib.md5(f"{src.name}|{top}|{bottom}|{height}|{width}".encode("utf-8")).hexdigest()[:10]
    out = src.with_name(f"unbox_{src.stem[:24]}_{key}.mp4")
    if out.exists() and out.stat().st_size > 4096:
        return out, f"reused rebuilt frame (bars {top}px/{bottom}px of {height}px)"
    # Even radii only; boxblur rejects a radius wider than half the plane, and the chroma
    # planes are half width, so keep the chroma radius half the luma one.
    radius = max(2, min(int(width / 12), int(width / 2) - 1))
    graph = (f"[0:v]crop={width}:{content}:0:{top},setsar=1,split=2[sharp][bg];"
             f"[bg]scale=-2:{height}:flags=bicubic,crop={width}:{height},"
             f"boxblur=luma_radius={radius}:luma_power=2:chroma_radius={max(1, radius // 2)}:"
             f"chroma_power=1,eq=brightness=-0.06:saturation=1.05[bgb];"
             f"[bgb][sharp]overlay=(W-w)/2:(H-h)/2:format=auto,format=yuv420p[v]")
    cmd = [ffmpeg, "-y", "-loglevel", "error", "-i", str(src),
           "-filter_complex", graph, "-map", "[v]", "-map", "0:a?",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
           "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(out)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except Exception as exc:                                            # noqa: BLE001
        return None, f"rebuild failed: {type(exc).__name__}"
    if proc.returncode != 0 or not out.exists() or out.stat().st_size < 4096:
        try:
            out.unlink(missing_ok=True)
        except OSError:
            pass
        detail = (proc.stderr or "").strip().splitlines()[-1:] or [""]
        return None, f"rebuild failed: {detail[0][:160]}"
    return out, (f"filled {top}px/{bottom}px black bars of a {width}x{height} frame with a "
                 f"blurred copy; {share:.0%} of the frame was real picture")


def _motion_and_black(path):
    """Cheap local proof of moving, native full-frame vertical footage.

    A vertical container with horizontal content inside it is not valid Short
    material. It must be rejected before vision review rather than cropped or
    silently made to look 9:16 by the renderer.
    """
    try:
        import cv2
        import numpy as np
        cap = cv2.VideoCapture(str(path)); frames = []
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        for i in range(6):
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(total * i / 6)); ok, frame = cap.read()
            if ok: frames.append(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY))
        cap.release()
        if len(frames) < 3: return False, "could not decode enough frames"
        means = [float(x.mean()) for x in frames]
        if sum(m < 10 for m in means) >= 2: return False, "black frames"
        # Persistent near-black bands at both top AND bottom with visible middle
        # content are letterboxing, not a dark scene. Use several sampled frames
        # so a brief fade cannot falsely reject a native nighttime clip.
        # MEASURE the matte instead of sampling a fixed slice of the frame. A 10% band is only
        # the right window when the bars happen to be about a tenth of the height: the clip that
        # slipped into the train-pushers timeline had 96px on top and 46px underneath a 1280-high
        # frame, so the fixed band swallowed real picture at both ends, the means rose above the
        # threshold, and visibly letterboxed footage passed. Walk in from each edge and find where
        # the darkness actually stops.
        # The per-frame measurement lives in _frame_matte so the repair pass crops exactly
        # the rows this gate counted.
        bars = sum(1 for frame in frames if _frame_matte(frame))
        if bars >= max(3, len(frames) - 1):
            return False, "persistent top-and-bottom black bars / letterboxed source"
        diffs = [float(cv2.absdiff(a, b).mean()) for a, b in zip(frames, frames[1:])]
        if max(diffs, default=0) < 1.1: return False, "static or repeated frames"
        return True, ""
    except Exception:
        return False, "local visual probe failed"


def _source_fingerprint(path):
    """Tiny perceptual signature used to reject near-identical reposts.

    Separate post IDs are not proof of separate footage: social platforms are
    full of identical reposts.  This deliberately catches only near-identical
    frames, not semantically similar but legitimately distinct clean-up shots.
    """
    try:
        import cv2
        cap = cv2.VideoCapture(str(path))
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, total // 2))
        ok, frame = cap.read()
        cap.release()
        if not ok:
            return ""
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        gray = cv2.resize(gray, (8, 8), interpolation=cv2.INTER_AREA)
        mean = float(gray.mean())
        bits = "".join("1" if int(value) >= mean else "0" for value in gray.flatten())
        return f"{int(bits, 2):016x}"
    except Exception:
        return ""


def _fingerprints_are_reposts(left, right, max_bit_delta=5):
    if not left or not right or len(left) != len(right):
        return False
    try:
        return (int(left, 16) ^ int(right, 16)).bit_count() <= max_bit_delta
    except ValueError:
        return False


def _pick_coverage_fill(shelf, wanted_queries, used_sources, used_ranges, used_fingerprints,
                        need=0.0, scene=None, allow_reviewed_fallback=False):
    """Choose the clip that fills an uncovered beat: (pick, repeats_earlier_shot).

    Production passes ``scene`` and therefore gets the line-specific, fail-closed behavior.
    Without a scene this remains a generic ranking primitive for diagnostic callers; it must
    never be used to assign a timeline beat.

    A DIFFERENT TIME WINDOW IS NOT A DIFFERENT PICTURE, and a different post id is not a
    different video. Measured on japanese_convenience_stores_look_effortless_but_the: scenes
    08, 10 and 12 of the finished Short are the same snack shelf. tiktok__7379470337752173841
    was filled twice (windows 1.56-4.31 and 9.56-12.31, both the same static shelf) and
    tiktok__7108113828465904897 is a repost of it carrying the same fingerprint 0e08080e1f3f3f.
    The ranking loop already consulted used_fingerprints; this path never did, and it also
    never recorded what it had used, so every fill after the first was blind.

    Within the contract-approved set the order remains the beat's own footage, an unused source,
    then another distinct window. When the review budget is exhausted, the caller may opt into a
    technically accepted, line-reviewed fallback. It remains marked for replacement rather than
    pretending it proved the beat's full editorial contract.
    """
    def _free(cand):
        return not any(cand.source_id == sid
                       and not (cand.end <= a - .45 or cand.start >= b + .45)
                       for sid, a, b in used_ranges)

    def _repeats(cand):
        """Is this the same PICTURE as something already on screen?

        The fingerprint is taken once per source file, so every window of a post carries the same
        one. Testing it blindly made "another window of this beat's own source" - the whole point
        of the first and third preferences - look like a repost of itself, and those two branches
        could never fire. A second window of a source we are already using is judged by `_free`,
        which is what separates two moments in time; the fingerprint is here to catch the OTHER
        thing, the same footage re-uploaded under a different post id.
        """
        mine = getattr(cand, "window_fingerprint", "") or ""
        for sid, source_fp, window_fp in used_fingerprints:
            # Same post: only the WINDOW decides. Two moments of one video are different
            # pictures; two windows of a locked-off static shot are not, and that is exactly
            # how the konbini Short ended up with three identical shelves.
            if sid == cand.source_id:
                if not (mine and window_fp):
                    # No signature for one of them, so there is no evidence the picture differs.
                    # The konbini Short shipped three identical shelf shots taken from two windows
                    # of one static post; without a measurement, assume that case.
                    return True
                if _fingerprints_are_reposts(mine, window_fp, max_bit_delta=12):
                    return True
                continue
            # A different post carrying the same footage is a repost, whichever window it is.
            if cand.fingerprint and source_fp and                     _fingerprints_are_reposts(cand.fingerprint, source_fp):
                return True
            if mine and window_fp and _fingerprints_are_reposts(mine, window_fp,
                                                                max_bit_delta=12):
                return True
        return False

    def _sound(cand):
        """Only footage this run actually approved may be a preferred fill.

        The shelf holds every candidate whose file is on disk, including ones rejected as not
        vertical, letterboxed, static, AI-watermarked or mostly filler - those files are never
        deleted. The second preference asked only whether the SOURCE was unused, so once the
        accepted pool was spent it handed a beat a gate-rejected clip in preference to an
        approved second window of a source already on screen.
        """
        return cand.status == "available"

    def _covers(cand):
        """Is this window long enough to hold the beat without running past its own shot?

        The matched path has always refused a window shorter than the beat (see V4_STRETCH_FLOOR
        at the assignment loop); this path had no length test at all. A window is chosen to end
        where the source's next hard cut is, so a beat that plays past its window plays straight
        through that cut - the exact thing the window was picked to avoid. Measured on the
        eating-walk Short: 8 of 15 scenes tripped `hidden_source_cut` at their saved in-points,
        and every one of them was a coverage fill whose window ended at the cut while the scene
        played on for another second or two.
        """
        if need <= 0:
            return True
        try:
            return float(cand.end) - float(cand.start) >= need * V4_STRETCH_FLOOR - .01
        except (TypeError, ValueError):
            return True

    wanted = wanted_queries or set()
    contract_only = scene is not None
    exact_contract = False
    if contract_only:
        exact = [cand for cand in shelf
                 if _sound(cand) and (cand.vision or {})
                 and editorial.evidence_fits(scene, cand.vision or {})
                 and _window_covers_scene(scene, cand, need)]
        if exact:
            shelf = exact
            exact_contract = True
        elif allow_reviewed_fallback:
            shelf = [cand for cand in shelf
                     if _sound(cand) and (cand.vision or {}) and _covers(cand)]
        else:
            return None, False

    def _first(where, covers_first=True):
        """The best candidate `where` allows: one long enough for the beat if there is one.

        LENGTH DECIDES INSIDE A TIER, NEVER ACROSS TIERS. The first version of this ran all three
        tiers with the length test and then all three again without it, which quietly reversed
        the order this function exists to enforce: an unrelated but longer window outranked the
        beat's OWN footage. The order of preference is the point - a second moment from a source
        this beat itself found is still about this beat, a stranger never is - and length is a
        tie-break within it.
        """
        for covers in ((_covers, lambda _c: True) if covers_first else (lambda _c: True,)):
            found = next((c for c in shelf if where(c) and covers(c)), None)
            if found is not None:
                return found
        return None

    pick = (_first(lambda c: _sound(c) and c.query.casefold() in wanted and _free(c)
                   and not _repeats(c))
            or _first(lambda c: _sound(c) and c.source_id not in used_sources and _free(c)
                      and not _repeats(c))
            or _first(lambda c: _sound(c) and _free(c) and not _repeats(c)))
    if pick is not None:
        return pick, not exact_contract
    if contract_only:
        return None, False
    # Nothing approved is left. A hole is still worse than a dull shot, so take the best thing
    # on the shelf - but the caller marks the beat for replacement when it does. This tier had no
    # length test either, and it is not the rare one: on the eating-walk run 7 of the 12 fills
    # came from here, so the rule the tiers above enforce was missing from the path most fills
    # actually take.
    pick = (_first(lambda c: _free(c) and not _repeats(c))
            or _first(lambda c: _free(c))
            or next(iter(shelf), None))
    return pick, pick is not None


def _fit_clip_to_beat(scene, window_seconds, need_seconds):
    """Slow a short clip to cover its beat. Returns the speed applied, or 0.0 for none.

    `window_seconds` is what the chosen source window actually supplies; `need_seconds` is what
    the narration beat is long. Equal or longer needs nothing.
    """
    try:
        window_seconds = float(window_seconds or 0.0)
        need_seconds = float(need_seconds or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if need_seconds <= 0 or window_seconds <= 0 or window_seconds + 0.05 >= need_seconds:
        return 0.0
    speed = round(max(V4_STRETCH_FLOOR, window_seconds / need_seconds), 4)
    scene["timeline_speed"] = speed
    scene["timeline_speed_src"] = str(scene.get("clip") or "")
    return speed

def _next_cut_after(when, cuts, duration):
    """Where this shot ends: the first hard cut after `when`, or the end of the source."""
    later = [float(c) for c in (cuts or []) if float(c) > float(when) + 0.05]
    return round(min(later) if later else float(duration), 3)


def _clean_gaps(duration, cuts, required):
    """Stretches of the source with no hard cut in them, long enough to hold one beat."""
    marks = [0.0] + sorted(float(c) for c in (cuts or []) if 0.0 < float(c) < duration) + [duration]
    return [(a, b) for a, b in zip(marks, marks[1:]) if (b - a) >= required + .2]


def _windows(duration, required, cuts=None, longest=None):
    """Candidate windows inside one source, spread out so they do not look like the same shot.

    A long post used to offer two windows at 8% and 49%. When the run finds fewer sources than
    the edit has beats - measured on a 30s Fact Short: 7 distinct accepted sources against 14
    beats - a second, genuinely different moment from a source that already matched the topic is
    worth far more than a clip pulled in from an unrelated beat. So a long source offers a third
    window near its end, and the separation test uses real seconds rather than one shot length.
    """
    required = max(MIN_SHOT, min(MAX_SHOT, required))
    # A BEAT LONGER THAN THE CADENCE STILL NEEDS A WINDOW. MIN_SHOT/MAX_SHOT describe the
    # intended rhythm, not a ceiling on what a source may offer, and every window was being
    # cut to at most 2.75s while assignment rejects anything under `beat x 0.84`. Measured:
    # a 40s source with no cuts at all could not cover a 3.02s beat, because its longest
    # candidate was 2.30s. Two beats of the walking/eating Short sat above that line. The
    # growth pass that used to serve them is gone - deliberately, because it extended into
    # seconds nobody had reviewed - so the length has to be asked for UP FRONT, where the
    # reviewer still sees the whole of it.
    longest = max(float(longest or 0.0), required)
    if duration < .6:
        return []
    # Enumerate actual shots across the ENTIRE source. Short clean shots are useful
    # candidates too; the assignment must check whether they can cover its beat.
    marks = [0.0] + sorted(set(float(c) for c in (cuts or []) if 0 < c < duration)) + [duration]
    windows = []
    for left, right in zip(marks, marks[1:]):
        available = right - left - .10
        if available < .6:
            continue
        # The cadence length, and - when the shot is long enough to hold it - one at the
        # longest beat this run has to fill, so long beats have a candidate at all.
        lengths = [min(required, available)]
        if longest > required + .05 and available >= longest:
            lengths.append(longest)
        for length in lengths:
            # Head/middle/tail of each shot exposes actions the old 8/49/80% missed.
            for start in (left + .05, (left + right - length) / 2, right - length - .05):
                if not any(abs(start - a) < max(.3, length * .6) and abs(length - (b - a)) < .05
                           for a, b in windows):
                    windows.append((round(start, 3), round(start + length, 3)))
    # Bounded review cost with deterministic coverage of long sources.
    if len(windows) > 18:
        windows = [windows[round(i * (len(windows) - 1) / 17)] for i in range(18)]
    return windows


def _source_text(item):
    """Metadata is weak evidence, but it catches obviously unrelated records.

    Bright can return a lifestyle clip for a broad travel query.  If its own
    post text contains none of the scene's concrete nouns/actions, that record
    is not allowed to fill the scene merely because it is vertical and moving.
    """
    parts = [str((item or {}).get(key) or "") for key in ("desc", "caption", "title")]
    tags = (item or {}).get("hashtags") or []
    if isinstance(tags, list):
        parts.extend(str(tag) for tag in tags)
    return " ".join(parts).strip()


def _metadata_relevance(scene, query, item):
    terms = {x.lower() for x in _intent(scene) if len(x) >= 4}
    text = _source_text(item).lower()
    hits = sorted(term for term in terms if term in text)
    # The supplied search query is the first and most important binding.  Text
    # overlap is a second guard only: empty TikTok descriptions remain eligible,
    # but an explicitly unrelated description is not silently assigned.
    unrelated = bool(text) and len(terms) >= 2 and not hits
    return hits, unrelated


# A retry is another paid multimodal request. A failed window leaves a visible slot rather than
# silently spending the budget three times on the same strip.
VISION_ATTEMPTS = 1
VISION_REQUEST_LIMIT = 120
_VISION_LEDGER_LOCK = threading.Lock()


class VisionBudgetExceeded(RuntimeError):
    pass


def _reserve_vision_request(strip_path, model, key):
    """Persist each attempted request BEFORE submission, including retries/recovery."""
    folder = Path(strip_path).parent
    if folder.name == "continuations":
        folder = folder.parent
    ledger = folder / "v4_vision_requests.jsonl"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with _VISION_LEDGER_LOCK:
        used = len(ledger.read_text(encoding="utf-8").splitlines()) if ledger.exists() else 0
        if used >= VISION_REQUEST_LIMIT:
            raise VisionBudgetExceeded(
                f"Vision request limit reached ({used}/{VISION_REQUEST_LIMIT}); "
                "stopping paid reviews. Existing results are preserved.")
        with ledger.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"time": time.time(), "model": model, "key": key,
                                     "attempt": used + 1, "max_output_tokens": 1024,
                                     "reasoning_mode": "minimal" if "gemini" in str(model).lower() else None}) + "\n")


def _vision_budget_spent(project):
    ledger = Path(project) / "review" / "v4_vision_requests.jsonl"
    with _VISION_LEDGER_LOCK:
        used = len(ledger.read_text(encoding="utf-8").splitlines()) if ledger.exists() else 0
    return used >= VISION_REQUEST_LIMIT


def _collect_vision_jobs(review_jobs, model, status_cb=None):
    results = {}
    exhausted = False
    with ThreadPoolExecutor(max_workers=min(8, max(1, len(review_jobs))),
                            thread_name_prefix="v4-vision") as pool:
        futures = {pool.submit(vision_verdict, job, model): job[0] for job in review_jobs}
        for future in as_completed(futures):
            key = futures[future]
            if future.cancelled():
                results[key] = (key, "", "VisionBudgetExceeded")
                continue
            try:
                results[key] = future.result()
            except VisionBudgetExceeded:
                results[key] = (key, "", "VisionBudgetExceeded")
                if not exhausted:
                    exhausted = True
                    _log(status_cb, "V4: vision budget reached; finishing active reviews and assigning saved results.")
                    for pending in futures:
                        pending.cancel()
            except _balance_error():
                for pending in futures:
                    pending.cancel()
                raise
            except Exception as exc:
                results[key] = (key, "", type(exc).__name__)
    return results


def _save_assignment_checkpoint(project, scenes, clips, report):
    missing = [{"id": scene.get("id", index + 1), "text": _text(scene),
                "reason": "No usable reviewed footage assigned"}
               for index, (scene, clip) in enumerate(zip(scenes, clips)) if clip is None]
    report.update(vision_budget_exhausted=_vision_budget_spent(project),
                  missing_beats=missing, status="needs_footage" if missing else "ready")
    folder = Path(project) / "review"
    folder.mkdir(parents=True, exist_ok=True)
    checkpoint = {"status": report["status"], "scenes": scenes, "scene_clips": clips,
                  "missing_beats": missing,
                  "vision_budget_exhausted": report["vision_budget_exhausted"]}
    (folder / "scrape_v4_checkpoint.json").write_text(
        json.dumps(checkpoint, ensure_ascii=False, indent=2), encoding="utf-8")


def _shortlist_vision_candidates(candidates):
    """Share a bounded review pool across queries, with at most two windows per source/query."""
    buckets, per_source = {}, {}
    for candidate in sorted(candidates, key=lambda row: row.score, reverse=True):
        if candidate.status != "available":
            continue
        query = str(candidate.query).casefold()
        key = (query, candidate.source_id)
        if per_source.get(key, 0) >= 2:
            continue
        per_source[key] = per_source.get(key, 0) + 1
        buckets.setdefault(query, []).append(candidate)
    selected = []
    for rank in range(8):
        for bucket in buckets.values():
            if rank < len(bucket) and len(selected) < 96:
                selected.append(bucket[rank])
    selected_ids = {id(row) for row in selected}
    for row in candidates:
        if row.status == "available" and id(row) not in selected_ids:
            row.status = "deferred"
            row.rejection = "outside bounded vision shortlist; not visually reviewed"
    return selected


def vision_verdict(job, model, attempts=VISION_ATTEMPTS):
    """Ask one source's three-frame strip for a verdict, and ASK AGAIN before giving up.

    A single slow HTTP call used to end a candidate's life: the caller turns an empty answer into
    "V4 vision review unavailable for this source", and a clip that had already been searched for,
    downloaded and passed every technical gate was discarded. Measured across saved runs: 6
    TimeoutError and 2 IndexError verdicts, every one a source lost to a blip rather than to
    anything about the footage. The download path has had a retry for this exact reason.

    A module-level function rather than a closure so it can be tested against a fake transport -
    testing it in place would mean slicing this file and exec'ing the text back, which has
    silently broken several tests in this repo already.
    """
    import agent_core                    # imported per call site in this module, not at the top
    source_id, _candidate, strip_path, prompt = job
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "Return only valid JSON. Judge only what is visibly present."},
            {"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": agent_core.image_data_url(strip_path)}},
            ]},
        ],
        "temperature": 0.0, "max_tokens": 1024,
        "_strict_token_limit": True, "_transport_retries": 0,
    }
    if "gemini" in str(model).lower():
        payload["reasoning_mode"] = "minimal"
    last = ""
    for attempt in range(max(1, int(attempts))):
        _reserve_vision_request(strip_path, model, source_id)
        try:
            data = agent_core.post_json_url(agent_core.WAVESPEED_LLM_API, payload, timeout=180)
            with _VISION_LEDGER_LOCK:
                with (Path(strip_path).parent / "v4_vision_results.jsonl").open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps({"key": source_id, "model": model, "response": data},
                                            ensure_ascii=False) + "\n")
            return source_id, str(data["choices"][0]["message"].get("content") or ""), ""
        except agent_core.WaveSpeedBalanceError:
            raise
        except Exception as exc:                                        # noqa: BLE001
            last = type(exc).__name__
            if attempt < attempts - 1:
                time.sleep(1.5 * (attempt + 1))
    return source_id, "", last


def footage_review_prompt(source_id, query, brief="", topic=""):
    """The exact text the footage reviewer is asked.

    A module-level function on purpose. The prompt used to be assembled inline inside the
    review loop, and the only way to test it was to slice the source file and eval the string
    literals back together - a harness that silently returned nothing when the wording moved,
    and reported it as a prompt result. Calibrating a scale needs the shipped text.
    """
    scene_contract = brief
    brief = editorial.contract(brief)["line"]
    topic = str(topic or "").strip()
    # WITHOUT THE SUBJECT, THE LINE IS JUST WORDS. Measured on the convenience-store Short
    # (2026-09-04): the beat "somebody tried to fit every minor emergency into one BUILDING" was
    # given a tour of Hong Kong's Monster Building at relevance 8 - the reviewer matched the noun
    # because nothing in the prompt said the video is about Japanese convenience stores.
    wanted = ""
    if topic:
        wanted += f"THE VIDEO IS ABOUT: {topic[:120]}\n"
    if brief:
        wanted += f"THE LINE THIS SHOT HAS TO CARRY: \"{brief[:220]}\"\n"
    if topic:
        wanted += ("A clip that matches a WORD in the line but belongs to a different subject is "
                   "unrelated - score it 0-3, however striking it looks.\n")
    return (
        "You are cutting a vertical short-form video and deciding whether ONE candidate "
        "clip can be used in it. The three frames are the start, middle and end of the "
        f"clip, so together they show whether anything MOVES. Source `{source_id}`, found "
        f"by searching: `{query}`.\n"
        + wanted +
        "\nWHAT THIS CLIP HAS TO DO. It has to give roughly two to three seconds of real "
        "footage that a viewer accepts as the same world the line is describing. It does "
        "NOT have to contain the search words. Context may be illustrative, but action/payoff "
        "beats must satisfy the editorial contract below. It does not have to contain the "
        "search words. A classroom being mopped carries a line about students cleaning "
        "their school; a hand lifting a hot can carries a line about winter vending "
        "machines. Judge the place, the people and the action you can see.\n"
        "\nDISQUALIFIERS - these make a clip unusable no matter how on-topic it looks:\n"
        "- AI-generated or synthetic imagery, 3D animation, obvious filters\n"
        "- a WORLD-FAMOUS person somewhere they would never plausibly be, or any celebrity "
        "cameo in an everyday setting: that is a generated clip, not footage. A Short about "
        "Japanese convenience stores was opened with Beyonce holding an ice cream inside a "
        "7-Eleven and it was scored 8 out of 10 (2026-09-04). Ask yourself whether this exact "
        "moment could have been filmed by a passer-by with a phone; if the answer needs a film "
        "crew, a publicist or a model, reject it\n"
        "- a split screen, a reaction panel above or below the picture, or a video pasted onto "
        "a coloured background: the edit needs the whole frame\n"
        "- a person talking to camera about the topic instead of the topic happening\n"
        "- a screen recording, a slideshow, a photo held still, or a text card\n"
        "- nothing moving across the three frames, or the same frame repeated\n"
        "- large boxed captions, a coloured caption bar, a creator banner, or text "
        "covering the subject\n"
        "- a watermark or profile overlay large enough to dominate the frame\n"
        "\nSMALL burned-in subtitles are FINE: the render removes them.\n"
        "\nSCORE WHAT YOU SEE. Do not guess at what happens off-screen and do not "
        "reject because you are unsure what the clip is FOR - that decision is made "
        "later, using your score. `relevance` is an integer 0-10: 10 = this is plainly "
        "the moment the line describes, 8 = the right place and the right kind of action, "
        "6 = the right world but a different moment, 3 = same country or theme only, "
        "0 = unrelated. `accept` means only: real moving footage with no disqualifier, "
        "usable somewhere in this video. A clip can be accept=true with relevance 5.\n"
        "`best_frame` is 1, 2 or 3 - whichever of the three shows the action most "
        "clearly, so the edit can cut around it.\n"
        "\nNOW SCORE IT AS A PICTURE, not as a match. `visual_interest` is 0-10 and asks "
        "one thing: would a viewer scrolling past stop for this?\n"
        "  8-10  you would stop: something strange, funny, extreme or impressive happens "
        "on screen - a person doing something you have not seen before, a machine doing "
        "something surprising, a crowd at its limit, a face reacting, an object behaving "
        "oddly, a striking scale, or a genuinely beautiful frame\n"
        "  5-7   solid and watchable: a clear subject doing something, close enough to "
        "read, steady camera, something changing across the three frames\n"
        "  2-4   correct but inert: a wide static view, an empty room or street, a slow "
        "pan over nothing in particular, the subject small and far away\n"
        "  0-1   unwatchable: dark, blurred, shaking beyond use, heavily compressed, or "
        "almost frozen\n"
        "\nJudge the PICTURE. Never raise this because the clip fits the line well - "
        "`relevance` already says that. A perfectly on-topic empty platform is a 3. A "
        "strange or funny moment that is only loosely related is still an 8.\n"
        "\nCALIBRATE AGAINST THE MIDDLE. Asked without this paragraph on a real run, the "
        "model put 12 of 13 clips between 5 and 7 and gave an empty platform a 6 - a "
        "score that sorts nothing. MOST FOOTAGE IS A 5. Before writing 7 or more you must "
        "be able to name the specific thing that happens, and it has to appear in your "
        "`reason`: a guard shoving commuters into a carriage, a face reacting, an object "
        "doing something unexpected. 'People are present and the shot is competent' is a "
        "5, not a 7. If the only honest description is a category - passengers riding, "
        "people walking, a street with traffic - the score is 4 or below, however well "
        "the clip fits.\n"
        "\nReturn STRICT JSON ONLY, exactly this shape:\n"
        '{"accept":true,"relevance":8,"visual_interest":7,"real_footage":true,'
        '"static_or_repeated":false,"caption_severity":"none","best_frame":2,'
        '"reason":"what you actually see"}\n'
        "caption_severity is one of none, plain_small, intrusive_boxed. `reason` names "
        "what is in the frames in one short sentence - not whether it matches."
        + editorial.evidence_prompt(scene_contract)
    )


def _vision_source_review(candidates, project, status_cb=None, model=DEFAULT_V4_VISION_MODEL,
                          briefs=None, topic=""):
    """Run one compact vision verdict for every technically viable source.

    A source gets one small three-frame strip and one bounded JSON response.
    This is deliberately *not* one enormous all-source contact sheet: the model can
    see an individual strip reliably, whereas a dense multi-source sheet has
    occasionally returned an empty completion.  Seven 180-token standard-Luna
    reviews are still far cheaper than the old per-segment OCR/vision passes.
    """
    # JUDGE THE WINDOW THAT WILL BE ON SCREEN, not the post it came from. The strip used to
    # sample the whole source at 12%, 50% and 84% and one verdict covered every window of it -
    # but the edit shows a single ~2.75s slice taken at 8%, 49% or 80%. On a 30-second montage
    # those are different scenes entirely, so the verdict described footage the viewer never
    # sees. Measured on the couples Short (2026-09-04): vision reported "a couple embraces by a
    # pedestrian crosswalk" and "an upscale restaurant interior with premium beef" while the
    # windows actually rendered a man holding a photo card and a flower shop. Four of five
    # scenes were wrong that way, which is exactly why the numbers looked healthy and the video
    # did not.
    if _vision_budget_spent(project):
        for candidate in candidates:
            if candidate.status == "available" and not candidate.vision:
                candidate.status = "deferred"
                candidate.rejection = "Vision budget reached; not reviewed"
        return
    _shortlist_vision_candidates(candidates)
    reviewable = [item for item in candidates if item.status == "available"]
    if not reviewable:
        return
    try:
        import cv2
        from PIL import Image, ImageDraw, ImageFont
        tiles = []
        for candidate in reviewable:
            source_id = candidate.source_id
            cap = cv2.VideoCapture(candidate.path)
            count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
            fps = float(cap.get(cv2.CAP_PROP_FPS) or 0) or 30.0
            start_f = max(0, int(float(candidate.start or 0.0) * fps))
            end_f = int(float(candidate.end or 0.0) * fps)
            if end_f <= start_f:
                end_f = count or (start_f + 1)
            span = max(1, end_f - start_f)
            frames = []
            for fraction in (.10, .50, .90):
                cap.set(cv2.CAP_PROP_POS_FRAMES,
                        max(0, min(max(0, count - 1) if count else start_f,
                                   start_f + int(span * fraction))))
                ok, frame = cap.read()
                if ok:
                    frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                    image = Image.fromarray(frame)
                    image.thumbnail((240, 390))
                    frames.append(image.copy())
            cap.release()
            if len(frames) != 3:
                candidate.status = "rejected"
                candidate.rejection = "could not extract a three-frame review strip"
                continue
            # How much of this window is flat filler rather than picture. Free here: the frames
            # are already decoded. See dead_frame_share() for what this catches and why the
            # reviewer alone did not.
            try:
                import numpy as _np
                shares = []
                for image in frames:
                    grey = _np.asarray(image.convert("L"), dtype=float)
                    shares.append(float(((grey.mean(axis=1) < 26)
                                         & (grey.std(axis=1) < 12)).mean()))
                candidate.dead_share = float(sorted(shares)[len(shares) // 2])
            except Exception:                                           # noqa: BLE001
                candidate.dead_share = 0.0
            # Free, from the same three frames: how much this window moves. See Candidate.motion.
            try:
                import numpy as _np
                strip = [_np.asarray(image.convert("L").resize((96, 160)), dtype=float)
                         for image in frames]
                candidate.motion = float(min(_np.abs(strip[1] - strip[0]).mean(),
                                             _np.abs(strip[2] - strip[1]).mean()))
            except Exception:                                           # noqa: BLE001
                candidate.motion = -1.0
            if 0.0 <= candidate.motion < MIN_WINDOW_MOTION:
                candidate.status = "rejected"
                candidate.rejection = (f"nothing moves in this window "
                                       f"(motion {candidate.motion:.1f} < {MIN_WINDOW_MOTION})")
                continue
            candidate.synthetic_mark = synthetic_watermark(frames)
            # A signature of THIS WINDOW. `fingerprint` is taken once per file, so two windows of
            # one post carry the same value and nothing can tell "a different moment" from "the
            # same shot twice". That single value had to serve both jobs and could only do one:
            # blocking it kept the konbini Short's three identical shelf shots out, and also made
            # "another window of this beat's own source" impossible. Per window, both work.
            try:
                import numpy as _np
                grey = _np.asarray(frames[len(frames) // 2].convert("L").resize((16, 16)),
                                   dtype=float)
                bits = (grey > grey.mean()).flatten()
                candidate.window_fingerprint = "".join(
                    f"{int(''.join('1' if b else '0' for b in bits[i:i + 4]), 2):x}"
                    for i in range(0, len(bits), 4))
            except Exception:                                           # noqa: BLE001
                candidate.window_fingerprint = ""
            panel = Image.new("RGB", (748, 438), "#101317")
            draw = ImageDraw.Draw(panel)
            font = ImageFont.load_default()
            draw.text((10, 10), f"{source_id}  |  {candidate.query[:70]}", fill="white", font=font)
            x = 8
            for frame in frames:
                y = 36 + (390 - frame.height) // 2
                panel.paste(frame, (x + (240 - frame.width) // 2, y))
                x += 248
            tiles.append((source_id, candidate, panel))
        if not tiles:
            return
        review = Path(project) / "review"; review.mkdir(parents=True, exist_ok=True)
        import agent_core
        model = str(model or DEFAULT_V4_VISION_MODEL).strip()
        _log(status_cb, f"V4 vision ({model}): reviewing {len(tiles)} candidate WINDOWS as three-frame strips "
                       "taken from inside each window.")
        raw_log = []
        review_jobs = []
        for source_id, candidate, panel in tiles:
            # One strip per WINDOW: two windows of the same post are two different pictures and
            # sharing a filename made the second overwrite the first on disk.
            key = f"{source_id}@{candidate.start:.2f}"
            strip_path = review / f"v4_vision_{source_id}_{candidate.start:.2f}.jpg"
            panel.save(strip_path, quality=84)
            prompt = footage_review_prompt(source_id, candidate.query,
                                           (briefs or {}).get(
                                               str(candidate.query or "").casefold()),
                                           topic=topic)
            review_jobs.append((key, candidate, strip_path, prompt))

        results = _collect_vision_jobs(review_jobs, model, status_cb)

        # AN EMPTY ACCOUNT IS NOT A VERDICT ABOUT THE FOOTAGE. Measured on the walking/eating run
        # (2026-09-09 04:12): the balance ran out at source 76 of 203, and the next 1485 window
        # reviews came back WaveSpeedBalanceError. Each one was written down as "this window was
        # rejected", the run carried on, filled all 15 beats out of the 75 sources that had been
        # reviewed before the money ran out, and reported assigned=15 total=15 - which reads as a
        # complete success. Seven of those 15 beats came from ONE source: 47% of the video.
        # A rejection means the clip was looked at and found wanting. This was not looked at.
        _broke = sum(1 for _k, _r, _e in results.values() if "BalanceError" in str(_e))
        if _broke and _broke >= max(3, len(review_jobs) // 10):
            raise agent_core.WaveSpeedBalanceError(
                f"The WaveSpeed balance ran out during the vision review: {_broke} of "
                f"{len(review_jobs)} windows came back unreviewed. Stopping instead of "
                f"assigning beats from the fraction that was reviewed - that produces a Short "
                f"that looks complete and is built from whatever happened to be checked first. "
                f"Top up the balance and rerun; the downloaded sources are cached.")
        for key, candidate, _strip_path, _prompt in review_jobs:
            _key, raw_answer, error = results.get(key, (key, "", "UnknownError"))
            if error:
                candidate.status = "deferred" if error == "VisionBudgetExceeded" else "rejected"
                candidate.rejection = f"V4 vision review unavailable for this window: {error}"
                raw_log.append({"window": key, "response": "", "error": error})
                continue
            raw_log.append({"window": key, "response": raw_answer})
            decision = agent_core.extract_json_object(raw_answer) or {}
            if not isinstance(decision, dict):
                decision = {}
            if not decision:
                candidate.status = "rejected"
                candidate.rejection = "V4 vision returned no usable verdict for this window"
                continue
            caption_severity = str(decision.get("caption_severity") or
                                   ("intrusive_boxed" if decision.get("boxed_or_prominent_creator_captions") else "none"))
            accepted = (decision.get("accept") is True and float(decision.get("relevance") or 0) >= 7
                        and decision.get("real_footage") is True
                        and not decision.get("static_or_repeated")
                        and caption_severity != "intrusive_boxed")
            # A window that is mostly filler is not footage, whatever the reviewer said about it.
            # Measured on the couples Short: the rejected hook scored 0.465 while every other
            # scene in the same edit sat at 0.064 or below, so the cut is nowhere near a real clip.
            mark = str(getattr(candidate, "synthetic_mark", "") or "")
            if accepted and mark:
                accepted = False
                decision = dict(decision)
                decision["reason"] = (f"carries the generator watermark {mark!r} - this was "
                                      "made by an AI tool, not filmed")
            if accepted and float(getattr(candidate, "dead_share", 0.0) or 0.0) > 0.25:
                accepted = False
                decision = dict(decision)
                decision["reason"] = (f"{candidate.dead_share:.0%} of this window is flat black "
                                      "filler around the picture - a caption slab or a pasted-in "
                                      "layout, not full-frame footage")
            # MEASURE the burned-in text instead of trusting the label. The vision model reports
            # caption_severity as an impression, and a finished Short came back with four clips
            # carrying Japanese creator text at 14-20% of the frame - too much to inpaint, so the
            # render kept it. caption_coverage is the same measurement the removal pass makes, so
            # asking it here rejects the clip while alternatives still exist.
            share, share_ceiling = -1.0, 1.0
            if accepted:
                try:
                    import caption_remover
                    _ffm, _ = clip_scraper._ffmpeg_tools()
                    share_ceiling = float(caption_remover.MAX_COVERAGE)
                    if _ffm:
                        # Measured INSIDE the window too: a post can be clean where it starts and
                        # carry a caption bar exactly where the edit cuts in.
                        share = caption_remover.caption_coverage(
                            str(candidate.path), _ffm, seconds=2.2,
                            start=float(candidate.start or 0.0))
                except Exception:                                       # noqa: BLE001
                    share, share_ceiling = -1.0, 1.0
                if share > share_ceiling:
                    accepted = False
                    decision = dict(decision)
                    decision["reason"] = (f"burned-in text covers {share:.0%} of the frame - "
                                          "more than the removal pass can rebuild")
            reviewed_scene = (briefs or {}).get(str(candidate.query or "").casefold(), "")
            decision["reviewed_line"] = editorial.contract(reviewed_scene)["line"]
            decision["reviewed_sequence_id"] = editorial.contract(reviewed_scene)["sequence_id"]
            partial_action = (decision.get("action_visible") is True
                              and bool(str(decision.get("observed_action") or "").strip()))
            partial_result = decision.get("result_visible") is True
            if accepted and reviewed_scene and not editorial.evidence_fits(reviewed_scene, decision) and not (partial_action or partial_result):
                accepted = False
                decision["reason"] = "required action/result is not visibly evidenced: " + str(decision.get("reason") or "")
            candidate.vision = dict(decision)
            candidate.vision["caption_severity"] = caption_severity
            candidate.caption_share = share
            # THE VERDICT REPLACES THE PLACEHOLDER. `reason` is written before the review as
            # "native vertical moving footage; awaiting V4 vision review" and was never
            # overwritten, so every assigned scene of the finished run carried that sentence into
            # the saved config - including the three windows the reviewer HAD looked at. A reader
            # opening the project cannot tell a reviewed window from an unreviewed one, and one
            # reviewer read it as proof the whole run went unreviewed.
            _seen = str(decision.get("observed_action") or decision.get("reason") or "").strip()
            candidate.reason = (candidate.reason.split("; awaiting V4 vision review")[0]
                                + ("; reviewed: " + _seen if _seen else "; reviewed"))[:400]
            candidate.visual_interest = _interest_of(decision)
            if not accepted:
                candidate.status = "rejected"
                candidate.rejection = str(decision.get("reason") or "rejected by V4 window review")[:240]
        (review / "v4_vision_source_review_response.json").write_text(
            json.dumps(raw_log, ensure_ascii=False, indent=2), encoding="utf-8")
    except VisionBudgetExceeded:
        raise
    except _balance_error():
        raise
    except Exception as exc:
        # No vision verdict means no editorially verified source.  Do not turn an
        # API outage into a permissive fallback that puts random videos on screen.
        for item in candidates:
            if item.status == "available":
                item.status = "rejected"
                item.rejection = f"V4 vision review unavailable: {type(exc).__name__}"


# Existing diagnostic tooling calls this historic name. New V4 calls the model-agnostic
# version above; keeping this alias avoids breaking a saved project review.
def _luna_source_review(candidates, project, status_cb=None):
    return _vision_source_review(candidates, project, status_cb=status_cb)


def _window_covers_scene(scene, candidate, need=None):
    """All assignment paths must preserve the reviewed action and source duration."""
    if need is None:
        need = max(.1, float(scene.get("end", 0)) - float(scene.get("start", 0)))
    span = candidate.end - candidate.start
    return (span >= need * V4_STRETCH_FLOOR - .01
            and (need >= span or editorial.action_trim(
                candidate.start, candidate.end, need, candidate.vision or {}) is not None))


def _combine_short_windows(scene, candidates, used_sources, used_ranges, used_fingerprints):
    """Cover one narration beat with two distinct, evidenced shots at natural pace."""
    contract = editorial.contract(scene)
    need = float(scene.get("end", 0)) - float(scene.get("start", 0))
    choices = []
    for c in candidates:
        v = c.vision or {}
        if (c.status != "available" or v.get("reviewed_line") != contract["line"]
                or float(v.get("relevance") or 0) < 7 or c.end - c.start < .7):
            continue
        if used_sources.get(c.source_id, 0):
            continue
        if any(c.source_id == sid and not (c.end <= a - .45 or c.start >= b + .45)
               for sid, a, b in used_ranges):
            continue
        if any(c.window_fingerprint and fp and _fingerprints_are_reposts(c.window_fingerprint, fp, 12)
               for _, _, fp in used_fingerprints):
            continue
        choices.append(c)
    choices.sort(key=fill_rank, reverse=True)
    for left, right in combinations(choices[:20], 2):
        if left.source_id == right.source_id and not (
                left.end <= right.start - .05 or right.end <= left.start - .05):
            continue
        if (left.window_fingerprint and right.window_fingerprint
                and _fingerprints_are_reposts(left.window_fingerprint, right.window_fingerprint, 12)):
            continue
        if (contract["role"] != "context" and not (left.vision or {}).get("action_visible")
                and (right.vision or {}).get("action_visible")):
            left, right = right, left
        if contract["role"] != "context":
            if not ((left.vision or {}).get("action_visible") is True
                    and str((left.vision or {}).get("observed_action") or "").strip()):
                continue
            if contract["required_result"] and not (right.vision or {}).get("result_visible"):
                continue
        a, b = left.end - left.start, right.end - right.start
        if a + b < need * V4_STRETCH_FLOOR or need < 1.4:
            continue
        split = min(a / V4_STRETCH_FLOOR, max(.7, need * a / (a + b)))
        if min(split, need - split) < .7:
            continue
        parts = []
        for number, (c, length) in enumerate(((left, split), (right, need - split))):
            start_trim = (editorial.action_trim(c.start, c.end, length, c.vision or {})
                          if length < c.end - c.start else c.start)
            if start_trim is None:
                break
            child = dict(scene)
            child["parent_editorial_contract"] = contract
            child["id"] = f"{scene.get('id', 'beat')}_shot{number + 1}"
            child["start"] = float(scene["start"]) + (split if number else 0)
            child["end"] = child["start"] + length
            # Each shot has its actual visual duty; the parent preserves the complete claim.
            if contract["role"] != "context":
                child["editorial_role"] = "action" if number == 0 else "context"
                child["required_action"] = str((c.vision or {}).get("observed_action") or contract["required_result"])
                child["required_result"] = ""
            parts.append((child, c, start_trim))
        if len(parts) == 2:
            return parts
    return []


def _has_usable_line_evidence(scene, candidates):
    return any(c.status == "available" and editorial.evidence_fits(scene, c.vision or {})
               and _window_covers_scene(scene, c) for c in candidates)


def _review_for_missing_line(scene, candidates, project, model, status_cb=None, topic=""):
    """Search deduplication must not restrict an existing source to its first beat."""
    if _vision_budget_spent(project) or _has_usable_line_evidence(scene, candidates):
        return []
    need = max(.1, float(scene.get("end", 0)) - float(scene.get("start", 0)))
    choices = [c for c in candidates if c.status == "available"
               and (c.vision or {}).get("accept") is True
               and (c.vision or {}).get("reviewed_line") != editorial.contract(scene)["line"]
               and c.end - c.start >= need * V4_STRETCH_FLOOR - .01]
    choices.sort(key=fill_rank, reverse=True)
    copies = [replace(c, vision=None, borrowed_from=c.query) for c in choices[:3]]
    if copies:
        _log(status_cb, f"V4 assignment: rechecking {len(copies)} existing windows for "
                        f"{editorial.contract(scene)['line']!r} before rejecting their beat identity.")
        _vision_source_review(copies, project, status_cb=status_cb, model=model,
                              briefs={c.query.casefold(): dict(scene) for c in copies}, topic=topic)
    return [c for c in copies if c.status == "available"
            and editorial.evidence_fits(scene, c.vision or {})]


def _review_continuations(scene, candidates, assigned, project, model, reviewer=vision_verdict):
    """Re-review another moment of a sequence's source for THIS narration line.

    Source discovery is deduplicated by post ID. Without this pass all windows
    retained the first query's verdict and could never carry the next action.
    """
    if _vision_budget_spent(project):
        return []
    sequence = editorial.contract(scene)["sequence_id"]
    prior = next((s for s in reversed(assigned) if s and sequence
                  and s.get("sequence_id") == sequence and s.get("reviewed_window")), None)
    if not prior:
        return []
    from editorial_quality import _strip
    from PIL import Image
    import agent_core
    previous = prior["reviewed_window"]
    need = max(.1, float(scene.get("end", 0)) - float(scene.get("start", 0)))
    choices = [c for c in candidates if c.status == "available"
               and c.source_id == prior.get("scrape_clip_id")
               and c.end - c.start >= need * V4_STRETCH_FLOOR - .01
               and (c.end <= previous["start"] - .45 or c.start >= previous["end"] + .45)]
    choices.sort(key=lambda c: abs(c.start - previous["end"]))
    folder = Path(project) / "review" / "continuations"
    folder.mkdir(parents=True, exist_ok=True)
    accepted = []
    for number, candidate in enumerate(choices[:3]):
        key = f"{candidate.source_id}_{candidate.start:.3f}_{scene.get('id', 'scene')}"
        # IDs become filenames only after removing separators and other path characters.
        key = re.sub(r"[^\w.-]", "_", key)[:160]
        path = folder / f"{key}.jpg"
        try:
            rows = []
            for row, (a, b) in enumerate(((previous["start"], previous["end"]),
                                          (candidate.start, candidate.end))):
                strip = folder / f"{key}_{row}.jpg"
                _strip(candidate.path, [a + (b-a)*f for f in (.04, .25, .5, .75, .96)], strip)
                with Image.open(strip) as image:
                    rows.append(image.copy())
            panel = Image.new("RGB", (900, 680))
            panel.paste(rows[0], (0, 0)); panel.paste(rows[1], (0, 340)); panel.save(path)
            prompt = ("Top row: previous sequence shot. Bottom row: proposed next shot, five chronological frames. "
                      "Judge ONLY the bottom row for the new line. continues_sequence must be true only if "
                      "these visibly continue the same subject/event, not merely the same country. "
                      + editorial.evidence_prompt(scene) +
                      "Return JSON: accept, relevance (0..10), continues_sequence, action_visible, "
                      "result_visible, observed_action, action_start, action_end, best_frame (1..3), reason.")
            _, raw, error = reviewer((key, candidate, path, prompt), model)
            verdict = agent_core.extract_json_object(raw) if not error else {}
            if not isinstance(verdict, dict):
                continue
            verdict["reviewed_line"] = editorial.contract(scene)["line"]
            verdict["reviewed_sequence_id"] = editorial.contract(scene)["sequence_id"]
            if (verdict.get("accept") is True and verdict.get("continues_sequence") is True
                    and float(verdict.get("relevance") or 0) >= 7
                    and editorial.evidence_fits(scene, verdict)):
                accepted.append(replace(candidate, vision=verdict, borrowed_from=candidate.query,
                                        reason=str(verdict.get("reason") or candidate.reason)))
        except VisionBudgetExceeded:
            break
        except Exception as exc:
            candidate.rejection = f"continuation review unavailable: {type(exc).__name__}"
    return accepted


def _download_tiktok_post_media(item, destination, status_cb=None):
    """Fetch one already-selected post. Public extractor first, the session only if it fails.

    This used to refuse outright when no logged-in session was ready - `return False` before any
    attempt - so a run without a login could discover posts through Scrape.do and download none of
    them. Search needs the account; fetching a public post does not, measured 2026-09-09: eight of
    eight post ids from a finished run resolved with no cookie file, one downloaded whole in four
    seconds.

    WHOLE POST, not the first 16 seconds. `backend_download`'s default belongs to the preview use;
    a V4 source is scanned across its full length and its chosen window can sit at 37s, where a
    truncated file is not a cheaper download but the wrong footage.
    """
    def _delivered(result):
        return bool(result and Path(result).exists()
                    and Path(result).stat().st_size > 64 * 1024)

    try:
        if _delivered(clip_scraper.backend_download(item, destination, status_cb=status_cb,
                                                    max_seconds=None)):
            return True
    except Exception as exc:                                            # noqa: BLE001
        _log(status_cb, f"V4 public download failed ({type(exc).__name__}).")
    if not getattr(clip_scraper, "tiktok_backend_ready", lambda: False)():
        return False
    try:
        # The local browser session is intentionally single-threaded; serialize this fallback so
        # cookie export never races or provokes a burst of logged-in calls.
        with _TIKTOK_LOGIN_RECOVERY_LOCK:
            if not clip_scraper._ensure_tiktok_cookies(status_cb=status_cb, platforms=["tiktok"]):
                return False
            return _delivered(clip_scraper.backend_download(item, destination,
                                                            status_cb=status_cb, max_seconds=None))
    except Exception as exc:
        _log(status_cb, f"V4 TikTok-login recovery failed ({type(exc).__name__}).")
        return False


def _download_bright_media(item, destination, unlocker_zone="", status_cb=None,
                           authenticated_fallback=False):
    """Fetch exactly Bright Data's supplied CDN URL.

    A missing, expired or HTML-returning direct URL is therefore a named rejection,
    not a reason to launch a browser or resolve a TikTok page.
    """
    url = str((item or {}).get("_media_url") or "").strip()
    if not url:
        return False

    # Bright's TikTok dataset provides the record and direct `video_url`, but
    # TikTok may still deny a plain CDN fetch.  When the user has a Bright Web
    # Unlocker zone, use Bright's REST endpoint for that *same Bright record*.
    # This remains Bright-only: no local proxy, no Playwright, no TikTok page
    # resolver.  The zone is deliberately opt-in because it is a separately
    # billed Bright product.
    zone = str(unlocker_zone or os.environ.get("BRIGHTDATA_UNLOCKER_ZONE", "")).strip()
    key = clip_scraper.brightdata_tiktok._api_key() if getattr(clip_scraper, "brightdata_tiktok", None) else ""
    unlocker_issue = ""
    if zone and key:
        try:
            import requests
            response = requests.post("https://api.brightdata.com/request", json={
                "zone": zone, "url": url, "format": "raw",
            }, headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                timeout=(25, 180))
            if response.status_code == 200:
                payload = _unlocker_video_bytes(response, item)
                with open(destination, "wb") as handle:
                    handle.write(payload)
                with open(destination, "rb") as handle:
                    header = handle.read(32)
                if (destination.stat().st_size > 64 * 1024 and b"ftyp" in header[:32]
                        and b"<html" not in header.lower()
                        and b"<!doctype" not in header.lower()):
                    return True
                code = str(response.headers.get("x-brd-err-code") or "").strip()
                detail = str(response.headers.get("x-brd-error") or response.headers.get("x-brd-err-msg") or "").strip()
                if detail and ("allowlist" in detail.casefold() or "allowed target" in detail.casefold()):
                    detail = f"target host {urlparse(url).netloc} is not allowed in this Bright zone"
                unlocker_issue = ((code + ": ") if code else "") + (detail or "returned a non-media response")
                if detail:
                    item["_bright_download_error"] = detail[:240]
            else:
                code = str(response.headers.get("x-brd-err-code") or "").strip()
                detail = str(response.headers.get("x-brd-error") or response.headers.get("x-luminati-error") or "").strip()
                if detail and ("allowlist" in detail.casefold() or "allowed target" in detail.casefold()):
                    detail = f"target host {urlparse(url).netloc} is not allowed in this Bright zone"
                unlocker_issue = ((code + ": ") if code else "") + (detail or f"HTTP {response.status_code}")
        except Exception as exc:
            unlocker_issue = f"request failed ({type(exc).__name__})"
        destination.unlink(missing_ok=True)
    try:
        # Bright's ``video_url`` is sometimes a bot-check HTML page.  Only accept a
        # real media container; an HTTP 200 document is not a successfully downloaded clip.
        import requests
        with requests.get(url, stream=True, timeout=(15, 150), headers={"User-Agent": "Mozilla/5.0"}) as response:
            if response.status_code != 200: raise RuntimeError("Bright CDN status")
            with open(destination, "wb") as handle:
                for block in response.iter_content(1024 * 256):
                    if block:
                        handle.write(block)
        with open(destination, "rb") as handle:
            header = handle.read(32)
        if (destination.stat().st_size > 64 * 1024 and b"ftyp" in header[:32]
                and b"<html" not in header.lower() and b"<!doctype" not in header.lower()):
            return True
    except Exception as exc:
        if not unlocker_issue:
            unlocker_issue = f"direct Bright-supplied media URL failed ({type(exc).__name__})"
    destination.unlink(missing_ok=True)
    if authenticated_fallback and str(item.get("_platform") or "tiktok").lower() == "tiktok":
        _log(status_cb, "Bright media delivery failed; trying the selected post through the local TikTok session.")
        if _download_tiktok_post_media(item, destination, status_cb=status_cb):
            item["_downloaded_via"] = "authenticated_tiktok_recovery"
            return True
    if status_cb and unlocker_issue:
        status_cb("Bright media delivery failed for this record: " + unlocker_issue[:220])
    return False


def _bright_media_failure_reason(unlocker_zone="", item=None):
    """Give the user an actionable failure, never a fake successful scrape."""
    if not str(unlocker_zone or os.environ.get("BRIGHTDATA_UNLOCKER_ZONE", "")).strip():
        return ("Bright returned a TikTok video URL that denied direct download; "
                "configure BRIGHTDATA_UNLOCKER_ZONE to fetch the same Bright record through "
                "Bright Web Unlocker (still Bright-only).")
    detail = str((item or {}).get("_bright_download_error") or "").strip()
    if detail:
        return "Bright Web Unlocker rejected this media URL: " + detail
    return "Bright direct media and the configured Bright Web Unlocker both failed for this record"


def _resolve_bright_unlocker_zone(requested_zone=""):
    """Return an active Unlocker zone without spending a dataset record.

    Bright's TikTok dataset discovery can succeed while every supplied CDN URL is
    protected.  Listing zones first makes that account prerequisite explicit at
    the start of a run instead of wasting a paid discovery batch and failing
    later at media download.
    """
    requested_zone = str(requested_zone or "").strip()
    key = clip_scraper.brightdata_tiktok._api_key() if getattr(clip_scraper, "brightdata_tiktok", None) else ""
    if not key:
        raise RuntimeError("V4 Bright-only: Bright Data API key is not configured.")
    try:
        import requests
        response = requests.get("https://api.brightdata.com/zone/get_all_zones",
                                headers={"Authorization": f"Bearer {key}"}, timeout=25)
        if response.status_code != 200:
            raise RuntimeError(f"Bright zone preflight returned HTTP {response.status_code}")
        zones = response.json() if response.content else []
    except Exception as exc:
        raise RuntimeError("V4 Bright-only: could not verify a Bright Web Unlocker zone "
                           f"before discovery ({type(exc).__name__}).") from exc
    active = [str(zone.get("name") or "") for zone in (zones if isinstance(zones, list) else [])
              if isinstance(zone, dict) and str(zone.get("type") or "").lower() == "unblocker"
              and str(zone.get("status") or "").lower() == "active"]
    if requested_zone and requested_zone in active:
        return requested_zone
    if not requested_zone and active:
        return active[0]
    if requested_zone:
        raise RuntimeError(f"V4 Bright-only: Web Unlocker zone '{requested_zone}' is not active for this API key.")
    raise RuntimeError("V4 Bright-only needs one active Bright Web Unlocker zone to download protected "
                       "TikTok media. No paid Bright dataset search was started.")


def scrape_social_plan_v4(config, scenes, project_dir, platforms=None, per_clip_seconds=2.3,
                          script_relevancy=None, cookies=None, cancel_check=None,
                          understanding=None, reasoning_model=None, status_cb=None, script_text=""):
    """Rendered discovery → selected-post download → local gates → unique windows."""
    # The longest beat this run has to fill. Windows are cut before any beat is matched,
    # so without it every candidate is capped at the cadence and a long beat can never
    # be covered - see _windows.
    _longest_beat = max([max(.1, float((s or {}).get('end', 0)) - float((s or {}).get('start', 0)))
                         for s in (scenes or [])] or [per_clip_seconds])
    del platforms, script_relevancy, cookies, understanding, reasoning_model, script_text
    project = Path(project_dir); review = project / "review"; review.mkdir(parents=True, exist_ok=True)
    # A PREVIOUS ENGINE'S TIMELINE IS NOT THIS RUN'S. live_processing_timeline.json is written
    # only by V3 (which deletes it at the start of its own run, scrape_v3.py:3186). V4 never
    # touches it, so a project scraped by V3 and re-scraped by V4 keeps a file that says
    # `engine: scrape_v3` with the older run's timestamps - and a reviewer used exactly that
    # file to decide which engine had produced this run. Stale evidence is worse than none.
    (review / "live_processing_timeline.json").unlink(missing_ok=True)
    # HOW LONG THE RUN MAY KEEP LOOKING FOR A GOOD SHOT. A beat that settles for whatever it found
    # is what puts filler on screen: the pufferfish timeline used a cash box on an office desk for
    # "a padlocked metal box" and a street with a workshop for nothing in particular. Both beats
    # had a clip, so nothing in the run considered them a problem. Searching is now driven by
    # whether every beat has a GOOD clip, and this is the ceiling on that search - after it, the
    # remaining beats take the best thing found rather than leaving a hole.
    quality_budget_s = float((config or {}).get("v4_quality_budget_s") or 7200)
    quality_deadline = time.monotonic() + quality_budget_s
    # Discovery and media delivery are intentionally different capabilities.
    # Scrape.do supplies only canonical post URLs. The existing TikTok session
    # opens one already-selected post and captures its media response; it never
    # performs a keyword search in this V4 path.
    local_search_fallback = bool((config or {}).get("v4_tiktok_login_fallback", False))
    post_delivery_fallback = bool((config or {}).get("v4_tiktok_post_delivery", True))
    provider = str((config or {}).get("v4_tiktok_discovery_provider")
                   or os.environ.get("V4_TIKTOK_DISCOVERY_PROVIDER", "scrapedo")).strip().casefold()
    use_scrapedo = provider in {"scrapedo", "scrape.do", "rendered", "auto"} and scrapedo_tiktok.available()
    bright_ready = bool(getattr(clip_scraper, "brightdata_tiktok", None)
                        and clip_scraper.brightdata_tiktok.available())
    unlocker_zone = ""
    if use_scrapedo:
        _log(status_cb, "V4 discovery: Scrape.do rendered TikTok search (selected posts download through the TikTok session).")
    elif bright_ready:
        try:
            unlocker_zone = _resolve_bright_unlocker_zone(
                str((config or {}).get("bright_unlocker_zone")
                    or os.environ.get("BRIGHTDATA_UNLOCKER_ZONE", "")).strip())
            _log(status_cb, f"V4 Bright preflight: active Web Unlocker zone '{unlocker_zone}' confirmed.")
        except RuntimeError as exc:
            if not local_search_fallback:
                raise
            bright_ready = False
            _log(status_cb, f"V4 Bright preflight failed ({exc}); using the local TikTok session instead.")
    elif not local_search_fallback:
        raise RuntimeError("V4 needs SCRAPEDO_API_KEY for rendered TikTok discovery (or Bright Data as an explicit fallback).")
    _ffmpeg, ffprobe = clip_scraper._ffmpeg_tools()
    # set_brightdata_only returns the newly set value, not the prior mode.
    # Preserve the caller's state explicitly so a V4 run cannot silently force
    # every later non-V4 operation into Bright-only mode.
    old_mode = bool(getattr(clip_scraper, "BRIGHTDATA_ONLY", False))
    clip_scraper.set_brightdata_only(True)
    try:
        # Native phrases first: every measured run that produced a complete edit searched in the
        # language of the footage, and V4 was emitting none.
        native_by_beat = _native_terms_agent(config or {}, scenes, status_cb=status_cb)
        tasks = []
        for index, scene in enumerate(scenes or []):
            native = native_by_beat[index] if index < len(native_by_beat) else []
            if _text(scene):
                tasks.append((index, scene,
                              _queries(scene, str(config.get("title") or ""), native_terms=native)))
        if not tasks: raise RuntimeError("V4 received no voiced scenes to source.")
        # Ask each beat for the EXTREME version of its own subject too, and give the hook the most
        # of those - a hook that merely fits is a wasted opening. The phrases join the beat's own
        # query list so a clip found by one maps back to that beat during assignment; adding them
        # only to the discovery round would have made every such clip borrowable-only.
        striking_by_beat = {index: _striking_queries(queries, limit=3 if index == 0 else 1)
                            for index, _scene, queries in tasks}
        tasks = [(index, scene, list(queries) + list(striking_by_beat.get(index) or []))
                 for index, scene, queries in tasks]
        # The reviewer judged clips against the SEARCH PHRASE and never saw the narration, so it
        # graded literal word overlap. Every query knows which beat produced it.
        _topic = str((config or {}).get("topic") or (config or {}).get("title") or "").strip()
        _briefs = {}
        for _index, _scene, _qs in tasks:
            _line = _text(_scene) or str((_scene or {}).get("visual_action") or "")
            for _q in _qs:
                _briefs.setdefault(str(_q or "").casefold(), dict(_scene))
        review_folder = project / "review"
        review_folder.mkdir(parents=True, exist_ok=True)
        (review_folder / "v4_search_contracts.json").write_text(json.dumps([
            {"beat": i, "scene": scene, "queries": queries}
            for i, scene, queries in tasks], ensure_ascii=False, indent=2), encoding="utf-8")
        # Interleave hypotheses by narration beat.  Bright's record allowance
        # is global to the run: appending all six terms for beat one first meant
        # that a 3-beat Short could spend the entire quota before the final
        # beat had even been searched.  A fair first pass gives every beat its
        # subject/action/native variants before any one beat receives extras.
        requested = []
        max_query_depth = max(len(qs) for _, _, qs in tasks)
        for depth in range(max_query_depth):
            for _, _, qs in tasks:
                if depth < len(qs):
                    q = qs[depth]
                    if q not in requested:
                        requested.append(q)
        initial_queries, deferred_queries = _initial_query_round(tasks, per_beat=5)
        # The striking phrases sit at the END of each beat's list, so a beat that already has five
        # hypotheses would defer them and never look for anything arresting. Promote them into the
        # first round explicitly - ADDITIVE, so no proven term is displaced to pay for them.
        for extra in [query for beat in sorted(striking_by_beat) for query in striking_by_beat[beat]]:
            if extra not in initial_queries:
                initial_queries.append(extra)
            if extra in deferred_queries:
                deferred_queries.remove(extra)
        # TikTok is a keyword collector. Instagram is deliberately a separate ``url_all_reels``
        # collector because Bright does not expose a real Instagram keyword-search dataset.
        # Both are Bright API requests; neither path opens a logged-in browser or reads cached media.
        grouped = {}
        discovery_cost = 0.0
        discovery_rounds = []
        if use_scrapedo:
            geo = str((config or {}).get("v4_scrapedo_geo") or "jp")
            grouped = scrapedo_tiktok.search_many(initial_queries, geo_code=geo,
                                                   scroll_steps=int((config or {}).get("v4_scrapedo_scrolls") or 3),
                                                   workers=int((config or {}).get("v4_scrapedo_workers") or 4),
                                                   status_cb=status_cb) or {}
            round_cost = scrapedo_tiktok.total_cost(grouped)
            discovery_cost += round_cost
            discovery_rounds.append({"round": 1, "provider": "scrapedo", "geo": geo,
                                     "queries": list(initial_queries),
                                     "urls": sum(len(v or []) for v in grouped.values()),
                                     "cost": round_cost})
        elif bright_ready:
            _log(status_cb, f"V4 Bright TikTok collector: first pass uses {len(initial_queries)} deliberately different action-led keywords.")
            # Leave half of the record budget for a genuinely targeted second
            # pass.  Spending it all before looking at a frame makes the
            # scraper incapable of learning that a scene has only explainers,
            # reposts or unusable 16:9 material.
            grouped = clip_scraper.brightdata_tiktok.search_many(initial_queries[:24], per_query=3, status_cb=status_cb,
                                                                   timeout_s=900, max_records=120) or {}
        records = []
        seen = set()
        _merge_bright_grouped(records, grouped, seen)
        instagram = getattr(clip_scraper, "brightdata_instagram", None)
        if bright_ready and instagram is not None and instagram.available() and bool((config or {}).get("v4_instagram_enabled", True)):
            accounts = _instagram_accounts_agent(config or {}, scenes, status_cb=status_cb)
            if accounts:
                reels = instagram.reels_for_accounts(accounts, per_account=4, status_cb=status_cb,
                                                      timeout_s=900) or []
                task_for_query = {query: scene for _, scene, queries in tasks for query in queries}
                for item in reels:
                    # Profile pulls are not keyword-search hits.  Keep a reel only when its own
                    # caption/hashtags give a concrete beat match; an account's generic travel
                    # reel must not become filler just because it came from a selected creator.
                    best_query, best_hits = "", []
                    for query, scene in task_for_query.items():
                        hits, unrelated = _metadata_relevance(scene, query, item)
                        if hits and (not best_hits or len(hits) > len(best_hits)):
                            best_query, best_hits = query, hits
                    sid = ("instagram", str(item.get("id") or ""))
                    if best_query and sid not in seen:
                        records.append((best_query, item)); seen.add(sid)
                _log(status_cb, f"V4 Instagram collector: {len(reels)} reels fetched; "
                                f"{sum(1 for _, item in records if item.get('_platform') == 'instagram')} "
                                "metadata-grounded candidates joined the editorial pool.")
        if not records and local_search_fallback:
            # Bright discovery itself can occasionally return a provider error or an empty
            # snapshot.  Only then wake the already-authorized local TikTok session.  This is
            # intentionally not a parallel second scrape, so a healthy Bright run remains
            # Bright-first and does not consume a logged-in account's search allowance.
            _log(status_cb, "V4 Bright discovery returned no records; trying the local TikTok session.")
            clip_scraper.set_brightdata_only(False)
            try:
                records = _authenticated_tiktok_records(requested, status_cb=status_cb)
            finally:
                clip_scraper.set_brightdata_only(True)
        if not records: raise RuntimeError("V4 could not collect any TikTok records from Bright or the local session.")
        # Inspect a deliberately diverse editorial pool, not every near-identical response.
        # Earlier V4 downloaded all 40+ records before placing its first three beats; that
        # made one short wait on caption analysis that could not affect the initial edit.
        # Keep up to two records per query and enough distinct queries for four alternatives
        # per voiced beat. A later recovery pass can widen this pool only for uncovered beats.
        # Round-robin the requested queries before taking a second result from
        # any one of them. Otherwise the first two narration beats consume the
        # whole 12-record inspection budget and the final beat literally has no
        # candidate to choose from.
        pools = {query: [] for query in requested}
        for query, item in records:
            pools.setdefault(query, []).append(item)
        shortlisted, shortlisted_ids = [], set()
        # Do not abort after a tiny first handful when Bright has already paid
        # for and returned more alternatives.  A factual short needs enough
        # real footage to survive caption and relevance review.  This is still
        # bounded, concurrent and reuses the single Bright discovery batch.
        # A complete edit matters more than a fast-looking partial one. Bright
        # has already returned this bounded batch, so continue through every
        # distinct record it supplied rather than declaring an uncovered beat
        # after an arbitrary first 24 candidates.
        def take_wave(size):
            """Pull the next `size` distinct records, round-robin across the queries."""
            wave = []
            while len(wave) < size:
                added = False
                for query in requested:
                    pool = pools.get(query) or []
                    while pool:
                        item = pool.pop(0)
                        sid = (str(item.get("_platform") or "tiktok"),
                               str(item.get("id") or item.get("aweme_id") or item.get("webVideoUrl") or ""))
                        if not sid[1] or sid in shortlisted_ids:
                            continue
                        shortlisted_ids.add(sid)
                        wave.append((query, item)); added = True
                        break
                    if len(wave) >= size:
                        break
                if not added:
                    break
            return wave

        shortlisted = take_wave(min(len(records), max(45, len(tasks) * 15)))
        discovered_total = len(records)
        records = shortlisted
        _log(status_cb, f"V4 editorial pool: locally inspecting {len(records)} diverse Bright records.")
        proxy_dir = project / "seedance 2.0" / "_v4_proxies"; proxy_dir.mkdir(parents=True, exist_ok=True)
        # Bright discovery is already a single paid keyword batch.  The slow part used
        # to be the sequential CDN download + ffprobe + frame-motion gate: 40 clips
        # could spend 40 network waits in a row even though they are independent.
        # Run a bounded pool here.  Four workers is deliberately conservative for
        # Bright/TikTok CDNs; it gains wall-clock speed without starting a retry storm.
        worker_count = max(1, min(6, int(config.get("v4_download_workers") or 4)))
        def inspect_record(record):
            query, item = record
            platform = str(item.get("_platform") or "tiktok").lower()
            sid = str(item.get("id") or "unknown")
            source_id = f"{platform}__{sid}"
            path = proxy_dir / f"v4_{platform}_{sid}.mp4"
            try:
                _log(status_cb, f"V4 source gate: inspecting {platform} post {sid} for {query[:62]!r}.")
                if cancel_check and cancel_check(): raise RuntimeError("V4 scrape cancelled.")
                source_mode = str(item.get("_source") or "")
                is_login_record = source_mode in {"authenticated_tiktok", "scrapedo_tiktok"}
                downloaded = path.exists()
                if not downloaded and is_login_record:
                    downloaded = _download_tiktok_post_media(item, path, status_cb=status_cb)
                if not downloaded and not is_login_record:
                    downloaded = _download_bright_media(
                        item, path, unlocker_zone, status_cb=status_cb,
                        authenticated_fallback=post_delivery_fallback)
                if not downloaded:
                    _log(status_cb, f"V4 source gate: {platform} post {sid} could not deliver usable media.")
                    return [Candidate(source_id, query, str(path), 0, 0, 0, "", "rejected",
                                      ("The selected TikTok post could not be downloaded"
                                       if is_login_record else _bright_media_failure_reason(unlocker_zone, item)))]
                # Validate the FILE before asking anything about its geometry: a corrupt or
                # truncated download used to arrive here as (0, 0, 0.0) and be reported as
                # "not native vertical", which sent every investigation the wrong way.
                sound, media_reason, w, h, duration = validate_media(path, ffprobe)
                if not sound:
                    try:
                        Path(path).unlink(missing_ok=True)
                    except OSError:
                        pass
                    _log(status_cb, f"V4 source gate: rejected {sid} — {media_reason}.")
                    return [Candidate(source_id, query, str(path), 0, 0, 0, "", "rejected",
                                      media_reason)]
                if h < w or h < 800 or w < 400:
                    _log(status_cb, f"V4 source gate: rejected {sid} — not native vertical "
                                    f"({w}x{h}).")
                    return [Candidate(source_id, query, str(path), 0, 0, 0, "", "rejected",
                                      f"not native vertical ({w}x{h})")]
                ok, why = _motion_and_black(path)
                if not ok and "letterbox" in why.lower():
                    # The picture inside the bars is fine - only the container is wrong. On the
                    # fish run 2026-09-03 this verdict threw away 49 of 300 downloaded sources.
                    # Rebuild it the way every social editor does (blurred zoomed copy behind the
                    # sharp crop) and put it back through the same gate rather than discarding it.
                    repaired, note = unletterbox_clip(path, ffmpeg=_ffmpeg)
                    if repaired:
                        ok, why = _motion_and_black(repaired)
                        if ok:
                            path = repaired
                            _log(status_cb, f"V4 source gate: repaired letterboxed {sid} — {note}.")
                        else:
                            _log(status_cb, f"V4 source gate: {sid} still fails after the "
                                            f"letterbox rebuild — {why}.")
                    else:
                        _log(status_cb, f"V4 source gate: kept the letterbox rejection for {sid} "
                                        f"— {note}.")
                if not ok:
                    _log(status_cb, f"V4 source gate: rejected {sid} — {why}.")
                    return [Candidate(source_id, query, str(path), 0, 0, 0, "", "rejected", why)]
                popularity = math.log1p(float((item.get("stats") or {}).get("playCount") or 0)) / 20.0
                try:
                    _cuts = clip_scraper.hard_cut_times(
                        str(path), _ffmpeg, scan_seconds=duration + 1.0, threshold=.25)
                except Exception:                                       # noqa: BLE001
                    _cuts = None
                if _cuts is None:
                    return [Candidate(source_id, query, str(path), 0, 0, 0, "", "rejected",
                                      "source cut scan unavailable; window is not verified")]
                _wins = _windows(duration, per_clip_seconds, cuts=_cuts,
                                 longest=_longest_beat)
                out = []
                for start, end in _wins:
                    inside = sum(1 for c in _cuts if start < float(c) < end)
                    # a window that still cuts is worth less than one that does not; it is not
                    # refused, because an unfilled beat is worse than a busy shot
                    out.append(Candidate(source_id, query, str(path), start, end,
                                         5.0 + popularity - 0.6 * inside,
                                         ("native vertical moving footage; awaiting V4 vision review"
                                          if not inside else
                                          f"native vertical footage, but the source cuts {inside}x "
                                          "inside this window; awaiting V4 vision review"),
                                         "available",
                                         source_text=_source_text(item),
                                         fingerprint=_source_fingerprint(path),
                                         clean_until=_next_cut_after(start, _cuts, duration)))
                return out
            except Exception as exc:
                return [Candidate(source_id, query, str(path), 0, 0, 0, "", "rejected",
                                  f"local inspection failed: {type(exc).__name__}")]
        _log(status_cb, f"V4 parallel media gate: {len(records)} Bright records with {worker_count} workers.")
        inspected = {}
        with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="v4-media") as pool:
            futures = {pool.submit(inspect_record, record): index for index, record in enumerate(records)}
            # SAY SOMETHING WHILE THIS RUNS. Each record here is a download plus a decode and
            # several full-clip passes - motion, black bars, fingerprint, caption coverage - and
            # none of them writes a file. Measured on the walking/eating run: 82 minutes between
            # the last download and the first review strip, with nothing on disk changing and
            # nothing in the log, while four cores were saturated. From outside that is
            # indistinguishable from a hang, and the operator's only recourse was to read mtimes
            # and guess.
            _done = 0
            _step = max(10, len(futures) // 12)
            for future in as_completed(futures):
                index = futures[future]
                _done += 1
                if _done % _step == 0 or _done == len(futures):
                    _log(status_cb, f"V4 media gate: inspected {_done}/{len(futures)} source(s).")
                try:
                    inspected[index] = future.result()
                except Exception as exc:  # defensive: preserve an explicit rejection per record
                    query, item = records[index]
                    inspected[index] = [Candidate(str(item.get("id") or "unknown"), query, "", 0, 0, 0, "",
                                                   "rejected", f"parallel media gate failed: {type(exc).__name__}")]
        if _PROBE_FALLBACKS:
            from collections import Counter as _Counter
            _why = ", ".join(f"{n}x {w}" for w, n in _Counter(_PROBE_FALLBACKS).most_common(3))
            _log(status_cb, f"V4 source gate: the decoder rescued {len(_PROBE_FALLBACKS)} clips "
                            f"ffprobe would have discarded ({_why}).")
            _PROBE_FALLBACKS.clear()
        # NOTHING IS DISCARDED ON A TECHNICAL FAILURE ALONE. Measured on the train-pushers run
        # 2026-09-03: 190 of 190 records were thrown away, 126 of them as "corrupt download", and
        # all 16 re-checked afterwards were sound vertical videos. A body that never arrived, or a
        # file one probe could not read, says something about THAT ATTEMPT - not about the post.
        # The run used to accept those verdicts as final and then spend the rest of its time
        # searching for footage it had already found, so every technical failure now gets a second
        # attempt before the run is allowed to conclude it has nothing.
        def _is_technical(rows):
            if not rows or any(row.status == "available" for row in rows):
                return False
            reason = str((rows[0].rejection or "")).lower()
            return any(sign in reason for sign in _TECHNICAL_FAILURES)

        retry_indexes = [index for index in range(len(records)) if _is_technical(inspected.get(index))]
        if retry_indexes:
            _log(status_cb, f"V4 source gate: {len(retry_indexes)} of {len(records)} records failed "
                            "for technical reasons, not editorial ones; giving each a second attempt.")
            recovered = 0
            with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="v4-retry") as pool:
                retries = {pool.submit(inspect_record, records[index]): index for index in retry_indexes}
                for future in as_completed(retries):
                    index = retries[future]
                    try:
                        rows = future.result()
                    except Exception:                                       # noqa: BLE001
                        continue
                    if any(row.status == "available" for row in rows):
                        recovered += 1
                        inspected[index] = rows
            _log(status_cb, f"V4 source gate: the retry recovered {recovered} of "
                            f"{len(retry_indexes)} records that were about to be lost.")
            if _PROBE_FALLBACKS:
                _log(status_cb, f"V4 source gate: the decoder answered for {len(_PROBE_FALLBACKS)} "
                                "more clips during the retry.")
                _PROBE_FALLBACKS.clear()
        # Restore search order so editorial ranking and report diffs remain reproducible.
        candidates = [candidate for index in range(len(records)) for candidate in inspected.get(index, [])]
        still_technical = sum(1 for index in range(len(records)) if _is_technical(inspected.get(index)))
        if still_technical > max(3, len(records) // 3):
            # Say it plainly rather than letting the run look like a topic with no footage.
            _log(status_cb, f"V4 source gate: {still_technical} of {len(records)} posts could not be "
                            "fetched even on the second attempt - this run is short of footage "
                            "because of delivery, not because the topic has none.")
        # A Bright record can be perfectly valid while its CDN host is blocked by
        # Bright's compliance policy.  In that case retrying the *same CDN URL*
        # through yt-dlp cannot help: the local session needs its own ordinary
        # TikTok search result with a share-page URL.  Recover that editorial
        # pool only after every Bright download failed; a healthy Bright run
        # remains Bright-first and never doubles the discovery traffic.
        if local_search_fallback and not any(item.status == "available" for item in candidates):
            _log(status_cb, "V4 Bright media has no usable files; collecting matching posts through the local TikTok session.")
            clip_scraper.set_brightdata_only(False)
            try:
                recovery_records = _authenticated_tiktok_records(requested, status_cb=status_cb)
            finally:
                clip_scraper.set_brightdata_only(True)
            recovery_shortlist, recovery_seen = [], set()
            for query, item in recovery_records:
                sid = str(item.get("id") or item.get("aweme_id") or item.get("webVideoUrl") or "")
                if not sid or sid in recovery_seen:
                    continue
                recovery_seen.add(sid)
                recovery_shortlist.append((query, item))
                if len(recovery_shortlist) >= max(12, len(tasks) * 4):
                    break
            if recovery_shortlist:
                _log(status_cb, f"V4 TikTok-login recovery: inspecting {len(recovery_shortlist)} fresh local candidates.")
                with ThreadPoolExecutor(max_workers=1, thread_name_prefix="v4-login-media") as pool:
                    recovery_futures = [pool.submit(inspect_record, record) for record in recovery_shortlist]
                    for future in recovery_futures:
                        try:
                            candidates.extend(future.result())
                        except Exception as exc:
                            _log(status_cb, f"V4 TikTok-login recovery record failed ({type(exc).__name__}).")
                records.extend(recovery_shortlist)
        _vision_source_review(candidates, project, status_cb=status_cb, briefs=_briefs, topic=_topic,
                              model=config.get("v4_vision_model") or DEFAULT_V4_VISION_MODEL)

        # The first Bright pass is intentionally broad and economical.  Now
        # that there is visual evidence, use the reserved record budget only
        # for narration beats that genuinely have no review-approved source.
        # This is the crucial difference between "try six spellings" and an
        # editor that changes its search after learning what the platform
        # actually returned.
        # A BEAT IS NOT DONE BECAUSE IT HAS A CLIP. It is done when it has a clip worth watching.
        # The old test was "any available candidate at all", so a beat holding one weak shot looked
        # exactly like a beat holding the perfect one, and the search stopped. That is how a cash
        # box on an office desk ended up carrying "a padlocked metal box".
        #
        # The bar is deliberately the same two numbers the assignment already trusts: a relevance
        # of 7 is "the right world and the right kind of action", and interest band 1 is anything
        # that is not inert. Below that the run keeps looking - until the budget above runs out.
        def _good_enough(candidate):
            verdict = candidate.vision or {}
            return (candidate.status == "available"
                    and float(verdict.get("relevance") or 0) >= 7
                    and _interest_band(getattr(candidate, "visual_interest", 5.0)) >= 1)

        def _weak_beats():
            out = []
            for index, scene, queries in tasks:
                wanted = {str(query).casefold() for query in queries}
                need = max(.1, float(scene.get("end", 0)) - float(scene.get("start", 0)))
                if not any(_good_enough(item) and editorial.evidence_fits(scene, item.vision or {})
                           and item.end - item.start >= need * V4_STRETCH_FLOOR - .01
                           and item.query.casefold() in wanted
                           for item in candidates):
                    if not _combine_short_windows(scene, candidates, {}, [], []):
                        out.append((index, scene, queries))
            return out

        def _budget_spent(weak):
            if _vision_budget_spent(project):
                _log(status_cb, "V4: vision budget reached; stopping discovery and assigning existing footage.")
                return True
            if time.monotonic() < quality_deadline:
                return False
            _log(status_cb, f"V4: the {quality_budget_s / 60:.0f}-minute search budget is spent; "
                            f"{len(weak)} beat(s) will take the best clip found instead of a "
                            "better one that has not turned up.")
            return True

        # KEEP LOOKING WHILE ANY BEAT IS STILL SETTLING. This used to be one recovery pass for
        # beats that had NOTHING; now it repeats for beats that have nothing GOOD, and it spends
        # the cheapest option first: the posts discovery already paid for, then genuinely new
        # search angles. It stops when every beat is satisfied, when there is nothing left to try,
        # or when the budget above runs out - and only then does a beat settle for what it has.
        already = {str(q).casefold() for q in requested}
        recovery_round = 1
        for _attempt in range(8):
            uncovered = _weak_beats()
            if not uncovered:
                _log(status_cb, "V4: every beat has a clip that is both on-topic and worth watching.")
                break
            if _budget_spent(uncovered):
                break
            more = take_wave(60)
            if more:
                _log(status_cb, f"V4: {len(uncovered)} beat(s) still without a good clip; "
                                f"inspecting {len(more)} more of the {discovered_total} posts "
                                "discovery already paid for.")
                extra = []
                with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="v4-media") as pool:
                    for future in as_completed([pool.submit(inspect_record, r) for r in more]):
                        try:
                            extra.extend(future.result())
                        except Exception:                               # noqa: BLE001
                            continue
                if extra:
                    _vision_source_review(extra, project, status_cb=status_cb, briefs=_briefs, topic=_topic,
                                          model=config.get("v4_vision_model") or DEFAULT_V4_VISION_MODEL)
                    candidates.extend(extra)
                    records.extend(more)
                continue
            if not (use_scrapedo or bright_ready):
                break
            recovery_round += 1
            # A second round that re-asks the first round's phrases is the first round again.
            # The old version handed back exactly that - the same terms with "Japan " glued on
            # the front - so a beat that failed on hypothesis A was re-tried on "Japan A".
            # `_recovery_queries` changes the ANGLE instead: the place without the action, the
            # object alone, and any native phrase the first round did not spend.
            recovery_wanted = []
            for index, scene, queries in uncovered:
                native = (native_by_beat[index] if index < len(native_by_beat) else [])
                fresh = _recovery_queries(scene, str(config.get("title") or ""),
                                          used=list(queries) + list(requested),
                                          native_terms=native)
                mine = []
                for query in fresh:
                    if query.casefold() not in already and query not in recovery_wanted:
                        recovery_wanted.append(query)
                        mine.append(query)
                # A RECOVERY PHRASE HAS TO BELONG TO ITS BEAT. Both `_weak_beats` and the
                # assignment ask whether a candidate's query is in that beat's OWN query list, and
                # nothing used to put these there. So a recovery clip at relevance 10 left its
                # beat looking unsolved: the loop bought another round of searches for a beat that
                # was already served, and the clip itself could only reach the screen by being
                # borrowed or painted red as a coverage fill. The striking phrases learned this
                # same lesson earlier - adding them to discovery alone made them borrowable-only.
                if mine:
                    for position, (task_index, task_scene, task_queries) in enumerate(tasks):
                        if task_index == index:
                            tasks[position] = (task_index, task_scene, list(task_queries) + mine)
                            break
                    line = _text(scene) or str((scene or {}).get("visual_action") or "")
                    for query in mine:
                        _briefs.setdefault(str(query).casefold(), dict(scene))
                # Unspent phrases from this beat's own first list are still worth having, but
                # they come AFTER the genuinely new angles.
                wanted = {str(query).casefold() for query in queries}
                for query in deferred_queries:
                    if query.casefold() in wanted and query not in recovery_wanted:
                        recovery_wanted.append(query)
            if recovery_wanted:
                _log(status_cb, "V4 discovery recovery: "
                     f"{len(uncovered)} uncovered beat(s); using {len(recovery_wanted)} alternate hypotheses.")
                if use_scrapedo:
                    second_grouped = scrapedo_tiktok.search_many(
                        recovery_wanted[:24], geo_code=str((config or {}).get("v4_scrapedo_geo") or "jp"),
                        scroll_steps=int((config or {}).get("v4_scrapedo_scrolls") or 3),
                        workers=int((config or {}).get("v4_scrapedo_workers") or 4),
                        status_cb=status_cb) or {}
                    round_cost = scrapedo_tiktok.total_cost(second_grouped)
                    discovery_cost += round_cost
                    discovery_rounds.append({"round": recovery_round, "provider": "scrapedo",
                                             "geo": str((config or {}).get("v4_scrapedo_geo") or "jp"),
                                             "queries": list(recovery_wanted[:24]),
                                             "urls": sum(len(v or []) for v in second_grouped.values()),
                                             "cost": round_cost})
                else:
                    second_grouped = clip_scraper.brightdata_tiktok.search_many(
                        recovery_wanted[:24], per_query=3, status_cb=status_cb,
                        timeout_s=900, max_records=120) or {}
                new_records = []
                _merge_bright_grouped(new_records, second_grouped, seen)
                if new_records:
                    _log(status_cb, f"V4 discovery recovery: locally inspecting {len(new_records)} fresh records.")
                    second_inspected = {}
                    with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="v4-media-recovery") as pool:
                        futures = {pool.submit(inspect_record, record): pos
                                   for pos, record in enumerate(new_records)}
                        for future in as_completed(futures):
                            pos = futures[future]
                            try:
                                second_inspected[pos] = future.result()
                            except Exception as exc:
                                query, item = new_records[pos]
                                second_inspected[pos] = [Candidate(str(item.get("id") or "unknown"), query, "", 0, 0, 0, "",
                                                                   "rejected", f"recovery media gate failed: {type(exc).__name__}")]
                    recovery_candidates = [candidate for pos in range(len(new_records))
                                           for candidate in second_inspected.get(pos, [])]
                    candidates.extend(recovery_candidates)
                    records.extend(new_records)
                    _vision_source_review(recovery_candidates, project, status_cb=status_cb,
                                          briefs=_briefs, topic=_topic,
                                          model=config.get("v4_vision_model") or DEFAULT_V4_VISION_MODEL)
                else:
                    _log(status_cb, "V4 discovery recovery returned no new post IDs; no duplicate source was reused.")
            for query in recovery_wanted:
                already.add(query.casefold())
            if not recovery_wanted:
                _log(status_cb, "V4: no search angle is left to try for the remaining beat(s).")
                break
        uncovered = _weak_beats()
        available = [x for x in candidates if x.status == "available"]
        used_sources, used_ranges, used_fingerprints = {}, [], []
        # THE HOOK AND THE PAYOFF PICK FIRST. Assignment walks the beats in order and each one
        # takes the best clip still free, so the LAST beat - the payoff, the line the whole video
        # is built toward - chooses from leftovers. Measured on the school-rules Short
        # (2026-09-04): its closing line "your final subject is literally cleaning the floor" got
        # a girl turning on a corridor tap while seventeen clips of students cleaning a floor sat
        # unused. Those two beats are the ones a viewer decides on, so they choose while the pool
        # is still full; the middle keeps its natural order after that.
        _order = sorted(range(len(tasks)),
                        key=lambda pos: (0 if pos in (0, len(tasks) - 1) else 1,
                                         0 if pos == 0 else pos))
        # The edit still comes out in NARRATION order: results are written into their own slot,
        # not appended in the order they were decided.
        out_scenes = [None] * len(tasks)
        scene_clips = [None] * len(tasks)
        for _pos in _order:
            index, scene, qs = tasks[_pos]
            wanted = {q.casefold() for q in qs}
            # Never borrow a candidate from a different narration beat.  The
            # previous fallback sorted non-matches behind matches, then used
            # them anyway when the intended query had no clean result — exactly
            # how a romantic travel montage became "luggage delivery".
            matched = [x for x in available if x.status == "available" and x.query.casefold() in wanted]
            # A beat that has nothing may still borrow, but only on the strength of what vision
            # actually SAW. Measured 2026-09-02: a run ended with 5 of 12 beats empty while 16
            # distinct clips sat accepted and unused, several at relevance 10 - "worker rapidly
            # restocking a Japanese train station vending machine" could not fill the restocking
            # beat because a different beat's query had retrieved it.
            #
            # The old blanket ban was written when the only evidence was the post's own caption,
            # and borrowing on that basis turned a romantic travel montage into luggage delivery.
            # The verdict text describes the footage itself, so it is far better evidence - and
            # this only runs for a beat that would otherwise be left uncovered.
            borrowed = []
            if not _has_usable_line_evidence(scene, matched):
                for cand in available:
                    if cand.status != "available" or not _window_covers_scene(scene, cand):
                        continue
                    verdict = cand.vision or {}
                    if not verdict.get("accept"):
                        continue
                    if float(verdict.get("relevance") or 0) < 8:
                        continue
                    hits, unrelated = _metadata_relevance(
                        scene, cand.query, {"desc": str(verdict.get("reason") or "")})
                    if unrelated or not hits:
                        continue
                    borrowed.append(replace(cand, borrowed_from=cand.query))
            matched = matched + borrowed
            matched = matched + _review_for_missing_line(
                scene, matched, project,
                config.get("v4_vision_model") or DEFAULT_V4_VISION_MODEL,
                status_cb=status_cb, topic=_topic)
            # Same-source continuity gets its own line-specific review, not a borrowed score.
            if editorial.contract(scene)["sequence_id"]:
                matched = matched + _review_continuations(
                    scene, available, out_scenes, project,
                    config.get("v4_vision_model") or DEFAULT_V4_VISION_MODEL)
            # A window that was never looked at cannot be assigned. The per-window review runs in
            # a pool and a call can fail, time out or come back unparseable; the candidate then
            # keeps an empty ``vision`` and every relevance test below reads it as 0 - but the
            # metadata path does not test relevance at all, so an unreviewed window could still
            # win a beat on the strength of the POST'S CAPTION alone. Measured on the couples
            # Short (2026-09-05): nine of the ten assigned windows carry a verdict describing a
            # couple; the tenth has no verdict at all, and it is the shot of a wooden mallet that
            # the owner spotted at 6.3s under the word "COUPLES".
            unreviewed = [cand for cand in matched if not (cand.vision or {})]
            if unreviewed:
                for cand in unreviewed:
                    cand.rejection = "never reviewed: the vision pass produced no verdict for this window"
                matched = [cand for cand in matched if (cand.vision or {})]
                _log(status_cb, f"V4: {len(unreviewed)} window(s) for this beat were never "
                                f"reviewed and are not assignable.")
            ranked = []
            for cand in matched:
                if not editorial.evidence_fits(scene, cand.vision or {}):
                    cand.rejection = "no action evidence for this narration line"
                    continue
                needed = max(.1, float(scene.get("end", 0)) - float(scene.get("start", 0)))
                if cand.end - cand.start < needed * V4_STRETCH_FLOOR - .01:
                    cand.rejection = "clean reviewed window cannot cover beat at natural speed"
                    continue
                if needed < cand.end - cand.start and editorial.action_trim(
                        cand.start, cand.end, needed, cand.vision or {}) is None:
                    cand.rejection = "shortening this window would remove the action/result"
                    continue
                hits, explicitly_unrelated = _metadata_relevance(scene, cand.query,
                                                                  {"desc": cand.source_text})
                if explicitly_unrelated:
                    # The post copy is only discovery metadata.  It must not overrule the
                    # source-level visual review: creators frequently write generic captions
                    # (or describe another beat in a montage) while the inspected footage very
                    # clearly shows the requested action.  This was the reason a real classroom
                    # cleaning clip with an 8/10 vision verdict could not fill its beat.
                    #
                    # Keep the guard for borderline material.  A strong visual verdict is the
                    # authority; a 7/10 close match still needs metadata support so broad
                    # lifestyle footage cannot drift into a precise factual scene.
                    visual_relevance = float((cand.vision or {}).get("relevance") or 0)
                    if visual_relevance < 8:
                        cand.rejection = "record text has no concrete overlap with this scene"
                        continue
                    cand.rejection = ""
                    ranked.append((cand.score - .25, cand))
                    continue
                # Actual noun/action overlap earns a preference; empty source
                # descriptions can still be visually reviewed instead of being
                # falsely declared unrelated.
                ranked.append((cand.score + min(1.5, .35 * len(hits)), cand))
            ranked = [cand for _score, cand in
                      sorted(ranked, key=lambda pair: assignment_rank(pair[0], pair[1],
                                                                     is_hook=index == 0)
                                              + editorial.continuity_bonus(scene, pair[1], out_scenes),
                             reverse=True)]
            chosen = None
            for cand in ranked:
                if not editorial.may_continue(scene, cand, out_scenes,
                                               used_sources.get(cand.source_id, 0)): continue
                if any(cand.window_fingerprint and _win and
                       _fingerprints_are_reposts(cand.window_fingerprint, _win, max_bit_delta=12)
                       for _sid, _fp, _win in used_fingerprints):
                    cand.rejection = "same visible shot already assigned"
                    continue
                if any(sid != cand.source_id
                       and _fingerprints_are_reposts(cand.fingerprint, prior)
                       for sid, prior, _win in used_fingerprints):
                    cand.rejection = "near-identical repost of an already assigned source"
                    continue
                if any(cand.source_id == sid and not (cand.end <= a - .45 or cand.start >= b + .45)
                       for sid, a, b in used_ranges): continue
                chosen = cand; break
            if chosen is None:
                # Preserve the narration but do not lie: V4 returns no fake duplicate fallback.
                copy = dict(scene); copy["clip"] = None; copy["assignment_type"] = "uncovered"
                out_scenes[_pos] = copy; scene_clips[_pos] = None; continue
            dest_dir = project / "seedance 2.0"; dest_dir.mkdir(parents=True, exist_ok=True)
            # _pos, not len(out_scenes): the list is pre-sized now, so its length is
            # constant and every clip would have been written to the same filename.
            name = f"v4_{_pos:02d}_{chosen.source_id}.mp4"; dest = dest_dir / name
            if not dest.exists(): shutil.copy2(chosen.path, dest)
            source_platform = chosen.source_id.split("__", 1)[0]
            # CUT ON THE MOMENT THE REVIEWER LIKED. The window is a fixed 2.62s but a beat is
            # often shorter, and the edit used to show its FIRST slice - so the reviewer scored
            # three frames spread across 2.62s while the viewer saw the opening 1.3s of it.
            # Measured on the couples Short (2026-09-05): three beats showed 50-59% of the window
            # they were judged on, and the two weakest scenes were exactly those - "holds up a
            # decorated phone case" at relevance 10 rendered as an empty stretch of pavement,
            # because the phone case appears later in the window.
            #
            # `best_frame` (1, 2 or 3) says which third carried it. The prompt has asked for it
            # from the start and nothing ever read it. Centre the shown span on that third.
            _shown = 0.0
            try:
                _shown = float(scene.get("end") or 0.0) - float(scene.get("start") or 0.0)
            except (TypeError, ValueError):
                _shown = 0.0
            _win = max(0.0, float(chosen.end or 0.0) - float(chosen.start or 0.0))
            _cut_at = float(chosen.start or 0.0)
            if 0.0 < _shown < _win - 0.05:
                _cut_at = editorial.action_trim(chosen.start, chosen.end, _shown, chosen.vision or {})
            copy = dict(scene); copy.update({"clip": name, "asset": name, "seedance": True,
                "seedance_start_trim": round(_cut_at, 3), "scrape_source": f"brightdata_{source_platform}",
                "scrape_clip_id": chosen.source_id, "assignment_type": "v4_verified",
                "match_class": "vision_matched" if chosen.borrowed_from else "query_matched",
                "blur_captions": chosen.caption_share > 0.015,
                "source_has_captions": chosen.caption_share > 0.015,
                "v4_reason": chosen.reason})
            # Cover the beat rather than freezing on its last frame.
            _need = 0.0
            try:
                _need = float(scene.get("end") or 0.0) - float(scene.get("start") or 0.0)
            except (TypeError, ValueError):
                _need = 0.0
            _window = max(0.0, float(chosen.end or 0.0) - float(chosen.start or 0.0))
            # The review belongs to these exact seconds, not the remainder of the source.
            # Shortages were rejected above; modest slow motion stays within this window.
            copy["reviewed_window"] = {"start": chosen.start, "end": chosen.end,
                                       "source_id": chosen.source_id}
            copy["editorial_evidence"] = dict(chosen.vision or {})
            copy["seedance_end_trim"] = chosen.end
            _speed = _fit_clip_to_beat(copy, _window, _need)
            if _speed:
                _log(status_cb, f"V4 fit: beat {scene.get('id') or _pos} needs "
                                f"{_need:.2f}s and the window supplies {_window:.2f}s - "
                                f"playing it at {_speed:.2f}x instead of holding a still.")
            # The PATH, not the bare name. The consumer in agent_core tests `Path(clip).exists()`
            # before copying the clip into the edit, and only falls back to resolving the name
            # against the project when the entry is empty - so a bare filename is truthy, fails
            # the existence test against the working directory, and the scene is silently dropped.
            # Measured 2026-09-02: V4 assigned 10 of 10 beats, every file was on disk, and the
            # saved project came back with one clip and nine "uncovered_still" slates. V3 has
            # always appended `str(dest)` here.
            out_scenes[_pos] = copy; scene_clips[_pos] = str(dest)
            used_sources[chosen.source_id] = used_sources.get(chosen.source_id, 0) + 1
            if chosen.fingerprint:
                used_fingerprints.append((chosen.source_id, chosen.fingerprint,
                                          getattr(chosen, "window_fingerprint", "")))
            used_ranges.append((chosen.source_id, chosen.start, chosen.end)); chosen.status = "assigned"

        # The beat's own query set, keyed by its POSITION in the edit - out_scenes is built in
        # task order, which is not the same as the original scene index.
        _wanted_by_position = {pos: {q.casefold() for q in qs}
                               for pos, (_idx, _sc, qs) in enumerate(tasks)}
        _uncovered = [i for i, clip in enumerate(scene_clips) if clip is None]
        if _uncovered:
            _shelf = sorted((c for c in candidates
                             if c.status != "assigned" and Path(str(c.path or "")).is_file()),
                            key=fill_rank, reverse=True)
            for index in _uncovered:
                # ON TOPIC BEATS UNUSED. Measured on a 30s Fact Short: 7 distinct accepted
                # sources for 14 beats, so 8 beats were filled - and filling them from the
                # leftover pile meant 8 shots that had been found for a DIFFERENT sentence. A
                # second, well-separated moment from a source THIS beat's own query returned is
                # still about this beat; an unrelated clip never is. So the order is:
                #   1. another window of a source this beat itself found
                #   2. an accepted source nobody is using yet
                #   3. anything left that does not repeat a window already on screen
                wanted_here = _wanted_by_position.get(index) or set()
                try:
                    _beat_needs = max(0.0, float(out_scenes[index].get("end") or 0.0)
                                      - float(out_scenes[index].get("start") or 0.0))
                except (TypeError, ValueError):
                    _beat_needs = 0.0
                budget_fallback = _vision_budget_spent(project)
                pick, needs_replacement = _pick_coverage_fill(
                    _shelf, wanted_here, used_sources, used_ranges, used_fingerprints,
                    need=_beat_needs, scene=out_scenes[index],
                    allow_reviewed_fallback=budget_fallback)
                if pick is None:
                    continue
                _shelf.remove(pick)
                dest_dir = project / "seedance 2.0"; dest_dir.mkdir(parents=True, exist_ok=True)
                name = f"v4_{index:02d}_{pick.source_id}.mp4"; dest = dest_dir / name
                if not dest.exists(): shutil.copy2(pick.path, dest)
                scene = out_scenes[index]
                shown = max(0.0, float(scene.get("end") or 0.0)
                            - float(scene.get("start") or 0.0))
                window = max(0.0, float(pick.end or 0.0) - float(pick.start or 0.0))
                cut_at = float(pick.start or 0.0)
                if 0.0 < shown < window - .05:
                    cut_at = editorial.action_trim(
                        pick.start, pick.end, shown, pick.vision or {})
                    if cut_at is None:
                        continue
                scene.update({"clip": name, "asset": name, "seedance": True,
                              "seedance_start_trim": round(cut_at, 3),
                              "scrape_source": f"brightdata_{pick.source_id.split('__', 1)[0]}",
                              "scrape_clip_id": pick.source_id,
                              "assignment_type": ("v4_budget_fallback" if needs_replacement
                                                  else "v4_verified_fill"),
                              "match_class": ("reviewed_fallback" if needs_replacement
                                              else "vision_matched"),
                              # `coverage_gap` is the flag the timeline editor already draws as a
                              # red border plus a NO MATCH tag - V3 has used it for the same
                              # situation for a while, so V4 speaks the same language rather than
                              # inventing a second field the editor would ignore.
                              "coverage_gap": needs_replacement,
                              "coverage_gap_reason": (
                                  "Vision budget ended before this narration line received an exact "
                                  "match; this is technically accepted footage selected from the "
                                  "reviewed pool." if needs_replacement else ""),
                              "needs_replacement": needs_replacement,
                              "blur_captions": pick.caption_share > 0.015,
                              "source_has_captions": pick.caption_share > 0.015,
                              "v4_reason": pick.reason})
                scene["reviewed_window"] = {"start": pick.start, "end": pick.end,
                                            "source_id": pick.source_id}
                scene["editorial_evidence"] = dict(pick.vision or {})
                scene["seedance_end_trim"] = pick.end
                _fit_clip_to_beat(scene, window, shown)
                scene_clips[index] = str(dest)
                used_sources[pick.source_id] = used_sources.get(pick.source_id, 0) + 1
                pick.status = "assigned"
                used_ranges.append((pick.source_id, pick.start, pick.end))
                # Every later fill has to see this one, or the second repeat is as blind as the first.
                if pick.fingerprint:
                    used_fingerprints.append((pick.source_id, pick.fingerprint,
                                              getattr(pick, "window_fingerprint", "")))
                if needs_replacement:
                    _log(status_cb, f"V4 budget fallback: beat {scene.get('id') or index} uses "
                                    "technically accepted reviewed footage and is marked for replacement.")
                else:
                    _log(status_cb, f"V4 verified fill: beat {scene.get('id') or index} uses another "
                                    f"window reviewed for this exact beat "
                                    f"({float((pick.vision or {}).get('relevance') or 0):.0f}/10) and "
                                    "keeps its action evidence.")
        # Let the available footage determine the visual cuts inside a narration beat.
        expanded_scenes, expanded_clips = [], []
        for scene, clip in zip(out_scenes, scene_clips):
            parts = (_combine_short_windows(scene, candidates, used_sources, used_ranges,
                                            used_fingerprints) if clip is None else [])
            if not parts:
                expanded_scenes.append(scene); expanded_clips.append(clip)
                continue
            for child, candidate, trim in parts:
                dest = project / "seedance 2.0" / f"v4_combo_{len(expanded_scenes):03d}_{candidate.source_id}.mp4"
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(candidate.path, dest)
                child.update(clip=dest.name, asset=dest.name, seedance=True,
                             seedance_start_trim=trim, seedance_end_trim=candidate.end,
                             scrape_clip_id=candidate.source_id, assignment_type="v4_verified_sequence",
                             reviewed_window={"start": candidate.start, "end": candidate.end,
                                              "source_id": candidate.source_id},
                             editorial_evidence=dict(candidate.vision or {}),
                             source_has_captions=candidate.caption_share > .015,
                             blur_captions=candidate.caption_share > .015)
                _fit_clip_to_beat(child, candidate.end - candidate.start, child["end"] - child["start"])
                candidate.status = "assigned"
                used_sources[candidate.source_id] = used_sources.get(candidate.source_id, 0) + 1
                used_ranges.append((candidate.source_id, candidate.start, candidate.end))
                used_fingerprints.append((candidate.source_id, candidate.fingerprint, candidate.window_fingerprint))
                expanded_scenes.append(child); expanded_clips.append(str(dest))
            _log(status_cb, f"V4: beat {scene.get('id')} covered by two short shots instead of requiring one long source.")
        out_scenes, scene_clips = expanded_scenes, expanded_clips
        fallback_used = any(str(item.get("_source") or "") == "authenticated_tiktok"
                            for _query, item in records) or any(
                                str(item.get("_downloaded_via") or "") == "authenticated_tiktok_recovery"
                                for _query, item in records)
        report = {"engine": V4_NAME, "discovery_provider": "scrapedo" if use_scrapedo else "brightdata",
                  "bright_first": not use_scrapedo,
                  "local_tiktok_recovery_used": fallback_used, "queries": requested,
                  "assigned": sum(x is not None for x in scene_clips), "total": len(out_scenes),
                  # What discovery actually cost, per round, so a run can be priced from its own
                  # artefact instead of from a status line that scrolls away.
                  "discovery_cost": round(discovery_cost, 4),
                  "discovery_rounds": discovery_rounds,
                  "candidates": [asdict(x) for x in candidates]}
        _save_assignment_checkpoint(project, out_scenes, scene_clips, report)
        (review / "scrape_v4_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        config["_scrape_v4"] = report
        covered = sum(x is not None for x in scene_clips)
        if not covered:
            # Nothing at all was downloaded and verified, so there is no edit to open.
            policy_blocks = sum("special permission" in str(x.rejection).casefold()
                                or "policy_20050" in str(x.rejection).casefold()
                                for x in candidates)
            if policy_blocks and not fallback_used:
                raise RuntimeError("V4 Bright TikTok media delivery is blocked by Bright policy_20050 "
                                   "(special permission/KYC required for the target CDN). Nothing was downloaded.")
            raise RuntimeError("V4 found no usable footage at all for this script; "
                               "inspect review/scrape_v4_report.json.")
        missing = [str(out_scenes[i].get("id") or i + 1)
                   for i, clip in enumerate(scene_clips) if clip is None]
        if missing:
            _log(status_cb, "V4: " + str(len(missing)) + " replacement slot(s) remain after "
                            "budgeted review; continuing to the render with the saved timeline.")
        return out_scenes, scene_clips
    finally:
        clip_scraper.set_brightdata_only(old_mode)
