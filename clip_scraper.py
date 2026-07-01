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

import os
import re
import subprocess
import json
import hashlib
import shutil
import tempfile
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
    import tiktok_login              # logged-in TikTok search backend (Apify replacement)
except Exception:  # pragma: no cover - optional; falls back to Apify if token present
    tiktok_login = None


TARGET_W, TARGET_H = 1080, 1920
DEFAULT_CLIP_SECONDS = 4.0
MIN_LONG_SIDE = 700          # reject sources whose long edge is below this (low quality)
MIN_PORTRAIT_RATIO = 1.20    # height/width must be at least this (reject landscape/square)
CAPTION_FRAME_SAMPLES = 5    # frames sampled per clip for the burned-in-text check
CAPTION_REJECT_FRAMES = 2    # reject the clip if this many sampled frames look captioned

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


# ---- Apify TikTok scraper (real keyword search + watermark-free download) --------
# yt-dlp can't search TikTok; Apify can. With shouldDownloadVideos the actor returns a
# watermark-free mp4 on Apify storage (mediaUrls[0]) plus metadata (w/h/duration/caption)
# we use to pre-filter before downloading. Token lives in APIFY_TOKEN (env / .env).
# novi/tiktok-scraper-ultimate: ~10x cheaper than clockworks. It returns the RAW TikTok
# objects (watermark-free CDN url at video.download_no_watermark_addr.url_list[0], dims at
# video.width/height) instead of downloading to Apify storage - so we fetch the mp4 straight
# from the TikTok CDN (needs a tiktok Referer header). _apify_* below handle both formats.
APIFY_ACTOR = os.environ.get("APIFY_ACTOR", "novi~tiktok-scraper-ultimate").strip()
# This scraper path is specifically used for Japanese social-footage searches.  Novi's
# actor otherwise defaults to a US search region, which makes native Japanese queries
# return substantially less relevant results.
APIFY_LOCATION = (os.environ.get("SCRAPE_LOCATION", "JP") or "JP").strip().upper()


def apify_token():
    return (os.environ.get("APIFY_TOKEN", "") or "").strip()


# Set once apify_search hits an HTTP 402 / "not-enough-usage" so the run can stop and report a
# clear "out of credits" message instead of silently returning 0 clips for every query.
_APIFY_OUT_OF_CREDITS = [False]


def apify_out_of_credits():
    return _APIFY_OUT_OF_CREDITS[0]


def apify_active():
    return bool(apify_token())


def apify_search(queries, results_per_query, status_cb=None, sort_type="MOST_LIKED"):
    """Run the Apify TikTok scraper for the given search keywords. Returns raw dataset items.
    novi/tiktok-scraper-ultimate is the default actor (cheap, returns watermark-free CDN urls);
    the clockworks schema is still produced if APIFY_ACTOR points back to it."""
    tok = apify_token()
    if not tok or not queries:
        return []
    url = f"https://api.apify.com/v2/acts/{APIFY_ACTOR}/run-sync-get-dataset-items?token={tok}"
    if "clockworks" in APIFY_ACTOR:
        body = {
            "searchQueries": list(queries),
            "resultsPerPage": max(1, int(results_per_query)),
            "shouldDownloadVideos": True,
            "shouldDownloadCovers": False,
            "shouldDownloadSubtitles": False,
            "proxyConfiguration": {"useApifyProxy": True},
        }
    else:
        # Novi currently enforces a minimum maxItems of 20.  Values below that make a
        # perfectly valid keyword search return an actor input error/empty dataset.
        sort_type = str(sort_type or "MOST_LIKED").strip().upper()
        if sort_type not in {"RELEVANCE", "MOST_LIKED", "MOST_RECENT", "DEFAULT"}:
            sort_type = "RELEVANCE"
        body = {
            "keywords": list(queries),
            "maxItems": min(100, max(20, int(results_per_query) * max(1, len(queries)))),
            "sortType": sort_type,
            "dateRange": "DEFAULT",
            "includeSearchKeywords": True,
            "customMapFunction": "(object) => { return {...object} }",
        }
        if APIFY_LOCATION:
            body["location"] = APIFY_LOCATION
    try:
        req = urllib.request.Request(
            url, data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=290) as r:
            items = json.loads(r.read().decode("utf-8"))
        return items if isinstance(items, list) else []
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace").strip()[:180]
        except Exception:
            pass
        # Out of Apify credits / billing blocked -> a clear, actionable message (and a flag so the
        # scrape stops hammering the API instead of silently returning 0 clips for every query).
        if exc.code == 402 or "not-enough-usage" in detail or "exceed your remaining usage" in detail:
            _APIFY_OUT_OF_CREDITS[0] = True
            _status(status_cb, "Apify: OUT OF CREDITS - the account has no paid usage left to run the "
                               "TikTok scraper. Top up at apify.com (or set a different APIFY_TOKEN). "
                               "No clips can be scraped until then.")
            return []
        if exc.code in (401, 403):
            _status(status_cb, f"Apify: auth failed (HTTP {exc.code}) - check APIFY_TOKEN in .env.")
            return []
        hint = " (run-sync timed out; fewer queries per call needed)" if exc.code in (408, 504) else ""
        _status(status_cb, f"Apify: search failed (HTTP {exc.code}{': ' + detail if detail else ''}){hint}.")
        return []
    except Exception as exc:  # noqa: BLE001
        _status(status_cb, f"Apify: search failed ({exc.__class__.__name__}: {exc}).")
        return []


def _apify_media_url(item):
    if not isinstance(item, dict):
        return None
    # clockworks format: a ready Apify-storage url
    mu = item.get("mediaUrls")
    if isinstance(mu, list) and mu and isinstance(mu[0], str) and mu[0].startswith("http"):
        return mu[0]
    # novi raw-TikTok format: watermark-free CDN url nested in video.*
    v = item.get("video") if isinstance(item.get("video"), dict) else {}
    for field in ("download_no_watermark_addr", "play_addr_h264", "play_addr", "download_addr"):
        addr = v.get(field)
        if isinstance(addr, dict):
            ul = addr.get("url_list")
            if isinstance(ul, list) and ul and isinstance(ul[0], str) and ul[0].startswith("http"):
                return ul[0]
        elif isinstance(addr, str) and addr.startswith("http"):
            return addr
    return None


def _apify_download(media_url, dest, status_cb=None):
    tok = apify_token()
    dl = media_url
    if "api.apify.com" in media_url and tok and "token=" not in media_url:
        dl = media_url + (("&" if "?" in media_url else "?") + f"token={tok}")
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    if "tiktokcdn" in media_url or "tiktok.com" in media_url:
        headers["Referer"] = "https://www.tiktok.com/"   # TikTok CDN rejects requests without it
    try:
        req = urllib.request.Request(dl, headers=headers)
        with urllib.request.urlopen(req, timeout=120) as r, open(dest, "wb") as f:
            shutil.copyfileobj(r, f)
    except Exception as exc:  # noqa: BLE001
        _status(status_cb, f"Apify: download failed ({exc.__class__.__name__}).")
        return None
    return dest if dest.exists() and dest.stat().st_size > 4096 else None


def _apify_item_portrait_hq(item):
    if not isinstance(item, dict):
        return None
    vm = item.get("videoMeta") if isinstance(item.get("videoMeta"), dict) else {}
    v = item.get("video") if isinstance(item.get("video"), dict) else {}
    try:
        w = int(vm.get("width") or v.get("width") or 0)
        h = int(vm.get("height") or v.get("height") or 0)
    except (TypeError, ValueError):
        return None
    if w <= 0 or h <= 0:
        return None
    if max(w, h) < MIN_LONG_SIDE:
        return False
    return h >= w * MIN_PORTRAIT_RATIO


# ---- Search/download BACKEND switch (TikTok login preferred, Apify dormant fallback) ----
# The default, no-API-key path is a logged-in TikTok session driven by Playwright
# (tiktok_login.py): it runs the real keyword search and returns native TikTok item objects
# that _item_meta already understands, then yt-dlp downloads the (watermark-free) mp4 using
# cookies exported from that same session. Apify is only used if its token is set AND no
# TikTok login exists, so existing setups keep working without it.

def tiktok_backend_ready():
    return bool(tiktok_login is not None and tiktok_login.is_ready())


def _apify_opt_in():
    """Apify is OFF by default now - the login backend replaced it. It only runs when the
    user explicitly sets SCRAPE_BACKEND=apify in the environment (legacy escape hatch)."""
    return os.environ.get("SCRAPE_BACKEND", "").strip().lower() == "apify"


def apify_enabled():
    return bool(apify_active() and _apify_opt_in())


def backend_active():
    """True if a search backend can run. Default path = a saved TikTok login (no API key).
    Apify only counts when explicitly opted in via SCRAPE_BACKEND=apify."""
    return bool(tiktok_backend_ready() or apify_enabled())


def backend_name():
    if tiktok_backend_ready():
        return "tiktok_login"
    if apify_enabled():
        return "apify"
    return "none"


def backend_search_health():
    """Cumulative search health for the active backend this run: {searches, items, login_wall}.
    Lets the caller fail fast when the backend returns NOTHING (logged out / headless block)."""
    if tiktok_backend_ready() and tiktok_login is not None:
        try:
            return tiktok_login.search_stats()
        except Exception:
            return {}
    return {}


def reset_backend_search_health():
    if tiktok_login is not None:
        try:
            tiktok_login.reset_search_stats()
        except Exception:
            pass


def _ensure_tiktok_cookies(status_cb=None):
    """Open (lazily) the shared logged-in session and point yt-dlp at its cookies."""
    sess = tiktok_login.get_session(status_cb=status_cb)
    if sess is None:
        return None
    ck = tiktok_login.export_cookies_txt()
    if ck:
        set_cookies(ck)            # yt-dlp downloads authenticated as the logged-in user
    return sess


def backend_search(query, want, status_cb=None, sort="MOST_LIKED"):
    """Run one keyword search through the active backend. Returns raw item dicts."""
    if tiktok_backend_ready():
        sess = _ensure_tiktok_cookies(status_cb)
        if sess is not None:
            try:
                return sess.search(query, want=int(want), status_cb=status_cb, sort=sort) or []
            except Exception as exc:  # noqa: BLE001
                _status(status_cb, f"TikTok search failed ({exc.__class__.__name__}: {exc}).")
                return []
    if apify_enabled():
        return apify_search([query], int(want), status_cb=status_cb, sort_type=sort) or []
    return []


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
    """Download a candidate item to dest via the right mechanism for its source backend."""
    if isinstance(item, dict) and item.get("_source") == "tiktok_login":
        url = item.get("webVideoUrl") or item.get("url") or ""
        return _ytdlp_fetch(url, dest, status_cb=status_cb) if url else None
    murl = _apify_media_url(item)
    return _apify_download(murl, dest, status_cb=status_cb) if murl else None


def close_backend():
    """Tear down the shared TikTok session at the end of a run."""
    if tiktok_login is not None:
        try:
            tiktok_login.close_session()
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


def _sample_gray_frames(path, ffmpeg, n, seconds):
    """Grab n grayscale frames spread across the clip as numpy arrays."""
    if cv2 is None or not ffmpeg:
        return []
    frames = []
    tmp = Path(tempfile.mkdtemp(prefix="capchk_"))
    try:
        for i in range(n):
            t = max(0.1, seconds * (i + 0.5) / n)
            fp = tmp / f"f{i}.jpg"
            subprocess.run([ffmpeg, "-y", "-ss", str(t), "-i", str(path),
                            "-frames:v", "1", "-vf", "scale=360:-1", str(fp)],
                           capture_output=True, timeout=30)
            if fp.exists():
                img = cv2.imread(str(fp), cv2.IMREAD_GRAYSCALE)
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


def _frame_caption_lines(gray):
    """Count big white/yellow caption-like text lines in the centre band of one frame.

    Edited captions are near-WHITE (or bright yellow) bold text, usually outlined and
    horizontally centred. We threshold for very bright pixels and look for a wide, text-
    height, centred blob whose stroke density is text-like. This deliberately ignores
    coloured street signage, red lanterns and scattered city lights (which the reference
    channels are full of and the user wants to keep). Returns an integer count.
    """
    if cv2 is None or np is None:
        return 0
    h, w = gray.shape
    y0, y1 = int(0.30 * h), int(0.93 * h)
    band = gray[y0:y1, :]
    # very bright pixels only -> white/yellow caption text, not coloured signage/lanterns
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
    lines = 0
    for c in cnts:
        x, y, bw, bbh = cv2.boundingRect(c)
        cx = x + bw / 2.0
        if bw > 0.22 * w and 0.035 * h < bbh < 0.16 * h and 0.12 * w < cx < 0.88 * w:
            # within the line box, how much is actually bright text vs filled block?
            roi = bright[y:y + bbh, x:x + bw]
            fill = float(roi.mean()) / 255.0
            # text strokes give moderate fill; exclude solid white panels (high) and noise (low)
            if 0.05 < fill < 0.55:
                lines += 1
    return lines


def has_burned_captions(path, ffmpeg, seconds=DEFAULT_CLIP_SECONDS, status_cb=None):
    """True if several sampled frames show big edited caption/text lines."""
    if cv2 is None:
        return False  # can't check -> don't block
    frames = _sample_gray_frames(path, ffmpeg, CAPTION_FRAME_SAMPLES, seconds)
    if not frames:
        return False
    hits = sum(1 for g in frames if _frame_caption_lines(g) >= 1)
    return hits >= CAPTION_REJECT_FRAMES


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


def _scrape_via_apify(out_dir, terms, count, script_text, script_relevancy,
                      per_clip_seconds, lead_query, status_cb, cancel_check):
    """Apify path: real TikTok keyword search + watermark-free download, then the same
    vertical/HQ/no-text filters + normalize. Returns normalized clip Paths (hook first)."""
    out_dir = Path(out_dir)
    raw_dir = out_dir / "_raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    ffmpeg, ffprobe = _ffmpeg_tools()
    if not ffmpeg:
        _status(status_cb, "Scrape: ffmpeg not found, cannot prepare clips.")
        return []
    accepted = []   # raw paths, hook first
    idx = [0]

    def _take(items, need, hook=False):
        # prefer portrait+HQ by metadata, then download watermark-free + filter text
        ordered = sorted(items, key=lambda it: 0 if _apify_item_portrait_hq(it) else 1)
        for it in ordered:
            if cancel_check and cancel_check():
                return
            if len(accepted) >= need:
                return
            if _apify_item_portrait_hq(it) is False:
                continue  # landscape / low-res by metadata
            murl = _apify_media_url(it)
            if not murl:
                continue
            raw = raw_dir / f"raw_{idx[0]:02d}.mp4"
            idx[0] += 1
            if not _apify_download(murl, raw, status_cb=status_cb):
                continue
            if not is_vertical_hq(raw, ffprobe):
                try: raw.unlink()
                except Exception: pass
                continue
            # NOTE: the brightness-based burned-caption heuristic is intentionally NOT applied
            # here. It false-positives on bright Tokyo neon/daylight footage and was rejecting
            # ~every clip, starving the pool. The vision quality-gate in agent_core
            # (assign_clips_to_scenes_by_vision) is the authoritative caption/AI/relevance
            # filter now and reliably rejects captioned clips from the poster frames.
            accepted.append(raw)
            if hook:
                return

    # 1) Hook: a real woman via a dedicated search query.
    if lead_query:
        _status(status_cb, "Apify: searching TikTok for a hook clip (real influencer)...")
        _take(apify_search([lead_query], 6, status_cb=status_cb), 1, hook=True)

    # 2) The rest from per-style queries. Apify's run-sync endpoint caps near 300s and
    #    downloads every matched video before returning, so sending all queries at once
    #    (with shouldDownloadVideos) routinely times out. Search in small chunks instead:
    #    each sync run stays fast, one failing chunk can't wipe out the whole pool, and we
    #    stop early once enough clips are accepted.
    queries = build_queries(["tiktok"], terms, script_text, script_relevancy, count=max(count, 3))
    _status(status_cb, f"Apify (legacy fallback path): searching TikTok for {len(queries)} flat queries...")
    per_q = max(2, (count // max(len(queries), 1)) + 2)
    need_total = count + (1 if accepted else 0)
    CHUNK = 3
    got_any = False
    for i in range(0, len(queries), CHUNK):
        if (cancel_check and cancel_check()) or len(accepted) >= need_total:
            break
        chunk = queries[i:i + CHUNK]
        items = apify_search(chunk, per_q, status_cb=status_cb)
        if items:
            got_any = True
            _take(items, need_total)
    if not got_any and not accepted:
        _status(status_cb, "Apify: no clips returned for these queries. Try broader style terms.")
        return []

    # 3) Normalize accepted raws into the seedance folder, hook first.
    results = []
    for raw in accepted[:count]:
        stability = stable_segment_profile(raw, ffmpeg, ffprobe, per_clip_seconds)
        if not stability["stable"]:
            _status(status_cb, "Scrape: skipped a clip whose usable window is an internal rapid-cut montage.")
            continue
        final = normalize_clip(raw, out_dir / f"scraped_{len(results):02d}.mp4", ffmpeg,
                               seconds=per_clip_seconds, start=stability["start"])
        if final:
            results.append(final)
            _status(status_cb, f"Scrape: prepared clip {len(results)}.")
    try:
        for f in raw_dir.glob("*"):
            f.unlink()
        raw_dir.rmdir()
    except Exception:
        pass
    _status(status_cb, f"Scrape: {len(results)} watermark-free TikTok clip(s) ready.")
    return results


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
    m["cover"] = str(v.get("cover") or v.get("originCover") or vm.get("coverUrl") or "")
    stats = item.get("statistics") if isinstance(item.get("statistics"), dict) else (
        item.get("stats") if isinstance(item.get("stats"), dict) else {})
    try:
        m["likes"] = int(float(
            item.get("diggCount") or item.get("digg_count") or item.get("likeCount")
            or stats.get("diggCount") or stats.get("digg_count") or stats.get("likeCount") or 0
        ))
    except (TypeError, ValueError):
        m["likes"] = 0
    return m


def pre_download_candidate_filter(item, bucket_terms="", seen_ids=None, min_likes=0):
    """Decide BEFORE download whether a candidate is worth fetching, from metadata only.
    Returns (accept: bool, reason: str, meta: dict). Rejects format junk (slideshow / live /
    screen-recording / anime / news / quiz / idol-promo / non-vertical / low-res / duplicate)."""
    m = _item_meta(item)
    seen_ids = seen_ids if seen_ids is not None else set()
    blob = " ".join([m.get("caption", ""), " ".join(m.get("hashtags", [])),
                     m.get("author_name", ""), m.get("author_sig", ""), m.get("music", "")]).lower()
    vid = m.get("id") or m.get("url")
    if vid and vid in seen_ids:
        return False, "duplicate video id/url", m
    if m.get("is_image_post"):
        return False, "slideshow/image post", m
    w, h = m.get("w", 0), m.get("h", 0)
    if w and h:
        if h < w * MIN_PORTRAIT_RATIO:
            return False, "non-vertical source (metadata aspect ratio)", m
        if max(w, h) < 600:
            return False, "too low resolution", m
    dur = m.get("duration", 0.0)
    if dur and (dur < 1.5 or dur > 600):
        return False, "duration out of range", m
    try:
        min_likes = max(0, int(min_likes or 0))
    except (TypeError, ValueError):
        min_likes = 0
    if min_likes and int(m.get("likes") or 0) < min_likes:
        likes = int(m.get("likes") or 0)
        reason = f"likes below {min_likes:,} ({likes:,})" if likes else f"like count missing/below {min_likes:,}"
        return False, reason, m
    if any(t in blob for t in _ANIME_GAME_TERMS):
        return False, "anime/vtuber/game/cgi content", m
    if any(t in blob for t in _NEWS_QUIZ_TERMS):
        return False, "news/quiz/diagnosis/textpost", m
    if any(t in blob for t in _LIVE_SCREEN_TERMS):
        return False, "livestream/screen recording", m
    if any(t in blob for t in _IDOL_PROMO_TERMS):
        return False, "idol/audition/promo content", m
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


def text_heaviness_score(path, ffmpeg, seconds=DEFAULT_CLIP_SECONDS):
    """0-10 estimate of burned-in caption/text load across 5 frames (10%/30%/50%/70%/90%-ish).
    Uses the centre-band bright-text heuristic; tuned to ignore signage/neon."""
    if cv2 is None or np is None:
        return 0.0
    frames = _sample_gray_frames(path, ffmpeg, 5, seconds)
    if not frames:
        return 0.0
    counts = [_frame_caption_lines(g) for g in frames]
    frac = sum(1 for c in counts if c >= 1) / len(counts)
    avg = sum(counts) / len(counts)
    return round(min(10.0, frac * 6.0 + avg * 2.0), 1)


def scrape_bucket(out_dir, queries, want, bucket_id="", tier="exact", bucket_terms="",
                  per_clip_seconds=DEFAULT_CLIP_SECONDS, status_cb=None, cancel_check=None,
                  seen_ids=None, query_perf=None, candidate_statuses=None, min_likes=0,
                  search_sort="MOST_LIKED"):
    """Search the EXACT given bucket queries (NO script-derived expansion via build_queries),
    pre-filter by metadata, download, then reject fake-vertical/black-bar and text-heavy clips.
    Returns accepted dicts: {path, meta, query, tier, clip_id, black_bar_score, text_heaviness,
    is_fake_vertical}. Records per-query stats in query_perf and per-candidate status rows in
    candidate_statuses (lists, if provided). Uses the active search backend (a logged-in TikTok
    session by default, Apify only as a fallback when its token is set)."""
    queries = [str(q).strip() for q in (queries or []) if str(q).strip()]
    if not backend_active() or not queries:
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
    diversity_slots = min(3, len(queries), max(1, int(want)))
    per_query_quota = max(1, (int(want) + diversity_slots - 1) // diversity_slots)

    def _reject(raw):
        try:
            raw.unlink()
        except Exception:
            pass

    def _cstat(cid, status, reason, extra=None):
        if candidate_statuses is not None:
            row = {"clip_id": cid, "bucket_id": bucket_id, "source_query": q, "tier": tier,
                   "status": status, "shown_in_media_panel": False, "reason": reason}
            if extra:
                row.update(extra)
            candidate_statuses.append(row)

    for q in queries:
        if (cancel_check and cancel_check()) or len(accepted) >= want:
            break
        query_stop = min(int(want), len(accepted) + per_query_quota)
        items = backend_search(q, max(4, want + 2), status_cb=status_cb,
                               sort=search_sort) or []
        if _APIFY_OUT_OF_CREDITS[0]:        # apify fallback: stop hammering a billing-blocked account
            break
        raw_n = len(items)
        meta_rej = dl = vert = clean = acc = 0
        meta_reasons = {}
        for it in items:
            if (cancel_check and cancel_check()) or len(accepted) >= query_stop:
                break
            ok, reason, m = pre_download_candidate_filter(
                it, bucket_terms, seen_ids, min_likes=min_likes)
            cid = m.get("id") or m.get("url") or f"{bucket_id}:{q}:{raw_n}:{dl}"
            if not ok:
                meta_rej += 1
                meta_reasons[reason] = meta_reasons.get(reason, 0) + 1
                _cstat(cid, "pre_download_rejected", reason)
                continue
            raw = raw_dir / f"raw_{len(accepted)}_{dl}.mp4"
            dl += 1
            if not backend_download(it, raw, status_cb=None):
                _cstat(cid, "download_failed", "download failed")
                continue
            if cid:
                seen_ids.add(cid)
            if not is_vertical_hq(raw, ffprobe):
                _reject(raw)
                _cstat(cid, "rejected_quality", "low-res / landscape file")
                continue
            vert += 1
            fv = detect_fake_vertical_or_black_bars(raw, ffmpeg, per_clip_seconds)
            if fv["is_fake_vertical"] or fv["black_bar_score"] > 4.0:
                _status(status_cb, f"Rejected clip ({bucket_id}/{q}): fake vertical / black bars "
                                   f"({fv['reason']})")
                _reject(raw)
                _cstat(cid, "rejected_black_bars", fv["reason"],
                       {"black_bar_score": fv["black_bar_score"], "is_fake_vertical": fv["is_fake_vertical"]})
                continue
            th = text_heaviness_score(raw, ffmpeg, per_clip_seconds)
            # 4.0 was too lax (it let clips with captions in 2 of 5 sampled frames through). 2.8
            # rejects anything with burned-in text on more than one frame, keeping only clips that
            # are essentially text-free (incidental signage still scores ~1.6 and is allowed).
            if th > 2.8:
                _status(status_cb, f"Rejected clip ({bucket_id}/{q}): text-heavy TikTok captions ({th}/10)")
                _reject(raw)
                _cstat(cid, "rejected_text_heavy", f"burned-in text {th}/10",
                       {"text_heaviness_score": th})
                continue
            stability = stable_segment_profile(raw, ffmpeg, ffprobe, per_clip_seconds)
            if not stability["stable"]:
                _status(status_cb, f"Rejected clip ({bucket_id}/{q}): rapid internal edit montage "
                                   f"({stability['internal_cut_count']} cuts; shortest hold "
                                   f"{stability['min_shot_seconds']:.2f}s)")
                _reject(raw)
                _cstat(cid, "rejected_rapid_cuts", "source contains rapid internal edit cuts",
                       stability)
                continue
            # Tier calls share out_dir.  A simple 00/01 counter overwrote clips from an
            # earlier tier/round while the matcher still referenced those paths, making
            # the selected footage differ from what vision reviewed.  Use a stable unique
            # name tied to the TikTok item instead.
            file_key = hashlib.sha1(
                str(cid or it.get("webVideoUrl") or it.get("id") or raw.name).encode("utf-8", "ignore")
            ).hexdigest()[:12]
            safe_bucket = re.sub(r"[^A-Za-z0-9_-]+", "_", str(bucket_id or "bucket"))[:36]
            safe_tier = re.sub(r"[^A-Za-z0-9_-]+", "_", str(tier or "tier"))[:20]
            final = normalize_clip(raw, out_dir / f"cand_{safe_bucket}_{safe_tier}_{file_key}.mp4",
                                   ffmpeg, seconds=per_clip_seconds, start=stability["start"])
            _reject(raw)
            if not final:
                _cstat(cid, "rejected_quality", "normalize failed")
                continue
            clean += 1
            acc += 1
            accepted.append({"path": final, "meta": m, "query": q, "tier": tier, "clip_id": cid,
                             "likes": m.get("likes", 0),
                             "black_bar_score": fv["black_bar_score"], "text_heaviness": th,
                             "is_fake_vertical": False,
                             "internal_cut_count": stability["internal_cut_count"],
                             "rapid_internal_cut_count": stability["rapid_internal_cut_count"],
                             "min_shot_seconds": stability["min_shot_seconds"]})
            _status(status_cb, f"Downloaded accepted candidate {len(accepted)} for bucket {bucket_id} (query {q})")
            _cstat(cid, "downloaded_pending_review", "passed pre-filters",
                   {"likes": m.get("likes", 0), "black_bar_score": fv["black_bar_score"],
                    "text_heaviness_score": th,
                    "internal_cut_count": stability["internal_cut_count"],
                    "rapid_internal_cut_count": stability["rapid_internal_cut_count"],
                    "min_shot_seconds": stability["min_shot_seconds"]})
        if query_perf is not None:
            query_perf.append({"query": q, "bucket_id": bucket_id, "tier": tier,
                               "sort": str(search_sort or "MOST_LIKED").upper(),
                               "raw_results": raw_n, "metadata_rejected": meta_rej,
                               "downloadable": dl, "vertical_hq": vert, "text_clean": clean,
                               "accepted": acc, "metadata_rejection_reasons": meta_reasons})
        if meta_rej:
            summary = ", ".join(f"{reason}: {count}" for reason, count in
                                sorted(meta_reasons.items(), key=lambda row: row[1], reverse=True))
            _status(status_cb, f"Metadata filter ({bucket_id}/{q}): rejected {meta_rej}/{raw_n} "
                               f"candidate(s) ({summary}).")
    try:
        if raw_dir.exists():
            for f in raw_dir.glob("*"):
                f.unlink()
            raw_dir.rmdir()
    except Exception:
        pass
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
    # Apify is the preferred path when configured: real TikTok keyword search +
    # watermark-free download. yt-dlp (cookies) is the fallback.
    if apify_active():
        _status(status_cb, "Scrape: using Apify TikTok search (watermark-free).")
        return _scrape_via_apify(out_dir, terms, count, script_text, script_relevancy,
                                 per_clip_seconds, lead_query, status_cb, cancel_check)
    # TikTok-only via yt-dlp; a cookie connection is mandatory for that path.
    if not cookies_active():
        _status(status_cb, "Scrape: not connected. Set an Apify token, or connect TikTok "
                           "(pick your signed-in browser) in the scrape settings, and try again.")
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
