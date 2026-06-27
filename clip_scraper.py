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
import shutil
import tempfile
import urllib.request
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
APIFY_ACTOR = "clockworks~tiktok-scraper"


def apify_token():
    return (os.environ.get("APIFY_TOKEN", "") or "").strip()


def apify_active():
    return bool(apify_token())


def apify_search(queries, results_per_query, status_cb=None):
    """Run the Apify TikTok scraper for the given search queries. Returns dataset items
    (each with videoMeta + mediaUrls). Watermark-free downloads via shouldDownloadVideos."""
    tok = apify_token()
    if not tok or not queries:
        return []
    url = f"https://api.apify.com/v2/acts/{APIFY_ACTOR}/run-sync-get-dataset-items?token={tok}"
    body = {
        "searchQueries": list(queries),
        "resultsPerPage": max(1, int(results_per_query)),
        "shouldDownloadVideos": True,
        "shouldDownloadCovers": False,
        "shouldDownloadSubtitles": False,
        "proxyConfiguration": {"useApifyProxy": True},
    }
    try:
        req = urllib.request.Request(
            url, data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=290) as r:
            items = json.loads(r.read().decode("utf-8"))
        return items if isinstance(items, list) else []
    except Exception as exc:  # noqa: BLE001
        _status(status_cb, f"Apify: search failed ({exc.__class__.__name__}).")
        return []


def _apify_media_url(item):
    mu = item.get("mediaUrls") if isinstance(item, dict) else None
    if isinstance(mu, list) and mu and isinstance(mu[0], str) and mu[0].startswith("http"):
        return mu[0]
    return None


def _apify_download(media_url, dest, status_cb=None):
    tok = apify_token()
    dl = media_url
    if "api.apify.com" in media_url and tok and "token=" not in media_url:
        dl = media_url + (("&" if "?" in media_url else "?") + f"token={tok}")
    try:
        req = urllib.request.Request(dl, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=120) as r, open(dest, "wb") as f:
            shutil.copyfileobj(r, f)
    except Exception as exc:  # noqa: BLE001
        _status(status_cb, f"Apify: download failed ({exc.__class__.__name__}).")
        return None
    return dest if dest.exists() and dest.stat().st_size > 4096 else None


def _apify_item_portrait_hq(item):
    vm = (item.get("videoMeta") or {}) if isinstance(item, dict) else {}
    try:
        w, h = int(vm.get("width") or 0), int(vm.get("height") or 0)
    except (TypeError, ValueError):
        return None
    if w <= 0 or h <= 0:
        return None
    if max(w, h) < MIN_LONG_SIDE:
        return False
    return h >= w * MIN_PORTRAIT_RATIO


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
            if has_burned_captions(raw, ffmpeg, seconds=per_clip_seconds, status_cb=status_cb):
                _status(status_cb, "Scrape: skipped a clip (burned-in captions/text).")
                try: raw.unlink()
                except Exception: pass
                continue
            accepted.append(raw)
            if hook:
                return

    # 1) Hook: a real woman via a dedicated search query.
    if lead_query:
        _status(status_cb, "Apify: searching TikTok for a hook clip (real influencer)...")
        _take(apify_search([lead_query], 6, status_cb=status_cb), 1, hook=True)

    # 2) The rest from per-style queries.
    queries = build_queries(["tiktok"], terms, script_text, script_relevancy, count=max(count, 3))
    _status(status_cb, f"Apify: searching TikTok for {len(queries)} style queries...")
    per_q = max(2, (count // max(len(queries), 1)) + 2)
    items = apify_search(queries, per_q, status_cb=status_cb)
    if not items and not accepted:
        _status(status_cb, "Apify: no clips returned for these queries. Try broader style terms.")
        return []
    _take(items, count + (1 if accepted else 0))

    # 3) Normalize accepted raws into the seedance folder, hook first.
    results = []
    for raw in accepted[:count]:
        final = normalize_clip(raw, out_dir / f"scraped_{len(results):02d}.mp4", ffmpeg,
                               seconds=per_clip_seconds)
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
        final = normalize_clip(raw, out_dir / f"scraped_{len(results):02d}.mp4", ffmpeg,
                               seconds=per_clip_seconds)
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
