"""
clip_scraper.py - download real short-form clips (TikTok / Instagram / YouTube Shorts)
and normalize them to clean 9:16 1080x1920 segments. This powers the "scrape" clip
source used by the Japanese-facts style preset, where the visuals are real found
footage cut together instead of AI-generated video/images.

Quality gates applied to every candidate before it is accepted (so the output looks
like the reference channels, not a random download):
  - VERTICAL ONLY: landscape sources are rejected, so there is no ugly side-crop.
  - DECENT RESOLUTION: tiny / low-res sources are rejected (prefers >=720 on the long side).
  - NO BURNED-IN CAPTIONS: an OpenCV heuristic rejects clips that already have large
    edited text/caption lines (which would clash with our own captions). It targets big
    horizontal centre text, not incidental street signage.
  - HOOK = A WOMAN: when the style terms mention women, the first clip (the scroll-stop
    hook) is pulled from a dedicated "attractive japanese woman" query.

Be honest about the limits:
  - TikTok and Instagram have no open search API; discovery there is best-effort and
    usually needs a COOKIES_FILE. YouTube Shorts discovery is reliable.
  - The caption/text filter is a heuristic (no OCR engine installed) - it catches obvious
    edited caption bars but can miss stylised text or over-reject very busy neon frames.
  - "Style matching" is term-based, not real CV similarity to a reference video.

scrape_clips() never raises into the render; on failure it returns what it has.
"""

import concurrent.futures
import math
import os
import re
import subprocess
import json
import hashlib
import shutil
import tempfile
import threading
import time
import urllib.request
import urllib.error
from pathlib import Path

try:
    import yt_dlp
except Exception:  # pragma: no cover - module optional at import time
    yt_dlp = None

try:
    import cv2
    import numpy as np
except Exception:  # pragma: no cover - CV filters degrade gracefully
    cv2 = None
    np = None

try:
    import tiktok_login              # logged-in TikTok search backend (primary)
except Exception:  # pragma: no cover - Playwright not installed
    tiktok_login = None

try:
    import twitter_login             # logged-in X/Twitter backend (searched IN PARALLEL)
except Exception:  # pragma: no cover - Playwright not installed
    twitter_login = None

try:
    import instagram_login           # logged-in Instagram Reels backend (searched IN PARALLEL)
except Exception:  # pragma: no cover - Playwright not installed
    instagram_login = None


TARGET_W, TARGET_H = 1080, 1920
DEFAULT_CLIP_SECONDS = 4.0
MIN_LONG_SIDE = 700          # reject sources whose long edge is below this (low quality)
MIN_PORTRAIT_RATIO = 1.20    # height/width must be at least this (reject landscape/square)
CAPTION_FRAME_SAMPLES = 5    # frames sampled per clip for the burned-in-text check
CAPTION_REJECT_FRAMES = 2    # reject the clip if this many sampled frames look captioned
# Found-footage renders add their own captions. Even one persistent creator subtitle clashes
# with them, while blurring a large subtitle destroys the footage underneath. Keep only clips
# whose OCR load stays in the incidental-sign/watermark band.
MAX_ACCEPTED_TEXT_HEAVINESS = 1.5

# Lead query for the hook clip when the style is "women-forward" (the reference channels
# always open on an attractive woman as the scroll-stop). Used on YouTube (anonymous).
DEFAULT_WOMAN_LEAD = "beautiful japanese woman, tokyo street style, fashionable, candid"

# When CONNECTED (cookies), real influencer footage lives behind TikTok/Instagram
# hashtags. The hook is pulled from these so it is a real woman, not AI filler.
WOMAN_TAGS = ["japanesegirl", "tokyofashion", "ootdjapan", "japanstyle", "tokyostreetstyle", "fyp"]

# very small English stop list - enough to pull nouns out of a script line
_STOP = set(
    "the a an and or but if then than that this these those of to in on at by for with "
    "from into over under as is are was were be been being it its it's they them their "
    "you your we our he she his her i me my no not so do does did has have had will would "
    "can could should about up down out off here there what when where who why how".split()
)


def _status(status_cb, message):
    if status_cb:
        try:
            status_cb(message)
        except Exception:
            pass


def _ffmpeg_tools():
    """Reuse the pipeline's ffmpeg/ffprobe discovery so we behave like the renderer."""
    try:
        import pipeline
        ffmpeg = pipeline.find_ffmpeg()
        ffprobe = pipeline.find_ffprobe(ffmpeg)
        return ffmpeg, ffprobe
    except Exception:
        import shutil
        return shutil.which("ffmpeg"), shutil.which("ffprobe")


def _keywords(text, limit=6):
    words = re.findall(r"[A-Za-z][A-Za-z'-]{2,}", text or "")
    out = []
    for w in words:
        lw = w.lower()
        if lw in _STOP or lw in out:
            continue
        out.append(lw)
        if len(out) >= limit:
            break
    return out


def build_queries(platforms, terms, script_text="", script_relevancy=70, count=4):
    """Turn the style box + script into a handful of search queries.

    script_relevancy is 0-100. High -> bias toward what the narration actually says;
    low -> bias toward the pure visual style terms.
    """
    style = [t.strip() for t in re.split(r"[,\n]", terms or "") if t.strip()]
    script_kw = _keywords(script_text)
    queries = []
    rel = max(0, min(100, int(script_relevancy)))
    if not style and not script_kw:
        style = ["aesthetic b roll"]
    use_script = rel >= 50
    pool = []
    if style:
        pool.extend(style)
    if script_kw and use_script:
        for i, s in enumerate(list(pool)):
            if i < len(script_kw):
                pool[i] = f"{s} {script_kw[i]}"
    elif script_kw and not style:
        pool.extend(script_kw)
    seen = []
    for q in pool:
        q = q.strip()
        if q and q not in seen:
            seen.append(q)
    if not seen:
        seen = ["aesthetic b roll"]
    while len(queries) < max(count, 1):
        queries.append(seen[len(queries) % len(seen)])
    return queries[: max(count, 1)]


# ---- TikTok / Instagram "connection" via the user's logged-in session ----------
# A scrape can authenticate as the user either by reading cookies straight from their
# browser (cookiesfrombrowser, the "Connect" UX) or from an exported cookies.txt.
_COOKIES_SPEC = None  # set per scrape_clips() call: a browser name or a cookies.txt path
_BROWSERS = {"chrome", "edge", "firefox", "brave", "chromium", "opera", "vivaldi", "safari"}


def set_cookies(spec):
    """Remember the connection for this process: a browser name or a cookies.txt path."""
    global _COOKIES_SPEC
    _COOKIES_SPEC = (str(spec).strip() or None) if spec else None


def cookies_active():
    spec = _COOKIES_SPEC or os.environ.get("COOKIES_FILE", "")
    return bool((spec or "").strip())


def _apply_cookies(opts):
    spec = (_COOKIES_SPEC or os.environ.get("COOKIES_FILE", "") or "").strip()
    if not spec:
        return opts
    low = spec.lower()
    if low in _BROWSERS:
        opts["cookiesfrombrowser"] = (low,)
    elif Path(spec).exists():
        opts["cookiefile"] = spec
    return opts


def _ydl_opts(extra=None):
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "skip_download": True,
        "extract_flat": True,
        "ignoreerrors": True,
    }
    _apply_cookies(opts)
    if extra:
        opts.update(extra)
    return opts


# ---- Search/download BACKENDS: logged-in TikTok + X sessions (searched in parallel) ----
# TikTok (tiktok_login.py) is the primary backend; X/Twitter (twitter_login.py) is searched
# AT THE SAME TIME when its login is saved. Both return items in the same schema that
# _item_meta understands, then yt-dlp downloads the mp4 using cookies exported from the
# respective session. No API keys involved.

def tiktok_backend_ready():
    return bool(tiktok_login is not None and tiktok_login.is_ready())


def twitter_backend_ready():
    return bool(twitter_login is not None and twitter_login.is_ready())


def instagram_backend_ready():
    return bool(instagram_login is not None and instagram_login.is_ready())


def normalize_platforms(platforms=None):
    """Return the requested scrape backends using the canonical names."""
    if isinstance(platforms, str):
        platforms = re.split(r"[,\s]+", platforms)
    requested = {str(p or "").strip().lower() for p in (platforms or ("tiktok", "twitter"))}
    out = set()
    if requested & {"tiktok", "tt"}:
        out.add("tiktok")
    if requested & {"x", "twitter", "x.com"}:
        out.add("twitter")
    if requested & {"instagram", "ig", "insta", "reels"}:
        out.add("instagram")
    return out or {"tiktok", "twitter"}


def platform_like_floor(min_likes, platform):
    """Translate a TikTok-scale engagement floor to the source platform."""
    try:
        floor = max(0, int(min_likes or 0))
    except (TypeError, ValueError):
        floor = 0
    if not floor:
        return floor
    p = str(platform).lower()
    if p == "twitter":
        return max(1, int(math.ceil(floor / 4.0)))
    if p == "instagram":            # Reels likes trail TikTok's for the same reach
        return max(1, int(math.ceil(floor / 2.0)))
    return floor


def backend_active(platforms=None):
    """True when at least one requested backend has a saved login."""
    selected = normalize_platforms(platforms)
    return (("tiktok" in selected and tiktok_backend_ready())
            or ("twitter" in selected and twitter_backend_ready())
            or ("instagram" in selected and instagram_backend_ready()))


def backend_name(platforms=None):
    selected = normalize_platforms(platforms)
    names = []
    if "tiktok" in selected and tiktok_backend_ready():
        names.append("tiktok_login")
    if "twitter" in selected and twitter_backend_ready():
        names.append("twitter_login")
    if "instagram" in selected and instagram_backend_ready():
        names.append("instagram_login")
    return "+".join(names) if names else "none"


# Queries already searched THIS RUN (normalized). Buckets, tiers and retry rounds routinely
# produce near-duplicate queries - re-searching them costs ~5-15s each for zero new clips.
_SEEN_QUERIES = set()
_X_ZERO_STREAK = 0
_X_UNAVAILABLE = False
_IG_ZERO_STREAK = 0
_IG_UNAVAILABLE = False
X_QUERY_STOPWORDS = {
    "a", "an", "the", "and", "or", "but", "for", "to", "of", "in", "on", "at",
    "with", "from", "will", "would", "could", "first", "next", "then", "many",
    "its", "it's", "this", "that", "love", "speech", "it", "is", "are", "was",
}
X_QUERY_INTENTS = {"specific_action", "proof_like_social_clip", "specific_proof",
                   "event_object", "exact_action", "viral_incident"}
_X_PROOF_TERMS = {
    "incident", "event", "reaction", "viral", "rule", "law", "service", "machine",
    "vending", "train", "station", "product", "restocking", "worker", "passenger",
    "students", "cleaning", "classroom", "salaryman", "commuter", "convenience store",
    "transport", "delay", "disruption", "self checkout", "proof",
}


def normalize_query_list(value):
    """Return complete query phrases without ever whitespace-splitting a phrase/string."""
    if isinstance(value, (list, tuple, set)):
        candidates = list(value)
    elif isinstance(value, str):
        raw = value.strip()
        if not raw:
            return []
        candidates = None
        if raw.startswith("["):
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    candidates = parsed
            except Exception:
                pass
        if candidates is None:
            candidates = re.split(r"(?:\r?\n|\s*[\u00b7\u2022]\s*)+", raw)
    else:
        return []
    out, seen = [], set()
    for candidate in candidates:
        if not isinstance(candidate, (str, int, float)):
            continue
        query = re.sub(r"^\s*(?:[-*\u2022]+|\d+[.)])\s*", "", str(candidate))
        query = re.sub(r"^[\s,;:|]+|[\s,;:|]+$", "", query)
        query = " ".join(query.split())
        key = _norm_query(query)
        if query and key and key not in seen:
            seen.add(key); out.append(query)
    return out


def meaningful_query_tokens(query):
    return [token for token in re.findall(r"[^\W_]+(?:['’-][^\W_]+)?", str(query), re.UNICODE)
            if token.casefold() not in X_QUERY_STOPWORDS and len(token) > 1]


def is_valid_x_query(query):
    query = " ".join(str(query or "").split()).strip(" ,;:|")
    if not query or len(query) > 180:
        return False
    if re.search(r"[\u3040-\u30ff\u3400-\u9fff]", query):
        return len(re.sub(r"\s+", "", query).lstrip("#")) >= 2
    tokens = meaningful_query_tokens(query)
    if len(tokens) < 2:
        return bool(query.startswith("#") and len(query) >= 4)
    return True


def x_query_is_suitable(query, search_intent=""):
    if not is_valid_x_query(query):
        return False
    if str(search_intent or "").strip().lower() in X_QUERY_INTENTS:
        return True
    low = str(query).casefold()
    return any(term in low for term in _X_PROOF_TERMS)


def x_queries_for_search(value, bucket_terms="", search_intent="", limit=4):
    # ``bucket_terms`` can contain the raw voice script. It is deliberately not promoted
    # to a query: only explicit planner/user query phrases may reach X.
    candidates = normalize_query_list(value)
    out, seen = [], set()
    for query in candidates:
        key = _norm_query(query)
        if x_query_is_suitable(query, search_intent) and key not in seen:
            seen.add(key); out.append(query)
        if len(out) >= max(1, int(limit)):
            break
    return out
_CJK_RE = re.compile(r"[぀-ヿ㐀-鿿]")


def _norm_query(q):
    return " ".join(str(q).replace("#", " ").split()).casefold()


def _jp_first(queries):
    """Japanese queries first - Japanese TikTok content is indexed under native terms, so JP
    queries yield far more usable clips per search than their English variants."""
    return sorted(queries, key=lambda q: 0 if _CJK_RE.search(str(q)) else 1)


def backend_search_health(platforms=None):
    """Cumulative search health across BOTH backends this run: {searches, items, login_wall}.
    Lets the caller fail fast when the backends return NOTHING (logged out / blocked)."""
    total = {"searches": 0, "items": 0, "login_wall": 0}
    selected = normalize_platforms(platforms)
    for platform, mod, ready in (("tiktok", tiktok_login, tiktok_backend_ready()),
                                 ("twitter", twitter_login, twitter_backend_ready()),
                                 ("instagram", instagram_login, instagram_backend_ready())):
        if platform not in selected:
            continue
        if not ready or mod is None:
            continue
        try:
            st = mod.search_stats() or {}
            for k in total:
                total[k] += int(st.get(k) or 0)
        except Exception:
            pass
    return total


def reset_backend_search_health():
    global _X_ZERO_STREAK, _X_UNAVAILABLE, _IG_ZERO_STREAK, _IG_UNAVAILABLE
    _SEEN_QUERIES.clear()               # fresh run -> allow every query once again
    _X_ZERO_STREAK = 0
    _X_UNAVAILABLE = False
    _IG_ZERO_STREAK = 0
    _IG_UNAVAILABLE = False
    for mod in (tiktok_login, twitter_login, instagram_login):
        if mod is not None:
            try:
                mod.reset_search_stats()
            except Exception:
                pass


def _merge_backend_cookies(platforms=None):
    """Write ONE Netscape cookies.txt combining the TikTok + X session cookies (the format is
    domain-scoped, so yt-dlp picks the right ones per URL automatically). Returns the path."""
    selected = normalize_platforms(platforms)
    parts = []
    if "tiktok" in selected and tiktok_backend_ready():
        try:
            ck = tiktok_login.export_cookies_txt()
            if ck and Path(ck).exists():
                parts.append(Path(ck).read_text("utf-8"))
        except Exception:
            pass
    if "twitter" in selected and twitter_backend_ready():
        try:
            ck = twitter_login.export_cookies_txt()
            if ck and Path(ck).exists():
                parts.append(Path(ck).read_text("utf-8"))
        except Exception:
            pass
    if "instagram" in selected and instagram_backend_ready():
        try:
            ck = instagram_login.export_cookies_txt()
            if ck and Path(ck).exists():
                parts.append(Path(ck).read_text("utf-8"))
        except Exception:
            pass
    if not parts:
        return None
    merged = Path(__file__).resolve().parent / "generated_assets" / "merged_cookies.txt"
    merged.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(p.strip() for p in parts if p.strip())
    merged.write_text("# Netscape HTTP Cookie File\n"
                      + "\n".join(l for l in body.splitlines() if l and not l.startswith("#"))
                      + "\n", "utf-8")
    return str(merged)


def _ensure_tiktok_cookies(status_cb=None, platforms=None):
    """Open (lazily) the shared TikTok session ON ITS WORKER THREAD and point yt-dlp at
    the merged cookies. Returns True when the session is ready."""
    ready = tiktok_login.ensure_session(status_cb=status_cb)
    ck = _merge_backend_cookies(platforms)
    if ck:
        set_cookies(ck)            # yt-dlp downloads authenticated per-domain
    return ready


SORT_ALL_ORDER = ("MOST_LIKED", "RELEVANCE", "MOST_VIEWED", "MOST_RECENT")


def sanitize_social_search_query(query):
    """Remove platform boilerplate centrally so every TikTok scrape mode benefits."""
    value = str(query or "").strip()
    value = re.sub(r"(?i)(?<![#\w])(?:tiktok|instagram|youtube\s+shorts?|reels?)(?!\w)", " ", value)
    value = re.sub(r"(?i)(?<![#\w])(?:on\s+)?(?:twitter|x\.com)(?!\w)", " ", value)
    return re.sub(r"\s+", " ", value).strip(" ,;:-")


def _native_hashtag_fallback(query):
    compact = re.sub(r"[^\u3040-\u30ff\u3400-\u9fff0-9]", "", str(query or ""))
    return ("#" + compact) if 2 <= len(compact) <= 18 else ""


def backend_search(query, want, status_cb=None, sort="MOST_LIKED", platforms=None, deadline=None):
    """Search the selected logged-in backends and return one popularity-ranked result set.

    ``deadline`` is an absolute ``time.monotonic()`` value shared with the bucket caller, so
    one weak query cannot silently run beyond the bucket budget.

    ``sort="ALL"`` runs EVERY backend sort order in the viral-priority sequence and merges the
    unique results (MOST_LIKED first, then RELEVANCE, then MOST_VIEWED, then MOST_RECENT) so a
    single query harvests the top clips the platform surfaces under each ordering.
    """
    original_query = str(query or "").strip()
    query = sanitize_social_search_query(original_query)
    if not query:
        _status(status_cb, f"Search skipped platform-only query {original_query!r}.")
        return []
    if query != original_query:
        _status(status_cb, f"Search Controller: cleaned platform boilerplate: {original_query!r} -> {query!r}.")
    if str(sort or "").upper() == "ALL":
        seen, merged = set(), []
        for _mode in SORT_ALL_ORDER:
            if deadline is not None and time.monotonic() >= deadline:
                break
            batch = backend_search(query, want, status_cb=status_cb, sort=_mode,
                                   platforms=platforms, deadline=deadline) or []
            added = 0
            for _it in batch:
                _m = _item_meta(_it) or {}
                _key = str(_m.get("id") or _m.get("url") or "")
                if not _key or _key in seen:
                    continue
                seen.add(_key); merged.append(_it); added += 1
            if status_cb:
                _status(status_cb, f"Sort {_mode}: +{added} new clip(s) ({len(merged)} total) for {query!r}.")
        return merged
    global _X_ZERO_STREAK, _X_UNAVAILABLE, _IG_ZERO_STREAK, _IG_UNAVAILABLE
    selected = normalize_platforms(platforms)
    if _X_UNAVAILABLE:
        selected.discard("twitter")
    if _IG_UNAVAILABLE:
        selected.discard("instagram")
    remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
    if remaining is not None and remaining <= 0:
        return []
    # Ensure the TikTok session exists BEFORE kicking off the X worker: two sync_playwright
    # instances starting at the same moment in different threads race in greenlet dispatch
    # (observed: "Cannot switch to a different thread" on the very first parallel search).
    # Both backends now run on their own dedicated worker threads - this thread only waits.
    tt_ready = (_ensure_tiktok_cookies(status_cb, selected)
                if "tiktok" in selected and tiktok_backend_ready() else False)
    x_future = None
    if "twitter" in selected and twitter_backend_ready():
        # X ONLY receives full, valid visual phrases. Isolated script tokens ("will", "First",
        # "for", ...) are never sent - they can only waste searches and return noise. TikTok is
        # far more tolerant, so it still runs the raw query.
        if is_valid_x_query(query):
            x_future = twitter_login.search_async(
                query, want=int(want), status_cb=lambda _message: None, timeout_s=remaining,
                sort=sort)
        else:
            _status(status_cb, f"X: skipped malformed query {query!r} (not a valid search phrase).")
    ig_future = None
    if "instagram" in selected and instagram_backend_ready():
        # Instagram runs on its own worker thread, concurrently with TikTok + X.
        ig_future = instagram_login.search_async(
            query, want=int(want), status_cb=lambda _message: None, timeout_s=remaining,
            sort=sort)
    items = []
    if tt_ready:
        items = tiktok_login.search_sync(query, want=int(want), status_cb=status_cb,
                                         sort=sort, timeout_s=remaining) or []
        fallback = _native_hashtag_fallback(query) if not items else ""
        if fallback and (deadline is None or time.monotonic() < deadline):
            _status(status_cb, f"TikTok: {query!r} had no relevant results; trying native tag {fallback!r} once.")
            retry_remaining = None if deadline is None else max(0.1, deadline - time.monotonic())
            items = tiktok_login.search_sync(
                fallback, want=int(want), status_cb=status_cb,
                sort=sort, timeout_s=retry_remaining) or []
    if x_future is not None:
        try:
            wait_s = 90.0 if deadline is None else max(0.1, deadline - time.monotonic())
            x_items = x_future.result(timeout=min(90.0, wait_s)) or []
        except concurrent.futures.TimeoutError:
            x_future.cancel()
            _status(status_cb, f"X search timed out for {query!r}; moving to the next query.")
            x_items = []
        except Exception as exc:  # noqa: BLE001
            _status(status_cb, f"X search failed ({exc.__class__.__name__}: {exc}).")
            x_items = []
        if x_items:
            _X_ZERO_STREAK = 0
            ck = _merge_backend_cookies(selected)
            if ck:
                set_cookies(ck)    # make sure yt-dlp has x.com cookies for these downloads
            items.extend(x_items)
        else:
            _X_ZERO_STREAK += 1
            if _X_ZERO_STREAK >= 3 and twitter_login is not None:
                # Learn during the run instead of repeating dozens of visibly empty X Media
                # searches. One independent health probe distinguishes a bad term from a broken,
                # logged-out or challenge-gated X session.
                try:
                    health = twitter_login.health_check(timeout_s=35)
                except Exception:
                    health = {"ok": False}
                if not health.get("ok"):
                    _X_UNAVAILABLE = True
                    _status(status_cb,
                            "Search Controller: X returned no videos repeatedly and failed its "
                            "live health check; disabling X for the rest of this run.")
                else:
                    _X_ZERO_STREAK = 0
                    _status(status_cb,
                            "Search Controller: X is healthy but this query strategy returned "
                            "nothing; switching terms instead of repeating it.")
    if ig_future is not None:
        try:
            wait_s = 90.0 if deadline is None else max(0.1, deadline - time.monotonic())
            ig_items = ig_future.result(timeout=min(90.0, wait_s)) or []
        except concurrent.futures.TimeoutError:
            ig_future.cancel()
            _status(status_cb, f"Instagram search timed out for {query!r}; moving to the next query.")
            ig_items = []
        except Exception as exc:  # noqa: BLE001
            _status(status_cb, f"Instagram search failed ({exc.__class__.__name__}: {exc}).")
            ig_items = []
        if ig_items:
            _IG_ZERO_STREAK = 0
            ck = _merge_backend_cookies(selected)
            if ck:
                set_cookies(ck)    # instagram.com cookies for the yt-dlp reel downloads
            items.extend(ig_items)
        else:
            _IG_ZERO_STREAK += 1
            if _IG_ZERO_STREAK >= 3 and instagram_login is not None:
                try:
                    health = instagram_login.health_check(timeout_s=35)
                except Exception:
                    health = {"ok": False}
                if not health.get("ok"):
                    _IG_UNAVAILABLE = True
                    _status(status_cb,
                            "Search Controller: Instagram returned no Reels repeatedly and failed "
                            "its live health check; disabling Instagram for the rest of this run.")
                else:
                    _IG_ZERO_STREAK = 0
                    _status(status_cb,
                            "Search Controller: Instagram is healthy but this hashtag/keyword "
                            "strategy returned nothing; switching terms instead of repeating it.")
    sort_mode = str(sort or "MOST_LIKED").upper()
    metric = {"MOST_VIEWED": "views", "MOST_RECENT": "created_at"}.get(sort_mode, "likes")
    if sort_mode in {"MOST_LIKED", "MOST_VIEWED", "MOST_RECENT"}:
        items.sort(key=lambda item: (float(item.get("_query_relevance", 0.5)),
                   int((_item_meta(item) or {}).get(metric) or 0)), reverse=True)
    else:
        items.sort(key=lambda item: float(item.get("_query_relevance", 0.5)), reverse=True)
    return items


def _ytdlp_fetch(url, dest, status_cb=None, max_seconds=16.0):
    """Download the first ~max_seconds of a TikTok video URL to an exact path (clean mp4)."""
    if yt_dlp is None or not url:
        return None
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmpl = str(dest.with_suffix("")) + ".%(ext)s"
    fmt = ("bestvideo[height>=600][height<=1920]+bestaudio/"
           "best[height>=600][height<=1920]/best[height<=1920]/best")
    opts = {
        "quiet": True, "no_warnings": True, "noprogress": True,
        "outtmpl": tmpl, "format": fmt, "merge_output_format": "mp4",
        "max_filesize": 80 * 1024 * 1024, "ignoreerrors": True,
    }
    try:
        opts["download_ranges"] = yt_dlp.utils.download_range_func(None, [(0.0, float(max_seconds))])
        opts["force_keyframes_at_cuts"] = True
    except Exception:
        pass
    _apply_cookies(opts)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except Exception as exc:  # noqa: BLE001
        _status(status_cb, f"TikTok download failed ({exc.__class__.__name__}).")
        return None
    for cand in sorted(dest.parent.glob(dest.stem + ".*")):
        if cand.suffix.lower() in (".mp4", ".mov", ".mkv", ".webm") and cand.stat().st_size > 4096:
            if cand != dest:
                try:
                    cand.replace(dest)
                    return dest
                except Exception:
                    return cand
            return dest
    return None


def backend_download(item, dest, status_cb=None):
    """Download a candidate item to dest (yt-dlp with the logged-in session's cookies)."""
    if not isinstance(item, dict):
        return None
    url = item.get("webVideoUrl") or item.get("url") or ""
    return _ytdlp_fetch(url, dest, status_cb=status_cb) if url else None


def close_backend():
    """Tear down the shared TikTok + X + Instagram sessions at the end of a run."""
    for mod in (tiktok_login, twitter_login, instagram_login):
        if mod is not None:
            try:
                mod.close_session()
            except Exception:
                pass


def _entries(info):
    if not info:
        return []
    if "entries" in info and info["entries"]:
        return [e for e in info["entries"] if e]
    return [info]


def discover_urls(platforms, queries, count, status_cb=None):
    """Find candidate clip URLs for the given queries on the given platforms.

    Returns a de-duplicated list of webpage URLs (best-effort).
    """
    if yt_dlp is None:
        _status(status_cb, "Scrape: yt-dlp is not installed, cannot discover clips.")
        return []
    urls = []

    def _collect(target, n):
        try:
            with yt_dlp.YoutubeDL(_ydl_opts({"playlistend": n})) as ydl:
                info = ydl.extract_info(target, download=False)
            for e in _entries(info):
                u = e.get("webpage_url") or e.get("url")
                if u and u.startswith("http") and u not in urls:
                    urls.append(u)
        except Exception as exc:  # noqa: BLE001 - discovery is allowed to fail
            _status(status_cb, f"Scrape: discovery failed for {target!r} ({exc.__class__.__name__}).")

    per_query = max(3, (count // max(len(queries), 1)) + 3)
    for q in queries:
        for plat in platforms:
            plat = plat.lower().strip()
            if plat in ("youtube", "yt", "shorts", "youtube shorts"):
                _collect(f"ytsearch{per_query}:{q} #shorts vertical", per_query)
            elif plat == "tiktok":
                tag = re.sub(r"[^a-z0-9]", "", q.lower().split()[0]) if q else ""
                if tag:
                    _collect(f"https://www.tiktok.com/tag/{tag}", per_query)
            else:
                if q.startswith("http"):
                    _collect(q, per_query)
            if len(urls) >= count * 4:
                break
        if len(urls) >= count * 4:
            break
    return urls


def download_raw(url, out_dir, index, status_cb=None):
    """Download a single clip (preferring tall, >=720p sources) to out_dir. Returns the path or None."""
    if yt_dlp is None:
        return None
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    template = str(out_dir / f"raw_{index:02d}.%(ext)s")
    # Aim for ~720-1080p: high enough to clear the quality gate, capped so we don't pull
    # multi-hundred-MB 4K sections. Portrait is enforced after download by is_vertical_hq.
    fmt = (
        "bestvideo[height>=720][height<=1920]+bestaudio/"
        "best[height>=720][height<=1920]/"
        "bestvideo[height<=1920]+bestaudio/best[height<=1920]/best"
    )
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "outtmpl": template,
        "format": fmt,
        "merge_output_format": "mp4",
        "max_filesize": 60 * 1024 * 1024,
        "ignoreerrors": True,
    }
    # Only fetch the first ~16s, not the whole (possibly multi-minute) video. This keeps
    # downloads fast/small and avoids the size cap + partial-merge probe failures that
    # made long videos look "low-res".
    try:
        opts["download_ranges"] = yt_dlp.utils.download_range_func(None, [(0.0, 16.0)])
        opts["force_keyframes_at_cuts"] = True
    except Exception:
        pass
    _apply_cookies(opts)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except Exception as exc:  # noqa: BLE001
        _status(status_cb, f"Scrape: download failed ({exc.__class__.__name__}).")
        return None
    for cand in sorted(out_dir.glob(f"raw_{index:02d}.*")):
        if cand.suffix.lower() in (".mp4", ".mov", ".mkv", ".webm") and cand.stat().st_size > 4096:
            return cand
    return None


def download_full(url, out_dir, name="discover_src", status_cb=None):
    """Download the WHOLE video (no 16s range cap) for Discovery mode, where one long
    source TikTok is recut to a generated voiceover. Returns the path or None."""
    if yt_dlp is None:
        return None
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    template = str(out_dir / f"{name}.%(ext)s")
    fmt = (
        "bestvideo[height>=720][height<=1920]+bestaudio/"
        "best[height>=720][height<=1920]/"
        "bestvideo[height<=1920]+bestaudio/best[height<=1920]/best"
    )
    opts = {
        "quiet": True, "no_warnings": True, "noprogress": True,
        "outtmpl": template, "format": fmt, "merge_output_format": "mp4",
        "max_filesize": 300 * 1024 * 1024,
        "ignoreerrors": True,
    }
    _apply_cookies(opts)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except Exception as exc:  # noqa: BLE001
        _status(status_cb, f"Discovery: full download failed ({exc.__class__.__name__}).")
        return None
    for cand in sorted(out_dir.glob(f"{name}.*")):
        if cand.suffix.lower() in (".mp4", ".mov", ".mkv", ".webm") and cand.stat().st_size > 4096:
            return cand
    return None


def _probe_dims(path, ffprobe):
    if not ffprobe:
        return None
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", str(path)],
            capture_output=True, text=True, timeout=20).stdout.strip()
        w, h = out.split("x")[:2]
        return int(w), int(h)
    except Exception:
        return None


def _probe_duration(path, ffprobe):
    if not ffprobe:
        return 0.0
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=20).stdout.strip()
        return max(0.0, float(out))
    except Exception:
        return 0.0


def hard_cut_times(path, ffmpeg, scan_seconds=24.0, threshold=0.30):
    """Return hard-cut timestamps using ffmpeg's scene score (no OpenCV dependency)."""
    if not ffmpeg:
        return []
    try:
        cmd = [ffmpeg, "-hide_banner", "-nostats", "-i", str(path)]
        if scan_seconds and float(scan_seconds) > 0:
            cmd += ["-t", f"{float(scan_seconds):.3f}"]
        cmd += ["-vf", f"select='gt(scene,{float(threshold):.3f})',showinfo",
                "-an", "-f", "null", os.devnull]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
        text = f"{result.stdout or ''}\n{result.stderr or ''}"
        return sorted({
            round(float(match), 3)
            for match in re.findall(r"pts_time:([0-9]+(?:\.[0-9]+)?)", text)
            if float(match) > 0.08
        })
    except Exception:
        return []


def stable_segment_profile(path, ffmpeg, ffprobe, seconds=DEFAULT_CLIP_SECONDS):
    """Pick the calmest source window and describe any internal edit jitter.

    Finished TikToks often contain several edits inside the four seconds we reuse. When those
    clips are cut again by our timeline, sub-second cut clusters result. Prefer a continuous
    source window; if none exists, expose the cut count so the caller can reject the montage.
    """
    wanted = max(0.5, float(seconds or DEFAULT_CLIP_SECONDS))
    duration = _probe_duration(path, ffprobe)
    scan = min(duration or max(24.0, wanted * 4.0), max(24.0, wanted * 4.0))
    cuts = hard_cut_times(path, ffmpeg, scan_seconds=scan)
    latest_start = max(0.0, (duration or scan) - wanted)
    starts = {0.0, latest_start}
    for cut in cuts:
        starts.add(max(0.0, min(latest_start, cut + 0.08)))
        starts.add(max(0.0, min(latest_start, cut - wanted - 0.08)))

    best = None
    for start in sorted(starts):
        end = start + wanted
        inside = [cut for cut in cuts if start + 0.10 < cut < end - 0.10]
        bounds = [start] + inside + [end]
        gaps = [bounds[i] - bounds[i - 1] for i in range(1, len(bounds))]
        rapid = sum(1 for gap in gaps if gap < 0.80)
        # Fewer cuts wins first; then avoid tight clusters; then keep the earliest useful action.
        rank = (len(inside), rapid, -min(gaps or [wanted]), start)
        if best is None or rank < best[0]:
            best = (rank, start, inside, gaps)
    _, start, inside, gaps = best or ((0, 0, 0, 0), 0.0, [], [wanted])
    return {
        "start": round(start, 3),
        "duration": round(wanted, 3),
        "source_duration": round(duration, 3),
        "internal_cut_count": len(inside),
        "rapid_internal_cut_count": sum(1 for gap in gaps if gap < 0.80),
        "min_shot_seconds": round(min(gaps or [wanted]), 3),
        "cut_times": [round(cut - start, 3) for cut in inside],
        "stable": len(inside) <= 1 and all(gap >= 0.80 for gap in gaps),
    }


def is_vertical_hq(path, ffprobe):
    """True only for portrait clips with a reasonable resolution (avoids landscape crop + low quality)."""
    dims = _probe_dims(path, ffprobe)
    if not dims:
        return False
    w, h = dims
    if w <= 0 or h <= 0:
        return False
    if h < w * MIN_PORTRAIT_RATIO:   # not clearly portrait
        return False
    if max(w, h) < MIN_LONG_SIDE:    # too low-res
        return False
    return True


def _sample_bgr_frames(path, ffmpeg, n, seconds, width=360):
    """Grab n COLOR (BGR) frames spread across the clip as numpy arrays."""
    if cv2 is None or not ffmpeg:
        return []
    frames = []
    tmp = Path(tempfile.mkdtemp(prefix="capchk_"))
    try:
        for i in range(n):
            t = max(0.1, seconds * (i + 0.5) / n)
            fp = tmp / f"f{i}.jpg"
            subprocess.run([ffmpeg, "-y", "-ss", str(t), "-i", str(path),
                            "-frames:v", "1", "-vf", f"scale={int(width)}:-1", str(fp)],
                           capture_output=True, timeout=30)
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


# ---- REAL text detection (RapidOCR) --------------------------------------------
# The brightness/shape/color heuristic behind the old caption detector scored WHITE SHIRTS,
# bright facades and train roofs as 10/10 "text-plastered" and rejected perfectly clean
# clips (verified on real declined footage 2026-07-03). Actual OCR is the only reliable
# signal: it reads LETTERS, not bright rectangles. RapidOCR (onnxruntime, offline, reads
# Japanese + Latin) runs per-thread; when it is unavailable the text score is 0 and the
# vision matcher remains the text gate.
_OCR_LOCAL = threading.local()


def _get_ocr():
    if getattr(_OCR_LOCAL, "failed", False):
        return None
    ocr = getattr(_OCR_LOCAL, "ocr", None)
    if ocr is None:
        try:
            from rapidocr_onnxruntime import RapidOCR
            ocr = RapidOCR()
            _OCR_LOCAL.ocr = ocr
        except Exception:
            _OCR_LOCAL.failed = True
            return None
    return ocr


def _ocr_text_rows(bgr, min_conf=0.55):
    """Detected REAL text lines in one frame: [(x, y, w, h), ...] (>=2 chars). The default
    confidence suits SCORING; the caption BLUR passes a lower one so partially-styled lines
    (bold outline text the recognizer half-reads) are still covered."""
    ocr = _get_ocr()
    if ocr is None:
        return []
    try:
        result, _ = ocr(bgr, use_det=True, use_cls=False, use_rec=True)
    except Exception:
        return []
    rows = []
    for r in (result or []):
        try:
            quad, text, conf = r[0], str(r[1]), float(r[2])
        except Exception:
            continue
        if conf < min_conf or len(text.strip()) < 2:
            continue
        xs = [p[0] for p in quad]
        ys = [p[1] for p in quad]
        x, y = int(min(xs)), int(min(ys))
        rows.append((x, y, max(1, int(max(xs) - x)), max(1, int(max(ys) - y))))
    return rows


def _sample_gray_frames(path, ffmpeg, n, seconds):
    """Grab n grayscale frames spread across the clip as numpy arrays."""
    return [cv2.cvtColor(f, cv2.COLOR_BGR2GRAY)
            for f in _sample_bgr_frames(path, ffmpeg, n, seconds)]


def _frame_caption_boxes(gray, hsv=None):
    """Find caption-like regions in one frame; returns [(x, y, w, h), ...] in the frame's own
    (scaled) coordinates.

    Catches BOTH burned-caption styles:
      - bright outlined text (white/yellow bold strokes, moderate fill), and
      - TikTok's classic solid WHITE caption BUBBLE with dark text (near-solid bright block).
    When `hsv` is given, a COLOR check rejects bright COLOURED regions (neon signs, lanterns,
    store fronts glow pink/red/orange/blue - captions are WHITE or YELLOW): the bright pixels in
    a candidate box must be predominantly white (low saturation) or caption-yellow.
    """
    if cv2 is None or np is None:
        return []
    boxes = []
    h, w = gray.shape
    y0, y1 = int(0.22 * h), int(0.93 * h)
    band = gray[y0:y1, :]
    # very bright pixels only -> white/yellow caption text or a white caption bubble
    _, bright = cv2.threshold(band, 205, 255, cv2.THRESH_BINARY)
    # drop tiny speckle (scattered city lights are isolated dots, not strokes)
    bright = cv2.morphologyEx(bright, cv2.MORPH_OPEN,
                              cv2.getStructuringElement(cv2.MORPH_RECT, (2, 2)))
    # connect characters horizontally into a word/line blob
    kx = max(12, w // 22)
    ky = max(3, h // 150)
    closed = cv2.morphologyEx(bright, cv2.MORPH_CLOSE,
                              cv2.getStructuringElement(cv2.MORPH_RECT, (kx, ky)))
    cnts, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    for c in cnts:
        x, y, bw, bbh = cv2.boundingRect(c)
        cx = x + bw / 2.0
        if bw > 0.16 * w and 0.028 * h < bbh < 0.22 * h and 0.12 * w < cx < 0.88 * w:
            roi = bright[y:y + bbh, x:x + bw]
            fill = float(roi.mean()) / 255.0
            shape_ok = (0.05 < fill < 0.55) or (fill >= 0.55 and bw > 0.28 * w)
            if not shape_ok:
                continue
            if hsv is not None:
                # caption ink is WHITE (low saturation) or YELLOW; bright coloured blobs are
                # signage/neon and must NOT count.
                m = roi > 0
                if int(m.sum()) >= 12:
                    sroi = hsv[y + y0:y + y0 + bbh, x:x + bw, 1][m].astype(float)
                    hroi = hsv[y + y0:y + y0 + bbh, x:x + bw, 0][m].astype(float)
                    whiteish = sroi < 70
                    yellowish = (sroi >= 70) & (hroi >= 18) & (hroi <= 38)
                    if float((whiteish | yellowish).mean()) < 0.65:
                        continue
            boxes.append((x, y + y0, bw, bbh))
    return boxes


def _frame_caption_lines(gray):
    """Count caption-like text lines in one frame (see _frame_caption_boxes)."""
    return len(_frame_caption_boxes(gray))


def _cluster_caption_boxes(per_frame_boxes):
    """Cluster caption boxes ACROSS frames by screen position (IoU > 0.3). Burned-in captions sit
    still; signage/scene text moves with the camera and never lines up. Returns a list of
    {"box": (x, y, w, h) union, "frames": set(frame_idx)}."""
    flat = [(fi, b) for fi, bs in enumerate(per_frame_boxes) for b in bs]
    used = [False] * len(flat)

    def _iou(a, b):
        ax, ay, aw, ah = a; bx, by, bw, bh = b
        ix = max(0, min(ax + aw, bx + bw) - max(ax, bx))
        iy = max(0, min(ay + ah, by + bh) - max(ay, by))
        inter = ix * iy
        union = aw * ah + bw * bh - inter
        return inter / union if union else 0.0

    clusters = []
    for i, (fi, b) in enumerate(flat):
        if used[i]:
            continue
        used[i] = True
        members, seen_frames = [b], {fi}
        for j in range(i + 1, len(flat)):
            if used[j]:
                continue
            fj, b2 = flat[j]
            if _iou(b, b2) > 0.30:
                used[j] = True
                members.append(b2)
                seen_frames.add(fj)
        xs = [m[0] for m in members]; ys = [m[1] for m in members]
        x2 = [m[0] + m[2] for m in members]; y2 = [m[1] + m[3] for m in members]
        clusters.append({"box": (min(xs), min(ys), max(x2) - min(xs), max(y2) - min(ys)),
                         "frames": seen_frames})
    return clusters


def has_burned_captions(path, ffmpeg, seconds=DEFAULT_CLIP_SECONDS, status_cb=None):
    """True only for screen-fixed, caption-shaped text persistent across sampled frames.

    Natural scene text (vending-machine labels, storefront signs, packaging) may produce lots
    of OCR, but it must not be treated as a creator subtitle merely because it is readable.
    """
    if cv2 is None or np is None:
        return False  # can't check -> don't block
    frames = _sample_bgr_frames(path, ffmpeg, CAPTION_FRAME_SAMPLES, seconds)
    if not frames:
        return False
    h, w = frames[0].shape[:2]
    per_frame = []
    for frame in frames:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
        per_frame.append(_frame_caption_boxes(gray, hsv=hsv))
    clusters = _cluster_caption_boxes(per_frame)
    required_frames = max(3, int(math.ceil(len(frames) * 0.6)))
    for cluster in clusters:
        x, y, bw, bh = cluster["box"]
        cx = x + bw / 2.0
        # Creator subtitles are broad, screen-centred lines locked to the same UI position.
        # Product labels tend to be smaller/multiple/object-bound and fail this geometry.
        if (len(cluster["frames"]) >= required_frames
                and bw >= 0.25 * w
                and 0.22 * w <= cx <= 0.78 * w
                and 0.20 * h <= y <= 0.90 * h
                and bh <= 0.24 * h):
            return True
    return False


def normalize_clip(src, out_path, ffmpeg, seconds=DEFAULT_CLIP_SECONDS, start=0.0):
    """Trim + centre-crop a raw download into a uniform 9:16 1080x1920 mp4."""
    if not ffmpeg or not src:
        return None
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    vf = (
        f"scale={TARGET_W}:{TARGET_H}:force_original_aspect_ratio=increase,"
        f"crop={TARGET_W}:{TARGET_H},setsar=1,fps=30"
    )
    cmd = [
        ffmpeg, "-y", "-ss", str(max(0.0, start)), "-i", str(src),
        "-t", str(seconds), "-vf", vf,
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast", "-crf", "20",
        "-an", str(out_path),
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=180)
    except Exception:
        return None
    return out_path if out_path.exists() and out_path.stat().st_size > 4096 else None


def _evaluate_url(url, raw_dir, ffmpeg, ffprobe, per_clip_seconds, status_cb):
    """Download one URL; accept if HQ + no burned-in captions.

    Returns (raw_path, is_portrait) when accepted, else None (and deletes the reject).
    Portrait is preferred by the caller; landscape is only used to avoid starving.
    """
    raw = download_raw(url, raw_dir, _evaluate_url._n, status_cb=status_cb)
    _evaluate_url._n += 1
    if not raw:
        return None
    dims = _probe_dims(raw, ffprobe)
    if not dims or max(dims) < MIN_LONG_SIDE:
        _status(status_cb, "Scrape: skipped a clip (low-res).")
        try:
            raw.unlink()
        except Exception:
            pass
        return None
    w, h = dims
    portrait = h >= w * MIN_PORTRAIT_RATIO
    if has_burned_captions(raw, ffmpeg, seconds=per_clip_seconds, status_cb=status_cb):
        _status(status_cb, "Scrape: skipped a clip (burned-in captions/text).")
        try:
            raw.unlink()
        except Exception:
            pass
        return None
    return raw, portrait


_evaluate_url._n = 0


# ---- Metadata-driven pre-download filtering + post-download junk rejection -------
# Reject obvious junk by metadata BEFORE spending a download, then reject fake-vertical /
# black-bar / text-heavy clips AFTER download. The vision matcher in agent_core is still the
# authoritative relevance gate; these gates just stop junk from entering the pool/media panel.
_ANIME_GAME_TERMS = ("anime", "vtuber", "v-tuber", "アニメ", "vチューバー", "vtuver", "切り抜き",
                     "ゲーム実況", "実況", "原神", "ガチャ", "mmd", "cosplay", "コスプレ",
                     "fortnite", "フォートナイト", "apex", "valorant", "minecraft", "マイクラ", "cgi")
_NEWS_QUIZ_TERMS = ("ニュース", " news", "診断", "心理テスト", "テスト", "quiz", "クイズ", "占い",
                    "アンケート", "ランキング", "まとめ動画", "diagnosis", "personality test")
_LIVE_SCREEN_TERMS = ("ライブ配信", "生配信", "live配信", "配信中", "screen recording", "画面録画",
                      "実況プレイ", "live stream", "livestream")
_IDOL_PROMO_TERMS = ("オーディション", "audition", "アイドル募集", "案件", "プロモ", "宣伝",
                     "広告", "sponsored", "#pr", "#ad")
# AI-generated "photoreal Japan aesthetic" accounts flood 満員電車/新宿-style searches with
# fake footage (verified live: 3 of 4 accepted clips were @ArtGen-style AI videos). Their
# metadata almost always self-labels. Substring terms must be UNAMBIGUOUS (never bare "ai" -
# it matches 'air', 'sakai', ...); short tokens are matched exactly against the hashtag list.
_AI_CONTENT_TERMS = ("aiart", "ai art", "ai-generated", "ai generated", "aigenerated",
                     "generated by ai", "made with ai", "created with ai", "midjourney",
                     "stable diffusion", "stablediffusion", "comfyui", "civitai", "runway",
                     "texttovideo", "text to video", "ai video", "aivideo", "ai girl",
                     "aigirl", "ai beauty", "aibeauty", "ai model", "aimodel", "artgen",
                     "aigen", "ai_gen", "kling ai", "pika labs", "luma dream", "grok imagine",
                     "ai生成", "ai動画", "ai美女", "aiグラビア", "aiモデル", "aiイラスト", "ai美人")
_AI_HASHTAGS = {"ai", "aiart", "aigirl", "aivideo", "aimodel", "aibeauty", "aigravure",
                "sora", "midjourney", "veo", "veo3", "kling", "aiaesthetic", "aiphoto"}


def _item_meta(item):
    """Normalize a raw scraper item (novi raw-TikTok OR clockworks) into a flat metadata dict."""
    if not isinstance(item, dict):
        return {}
    m = {}
    m["caption"] = str(item.get("desc") or item.get("text") or item.get("title") or "")
    a = item.get("author") if isinstance(item.get("author"), dict) else (
        item.get("authorMeta") if isinstance(item.get("authorMeta"), dict) else {})
    m["author"] = str(a.get("uniqueId") or a.get("unique_id") or a.get("name") or "")
    m["author_name"] = str(a.get("nickname") or a.get("nickName") or a.get("nick_name") or "")
    m["author_sig"] = str(a.get("signature") or "")
    v = item.get("video") if isinstance(item.get("video"), dict) else {}
    vm = item.get("videoMeta") if isinstance(item.get("videoMeta"), dict) else {}
    try:
        m["w"] = int(v.get("width") or vm.get("width") or 0)
    except (TypeError, ValueError):
        m["w"] = 0
    try:
        m["h"] = int(v.get("height") or vm.get("height") or 0)
    except (TypeError, ValueError):
        m["h"] = 0
    try:
        raw_duration = float(v.get("duration") or vm.get("duration") or 0)
        # Novi returns TikTok's raw `video.duration` in milliseconds, while Clockworks'
        # `videoMeta.duration` is normally seconds.  Treat four/five-digit raw-video values
        # as milliseconds; otherwise every normal 10-60 second TikTok is rejected as >10 min.
        if v.get("duration") is not None and raw_duration >= 1000:
            raw_duration /= 1000.0
        m["duration"] = raw_duration
    except (TypeError, ValueError):
        m["duration"] = 0.0
    tags = []
    for t in (item.get("textExtra") or []):
        if isinstance(t, dict) and t.get("hashtagName"):
            tags.append(str(t["hashtagName"]))
    for t in (item.get("challenges") or []):
        if isinstance(t, dict) and t.get("title"):
            tags.append(str(t["title"]))
    for t in (item.get("hashtags") or []):
        if isinstance(t, dict) and t.get("name"):
            tags.append(str(t["name"]))
    m["hashtags"] = [h.lower() for h in tags]
    mu = item.get("music") if isinstance(item.get("music"), dict) else {}
    m["music"] = str(mu.get("title") or mu.get("musicName") or "")
    m["is_image_post"] = bool(item.get("imagePost") or item.get("imagePostInfo")
                              or item.get("image_post_info"))
    m["has_text_stickers"] = bool(item.get("stickersOnItem") or item.get("stickers"))
    m["id"] = str(item.get("id") or item.get("aweme_id") or item.get("itemId") or "")
    m["url"] = str(item.get("webVideoUrl") or item.get("shareUrl") or item.get("url") or "")
    # Instagram (instagram_login backend) carries the thumbnail at the item's top-level "cover",
    # not inside a TikTok-shaped video/videoMeta dict - fall back to it so IG previews resolve.
    m["cover"] = str(v.get("cover") or v.get("originCover") or vm.get("coverUrl")
                     or item.get("cover") or item.get("thumbnail") or "")
    stats = item.get("statistics") if isinstance(item.get("statistics"), dict) else (
        item.get("stats") if isinstance(item.get("stats"), dict) else {})
    try:
        m["likes"] = int(float(
            item.get("diggCount") or item.get("digg_count") or item.get("likeCount")
            or stats.get("diggCount") or stats.get("digg_count") or stats.get("likeCount") or 0
        ))
    except (TypeError, ValueError):
        m["likes"] = 0
    try:
        m["views"] = int(float(
            item.get("playCount") or item.get("play_count") or item.get("viewCount")
            or stats.get("playCount") or stats.get("play_count") or stats.get("viewCount") or 0
        ))
    except (TypeError, ValueError):
        m["views"] = 0
    try:
        m["created_at"] = int(float(item.get("createTime") or item.get("create_time")
                                     or item.get("created_at") or 0))
    except (TypeError, ValueError):
        m["created_at"] = 0
    m["platform"] = str(item.get("_platform") or "tiktok")
    return m


def pre_download_candidate_filter(item, bucket_terms="", seen_ids=None, min_likes=0):
    """Decide BEFORE download whether a candidate is worth fetching, from metadata only.
    Returns (accept: bool, reason: str, meta: dict).

    Metadata text remains a weak signal, so fuzzy topic/style flags only affect ranking.
    Reliable quality failures and the caller-supplied minimum-like floor are hard rejects.
    Surviving candidates are ranked by likes before download."""
    m = _item_meta(item)
    seen_ids = seen_ids if seen_ids is not None else set()
    blob = " ".join([m.get("caption", ""), " ".join(m.get("hashtags", [])),
                     m.get("author", ""), m.get("author_name", ""), m.get("author_sig", ""),
                     m.get("music", "")]).lower()
    vid = m.get("id") or m.get("url")
    if vid and vid in seen_ids:
        return False, "duplicate video id/url", m
    if m.get("is_image_post"):
        return False, "slideshow/image post", m
    w, h = m.get("w", 0), m.get("h", 0)
    if w and h:
        if h < w:                                      # true landscape by exact metadata
            return False, "landscape source (metadata dimensions)", m
        if max(w, h) < 600:
            return False, "too low resolution", m
    dur = m.get("duration", 0.0)
    if dur and (dur < 1.5 or dur > 600):
        return False, "duration out of range", m
    if any(t in blob for t in _LIVE_SCREEN_TERMS):
        return False, "livestream/screen recording", m
    # Self-labeled AI content stays a HARD reject: the tags are unambiguous by design
    # (never a bare "ai") and generated footage must not enter a found-footage edit.
    tags = {str(t).lstrip("#").strip().lower() for t in (m.get("hashtags") or [])}
    if any(t in blob for t in _AI_CONTENT_TERMS) or (tags & _AI_HASHTAGS):
        return False, "ai-generated content", m
    # X engagement is structurally lower than TikTok. Use the same 4x normalization as ranking:
    # TikTok body/hook floors stay 10K/20K; X equivalents become 2.5K/5K.
    like_floor = platform_like_floor(min_likes, m.get("platform"))
    m["effective_min_likes"] = like_floor
    if like_floor and int(m.get("likes") or 0) < like_floor:
        return False, f"below minimum likes ({int(m.get('likes') or 0):,} < {like_floor:,})", m

    # ---- soft signals: penalties + popularity bonus -> rank_score (higher = fetch first)
    penalty = 0.0
    flags = []
    if any(t in blob for t in _ANIME_GAME_TERMS):
        penalty += 3.0; flags.append("possible_anime_gaming")
    if any(t in blob for t in _NEWS_QUIZ_TERMS):
        penalty += 2.0; flags.append("possible_news_textpost")
    if any(t in blob for t in _IDOL_PROMO_TERMS):
        penalty += 2.0; flags.append("possible_promo_ad")
    if w and h and h < w * MIN_PORTRAIT_RATIO:
        penalty += 1.5; flags.append("squareish_aspect")
    likes = int(m.get("likes") or 0)
    if m.get("platform") == "twitter":
        likes *= 4                                     # X likes run ~4x lower for equal reach
    m["penalty"] = round(penalty, 1)
    m["penalty_flags"] = flags
    m["rank_score"] = round(math.log10(likes + 1) - penalty, 2)
    return True, "passed metadata pre-filter", m


def detect_fake_vertical_or_black_bars(path, ffmpeg, seconds=DEFAULT_CLIP_SECONDS):
    """Detect horizontal footage padded into a 9:16 canvas (letterbox/pillarbox) by measuring
    contiguous all-black borders across sampled frames. Returns
    {is_fake_vertical, black_bar_score 0-10, content_aspect_ratio_estimate, reason}."""
    out = {"is_fake_vertical": False, "black_bar_score": 0.0,
           "content_aspect_ratio_estimate": "unknown", "reason": "no frames / cv unavailable"}
    if cv2 is None or np is None:
        return out
    frames = _sample_gray_frames(path, ffmpeg, 5, seconds)
    if not frames:
        return out
    tops, bots, lefts, rights = [], [], [], []
    for g in frames:
        h, w = g.shape
        dark = g < 20
        row_black = dark.mean(axis=1) > 0.97
        col_black = dark.mean(axis=0) > 0.97

        def _lead(mask):
            c = 0
            for v in mask:
                if v:
                    c += 1
                else:
                    break
            return c
        t = _lead(row_black); b = _lead(row_black[::-1])
        l = _lead(col_black); r = _lead(col_black[::-1])
        tops.append(t / h); bots.append(b / h); lefts.append(l / w); rights.append(r / w)
    med = lambda xs: sorted(xs)[len(xs) // 2]
    tb = med(tops) + med(bots)
    lr = med(lefts) + med(rights)
    h0, w0 = frames[0].shape
    content_h = h0 * max(0.0, 1 - tb)
    content_w = w0 * max(0.0, 1 - lr)
    car = content_h / max(1.0, content_w)            # content height/width (portrait >> 1.0)
    is_fake = (tb > 0.12) or (car < 1.15)
    score = round(min(10.0, max(tb, lr) * 22.0), 1)
    if tb > 0.12:
        reason = f"letterbox: {tb*100:.0f}% black top/bottom (horizontal source padded to 9:16)"
    elif lr > 0.12:
        reason = f"pillarbox: {lr*100:.0f}% black sides"
    elif car < 1.15:
        reason = f"content ~16:9 inside vertical frame (h/w={car:.2f})"
    else:
        reason = "clean vertical"
    out.update({"is_fake_vertical": bool(is_fake), "black_bar_score": float(score),
                "content_aspect_ratio_estimate": f"{content_w:.0f}x{content_h:.0f} (h/w={car:.2f})",
                "reason": reason})
    return out


def _ocr_frame_stats(path, ffmpeg, seconds=DEFAULT_CLIP_SECONDS, n=4, width=540):
    """OCR n sampled frames. Returns (area_fracs, line_counts, texts): per-frame text-area
    fraction + confident line count for the heaviness score, and ALL read strings (looser
    conf 0.4) so callers can check for AI-art watermarks like '@ArtGenTokyo'."""
    if cv2 is None or np is None:
        return [], [], []
    bgr = _sample_bgr_frames(path, ffmpeg, n, seconds, width=width)
    ocr = _get_ocr()
    if not bgr or ocr is None:
        return [], [], []
    areas, lines, texts = [], [], []
    for f in bgr:
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
            xs = [p[0] for p in quad]
            ys = [p[1] for p in quad]
            area += max(1.0, max(xs) - min(xs)) * max(1.0, max(ys) - min(ys))
            cnt += 1
        areas.append(area / float(w * h))
        lines.append(cnt)
    return areas, lines, texts


def _score_from_ocr(areas, lines):
    if not areas:
        return 0.0
    mean_area_pct = 100.0 * sum(areas) / len(areas)
    mean_lines = sum(lines) / float(len(lines))
    return round(min(10.0, mean_area_pct * 0.85 + mean_lines * 0.35), 1)


def is_captioned_candidate(text_heaviness, persistent_caption_shape=False):
    """Hard caption gate: require both OCR evidence and screen-fixed subtitle geometry.

    OCR load alone is natural on vending machines, menus, signs and packaging.
    """
    try:
        score = float(text_heaviness or 0.0)
    except (TypeError, ValueError):
        score = 0.0
    return bool(persistent_caption_shape and score > 0.55)


def balanced_platform_candidates(scored, limit, selected_platforms=None):
    """Keep the globally strongest rows; never force an artificial TikTok/X quota."""
    limit = max(0, int(limit or 0))
    rows = list(scored or [])
    rows.sort(key=lambda row: row[0], reverse=True)
    return rows[:limit] if limit else []


# watermark patterns of AI-art accounts, matched against OCR-READ on-screen text
_AI_WATERMARK_RE = re.compile(
    r"artgen|aigen|ai[\s_.\-]?art|ai[\s_.\-]?generated|midjourney|stable\s*diffusion|"
    r"grok|aigc|ai[\s_.\-]?video|@ai\b", re.IGNORECASE)


def text_heaviness_score(path, ffmpeg, seconds=DEFAULT_CLIP_SECONDS):
    """0-10 burned-in text load, measured by REAL OCR on 4 sampled frames.

    Calibrated on real declined footage (2026-07-03): clean clips 0, a single caption line
    ~1-5, a multi-line info overlay ~4-7, a text wall (text post / karaoke subtitles) >8.5.
    The previous brightness heuristic scored white shirts and bright facades as 10/10
    "text-plastered" and rejected perfectly clean clips. Returns 0.0 when OCR is
    unavailable - the vision matcher remains the text gate in that case."""
    areas, lines, _texts = _ocr_frame_stats(path, ffmpeg, seconds)
    return _score_from_ocr(areas, lines)


def _cluster_text_lines(per_frame, frame_w):
    """Cluster OCR text boxes across frames into CAPTION LINES. Word-by-word captions change
    their WIDTH every second at the same screen line, so strict IoU clustering split them into
    fragments (-> half-blurred captions). Two boxes belong to the same line when they overlap
    vertically by >=50% of the smaller height AND their horizontal centers sit within 35% of
    the frame width. Returns [{"box": union, "frames": set}]."""
    clusters = []
    for fi, boxes in enumerate(per_frame):
        for (x, y, w, h) in boxes:
            cx = x + w / 2.0
            placed = False
            for c in clusters:
                X, Y, W, H = c["box"]
                ov = min(y + h, Y + H) - max(y, Y)
                if ov >= 0.5 * min(h, H) and abs(cx - (X + W / 2.0)) <= 0.35 * frame_w:
                    nx, ny = min(x, X), min(y, Y)
                    c["box"] = (nx, ny, max(x + w, X + W) - nx, max(y + h, Y + H) - ny)
                    c["frames"].add(fi)
                    placed = True
                    break
            if not placed:
                clusters.append({"box": (x, y, w, h), "frames": {fi}})
    return clusters


def _caption_text_stroke_mask(frames, per_frame_boxes, allowed_regions=None):
    """Build a feathered mask of caption GLYPHS, never their rectangular OCR boxes.

    OCR only supplies line quadrilaterals. Inside those lines, local contrast plus common
    white/yellow caption colours isolate letter strokes and their outlines. The union across
    sampled frames handles changing word-by-word captions without masking the scene between
    letters. ``allowed_regions`` limits work to persistent caption clusters.
    """
    if cv2 is None or np is None or not frames:
        return None
    fh, fw = frames[0].shape[:2]
    mask = np.zeros((fh, fw), dtype=np.uint8)

    def overlaps_region(box):
        if not allowed_regions:
            return True
        x, y, w, h = box
        for X, Y, W, H in allowed_regions:
            if min(x + w, X + W) > max(x, X) and min(y + h, Y + H) > max(y, Y):
                return True
        return False

    for frame, boxes in zip(frames, per_frame_boxes):
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
        for box in boxes:
            if not overlaps_region(box):
                continue
            x, y, w, h = box
            pad_x, pad_y = max(1, int(w * 0.03)), max(1, int(h * 0.10))
            x0, y0 = max(0, x - pad_x), max(0, y - pad_y)
            x1, y1 = min(fw, x + w + pad_x), min(fh, y + h + pad_y)
            if x1 <= x0 or y1 <= y0:
                continue
            g = gray[y0:y1, x0:x1]
            c = hsv[y0:y1, x0:x1]
            smooth = cv2.GaussianBlur(g, (0, 0), sigmaX=max(0.8, h / 14.0))
            contrast = cv2.absdiff(g, smooth)
            local_cut = max(14, int(np.percentile(contrast, 72)))
            high_contrast = contrast >= local_cut
            white = (g >= 175) & (c[:, :, 1] <= 105)
            yellow = ((c[:, :, 0] >= 15) & (c[:, :, 0] <= 42)
                      & (c[:, :, 1] >= 70) & (c[:, :, 2] >= 145))
            bright_fill = float((g >= 195).mean())
            if bright_fill >= 0.42:
                # TikTok white caption bubble: keep the bubble/scene intact and select only
                # its dark, locally contrasting letter strokes.
                glyph = (g <= 170) & high_contrast
            else:
                # Outlined creator captions: select bright/yellow ink plus its close outline.
                glyph = (white | yellow) & (high_contrast | (contrast >= 9))
                edges = cv2.Canny(g, 55, 150) > 0
                near_ink = cv2.dilate((white | yellow).astype(np.uint8),
                                      np.ones((3, 3), np.uint8), iterations=1) > 0
                glyph |= edges & near_ink
            glyph_u8 = (glyph.astype(np.uint8) * 255)
            glyph_u8 = cv2.dilate(glyph_u8,
                                  cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5)),
                                  iterations=1)
            target = mask[y0:y1, x0:x1]
            np.maximum(target, glyph_u8, out=target)
    if not int(np.count_nonzero(mask)):
        return None
    # A small feather hides mask edges but cannot expand into a rectangular patch.
    return cv2.GaussianBlur(mask, (0, 0), sigmaX=1.6)


def blur_caption_regions(path, ffmpeg, seconds=DEFAULT_CLIP_SECONDS, status_cb=None):
    """Blur only OCR-detected caption LETTERS in place, never a rectangular line/box.

    Automatic scraping rejects captioned footage; this is retained for explicit/manual and
    legacy cleanup. It creates a feathered glyph mask from sampled frames and blends a blurred
    copy of the video through that mask. Returns the caption-region count (0 = untouched).
    """
    if cv2 is None or np is None or not ffmpeg:
        return 0
    path = Path(path)
    bgr = _sample_bgr_frames(path, ffmpeg, 8, seconds, width=720)
    if not bgr or _get_ocr() is None:
        return 0
    gh, gw = bgr[0].shape[:2]
    # LOW-confidence detection for the blur: styled caption lines (bold outline text) are
    # often only half-read by the recognizer - at 0.55 whole lines went uncovered.
    per_frame = [_ocr_text_rows(f, min_conf=0.35) for f in bgr]
    clusters = _cluster_text_lines(per_frame, gw)
    regions = []
    for c in clusters:
        x, y, w, h = c["box"]
        cx = x + w / 2.0
        # persistent lines are burned captions; a line seen once still counts when it is
        # caption-SHAPED (wide or horizontally centered) - captions swap text every 1-2s
        # and can hit a single sampled frame ("only half the captions got blurred").
        if len(c["frames"]) >= 2 or w >= 0.20 * gw or 0.30 * gw <= cx <= 0.70 * gw:
            regions.append((x, y, w, h))
    if not regions:
        return 0
    regions = regions[:8]
    stroke_mask = _caption_text_stroke_mask(bgr, per_frame, allowed_regions=regions)
    if stroke_mask is None:
        return 0

    # Scale the sparse glyph mask to the real video size.
    _, ffprobe = _ffmpeg_tools()
    vw = vh = 0
    try:
        r = subprocess.run([ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
                            "stream=width,height", "-of", "csv=p=0:s=x", str(path)],
                           capture_output=True, text=True, timeout=20)
        vw, vh = (int(v) for v in (r.stdout or "0x0").strip().split("x")[:2])
    except Exception:
        pass
    if not (vw and vh):
        return 0
    mask_full = cv2.resize(stroke_mask, (vw, vh), interpolation=cv2.INTER_LINEAR)
    mask_path = path.with_name(path.stem + "_capmask.png")
    if not cv2.imwrite(str(mask_path), mask_full):
        return 0
    tmp = path.with_name(path.stem + "_capblur" + path.suffix)
    # NOTE: no "shortest=1" on maskedmerge (ffmpeg 8.x rejects it -> silent graph death) and
    # NO "-loop 1" on the mask (an endless input makes the graph run forever). The single-frame
    # mask is repeated by framesync and the graph ends with the primary video.
    graph = ("[0:v]split=2[base][blur_src];"
             "[blur_src]gblur=sigma=40[blurred];"
             "[1:v]format=gray[mask];"
             "[base][blurred][mask]maskedmerge[v]")
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(path),
           "-i", str(mask_path),
           "-filter_complex", graph, "-map", "[v]", "-map", "0:a?",
           "-c:v", "libx264", "-crf", "19", "-preset", "veryfast", "-c:a", "copy",
           "-movflags", "+faststart", str(tmp)]
    try:
        subprocess.run(cmd, capture_output=True, timeout=300)
        if tmp.exists() and tmp.stat().st_size > 4096:
            os.replace(str(tmp), str(path))
            return len(regions)
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
    except Exception:
        pass
    finally:
        try:
            mask_path.unlink(missing_ok=True)
        except Exception:
            pass
    return 0


def scrape_bucket(out_dir, queries, want, bucket_id="", tier="exact", bucket_terms="",
                  per_clip_seconds=DEFAULT_CLIP_SECONDS, status_cb=None, cancel_check=None,
                  seen_ids=None, query_perf=None, candidate_statuses=None, min_likes=0,
                  search_sort="MOST_LIKED", platforms=None, deadline=None,
                  search_intent="", x_queries=None):
    """Search the EXACT given bucket queries (NO script-derived expansion via build_queries),
    pre-filter by metadata, download, then reject fake-vertical/black-bar and text-heavy clips.
    Returns accepted dicts: {path, meta, query, tier, clip_id, black_bar_score, text_heaviness,
    is_fake_vertical}. Records per-query stats in query_perf and per-candidate status rows in
    candidate_statuses (lists, if provided). Uses the logged-in TikTok session backend."""
    global _X_ZERO_STREAK, _X_UNAVAILABLE
    queries = normalize_query_list(queries)
    selected_platforms = normalize_platforms(platforms)
    if not backend_active(selected_platforms) or not queries:
        return []
    # TikTok and X use separate query strategies. X only receives validated proof/event/action
    # phrases; raw script tokens and generic lifestyle queries never reach its backend.
    tiktok_queries = _jp_first(queries) if "tiktok" in selected_platforms else []
    x_source_queries = queries if x_queries is None else normalize_query_list(x_queries)
    x_queries = x_queries_for_search(x_source_queries, bucket_terms=bucket_terms,
                                     search_intent=search_intent, limit=4) \
        if "twitter" in selected_platforms else []
    invalid_x = [q for q in x_source_queries if not is_valid_x_query(q)]
    if invalid_x:
        _status(status_cb, f"X: skipped {len(invalid_x)} malformed quer"
                           f"{'y' if len(invalid_x) == 1 else 'ies'} generated from script tokens.")
        if query_perf is not None:
            query_perf.extend({"query": q, "bucket_id": bucket_id, "tier": tier,
                               "platform": "twitter", "status": "invalid_query",
                               "raw_results": 0} for q in invalid_x)
    # Instagram keyword search is tolerant like TikTok's -> it gets the raw phrases.
    instagram_queries = list(queries) if "instagram" in selected_platforms else []
    prefer_x = str(search_intent or "").lower() in X_QUERY_INTENTS
    search_jobs = []
    max_queries = max(len(tiktok_queries), len(x_queries), len(instagram_queries))
    platform_order = (("twitter", "tiktok", "instagram") if prefer_x
                      else ("tiktok", "instagram", "twitter"))
    q_by_platform = {"tiktok": tiktok_queries, "twitter": x_queries,
                     "instagram": instagram_queries}
    for index in range(max_queries):
        for platform in platform_order:
            source = q_by_platform[platform]
            if index < len(source):
                search_jobs.append((platform, source[index]))
    # dedupe NAMESPACE: retry rounds may re-run a main-phase query once (the pool state has
    # changed by then), but never repeat within their own phase.
    _ns = "retry" if str(tier or "").startswith("retry") else "main"
    fresh, dup = [], 0
    for platform, q in search_jobs:
        key = f"{_ns}|{platform}|{_norm_query(q)}"
        if key in _SEEN_QUERIES:
            dup += 1
            continue
        fresh.append((platform, q))
    if dup:
        _status(status_cb, f"Scrape: skipped {dup} duplicate quer{'y' if dup == 1 else 'ies'} "
                           "(already searched this run).")
    search_jobs = fresh
    if not search_jobs:
        return []
    out_dir = Path(out_dir)
    raw_dir = out_dir / "_raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    ffmpeg, ffprobe = _ffmpeg_tools()
    if not ffmpeg:
        _status(status_cb, "Scrape: ffmpeg not found, cannot prepare clips.")
        return []
    seen_ids = seen_ids if seen_ids is not None else set()
    accepted = []
    # Do not let the first query monopolize the whole pool.  Search at least three
    # variants (when available), taking a bounded number from each before widening.
    diversity_slots = min(5, len(search_jobs), max(1, int(want)))
    per_query_quota = max(1, (int(want) + diversity_slots - 1) // diversity_slots)

    def _reject(raw):
        try:
            raw.unlink()
        except Exception:
            pass

    def _declined_root():
        p = out_dir
        while p.name.lower() != "seedance 2.0" and p.parent != p:
            p = p.parent
        return (p if p.name.lower() == "seedance 2.0" else out_dir) / "_declined"

    def _save_declined(raw_path, cid_, reason, score=None):
        """Keep a bounded set of REJECTED-but-watchable clips (normalized 9:16) so the media panel
        can show them - the user can review and manually ACCEPT one with the checkmark."""
        try:
            droot = _declined_root()
            droot.mkdir(parents=True, exist_ok=True)
            key = hashlib.sha1(str(cid_ or raw_path).encode("utf-8", "ignore")).hexdigest()[:12]
            dst = droot / f"declined_{key}.mp4"
            if dst.exists():
                return
            if normalize_clip(raw_path, dst, ffmpeg, seconds=per_clip_seconds):
                info = {"reason": str(reason)[:160]}
                if score is not None:
                    info["score"] = score
                dst.with_suffix(".json").write_text(json.dumps(info), "utf-8")
        except Exception:
            pass

    def _cstat(q_, cid, status, reason, extra=None):
        if candidate_statuses is not None:
            row = {"clip_id": cid, "bucket_id": bucket_id, "source_query": q_, "tier": tier,
                   "status": status, "shown_in_media_panel": False, "reason": reason}
            if extra:
                row.update(extra)
            candidate_statuses.append(row)
        # Metadata rejects have deliberately not been downloaded, so there is no media file to
        # archive. Preserve their complete discovery record nevertheless: this makes *every*
        # rejected candidate auditable without wasting bandwidth downloading known-bad clips.
        if status == "pre_download_rejected":
            try:
                droot = _declined_root()
                droot.mkdir(parents=True, exist_ok=True)
                row = {"clip_id": cid, "bucket_id": bucket_id, "source_query": q_, "tier": tier,
                       "status": status, "reason": reason}
                if extra:
                    row.update(extra)
                with (droot / "metadata_rejected.jsonl").open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            except Exception:
                pass

    def _analyze_item(raw, cid, m, q_, file_key):
        """Full quality gate + normalize for ONE downloaded clip. Runs in a
        small thread pool: the ffmpeg/OpenCV work here (re-encode + several frame passes,
        ~20-30s per clip) dominated the whole scrape phase when it ran serially after each
        download. Workers never call status_cb (it raises RunCancelled on the main thread's
        cancel flag); log lines are returned and emitted serially by the collector."""
        logs = []
        if not is_vertical_hq(raw, ffprobe):
            # Keep every downloaded rejection inspectable.  A clip may be landscape or low-res
            # for the automatic render yet still be useful to the user manually.
            _save_declined(raw, cid, "low-res / landscape file")
            _reject(raw)
            return {"kind": "reject", "vertical": False, "query": q_, "logs": logs,
                    "status": "rejected_quality", "reason": "low-res / landscape file"}
        fv = detect_fake_vertical_or_black_bars(raw, ffmpeg, per_clip_seconds)
        if fv["is_fake_vertical"] or fv["black_bar_score"] > 4.0:
            logs.append(f"Rejected clip ({bucket_id}/{q_}): fake vertical / black bars "
                        f"({fv['reason']})")
            _save_declined(raw, cid, f"black bars / fake vertical: {fv['reason']}")
            _reject(raw)
            return {"kind": "reject", "vertical": True, "query": q_, "logs": logs,
                    "status": "rejected_black_bars", "reason": fv["reason"],
                    "extra": {"black_bar_score": fv["black_bar_score"],
                              "is_fake_vertical": fv["is_fake_vertical"]}}
        # One OCR pass (width 720 so small watermarks stay readable) gives BOTH the text
        # score and the on-screen strings for the AI-watermark check below.
        _areas, _lines, _texts = _ocr_frame_stats(raw, ffmpeg, per_clip_seconds, width=720)
        th = _score_from_ocr(_areas, _lines)
        # AI-art accounts flood these searches with photoreal FAKE footage; the metadata is
        # often clean but the video carries their watermark (e.g. '@ArtGenTokyo') - OCR
        # reads it. Found-footage mode must never use generated clips.
        _wm = next((t for t in _texts if _AI_WATERMARK_RE.search(t)), None)
        if _wm is not None:
            logs.append(f"Rejected clip ({bucket_id}/{q_}): AI-art watermark on screen ({_wm!r})")
            _save_declined(raw, cid, f"ai-generated (watermark {_wm[:40]!r})")
            _reject(raw)
            return {"kind": "reject", "vertical": True, "query": q_, "logs": logs,
                    "status": "rejected_ai_content", "reason": f"AI watermark {_wm[:40]!r}"}
        # Captions are a HARD reject. Blurring large subtitles makes the underlying subject
        # unrecognisable, so never modify and accept captioned footage as a workaround.
        persistent_caption = has_burned_captions(
            raw, ffmpeg, seconds=per_clip_seconds, status_cb=None)
        if is_captioned_candidate(th, persistent_caption):
            logs.append(f"Rejected clip ({bucket_id}/{q_}): burned-in captions/text ({th}/10)")
            _save_declined(raw, cid, f"burned-in captions/text ({th}/10)", score=th)
            _reject(raw)
            return {"kind": "reject", "vertical": True, "query": q_, "logs": logs,
                    "status": "rejected_captions", "reason": f"burned-in captions/text {th}/10",
                    "extra": {"text_heaviness_score": th}}
        stability = stable_segment_profile(raw, ffmpeg, ffprobe, per_clip_seconds)
        if not stability["stable"]:
            logs.append(f"Rejected clip ({bucket_id}/{q_}): rapid internal edit montage "
                        f"({stability['internal_cut_count']} cuts; shortest hold "
                        f"{stability['min_shot_seconds']:.2f}s)")
            _save_declined(raw, cid, f"rapid internal cuts ({stability['internal_cut_count']})")
            _reject(raw)
            return {"kind": "reject", "vertical": True, "query": q_, "logs": logs,
                    "status": "rejected_rapid_cuts",
                    "reason": "source contains rapid internal edit cuts", "extra": stability}
        # Tier calls share out_dir.  A simple 00/01 counter overwrote clips from an
        # earlier tier/round while the matcher still referenced those paths, making
        # the selected footage differ from what vision reviewed.  Use a stable unique
        # name tied to the TikTok item instead.
        safe_bucket = re.sub(r"[^A-Za-z0-9_-]+", "_", str(bucket_id or "bucket"))[:36]
        safe_tier = re.sub(r"[^A-Za-z0-9_-]+", "_", str(tier or "tier"))[:20]
        final = normalize_clip(raw, out_dir / f"cand_{safe_bucket}_{safe_tier}_{file_key}.mp4",
                               ffmpeg, seconds=per_clip_seconds, start=stability["start"])
        if not final:
            _save_declined(raw, cid, "normalize failed")
            _reject(raw)
            return {"kind": "reject", "vertical": True, "query": q_, "logs": logs,
                    "status": "rejected_quality", "reason": "normalize failed"}
        _reject(raw)
        record = {"path": final, "meta": m, "query": q_, "tier": tier, "clip_id": cid,
                  "platform": m.get("platform", "tiktok"), "likes": m.get("likes", 0),
                  "black_bar_score": fv["black_bar_score"], "text_heaviness": th,
                  "is_fake_vertical": False,
                  "internal_cut_count": stability["internal_cut_count"],
                  "rapid_internal_cut_count": stability["rapid_internal_cut_count"],
                  "min_shot_seconds": stability["min_shot_seconds"]}
        # Persist live-panel metadata next to the accepted candidate immediately. The previous
        # implementation kept this only in memory, so the running-job UI could not distinguish
        # accepted TikTok footage from accepted X footage.
        try:
            final.with_suffix(".json").write_text(json.dumps({
                "status": "accepted", "platform": m.get("platform", "tiktok"),
                "likes": m.get("likes", 0), "clip_id": cid, "query": q_,
                "bucket_id": bucket_id, "tier": tier, "text_heaviness": th,
                "captioned": False,
            }, indent=2), encoding="utf-8")
        except Exception:
            pass
        return {"kind": "accepted", "vertical": True, "query": q_, "logs": logs,
                "status": "downloaded_pending_review", "reason": "passed pre-filters",
                "record": record,
                "extra": {"platform": m.get("platform", "tiktok"),
                          "likes": m.get("likes", 0), "black_bar_score": fv["black_bar_score"],
                          "text_heaviness_score": th,
                          "internal_cut_count": stability["internal_cut_count"],
                          "rapid_internal_cut_count": stability["rapid_internal_cut_count"],
                          "min_shot_seconds": stability["min_shot_seconds"]}}

    # Downloads MUST stay on this thread (the logged-in browser session is single-threaded),
    # but the per-clip ffmpeg/OpenCV gauntlet runs in a small pool so the next download
    # proceeds while earlier clips are still being scored and normalized.
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=3)
    search_summary = {"tiktok": {"queries": 0, "items": 0},
                      "twitter": {"queries": 0, "items": 0},
                      "instagram": {"queries": 0, "items": 0}}
    try:
      for qidx, (search_platform, q) in enumerate(search_jobs):
        if ((cancel_check and cancel_check()) or len(accepted) >= want
                or (deadline is not None and time.monotonic() >= deadline)):
            break
        if search_platform == "twitter" and _X_UNAVAILABLE:
            continue
        query_stop = min(int(want), len(accepted) + per_query_quota)
        _SEEN_QUERIES.add(f"{_ns}|{search_platform}|{_norm_query(q)}")
        # collect a LARGE candidate pool per search - the accept target is what must survive
        # the gates, not what the search is allowed to return.
        items = backend_search(q, max(12, want * 3), status_cb=status_cb,
                               sort=search_sort, platforms={search_platform},
                               deadline=deadline) or []
        raw_n = len(items)
        search_summary[search_platform]["queries"] += 1
        search_summary[search_platform]["items"] += raw_n
        if search_platform == "twitter":
            _X_ZERO_STREAK = _X_ZERO_STREAK + 1 if raw_n == 0 else 0
            if _X_ZERO_STREAK >= 5 and twitter_login is not None:
                try:
                    health = twitter_login.health_check(timeout_s=40)
                except Exception:
                    health = {"ok": False}
                if not health.get("ok"):
                    _X_UNAVAILABLE = True
                    _status(status_cb, "X: backend health check failed after 5 valid zero-result "
                                       "queries; skipping further X searches this run.")
                else:
                    _X_ZERO_STREAK = 0
        meta_rej = dl = vert = clean = acc = 0
        meta_reasons = {}
        # Phase A (serial): metadata-filter ALL items first, then download in RANK order
        # (popularity bonus minus soft penalties, after the requested minimum-like gate).
        # Overshoot the quota because some downloads will fail the parallel analysis below.
        need = max(0, (query_stop - len(accepted)) * 2 + 4)
        scored = []
        for item_index, it in enumerate(items):
            ok, reason, m = pre_download_candidate_filter(
                it, bucket_terms, seen_ids, min_likes=min_likes)
            cid = m.get("id") or m.get("url") or f"{bucket_id}:{q}:{raw_n}:{len(scored)}"
            if not ok:
                meta_rej += 1
                meta_reasons[reason] = meta_reasons.get(reason, 0) + 1
                _cstat(q, cid, "pre_download_rejected", reason,
                       {"platform": m.get("platform"), "likes": m.get("likes", 0)})
                continue
            sort_mode = str(search_sort or "MOST_LIKED").upper()
            if sort_mode == "MOST_VIEWED":
                m["rank_score"] = math.log10(int(m.get("views") or 0) + 1) - float(m.get("penalty") or 0)
            elif sort_mode == "MOST_RECENT":
                m["rank_score"] = float(m.get("created_at") or 0)
            elif sort_mode in ("RELEVANCE", "ALL"):
                # Preserve the backend's merged order (RELEVANCE = topical; ALL = the four sort
                # orders concatenated liked->relevance->viewed->recent) through the metadata gate.
                m["rank_score"] = -float(item_index)
            scored.append((float(m.get("rank_score", 0.0)), it, m, cid))
        scored.sort(key=lambda r: r[0], reverse=True)
        scored = balanced_platform_candidates(scored, need, selected_platforms)
        pending = []
        for _rs, it, m, cid in scored:
            if ((cancel_check and cancel_check()) or len(pending) >= need
                    or (deadline is not None and time.monotonic() >= deadline)):
                break
            raw = raw_dir / f"raw_{qidx}_{dl}.mp4"
            dl += 1
            if not backend_download(it, raw, status_cb=None):
                _cstat(q, cid, "download_failed", "download failed")
                continue
            if cid:
                seen_ids.add(cid)
            file_key = hashlib.sha1(
                str(cid or it.get("webVideoUrl") or it.get("id") or raw.name).encode("utf-8", "ignore")
            ).hexdigest()[:12]
            pending.append((executor.submit(_analyze_item, raw, cid, m, q, file_key), cid))
        # Phase B: collect the analysis results in download order.
        for fut, cid in pending:
            if cancel_check and cancel_check():
                break
            try:
                res = fut.result()
            except Exception:
                continue
            for line in res.get("logs") or []:
                _status(status_cb, line)
            if res.get("vertical"):
                vert += 1
            if res["kind"] == "accepted":
                clean += 1
                if len(accepted) < want:
                    acc += 1
                    accepted.append(res["record"])
                    _status(status_cb, f"Downloaded accepted candidate {len(accepted)} for bucket {bucket_id} (query {res['query']})")
                    _cstat(res["query"], cid, "downloaded_pending_review", "passed pre-filters", res.get("extra"))
                else:
                    _cstat(res["query"], cid, "downloaded_pending_review",
                           "passed pre-filters (bucket already full)", res.get("extra"))
            else:
                _cstat(res["query"], cid, res["status"], res["reason"], res.get("extra"))
        if query_perf is not None:
            query_perf.append({"query": q, "bucket_id": bucket_id, "tier": tier,
                               "platform": search_platform,
                               "sort": str(search_sort or "MOST_LIKED").upper(),
                               "raw_results": raw_n, "metadata_rejected": meta_rej,
                               "downloadable": dl, "vertical_hq": vert, "text_clean": clean,
                               "accepted": acc, "metadata_rejection_reasons": meta_reasons})
        if meta_rej:
            summary = ", ".join(f"{reason}: {count}" for reason, count in
                                sorted(meta_reasons.items(), key=lambda row: row[1], reverse=True))
            _status(status_cb, f"Metadata filter ({bucket_id}/{q}): rejected {meta_rej}/{raw_n} "
                               f"candidate(s) ({summary}).")
    finally:
        # On cancel, drop queued clips but let the (max 3) running ffmpeg jobs finish.
        executor.shutdown(wait=True, cancel_futures=True)
    for platform, summary in search_summary.items():
        if summary["queries"]:
            label = {"twitter": "X", "instagram": "Instagram"}.get(platform, "TikTok")
            _status(status_cb, f"{label}: searched {summary['queries']} quer"
                               f"{'y' if summary['queries'] == 1 else 'ies'} | "
                               f"found {summary['items']} raw video{'s' if summary['items'] != 1 else ''}.")
    try:
        if raw_dir.exists():
            for f in raw_dir.glob("*"):
                f.unlink()
            raw_dir.rmdir()
    except Exception:
        pass
    if accepted:
        platform_counts = {}
        for record in accepted:
            platform = str(record.get("platform") or "tiktok").lower()
            platform_counts[platform] = platform_counts.get(platform, 0) + 1
        _status(status_cb, f"Bucket {bucket_id}: accepted platform mix - "
                           + ", ".join(f"{count} {'X' if platform == 'twitter' else platform.title()}"
                                       for platform, count in sorted(platform_counts.items())))
    return accepted


def scrape_clips(
    out_dir,
    platforms,
    terms,
    count,
    script_text="",
    script_relevancy=70,
    per_clip_seconds=DEFAULT_CLIP_SECONDS,
    lead_query=None,
    cookies=None,
    status_cb=None,
    cancel_check=None,
):
    """End-to-end: discover -> download -> filter (vertical/HQ/no-text) -> normalize.

    The first accepted clip comes from `lead_query` when given (the hook). When a
    `cookies` connection is supplied (browser name or cookies.txt) and a real platform
    (TikTok/Instagram) is selected, the hook + b-roll are pulled from real influencer
    hashtags instead of YouTube's AI filler. Returns normalized clip Paths; never raises.
    """
    set_cookies(cookies)
    out_dir = Path(out_dir)
    raw_dir = out_dir / "_raw"
    out_dir.mkdir(parents=True, exist_ok=True)
    platforms = platforms or ["tiktok", "instagram"]
    # TikTok-only via yt-dlp; a cookie connection is mandatory for that path.
    if not cookies_active():
        _status(status_cb, "Scrape: not connected. Connect TikTok (Settings -> Connect TikTok) "
                           "and try again.")
        return []
    _status(status_cb, "Scrape: connected — pulling real clips from TikTok hashtags.")
    ffmpeg, ffprobe = _ffmpeg_tools()
    if not ffmpeg:
        _status(status_cb, "Scrape: ffmpeg not found, cannot prepare clips.")
        return []
    _evaluate_url._n = 0
    portrait_raws = []   # preferred: true vertical, no weird crop
    landscape_raws = []  # fallback: only used if not enough verticals
    hook_raw = [None]    # the lead (woman) clip, kept first
    # Bound total downloads so a landscape-heavy source (YouTube) can't make us pull
    # the entire candidate pool while hunting for verticals.
    budget = [max(8, count * 2 + 6)]

    def _drain(urls, need_portrait, is_hook=False):
        """Pull URLs until we have `need_portrait` portrait clips (still banking landscape)."""
        for url in urls:
            if cancel_check and cancel_check():
                return
            if len(portrait_raws) >= need_portrait or budget[0] <= 0:
                return
            # once we have plenty banked (portrait+landscape), stop hunting
            if not is_hook and len(portrait_raws) + len(landscape_raws) >= need_portrait * 2 + 2:
                return
            budget[0] -= 1
            ev = _evaluate_url(url, raw_dir, ffmpeg, ffprobe, per_clip_seconds, status_cb)
            if not ev:
                continue
            raw, portrait = ev
            if is_hook and hook_raw[0] is None and portrait:
                hook_raw[0] = raw
                return
            (portrait_raws if portrait else landscape_raws).append(raw)

    tiktok = ["tiktok"]
    # 1) Hook clip: a real woman, from real influencer hashtags on TikTok.
    if lead_query:
        _status(status_cb, "Scrape: finding a hook clip from real influencer hashtags...")
        lead_urls = discover_urls(tiktok, WOMAN_TAGS, 10, status_cb=status_cb)
        _drain(lead_urls, need_portrait=1, is_hook=True)

    # 2) The rest of the edit from the style/script queries, as TikTok hashtags.
    queries = build_queries(tiktok, terms, script_text, script_relevancy, count=max(count, 3))
    _status(status_cb, f"Scrape: searching TikTok for {len(queries)} style hashtags...")
    urls = discover_urls(tiktok, queries, count, status_cb=status_cb)
    if not urls and not (portrait_raws or landscape_raws or hook_raw[0]):
        _status(
            status_cb,
            "Scrape: TikTok returned no clips for these hashtags. yt-dlp's TikTok extractor "
            "can rate-limit or break even when connected; try broader style terms, or check "
            "the connection with the Test button.",
        )
        return []
    _drain(urls, need_portrait=count)

    # Prefer portrait; fall back to landscape (centre-cropped) only to reach the count.
    ordered = []
    if hook_raw[0]:
        ordered.append(hook_raw[0])
    ordered.extend(portrait_raws)
    if len(ordered) < count:
        n_land = count - len(ordered)
        if n_land > 0 and landscape_raws:
            _status(status_cb, f"Scrape: not enough vertical clips; using {min(n_land, len(landscape_raws))} "
                               "landscape clip(s) (centre-cropped) to fill out the edit.")
        ordered.extend(landscape_raws)
    if not ordered:
        _status(status_cb, "Scrape: every candidate was rejected (low-res/captioned). "
                           "Try broader style terms or add 'youtube'.")
        return []

    # 3) Normalize the accepted raws into the seedance folder, hook first.
    results = []
    for raw in ordered[:count]:
        stability = stable_segment_profile(raw, ffmpeg, ffprobe, per_clip_seconds)
        if not stability["stable"]:
            _status(status_cb, "Scrape: skipped a clip whose usable window is an internal rapid-cut montage.")
            continue
        final = normalize_clip(raw, out_dir / f"scraped_{len(results):02d}.mp4", ffmpeg,
                               seconds=per_clip_seconds, start=stability["start"])
        if final:
            results.append(final)
            _status(status_cb, f"Scrape: prepared clip {len(results)}.")

    # tidy raw downloads
    try:
        for f in raw_dir.glob("*"):
            f.unlink()
        if raw_dir.exists():
            raw_dir.rmdir()
    except Exception:
        pass
    _status(status_cb, f"Scrape: {len(results)} clip(s) ready in {out_dir.name}.")
    return results
