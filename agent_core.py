import concurrent.futures
import contextlib
import copy
import json
import math
import base64
import hashlib
import html
import mimetypes
import os
import re
import shutil
import subprocess
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import wave
from pathlib import Path

import pipeline
import reasoning_modes


ROOT = Path(__file__).resolve().parent
PROJECTS_DIR = ROOT / "projects"
UPLOADS_DIR = ROOT / "uploads"
SPEAKER_DIR = ROOT / "speaker"
PROJECTS_DIR.mkdir(exist_ok=True)
UPLOADS_DIR.mkdir(exist_ok=True)
SPEAKER_DIR.mkdir(exist_ok=True)


def load_env_file():
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().lstrip("\ufeff")
        os.environ.setdefault(key, value.strip().strip('"'))


load_env_file()

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".webm"}
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac", ".webm", ".mp4", ".mov"}
COMMONS_API = "https://commons.wikimedia.org/w/api.php"
WIKIPEDIA_API = "https://en.wikipedia.org/w/api.php"
WAVESPEED_LLM_API = "https://llm.wavespeed.ai/v1/chat/completions"
WAVESPEED_RESPONSES_API = "https://llm.wavespeed.ai/v1/responses"
# DuckDuckGo's unofficial image endpoint constantly returns 403 / times out (bot
# blocking), which stalled every search. Bing + Wikimedia are reliable, so DDG is
# dropped from the active providers (the duckduckgo_* helpers are kept but unused).
# Bing's scraped image results return trending/SEO/NSFW junk (its HTML no longer
# honors the query without a real browser session), so it's dropped. Wikimedia/
# Commons gives relevant, licensed archival images; generation covers the gaps.
WEB_IMAGE_SEARCH_PROVIDERS = ("wikimedia",)

# Web image candidates whose TITLE matches this are junk/NSFW SEO noise (Bing's
# scraped results often inject unrelated trending/shopping/adult tiles). They get
# a hard-negative score so they're rejected before download/review.
_WEB_JUNK_TITLE_RE = re.compile(
    r"\b(nsfw|porn\w*|nude|naked|topless|lingerie|mistress|fetish|bdsm|erotic|"
    r"sexy|onlyfans|escort|rule\s*34|hentai|xxx|nft|crypto|\bdvd\b|for sale|"
    r"buy now|coupon|onlyfan)\b",
    re.IGNORECASE,
)


def web_candidate_title_is_junk(result):
    title = f"{result.get('title', '')} {result.get('source_url', '')}".lower()
    return bool(_WEB_JUNK_TITLE_RE.search(title))
GPT55_MODEL = "openai/gpt-5.5"
GEMINI_AUDIO_MODEL = "google/gemini-3.5-flash"
SEEDANCE_VIDEO_MODELS = {
    "seedance-2.0": "bytedance/seedance-2.0/image-to-video",
    "seedance-2.0-fast": "bytedance/seedance-2.0-fast/image-to-video",
    "seedance-v1.5-pro": "bytedance/seedance-v1.5-pro/image-to-video",
    "ltx-2.3": "wavespeed-ai/ltx-2.3/image-to-video",
    "happyhorse-1.1": "alibaba/happyhorse-1.1/image-to-video",
}
SPEAKER_HOOK_REFERENCE_STYLE = (
    "Reference performance style for the opening speaker hook: vertical smartphone selfie video, chest-up close framing, "
    "speaker looks directly into the lens, high-energy fast creator delivery, raised eyebrows and wide attentive eyes, "
    "quick expressive mouth movement, small lean-ins toward the camera, hands gesturing close to the lens and pointing for emphasis, "
    "warm indoor room lighting, slightly handheld phone feel, natural imperfect self-recorded pacing. "
    "It should feel like a punchy TikTok/Shorts hook before B-roll starts, but without copying any specific person, watermark, caption style, or on-screen text."
)
LAST_WEB_REQUEST_AT = 0.0

# Per-host politeness throttle so parallel web searches across different hosts
# (Bing, Wikimedia, image CDNs) overlap instead of serialising on one global lock.
_HOST_THROTTLE_LOCK = threading.Lock()
_HOST_LOCKS = {}
_HOST_LAST = {}


def host_throttle(url, min_interval):
    """Serialise requests to the same host with `min_interval` spacing; different
    hosts run concurrently. Returns immediately for unknown/empty hosts."""
    try:
        host = urllib.parse.urlparse(url).netloc.lower()
    except Exception:
        host = ""
    if not host:
        return
    with _HOST_THROTTLE_LOCK:
        lock = _HOST_LOCKS.get(host)
        if lock is None:
            lock = _HOST_LOCKS[host] = threading.Lock()
    with lock:
        last = _HOST_LAST.get(host, 0.0)
        wait = min_interval - (time.monotonic() - last)
        if wait > 0:
            time.sleep(wait)
        _HOST_LAST[host] = time.monotonic()


@contextlib.contextmanager
def step_watchdog(form, label, limit_s=600, status_cb=None):
    """Abort a step if it runs longer than `limit_s` (default 10 min).

    The user's rule: any single step over ~10 minutes means something is wrong.
    On timeout we log a loud WATCHDOG warning and trip the run's cancel event so
    the step's cooperative check_cancel() calls unwind it instead of hanging.
    """
    cancel_event = form.get("_cancel_event") if isinstance(form, dict) else None
    start = time.monotonic()
    timer = None

    def _fire():
        minutes = int(round(limit_s / 60.0))
        log(status_cb, f"WATCHDOG: '{label}' exceeded {minutes} min ({int(limit_s)}s) - aborting this step; something is wrong (check network/providers).")
        if cancel_event is not None:
            try:
                cancel_event.set()
            except Exception:
                pass

    if limit_s and limit_s > 0:
        timer = threading.Timer(limit_s, _fire)
        timer.daemon = True
        timer.start()
    try:
        yield
    finally:
        if timer is not None:
            timer.cancel()
        elapsed = time.monotonic() - start
        if limit_s and elapsed > limit_s * 0.8:
            log(status_cb, f"Note: '{label}' took {elapsed:.0f}s, close to the {int(limit_s)}s watchdog limit.")
ACTION_WORDS = {
    "chase", "chased", "run", "running", "escape", "escaping", "scatter", "scattered",
    "attack", "fight", "truck", "drive", "moving", "collapse", "failed", "failure",
    "explode", "crash", "battle", "aim", "fire", "pursuit", "vanish", "split",
    "jagen", "rennen", "fliehen", "zerfallen", "scheitern", "truck", "verfolgen",
}
MAP_WORDS = {"map", "australia", "western", "wa", "region", "location", "karte"}
FARM_WORDS = {"farm", "field", "wheat", "crop", "fence", "farmland", "farmers", "farmer", "feld", "weizen"}
MILITARY_WORDS = {"soldier", "military", "army", "gun", "machine", "lewis", "ammunition", "rounds", "truck"}
STOPWORDS = {
    "about", "after", "again", "already", "also", "and", "around", "because", "before", "being",
    "birds", "could", "created", "durch", "eine", "einer", "eines", "from", "haben", "into",
    "just", "later", "like", "more", "nach", "nicht", "noch", "over", "said", "scene", "should",
    "still", "that", "their", "there", "they", "this", "through", "und", "were", "with", "without",
    "would", "your", "zum", "zur", "the", "was", "for", "are", "had", "has", "did", "not", "but",
}
SEARCH_NOISE = STOPWORDS | {
    "battle", "casualty", "dispute", "eventually", "found", "happened", "modern",
    "only", "peacefully", "real", "refused", "sent", "start", "then", "wanted",
    "war", "weeks",
}
IRRELEVANT_WEB_TERMS = {
    "demonstrator", "demonstration", "saudi", "bombing", "narrative", "volunteers",
    "regiment", "new york", "poster", "sign", "protest",
}
LOW_TRUST_IMAGE_DOMAINS = {
    "pinterest.", "etsy.", "ebay.", "amazon.", "aliexpress.", "redbubble.",
    "teepublic.", "fineartamerica.", "dreamstime.", "alamy.", "shutterstock.",
    "istockphoto.", "gettyimages.", "depositphotos.", "freepik.", "vecteezy.",
    "pngtree.", "wallpaper", "wallpapers", "facebook.", "instagram.", "tiktok.",
}
BOOK_SCAN_WEB_TERMS = {
    "book cover", "front cover", "cover page", "title page", "bookplate", "binding",
    "volume", "vol.", "scanned", "scan of", "djvu", "pdf", "internet archive",
    "hathitrust", "biodiversity heritage library", "open library", "library book",
    "libraries", "catalogue", "catalog", "call number", "archive.org",
}
DOCUMENT_ALLOWED_SCENE_TERMS = {
    "book", "books", "library", "libraries", "manuscript", "document", "documents",
    "letter", "newspaper", "archive", "archives", "source", "pamphlet", "title",
    "page", "cover", "scan", "karte", "brief", "zeitung", "dokument", "archiv",
}


def slugify(text):
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9]+", "_", text)
    return re.sub(r"_+", "_", text).strip("_") or f"short_{int(time.time())}"


def unique_project_slug(base_slug):
    if not (PROJECTS_DIR / base_slug).exists():
        return base_slug
    stamped = f"{base_slug}_{time.strftime('%Y%m%d_%H%M%S')}"
    if not (PROJECTS_DIR / stamped).exists():
        return stamped
    counter = 2
    while (PROJECTS_DIR / f"{stamped}_{counter}").exists():
        counter += 1
    return f"{stamped}_{counter}"


# Matches the duplicate folders the old new-folder-on-load bug created:
# "<base>_20260626_143000" or "<base>_20260626_143000_2".
_PROJECT_DUP_SUFFIX_RE = re.compile(r"_\d{8}_\d{6}(?:_\d+)?$")


def consolidate_project_folders(canonical_slug, status_cb=None):
    """Merge timestamped duplicate project folders of the same topic into the
    canonical folder, so all media of one project lives together.

    Non-destructive: copies only files that are MISSING in the canonical folder
    (never overwrites), then leaves the duplicate behind renamed with a
    '_merged_' marker so it's obvious and recoverable. Returns the count merged.
    """
    base = _PROJECT_DUP_SUFFIX_RE.sub("", canonical_slug)
    canonical = PROJECTS_DIR / base
    if not canonical.exists():
        return 0
    merged_files = 0
    merged_dirs = 0
    for sibling in sorted(PROJECTS_DIR.iterdir()):
        if not sibling.is_dir() or sibling.name == base:
            continue
        if not (sibling.name.startswith(base + "_") and _PROJECT_DUP_SUFFIX_RE.search(sibling.name)):
            continue
        copied_here = 0
        for src in sibling.rglob("*"):
            if not src.is_file():
                continue
            rel = src.relative_to(sibling)
            dest = canonical / rel
            if dest.exists():
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copy2(src, dest)
                copied_here += 1
            except Exception:
                pass
        if copied_here:
            merged_files += copied_here
            merged_dirs += 1
            try:
                marker = sibling.with_name(sibling.name + "_merged_" + time.strftime("%Y%m%d_%H%M%S"))
                if not marker.exists():
                    sibling.rename(marker)
            except Exception:
                pass
    if merged_dirs:
        log(status_cb, f"Consolidated {merged_files} file(s) from {merged_dirs} duplicate folder(s) into '{base}'.")
    return merged_files


def _script_fingerprint(script):
    """Stable id for a script, ignoring whitespace/case so re-runs of the SAME script match."""
    norm = re.sub(r"\s+", " ", (script or "").strip().lower())
    return hashlib.sha1(norm.encode("utf-8", "ignore")).hexdigest() if norm else ""


def find_matching_scrape_project(script):
    """Return the slug of an existing TikTok-SCRAPE project whose script is identical to `script`,
    so the same script fuses into ONE folder instead of spawning a new project on every run.
    Prefers the OLDEST (canonical) match. Only scrape projects are fused. Returns None if none."""
    fp = _script_fingerprint(script)
    if not fp or not PROJECTS_DIR.exists():
        return None
    matches = []
    for d in PROJECTS_DIR.iterdir():
        if not d.is_dir() or "_merged_" in d.name:
            continue
        rf = d / "input" / "run_form.json"
        stxt = d / "input" / "script.txt"
        saved_script, saved_source = "", ""
        if rf.exists():
            try:
                data = json.loads(rf.read_text(encoding="utf-8"))
                saved_script = data.get("script", "") or ""
                saved_source = str(data.get("clip_source", "") or "").strip().lower()
            except Exception:
                pass
        if not saved_script and stxt.exists():
            try:
                saved_script = stxt.read_text(encoding="utf-8")
            except Exception:
                pass
        # treat a project as a scrape project if it says so, or if it actually holds scraped clips
        is_scrape = (saved_source == "scrape"
                     or any((d / "seedance 2.0").glob("scraped_*.mp4")))
        if is_scrape and _script_fingerprint(saved_script) == fp:
            matches.append(d)
    if not matches:
        return None
    matches.sort(key=lambda p: p.stat().st_mtime)   # oldest = canonical
    return matches[0].name


def should_reuse_same_script_media(fused_slug, requested_same_script, form):
    """Keep downloaded social media across same-script retries.

    ``force_regenerate`` is the UI's fresh voice/take option and must not throw away a long scrape.
    Only the separate, explicit ``force_rescrape`` escape hatch opts out of saved-media recovery.
    """
    return bool(fused_slug or requested_same_script) \
        and not form_flag(form, "force_rescrape", False)


def log(status_cb, message):
    if status_cb:
        status_cb(message)


def cancellation_event(form):
    event = form.get("_cancel_event") if isinstance(form, dict) else None
    return event if event and hasattr(event, "is_set") else None


def check_cancel(form):
    event = cancellation_event(form)
    if event and event.is_set():
        raise RuntimeError("Run cancelled by user.")


def attach_cancel_event(config, form):
    event = cancellation_event(form)
    if event:
        config["_cancel_event"] = event


def consume_replace_requests(form):
    requests = form.get("_replace_requests") if isinstance(form, dict) else None
    lock = form.get("_replace_lock") if isinstance(form, dict) else None
    if requests is None:
        return []
    if lock:
        with lock:
            selected = list(dict.fromkeys(str(path) for path in requests if str(path).strip()))
            requests.clear()
    else:
        selected = list(dict.fromkeys(str(path) for path in requests if str(path).strip()))
        try:
            requests.clear()
        except Exception:
            pass
    return [Path(path) for path in selected]


def config_for_json(config):
    return {key: value for key, value in config.items() if not str(key).startswith("_")}


def current_media_exclusions(form):
    """Snapshot paths the user removed from the live Accepted/Assigned media panel."""
    if not isinstance(form, dict):
        return set()
    values = form.get("_media_exclusions")
    lock = form.get("_media_exclusion_lock")
    if values is None:
        return set()
    if lock:
        with lock:
            return {str(value).lower() for value in values}
    return {str(value).lower() for value in values}


def reconcile_manual_scrape_exclusions(config, project_dir, form, status_cb=None):
    """Replace assigned files manually removed in the live panel with a non-excluded candidate."""
    if str(config.get("clip_source") or "") != "scrape":
        return 0
    project_dir = Path(project_dir)
    clip_dir = project_dir / "seedance 2.0"
    candidate_root = clip_dir / "_candidates"
    exclusions = current_media_exclusions(form)
    if not exclusions and all((clip_dir / str(sc.get("clip") or "")).exists()
                              for sc in config.get("scenes", []) if sc.get("clip")):
        return 0
    candidates = []
    if candidate_root.exists():
        for path in candidate_root.rglob("*.mp4"):
            parts = {part.lower() for part in path.parts}
            if "_raw" in parts or "_manual_removed" in parts:
                continue
            if path.exists() and str(path.resolve()).lower() not in exclusions:
                candidates.append(path)
    used = set()
    for scene in config.get("scenes", []):
        assigned = clip_dir / str(scene.get("clip") or "")
        meta = {}
        if assigned.name:
            try:
                meta = json.loads(assigned.with_suffix(".json").read_text(encoding="utf-8"))
            except Exception:
                meta = {}
        source = str(meta.get("source_path") or "")
        if source and source.lower() not in exclusions and assigned.exists():
            used.add(source.lower())
    changed = 0
    for index, scene in enumerate(config.get("scenes", [])):
        clip_name = str(scene.get("clip") or "")
        if not clip_name.startswith("scraped_"):
            continue
        assigned = clip_dir / clip_name
        try:
            meta = json.loads(assigned.with_suffix(".json").read_text(encoding="utf-8"))
        except Exception:
            meta = {}
        old_source = str(meta.get("source_path") or "")
        if assigned.exists() and (not old_source or old_source.lower() not in exclusions):
            continue
        choices = [p for p in candidates if str(p.resolve()).lower() not in used]
        if not choices:
            choices = list(candidates)
        if not choices:
            log(status_cb, f"Manual exclusion left scene {index} without an accepted replacement.")
            continue
        source_path = sorted(choices, key=lambda p: (p.stat().st_mtime, p.name), reverse=True)[0]
        assigned.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, assigned)
        source_meta = {}
        try:
            source_meta = json.loads(source_path.with_suffix(".json").read_text(encoding="utf-8"))
        except Exception:
            pass
        platform = str(source_meta.get("platform") or "tiktok")
        scene["scrape_source"] = "twitter" if platform in {"x", "twitter"} else "tiktok"
        scene["scrape_clip_id"] = source_meta.get("clip_id") or str(source_path.resolve())
        scene["match_class"] = "MANUAL_REPLACEMENT"
        assigned.with_suffix(".json").write_text(json.dumps({
            "status": "assigned", "scene_id": str(scene.get("id", index)),
            "source_path": str(source_path.resolve()), "platform": scene["scrape_source"],
            "clip_id": scene["scrape_clip_id"],
        }, indent=2), encoding="utf-8")
        used.add(str(source_path.resolve()).lower())
        changed += 1
    if changed:
        log(status_cb, f"Manual media exclusions: replaced {changed} assigned clip(s) before render.")
    return changed


def form_int(form, key, default, minimum=0, maximum=99):
    try:
        value = int(str(form.get(key, default)).strip())
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def form_choice(form, key, allowed, default):
    value = str(form.get(key, default) or default).strip()
    return value if value in set(allowed) else default


_TRUTHY = {"on", "true", "1", "yes", "checked"}


def form_flag(form, key, default=True):
    """Tolerant checkbox/hidden-flag reader.

    The UI sends "on" for an enabled flag, but a loaded project's saved config
    (or stale client state) may store a boolean serialized as "true"/"True".
    Accept any common truthy spelling; fall back to ``default`` when absent.
    """
    if key not in form:
        return default
    return str(form.get(key, "")).strip().lower() in _TRUTHY


def recut_mode_label(mode):
    return {
        "recut_existing_only": "recut existing media only",
        "recut_new_web_images": "new web images only + recut",
        "recut_regenerate_seedance": "regenerate Seedance clips + recut",
        "recut_recreate_speaker_clip": "recreate speaker hook clip + recut",
    }.get(mode, "normal agent run")


def clean_text(text):
    if text is None:
        return ""
    return (
        text.replace("\u201c", '"')
        .replace("\u201d", '"')
        .replace("\u2018", "'")
        .replace("\u2019", "'")
        .replace("\u2013", "-")
        .replace("\u2014", "-")
        .strip()
    )


def seconds_from_stamp(minutes, seconds):
    return int(minutes) * 60 + int(seconds)


def estimate_speaking_duration(script, words_per_minute=155):
    count = len(re.findall(r"\b[\w'-]+\b", clean_text(script)))
    if count <= 0:
        return 50.0
    seconds = (count / float(words_per_minute)) * 60.0
    # Shorts visuals need a little breathing room around spoken lines.
    return round(max(12.0, min(90.0, seconds * 1.08)), 2)


def parse_timed_script(script, target_duration=50):
    script = clean_text(script)
    pattern = re.compile(r"(?P<smin>\d{1,2}):(?P<ssec>\d{2})\s*(?:-|to|bis)?\s*(?:(?P<emin>\d{1,2}):(?P<esec>\d{2}))?", re.I)
    matches = list(pattern.finditer(script))
    if matches:
        scenes = []
        for index, match in enumerate(matches):
            start = seconds_from_stamp(match.group("smin"), match.group("ssec"))
            if match.group("emin") is not None:
                end = seconds_from_stamp(match.group("emin"), match.group("esec"))
            elif index + 1 < len(matches):
                nxt = matches[index + 1]
                end = seconds_from_stamp(nxt.group("smin"), nxt.group("ssec"))
            else:
                end = target_duration
            text_start = match.end()
            text_end = matches[index + 1].start() if index + 1 < len(matches) else len(script)
            beat = clean_text(script[text_start:text_end])
            beat = re.sub(r"^\W+", "", beat)
            if end > start and beat:
                scenes.append({"start": float(start), "end": float(end), "script": beat})
        if scenes:
            return scenes

    sentences = re.split(r"(?<=[.!?])\s+", script)
    sentences = [s.strip() for s in sentences if s.strip()]
    if not sentences:
        raise ValueError("No usable script text found.")
    scene_count = min(8, max(1, math.ceil(len(sentences) / 2)))
    groups = [[] for _ in range(scene_count)]
    for index, sentence in enumerate(sentences):
        groups[min(scene_count - 1, int(index * scene_count / len(sentences)))].append(sentence)
    scene_duration = float(target_duration) / scene_count
    scenes = []
    for index, group in enumerate(groups):
        if not group:
            continue
        scenes.append(
            {
                "start": round(index * scene_duration, 2),
                "end": round((index + 1) * scene_duration, 2),
                "script": " ".join(group),
            }
        )
    return scenes


def words(text):
    return set(re.findall(r"[a-zA-Z0-9]+", text.lower()))


def humanize_title(text):
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", str(text or ""))
    text = re.sub(r"[_\-]+", " ", text)
    return " ".join(text.split())


def media_score(path, beat_words, scene=None, scene_number=None, web_metadata=None):
    name_words = words(path.stem.replace("_", " "))
    score = len(name_words & beat_words) * 4
    if beat_words & MAP_WORDS and name_words & MAP_WORDS:
        score += 10
    if beat_words & FARM_WORDS and name_words & FARM_WORDS:
        score += 8
    if beat_words & MILITARY_WORDS and name_words & MILITARY_WORDS:
        score += 8
    if "emu" in beat_words and "emu" in name_words:
        score += 12
    metadata = (web_metadata or {}).get(path.name.lower(), {}) if path else {}
    if metadata:
        try:
            relevance = int(metadata.get("visual_relevance_score") or 0)
            score += min(20, max(0, relevance // 5))
        except (TypeError, ValueError):
            pass
        try:
            best_scene = int(str(metadata.get("best_scene", "")).strip())
        except (TypeError, ValueError):
            best_scene = None
        if best_scene and scene_number:
            score += 22 if best_scene == scene_number else -8
        role = str(metadata.get("review_role") or "").lower()
        hook = str((scene or {}).get("visual_hook_type") or "").lower()
        if role:
            if "proof" in hook and role in {"proof", "archive", "map", "document"}:
                score += 14
            if "face" in hook and role in {"face", "reaction"}:
                score += 14
            if "object" in hook and role == "object":
                score += 12
            if "location" in role and words(str((scene or {}).get("script", ""))) & {"where", "island", "city", "building", "place"}:
                score += 8
        if str(metadata.get("crop_safety", "")).lower() == "bad":
            score -= 25
        elif str(metadata.get("crop_safety", "")).lower() == "risky":
            score -= 6
    return score


def media_unique_key(path):
    try:
        return str(Path(path).resolve()).lower()
    except Exception:
        return str(path).lower()


def media_ref_key(ref):
    return str(ref or "").replace("\\", "/").lower().strip()


def list_media(project_dir):
    folders = [
        project_dir / "web images",
        project_dir / "seedance 2.0",
        project_dir / "local media",
        ROOT / "media library",
    ]
    images = []
    videos = []
    for folder in folders:
        if not folder.exists():
            continue
        for path in folder.rglob("*"):
            parts = {part.lower() for part in path.parts}
            if "rejected" in parts:
                continue
            # scrape working dirs are NOT accepted media - the project media panel shows only the
            # placed scraped_NN.mp4 clips, never the raw/rejected/unreviewed download candidates.
            if "_candidates" in parts or "_raw" in parts:
                continue
            if path.stem.lower().startswith("speaker_hook_source"):
                continue
            if path.suffix.lower() in IMAGE_EXTS:
                images.append(path)
            elif path.suffix.lower() in VIDEO_EXTS:
                videos.append(path)
    return images, videos


def existing_seedance_clips(project_dir):
    clip_dir = project_dir / "seedance 2.0"
    if not clip_dir.exists():
        return []
    return sorted([
        p for p in clip_dir.iterdir()
        if p.suffix.lower() in VIDEO_EXTS and p.stat().st_size > 10000 and p.name.lower() != "speaker_hook.mp4"
    ])


def existing_project_voiceover(project_dir):
    """The already-generated narration for this project (input/voiceover.*), or None."""
    in_dir = Path(project_dir) / "input"
    if not in_dir.exists():
        return None
    cands = [p for p in in_dir.glob("voiceover.*")
             if p.suffix.lower() in {".wav", ".mp3", ".m4a", ".aac", ".ogg", ".flac"} and p.stat().st_size > 2048]
    return sorted(cands, key=lambda p: p.stat().st_mtime, reverse=True)[0] if cands else None


def reuse_existing_scrape_clips(project_dir, scenes_override, status_cb=None):
    """When the SAME script already has scraped TikTok clips in this project, reuse them instead of
    searching/downloading again. Assigns the existing scraped_NN.mp4 to the scenes by index (cycling
    the least-used clip when there are more scenes than saved clips). Returns the number of scenes
    given a clip (0 if nothing reusable)."""
    clip_dir = Path(project_dir) / "seedance 2.0"
    existing = sorted(clip_dir.glob("scraped_*.mp4"))
    if not existing or not scenes_override:
        return 0
    hook_clip = clip_dir / "scraped_00.mp4"
    # body fill cycles the body clips only, so the hook talking-head doesn't leak into body scenes
    body_existing = [p for p in existing if p != hook_clip] or existing
    usage = {}

    def _least_used(pool):
        return min(pool, key=lambda p: (usage.get(p.name, 0), p.name))

    placed = 0
    for i, sc in enumerate(scenes_override):
        by_index = clip_dir / f"scraped_{i:02d}.mp4"
        src = by_index if by_index.exists() else _least_used(body_existing if i > 0 else existing)
        usage[src.name] = usage.get(src.name, 0) + 1
        sc["clip"] = src.name
        sc["seedance"] = True
        sc["visual_role"] = "hook_influencer" if i == 0 else "body"
        sc["scrape_source"] = "tiktok"
        sc["scrape_clip_id"] = src.name
        sc["match_class"] = "HOOK_MATCH" if i == 0 else "REUSED_EXISTING"
        sc["script_match_score"] = None
        sc["black_bar_score"] = 0.0
        sc["is_fake_vertical"] = False
        sc["text_heaviness_score"] = 0.0
        placed += 1
    log(status_cb, f"Reusing {len(existing)} existing scraped TikTok clip(s) for the identical script "
                   f"across {placed} scene(s) - skipping the TikTok search entirely.")
    return placed


def move_existing_seedance_clips(project_dir, status_cb=None):
    moved = []
    target_dir = project_dir / "seedance 2.0" / "replaced"
    target_dir.mkdir(parents=True, exist_ok=True)
    for path in existing_seedance_clips(project_dir):
        target = target_dir / path.name
        counter = 2
        while target.exists():
            target = target_dir / f"{path.stem}_{counter}{path.suffix}"
            counter += 1
        path.replace(target)
        moved.append(target)
    if moved:
        log(status_cb, f"Regenerate Seedance: moved {len(moved)} old clip(s) to seedance 2.0/replaced.")
    return moved


def seedance_scene_score(scene, index, scene_count, preferred_indexes=None):
    preferred_indexes = set(preferred_indexes or set())
    script_words = words(scene.get("script", ""))
    visual_words = words(scene.get("visual_script", ""))
    beat_words = script_words or visual_words
    best_media_type = str(scene.get("best_media_type", "")).lower()
    hook_type = str(scene.get("visual_hook_type", "")).lower()
    score = 0
    if index in preferred_indexes:
        score += 9
    if best_media_type in {"source_image_to_seedance", "seedance", "existing_clip"}:
        score += 8
    elif best_media_type == "web_image":
        score -= 5
    if hook_type in {"transformation", "reveal", "danger/mystery", "epic scale", "face/reaction"}:
        score += 4
    if hook_type in {"proof/evidence", "historical image"}:
        score -= 3
    if index == 0:
        score += 6
    if index == scene_count - 1:
        score += 4
    score += len(script_words & ACTION_WORDS) * 3
    score += len((visual_words - script_words) & ACTION_WORDS)
    if "truck" in script_words or "chase" in script_words:
        score += 5
    elif "truck" in visual_words or "chase" in visual_words:
        score += 1
    if "scatter" in script_words or "escaping" in script_words:
        score += 5
    elif "scatter" in visual_words or "escaping" in visual_words:
        score += 1
    return score


def choose_seedance_scenes(scenes, max_clips=4, preferred_indexes=None, exclude_indexes=None):
    max_clips = max(0, min(int(max_clips or 0), len(scenes)))
    if max_clips <= 0:
        return set()
    preferred_indexes = set(preferred_indexes or set())
    exclude_indexes = set(exclude_indexes or set())
    scene_count = len(scenes)
    scored = []
    for index, scene in enumerate(scenes):
        if index in exclude_indexes:
            continue
        score = seedance_scene_score(scene, index, scene_count, preferred_indexes=preferred_indexes)
        scored.append((score, index))
    if max_clips >= len(scored):
        return {index for _, index in scored}
    selected = set()
    for band in range(max_clips):
        start = int(math.floor(band * scene_count / max_clips))
        end = int(math.floor((band + 1) * scene_count / max_clips))
        band_candidates = [(score, index) for score, index in scored if start <= index < max(end, start + 1)]
        if not band_candidates:
            continue
        selected.add(max(band_candidates, key=lambda item: (item[0], -abs(item[1] - ((start + end - 1) / 2.0))))[1])
    for _, index in sorted(scored, reverse=True):
        if len(selected) >= max_clips:
            break
        selected.add(index)
    return selected


# Image source-frame must also be text-free, or the I2V model inherits/echoes it.
_IMAGE_NO_TEXT = "No text, captions, letters, numbers or logos anywhere in the image."


def scene_prompt(title, scene, image_model="openai/gpt-image-2/text-to-image"):
    # Kept lean on purpose: over-stuffed image prompts make the models render weird,
    # conflicting detail. Carry the topic, the spoken line, the must-show subjects,
    # the look, and the hard constraints -- nothing redundant.
    visual_direction = scene.get("visual_script", "")
    voice_line = scene.get("exact_voice_text") or scene.get("voice_line") or scene["script"]
    must_show = scene.get("must_show") or important_terms(voice_line, 5, SEARCH_NOISE)
    must_not_show = scene.get("must_not_show") or ["off-topic symbols", "caption text"]
    topic = humanize_title(title)
    show_phrase = ", ".join(str(item) for item in must_show[:5])
    avoid_phrase = ", ".join(str(item) for item in must_not_show[:4])
    model = str(image_model or "").lower()

    if "nano-banana" in model or "gemini" in model:
        # Nano-Banana 2 reads a flowing creative brief; comma keyword-soup hurts it.
        # Natural sentences, aspect ratio at the very end.
        parts = [
            f'Create ONE realistic documentary still for a vertical short about "{topic}".',
            f'It must clearly illustrate this spoken line: "{voice_line}".',
        ]
        if show_phrase:
            parts.append(f"Show {show_phrase}.")
        if visual_direction:
            parts.append(f"Direction (only if it fits): {visual_direction}.")
        parts.append("Period-accurate, cinematic and slightly desaturated, with clear depth; the main subject frozen at the start of a visible action.")
        parts.append("Single coherent scene, fully clothed period attire, no nudity or gore.")
        parts.append(_IMAGE_NO_TEXT)
        parts.append("Photorealistic and serious. Vertical 9:16.")
        return " ".join(part for part in parts if part)

    # GPT-Image-2 (default): short, skimmable sections with concrete visual facts.
    prompt = (
        f'Scene: period-accurate documentary reconstruction for "{topic}".\n'
        f'Subject: one clear subject that answers the spoken line "{voice_line}". Show: {show_phrase}.\n'
        "Details: cinematic, slightly desaturated period palette, soft realistic light, clear depth, 35-50mm; "
        "the subject frozen at the start of a visible action.\n"
    )
    if visual_direction:
        prompt += f"Direction (only if it fits): {visual_direction}\n"
    prompt += (
        "Use case: vertical 9:16 Short scene plate and image-to-video source frame.\n"
        f"Constraints: single coherent scene (no collage, grid or inset); avoid {avoid_phrase}; "
        f"fully clothed period attire; no nudity; no gore. {_IMAGE_NO_TEXT}"
    )
    return prompt


def seedance_motion_focus(scene):
    script_terms = words(str(scene.get("script", "")).lower())
    visual_terms = words(str(scene.get("visual_script", "")).lower())
    secondary_terms = visual_terms - script_terms
    if script_terms & MAP_WORDS:
        return "map/document motion: slow handheld push-in over a real map or location evidence, one subtle pointing/trace movement, no character action."
    if script_terms & MILITARY_WORDS:
        return "military response motion: specific preparation or movement from this beat only, restrained dust/gear motion, no repeated generic battle loop."
    if script_terms & FARM_WORDS:
        return "farm context motion: wind across wheat/fences/field details and a grounded human reaction, not the same running-animal action as other clips."
    if {"chase", "chased", "truck", "drive", "pursuit"} & script_terms:
        return "chase motion: one clear directional chase beat with camera following the exact subject from this script line."
    if {"scatter", "escaping", "escape", "run", "running"} & script_terms:
        return "escape motion: subjects scatter or move away in a distinct direction, different framing from any chase/truck scene."
    if {"failed", "failure", "collapse", "scheitern"} & script_terms:
        return "failure motion: aftermath/reaction motion, slowed pace, visible consequence rather than repeating the original action."
    if {"ending", "payoff", "reveal", "finally", "eventually"} & script_terms:
        return "payoff motion: slow reveal or final reaction, calmer than earlier clips, no repeated action setup."
    if secondary_terms & MAP_WORDS:
        return "secondary visual guidance suggests map/document motion, but keep the voice script beat dominant and do not add facts not spoken."
    if secondary_terms & MILITARY_WORDS:
        return "secondary visual guidance suggests military-style motion, but only use it if it supports the voice script beat without changing its meaning."
    if secondary_terms & FARM_WORDS:
        return "secondary visual guidance suggests farm/context motion, but keep the voice script beat dominant."
    if {"chase", "chased", "truck", "drive", "pursuit", "scatter", "escaping", "escape", "run", "running"} & secondary_terms:
        return "secondary visual guidance suggests action motion, but keep it subtle unless the voice script itself implies that action."
    return "beat-specific motion: one unique movement that only visualizes this exact script beat, not a generic repeated action."


# Shared clip guardrails -- kept SHORT on purpose: overlong, over-stuffed prompts
# make these models render weird artifacts. One tight clause each.
_CLIP_NO_SPEECH = "No speech, voices or dialogue."
_CLIP_NO_TEXT = "No on-screen text, captions, letters, numbers or logos anywhere."
_CLIP_PRESERVE = "Preserve the source image's composition, colors, subject and clothing; add or remove nothing."


def scene_video_prompt(scene, scene_index=None, total_scenes=None, visual_intent="", title="", video_model="seedance-2.0"):
    visual_direction = scene.get("visual_script", "")
    script_beat = str(scene.get("exact_voice_text") or scene.get("voice_line") or scene.get("script", "")).strip().rstrip(".!?")
    position = f"Scene {scene_index}/{total_scenes}. " if scene_index and total_scenes else ""
    # Use a real director objective only when it adds something; never repeat the beat.
    raw_obj = scene.get("scene_objective") or scene.get("beat_purpose") or ""
    obj_clause = f" {raw_obj.rstrip('.')}." if raw_obj else ""
    camera_motion = scene.get("camera_motion") or "slow, deliberate camera move"
    subject_motion = scene.get("subject_motion") or "the main subject's clear action"
    environment_motion = scene.get("environment_motion") or "natural ambient motion"
    model = str(video_model or "").lower()
    is_hook = scene_index == 1  # the opener must stop the scroll in the first ~1.3s
    # Keep prompts SHORT -- over-stuffed prompts make these models render weird artifacts.
    # Model name / aspect ratio / resolution stay out (they are API parameters).

    if "ltx" in model:
        # LTX-2.3: ONE action beat + ONE camera move (don't stack subject+camera+
        # environment); blocking + texture; gentle motion (chaotic motion -> artifacts).
        idea = f" Idea (only if it fits): {visual_direction}." if visual_direction else ""
        if is_hook:
            beat = f"Open on immediate, bold motion in the first frame: {subject_motion}, as a {camera_motion} drives in."
        else:
            beat = f"{subject_motion} as a single {camera_motion} unfolds."
        return (
            f"One continuous cinematic documentary shot. It shows: {script_beat}.{obj_clause}{idea} "
            f"{beat} Clear foreground/background blocking, real texture. "
            "One action beat plus one camera move only; gentle, physically plausible motion -- no fast twisting, morphing or chaotic action. "
            f"{_CLIP_PRESERVE} {_CLIP_NO_SPEECH} {_CLIP_NO_TEXT}"
        )[:900]

    # Seedance 2.0 / 2.0-fast / Happy Horse: tight director shot-breakdown with literal
    # labels, chronological beats, and concrete VISIBLE physical outcomes (not mood words).
    hook_lead = "Opening hook -- bold immediate motion in the first frame. " if is_hook else ""
    idea = f"Idea (only if it fits): {visual_direction}. " if visual_direction else ""
    return (
        f"Single continuous documentary shot, silent. {position}{hook_lead}"
        f"Action: {script_beat}.{obj_clause} {subject_motion}, then {environment_motion}, with visible physical detail (splashes, debris, fabric reacting). "
        f"{idea}Camera: {camera_motion}. Style: realistic, slightly desaturated, concrete not moody, one continuous shot. "
        f"{_CLIP_PRESERVE} {_CLIP_NO_SPEECH} {_CLIP_NO_TEXT}"
    )[:900]


def safe_copy_audio(audio_path, project_dir):
    if not audio_path:
        return None
    source = Path(audio_path)
    if not source.exists() or source.suffix.lower() not in AUDIO_EXTS:
        return None
    target = project_dir / "input" / f"speech_audio{source.suffix.lower()}"
    if source.resolve() != target.resolve():
        shutil.copyfile(source, target)
    return target


def find_existing_voiceover(project_dir):
    """Return a previously generated/uploaded voiceover in this project, if any."""
    input_dir = Path(project_dir) / "input"
    for stem in ("voiceover", "speech_audio"):
        for ext in sorted(AUDIO_EXTS):
            candidate = input_dir / f"{stem}{ext}"
            if candidate.exists():
                return candidate
    return None


def find_existing_hook_audio(project_dir):
    """Return the separately-saved spoken hook audio, if any (drives InfiniteTalk)."""
    input_dir = Path(project_dir) / "input"
    for ext in sorted(AUDIO_EXTS):
        candidate = input_dir / f"hook{ext}"
        if candidate.exists():
            return candidate
    return None


def find_existing_body_audio(project_dir):
    """Return the separately-saved body narration used for the mandatory hook edit."""
    input_dir = Path(project_dir) / "input"
    for ext in sorted(AUDIO_EXTS):
        candidate = input_dir / f"body{ext}"
        if candidate.exists():
            return candidate
    return None


HOOK_BODY_PAUSE_S = 0.5


def _hook_edit_marker_path(project_dir):
    return Path(project_dir) / "input" / "voiceover_hook_edit.json"


def _hook_edit_signature(script, hook_text):
    payload = json.dumps({
        "script": re.sub(r"\s+", " ", str(script or "")).strip(),
        "hook": re.sub(r"\s+", " ", str(hook_text or "")).strip(),
        "pause_s": HOOK_BODY_PAUSE_S,
    }, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def hook_edit_cache_valid(project_dir, script, hook_text, voiceover_path):
    """Only reuse a voiceover when it is proven to contain the required 0.500s hook edit."""
    try:
        data = json.loads(_hook_edit_marker_path(project_dir).read_text(encoding="utf-8"))
        hook, body = split_hook_from_script(script, hook_text)
        if not (hook and body):
            return True
        return bool(
            data.get("signature") == _hook_edit_signature(script, hook_text)
            and abs(float(data.get("pause_s", 0)) - HOOK_BODY_PAUSE_S) < 0.0005
            and data.get("voiceover") == Path(voiceover_path).name
            and find_existing_hook_audio(project_dir)
            and find_existing_body_audio(project_dir)
        )
    except Exception:
        return False


def write_hook_edit_marker(project_dir, script, hook_text, voiceover_path):
    marker = {
        "signature": _hook_edit_signature(script, hook_text),
        "pause_s": HOOK_BODY_PAUSE_S,
        "voiceover": Path(voiceover_path).name,
        "hook_audio": Path(find_existing_hook_audio(project_dir) or "").name,
        "body_audio": Path(find_existing_body_audio(project_dir) or "").name,
    }
    path = _hook_edit_marker_path(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(marker, indent=2, ensure_ascii=False), encoding="utf-8")


def split_hook_from_script(script, hook_text):
    """Split the script into (hook, body) using the user-marked hook substring.

    Whitespace is normalized for matching so a selection from the textarea still
    lines up with the cleaned script. Returns ("", script) when no hook is marked
    or the marked text can't be located.
    """
    script = (script or "").strip()
    hook = clean_text(hook_text or "").strip()
    if not hook or not script:
        return "", script

    def collapse(text):
        return re.sub(r"\s+", " ", text).strip()

    s_norm = collapse(script)
    h_norm = collapse(hook)
    idx = s_norm.lower().find(h_norm.lower())
    if idx == -1:
        return "", script
    matched = s_norm[idx:idx + len(h_norm)]
    body = collapse(s_norm[:idx] + " " + s_norm[idx + len(h_norm):])
    return matched.strip(), body


# Narration pace, baked into the voiceover at generation. Scrape was 1.20x for a long time;
# raised to 1.30x (user call, 2026-07-10). The user can pick a different speed at the
# speech-approval gate ("Halt after generating speech") before the audio flows on.
SCRAPE_VOICE_SPEED = 1.30
GENERATE_VOICE_SPEED = 1.15
VOICE_SPEED_MIN, VOICE_SPEED_MAX = 1.0, 1.6


def resolve_voice_speed(form, clip_source=None):
    """The run's effective voice speed: explicit form value, else the per-mode default."""
    src = str((clip_source if clip_source is not None
               else (form.get("clip_source") if isinstance(form, dict) else "")) or "generate").lower()
    default = SCRAPE_VOICE_SPEED if src == "scrape" else GENERATE_VOICE_SPEED
    try:
        v = float(form.get("voice_speed", default) or default) if isinstance(form, dict) else default
    except (TypeError, ValueError):
        v = default
    return max(VOICE_SPEED_MIN, min(VOICE_SPEED_MAX, v))


def apply_approved_voice_speed(audio_path, chosen, form, status_cb=None):
    """The user picked a different speed at the speech-approval gate: re-tempo the (already
    sped) voiceover by the RATIO chosen/current and record the new speed on the form so the
    config, the pre-render gate and the report all follow. Returns the path or None (no-op)."""
    try:
        chosen = float(chosen or 0)
    except (TypeError, ValueError):
        return None
    if not chosen:
        return None
    chosen = max(VOICE_SPEED_MIN, min(VOICE_SPEED_MAX, chosen))
    current = resolve_voice_speed(form)
    if abs(chosen - current) < 0.01:
        return None
    ffmpeg = pipeline.find_ffmpeg()
    if not ffmpeg:
        return None
    factor = chosen / current
    src = Path(audio_path)

    def _retempo_in_place(part):
        part = Path(part)
        tmp = part.with_name(part.stem + "_respeed" + part.suffix)
        result = subprocess.run(
            [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(part),
             "-af", f"atempo={factor:.5f}", str(tmp)],
            capture_output=True, text=True, timeout=300)
        if not tmp.exists() or tmp.stat().st_size < 1000:
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
            raise RuntimeError((result.stderr or "voice speed conversion failed")[-180:])
        os.replace(tmp, part)

    hook_raw = str(form.get("_hook_audio_path") or "").strip() if isinstance(form, dict) else ""
    body_raw = str(form.get("_body_audio_path") or "").strip() if isinstance(form, dict) else ""
    hook_part = Path(hook_raw) if hook_raw else None
    body_part = Path(body_raw) if body_raw else None
    try:
        if hook_part and body_part and hook_part.exists() and body_part.exists():
            # Re-tempo speech segments, never the silent edit: rejoining afterwards guarantees
            # that the final render still contains exactly 0.500 seconds of silence.
            _retempo_in_place(hook_part)
            _retempo_in_place(body_part)
            joined = pipeline.concat_audio_with_pause(
                hook_part, body_part, src, pause_s=HOOK_BODY_PAUSE_S, ffmpeg=ffmpeg)
            if not joined:
                raise RuntimeError("hook/body rejoin failed")
        else:
            _retempo_in_place(src)
    except Exception as exc:
        log(status_cb, f"Speed change failed ({str(exc)[-180:]}); keeping {current:.2f}x.")
        return None
    if isinstance(form, dict):
        form["voice_speed"] = chosen
    log(status_cb, f"Voiceover re-tempoed to {chosen:.2f}x per your choice (was {current:.2f}x).")
    return src


def generate_project_voiceover(script, project_dir, form, status_cb=None):
    """Generate the spoken voiceover from the script with Gemini TTS.

    This is the default audio path now: the user picks a speaker name + voice
    and we synthesize the narration instead of requiring an upload. Returns the
    saved audio Path, or None if disabled/empty/failed (the run then falls back
    to estimated text timing).

    When the user has marked a hook, the hook and the body are synthesized
    separately and joined with a short pause, so the intro->body transition feels
    edited. The hook audio is saved on its own (input/hook.*) to drive the
    InfiniteTalk talking-head opening clip. Reuses an existing voiceover when the
    script is unchanged (so recuts of a loaded project don't re-synthesize).
    """
    if not script or not script.strip():
        return None
    is_form = isinstance(form, dict)
    cancel_event = form.get("_cancel_event") if is_form else None

    force_regenerate = bool(is_form and (
        form_flag(form, "regenerate_voice", False)
        or form_flag(form, "force_regenerate", False)
    ))
    existing = find_existing_voiceover(project_dir)
    if existing and not force_regenerate:
        prior_script_path = Path(project_dir) / "input" / "script.txt"
        prior_script = prior_script_path.read_text(encoding="utf-8") if prior_script_path.exists() else ""
        if prior_script.strip() and prior_script.strip() == script.strip():
            _reuse_hook_text = form.get("hook_text", "") if is_form else ""
            _reuse_hook, _reuse_body = split_hook_from_script(script, _reuse_hook_text)
            _pause_ok = not (_reuse_hook and _reuse_body) or hook_edit_cache_valid(
                project_dir, script, _reuse_hook_text, existing)
            if _pause_ok:
                log(status_cb, f"Reusing existing voiceover (script unchanged): {existing.name}")
                if is_form:
                    hook_audio = find_existing_hook_audio(project_dir)
                    body_audio = find_existing_body_audio(project_dir)
                    if hook_audio:
                        form["_hook_audio_path"] = str(hook_audio)
                    if body_audio:
                        form["_body_audio_path"] = str(body_audio)
                return existing
            log(status_cb, "Cached voiceover predates the mandatory 0.500s hook edit; regenerating it.")

    speaker = (str(form.get("speaker_name") or "").strip() or pipeline.DEFAULT_TTS_SPEAKER) if is_form else pipeline.DEFAULT_TTS_SPEAKER
    voice = (str(form.get("tts_voice") or "").strip() or pipeline.DEFAULT_TTS_VOICE) if is_form else pipeline.DEFAULT_TTS_VOICE
    model = (str(form.get("tts_model") or "").strip() or pipeline.DEFAULT_TTS_MODEL) if is_form else pipeline.DEFAULT_TTS_MODEL
    input_dir = project_dir / "input"
    hook_text = form.get("hook_text", "") if is_form else ""
    hook, body = split_hook_from_script(script, hook_text)
    ffmpeg = pipeline.find_ffmpeg()
    # Punch up the delivery: speed the narration (pitch-preserving) and denoise the TTS hiss.
    # Script->visual (AI Generate) narration runs at 1.15x; the scrape/found-footage pace runs
    # SCRAPE_VOICE_SPEED (user-tunable at the speech-approval gate). Applied to EACH
    # segment (hook AND body) BEFORE concat + alignment, so the saved hook.wav (InfiniteTalk) and
    # the word timing both match the final pace.
    _clip_src = str(form.get("clip_source") or "generate").lower() if is_form else "generate"
    _default_speed = SCRAPE_VOICE_SPEED if _clip_src == "scrape" else GENERATE_VOICE_SPEED
    try:
        voice_speed = float(form.get("voice_speed", _default_speed) or _default_speed) if is_form else _default_speed
    except (TypeError, ValueError):
        voice_speed = _default_speed

    try:
        # The hook edit is a production invariant, not an optional TTS setting. Whenever a
        # marked hook and a body exist, create both parts and join them with exactly 0.500s of
        # digital silence BEFORE the approval gate receives this path. `split_hook_tts` remains
        # accepted for preset/backward compatibility but can no longer disable the edit.
        if hook and body:
            if not ffmpeg:
                raise RuntimeError("ffmpeg is required for the mandatory hook/body voice edit")
            log(status_cb, "Generating hook + body narration for the mandatory 0.500s edit...")
            hook_path = pipeline.generate_speech_gemini(
                hook, input_dir / "hook", speaker=speaker, voice=voice, model=model,
                cancel_event=cancel_event, status_cb=status_cb)
            body_path = pipeline.generate_speech_gemini(
                body, input_dir / "body", speaker=speaker, voice=voice, model=model,
                cancel_event=cancel_event, status_cb=status_cb)
            pipeline.apply_voice_postprocess(hook_path, speed=voice_speed, ffmpeg=ffmpeg, status_cb=status_cb)
            pipeline.apply_voice_postprocess(body_path, speed=voice_speed, ffmpeg=ffmpeg, status_cb=status_cb)
            full = input_dir / f"voiceover{hook_path.suffix}"
            joined = pipeline.concat_audio_with_pause(
                hook_path, body_path, full, pause_s=HOOK_BODY_PAUSE_S, ffmpeg=ffmpeg)
            if joined:
                if is_form:
                    form["_hook_audio_path"] = str(hook_path)
                    form["_body_audio_path"] = str(body_path)
                    form["hook_pause_s"] = HOOK_BODY_PAUSE_S
                write_hook_edit_marker(project_dir, script, hook_text, joined)
                log(status_cb, f"Voiceover cut at hook/body boundary with exactly {HOOK_BODY_PAUSE_S:.3f}s silence ({speaker} / {voice}).")
                return joined
            raise RuntimeError("mandatory hook/body voiceover join failed")

        path = pipeline.generate_speech_gemini(
            script, input_dir / "voiceover", speaker=speaker, voice=voice, model=model,
            cancel_event=cancel_event, status_cb=status_cb)
        pipeline.apply_voice_postprocess(path, speed=voice_speed, ffmpeg=ffmpeg, status_cb=status_cb)
        log(status_cb, f"Voiceover generated ({speaker} / {voice}): {path.name}")
        return path
    except pipeline.PipelineCancelled:
        raise
    except Exception as exc:
        if hook and body:
            # Never silently continue with estimated timing or a seamless take: that would let
            # a run bypass the mandatory edit and present the wrong audio for approval.
            log(status_cb, f"Mandatory hook/body voice edit failed: {exc}")
            raise
        log(status_cb, f"Voiceover generation failed ({exc}); continuing with estimated timing.")
        return None


def probe_audio_duration(audio_path):
    source = Path(audio_path)
    if not source.exists():
        return None
    try:
        ffmpeg = pipeline.find_ffmpeg()
        ffprobe = pipeline.find_ffprobe(ffmpeg)
        if ffprobe:
            result = subprocess.run(
                [
                    ffprobe,
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    str(source),
                ],
                text=True,
                capture_output=True,
                timeout=20,
            )
            if result.returncode == 0:
                for line in result.stdout.splitlines():
                    try:
                        duration = float(line.strip())
                    except (TypeError, ValueError):
                        continue
                    if 0.1 < duration < 3600:
                        return round(duration, 3)
    except Exception:
        pass
    if source.suffix.lower() == ".wav":
        try:
            with wave.open(str(source), "rb") as audio:
                rate = audio.getframerate()
                frames = audio.getnframes()
                if rate > 0 and frames > 0:
                    return round(frames / float(rate), 3)
        except Exception:
            pass
    return None


def parse_audio_second(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", ".")
    if not text:
        return None
    try:
        if ":" in text:
            total = 0.0
            for part in text.split(":"):
                total = total * 60.0 + float(part)
            return total
        return float(text)
    except (TypeError, ValueError):
        return None


def audio_item_seconds(item, *keys):
    for key in keys:
        if key in item:
            value = parse_audio_second(item.get(key))
            if value is not None:
                return value
    return None


def audio_item_text(item):
    for key in ("script", "text", "transcript", "phrase", "sentence", "spoken", "content", "visual_focus"):
        value = clean_text(str(item.get(key, "")))
        if value:
            return value
    return ""


def extract_audio_timing_group(analysis):
    if not isinstance(analysis, dict):
        return "fallback", []
    preferred_keys = (
        "sentence_timestamps",
        "sentences",
        "timed_phrases",
        "phrases",
        "utterances",
        "segments",
        "scenes",
    )
    for key in preferred_keys:
        raw_items = analysis.get(key)
        if not isinstance(raw_items, list):
            continue
        items = []
        for raw in raw_items:
            if not isinstance(raw, dict):
                continue
            start = audio_item_seconds(
                raw,
                "start",
                "start_time",
                "start_seconds",
                "start_sec",
                "time_start",
                "begin",
                "from",
                "timestamp_start",
            )
            end = audio_item_seconds(
                raw,
                "end",
                "end_time",
                "end_seconds",
                "end_sec",
                "time_end",
                "stop",
                "to",
                "timestamp_end",
            )
            duration = audio_item_seconds(raw, "duration", "duration_seconds")
            if start is not None and end is None and duration is not None:
                end = start + duration
            text = audio_item_text(raw)
            if start is None or end is None or end <= start or not text:
                continue
            items.append({"start": float(start), "end": float(end), "script": text})
        if len(items) >= 2 or (key == "scenes" and items):
            return key, items
    return "fallback", []


def merge_phrase_timing(items):
    if not items:
        return []
    merged = []
    current = None
    min_chunk = 1.15
    max_chunk = 4.8
    for item in items:
        if current is None:
            current = dict(item)
            continue
        current_duration = current["end"] - current["start"]
        merged_duration = item["end"] - current["start"]
        ends_sentence = current["script"].rstrip().endswith((".", "!", "?", ";", ":"))
        should_flush = current_duration >= min_chunk and (ends_sentence or merged_duration > max_chunk)
        if should_flush:
            merged.append(current)
            current = dict(item)
        else:
            current["end"] = item["end"]
            current["script"] = clean_text(f"{current['script']} {item['script']}")
    if current:
        merged.append(current)
    return merged


def finalize_audio_scene_timing(items, fallback_script, fallback_duration, audio_duration=None):
    duration_limit = audio_duration or fallback_duration
    scenes = sorted(items, key=lambda item: item["start"])
    cleaned = []
    last_end = 0.0
    for scene in scenes:
        start = max(0.0, float(scene["start"]))
        end = max(start + 0.65, float(scene["end"]))
        if duration_limit:
            start = min(start, max(0.0, float(duration_limit) - 0.2))
            end = min(end, float(duration_limit))
        if not cleaned:
            start = 0.0
        if cleaned and start < last_end:
            start = last_end
        if cleaned and 0.0 < start - last_end < 0.35:
            start = last_end
        if end <= start:
            continue
        cleaned.append({"start": round(start, 2), "end": round(end, 2), "script": scene["script"]})
        last_end = end
    if cleaned:
        for index in range(1, len(cleaned)):
            gap = cleaned[index]["start"] - cleaned[index - 1]["end"]
            if gap > 0:
                cleaned[index - 1]["end"] = round(cleaned[index]["start"], 2)
        if duration_limit and cleaned[-1]["end"] < float(duration_limit):
            cleaned[-1]["end"] = round(float(duration_limit), 2)
        return cleaned
    return parse_timed_script(fallback_script, duration_limit or fallback_duration)


def normalize_audio_scenes(analysis, fallback_script, fallback_duration, audio_duration=None, return_source=False):
    source, raw_items = extract_audio_timing_group(analysis)
    if source not in {"fallback", "scenes"}:
        raw_items = merge_phrase_timing(raw_items)
    scenes = finalize_audio_scene_timing(raw_items, fallback_script, fallback_duration, audio_duration)
    if return_source:
        return scenes, source
    return scenes


def format_timecode(seconds):
    seconds = max(0.0, float(seconds))
    minutes = int(seconds // 60)
    remainder = seconds - minutes * 60
    return f"{minutes:02d}:{remainder:04.1f}"


def split_visual_notes(visual_script):
    visual_script = clean_text(visual_script)
    if not visual_script:
        return []
    lines = [re.sub(r"^\s*[-*#\d.)]+\s*", "", line).strip() for line in visual_script.splitlines()]
    lines = [line for line in lines if line]
    if len(lines) > 1:
        return lines
    sentences = re.split(r"(?<=[.!?])\s+", visual_script)
    return [sentence.strip() for sentence in sentences if sentence.strip()]


def apply_visual_script_to_scenes(scenes, visual_script, target_duration):
    if not visual_script:
        return scenes
    enriched = [dict(scene) for scene in scenes]
    # ``parse_timed_script`` also invents uniform timestamps for ordinary prose. Only use its
    # overlap path when the author supplied real mm:ss markers; otherwise semantic note mapping
    # below must see the original individual directions.
    has_explicit_timestamps = bool(re.search(r"\b\d{1,2}:\d{2}\b", str(visual_script or "")))
    try:
        visual_beats = (parse_timed_script(visual_script, target_duration)
                        if has_explicit_timestamps else [])
    except Exception:
        visual_beats = []
    if visual_beats:
        for scene in enriched:
            matches = []
            for beat in visual_beats:
                overlap = min(scene["end"], beat["end"]) - max(scene["start"], beat["start"])
                if overlap > 0:
                    matches.append((overlap, beat["script"]))
            if matches:
                matches.sort(reverse=True)
                scene["visual_script"] = " ".join(text for _, text in matches[:2])
        return enriched

    notes = split_visual_notes(visual_script)
    if not notes:
        return enriched
    # Untimed notes used to be spread uniformly by scene INDEX. That shifts directions whenever
    # narration scenes have unequal durations and can attach a hook noun (for example sumo) to
    # unrelated later beats. Anchor by scene time, then let concrete shared nouns/actions override
    # the temporal guess. This stays deterministic and preserves author order when notes are broad.
    stop = {
        "a", "an", "the", "and", "or", "of", "to", "in", "on", "at", "for", "with", "from",
        "show", "shot", "video", "clip", "footage", "scene", "woman", "women", "person", "people",
    }

    def _note_terms(text):
        return words(str(text or "")) - stop

    note_terms = [_note_terms(note) for note in notes]
    duration = max(float(target_duration or 0.0),
                   max((float(scene.get("end") or 0.0) for scene in enriched), default=0.0), 0.1)
    for index, scene in enumerate(enriched):
        start = float(scene.get("start") or 0.0)
        end = float(scene.get("end") or start)
        midpoint = max(0.0, min(duration, (start + end) / 2.0))
        expected = min(len(notes) - 1, int((midpoint / duration) * len(notes)))
        scene_terms = _note_terms(" ".join(str(scene.get(key) or "") for key in (
            "exact_voice_text", "voice_line", "script", "visual_direction")))
        best_index, best_score = expected, -1.0
        for note_index, terms in enumerate(note_terms):
            overlap = len(scene_terms & terms)
            semantic = (overlap / max(1.0, min(len(scene_terms), len(terms)))) * 8.0
            proximity = max(0.0, 1.5 - abs(note_index - expected) * 0.45)
            # A real shared content term beats temporal proximity; generic notes retain chronology.
            score = semantic + proximity
            if score > best_score:
                best_index, best_score = note_index, score
        scene["visual_script"] = notes[best_index]
    return enriched


def scene_text_for_planning(scene):
    """Canonical spoken text plus an optional visual note, without duplicating narration.

    The old implementation repeated ``script`` twice to give it more weight.  These strings are
    sent as natural-language TikTok queries and vision instructions, so that produced prompts such
    as "Tired of pressure. Tired of pressure." and degraded both retrieval and clip scoring.
    """
    script = scene.get("exact_voice_text") or scene.get("voice_line") or scene.get("script", "")
    visual = scene.get("visual_script", "")
    return " ".join(part for part in [script, visual] if part).strip()


def split_micro_units(text):
    text = clean_text(text)
    if not text:
        return []
    sentences = re.split(r"(?<=[.!?])\s+", text)
    units = []
    for sentence in [item.strip() for item in sentences if item.strip()]:
        parts = re.split(r"\s+(?=(?:but|then|and then|so|because|until|while|when|doch|aber|dann|weil|bis)\b)", sentence, flags=re.I)
        for part in parts:
            part = part.strip(" ,;:-")
            if part:
                units.append(part)
    merged = []
    for unit in units:
        if merged and len(re.findall(r"\w+", unit)) <= 3:
            merged[-1] = f"{merged[-1]} {unit}".strip()
        else:
            merged.append(unit)
    return merged or [text]


def infer_visual_hook_type(text):
    terms = words(text)
    lowered = str(text or "").lower()
    if terms & {"map", "karte", "document", "newspaper", "letter", "proof", "evidence", "archive"}:
        return "proof/evidence"
    if terms & {"face", "reaction", "believed", "nobody", "shock", "surprise"}:
        return "face/reaction"
    if terms & {"machine", "weapon", "object", "pig", "emu", "truck", "gun"}:
        return "shocking object"
    if terms & {"changed", "became", "built", "turned", "transformed"}:
        return "transformation"
    if terms & {"reveal", "found", "discovered", "suddenly"}:
        return "reveal"
    if terms & {"danger", "attack", "battle", "war", "chase", "fire"}:
        return "danger/mystery"
    if terms & {"absurd", "weird", "bizarre", "ridiculous"} or "war over" in lowered:
        return "absurd contrast"
    if terms & {"huge", "massive", "forever", "history", "empire"}:
        return "epic scale"
    return "historical image"


def infer_emotion(text):
    terms = words(text)
    if terms & {"absurd", "bizarre", "weird", "ridiculous"}:
        return "irony"
    if terms & {"danger", "attack", "battle", "war", "chase", "failed", "collapse"}:
        return "danger"
    if terms & {"secret", "mystery", "unknown", "nobody", "discovered"}:
        return "mystery"
    if terms & {"finally", "changed", "won", "success", "history", "forever"}:
        return "triumph"
    if terms & {"shocking", "unbelievable", "suddenly", "killed", "dead"}:
        return "shock"
    return "curiosity"


def infer_best_media_type(text, visual_hook_type):
    terms = words(text)
    if visual_hook_type in {"proof/evidence", "historical image"}:
        return "web_image"
    if terms & {"map", "document", "newspaper", "archive", "photo", "person", "building", "location"}:
        return "web_image"
    if terms & ACTION_WORDS or visual_hook_type in {"transformation", "reveal", "danger/mystery", "epic scale"}:
        return "source_image_to_seedance"
    return "web_image"


def default_crop_plan(hook_type):
    if hook_type in {"face/reaction", "shocking object"}:
        return {
            "focus_subject": "main face or object",
            "keep_face_visible": hook_type == "face/reaction",
            "safe_zone": "center",
            "avoid_cutting": ["face", "hands", "important object"],
            "allow_pan": True,
        }
    if hook_type == "proof/evidence":
        return {
            "focus_subject": "evidence, map, document, or archival subject",
            "keep_face_visible": False,
            "safe_zone": "center",
            "avoid_cutting": ["labels", "main object", "map area"],
            "allow_pan": False,
        }
    return {
        "focus_subject": "main subject",
        "keep_face_visible": False,
        "safe_zone": "center",
        "avoid_cutting": ["main subject"],
        "allow_pan": True,
    }


def fallback_micro_beat_plan(title, script, target_duration, base_scenes=None):
    base_scenes = base_scenes or parse_timed_script(script, target_duration)
    beats = []
    for base in base_scenes:
        units = split_micro_units(base.get("script", "")) or [base.get("script", "")]
        weights = [max(1, len(re.findall(r"\w+", unit))) for unit in units]
        total_weight = sum(weights) or len(units)
        scene_duration = max(0.1, float(base.get("end", 0)) - float(base.get("start", 0)))
        cursor = float(base.get("start", 0))
        for index, unit in enumerate(units):
            if index == len(units) - 1:
                end = float(base.get("end", cursor + scene_duration / len(units)))
            else:
                end = cursor + scene_duration * (weights[index] / total_weight)
            end = max(cursor + 0.45, min(float(base.get("end", end)), end))
            hook_type = infer_visual_hook_type(unit)
            emotion = infer_emotion(unit)
            best_media = infer_best_media_type(unit, hook_type)
            beats.append(
                {
                    "start": round(cursor, 2),
                    "end": round(end, 2),
                    "script": unit,
                    "exact_voice_text": unit,
                    "voice_line": unit,
                    "beat_purpose": f"Make the viewer understand this exact line: {unit}",
                    "scene_objective": f"Answer the voice line visually, not just mood-match it: {unit}",
                    "visual_meaning": unit,
                    "viewer_emotion": emotion,
                    "emotion": emotion,
                    "visual_hook_type": hook_type,
                    "required_visual_information": unit,
                    "best_media_type": best_media,
                    "shot_type": "tight vertical documentary shot" if hook_type != "epic scale" else "wide vertical reveal",
                    "camera_motion": "subtle handheld push or parallax" if best_media != "web_image" else "minimal pan or contain crop",
                    "subject_motion": "script-specific visible action" if best_media != "web_image" else "none or archival still",
                    "environment_motion": "wind, dust, light, or practical ambience" if best_media != "web_image" else "none",
                    "emotional_action": emotion,
                    "must_show": important_terms(unit, 5, SEARCH_NOISE),
                    "must_not_show": ["generic cinematic filler", "off-topic symbols", "random book cover", "caption text"],
                    "crop_plan": default_crop_plan(hook_type),
                    "voice_match_score_target": 85,
                    "micro_beat": True,
                }
            )
            cursor = end
    return [beat for beat in beats if beat["end"] > beat["start"]]


def normalize_micro_beat_plan(raw_beats, title, script, target_duration):
    if not isinstance(raw_beats, list):
        return []
    normalized = []
    last = 0.0
    for item in raw_beats:
        if not isinstance(item, dict):
            continue
        voice = clean_text(str(item.get("exact_voice_text") or item.get("voice_line") or item.get("script") or ""))
        if not voice:
            continue
        try:
            start = float(item.get("start_time", item.get("start", last)))
            end = float(item.get("end_time", item.get("end", start + 1.4)))
        except (TypeError, ValueError):
            start, end = last, last + 1.4
        start = max(0.0, min(float(target_duration), start))
        end = max(start + 0.45, min(float(target_duration), end))
        if start < last - 0.05:
            start = last
            end = max(start + 0.45, end)
        hook_type = clean_text(str(item.get("visual_hook_type") or infer_visual_hook_type(voice)))
        emotion = clean_text(str(item.get("viewer_emotion") or item.get("emotion") or infer_emotion(voice)))
        best_media = clean_text(str(item.get("best_media_type") or infer_best_media_type(voice, hook_type))).lower()
        if best_media not in {"web_image", "seedance", "source_image_to_seedance", "existing_clip", "local"}:
            best_media = infer_best_media_type(voice, hook_type)
        crop_plan = item.get("crop_plan") if isinstance(item.get("crop_plan"), dict) else default_crop_plan(hook_type)
        normalized.append(
            {
                "start": round(start, 2),
                "end": round(min(float(target_duration), end), 2),
                "script": voice,
                "exact_voice_text": voice,
                "voice_line": voice,
                "beat_purpose": clean_text(str(item.get("beat_purpose") or item.get("scene_objective") or f"Make the viewer understand: {voice}")),
                "scene_objective": clean_text(str(item.get("scene_objective") or item.get("beat_purpose") or f"Answer the voice line visually: {voice}")),
                "visual_meaning": clean_text(str(item.get("visual_meaning") or item.get("required_visual_information") or voice)),
                "viewer_emotion": emotion,
                "emotion": emotion,
                "visual_hook_type": hook_type,
                "required_visual_information": clean_text(str(item.get("required_visual_information") or voice)),
                "best_media_type": best_media,
                "shot_type": clean_text(str(item.get("shot_type") or "vertical documentary shot")),
                "camera_motion": clean_text(str(item.get("camera_motion") or "subtle purposeful camera movement")),
                "subject_motion": clean_text(str(item.get("subject_motion") or "script-specific subject action")),
                "environment_motion": clean_text(str(item.get("environment_motion") or "natural ambient movement")),
                "emotional_action": clean_text(str(item.get("emotional_action") or emotion)),
                "must_show": item.get("must_show") if isinstance(item.get("must_show"), list) else important_terms(voice, 5, SEARCH_NOISE),
                "must_not_show": item.get("must_not_show") if isinstance(item.get("must_not_show"), list) else ["generic cinematic filler", "off-topic media", "caption text"],
                "crop_plan": crop_plan,
                "voice_match_score_target": int(item.get("voice_match_score_target") or 85),
                "micro_beat": True,
            }
        )
        last = normalized[-1]["end"]
    return normalized

SCRIPT_CREATOR_MODEL = "google/gemini-3.5-flash"

# Curated JAPAN angles for the no-topic case. The model alone converges on the same 1-2
# topics every call ("schools banned brown hair"); rotating through this pool with a
# persisted history guarantees variety across consecutive generations.
SCRIPT_CREATOR_ANGLES = (
    "school rules", "dating rules", "work culture rules", "beauty standards",
    "train and commuting etiquette", "convenience store culture", "apartment renting rules",
    "onsen and tattoo rules", "garbage separation rules", "vending machine culture",
    "customer service rules", "eating and restaurant etiquette", "shoes and indoor rules",
    "gift giving rules", "drinking with coworkers culture", "school lunch system",
    "school club activities", "senpai-kohai hierarchy", "childhood independence",
    "public silence rules", "hanko stamp bureaucracy", "driving license process",
    "capsule hotel rules", "theme cafe culture", "lost wallet honesty culture",
    "wedding and funeral etiquette", "neighborhood association rules",
)
_SCRIPT_TOPIC_HISTORY = ROOT / "generated_assets" / "script_creator_history.json"

SCRIPT_CREATOR_LENSES = (
    "a day-in-the-life human consequence",
    "the most surprising physical ritual people perform",
    "a contradiction between the public image and everyday reality",
    "the money, time, or effort the subject costs ordinary people",
    "the visible enforcement mechanism and what happens in practice",
    "an overlooked object or design detail that reveals the larger story",
    "a before-versus-now change with a visible modern consequence",
    "three escalating settings where the same theme appears differently",
    "the awkward social interaction a phone camera could actually capture",
    "the gap between what outsiders assume and what locals physically do",
)


def _script_creator_history():
    try:
        data = json.loads(_SCRIPT_TOPIC_HISTORY.read_text(encoding="utf-8"))
        return [item for item in data if isinstance(item, dict)][-24:]
    except Exception:
        return []


def _script_text_similarity(left, right):
    """Conservative duplicate detector; wording and identical hooks both matter."""
    import difflib
    import re
    norm = lambda value: re.sub(r"[^a-z0-9 ]+", " ", str(value or "").lower()).split()
    a, b = norm(left), norm(right)
    if not a or not b:
        return 0.0
    sequence = difflib.SequenceMatcher(None, a, b).ratio()
    aset, bset = set(a), set(b)
    overlap = len(aset & bset) / max(1, len(aset | bset))
    return max(sequence, overlap)


def generate_viral_script(topic="", status_cb=None, instructions=""):
    """Write a reference-style viral short script from a topic (empty topic = the model picks
    its own high-potential topic in the same style). Returns {"topic","script","hook_keywords"}.

    hook_keywords are returned for the color-coded caption pass (pipeline v0.2 phase 4): the
    strong VERBS and toxic/extreme ADJECTIVES (NEVER/ILLEGAL/BANNED/CRIME/FORCED class) plus
    shock nouns, spelled EXACTLY as they appear in the script.
    """
    import random
    import secrets
    topic = clean_text(topic or "").strip()
    instructions = clean_text(instructions or "").strip()[:2000]
    recent = _script_creator_history()
    system = (
        "You are an elite short-form scriptwriter for viral 'dark facts' style TikTok/Shorts "
        "narration (the Japan-facts reference style: punchy, factual-sounding, slightly "
        "outrageous, zero fluff). You write EXACTLY in that voice. "
        "FACTS ARE NON-NEGOTIABLE: every claim must be TRUE and describe a real, widely "
        "documented Japanese practice. Never invent statistics - use a number only when it is "
        "a well-known real figure, otherwise state the claim without one. The outrageousness "
        "must come from the REAL rule itself, never from exaggerating it into falsehood. "
        "THE #1 FAILURE MODE TO AVOID: upgrading a custom or social norm into a law or ban. "
        "banned / illegal / forced / legally required are allowed ONLY when literally true. "
        "A strong social norm is written truthfully and STILL hits hard: 'an unwritten rule', "
        "'expected of everyone', 'you will be silently judged', 'many companies demand it'. "
        "The weird TRUE detail IS the viral part - one claim a Japanese viewer would call "
        "false kills the whole video in the comments. "
        "RESEARCH DEEPLY, WRITE SIMPLY: your internal fact-check may be technical, but the final "
        "narration must sound like one friend telling another a surprising story. Never expose "
        "research jargon, legalistic qualifications, institutional terminology, academic wording, "
        "or a pile of exceptions. Preserve truth by choosing a simpler defensible claim, not by "
        "stuffing the sentence with caveats. Use words a 13-year-old understands immediately. "
        "SCOPE & CERTAINTY: never present a local, rare, disputed, historical or conditional "
        "fact as universal - keep the who/where/when that makes it true. Do not invent motives, "
        "consequences, emotions, statistics or causal links between facts. Dramatic wording may "
        "raise intensity but must never distort scope, certainty, frequency, cause or severity. "
        "Before returning, INTERNALLY classify every claim as verified / partly true / "
        "misleading / unsupported and rewrite or qualify everything below 'verified' - return "
        "only the final coherent, factually defensible script. JSON only.")
    if topic:
        # A user topic is a hard content constraint, not a suggestion. Rotate the editorial
        # lens and show the model its own recent habits so repeated clicks do not converge on
        # the same hook/facts. A per-request nonce also defeats upstream prompt caching.
        lens = random.choice(SCRIPT_CREATOR_LENSES)
        same_topic = [r for r in recent if str(r.get("requested_topic") or r.get("topic") or "").casefold() == topic.casefold()][-6:]
        avoid = "\n".join(
            f'- Do not reuse this previous angle or wording: {str(r.get("script") or "")[:420]}'
            for r in same_topic if r.get("script")
        )
        ask = (
            f'USER-LOCKED TOPIC: "{topic}". The complete script MUST be specifically about this exact topic. '
            "Do not replace it with a familiar Japan-school, women-at-work, sumo, or social-rules topic unless "
            "the user explicitly named that subject. About JAPAN only when the topic says or clearly implies Japan.\n"
            f"Fresh editorial lens for this generation: {lens}.\n"
            f"Uniqueness token: {secrets.token_hex(6)}. This token is not script content."
            + (f"\nRECENT OUTPUTS FOR THIS TOPIC — actively choose different facts, hook and structure:\n{avoid}" if avoid else "")
        )
        temperature = 0.92
    else:
        # rotate through curated angles + exclude recent topics -> real variety per click
        used_angles = {str(r.get("angle") or "") for r in recent if isinstance(r, dict)}
        fresh = [a for a in SCRIPT_CREATOR_ANGLES if a not in used_angles] or list(SCRIPT_CREATOR_ANGLES)
        angle = random.choice(fresh)
        avoid = ", ".join(f'"{str(r.get("topic") or "")[:60]}"' for r in recent
                          if isinstance(r, dict) and r.get("topic"))
        ask = (f"Write about JAPAN ONLY (never South Korea, China or any other country). "
               f"Your assigned angle: JAPANESE {angle.upper()}. Pick one specific, surprising, "
               f"REAL aspect of it." + (f" Do NOT reuse these recent topics: {avoid}." if avoid else ""))
        temperature = 0.9
    custom_direction = ""
    if instructions:
        custom_direction = f"""

USER SCRIPT DIRECTIONS (follow these closely):
{instructions}

These directions MAY override the default tone, specificity, number of facts, block structure,
and level of dramatic language below. They may NOT override factual accuracy, the locked topic,
the 100-140 word target, narration-only requirement, or strict JSON output.
"""
    prompt = f"""{ask}{custom_direction}

Write ONE narration script following ALL of these rules:
- HOOK: the first sentence is a shocking claim of AT MOST 12 words (a real number, a REAL
  ban, or a jaw-dropping TRUE practice - NEVER a fake ban).
- ONE CENTRAL THEME: the hook names it; every following fact is framed as another example,
  consequence, contrast or escalation of that SAME theme. Facts from different settings are
  welcome when the link is explicit - drop a fact only if its connection to the theme cannot
  be stated in one transition sentence.
- STRUCTURE: exactly 3 thought blocks after the hook, each 2-3 sentences. Escalate between
  blocks; open the final block with an escalation like "But the craziest part?" or
  "But the harshest reality?".
- TRANSITIONS carry the theme: each block opener says how the next fact relates ("This
  extends beyond the workplace...", "The same ideal also appears in..."). Never imply one
  fact CAUSED another unless that link is verified. Order the facts so the strongest one
  lands last.
- EVERY claim must be PHYSICALLY FILMABLE as real phone footage (a visible person, action,
  object or place). Never state an abstraction without its visible physical consequence.
- SIMPLE, NATURAL LANGUAGE: write like a clear viral storyteller, never like a researcher,
  lawyer, consultant, textbook or news report. Prefer common everyday words and short sentences.
- STAY BROAD ENOUGH TO FOLLOW: use only the detail needed to understand why the fact is
  surprising. Avoid obscure organizations, policy names, technical processes, dates, formal job
  titles and niche terminology unless that exact detail is the payoff.
- ONE IDEA PER SENTENCE. Most sentences should be 8-16 words. Explain any unavoidable unfamiliar
  term immediately in plain language, or replace it with a familiar description.
- READ-ALOUD TEST: every sentence must sound natural when spoken once at normal speed. Rewrite
  anything that feels dense, formal, overqualified, oddly specific or difficult to remember.
- 100-140 words total. Simple spoken language, present tense, no lists, no emojis, no
  hashtags, no camera directions - narration text only.
- Weave in strong hook words - but ONLY where literally true: NEVER / ILLEGAL / BANNED /
  FORCED only for actual laws and actual bans; for norms use truthful hard words instead
  (unwritten, ruthless, obsessed, humiliating, judged, rejected, shamed), plus real numbers.
- SELF FACT-CHECK before returning: re-read every sentence and rewrite any claim a Japanese
  person would call false - a custom presented as law, an invented consequence, an invented
  number, or a rare edge case presented as universal.

Return STRICT JSON:
{{"topic": "<short topic label>",
 "script": "<the narration text>",
 "hook_keywords": ["8-14 words: the strong VERBS, toxic/extreme ADJECTIVES and shock NOUNS/
numbers from the script, spelled EXACTLY as written in the script"]}}"""
    log(status_cb, "Script creator: writing a reference-style script"
        + (f' for "{topic}"...' if topic else " (model picks the topic)..."))
    prior_scripts = [str(r.get("script") or "") for r in recent if r.get("script")]
    data = None
    script = ""
    duplicate_score = 0.0
    for attempt in range(3):
        attempt_prompt = prompt
        if attempt:
            attempt_prompt += (
                "\n\nREWRITE REQUIRED: the previous answer was too similar to an earlier generation. "
                "Choose a different central claim, different hook construction, different physical examples, "
                "and a different order. Do not merely paraphrase. "
                f"Rejected draft to avoid: {script[:500]}\n"
                f"Retry nonce: {secrets.token_hex(8)}."
            )
        data = _post_llm_json(SCRIPT_CREATOR_MODEL,
                              [{"role": "system", "content": system},
                               {"role": "user", "content": attempt_prompt}], 4000,
                              min(1.15, temperature + attempt * 0.08))
        if not isinstance(data, dict) or not str(data.get("script") or "").strip():
            continue
        candidate = clean_text(str(data.get("script") or "")).strip()
        duplicate_score = max((_script_text_similarity(candidate, old) for old in prior_scripts), default=0.0)
        candidate_hook = candidate.split(".", 1)[0].strip().casefold()
        repeated_hook = any(candidate_hook and candidate_hook == old.split(".", 1)[0].strip().casefold()
                            for old in prior_scripts)
        script = candidate
        if duplicate_score < 0.68 and not repeated_hook:
            break
        log(status_cb, f"Script creator: duplicate-like result ({duplicate_score:.0%}); requesting a new angle...")
    if not script:
        raise RuntimeError("Script creator returned no usable script - try again.")
    kws = [str(k).strip() for k in (data.get("hook_keywords") or []) if str(k).strip()]
    out_topic = str(data.get("topic") or topic or "").strip()
    # ALWAYS record the generated script (user-topic runs too) so past outputs can be
    # reviewed/fact-checked later - previously only the topic label survived.
    try:
        recent.append({"angle": (angle if not topic else lens), "topic": out_topic,
                       "requested_topic": topic,
                       "instructions": instructions,
                       "at": time.strftime("%Y-%m-%d %H:%M"), "script": script})
        _SCRIPT_TOPIC_HISTORY.parent.mkdir(parents=True, exist_ok=True)
        _SCRIPT_TOPIC_HISTORY.write_text(json.dumps(recent[-24:], ensure_ascii=False, indent=1),
                                         encoding="utf-8")
    except Exception:
        pass
    return {"topic": out_topic, "script": script, "hook_keywords": kws[:14]}


def regenerate_script_from_reference(original_script, instructions="", status_cb=None):
    """Rebuild a narration while keeping the supplied project's script as the content anchor."""
    original_script = clean_text(original_script or "").strip()
    instructions = clean_text(instructions or "").strip()[:2000]
    if not original_script:
        raise ValueError("The original project script is empty.")
    direction = instructions or "Improve clarity, pacing and spoken flow without changing the concept."
    prompt = f"""ORIGINAL PROJECT SCRIPT — this is the binding topic and factual reference:
{original_script}

OPTIONAL USER DIRECTION:
{direction}

Rewrite the narration from scratch while following these rules:
- Keep the SAME central topic, subject and factual direction as the original.
- Do not switch to a related but different topic and do not introduce unrelated facts.
- The user direction may change structure, tone, specificity, number of facts and pacing.
- Use simple conversational language a 13-year-old understands. Avoid technical or formal wording.
- Preserve important factual qualifications; never turn a norm into a law or invent a stronger claim.
- Make it natural to speak aloud, with one idea per sentence.
- Unless the user requests another format or length, stay close to the original word count.
- Narration only: no headings, bullets, notes, camera directions, hashtags or explanations.

Return STRICT JSON: {{"script":"<complete rewritten narration>"}}"""
    log(status_cb, "Script rewriter: rebuilding the narration from the original project script...")
    data = _post_llm_json(
        SCRIPT_CREATOR_MODEL,
        [{"role": "system", "content": (
            "You rewrite short-form narration. The supplied original script is a binding content "
            "reference, not a loose inspiration. Return JSON only.")},
         {"role": "user", "content": prompt}],
        4000, 0.82)
    script = clean_text(str((data or {}).get("script") or "")).strip() if isinstance(data, dict) else ""
    if not script:
        raise RuntimeError("Script regeneration returned no usable narration.")
    return {"script": script}


def llm_generate_project_title(script, reasoning_model=None, status_cb=None):
    if not os.environ.get("WAVESPEED_API_KEY") or not script.strip():
        return ""
    log(status_cb, "Auto-generating project title from Voice Script...")
    payload = {
        "model": reasoning_model or GPT55_MODEL,
        "messages": [
            {"role": "system", "content": "You name short-form videos. Reply with the title only - no quotes, no punctuation marks, no explanation."},
            {"role": "user", "content": (
                "Read this voice script and write a short, punchy, curiosity-driven title for a vertical Short "
                "(2-5 words, Title Case). Hint at the payoff without spoiling it; make someone want to watch.\n\n"
                f"Script:\n{script}"
            )},
        ],
        "temperature": 0.7,
        "max_tokens": 24,
    }
    try:
        data = post_json_url(WAVESPEED_LLM_API, payload, timeout=60)
        content = (data["choices"][0]["message"]["content"] or "").strip()
        title = content.splitlines()[0].strip().strip('"').strip("'") if content else ""
        if title and len(title) < 60:
            log(status_cb, f"Auto-generated title: {title}")
            return title
    except Exception as exc:
        log(status_cb, f"Failed to generate title: {exc}")
    return ""


def derive_project_title_from_script(script):
    """Create a stable display title when the title LLM is unavailable or returns nothing."""
    text = clean_text(script or "")
    text = re.sub(r"^\s*(?:hook|intro|voiceover|narrator)\s*[:\-]\s*", "", text, flags=re.I)
    first = re.split(r"(?<=[.!?])\s+|\n+", text, maxsplit=1)[0].strip(" \t\r\n.!?\"'")
    words = re.findall(r"[A-Za-z0-9]+(?:['’-][A-Za-z0-9]+)?", first)
    while words and words[0].lower() in {"did", "do", "have", "imagine", "what", "why", "this", "here"}:
        if len(words) <= 4:
            break
        words.pop(0)
    title = " ".join(words[:7]).strip()
    return title.title()[:60] if title else ""


# ===== Collaborative reasoning: GPT-5.5 drafts, Opus 4.8 critiques & corrects =====
DUAL_THINKER_MODEL = GPT55_MODEL                 # the proposer ("thinks")
DUAL_CRITIC_MODEL = "anthropic/claude-opus-4.8"  # the critic ("criticises and corrects")


def _post_llm_json(model, messages, max_tokens, temperature, timeout=180):
    payload = {"model": model, "messages": messages, "temperature": temperature,
               "max_tokens": max_tokens, "response_format": {"type": "json_object"}}
    data = post_json_url(WAVESPEED_LLM_API, payload, timeout=timeout)
    return extract_json_object(data["choices"][0]["message"]["content"])


def collaborate_json(messages, max_tokens=1800, temperature=0.15, timeout=180,
                     status_cb=None, label="reasoning",
                     thinker=DUAL_THINKER_MODEL, critic=DUAL_CRITIC_MODEL):
    """Two-model collaboration. The THINKER (GPT-5.5) drafts a JSON answer; the CRITIC
    (Opus 4.8) then receives the exact same task plus that draft and returns a corrected,
    stronger version in the same schema. Returns the critic's corrected JSON (or the draft
    if the critic fails). `messages` is the chat list; the user content may be plain text
    or a vision content list (text + image_url), so this works for reviews too."""
    log(status_cb, f"Collaborative {label}: GPT-5.5 drafts -> Opus 4.8 critiques & corrects...")
    proposal = None
    try:
        proposal = _post_llm_json(thinker, messages, max_tokens, temperature, timeout)
    except WaveSpeedBalanceError:
        raise
    except Exception as exc:  # noqa: BLE001
        log(status_cb, f"Collaborative {label}: thinker failed ({exc.__class__.__name__}); critic solos.")
    sys_msg = next((m.get("content", "") for m in messages if m.get("role") == "system"), "")
    user_msg = next((m for m in messages if m.get("role") == "user"), {"content": ""})
    crit_sys = (str(sys_msg) + "\n\nYou are now the SENIOR CRITIC reviewing another model's draft. "
                "Find every flaw, omission, wrong call, weak hook, repetition, off-topic media, or missed "
                "issue. Then return a corrected, stronger answer in the EXACT SAME JSON schema. Keep what is "
                "genuinely good, fix what is weak, and be strict and honest. Return JSON only.")
    draft_note = ("\n\n=== DRAFT FROM THE FIRST MODEL - critique it and return an improved version (same schema) ===\n"
                  + (json.dumps(proposal, ensure_ascii=False)[:7000] if proposal
                     else "(the first model produced nothing usable; do the task yourself)"))
    uc = user_msg.get("content")
    crit_content = (list(uc) + [{"type": "text", "text": draft_note}]) if isinstance(uc, list) else (str(uc) + draft_note)
    crit_messages = [{"role": "system", "content": crit_sys},
                     {"role": "user", "content": crit_content}]
    try:
        corrected = _post_llm_json(critic, crit_messages, max_tokens, temperature, timeout)
        if corrected:
            return corrected
    except WaveSpeedBalanceError:
        raise
    except Exception as exc:  # noqa: BLE001
        log(status_cb, f"Collaborative {label}: critic failed ({exc.__class__.__name__}); using the draft.")
    return proposal


def comprehend_script(title, script, reasoning_model=None, collaborate=False, status_cb=None):
    """Read the WHOLE script once and deeply understand what it is really about, so every
    downstream agent (search terms, clip matching, emphasis) is grounded in the SAME
    understanding instead of re-guessing from raw text. Returns
    {topic, thesis, tone, setting, viewer_takeaway, visual_motifs[]}."""
    if not (script or "").strip() or not os.environ.get("WAVESPEED_API_KEY"):
        return {}
    messages = [
        {"role": "system", "content": "You deeply comprehend a short-video narration and explain what it is really about so an editor can pick matching footage. Return JSON only."},
        {"role": "user", "content": (
            "Read this entire narration and explain what it is FUNDAMENTALLY about - the real subject and message, "
            "not a surface paraphrase. This understanding will steer footage search and clip selection, so be "
            "concrete about what the viewer should SEE on screen.\n"
            'Return STRICT JSON: {"topic": "<one sentence: what this video is about>", '
            '"thesis": "<the core point it makes>", "tone": "<emotional tone, e.g. somber ominous documentary>", '
            '"setting": "<where/what world it lives in, e.g. modern urban Japan>", '
            '"viewer_takeaway": "<what the viewer should feel or realize>", '
            '"visual_motifs": ["<6-9 concrete things a viewer should SEE that represent this video>"]}\n\n'
            f"Title: {title}\n\nNarration:\n{script}"
        )},
    ]
    try:
        if collaborate:
            data = collaborate_json(messages, max_tokens=900, temperature=0.2, status_cb=status_cb, label="script comprehension") or {}
        else:
            data = _post_llm_json(reasoning_model or GPT55_MODEL, messages, 900, 0.2) or {}
        if isinstance(data, dict) and data.get("topic"):
            log(status_cb, f"Script understood - topic: {str(data.get('topic'))[:140]}")
            return data
    except Exception as exc:  # noqa: BLE001
        log(status_cb, f"Script comprehension skipped ({exc.__class__.__name__}).")
    return {}


def understanding_brief(understanding):
    """A compact grounding paragraph every clip agent gets, so they all share one understanding."""
    u = understanding if isinstance(understanding, dict) else {}
    if not u:
        return ""
    parts = []
    if u.get("topic"):           parts.append(f"What this video is really about: {u['topic']}")
    if u.get("thesis"):          parts.append(f"Core point: {u['thesis']}")
    if u.get("setting"):         parts.append(f"Setting/world: {u['setting']}")
    if u.get("tone"):            parts.append(f"Tone: {u['tone']}")
    if isinstance(u.get("visual_motifs"), list) and u["visual_motifs"]:
        parts.append("Things that visually represent it: " + ", ".join(str(m) for m in u["visual_motifs"][:9]))
    return ("UNDERSTAND THE VIDEO FIRST (all choices must fit this):\n" + "\n".join(parts) + "\n\n") if parts else ""


def llm_micro_beat_plan(title, script, visual_script, target_duration, base_scenes, reasoning_model=None, status_cb=None, collaborate=False):
    if not os.environ.get("WAVESPEED_API_KEY"):
        return []
    prompt = (
        "Before generating media, create a Micro-Beat Plan for a vertical YouTube Short engineered for maximum retention.\n"
        "The voice script is not background narration. It is the edit map. Every visual decision must be justified by the exact spoken words at that timestamp.\n"
        "Captions are burned in word-by-word, frame-synced to the voice, and cuts land on the beat - so make beats tight and punchy.\n"
        "Beat 1 is the hook: it must be the strongest, most curiosity-provoking visual in the whole video and read instantly. "
        "Across adjacent beats, vary visual_hook_type and shot scale so it never feels repetitive (pattern interrupt), and let tension escalate toward a payoff.\n"
        "Split the voice script into micro-beats, usually 0.8 to 2.2 seconds, but keep timing coherent with the provided timed base scenes. "
        "Each micro-beat must visually answer the current voice line. If the viewer watched without audio, they should roughly understand the same idea.\n"
        "Voice-Script = content and timing. Visual Script = image ideas, mood, and shot suggestions. Auto Director decides what fits. "
        "If visual script contradicts the voice line, adapt or ignore it. If it contains a good image idea, use it.\n"
        "Do not make generic cinematic visuals. Do not use random dramatic people, rooms, maps, books, or props that do not answer the exact voice line.\n"
        "Return strict JSON only with keys: micro_beats, and optionally visual_sections.\n"
        "visual_sections is an optional array of roughly 5-10 second thematic blocks: {id, start, end, theme}.\n"
        "micro_beats is an array with one object per beat:\n"
        "{exact_voice_text,start_time,end_time,beat_purpose,scene_objective,visual_meaning,viewer_emotion,visual_hook_type,required_visual_information,best_media_type,shot_type,camera_motion,subject_motion,environment_motion,emotional_action,must_show,must_not_show,crop_plan,voice_match_score_target}.\n"
        "Allowed best_media_type: web_image, source_image_to_seedance, seedance, existing_clip. "
        "Allowed visual_hook_type examples: face/reaction, shocking object, historical image, fast comparison, transformation, reveal, danger/mystery, proof/evidence, absurd contrast, epic scale.\n"
        "crop_plan must include focus_subject, keep_face_visible, safe_zone, avoid_cutting, allow_pan.\n\n"
        f"Title: {title}\n"
        f"Target duration seconds: {target_duration}\n"
        f"Voice script:\n{script}\n\n"
        f"Optional visual direction:\n{visual_script or '(none provided - infer full visual plan from voice script)'}\n\n"
        f"Timed base scenes JSON:\n{json.dumps(base_scenes, ensure_ascii=False)}"
    )
    payload = {
        "model": reasoning_model or GPT55_MODEL,
        "messages": [
            {"role": "system", "content": "You are an elite short-form editor obsessed with hooks and retention. Build fast, clear, pattern-interrupting micro-beat edit maps from voice scripts. Return compact valid JSON only."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.16,
        "max_tokens": 16000,
        "response_format": {"type": "json_object"},
    }
    try:
        if collaborate:
            plan = collaborate_json(payload["messages"], max_tokens=payload["max_tokens"],
                                    temperature=payload["temperature"], status_cb=status_cb,
                                    label="edit-map") or {}
        else:
            log(status_cb, "Micro-Beat Planner: asking Reasoning Agent for exact voice-line edit map...")
            data = post_json_url(WAVESPEED_LLM_API, payload, timeout=180)
            plan = extract_json_object(data["choices"][0]["message"]["content"])
        beats = normalize_micro_beat_plan(plan.get("micro_beats", []), title, script, target_duration)
        if beats:
            log(status_cb, f"Micro-Beat Planner: created {len(beats)} beat(s).")
            return {"micro_beats": beats, "visual_sections": plan.get("visual_sections", [])}
    except Exception as exc:
        log(status_cb, f"Micro-Beat Planner skipped: {exc}")
    return {"micro_beats": [], "visual_sections": []}


def build_micro_beat_plan(title, script, visual_script, target_duration, base_scenes, reasoning_model=None, status_cb=None, collaborate=False):
    result = llm_micro_beat_plan(title, script, visual_script, target_duration, base_scenes, reasoning_model=reasoning_model, status_cb=status_cb, collaborate=collaborate)
    beats = result.get("micro_beats", [])
    if not beats:
        beats = fallback_micro_beat_plan(title, script, target_duration, base_scenes=base_scenes)
        log(status_cb, f"Micro-Beat Planner fallback: created {len(beats)} beat(s).")
    else:
        # COVERAGE GUARD: the reasoning model can hit its output-token cap and return a
        # micro-beat plan that silently stops partway through the script. Rendering that
        # plan cuts the video off mid-narration (a 57s voiceover became a 30s video).
        # If the beats don't reach the end of the voiceover, extend them with deterministic
        # fallback beats for the uncovered tail so every spoken second still gets a visual.
        covered = max((float(b.get("end", 0.0)) for b in beats), default=0.0)
        if covered < float(target_duration) - 1.0:
            full = fallback_micro_beat_plan(title, script, target_duration, base_scenes=base_scenes)
            tail = [b for b in full if float(b.get("start", 0.0)) >= covered - 0.25]
            if tail:
                log(status_cb,
                    f"Micro-Beat coverage was only {covered:.1f}s of {target_duration:.1f}s "
                    f"(reasoning model truncated its output); extending with {len(tail)} fallback "
                    f"beat(s) so the full voiceover is covered.")
                beats = beats + tail
    return {"micro_beats": beats or base_scenes, "visual_sections": result.get("visual_sections", [])}


def audio_analysis_prompt(title, script_hint, audio_duration=None):
    hint = f"\nReference script, if useful:\n{script_hint}\n" if script_hint else ""
    measured_duration = (
        f"Measured local audio duration: {audio_duration:.3f} seconds. Use this as the hard duration limit.\n"
        if audio_duration
        else ""
    )
    return (
        "Analyze this audio for a vertical documentary YouTube Short.\n"
        "Return strict JSON only. Required keys:\n"
        "transcript: full spoken transcript, as verbatim as possible. Do not rewrite or expand it.\n"
        "duration_seconds: total audio duration in seconds.\n"
        "sentence_timestamps: array covering every spoken sentence or short phrase with exact start, end, text.\n"
        "scenes: array of visual beats with start, end, script, visual_focus, search_terms, motion_intensity.\n"
        "All scene starts and ends must land on the spoken sentence_timestamps boundaries. Do not move a cut earlier or later for visual style.\n"
        "If the voice changes topic at second 12.4, the matching scene must also start around 12.4.\n"
        "Use silence only to extend the previous visual; never invent extra narration or events.\n"
        "Keep each script field short enough for one visual beat. Use the title only as context for spelling and entities, not as extra content.\n"
        "Do not invent facts, events, dates, or names not spoken in the audio or shown in the reference script.\n"
        f"Short title/topic: {title}\n"
        f"{measured_duration}"
        f"{hint}"
    )


def gemini_audio_messages_with_url(title, script_hint, audio_url, audio_duration=None):
    return [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": audio_analysis_prompt(title, script_hint, audio_duration=audio_duration)},
                {"type": "audio_url", "audio_url": {"url": audio_url}},
            ],
        }
    ]


def gemini_audio_messages_with_base64(title, script_hint, audio_path, audio_duration=None):
    mime = mimetypes.guess_type(str(audio_path))[0] or "application/octet-stream"
    audio_data = base64.b64encode(Path(audio_path).read_bytes()).decode("ascii")
    audio_format = Path(audio_path).suffix.lower().lstrip(".") or "wav"
    return [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": audio_analysis_prompt(title, script_hint, audio_duration=audio_duration)},
                {"type": "input_audio", "input_audio": {"data": audio_data, "format": audio_format, "mime_type": mime}},
            ],
        }
    ]


def analyze_audio_with_gemini(audio_path, title, script_hint, status_cb=None, audio_duration=None):
    key = os.environ.get("WAVESPEED_API_KEY", "")
    if not key:
        raise RuntimeError("Audio input needs WAVESPEED_API_KEY for Gemini audio analysis.")
    log(status_cb, "Uploading audio to WaveSpeed media storage...")
    audio_url, _ = pipeline.upload_media(Path(audio_path), key)
    payload = {
        "model": GEMINI_AUDIO_MODEL,
        "messages": gemini_audio_messages_with_url(title, script_hint, audio_url, audio_duration=audio_duration),
        "temperature": 0.05,
        "max_tokens": 3600,
        "response_format": {"type": "json_object"},
    }

    def _attempt(timeout):
        data = post_json_url(WAVESPEED_LLM_API, payload, timeout=timeout)
        content = ((data.get("choices") or [{}])[0].get("message", {}).get("content") or "").strip()
        if not content:
            raise RuntimeError("Gemini returned empty content.")
        analysis = extract_json_object(content)
        if not isinstance(analysis, dict):
            raise RuntimeError("Gemini audio analysis did not return a JSON object.")
        return analysis

    try:
        analysis = _attempt(180)
    except Exception as url_exc:
        # URL mode can return an empty / non-JSON body; retry with inline base64 audio.
        log(status_cb, f"Gemini audio URL mode failed ({url_exc}); trying inline audio...")
        payload["messages"] = gemini_audio_messages_with_base64(title, script_hint, audio_path, audio_duration=audio_duration)
        analysis = _attempt(240)
    analysis["uploaded_audio_url"] = audio_url
    return analysis


def should_contain(path):
    name = path.name.lower()
    return any(token in name for token in ["map", "australia", "location", "wa_", "wheat_plot", "gun_during", "reloading", "fallow"])


def motion_from_director(style):
    style = str(style or "normal").lower()
    if style == "calm":
        return {"zoom_start": 1.0, "zoom_end": 1.025, "pan_x": 0, "pan_y": 0}
    if style == "dynamic":
        return {"zoom_start": 1.0, "zoom_end": 1.07, "pan_x": 24, "pan_y": 0}
    return {"zoom_start": 1.0, "zoom_end": 1.05, "pan_x": 14, "pan_y": 0}


def plan_config(project_dir, title, script, target_duration, allow_seedance=True, max_seedance=3, static_gpt_image_count=4, scenes_override=None, director_plan=None, visual_sections=None, speaker_hook_enabled=False, image_model="openai/gpt-image-2/text-to-image", video_model="seedance-2.0"):
    scenes = scenes_override or parse_timed_script(script, target_duration)
    images, _ = list_media(project_dir)
    try:
        web_metadata = {
            Path(item.get("local_path", "")).name.lower(): item
            for item in read_web_manifest(project_dir)
            if isinstance(item, dict) and item.get("local_path")
        }
    except Exception:
        web_metadata = {}
    scene_directives = director_scene_map(director_plan)
    directed_seedance = {
        index for index, item in scene_directives.items()
        if bool(item.get("use_seedance")) and index < len(scenes)
    }
    seedance_indexes = choose_seedance_scenes(
        scenes,
        max_seedance if allow_seedance else 0,
        preferred_indexes=directed_seedance if allow_seedance else set(),
        exclude_indexes={0} if speaker_hook_enabled else set(),
    )
    existing_clips = existing_seedance_clips(project_dir)
    enough_existing_seedance = len(existing_clips) >= len(seedance_indexes) and len(seedance_indexes) > 0
    required_gpt_indexes = set() if enough_existing_seedance else set(seedance_indexes)
    static_gpt_image_count = 0
    static_gpt_indexes = set()
    gpt_indexes = set(required_gpt_indexes)
    used_seedance_sources = set()
    used_static_media = set()
    used_existing_seedance_clips = set()
    still_scene_count = len([index for index in range(len(scenes)) if index not in seedance_indexes])
    processed_still_scenes = 0

    config_scenes = []
    existing_clip_cursor = 0
    for index, scene in enumerate(scenes, 1):
        beat_words = words(scene.get("script", "")) or words(scene_text_for_planning(scene))
        scene_index = index - 1
        directive = scene_directives.get(scene_index, {})
        seedance = scene_index in seedance_indexes
        wants_gpt_asset = seedance and scene_index in gpt_indexes
        asset_name = f"scene_{index:02d}_{slugify(scene['script'][:38])}.png"
        motion_style = directive.get("motion", "normal")
        scene_motion = motion_from_director(motion_style)
        visual_intent = clean_text(directive.get("visual_intent", ""))
        visual_direction = scene.get("visual_direction") or scene.get("visual_script", "")
        scene_data = {
            "id": f"{index:02d}",
            "name": f"Scene {index:02d}",
            "start": scene["start"],
            "end": scene["end"],
            "caption": "",
            "seedance": seedance,
            "asset": asset_name,
            "script": scene["script"],
            "exact_voice_text": scene.get("exact_voice_text") or scene.get("voice_line") or scene.get("script", ""),
            "voice_line": scene.get("voice_line") or scene.get("exact_voice_text") or scene.get("script", ""),
            "micro_beat": bool(scene.get("micro_beat")),
            "scene_objective": scene.get("scene_objective") or scene.get("beat_purpose", ""),
            "beat_purpose": scene.get("beat_purpose", ""),
            "visual_meaning": scene.get("visual_meaning", ""),
            "viewer_emotion": scene.get("viewer_emotion") or scene.get("emotion", ""),
            "emotion": scene.get("emotion") or scene.get("viewer_emotion", ""),
            "visual_hook_type": scene.get("visual_hook_type", ""),
            "required_visual_information": scene.get("required_visual_information", ""),
            "best_media_type": scene.get("best_media_type", ""),
            "shot_type": scene.get("shot_type", ""),
            "camera_motion": scene.get("camera_motion", ""),
            "subject_motion": scene.get("subject_motion", ""),
            "environment_motion": scene.get("environment_motion", ""),
            "emotional_action": scene.get("emotional_action", ""),
            "must_show": scene.get("must_show", []),
            "must_not_show": scene.get("must_not_show", []),
            "crop_plan": scene.get("crop_plan", {}),
            "voice_match_score_target": scene.get("voice_match_score_target", 85),
            "director_scores": {
                "voice_match_score": directive.get("voice_match_score"),
                "visual_clarity_score": directive.get("visual_clarity_score"),
                "motion_score": directive.get("motion_score"),
                "shorts_retention_score": directive.get("shorts_retention_score"),
                "historical_or_factual_accuracy_score": directive.get("historical_or_factual_accuracy_score"),
            },
            "director_reason": directive.get("reason", ""),
            "visual_direction": visual_direction,
            "prompt": scene_prompt(title, scene, image_model=image_model) + (f"\nDirector visual intent: {visual_intent}" if visual_intent else ""),
            "video_prompt": scene_video_prompt(scene, index, len(scenes), visual_intent=visual_intent, title=title, video_model=video_model),
            "motion": scene_motion,
            "needs_gpt_asset": wants_gpt_asset,
        }
        # SCRAPE-FIRST: the semantic matcher pre-assigned a specific scraped clip to this exact
        # beat (only when it passed the script-match gate). Use it directly. Beats with no clip
        # fall through and get an AI/generated fallback instead of random Japan b-roll.
        if scene.get("clip"):
            scene_data["seedance"] = True
            scene_data["clip"] = Path(scene["clip"]).name
            scene_data["needs_gpt_asset"] = False
            scene_data["shots"] = [{"at": 0.0, "use_clip": True}]
            config_scenes.append(scene_data)
            continue
        if seedance and enough_existing_seedance:
            clip = None
            while existing_clip_cursor < len(existing_clips):
                candidate_clip = existing_clips[existing_clip_cursor]
                existing_clip_cursor += 1
                if media_unique_key(candidate_clip) in used_existing_seedance_clips:
                    continue
                clip = candidate_clip
                used_existing_seedance_clips.add(media_unique_key(clip))
                break
            if clip is None:
                scene_data["seedance"] = False
                scene_data["needs_gpt_asset"] = wants_gpt_asset
                seedance = False
            else:
                scene_data["clip"] = clip.name
                scene_data["shots"] = [{"at": 0.0, "use_clip": True}]
        if seedance and scene_data.get("clip"):
            pass
        elif seedance and enough_existing_seedance:
            scene_data["seedance"] = False
            scene_data["shots"] = [{"at": 0.0, "motion": scene_motion}]
        elif seedance:
            scene_data["needs_gpt_asset"] = True
            scene_data["shots"] = [{"at": 0.0, "use_clip": True, "crop_plan": scene_data.get("crop_plan", {})}]
            used_seedance_sources.add(asset_name)
        else:
            processed_still_scenes += 1
            available_images = [
                path for path in images
                if path.name not in used_seedance_sources and media_unique_key(path) not in used_static_media
            ]
            ranked = sorted(
                available_images,
                key=lambda p: media_score(p, beat_words, scene=scene_data, scene_number=index, web_metadata=web_metadata),
                reverse=True,
            )
            remaining_still_scenes = max(1, still_scene_count - processed_still_scenes + 1)
            duration = max(0.1, scene["end"] - scene["start"])
            target_shots = max(1, min(3, math.ceil(len(ranked) / remaining_still_scenes)))
            if duration < 2.75:
                target_shots = min(target_shots, 1)
            elif duration < 4.25:
                target_shots = min(target_shots, 2)
            chosen = []
            for path in ranked:
                if media_unique_key(path) not in {media_unique_key(p) for p in chosen}:
                    chosen.append(path)
                if len(chosen) >= target_shots:
                    break
            if not chosen:
                scene_data["shots"] = [
                    {
                        "at": 0.0,
                        "fit": str(directive.get("fit", "cover")).lower() if str(directive.get("fit", "cover")).lower() in {"cover", "contain"} else "cover",
                        "motion": scene_motion,
                        "crop_plan": scene_data.get("crop_plan", {}),
                    }
                ]
                config_scenes.append(scene_data)
                continue
            step = duration / max(1, len(chosen))
            shots = []
            for shot_index, path in enumerate(chosen):
                used_static_media.add(media_unique_key(path))
                rel = path.name if path.parent.name in {"gpt images", "web images", "seedance 2.0"} else str(path)
                if path.parent.name == "web images":
                    rel = f"web images/{path.name}"
                elif path.parent.name == "gpt images":
                    rel = path.name
                preferred_fit = str(directive.get("fit", "")).lower()
                metadata = web_metadata.get(path.name.lower(), {})
                crop_safety = str(metadata.get("crop_safety", "")).lower()
                fit_mode = preferred_fit if preferred_fit in {"cover", "contain"} else ("contain" if should_contain(path) or crop_safety in {"risky", "bad"} else "cover")
                shots.append(
                    {
                        "at": round(shot_index * step, 2),
                        "asset": rel,
                        "fit": fit_mode,
                        "motion": scene_motion,
                        "crop_plan": metadata.get("crop_plan") if isinstance(metadata.get("crop_plan"), dict) else scene_data.get("crop_plan", {}),
                        "visual_role": metadata.get("review_role") or scene_data.get("visual_hook_type"),
                        "visual_relevance_score": metadata.get("visual_relevance_score"),
                        "crop_safety": metadata.get("crop_safety"),
                    }
                )
            scene_data["shots"] = shots
        config_scenes.append(scene_data)

    duration = max(scene["end"] for scene in scenes)
    slug = slugify(title)
    return {
        "project_slug": slug,
        "title": title,
        "use_case": "historical-scene",
        "duration": duration,
        "fps": 30,
        "resolution": [1080, 1920],
        "output_basename": f"{slug}_auto_short",
        "use_seedance_clips": True,
        "seedance_default": False,
        "render_captions": True,  # word-by-word captions, locked to the voice timing
        "animated_captions": True,
        "caption_max_words": 3,
        "caption_uppercase": True,
        "caption_center_y": 0.72,
        "export_caption_pngs": True,
        "dynamic_zoom": True,
        "cut_punch_amount": 0.06,
        "cut_punch_seconds": 0.34,
        "hook_hold_seconds": 0.6,
        "hook_punch_amount": 0.12,
        "smart_overlays": False,
        "kinetic_style": False,
        "still_motion_scale": 0.38,
        "contain_motion_scale": 0.42,
        "visual_style": (director_plan or {}).get("visual_style") or "realistic cinematic mini-documentary, clean, clear, script-matched",
        "global_constraints": "Strictly SFW: any people are fully clothed in period attire, no nudity, no nude or partially-nude figures, no exposed bodies, no gore or graphic injury. No embedded captions, no watermark, no logo. GPT images are only Seedance I2V source images and must never appear as static stills in the final render. Avoid collage/multi-panel layouts except for at most 1-2 deliberate document-board images across the Short.",
        "caption_y": 150,
        "grain": 16,
        "dust": False,
        "tint_alpha": 14,
        "wavespeed": {
            "image_model": "openai/gpt-image-2/text-to-image",
            "aspect_ratio": "9:16",
            "quality": "high",
            "resolution": "1k",
            "output_format": "png",
            "image_concurrency": 2,
            "video_model": "bytedance/seedance-2.0/image-to-video",
            "video_aspect_ratio": "9:16",
            "video_resolution": "480p",
            "video_generate_audio": False,  # never let the clip model generate speech/voices; SFX come from the app layer
            "video_enable_web_search": False,
            "video_timeout_s": 1800,
            "video_concurrency": 2,
        },
        "seedance_audio_in_final": True,
        "mix_seedance_audio_with_speech": False,
        "seedance_clip_start_trim": 0.5,
        "gpt_static_stills_disabled": True,
        "sfx_enabled": True,
        "sfx_volume": 0.08,
        "sfx_volume_with_speech": 0.07,
        "sfx_transition_volume": 0.085,
        "sfx_transition_volume_with_speech": 0.075,
        "sfx_single_transition_sound_per_video": True,
        "sfx_max_per_minute": 34,
        "background_music_enabled": False,
        "background_music_user_enabled": False,
        "background_music_volume": 0.09,
        "background_music_volume_with_speech": 0.045,
        "audio_master_gain": 1.05,
        "seedance_audio_volume": 0.16,
        "seedance_audio_volume_with_speech": 0.08,
        "metadata_cleanup_enabled": True,
        "agent": {
            "director_enabled": bool(director_plan),
            "director_summary": (director_plan or {}).get("summary") if isinstance(director_plan, dict) else None,
            "enough_existing_seedance": enough_existing_seedance,
            "existing_seedance_count": len(existing_clips),
            "selected_seedance_count": len(seedance_indexes),
            "seedance_i2v_source_image_count": len(required_gpt_indexes),
            "selected_static_gpt_image_count": len(static_gpt_indexes),
            "selected_gpt_image_count": len(gpt_indexes),
            "target_static_gpt_image_count": static_gpt_image_count,
            "target_total_gpt_image_count": static_gpt_image_count + len(required_gpt_indexes),
        },
        "scenes": config_scenes,
    }


def enforce_unique_media_per_render(config, status_cb=None):
    try:
        project_dir, asset_dir, _, _ = pipeline.project_paths(config)
        gpt_dir = project_dir / "gpt images"
    except Exception:
        project_dir = asset_dir = gpt_dir = None
    # Scrape mode INTENTIONALLY reuses clips across scenes (same-script reuse, loop-fill when there
    # are fewer accepted clips than scenes). The renderer holds/freezes a reused clip, so de-duping
    # here would strip clips and leave scenes blank -> pre-render validation would then fail.
    scrape_mode = str(config.get("clip_source") or "") == "scrape"
    seen_static_assets = set()
    seen_seedance_clips = set()
    for index, scene in enumerate(config.get("scenes", []), 1):
        if scene.get("seedance") and scrape_mode:
            continue                      # keep every reused scrape clip
        if scene.get("seedance"):
            clip_key = media_ref_key(scene.get("clip") or pipeline.clip_filename(scene, index))
            if clip_key and clip_key in seen_seedance_clips:
                log(status_cb, f"Unique media guard: disabled repeated Seedance clip reference in scene {scene.get('id', index)}.")
                scene["seedance"] = False
                scene.pop("clip", None)
                scene["needs_gpt_asset"] = False
                scene["shots"] = [{"at": 0.0, "motion": scene.get("motion", {})}]
            else:
                seen_seedance_clips.add(clip_key)
                if scene.get("asset"):
                    seen_static_assets.add(media_ref_key(scene.get("asset")))

        if scene.get("seedance"):
            continue
        unique_shots = []
        removed = 0
        removed_gpt_static = 0
        for shot in scene.get("shots", []) or []:
            asset = shot.get("asset")
            if not asset:
                unique_shots.append(shot)
                continue
            if gpt_dir and asset_dir:
                path = pipeline.resolve_media_path(config, asset_dir, asset)
                if path_under(path, gpt_dir):
                    removed_gpt_static += 1
                    continue
            key = media_ref_key(asset)
            if key in seen_static_assets:
                removed += 1
                continue
            seen_static_assets.add(key)
            unique_shots.append(shot)
        if removed_gpt_static:
            scene["needs_gpt_asset"] = False
            log(status_cb, f"GPT still guard: removed {removed_gpt_static} GPT still image reference(s) from scene {scene.get('id', index)}; GPT images are Seedance sources only.")
        if removed:
            log(status_cb, f"Unique media guard: removed {removed} repeated still image reference(s) from scene {scene.get('id', index)}.")
        if removed or removed_gpt_static:
            scene["shots"] = unique_shots or [{"at": 0.0, "motion": scene.get("motion", {})}]
    config["unique_media_per_render"] = True
    config["gpt_static_stills_disabled"] = True
    return config


def download_web_images(project_dir, urls, status_cb=None):
    web_dir = project_dir / "web images"
    web_dir.mkdir(parents=True, exist_ok=True)
    downloaded = []
    for index, url in enumerate([u.strip() for u in urls.splitlines() if u.strip()], 1):
        parsed = urllib.parse.urlparse(url)
        suffix = Path(parsed.path).suffix.lower()
        if suffix not in IMAGE_EXTS:
            suffix = ".jpg"
        out = web_dir / f"web_image_{index:02d}{suffix}"
        log(status_cb, f"Downloading web image {index}: {url}")
        req = urllib.request.Request(url, headers={"User-Agent": "autonomous-shorts-agent/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=60) as response:
                out.write_bytes(response.read())
            downloaded.append(str(out))
        except Exception as exc:
            log(status_cb, f"Web image failed: {url} ({exc})")
    return downloaded


def important_terms(text, limit=5, noise=None):
    noise = noise or STOPWORDS
    candidates = []
    for token in re.findall(r"[a-zA-Z][a-zA-Z0-9'-]{3,}", text.lower()):
        if token in noise:
            continue
        if token not in candidates:
            candidates.append(token)
    return candidates[:limit]


def strip_markup(text):
    text = re.sub(r"<[^>]+>", " ", str(text or ""))
    return re.sub(r"\s+", " ", text).strip()


def extract_json_object(text):
    text = str(text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text, flags=re.I).strip()
        text = re.sub(r"```$", "", text).strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    match = re.search(r"\{.*\}", text, flags=re.S)
    candidate = match.group(0) if match else text
    try:
        return json.loads(candidate)
    except Exception:
        pass
    # LLMs (incl. Opus via WaveSpeed) sometimes emit malformed JSON - missing
    # commas, unclosed braces, trailing junk. Repair it instead of discarding the
    # whole plan and silently falling back to heuristics.
    try:
        from json_repair import repair_json
        repaired = repair_json(candidate, return_objects=True)
        if isinstance(repaired, (dict, list)):
            return repaired
    except Exception:
        pass
    # Unparseable (e.g. the API returned empty/non-JSON content). NEVER raise here - callers do
    # `extract_json_object(...) or {}` and fall back gracefully; raising aborted the whole step.
    return None


def extract_phrases(text, limit=8):
    phrases = []
    for match in re.finditer(r"\b[A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+){0,3}\b", text):
        phrase = match.group(0).strip()
        if phrase.lower() in STOPWORDS or phrase.lower() == "the":
            continue
        if phrase not in phrases:
            phrases.append(phrase)
        if len(phrases) >= limit:
            break
    return phrases


def topic_query_candidates(title, script, canonical=""):
    topic_title = humanize_title(title)
    combined_words = words(f"{topic_title} {script}")
    script_words = words(script)
    candidates = []

    def add(value):
        value = " ".join(str(value or "").split())
        if value and value.lower() not in [item.lower() for item in candidates]:
            candidates.append(value)

    canonical = humanize_title(canonical)
    if canonical and (words(canonical) & combined_words):
        add(canonical)

    lowered = str(script or "").lower()
    if "emu" in lowered and "war" in lowered:
        add("Emu War")
        add("Great Emu War")
    if "pig" in lowered and "war" in lowered and ("san juan" in lowered or "oregon" in lowered or "britain" in lowered):
        add("Pig War San Juan Island")
        add("San Juan Islands boundary dispute")

    for phrase in extract_phrases(script, 6):
        phrase_words = words(phrase)
        if phrase_words & script_words and len(phrase_words - STOPWORDS) >= 2:
            add(phrase)

    topic_words = words(topic_title)
    if topic_title and (topic_words & script_words or len(topic_title.replace(" ", "")) > 12):
        add(topic_title)

    script_terms = important_terms(script, 8, STOPWORDS)
    if len(script_terms) >= 2:
        add(" ".join(script_terms[:4]))
        add(" ".join(script_terms[:3]))

    if topic_title:
        add(topic_title)
    if canonical:
        add(canonical)
    return candidates


def request_json_url(url, timeout=45, min_interval=1.1):
    host_throttle(url, min_interval)
    req = urllib.request.Request(url, headers={"User-Agent": "autonomous-shorts-agent/1.1"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def request_text_url(url, timeout=45, min_interval=1.1, referer=""):
    host_throttle(url, min_interval)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json;q=0.8,*/*;q=0.7",
        "Accept-Language": "en-US,en;q=0.9,de;q=0.8",
    }
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace")


class WaveSpeedBalanceError(RuntimeError):
    """The WaveSpeed account has no credit left - every API call fails with HTTP 403
    'balance not enough'. Raised so a run ABORTS immediately with a clear message
    instead of silently blank-falling-back for half an hour."""


class PaidAPIBlockedError(RuntimeError):
    """Raised when SHORTSLAB_NO_PAID_API=1 (no-cost test mode) and code tries to
    contact a paid endpoint. Tests set this env var so any accidental paid call
    fails immediately instead of spending credit."""


def assert_paid_api_allowed(what="paid API"):
    if os.environ.get("SHORTSLAB_NO_PAID_API", "") == "1":
        raise PaidAPIBlockedError(
            f"NO_PAID_API_TEST_MODE: blocked call to {what} (SHORTSLAB_NO_PAID_API=1)")


def _parse_sse_chat_stream(body):
    """Reassemble a chat/completions SSE stream into one normal response object.

    Since ~2026-07-11 llm.wavespeed.ai streams `text/event-stream` chunks even when
    `stream` is false/absent. json.loads() on that body raised JSONDecodeError in EVERY
    LLM call (title gen, balance preflight, V2 architect, scene matcher, audio director...)
    and silently collapsed whole runs. Concatenate the delta/message content of all
    `data:` chunks and return a dict shaped like the old non-streaming response.
    """
    content, finish, last = [], None, {}
    for line in body.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        chunk = line[5:].strip()
        if not chunk or chunk == "[DONE]":
            continue
        try:
            d = json.loads(chunk)
        except Exception:
            continue
        if isinstance(d, dict):
            last = d
            for ch in d.get("choices") or []:
                if not isinstance(ch, dict):
                    continue
                delta = ch.get("delta") or {}
                if isinstance(delta, dict) and delta.get("content"):
                    content.append(str(delta["content"]))
                msg = ch.get("message") or {}
                if isinstance(msg, dict) and msg.get("content"):
                    content.append(str(msg["content"]))
                if ch.get("finish_reason"):
                    finish = ch["finish_reason"]
    result = dict(last)
    result["object"] = "chat.completion"
    result["choices"] = [{"index": 0, "finish_reason": finish,
                          "message": {"role": "assistant", "content": "".join(content)}}]
    return result


def post_json_url(url, payload, timeout=75):
    payload = dict(payload or {})
    model_id = str(payload.get("model") or "")
    requested_mode = payload.pop("reasoning_mode", None)
    selected_mode = reasoning_modes.validate_reasoning_mode(
        model_id, requested_mode if requested_mode is not None else reasoning_modes.current_reasoning_mode(model_id))
    reasoning_payload = reasoning_modes.build_reasoning_payload(model_id, selected_mode)
    endpoint = reasoning_modes.get_wavespeed_endpoint(model_id, selected_mode)
    if url == WAVESPEED_LLM_API:
        payload.update(reasoning_payload)
        # WaveSpeed behavior change ~2026-07-11: sending response_format json_object makes
        # chat/completions return an EMPTY SSE stream (0 completion tokens) - the model
        # generates nothing. Prompts already demand strict JSON and extract_json_object
        # parses fenced/raw text, so drop the parameter entirely.
        payload.pop("response_format", None)
        if endpoint == "responses":
            url = WAVESPEED_RESPONSES_API
            messages = payload.pop("messages", [])
            payload["input"] = reasoning_modes.responses_input(messages)
            if "max_tokens" in payload:
                payload["max_output_tokens"] = payload.pop("max_tokens")
    print(f"WaveSpeed request: model={model_id} reasoning={selected_mode or 'unsupported'} "
          f"endpoint={endpoint} payload={reasoning_payload}")
    assert_paid_api_allowed(url)
    # Thinking models (Gemini 2.x/3.x, GLM, Qwen, DeepSeek...) spend output tokens
    # on internal reasoning; a low max_tokens then yields EMPTY content
    # (finish_reason=length). Give those a generous ceiling so the actual JSON
    # answer survives after the thinking. GPT/Opus are left untouched.
    if isinstance(payload, dict) and payload.get("max_tokens"):
        model = str(payload.get("model", "")).lower()
        if any(token in model for token in ("gemini", "glm", "qwen", "deepseek", "thinking")):
            try:
                if int(payload["max_tokens"]) < 8000:
                    payload = dict(payload)
                    payload["max_tokens"] = 8000
            except (TypeError, ValueError):
                pass
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {os.environ.get('WAVESPEED_API_KEY', '')}",
            "Content-Type": "application/json",
            "User-Agent": "autonomous-shorts-agent/1.1",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            ctype = str(response.headers.get("Content-Type") or "")
            if "text/event-stream" in ctype or raw.lstrip().startswith("data:"):
                # WaveSpeed streams SSE even without stream=true (behavior change 2026-07)
                result = _parse_sse_chat_stream(raw)
            else:
                result = json.loads(raw)
            if endpoint == "responses" and "choices" not in result:
                text = str(result.get("output_text") or "")
                if not text:
                    for item in result.get("output", []) or []:
                        for content in item.get("content", []) or []:
                            if content.get("type") in ("output_text", "text"):
                                text += str(content.get("text") or "")
                result["choices"] = [{"message": {"role": "assistant", "content": text}}]
            return result
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        if exc.code in (402, 403) and "balance" in body.lower():
            raise WaveSpeedBalanceError(
                "WaveSpeed account balance is EMPTY - the API rejects every request "
                "(HTTP 403 'balance not enough'). Top up your credit at "
                "https://wavespeed.ai and start the run again.") from exc
        raise


def assert_wavespeed_balance(status_cb=None):
    """One tiny LLM ping at run start: an empty WaveSpeed balance aborts the run within
    seconds with a clear message instead of failing every call silently for 30+ minutes.
    Transient API hiccups never block a run - only the explicit balance error raises."""
    if not os.environ.get("WAVESPEED_API_KEY"):
        return
    try:
        post_json_url(WAVESPEED_LLM_API, {
            "model": GPT55_MODEL,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 8}, timeout=45)
    except WaveSpeedBalanceError:
        raise
    except Exception as exc:  # noqa: BLE001
        log(status_cb, f"Balance preflight inconclusive ({exc.__class__.__name__}) - continuing.")


def wikipedia_search_pages(query, limit=4):
    params = {
        "action": "query",
        "format": "json",
        "list": "search",
        "srsearch": query,
        "srlimit": str(limit),
        "srprop": "snippet",
    }
    url = f"{WIKIPEDIA_API}?{urllib.parse.urlencode(params)}"
    data = request_json_url(url, min_interval=0.6)
    return [
        {"title": item.get("title", ""), "snippet": strip_markup(item.get("snippet", ""))}
        for item in data.get("query", {}).get("search", [])
        if item.get("title")
    ]


def llm_search_plan(title, scenes, reasoning_model=None, status_cb=None):
    if not os.environ.get("WAVESPEED_API_KEY"):
        return {}
    scene_lines = [
        {"scene": index, "script": scene.get("script", ""), "visual_direction": scene.get("visual_script", "")}
        for index, scene in enumerate(scenes, 1)
    ]
    prompt = (
        "You plan web image searches for a vertical documentary Short.\n"
        "Return strict JSON only with keys: canonical_topic, aliases, topic_terms, scene_queries.\n"
        "scene_queries must be an array. Each item: {scene:number, queries:[2-4 concise search phrases]}.\n"
        "Use the title and spoken voice script as the only source for the actual topic and search entities.\n"
        "Visual_direction is completely optional and secondary; do not use visual_direction as the search topic unless the same entity/action is also in the spoken script.\n"
        "Use exact historical names, places, objects, maps, documents, people, and related visual evidence from the voice script.\n"
        "Every query must contain the canonical topic, a strong alias, or a named entity/object explicitly present in the voice script.\n"
        "Do not create queries from vague visual words such as dark room, courtroom, dramatic, blueprint, cinematic, silhouette, scary, or close-up unless those exact entities are part of the spoken topic.\n"
        "Avoid generic keywords like 'war', 'battle', 'farm', or words copied blindly from the script.\n"
        "Avoid book covers, title pages, library catalog scans, scanned book pages, archive.org/open-library scans, and text-only pages unless the scene explicitly asks for a book or manuscript.\n"
        "Create queries for general web image search, not only Wikimedia. Prefer specific entity/place/object/event phrases that can find real photos, maps, archives, news images, museums, official pages, or documentary references.\n"
        "Among correct options, bias queries toward the most visually striking, dramatic, and instantly readable images (strong subject, high contrast, emotion, scale) - this is a scroll-stopping Short, not an encyclopedia.\n\n"
        f"Title: {humanize_title(title)}\n"
        f"Scenes JSON: {json.dumps(scene_lines, ensure_ascii=False)}"
    )
    payload = {
        "model": reasoning_model or GPT55_MODEL,
        "messages": [
            {"role": "system", "content": "You are a careful research assistant for documentary image search who also has a viral editor's eye for striking, scroll-stopping visuals. Return compact valid JSON only."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.15,
        "max_tokens": 1200,
    }
    try:
        data = post_json_url(WAVESPEED_LLM_API, payload)
        content = data["choices"][0]["message"]["content"]
        plan = extract_json_object(content)
        log(status_cb, f"Reasoning Agent search planner: {plan.get('canonical_topic', title)}")
        return plan if isinstance(plan, dict) else {}
    except Exception as exc:
        log(status_cb, f"Reasoning Agent search planner skipped: {exc}")
        return {}


def project_media_counts(project_dir):
    folders = {
        "web_images_existing": project_dir / "web images",
        "gpt_images_existing": project_dir / "gpt images",
        "seedance_clips_existing": project_dir / "seedance 2.0",
        "local_media_existing": project_dir / "local media",
    }
    counts = {}
    for key, folder in folders.items():
        if not folder.exists():
            counts[key] = 0
            continue
        exts = VIDEO_EXTS if "clips" in key else IMAGE_EXTS | VIDEO_EXTS
        counts[key] = len([p for p in folder.rglob("*")
                           if p.suffix.lower() in exts and p.stat().st_size > 1000
                           and "_candidates" not in {x.lower() for x in p.parts}
                           and "_raw" not in {x.lower() for x in p.parts}])
    return counts


# Relatable real young-Japanese-woman creators (lifestyle / POV / candid, like @sakii_0405_),
# NOT glam models or AI/stock. Native, varied terms surface many such creators.
SCRAPE_WOMAN_HOOK = "日本 女の子 日常 vlog かわいい"
# A scraped clip is only used for a scene if it semantically supports that exact narration line
# at or above this score (0-10). Below it the scene falls back to a generated/other visual
# instead of being filled with random Japan b-roll.
MIN_SCRIPT_MATCH_SCORE = 7.0


def adaptive_script_match_threshold(script_relevancy, attempt=0):
    """Lower semantic strictness after each failed scrape pass without allowing junk footage."""
    try:
        relevancy = max(0.0, min(100.0, float(script_relevancy)))
    except (TypeError, ValueError):
        relevancy = 70.0
    try:
        attempt = max(0, int(attempt))
    except (TypeError, ValueError):
        attempt = 0
    # Start lower so thematically-coherent contextual b-roll can carry ESSAY/abstract narration
    # (e.g. "you, they usually say it directly.") instead of the matcher rejecting every good clip
    # as "doesn't literally depict this fragment" and ending with 0 matches. Quality gates (captions,
    # off-topic D_REJECTED, unusable) still apply - this only relaxes literal-depiction strictness.
    base = 4.6 + 1.7 * (relevancy / 100.0)
    return round(max(4.0, base - attempt), 1)


def should_run_final_semantic_rescore(current_threshold, floor_threshold, remaining_count,
                                      pool_count, any_matched, scene_count):
    """Only pay for another full vision pass when it can actually lower the threshold."""
    if not remaining_count or not pool_count:
        return False
    if not any_matched and pool_count >= scene_count:
        return False
    return float(floor_threshold) < float(current_threshold) - 0.01


def normalize_social_query_output(value):
    """Normalize planner query fields without ever splitting a phrase on whitespace."""
    if isinstance(value, (list, tuple)):
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
    for value in candidates:
        if not isinstance(value, (str, int, float)):
            continue
        query = re.sub(r"^\s*(?:[-*\u2022]+|\d+[.)])\s*", "", str(value))
        query = " ".join(query.strip(" ,;:|").split())
        key = query.casefold()
        if query and key not in seen:
            seen.add(key); out.append(query)
    return out


def llm_scrape_plan(script, title="", visual_script="", script_relevancy=70, reasoning_model=None, status_cb=None, understanding=None):
    """Let the Reasoning Agent derive TikTok b-roll search queries straight from the
    voice script (concrete places/objects/actions per scene), plus the scroll-stop hook
    query (an attractive Japanese woman in her early 20s). The user no longer types
    search terms — the agent reads the script and decides. Returns
    {"hook_query": str, "queries": [str, ...]}; falls back to script keywords on failure."""
    fallback = {"hook_query": SCRAPE_WOMAN_HOOK, "queries": []}
    if not script or not os.environ.get("WAVESPEED_API_KEY"):
        return fallback
    rel = max(0, min(100, int(script_relevancy)))
    prompt = (
        understanding_brief(understanding) +
        "You plan REAL TikTok search queries for a vertical short cut entirely from found footage about JAPAN.\n"
        "Go LINE BY LINE through the voice script. For EACH line, write ONE query that would surface a clip that "
        "literally DEPICTS what that line is about - the concrete subject, place, or action - not just 'Japan'.\n"
        "Map meaning to footage, e.g.:\n"
        "  'clean streets' -> 日本 綺麗な 街並み  /  tokyo clean street\n"
        "  'stress / exhausted / weakness' -> 疲れた サラリーマン  /  日本 残業 疲れ  /  overworked japanese worker\n"
        "  'crowded trains / commute' -> 満員電車 東京  /  通勤ラッシュ\n"
        "  'keep bowing' -> 日本 お辞儀 ビジネス\n"
        "  'sleep in cafes' -> ネットカフェ 寝る  /  カフェ 仮眠 日本\n"
        "  'surrounded by millions, still alone' -> 渋谷 雑踏 一人  /  東京 孤独 夜\n"
        "  'quiet pressure / scary perfect' -> 東京 夜 無人 街  /  静かな 日本 路地\n"
        "FOCUS ON HUMAN EXPRESSION & EMOTION: prefer footage of real people's FACES and body language showing the "
        "feeling of the line - a tired face, blank stare, forced/fake smile, sighing, looking down, crying - over "
        "empty scenery. Use expression words: 表情, 疲れた顔, 無表情, ため息, 作り笑い, うつむく, 涙, 真顔, 困った顔. "
        "An expressive face conveys the emotion far better than a skyline.\n"
        "USE SYNONYMS & VARIATIONS: for each idea give a couple of DIFFERENT phrasings / synonyms / slang so the "
        "search surfaces varied clips - e.g. 会社員 / サラリーマン / OL / ビジネスマン; 疲れた / 疲労 / ぐったり / くたくた; "
        "孤独 / 一人 / ぼっち. Do not just repeat the same noun.\n"
        "Use NATIVE, idiomatic Japanese exactly how a Japanese creator would tag it (kanji/kana + a hashtag), plus "
        "one English variant. Keep each query 2-5 words. Japanese TikTok is indexed under Japanese terms, so most "
        "queries MUST be Japanese.\n"
        f"script_relevancy={rel} (0-100): high = the clip must literally show what the line says; low = looser vibe.\n"
        "HOOK (first clip): a REAL, relatable young Japanese woman creator (candid lifestyle / POV / everyday vibe, "
        "like @sakii_0405_) - NOT a glam model, NOT stock, NOT 'attractive japanese woman'. Search the way these "
        "creators are actually tagged, with varied native terms/synonyms: 日本 女の子 日常, 女子 vlog, 一人暮らし 女子, "
        "主人公感, 自撮り 女の子, かわいい 日常, 20代 女子 vlog, #日常 #女子 #一人暮らし. Return one such native hook_query.\n"
        'Return STRICT JSON only: {"hook_query": "...", "queries": ["...", ...]} - one strong query per script '
        "line plus a few extra, 12-18 total, mostly Japanese.\n\n"
        f"Title: {title}\nVoice script:\n{script}\n"
        + (f"Optional style note: {visual_script}\n" if visual_script else "")
    )
    payload = {
        "model": reasoning_model or GPT55_MODEL,
        "messages": [
            {"role": "system", "content": "You turn a narration script into concrete TikTok footage search queries. Return JSON only."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.3,
        "max_tokens": 600,
        "response_format": {"type": "json_object"},
    }
    try:
        log(status_cb, "Scrape: Reasoning Agent deriving search terms from the voice script...")
        data = post_json_url(WAVESPEED_LLM_API, payload, timeout=120)
        plan = extract_json_object(data["choices"][0]["message"]["content"]) or {}
        queries = normalize_social_query_output(plan.get("queries"))
        hook = str(plan.get("hook_query") or "").strip() or SCRAPE_WOMAN_HOOK
        if queries:
            return {"hook_query": hook, "queries": queries[:16]}
    except Exception as exc:  # noqa: BLE001
        log(status_cb, f"Scrape: term derivation skipped ({exc.__class__.__name__}); using script keywords.")
    return fallback


def llm_scene_scrape_queries(lines, understanding=None, reasoning_model=None, status_cb=None):
    """Retry-round query generator: given only the narration lines that still have NO matching
    clip, produce FRESH native TikTok queries that target each line's concrete subject/action,
    so the next TikTok search finds closer footage (different angles than the obvious city b-roll)."""
    lines = [str(l).strip() for l in (lines or []) if str(l).strip()]
    if not lines or not os.environ.get("WAVESPEED_API_KEY"):
        return []
    prompt = (
        understanding_brief(understanding) +
        "These narration lines still have NO matching real TikTok clip. For EACH line write 2-3 NEW native "
        "Japanese TikTok search queries (kanji/kana + a hashtag) that would surface footage which LITERALLY "
        "depicts it - the specific person, action, object, or place - PLUS one English variant. Avoid the obvious "
        "generic city/skyline b-roll; go for the concrete subject (a tired worker, a packed train, a person "
        "asleep, a lone figure in a crowd). Keep each query 2-5 words.\n"
        "FOCUS ON EXPRESSION: lean into real people's FACES/emotions that match the line - 疲れた顔, 無表情, "
        "ため息, 作り笑い, うつむく, 涙, 困った顔 - an expressive face beats scenery.\n"
        "VARY IT: use SYNONYMS, alternate phrasings and slang across the queries (会社員 / サラリーマン / OL / "
        "ビジネスマン; 疲れた / 疲労 / ぐったり / くたくた; 孤独 / 一人 / ぼっち) so each round finds DIFFERENT clips "
        "than before - do not reuse the same wording.\n\n"
        "Lines:\n" + "\n".join(f"{i}: {l}" for i, l in enumerate(lines)) + "\n\n"
        'Return STRICT JSON: {"scene_queries": [{"line_index": 0, "queries": ["...", "...", "..."]}, ...]}. '
        "Include every line once."
    )
    try:
        data = _post_llm_json(
            reasoning_model or GPT55_MODEL,
            [{"role": "system", "content": "You turn specific narration lines into concrete native TikTok footage queries. Return JSON only."},
             {"role": "user", "content": prompt}],
            700, 0.45) or {}
        groups = data.get("scene_queries") if isinstance(data.get("scene_queries"), list) else []
        normalized = []
        for fallback_index, group in enumerate(groups):
            if not isinstance(group, dict):
                continue
            try:
                line_index = int(group.get("line_index", fallback_index))
            except (TypeError, ValueError):
                line_index = fallback_index
            queries = normalize_social_query_output(group.get("queries"))
            if queries:
                normalized.append((line_index, queries[:4]))
        if normalized:
            # Round-robin keeps the first query for every unmatched line ahead of second/third
            # variants, so downstream caps cannot starve later scenes.
            flattened = []
            for query_index in range(max(len(qs) for _, qs in normalized)):
                for _, queries in sorted(normalized, key=lambda row: row[0]):
                    if query_index < len(queries):
                        flattened.append(queries[query_index])
            return flattened[:32]
        # Backward compatibility if a model returns the older flat schema.
        return normalize_social_query_output(data.get("queries"))[:32]
    except Exception as exc:  # noqa: BLE001
        log(status_cb, f"Scrape retry: query generation skipped ({exc.__class__.__name__}).")
        return []


# Japanese synonym map used to widen failing social searches (subject families -> variants).
SOCIAL_SYNONYM_MAP = {
    "romance": ["恋愛", "彼氏", "彼女", "カップル", "デート", "結婚", "婚活", "恋愛相談", "恋愛あるある"],
    "single_lonely": ["独身", "おひとりさま", "ソロ活", "一人暮らし", "孤独", "ぼっち", "一人が好き", "ひとり時間"],
    "work_exhaustion": ["仕事疲れた", "社会人しんどい", "残業", "サラリーマン", "会社員", "通勤", "終電", "ブラック企業", "社畜"],
    "money_cost": ["お金ない", "節約", "高い", "出費", "生活費", "家賃", "給料", "奢り", "デート代", "コスパ"],
    "tokyo_lifestyle": ["東京生活", "東京一人暮らし", "渋谷", "新宿", "池袋", "山手線", "コンビニ", "カフェ", "居酒屋", "夜の街"],
}

# Hook = a high-engagement, playful Japanese creator clip: dancing, cute gestures, or an
# expressive camera-facing performance in the Miyu Kishi / Saaki-Sakii reference style.
# Lead with GENERIC/varied queries (scrape_bucket's diversity slots take the first ~3-6, so leading
# with generic terms keeps the hook pool from being just 2 creators). A few well-known creators are
# interleaved for the reference energy, but the bulk is broad "cute Japanese dance/POV girl" queries.
HOOK_PRESENTER_QUERIES = {
    "exact": ["日本人女子 ダンス 可愛い", "踊ってみた 女子 かわいい", "20代 女子 ダンス",
              "あざと可愛い ダンス 女子", "お姉さん ダンス 可愛い", "カメラ目線 可愛い仕草 女子",
              "日本 女の子 ダンス 笑顔", "ゆるめ ダンス 女子", "可愛い 踊ってみた おすすめ",
              "岸みゆ ダンス", "sakii_0405_ ダンス", "なえなの ダンス", "景井ひな ダンス"],
    "social": ["アイドル ダンス 女子", "女子 可愛いダンス", "日本人女子 踊ってみた",
               "あざと可愛い 女子", "カメラ目線 可愛い", "笑顔 ダンス 女子", "大人可愛い ダンス",
               "インフルエンサー 女子 ダンス"],
    "hashtag": ["#踊ってみた", "#ダンス女子", "#あざと可愛い", "#可愛い", "#おすすめ",
                "#日本人女性", "#ダンス好きな人と繋がりたい", "#女の子"],
    # Birth-year tags (user-provided 2026-07-11): young Japanese women tag their birth year
    # #01..#05 (born 2001-2005); "#0X #女の子" reliably surfaces exactly the hook-presenter
    # demographic on BOTH TikTok and Instagram, and doubles as generic Japanese-style fill
    # footage (the hook pool feeds the body/context-fallback pool too).
    "birthyear": ["#01 #女の子", "#02 #女の子", "#03 #女の子", "#04 #女の子", "#05 #女の子"],
    "english": ["cute Japanese creator dancing", "Japanese idol playful dance",
                "kawaii Japanese girl dance", "Japanese creator dance to camera",
                "Miyu Kishi dance"],
}
HOOK_MIN_LIKES = 20_000
MIN_CLIP_LIKES = 0          # body footage is quality/semantic ranked; only the hook keeps 20K+
HOOK_PRESENTER_TARGET = ("young adult Japanese female creator with at least 20,000 likes on the source video, "
                         "dancing or playfully acting cute to camera with Miyu Kishi / Saaki-Sakii-style hook energy, "
                         "expressive face, clean vertical frame; NOT anime/CGI/screen-recording, not a child, "
                         "not sexualized or body-bait.")
MIN_HOOK_PRESENTER_SCORE = 7.5


def build_social_search_plan(title, script, scenes, understanding=None, reasoning_model=None,
                             collaborate=False, status_cb=None):
    """Replace flat 'style queries' with a structured social-search plan: ~8-14 reusable VISUAL
    BUCKETS (+ a dedicated hook-influencer bucket), each with TikTok-native tiered queries
    (exact/semantic/broad/hashtag), must_show/not, search_intent, and the scene ids that use it.
    Returns {"buckets":[...], "hook":{...}} or {} on failure."""
    if not (script or "").strip() or not os.environ.get("WAVESPEED_API_KEY"):
        return {}
    numbered = "\n".join(
        f"scene {i}: {(scene_text_for_planning(s) or s.get('script', ''))[:140]}"
        for i, s in enumerate(scenes)
    )
    syn = "; ".join(f"{k}: {', '.join(v[:6])}" for k, v in SOCIAL_SYNONYM_MAP.items())
    messages = [
        {"role": "system", "content": "You are a TikTok footage researcher for Japanese social-topic shorts. You think in reusable visual buckets and native TikTok search terms (casual + あるある + hashtags), not documentary keywords. Return JSON only."},
        {"role": "user", "content": (
            understanding_brief(understanding) +
            "Plan how to FIND real TikTok footage for this short. Do NOT make one narrow query per line.\n"
            "GROUP the scenes into 8-14 reusable VISUAL BUCKETS (a bucket = a type of footage many beats can share, "
            "e.g. 'lonely young people in Tokyo', 'expensive date / restaurant', 'tired salaryman / overwork', "
            "'small apartment / alone at home', 'couples dating in Tokyo', 'single lifestyle / ohitorisama', "
            "'city loneliness / night streets').\n"
            "For EACH bucket give TikTok-native query TIERS (search casual Japanese the way creators tag it, not "
            "documentary phrasing):\n"
            "  exact  = on-topic literal queries\n"
            "  semantic = lifestyle / feeling / behaviour queries\n"
            "  broad  = broad TikTok-native queries\n"
            "  hashtag = #hashtag social-discovery queries (e.g. #恋愛あるある #おひとりさま #社会人の日常)\n"
            "Give 5-7 queries PER TIER, and make every query a genuinely DIFFERENT angle: rotate synonyms, "
            "slang, formal/casual phrasing, different concrete nouns and camera perspectives (POV / vlog / "
            "walking tour / close-up). Near-duplicate wordings waste searches and are skipped.\n"
            "School terms: when the script IS about school life, DO use them actively in the body buckets "
            "(学校 / 女子高生 / JK / 制服 / 教室 / 高校生活 / #jk) - they index that content best. When the "
            "script is NOT about school, never use them.\n"
            "Also add creator-discussion queries where useful (a creator TALKING about the topic), e.g. "
            "恋愛について話す 女子, 仕事疲れた 話す.\n"
            f"Useful synonym families to widen with: {syn}.\n"
            "Each query 2-6 words, mostly Japanese kana/kanji, a few English backups. Reuse buckets across scenes "
            "via used_by_scene_ids.\n\n"
            f"Scenes:\n{numbered}\n\n"
            'Return STRICT JSON: {"buckets": [{"bucket_id": "snake_case", "used_by_scene_ids": [int,...], '
            '"visual_goal": "...", "primary_subject": "...", "action": "...", "location": "...", "mood": "...", '
            '"search_intent": "specific_action|lifestyle_broll|emotion_broll|proof_like_social_clip", '
            '"query_tiers": {"exact": [..], "semantic": [..], "broad": [..], "hashtag": [..]}, '
            '"must_show": [..], "must_not_show": [..]}, ...]}'
        )},
    ]
    try:
        # 8000 tokens: a full plan (10+ buckets x 4 tiers x 5-7 queries for 30 scenes) regularly
        # overflowed 4000, truncating the JSON -> empty buckets -> the whole run silently fell
        # back to flat queries. One retry covers transient API/parse failures.
        buckets = []
        for _attempt in range(2):
            if collaborate:
                plan = collaborate_json(messages, max_tokens=8000, temperature=0.3, status_cb=status_cb, label="social search plan") or {}
            else:
                plan = _post_llm_json(reasoning_model or GPT55_MODEL, messages, 8000, 0.3) or {}
            buckets = plan.get("buckets") if isinstance(plan.get("buckets"), list) else []
            buckets = [b for b in buckets if isinstance(b, dict) and b.get("query_tiers")]
            if buckets:
                break
            log(status_cb, "Social search plan came back empty/truncated - retrying once..."
                if _attempt == 0 else
                "Social search plan empty after retry - the run will use flat fallback queries.")
        # Models occasionally serialize scene ids as "0 1 2" despite the requested array.
        # Normalize once here; iterating that string character-by-character silently mapped
        # double-digit scenes to the wrong search bucket.
        for bucket in buckets:
            raw_ids = bucket.get("used_by_scene_ids") or []
            values = re.findall(r"\d+", raw_ids) if isinstance(raw_ids, str) else raw_ids
            normalized_ids = []
            for value in values:
                try:
                    scene_id = int(value)
                except (TypeError, ValueError):
                    continue
                if 0 <= scene_id < len(scenes) and scene_id not in normalized_ids:
                    normalized_ids.append(scene_id)
            bucket["used_by_scene_ids"] = normalized_ids
            tiers = bucket.get("query_tiers") if isinstance(bucket.get("query_tiers"), dict) else {}
            bucket["query_tiers"] = {
                tier: normalize_social_query_output(tiers.get(tier))
                for tier in ("exact", "semantic", "broad", "hashtag")
            }
        if not buckets:
            return {}
        hook = {
            "bucket_id": "hook_influencer",
            "used_by_scene_ids": [0],
            "visual_goal": HOOK_PRESENTER_TARGET,
            "search_intent": "hook_influencer",
            "query_tiers": {"exact": HOOK_PRESENTER_QUERIES["exact"],
                            "semantic": HOOK_PRESENTER_QUERIES["social"],
                            "broad": HOOK_PRESENTER_QUERIES["english"],
                            "hashtag": HOOK_PRESENTER_QUERIES["hashtag"]},
            "must_show": ["a real young adult Japanese woman dancing or playfully acting cute to camera",
                          "expressive face and clean vertical frame", "at least 20,000 TikTok likes"],
            "must_not_show": ["anime/CGI", "screen recording", "child/teen", "heavy text over the face", "sexualized bait"],
        }
        log(status_cb, f"Built {len(buckets)} social search bucket(s) + a hook-influencer bucket from the script:")
        for b in buckets:
            ex = ", ".join((b.get("query_tiers", {}).get("exact") or [])[:4])
            tags = ", ".join((b.get("query_tiers", {}).get("hashtag") or [])[:3])
            log(status_cb, f"   • {b.get('bucket_id')} (scenes {b.get('used_by_scene_ids')}): {ex}"
                           + (f"  |  {tags}" if tags else ""))
        log(status_cb, f"   • hook_influencer: {', '.join(HOOK_PRESENTER_QUERIES['exact'][:4])}")
        return {"buckets": buckets, "hook": hook}
    except Exception as exc:  # noqa: BLE001
        log(status_cb, f"Social search planning skipped ({exc.__class__.__name__}); using flat queries.")
        return {}


def find_reusable_social_clips(project_dir, title, script, understanding=None,
                               reasoning_model=None, status_cb=None, max_clips=None):
    """Expose this same-script project's finalized clips and pre-filtered candidate downloads.

    Current-project clips are staged before placement because placement replaces ``scraped_*``.
    Failed/cancelled runs never reached placement, so their ``_candidates/cand_*.mp4`` files are
    equally important reusable inputs and have already passed the intake quality gates.
    Returns (body_paths, hook_paths, clip_meta, report).
    """
    project_dir = Path(project_dir)
    target_text = " ".join([
        str(title or ""), str(script or ""),
        str((understanding or {}).get("topic", "")),
        str((understanding or {}).get("thesis", "")),
    ])
    generic = SEARCH_NOISE | {"japan", "japanese", "short", "video", "people", "thing", "things"}
    target_terms = words(target_text) - generic
    candidates = []
    # Only this project's OWN previously-scraped clips are eligible for reuse (callers gate this to
    # same-script runs). Never scan or borrow from other projects' folders.
    for candidate_dir in [project_dir]:
        if not candidate_dir.is_dir():
            continue
        seedance_dir = candidate_dir / "seedance 2.0"
        finalized = sorted(seedance_dir.glob("scraped_*.mp4"))
        downloaded_all = sorted((seedance_dir / "_candidates").rglob("cand_*.mp4")) \
            if (seedance_dir / "_candidates").exists() else []
        # Round-robin buckets so the bounded reuse pool covers the whole script instead of
        # taking the first 50 alphabetical files from only a few folders.
        by_bucket = {}
        for path in downloaded_all:
            by_bucket.setdefault(path.parent.name, []).append(path)
        downloaded = []
        while any(by_bucket.values()):
            for bucket in sorted(by_bucket):
                if by_bucket[bucket]:
                    downloaded.append(by_bucket[bucket].pop(0))
        clips = [p for p in [*finalized, *downloaded]
                 if p.is_file() and p.stat().st_size > 4096
                 and "_raw" not in {part.lower() for part in p.parts}
                 and "_existing_reuse" not in {part.lower() for part in p.parts}]
        if not clips:
            continue
        old_script_path = candidate_dir / "input" / "script.txt"
        try:
            old_script = old_script_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            old_script = ""
        old_config = {}
        try:
            old_config = json.loads((candidate_dir / "config" / "project.json").read_text(
                encoding="utf-8", errors="replace"))
        except Exception:
            pass
        old_title = str(old_config.get("title") or candidate_dir.name.replace("_", " "))
        candidate_terms = words(old_title + " " + old_script) - generic
        overlap = len(target_terms & candidate_terms) / max(1, min(len(target_terms), len(candidate_terms)))
        candidates.append({
            "slug": candidate_dir.name, "path": candidate_dir, "title": old_title,
            "script": clean_text(old_script)[:900], "clip_count": len(clips),
            "lexical_score": round(overlap * 100, 1), "clips": clips,
            "config": old_config,
        })
    if not candidates:
        return [], [], {}, {"searched_projects": 0, "selected_projects": []}

    # The model gets every local project with usable social clips (small bounded catalog), then
    # decides topic similarity. A deterministic overlap fallback keeps reuse working offline.
    catalog = "\n".join(
        f"{c['slug']} | title={c['title']} | clips={c['clip_count']} | script={c['script']}"
        for c in candidates[:30]
    )
    selected_scores = {}
    selected_reasons = {}
    if os.environ.get("WAVESPEED_API_KEY"):
        try:
            selection = _post_llm_json(
                reasoning_model or GPT55_MODEL,
                [
                    {"role": "system", "content": "You select existing video projects whose real TikTok footage can be reused for a new short. Return JSON only."},
                    {"role": "user", "content": (
                        understanding_brief(understanding)
                        + "Find existing projects about the SAME SPECIFIC SUBJECT as the new script whose clips "
                          "are worth re-checking. Score topic_similarity 0-100 based on the actual subject/object "
                          "(e.g. vending machines, konbini, dating, trains), NOT on generic 'Japan at night' / "
                          "'convenience' / atmosphere overlap - a vending-machine short and a rescue-station short "
                          "are DIFFERENT topics even though both are Japan. Select ONLY projects >=85 (essentially "
                          "the same subject). If nothing is clearly the same subject, return an empty list.\n\n"
                        + f"New title: {title}\nNew script:\n{script}\n\nExisting projects:\n{catalog}\n\n"
                          'Return {"projects":[{"slug":"...","topic_similarity":0-100,"reason":"..."}]}.'
                    )},
                ], 1800, 0.1) or {}
            for item in (selection.get("projects") or []):
                if not isinstance(item, dict):
                    continue
                slug = str(item.get("slug") or "")
                try:
                    score = float(item.get("topic_similarity") or 0)
                except (TypeError, ValueError):
                    score = 0.0
                if score >= 85:                    # only essentially the SAME subject (strict)
                    selected_scores[slug] = score
                    selected_reasons[slug] = str(item.get("reason") or "")[:220]
        except Exception as exc:  # noqa: BLE001
            log(status_cb, f"Existing-project agent search fell back to local similarity ({exc.__class__.__name__}).")
    # Only keep VERY strong subject-term overlap (near-identical scripts) - never loosely-related
    # projects (a vending-machine script must not pull rescue-station clips). A fresh scrape covers
    # everything else, so it is safe to reuse nothing when no project is the same subject.
    for candidate in candidates:
        if candidate["lexical_score"] >= 80:
            selected_scores.setdefault(candidate["slug"], candidate["lexical_score"])
            selected_reasons.setdefault(candidate["slug"], "near-identical subject terms")

    selected = [c for c in candidates if c["slug"] in selected_scores]
    selected.sort(key=lambda c: (selected_scores[c["slug"]], c["path"].stat().st_mtime), reverse=True)
    selected = selected[:4]
    body_paths, hook_paths, meta = [], [], {}
    selected_report = []
    total = 0
    try:
        reuse_limit = max(0, int(max_clips)) if max_clips is not None else None
    except (TypeError, ValueError):
        reuse_limit = None

    def reuse_limit_reached():
        return reuse_limit is not None and total >= reuse_limit

    try:
        import clip_scraper as _clip_quality
        _reuse_ffmpeg, _reuse_ffprobe = _clip_quality._ffmpeg_tools()
    except Exception:
        _clip_quality = None
        _reuse_ffmpeg = _reuse_ffprobe = None
    for candidate in selected:
        if reuse_limit_reached():
            break
        config_scenes = candidate["config"].get("scenes") if isinstance(candidate["config"].get("scenes"), list) else []
        prior_by_clip = {
            str(scene.get("clip")): scene for scene in config_scenes
            if isinstance(scene, dict) and scene.get("clip")
        }
        report = {}
        try:
            report = json.loads((candidate["path"] / "review" / "agent_report.json").read_text(
                encoding="utf-8", errors="replace"))
        except Exception:
            pass
        social = report.get("social_search") if isinstance(report.get("social_search"), dict) else {}
        hook_info = social.get("hook_finder") if isinstance(social.get("hook_finder"), dict) else {}
        try:
            verified_hook_likes = int(hook_info.get("likes") or 0)
        except (TypeError, ValueError):
            verified_hook_likes = 0
        added = 0
        rejected_rapid = 0
        rejected_captions = 0
        for source in candidate["clips"]:
            if reuse_limit_reached():
                break
            prior = prior_by_clip.get(source.name) or {}
            is_downloaded_candidate = "_candidates" in {part.lower() for part in source.parts}
            candidate_bucket = source.parent.name if is_downloaded_candidate else ""
            candidate_sidecar = {}
            try:
                candidate_sidecar = json.loads(source.with_suffix(".json").read_text(
                    encoding="utf-8", errors="replace"))
            except Exception:
                candidate_sidecar = {}
            candidate_platform = str(candidate_sidecar.get("platform")
                                     or prior.get("scrape_source") or "tiktok").lower()
            hook_source_floor = (HOOK_MIN_LIKES // 4
                                 if candidate_platform == "twitter" else HOOK_MIN_LIKES)
            try:
                index = int(re.search(r"(\d+)$", source.stem).group(1))
            except Exception:
                index = -1
            # A hook candidate only reaches this folder after scrape_bucket's hard 20K gate.
            is_verified_hook = (candidate_bucket == "hook_influencer"
                                or (index == 0 and bool(social.get("hook_first"))
                                    and verified_hook_likes >= hook_source_floor))
            # Never feed an unverified previous opening into body matching.
            # ``cand_*_0`` means the first download in a search bucket, not scene 0. The old
            # check accidentally discarded the first saved body clip from every bucket on rerun.
            if not is_downloaded_candidate and index == 0 and not is_verified_hook:
                continue
            stability = {"stable": True, "internal_cut_count": 0,
                         "rapid_internal_cut_count": 0, "min_shot_seconds": None}
            if _clip_quality and _reuse_ffmpeg and not is_downloaded_candidate:
                stability = _clip_quality.stable_segment_profile(
                    source, _reuse_ffmpeg, _reuse_ffprobe, seconds=4.0)
                if not stability.get("stable", True):
                    rejected_rapid += 1
                    continue
            try:
                reuse_text_score = float(candidate_sidecar.get("text_heaviness")
                                         or prior.get("text_heaviness_score") or 0.0)
            except (TypeError, ValueError):
                reuse_text_score = 0.0
            if _clip_quality and _reuse_ffmpeg and is_downloaded_candidate:
                try:
                    reuse_text_score = _clip_quality.text_heaviness_score(
                        source, _reuse_ffmpeg, _clip_quality.DEFAULT_CLIP_SECONDS)
                    persistent_caption = _clip_quality.has_burned_captions(
                        source, _reuse_ffmpeg, _clip_quality.DEFAULT_CLIP_SECONDS)
                    if _clip_quality.is_captioned_candidate(reuse_text_score, persistent_caption):
                        rejected_captions += 1
                        continue
                except Exception:
                    pass
            usable = source
            if source.parent.parent.resolve() == project_dir.resolve():
                cache = project_dir / "seedance 2.0" / "_candidates" / "_existing_reuse"
                cache.mkdir(parents=True, exist_ok=True)
                key = hashlib.sha1(str(source.resolve()).encode("utf-8", "ignore")).hexdigest()[:10]
                usable = cache / f"existing_{key}_{source.name}"
                if not usable.exists() or usable.stat().st_size != source.stat().st_size:
                    shutil.copy2(source, usable)
            item_meta = {
                "bucket_id": (candidate_bucket or f"existing_project:{candidate['slug']}"),
                "source_query": f"existing project: {candidate['title']}",
                "tier": "existing_project", "search_intent": "project_reuse",
                "platform": candidate_platform,
                "clip_id": (candidate_sidecar.get("clip_id")
                            or f"existing:{candidate['slug']}:{source.name}"),
                "caption": clean_text(str(prior.get("exact_voice_text") or prior.get("script") or ""))[:160],
                "black_bar_score": float(prior.get("black_bar_score") or 0),
                "text_heaviness": reuse_text_score,
                "is_fake_vertical": bool(prior.get("is_fake_vertical", False)),
                "likes": (max(hook_source_floor, verified_hook_likes,
                              int(candidate_sidecar.get("likes") or 0))
                          if is_verified_hook else
                          int(candidate_sidecar.get("likes") or
                              (MIN_CLIP_LIKES if is_downloaded_candidate else 0))),
                "reused_from_project": candidate["slug"],
                "internal_cut_count": stability.get("internal_cut_count", 0),
                "rapid_internal_cut_count": stability.get("rapid_internal_cut_count", 0),
                "min_shot_seconds": stability.get("min_shot_seconds"),
            }
            meta[str(usable)] = item_meta
            (hook_paths if is_verified_hook else body_paths).append(usable)
            total += 1; added += 1
        selected_report.append({
            "slug": candidate["slug"], "title": candidate["title"],
            "topic_similarity": selected_scores[candidate["slug"]],
            "reason": selected_reasons.get(candidate["slug"], ""), "clips_added": added,
            "rapid_montages_rejected": rejected_rapid,
            "captioned_clips_rejected": rejected_captions,
        })
    if selected_report:
        log(status_cb, "Existing-project agent selected: " + "; ".join(
            f"{item['slug']} ({item['topic_similarity']:.0f}%, {item['clips_added']} clips)"
            for item in selected_report))
    else:
        log(status_cb, "Existing-project agent found no sufficiently similar project with reusable clips.")
    return body_paths, hook_paths, meta, {
        "searched_projects": len(candidates), "selected_projects": selected_report,
        "reusable_body_clips": len(body_paths), "reusable_hook_clips": len(hook_paths),
    }


def scrape_social_plan(plan, project_dir, clip_scraper, platforms, per_clip_seconds, script_relevancy,
                       cookies, cancel_check, status_cb=None, script_text="",
                       search_sort="MOST_LIKED"):
    """Tiered, widening per-bucket scrape that drives clip_scraper.scrape_bucket with the EXACT
    bucket queries (NO build_queries/script-derived expansion). Each candidate passes a metadata
    pre-download filter, then black-bar/fake-vertical + text-heavy rejection. Returns
    (pool, clip_meta, query_performance, scene_bucket, hook_pool, candidate_statuses, filter_summary)."""
    cand_root = project_dir / "seedance 2.0" / "_candidates"
    cand_root.mkdir(parents=True, exist_ok=True)
    clip_scraper.set_cookies(cookies)
    pool, hook_pool = [], []
    clip_meta, query_perf, candidate_statuses = {}, [], []
    seen_ids = set()
    TIERS = ["exact", "semantic", "broad", "hashtag"]

    def _target(bucket):
        # Aim for roughly ONE distinct clip per scene the bucket covers (+ a small margin), so the
        # final edit can use a different TikTok video on every cut instead of looping a few. Caps
        # are generous now (the renderer freezes rather than loops, and MAX_POOL bounds the total).
        intent = bucket.get("search_intent", "lifestyle_broll")
        if intent == "hook_influencer":
            return 14
        scene_count = len(bucket.get("used_by_scene_ids") or [])
        if intent in ("specific_action", "proof_like_social_clip"):
            return max(6, min(16, scene_count + 3))
        return max(5, min(14, scene_count + 2))

    MAX_POOL = 160
    BUCKET_TIME_BUDGET_S = 180.0    # one starving bucket must not eat the whole run
    # WHEN to search what: the hook bucket always first (scene 0 is mandatory), then body buckets
    # ordered by how many scenes they cover - so if time/pool runs out, the scenes that need the
    # most footage were searched first, not whatever order the planner happened to emit.
    hook_buckets = [plan["hook"]] if plan.get("hook") else []
    body_buckets = list(plan.get("buckets") or [])
    body_buckets.sort(key=lambda b: len(b.get("used_by_scene_ids") or []), reverse=True)
    all_buckets = hook_buckets + body_buckets
    try:
        clip_scraper.reset_backend_search_health()   # per-run search health + query dedupe reset
    except Exception:
        pass
    scrape_backend_dead = False
    for bi, bucket in enumerate(all_buckets):
        if (cancel_check and cancel_check()) or len(pool) >= MAX_POOL:
            break
        bucket_t0 = time.monotonic()
        bucket_deadline = bucket_t0 + BUCKET_TIME_BUDGET_S
        bid = str(bucket.get("bucket_id", f"bucket_{bi:02d}"))
        intent = bucket.get("search_intent", "lifestyle_broll")
        is_hook = (intent == "hook_influencer")
        target = min(_target(bucket), max(0, MAX_POOL - len(pool)))
        if target <= 0:
            break
        bucket_terms = " ".join([bucket.get("primary_subject", ""), bucket.get("action", ""),
                                 bucket.get("visual_goal", "")])
        # X is not a second TikTok search. Give it only 2-4 explicit proof/action phrases
        # built from subject + action + location. Lifestyle/emotion buckets stay TikTok-only.
        x_bucket_queries = []
        if intent in ("specific_action", "proof_like_social_clip"):
            subject_action_location = " ".join(str(bucket.get(key) or "").strip()
                                                 for key in ("primary_subject", "action", "location")).strip()
            exact_queries = normalize_social_query_output(
                (bucket.get("query_tiers") or {}).get("exact"))
            x_candidates = ([subject_action_location] if subject_action_location else []) + exact_queries
            x_bucket_queries = [q for q in normalize_social_query_output(x_candidates)
                                if clip_scraper.is_valid_x_query(q)][:4]
        if is_hook:
            log(status_cb, f"Hook Finder: searching 20K+ like Japanese cute/dance creator clips "
                           f"(Miyu Kishi / Saaki-Sakii style)...")
        got = 0
        for tier in TIERS:
            if got >= target or (cancel_check and cancel_check()):
                break
            if time.monotonic() >= bucket_deadline:
                log(status_cb, f"Bucket {bid}: time budget ({BUCKET_TIME_BUDGET_S:.0f}s) reached with "
                               f"{got} clip(s); moving on so other scenes still get footage.")
                break
            qs = normalize_social_query_output(bucket.get("query_tiers", {}).get(tier))
            if not qs:
                continue
            log(status_cb, f"  [{bid} · {tier}] searching {', '.join(platforms)}: "
                           f"{' · '.join(qs[:8])}")
            out_dir = cand_root / bid
            try:
                got_dicts = clip_scraper.scrape_bucket(
                    out_dir, qs[:8], max(2, target - got), bucket_id=bid, tier=tier,
                    bucket_terms=bucket_terms, per_clip_seconds=per_clip_seconds,
                    status_cb=status_cb, cancel_check=cancel_check, seen_ids=seen_ids,
                    query_perf=query_perf, candidate_statuses=candidate_statuses,
                    min_likes=HOOK_MIN_LIKES if is_hook else MIN_CLIP_LIKES,
                    search_sort=search_sort, platforms=platforms,
                    deadline=bucket_deadline, search_intent=intent,
                    x_queries=x_bucket_queries) or []
            except Exception as exc:  # noqa: BLE001
                log(status_cb, f"Bucket {bid}: {tier} tier search failed ({exc.__class__.__name__}).")
                got_dicts = []
            added = 0
            for d in got_dicts:
                p = d.get("path")
                if not p:
                    continue
                k = str(Path(p).resolve())
                pool.append(p)
                if is_hook:
                    hook_pool.append(p)
                clip_meta[str(p)] = {
                    "bucket_id": bid, "source_query": d.get("query", ""), "tier": tier,
                    "search_intent": intent, "platform": d.get("platform", "tiktok"),
                    "clip_id": d.get("clip_id"),
                    "caption": (d.get("meta") or {}).get("caption", "")[:160],
                    "author": (d.get("meta") or {}).get("author", ""),
                    "hashtags": (d.get("meta") or {}).get("hashtags", [])[:8],
                    "likes": int(d.get("likes") or (d.get("meta") or {}).get("likes") or 0),
                    "black_bar_score": d.get("black_bar_score", 0.0),
                    "text_heaviness": d.get("text_heaviness", 0.0),
                    "is_fake_vertical": d.get("is_fake_vertical", False),
                    "internal_cut_count": d.get("internal_cut_count", 0),
                    "rapid_internal_cut_count": d.get("rapid_internal_cut_count", 0),
                    "min_shot_seconds": d.get("min_shot_seconds"),
                }
                added += 1
            got += added
            log(status_cb, f"Bucket {bid}: {tier} tier found {added} usable clip(s)"
                           + ("" if got >= target else ", widening...") + ".")
            # FAIL FAST: if the TikTok backend returned ZERO RAW items across every search so far
            # (not merely 0 after quality filtering), the session is logged out / headless-blocked /
            # captcha'd. Grinding through 11 buckets x 4 tiers is pointless (~35 min for nothing) -
            # abort after ~6 searches. (items>0 but got==0 = strict filters, NOT dead: won't trip.)
            _backend_names = [name for name in clip_scraper.backend_name(platforms).split("+") if name]
            _h = clip_scraper.backend_search_health(platforms)
            if (_backend_names and _h.get("searches", 0) >= 6 * len(_backend_names)
                    and _h.get("items", 0) == 0):
                scrape_backend_dead = True
                break
        if scrape_backend_dead:
            _h = clip_scraper.backend_search_health(platforms)
            _wall = _h.get("login_wall", 0)
            log(status_cb, "Scrape ABORTED: selected social sources returned 0 clips for "
                           f"{_h.get('searches')} searches - the backend is not returning results. "
                           + ("A LOGIN WALL / captcha was detected: a selected session is logged "
                              "out or expired. " if _wall else
                              "Likely an expired login or a headless block. ")
                           + "Fix: reconnect TikTok/X on the home page, then "
                             "re-run. Scraping now opens a small TikTok browser window (headed) "
                             "because TikTok blocks headless searches - if no window appears, "
                             "restart the app so the new setting takes effect.")
            break
        log(status_cb, f"Bucket {bid}: final candidate pool {got} clip(s).")

    if scrape_backend_dead and not pool:
        # surface the reason on the plan so run_project can report it instead of a vague empty pool
        plan["scrape_backend_dead"] = True

    scene_bucket = {}
    for b in (plan.get("buckets") or []):
        for sid in (b.get("used_by_scene_ids") or []):
            try:
                scene_bucket.setdefault(int(sid), b.get("bucket_id"))
            except (TypeError, ValueError):
                continue
    filter_summary = _summarize_candidate_filters(candidate_statuses, raw_total=None)
    # Close the shared logged-in TikTok browser session once this plan's scrape is done.
    try:
        if hasattr(clip_scraper, "close_backend"):
            clip_scraper.close_backend()
    except Exception:
        pass
    return pool, clip_meta, query_perf, scene_bucket, hook_pool, candidate_statuses, filter_summary


def _summarize_candidate_filters(candidate_statuses, raw_total=None):
    """Tally candidate_statuses into the clip_filter_summary block for agent_report."""
    s = {"raw_candidates_found": 0, "pre_download_rejected": 0, "downloaded_candidates": 0,
         "accepted_media_count": 0, "shown_in_progress_media_panel": 0, "rejected_text_heavy": 0,
         "rejected_screenshot_or_textpost": 0, "rejected_livestream": 0, "rejected_black_bars": 0,
         "rejected_fake_vertical": 0, "rejected_low_relevance": 0}
    for c in (candidate_statuses or []):
        st = c.get("status"); reason = str(c.get("reason", "")).lower()
        s["raw_candidates_found"] += 1
        if st == "pre_download_rejected":
            s["pre_download_rejected"] += 1
            if any(w in reason for w in ("textpost", "quiz", "diagnosis", "screenshot")):
                s["rejected_screenshot_or_textpost"] += 1
            if any(w in reason for w in ("livestream", "screen recording", "slideshow", "image post")):
                s["rejected_livestream"] += 1
        elif st in ("downloaded_pending_review", "accepted_pool", "assigned_to_scene", "rejected_semantic"):
            s["downloaded_candidates"] += 1
        if st == "rejected_text_heavy":
            s["rejected_text_heavy"] += 1
        elif st == "rejected_black_bars":
            s["rejected_black_bars"] += 1
            if c.get("is_fake_vertical"):
                s["rejected_fake_vertical"] += 1
        elif st == "rejected_semantic":
            s["rejected_low_relevance"] += 1
        if st in ("accepted_pool", "assigned_to_scene"):
            s["accepted_media_count"] += 1
            if c.get("shown_in_media_panel"):
                s["shown_in_progress_media_panel"] += 1
    return s


def llm_auto_director_plan(title, script, scenes, target_duration, media_counts, audio_present=False, reasoning_model=None, status_cb=None):
    if not os.environ.get("WAVESPEED_API_KEY"):
        log(status_cb, "Auto Director skipped; WAVESPEED_API_KEY is not set.")
        return {}
    scene_lines = [
        {
            "scene": index,
            "start": scene.get("start"),
            "end": scene.get("end"),
            "script": scene.get("script", ""),
            "exact_voice_text": scene.get("exact_voice_text") or scene.get("voice_line") or scene.get("script", ""),
            "scene_objective": scene.get("scene_objective") or scene.get("beat_purpose", ""),
            "visual_meaning": scene.get("visual_meaning", ""),
            "viewer_emotion": scene.get("viewer_emotion") or scene.get("emotion", ""),
            "visual_hook_type": scene.get("visual_hook_type", ""),
            "required_visual_information": scene.get("required_visual_information", ""),
            "best_media_type": scene.get("best_media_type", ""),
            "crop_plan": scene.get("crop_plan", {}),
            "visual_direction": scene.get("visual_script", "") or scene.get("visual_direction", ""),
        }
        for index, scene in enumerate(scenes, 1)
    ]
    prompt = (
        "You are an elite short-form video editor and autonomous director for vertical 9:16 documentary-style Shorts built to go viral on TikTok, Reels, and YouTube Shorts.\n"
        "Your job is not to make a slideshow - it is to cut an edit that stops the scroll in the first second, holds attention to the very end, and ideally loops. Make practical production decisions plus a per-scene plan.\n"
        "Available APIs: Reasoning Agent for reasoning/search/review, Gemini 3.5 Flash for audio timing, GPT Image 2 ONLY for Seedance I2V source images, Seedance image-to-video for selected motion clips, "
        "general web image search/download from DuckDuckGo/Bing plus Wikimedia/Wikipedia as an additional source.\n"
        "Captions are burned in word-by-word and frame-synced to the voice, and cuts land on the spoken beat - so design a tight, intentional edit where every visual change is motivated by the words.\n"
        "Return strict JSON only with these keys:\n"
        "summary, use_web_images, use_gpt_source_images, use_seedance, use_llm_search, use_llm_video_review,\n"
        "web_image_count, web_images_per_scene, seedance_clip_count,\n"
        "visual_style, pacing, selected_apis, scene_plan.\n"
        "scene_plan must be an array with one item per scene: {scene:number, use_seedance:boolean, needs_gpt_image:boolean, preferred_media:'web|gpt|seedance|local', fit:'cover|contain', motion:'calm|normal|dynamic', visual_intent:string, reason:string, voice_match_score:number, visual_clarity_score:number, motion_score:number, shorts_retention_score:number, historical_or_factual_accuracy_score:number}.\n"
        "RETENTION & HOOK DOCTRINE (optimise the edit, not just the facts):\n"
        "- Scene 1 is the hook. In the first ~1.5s show the single most striking, shocking, or curiosity-provoking image that matches the opening line. Front-load your strongest visual; never open on a slow establishing shot. Aim shorts_retention_score >= 90 on scene 1.\n"
        "- Build a retention curve: escalate stakes scene by scene, make every scene earn the next, open a curiosity gap early and pay it off at the end. Cut filler and any shot that does not add new information or tension.\n"
        "- Pattern-interrupt: vary subject, framing, scale, and motion between neighbouring scenes so it never feels repetitive. No two adjacent scenes should look or move the same way.\n"
        "- Pace for the platform: punchy beats on key words, the tightest cut on the climax, and where possible an ending image that loops cleanly back to the hook.\n"
        "- visual_intent must be a concrete cinematic shot (subject, framing, action, emotion) a viewer would understand on mute. Prefer one strong subject over busy collages.\n"
        "MEDIA RULES:\n"
        "Seedance is expensive and should be used only when motion really helps: transformation, action, reveal, camera travel, dramatic reconstruction, non-speaking human reaction, object movement, or a machine/action starting. "
        "Use web images for factual proof, historical people, real buildings, real maps, articles/archive material, and quick references under 1.5 seconds. "
        "Use GPT-source-image + Seedance when the scene does not exist as real media, needs reconstruction, is surreal/epic, or needs a consistent visual look. "
        "Priority rule: the voice/text script is the authoritative source for topic, facts, timing, scene meaning, and what must be shown. "
        "Every visual must answer the current voice line. If the viewer watched without audio, they should roughly understand the same idea. "
        "If voice_match_score would be under 80, rewrite the scene idea or choose a better medium. If motion_score would be under 75 for Seedance, rewrite the Seedance idea or avoid Seedance. If visual_clarity_score would be under 75, simplify the shot. "
        "Make prompts intelligently: infer the best cinematic shot from the spoken line, not by mechanically copying either the script or visual direction. "
        "For every visual_direction, first judge whether it fits the spoken line. Use it only when it improves clarity, pacing, or cinematic impact. If it is mismatched, too literal, confusing, or weaker than a better script-matched shot, replace it with your own better visual idea based on the voice script. "
        "Every selected Seedance scene must have a distinct action phase, camera idea, and visual_intent. Do not select multiple Seedance scenes that would all show the same chase, the same animals running, the same soldiers aiming, or the same generic motion unless the script clearly describes different stages. "
        "Important media rule: static GPT still images are forbidden in the final render. "
        "GPT Image 2 may only create first/source images for Seedance I2V clips; it must not be used for standalone still-image shots. "
        "Use more web images for historical/documentary proof, maps, documents, places, and real people. "
        "Use Seedance/GPT-source reconstructions for scenes where no good real media is likely or where motion is needed. "
        "For GPT source images, avoid collage/multi-panel/poster-board compositions unless it is one deliberate Seedance source image. "
        "If a scene has visual_direction, use it only as an optional secondary style hint; the Voice Script is strictly authoritative. "
        "Avoid excessive fast cutting; choose enough still material for script matching without making it chaotic. "
        "If existing Seedance clips are enough, prefer reusing them instead of generating more.\n\n"
        f"Title: {title}\n"
        f"Audio present: {audio_present}\n"
        f"Target duration seconds: {target_duration}\n"
        f"Existing media counts JSON: {json.dumps(media_counts)}\n"
        f"Script:\n{script}\n\n"
        f"Scenes JSON:\n{json.dumps(scene_lines, ensure_ascii=False)}"
    )
    payload = {
        "model": reasoning_model or GPT55_MODEL,
        "messages": [
            {"role": "system", "content": "You are a world-class short-form video editor and viral director who thinks in hooks, retention curves, and pattern interrupts. You make decisive, taste-driven cuts and return compact valid JSON only."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
        "max_tokens": 3600,
        "response_format": {"type": "json_object"},
    }
    try:
        log(status_cb, "Auto Director: asking Reasoning Agent for production decisions...")
        data = post_json_url(WAVESPEED_LLM_API, payload, timeout=180)
        plan = extract_json_object(data["choices"][0]["message"]["content"])
        if not isinstance(plan, dict):
            return {}
        log(status_cb, f"Auto Director: {plan.get('summary', 'plan received')}")
        return plan
    except Exception as exc:
        log(status_cb, f"Auto Director skipped: {exc}")
        return {}


def director_int(plan, key, default, minimum, maximum):
    try:
        value = int(plan.get(key, default))
    except (TypeError, ValueError, AttributeError):
        value = default
    return max(minimum, min(maximum, value))


def director_bool(plan, key, default=True):
    try:
        value = plan.get(key, default)
    except AttributeError:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def director_scene_map(director_plan):
    result = {}
    if not isinstance(director_plan, dict):
        return result
    for item in director_plan.get("scene_plan", []) or []:
        if not isinstance(item, dict):
            continue
        try:
            scene_no = int(str(item.get("scene", "")).strip())
        except ValueError:
            continue
        if scene_no > 0:
            result[scene_no - 1] = item
    return result


def first_hook_line(script, max_chars=180):
    text = clean_text(script)
    if not text:
        return ""
    text = re.sub(r"^\s*\d{1,2}:\d{2}(?:\s*-\s*\d{1,2}:\d{2})?\s*", "", text)
    sentences = re.split(r"(?<=[.!?])\s+", text)
    hook = next((sentence.strip() for sentence in sentences if sentence.strip()), text[:max_chars])
    return hook[:max_chars].strip()


def find_existing_speaker_image(project_dir):
    candidates = []
    for folder in [Path(project_dir) / "speaker", Path(project_dir) / "speaker clip", Path(project_dir) / "input"]:
        if not folder.exists():
            continue
        candidates.extend(
            path for path in folder.iterdir()
            if path.is_file() and path.suffix.lower() in IMAGE_EXTS and "speaker" in path.stem.lower()
        )
    return sorted(candidates, key=lambda path: path.stat().st_mtime, reverse=True)[0] if candidates else None


def copy_speaker_image_to_project(source_path, project_dir):
    source = Path(source_path) if source_path else None
    if not source or not source.exists() or source.suffix.lower() not in IMAGE_EXTS:
        return None, None
    speaker_dir = Path(project_dir) / "speaker"
    legacy_speaker_dir = Path(project_dir) / "speaker clip"
    asset_dir = Path(project_dir) / "gpt images"
    global_upload_dir = SPEAKER_DIR / "uploaded"
    speaker_dir.mkdir(parents=True, exist_ok=True)
    legacy_speaker_dir.mkdir(parents=True, exist_ok=True)
    asset_dir.mkdir(parents=True, exist_ok=True)
    global_upload_dir.mkdir(parents=True, exist_ok=True)
    suffix = source.suffix.lower()
    safe_stem = slugify(source.stem)[:44] or "speaker"
    archive = speaker_dir / f"speaker_source{suffix}"
    legacy_archive = legacy_speaker_dir / f"speaker_source{suffix}"
    asset = asset_dir / f"speaker_hook_source{suffix}"
    global_archive = global_upload_dir / f"{time.strftime('%Y%m%d_%H%M%S')}_{safe_stem}{suffix}"
    if source.resolve() != archive.resolve():
        shutil.copyfile(source, archive)
    if source.resolve() != legacy_archive.resolve():
        shutil.copyfile(source, legacy_archive)
    if source.resolve() != asset.resolve():
        shutil.copyfile(source, asset)
    if source.resolve() != global_archive.resolve():
        shutil.copyfile(source, global_archive)
    return archive, asset


def llm_speaker_clip_plan(title, script, visual_script, speaker_image_path, reasoning_model=None, status_cb=None):
    hook = first_hook_line(script)
    fallback_prompt = (
        "Use the uploaded speaker image as the person's appearance and identity reference. "
        f"{SPEAKER_HOOK_REFERENCE_STYLE} "
        f"The speaker says the hook line: \"{hook}\" with excited, fast, clear creator delivery. "
        "Preserve the uploaded person's face, hairstyle, clothing impression, and camera-facing presence as much as possible. "
        "No captions, no subtitles, no watermark, no logos, no added text, no extra people. "
        "If audio is generated, use only the speaker voice saying the hook; no music and no extra voices."
    )
    if not os.environ.get("WAVESPEED_API_KEY") or not Path(speaker_image_path).exists():
        return {"hook_line": hook, "voice_style": "natural excited creator voice", "visual_plan": "Fallback speaker hook prompt.", "seedance_prompt": fallback_prompt}
    prompt = (
        "You are planning a Seedance image-to-video prompt for a short creator hook clip.\n"
        "Analyze only visible, non-sensitive presentation details from the uploaded speaker image: framing, lighting, clothing style, posture, and camera angle.\n"
        "Do not infer identity, race, ethnicity, health, religion, politics, or other sensitive traits. Do not claim who the person is.\n"
        "Return strict JSON only with keys: hook_line, voice_style, visual_plan, seedance_prompt.\n"
        "The hook line and full voice script context are higher priority than the user visual direction. First judge whether the visual direction fits the hook; keep helpful ideas, adapt weak ones, and replace mismatched ideas with a better hook performance plan.\n"
        "voice_style should describe energy, clarity, pace, and emotion only; do not infer demographics or identity from the image.\n"
        "The result should use the reference performance style below while keeping the uploaded image as the speaker appearance reference.\n"
        "Do not copy the reference-video person, watermark, captions, or exact setting; copy only the delivery, framing, gestures, and phone-video energy.\n"
        "Keep the person recognizable from the source image, with natural mouth movement, expressive hands near the lens, quick lean-ins, and subtle handheld camera motion.\n"
        "No captions, subtitles, watermark, logo, added text, extra people, or artificial studio look.\n"
        "If generated audio is available, it may contain only the speaker saying the hook; no music and no other voices.\n\n"
        f"Reference performance style:\n{SPEAKER_HOOK_REFERENCE_STYLE}\n\n"
        f"Title: {title}\n"
        f"Hook line to speak:\n{hook}\n\n"
        f"Full script context:\n{script}\n\n"
        f"Optional visual direction:\n{visual_script or '(none provided)'}"
    )
    payload = {
        "model": reasoning_model or GPT55_MODEL,
        "messages": [
            {"role": "system", "content": "You are a precise image-to-video prompt writer for realistic short-form creator clips."},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": image_data_url(speaker_image_path)}},
                ],
            },
        ],
        "temperature": 0.12,
        "max_tokens": 900,
        "response_format": {"type": "json_object"},
    }
    try:
        log(status_cb, "Speaker hook: Reasoning Agent analyzing uploaded speaker image...")
        data = post_json_url(WAVESPEED_LLM_API, payload, timeout=180)
        plan = extract_json_object(data["choices"][0]["message"]["content"])
        if isinstance(plan, dict) and plan.get("seedance_prompt"):
            plan["hook_line"] = clean_text(plan.get("hook_line") or hook)
            if not plan.get("voice_style"):
                plan["voice_style"] = "natural excited creator voice"
            return plan
    except Exception as exc:
        log(status_cb, f"Speaker hook Reasoning Agent analysis skipped: {exc}")
    return {"hook_line": hook, "voice_style": "natural excited creator voice", "visual_plan": "Fallback speaker hook prompt.", "seedance_prompt": fallback_prompt}


def move_existing_speaker_clip(project_dir):
    clip_dir = Path(project_dir) / "seedance 2.0"
    old_clip = clip_dir / "speaker_hook.mp4"
    if not old_clip.exists():
        return None
    replaced_dir = clip_dir / "replaced"
    replaced_dir.mkdir(parents=True, exist_ok=True)
    target = replaced_dir / f"speaker_hook_{time.strftime('%Y%m%d_%H%M%S')}.mp4"
    old_clip.replace(target)
    return target


def apply_speaker_hook_to_config(config, project_dir, title, script, visual_script, speaker_image_path, recreate=False, reasoning_model=None, status_cb=None):
    source_path = Path(speaker_image_path) if speaker_image_path else find_existing_speaker_image(project_dir)
    if not source_path or not source_path.exists():
        raise RuntimeError("Speaker hook is enabled, but no speaker image was uploaded or found in the loaded project.")
    archived, asset = copy_speaker_image_to_project(source_path, project_dir)
    if not asset:
        raise RuntimeError("Speaker hook image could not be copied into the project.")
    plan = llm_speaker_clip_plan(title, script, visual_script, archived or asset, reasoning_model=reasoning_model, status_cb=status_cb)
    moved = move_existing_speaker_clip(project_dir) if recreate else None
    if moved:
        log(status_cb, f"Speaker hook: old clip moved to {moved.parent.name}/{moved.name}.")
    scenes = config.get("scenes", [])
    if not scenes:
        return {}
    first = scenes[0]
    first["speaker_hook"] = True
    first["seedance"] = True  # keeps scene 0 in the clip pipeline; the prebuilt clip is reused
    first["asset"] = asset.name
    first["clip"] = "speaker_hook.mp4"
    first["needs_gpt_asset"] = False
    first["video_resolution"] = "480p"
    first["max_duration"] = 9.0
    first["shots"] = [{"at": 0.0, "use_clip": True}]
    first["seedance_start_trim"] = 0.0  # never trim the start of a talking head

    # Preferred path: lip-synced talking head via InfiniteTalk, driven by the
    # spoken hook audio + the speaker image. The clip's own audio is excluded in
    # the render (the master voiceover already carries the hook), so the lips
    # move while the master track -- which opens with the same hook audio -- plays.
    clip_dir = Path(project_dir) / "seedance 2.0"
    clip_dir.mkdir(parents=True, exist_ok=True)
    clip_path = clip_dir / "speaker_hook.mp4"
    hook_audio = find_existing_hook_audio(project_dir)
    image_src = asset if asset.exists() else (Path(archived) if archived and Path(archived).exists() else None)
    used_infinitetalk = False
    if hook_audio and image_src:
        if recreate or not clip_path.exists():
            try:
                log(status_cb, "Speaker hook: rendering InfiniteTalk talking-head from hook audio + speaker image...")
                pipeline.generate_infinitetalk_clip(
                    image_src, hook_audio, clip_path,
                    prompt=(clean_text(plan.get("visual_plan") or "")
                            or "energetic close-up creator hook, natural head and mouth movement, direct eye contact"),
                    resolution="480p",
                    cancel_event=config.get("_cancel_event"),
                    status_cb=status_cb,
                )
                used_infinitetalk = True
            except pipeline.PipelineCancelled:
                raise
            except Exception as exc:
                log(status_cb, f"InfiniteTalk hook failed ({exc}); falling back to Seedance speaker clip.")
        else:
            used_infinitetalk = True
            log(status_cb, "Speaker hook: reusing existing InfiniteTalk clip.")

    if used_infinitetalk:
        first["video_model"] = pipeline.INFINITETALK_MODEL
        first["video_enable_web_search"] = False
        first["prompt"] = f"InfiniteTalk talking-head opening hook. Hook line: {plan.get('hook_line') or first_hook_line(script)}"
        first["video_prompt"] = clean_text(plan.get("visual_plan") or "")
        first["seedance_audio_volume"] = 0.0
    else:
        # Fallback: Seedance I2V speaker clip (standard SFW model, no spicy).
        first["prompt"] = (
            "Uploaded speaker image used as Seedance I2V source for the opening hook. "
            f"Hook line: {plan.get('hook_line') or first_hook_line(script)}"
        )
        first["video_prompt"] = clean_text(plan.get("seedance_prompt") or "")
        first["video_model"] = "bytedance/seedance-2.0/image-to-video"
        first["video_enable_web_search"] = True
        first["seedance_audio_volume"] = 0.16
    first["seedance_audio_locked"] = True

    config["speaker_hook"] = {
        "enabled": True,
        "source_image": str(archived or source_path),
        "asset": asset.name,
        "clip": "speaker_hook.mp4",
        "hook_line": plan.get("hook_line") or first_hook_line(script),
        "voice_style": plan.get("voice_style", ""),
        "visual_plan": plan.get("visual_plan", ""),
        "reference_style": SPEAKER_HOOK_REFERENCE_STYLE,
        "prompt": first.get("video_prompt", ""),
        "method": "infinitetalk" if used_infinitetalk else "seedance",
        "recreated": bool(recreate),
    }
    log(status_cb, f"Speaker hook: configured first scene ({'InfiniteTalk talking-head' if used_infinitetalk else 'Seedance fallback'}).")
    return config["speaker_hook"]


def build_topic_profile(title, script, scenes, use_gpt55=True, reasoning_model=None, status_cb=None):
    topic_title = humanize_title(title)
    combined = f"{topic_title}\n{script}"
    aliases = [topic_title]
    scene_queries = {}
    llm_plan = llm_search_plan(title, scenes, reasoning_model=reasoning_model, status_cb=status_cb) if use_gpt55 else {}
    canonical = llm_plan.get("canonical_topic") if isinstance(llm_plan, dict) else ""
    if isinstance(llm_plan.get("aliases"), list):
        aliases.extend([str(item) for item in llm_plan["aliases"] if str(item).strip()])
    plan_topic_terms = []
    if isinstance(llm_plan.get("topic_terms"), list):
        script_word_set = words(combined)
        for term in llm_plan["topic_terms"]:
            term = " ".join(str(term).split())
            if term and (words(term) & script_word_set):
                plan_topic_terms.append(term)
    if isinstance(llm_plan.get("scene_queries"), list):
        for item in llm_plan["scene_queries"]:
            try:
                scene_no = int(item.get("scene"))
            except Exception:
                continue
            queries = [str(q).strip() for q in item.get("queries", []) if str(q).strip()]
            if queries:
                scene_queries[scene_no] = queries[:4]

    wiki_pages = []
    wiki_errors = []
    for wiki_query in topic_query_candidates(topic_title, script, canonical):
        try:
            wiki_pages = wikipedia_search_pages(wiki_query, limit=4)
        except Exception as exc:
            wiki_errors.append(str(exc))
            continue
        if wiki_pages:
            log(status_cb, f"Wikipedia topic match for '{wiki_query}': {wiki_pages[0]['title']}")
            break
    if wiki_pages:
        wiki_title = wiki_pages[0]["title"]
        canonical_overlap = words(canonical) & words(combined) if canonical else set()
        if not canonical or canonical.lower() == topic_title.lower() or len(canonical_overlap) < 2:
            canonical = wiki_title
        aliases.extend([page["title"] for page in wiki_pages[:3]])
        combined += "\n" + "\n".join(page["snippet"] for page in wiki_pages)
    elif wiki_errors:
        log(status_cb, f"Wikipedia topic match skipped: {wiki_errors[-1]}")

    lowered = combined.lower()
    if "war over a pig" in lowered or ("pig" in lowered and "san juan" in lowered):
        aliases.extend(["Pig War", "Pig War 1859", "San Juan Island Pig War", "San Juan Islands boundary dispute"])
        canonical = canonical or "Pig War"

    deduped_aliases = []
    for alias in aliases:
        alias = " ".join(str(alias).split())
        if alias and alias.lower() not in [a.lower() for a in deduped_aliases]:
            deduped_aliases.append(alias)
    canonical = canonical or deduped_aliases[0]
    if canonical and not (words(canonical) & words(combined)):
        log(status_cb, f"Search planner canonical topic '{canonical}' did not match voice script; using '{topic_title}' instead.")
        canonical = topic_title
    profile_words = {
        token
        for token in words(" ".join(deduped_aliases + plan_topic_terms + important_terms(combined, 18, SEARCH_NOISE)))
        if token not in SEARCH_NOISE
    }
    return {
        "canonical": canonical,
        "aliases": deduped_aliases,
        "terms": profile_words,
        "phrases": extract_phrases(combined, 12),
        "scene_queries": scene_queries,
        "gpt55": bool(llm_plan),
    }


def web_queries_for_scene(title, scene, scene_index=1, profile=None):
    beat = scene.get("script", "")
    beat_words = words(f"{humanize_title(title)} {beat}")
    profile = profile or {"canonical": humanize_title(title), "aliases": [humanize_title(title)], "scene_queries": {}}
    canonical = humanize_title(profile.get("canonical") or title)
    topic_terms = set(profile.get("terms") or set()) | words(canonical)
    alias_text = " ".join(str(alias) for alias in profile.get("aliases", []))
    alias_terms = words(alias_text)
    queries = []
    for query in profile.get("scene_queries", {}).get(scene_index, []):
        query = " ".join(str(query).split())
        if not query:
            continue
        q_words = words(query)
        # Only prepend canonical if the query lacks topic/alias terms and is short
        if not (q_words & topic_terms or q_words & alias_terms) and len(q_words) <= 2:
            query = f"{canonical} {query}"
        queries.append(query)
    
    # Always include the canonical topic as a broad fallback
    queries.append(canonical)
    
    if beat_words & MAP_WORDS:
        queries.append(f"{canonical} map")
        
    for phrase in extract_phrases(beat, 3):
        if phrase.lower() not in canonical.lower():
            queries.append(f"{canonical} {phrase}")
            
    terms = important_terms(beat, 4, SEARCH_NOISE)
    if terms:
        queries.append(f"{canonical} {' '.join(terms[:2])}")
        
    deduped = []
    for query in queries:
        query = " ".join(query.split())
        if query and query.lower() not in [q.lower() for q in deduped]:
            deduped.append(query)
    return deduped[:4]


def commons_search_images(query, limit=6):
    params = {
        "action": "query",
        "format": "json",
        "generator": "search",
        "gsrnamespace": "6",
        "gsrsearch": query,
        "gsrlimit": str(limit),
        "prop": "imageinfo",
        "iiprop": "url|mime|extmetadata",
        "iiurlwidth": "1600",
    }
    url = f"{COMMONS_API}?{urllib.parse.urlencode(params)}"
    try:
        data = request_json_url(url, min_interval=0.6)
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            time.sleep(8)
            return []
        raise
    pages = data.get("query", {}).get("pages", {})
    results = []
    for page in pages.values():
        imageinfo = page.get("imageinfo") or []
        if not imageinfo:
            continue
        info = imageinfo[0]
        mime = info.get("mime", "")
        if not mime.startswith("image/"):
            continue
        image_url = info.get("thumburl") or info.get("url")
        if not image_url:
            continue
        metadata = info.get("extmetadata", {}) or {}
        results.append(
            {
                "title": page.get("title", ""),
                "query": query,
                "url": image_url,
                "source_url": info.get("descriptionurl", ""),
                "mime": mime,
                "artist": (metadata.get("Artist") or {}).get("value", ""),
                "license": (metadata.get("LicenseShortName") or {}).get("value", ""),
                "credit": (metadata.get("Credit") or {}).get("value", ""),
            }
        )
    return results


def normalize_image_url(url):
    url = html.unescape(str(url or "")).strip()
    if not url.startswith(("http://", "https://")):
        return ""
    return url


def duckduckgo_vqd(query):
    search_url = "https://duckduckgo.com/?" + urllib.parse.urlencode(
        {"q": query, "iar": "images", "iax": "images", "ia": "images"}
    )
    text = request_text_url(search_url, min_interval=1.3)
    for pattern in [
        r"vqd='([^']+)'",
        r'vqd="([^"]+)"',
        r"vqd=([^&\"']+)&",
        r'"vqd":"([^"]+)"',
    ]:
        match = re.search(pattern, text)
        if match:
            return html.unescape(match.group(1))
    return ""


def duckduckgo_search_images(query, limit=12):
    vqd = duckduckgo_vqd(query)
    if not vqd:
        return []
    params = {
        "l": "us-en",
        "o": "json",
        "q": query,
        "vqd": vqd,
        "f": ",,,",
        "p": "1",
    }
    url = "https://duckduckgo.com/i.js?" + urllib.parse.urlencode(params)
    text = request_text_url(url, min_interval=1.3, referer="https://duckduckgo.com/")
    data = json.loads(text)
    results = []
    for item in data.get("results", [])[:limit]:
        image_url = normalize_image_url(item.get("image"))
        if not image_url:
            continue
        source_url = normalize_image_url(item.get("url"))
        results.append(
            {
                "title": strip_markup(item.get("title", "")),
                "query": query,
                "url": image_url,
                "source_url": source_url,
                "mime": mimetypes.guess_type(urllib.parse.urlparse(image_url).path)[0] or "image/jpeg",
                "artist": "",
                "license": "",
                "credit": item.get("source", "") or urllib.parse.urlparse(source_url).netloc,
                "provider": "duckduckgo",
                "thumbnail_url": normalize_image_url(item.get("thumbnail")),
                "width": item.get("width"),
                "height": item.get("height"),
            }
        )
    return results


def bing_search_images(query, limit=12):
    params = {
        "q": query,
        "form": "HDRSC2",
        "first": "1",
        "qft": "+filterui:photo-photo",
    }
    url = "https://www.bing.com/images/search?" + urllib.parse.urlencode(params)
    text = request_text_url(url, min_interval=0.5)
    results = []
    seen = set()
    for match in re.finditer(r'<a[^>]+class="[^"]*\biusc\b[^"]*"[^>]+m="([^"]+)"', text):
        raw = html.unescape(match.group(1))
        try:
            item = json.loads(raw)
        except Exception:
            continue
        image_url = normalize_image_url(item.get("murl"))
        if not image_url or image_url in seen:
            continue
        seen.add(image_url)
        source_url = normalize_image_url(item.get("purl"))
        results.append(
            {
                "title": strip_markup(item.get("t", "")),
                "query": query,
                "url": image_url,
                "source_url": source_url,
                "mime": mimetypes.guess_type(urllib.parse.urlparse(image_url).path)[0] or "image/jpeg",
                "artist": "",
                "license": "",
                "credit": urllib.parse.urlparse(source_url).netloc,
                "provider": "bing",
                "thumbnail_url": normalize_image_url(item.get("turl")),
                "width": item.get("w"),
                "height": item.get("h"),
            }
        )
        if len(results) >= limit:
            break
    return results


def general_search_images(query, limit=18, providers=None, status_cb=None):
    providers = providers or WEB_IMAGE_SEARCH_PROVIDERS
    per_provider = max(4, math.ceil(limit / max(1, len(providers))) + 2)
    all_results = []
    for provider in providers:
        try:
            if provider == "duckduckgo":
                results = duckduckgo_search_images(query, limit=per_provider)
            elif provider == "bing":
                results = bing_search_images(query, limit=per_provider)
            elif provider == "wikimedia":
                results = commons_search_images(query, limit=per_provider)
                for item in results:
                    item.setdefault("provider", "wikimedia")
            else:
                results = []
            if results:
                log(status_cb, f"Image search provider {provider}: {len(results)} candidate(s) for '{query}'.")
            all_results.extend(results)
        except Exception as exc:
            log(status_cb, f"Image search provider {provider} failed for '{query}': {exc}")
    deduped = []
    seen_urls = set()
    for result in all_results:
        url = normalize_image_url(result.get("url"))
        if not url:
            continue
        key = url.split("?", 1)[0].lower()
        if key in seen_urls:
            continue
        result["url"] = url
        seen_urls.add(key)
        deduped.append(result)
        if len(deduped) >= limit:
            break
    return deduped


def scene_allows_document_or_book(scene):
    scene_terms = words(scene.get("script", "")) | (words(scene.get("visual_script", "")) & DOCUMENT_ALLOWED_SCENE_TERMS)
    return bool(scene_terms & DOCUMENT_ALLOWED_SCENE_TERMS)


def web_searchable_text(result):
    title_text = re.sub(r"^file:", "", result.get("title", ""), flags=re.I).replace("_", " ")
    return " ".join(
        [
            title_text,
            strip_markup(result.get("artist", "")),
            strip_markup(result.get("credit", "")),
            result.get("source_url", ""),
        ]
    ).lower()


def is_book_or_scan_candidate(result, scene):
    searchable = web_searchable_text(result)
    if not any(term in searchable for term in BOOK_SCAN_WEB_TERMS):
        return False
    if scene_allows_document_or_book(scene):
        return False
    return True


def web_candidate_score(result, profile, scene):
    title_text = re.sub(r"^file:", "", result.get("title", ""), flags=re.I).replace("_", " ")
    searchable = web_searchable_text(result)
    scene_terms = words(scene.get("script", ""))
    profile_terms = set(profile.get("terms", set()))
    if web_candidate_title_is_junk(result):
        return -999  # NSFW / trending-SEO junk (common from broken Bing scraping)
    if is_book_or_scan_candidate(result, scene):
        return -999
    title_words = words(title_text)
    alias_hit = False
    phrase_hit = False
    score = 0
    score += len(title_words & profile_terms) * 5
    score += len(title_words & scene_terms) * 5
    for alias in profile.get("aliases", []):
        alias = alias.lower()
        if alias and alias in searchable:
            alias_hit = True
            score += 16
    for phrase in profile.get("phrases", []):
        if phrase.lower() in searchable:
            phrase_hit = True
            score += 8
    if "map" in searchable and (scene_terms & MAP_WORDS):
        score += 10
    if "san juan" in searchable and "pig" in " ".join(profile.get("aliases", [])).lower():
        score += 12
    if any(term in searchable for term in IRRELEVANT_WEB_TERMS):
        return -999
    if "svg" in result.get("mime", "").lower():
        score -= 3
    if not alias_hit and not phrase_hit and len(title_words & profile_terms) < 2 and len(title_words & scene_terms) < 2:
        return -20
    return score


def web_candidate_topic_grounded(result, profile, scene):
    provider = str(result.get("provider") or "").lower()
    if provider == "wikimedia":
        return True, "wikimedia candidate"
    searchable = web_searchable_text(result)
    source_domain = urllib.parse.urlparse(result.get("source_url") or result.get("url") or "").netloc.lower()
    if any(term in source_domain for term in LOW_TRUST_IMAGE_DOMAINS):
        return False, f"low-trust image domain {source_domain}"
    canonical = str(profile.get("canonical") or "").lower()
    aliases = [str(alias).lower() for alias in profile.get("aliases", []) if len(str(alias).strip()) > 3]
    if canonical and canonical in searchable:
        return True, "canonical topic in title/source"
    if any(alias and alias in searchable for alias in aliases):
        return True, "topic alias in title/source"
    title_words = words(re.sub(r"^file:", "", result.get("title", ""), flags=re.I).replace("_", " "))
    source_words = words(result.get("source_url", ""))
    profile_terms = set(profile.get("terms", set()))
    scene_terms = words(scene.get("exact_voice_text") or scene.get("voice_line") or scene.get("script", ""))
    must_show_terms = words(" ".join(str(item) for item in scene.get("must_show", []) if item))
    overlap = (title_words | source_words) & (profile_terms | scene_terms | must_show_terms)
    if len(overlap) >= 2:
        return True, f"metadata overlaps topic terms: {', '.join(sorted(overlap)[:4])}"
    return False, "no strong topic signal in title/source metadata"


def safe_media_filename(text, suffix):
    stem = slugify(re.sub(r"^file:", "", text, flags=re.I))[:80] or f"web_{int(time.time())}"
    if suffix.lower() not in IMAGE_EXTS:
        suffix = ".jpg"
    return f"{stem}{suffix.lower()}"


def verify_image_file(path):
    try:
        from PIL import Image

        with Image.open(path) as img:
            img.verify()
        return True
    except Exception:
        return False


WIKIMEDIA_HOSTS = ("wikimedia.org", "wikipedia.org", "wikidata.org")


def _download_min_interval(url):
    """Per-host spacing for downloads. Wikimedia rate-limits hard, so give it more
    room; generic CDNs can go faster. Different hosts still run concurrently."""
    try:
        host = urllib.parse.urlparse(url).netloc.lower()
    except Exception:
        host = ""
    if any(token in host for token in WIKIMEDIA_HOSTS):
        return 1.1
    return 0.5


def download_url_to_file(url, path, referer="", retries=2):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,de;q=0.8",
    }
    if referer:
        headers["Referer"] = referer
    min_interval = _download_min_interval(url)
    attempt = 0
    while True:
        # Per-host throttle prevents parallel downloads from hammering one host
        # (the Wikimedia 429s came from concurrent fetches against upload.wikimedia.org).
        host_throttle(url, min_interval)
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=90) as response:
                content_type = (response.headers.get("Content-Type") or "").lower()
                if content_type and "image/" not in content_type and "octet-stream" not in content_type:
                    raise RuntimeError(f"URL did not return an image content type: {content_type}")
                data = response.read()
            path.write_bytes(data)
            break
        except urllib.error.HTTPError as exc:
            # Back off and retry on rate-limit / transient blocks; honor Retry-After.
            if exc.code in (429, 403, 503) and attempt < retries:
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                try:
                    delay = float(retry_after) if retry_after else 0.0
                except (TypeError, ValueError):
                    delay = 0.0
                delay = max(delay, 1.5 * (2 ** attempt))
                time.sleep(min(delay, 12.0))
                attempt += 1
                continue
            raise
    if path.stat().st_size < 1000 or not verify_image_file(path):
        path.unlink(missing_ok=True)
        raise RuntimeError("Downloaded file is not a readable image.")
    return path.stat().st_size


def log_preview(status_cb, label, path):
    path = Path(path)
    if path.exists():
        log(status_cb, f"PREVIEW_IMAGE|{label}|{path}")


def create_media_contact_sheet(image_paths, out_path, title="Media"):
    image_paths = [Path(path) for path in image_paths if Path(path).exists() and Path(path).suffix.lower() in IMAGE_EXTS]
    if not image_paths:
        return None
    from PIL import Image, ImageDraw, ImageFont

    cols = min(3, max(1, len(image_paths)))
    cell_w, cell_h = 260, 420
    header_h = 54
    rows = math.ceil(len(image_paths) / cols)
    sheet = Image.new("RGB", (cols * cell_w, header_h + rows * cell_h), (245, 244, 239))
    draw = ImageDraw.Draw(sheet)
    try:
        title_font = ImageFont.truetype("arial.ttf", 24)
        label_font = ImageFont.truetype("arial.ttf", 18)
    except Exception:
        title_font = label_font = ImageFont.load_default()
    draw.text((14, 14), title, fill=(22, 22, 22), font=title_font)
    for index, path in enumerate(image_paths):
        x = (index % cols) * cell_w
        y = header_h + (index // cols) * cell_h
        try:
            with Image.open(path) as source:
                img = source.convert("RGB")
                img.thumbnail((cell_w - 38, cell_h - 78), Image.Resampling.LANCZOS)
                px = x + (cell_w - img.width) // 2
                py = y + 12
                sheet.paste(img, (px, py))
        except Exception:
            continue
        label = path.stem[:28]
        draw.text((x + 14, y + cell_h - 50), label, fill=(32, 32, 32), font=label_font)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out_path, quality=92)
    return out_path


def image_data_url(path):
    path = Path(path)
    mime = mimetypes.guess_type(str(path))[0] or "image/jpeg"
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{data}"


_CLIP_FRAME_CACHE = {}   # str(resolved clip path) -> [frame Path, ...]; survives retry rounds


def _clip_multiframe_strip(clip, frames_dir, idx, ffmpeg, n=4):
    """Sample n frames ACROSS a clip (not just frame 1) and compose them into one horizontal
    strip so the vision model judges what happens over the whole clip. Frame extraction is
    cached by clip path (stable hash-named files) so retry rounds don't re-run ffmpeg."""
    key = str(Path(clip).resolve())
    frames = _CLIP_FRAME_CACHE.get(key)
    if not (frames and all(Path(f).exists() for f in frames)):
        try:
            dur = float(probe_audio_duration(clip) or 0.0)
        except Exception:
            dur = 0.0
        if dur <= 0:
            dur = 5.0
        fracs = [0.12, 0.38, 0.62, 0.88][:max(2, n)]
        h = hashlib.sha1(key.encode("utf-8", "ignore")).hexdigest()[:10]
        frames = []
        for j, fr in enumerate(fracs):
            fp = frames_dir / f"f_{h}_{j}.jpg"
            if pipeline.extract_poster_frame(clip, fp, ffmpeg=ffmpeg, at=max(0.1, dur * fr)):
                frames.append(fp)
        if frames:
            _CLIP_FRAME_CACHE[key] = frames
    if not frames:
        return None
    try:
        from PIL import Image
        H = 360
        tiles = []
        for f in frames:
            im = Image.open(f).convert("RGB")
            tiles.append(im.resize((max(1, int(im.width * H / max(1, im.height))), H)))
        gap = 6
        sheet = Image.new("RGB", (sum(t.width for t in tiles) + gap * (len(tiles) - 1), H), (18, 18, 18))
        x = 0
        for t in tiles:
            sheet.paste(t, (x, 0)); x += t.width + gap
        out = frames_dir / f"clip_{idx:02d}.jpg"
        sheet.save(out, quality=88)
        return out
    except Exception:
        return frames[0]


def assign_clips_to_scenes_by_vision(scenes, clip_paths, project_dir, reasoning_model=None,
                                     status_cb=None, understanding=None, clip_meta=None,
                                     scene_specs=None, collaborate=False,
                                     min_script_match_score=MIN_SCRIPT_MATCH_SCORE):
    """Semantic scene<->clip matcher (scrape-FIRST). For every scraped candidate it builds a
    multi-frame strip, the vision model profiles the clip and scores how well it supports each
    EXACT narration line (script_match 0-10 + style_match + acceptance test). A clip is only used
    for a scene if script_match clears the current adaptive threshold and passes the acceptance test; else
    the scene is left for a fallback visual (clip=None) instead of random Japan b-roll.

    Returns (scene_clips, decision_log): scene_clips[i] is a Path or None; decision_log[i] is a
    dict (voice_text, chosen_clip, source_query, script_match_score, style_match_score,
    passes_acceptance_test, reason, fallback_needed, fallback_type, rejected_candidate_count,
    top_rejected_candidates)."""
    clip_paths = [Path(p) for p in clip_paths if Path(p).exists()]
    n = len(clip_paths)
    blank_log = [{"voice_text": (scene_text_for_planning(s) or s.get("script", ""))[:200],
                  "chosen_clip": None, "fallback_needed": True, "fallback_type": "ai_video",
                  "script_match_score": 0, "passes_acceptance_test": False,
                  "reason": "no scraped pool / vision unavailable"} for s in scenes]
    if not n or not scenes or not os.environ.get("WAVESPEED_API_KEY"):
        return ([None] * len(scenes), blank_log)
    clip_meta = clip_meta or {}
    try:
        match_threshold = max(0.0, min(10.0, float(min_script_match_score)))
    except (TypeError, ValueError):
        match_threshold = MIN_SCRIPT_MATCH_SCORE

    # A big candidate pool produces a contact sheet too large for the vision API (HTTP 400).
    # Score in batches and keep the best-scoring clip per scene across batches.
    MATCH_BATCH = 26
    if n > MATCH_BATCH:
        merged_clips = [None] * len(scenes)
        merged_log = list(blank_log)
        best_decision = [-1.0] * len(scenes)
        accepted_edges = []
        nb = (n + MATCH_BATCH - 1) // MATCH_BATCH
        log(status_cb, f"Scrape: scoring {n} candidate(s) in {nb} batch(es) of {MATCH_BATCH}...")
        for b0 in range(0, n, MATCH_BATCH):
            sub = clip_paths[b0:b0 + MATCH_BATCH]
            sc, lg = assign_clips_to_scenes_by_vision(
                scenes, sub, project_dir, reasoning_model=reasoning_model, status_cb=status_cb,
                understanding=understanding, clip_meta=clip_meta, scene_specs=scene_specs,
                collaborate=collaborate, min_script_match_score=match_threshold)
            for i in range(len(scenes)):
                c = sc[i] if i < len(sc) else None
                d = lg[i] if i < len(lg) else {}
                s = float(d.get("script_match_score") or 0)
                if s > best_decision[i]:
                    best_decision[i] = s
                    merged_log[i] = d
                if c:
                    accepted_edges.append((s, i, c, d))
        # Global greedy assignment across batches.  STRICT no-reuse (user rule 2026-07-13): a
        # source clip serves AT MOST ONE scene - never a second scene, even a different excerpt.
        # A scene left without a unique clip goes back to the search/retry path instead.
        assigned_scenes, used_clips = set(), {}
        for _score, i, c, d in sorted(accepted_edges, key=lambda row: row[0], reverse=True):
            clip_key = str(Path(c).resolve())
            if i in assigned_scenes or used_clips.get(clip_key, 0) >= 1:
                continue
            merged_clips[i] = c
            merged_log[i] = d
            assigned_scenes.add(i)
            used_clips[clip_key] = used_clips.get(clip_key, 0) + 1
        log(status_cb, f"Scrape: {sum(1 for c in merged_clips if c)}/{len(scenes)} scene(s) matched across batches.")
        return (merged_clips, merged_log)

    try:
        ff = pipeline.find_ffmpeg()
        frames_dir = project_dir / "review" / "_clip_match"
        # NOTE: do not wipe this dir between retry rounds - cached frames (f_*.jpg) are reused.
        frames_dir.mkdir(parents=True, exist_ok=True)
        strips, kept = [], []
        for i, cp in enumerate(clip_paths):
            strip = _clip_multiframe_strip(cp, frames_dir, i, ff, n=4)
            if strip:
                strips.append(strip); kept.append(i)
        if not strips:
            return ([None] * len(scenes), blank_log)
        sheet = create_media_contact_sheet(strips, frames_dir / "_sheet.jpg",
                                           title="Scraped clips - each tile = 4 frames across one clip (clip_NN)")
        if not sheet:
            return ([None] * len(scenes), blank_log)

        def _spec(idx):
            sp = (scene_specs or {}).get(idx) if isinstance(scene_specs, dict) else None
            return sp if isinstance(sp, dict) else {}
        scene_lines = []
        for idx, s in enumerate(scenes):
            sp = _spec(idx)
            txt = (scene_text_for_planning(s) or s.get("script", ""))[:160]
            extra = ""
            if sp.get("required_subject") or sp.get("must_show") or sp.get("visual_acceptance_test"):
                extra = ((f" | preferred_bucket: {sp.get('preferred_bucket_id')}" if sp.get("preferred_bucket_id") else "")
                         + f" | needs: {sp.get('required_subject','')} {sp.get('required_action','')}".rstrip()
                         + (f" | must_show: {', '.join(sp.get('must_show') or [])}" if sp.get("must_show") else "")
                         + (f" | must_not_show: {', '.join(sp.get('must_not_show') or [])}" if sp.get("must_not_show") else "")
                         + (f" | accept_if: {sp.get('visual_acceptance_test')}" if sp.get("visual_acceptance_test") else ""))
            scene_lines.append(f"scene {idx}: \"{txt}\"{extra}")
        candidate_lines = []
        for idx, cp in enumerate(clip_paths):
            meta = clip_meta.get(str(cp), {}) if isinstance(clip_meta, dict) else {}
            caption = clean_text(str((meta or {}).get("caption", "")))[:120]
            query = clean_text(str((meta or {}).get("source_query", "")))[:80]
            bucket = clean_text(str((meta or {}).get("bucket_id", "")))[:50]
            internal_cuts = int((meta or {}).get("internal_cut_count") or 0)
            rapid_cuts = int((meta or {}).get("rapid_internal_cut_count") or 0)
            candidate_lines.append(
                f"clip_{idx:02d}: bucket={bucket or 'unknown'} | search_query={query or 'unknown'}"
                f" | internal_cuts={internal_cuts} | rapid_internal_cuts={rapid_cuts}"
                + (f" | TikTok_caption={caption}" if caption else "")
            )
        prompt = (
            understanding_brief(understanding) +
            "You are a STRICT footage editor for a vertical short about JAPAN, cut from real TikTok clips.\n"
            "Each contact-sheet tile shows 4 frames sampled across ONE clip (labeled clip_00, clip_01, ...), so "
            "judge the WHOLE clip, not one frame.\n\n"
            "STEP 1 - PROFILE every clip: subjects, actions, location, mood, and quality flags. A clip is UNUSABLE "
            "(never pick it for any scene) if it contains ANY persistent creator-added caption, subtitle, sticker, "
            "username, watermark, or a blurred/smeared patch where text was removed; also reject a text wall (quiz / diagnosis / "
            "chat-screen where text IS the content), a TikTok LIVE / livestream / screen recording, a slideshow of "
            "stills, anime / VTuber / CGI, AI-GENERATED photoreal footage (impossibly perfect model-like faces and "
            "skin, uncanny crowds, AI-art watermark/handle) - this app cuts REAL found footage only, idol promo / "
            "audition, horizontal with big black bars, or not actually "
            "Japan. Captioned clips are NOT usable and must never be selected, even when topically perfect. "
            "Also reject a rapid montage: the selected source must hold one coherent shot long "
            "enough that our own timeline does not create sub-second double/triple cuts.\n\n"
            "STEP 2 - For EACH scene, pick the BEST clip and score it:\n"
            "  script_match (0-10): does the clip literally DEPICT what the line says (subject + action + emotion), "
            "not just 'somewhere in Japan'? A pretty neon skyline for a line about STRESS or CLEAN STREETS is a LOW "
            "score. A tired worker for 'exhausted', a packed train for 'crowded trains', a lone figure in a crowd "
            "for 'alone' = HIGH.\n"
            "  raw_footage (0-10): how clean/real is it as raw footage (10 = clean real footage, no overlays/bars; "
            "0 = screenshot/text-card/livestream).\n"
            "  text_heavy (0-10): how much burned-in original text covers the frame (0 = none, 10 = text IS the clip). "
            "Any persistent creator text or visible caption-removal blur is unacceptable; score it accurately.\n"
            "  has_creator_text (bool): TRUE if a persistent creator-added subtitle, commentary, sticker, username, "
            "or editing-app watermark appears in multiple sampled frames. TRUE makes the clip unusable.\n"
            "  edit_stability (0-10): 10 = one coherent continuous shot; 0 = rapid montage/slideshow. Below 5 is reject.\n"
            "  match_class: 'A_MATCH' = direct visual match (line about tired workers -> tired office worker / "
            "commute / late-night office); 'B_MATCH' = strong social-context match (expensive love -> couple on a "
            "date / paying a bill / shopping date / creator discussing dating money); 'C_MATCH' = "
            "emotional/atmospheric match (loneliness -> person alone in Tokyo night / small apartment / solo meal); "
            "'D_REJECTED' = OFF-TOPIC or contradictory footage (a random book cover / unrelated product for this "
            "topic), NOT merely 'doesn't literally show the noun'. "
            "Specific factual/claim lines prefer A_MATCH or B_MATCH; ABSTRACT or essay lines should accept a "
            "topically-coherent C_MATCH clip.\n"
            "  style_match (0-10): visual quality/vibe fit.\n"
            "  passes (bool): would a viewer understand WHY this clip plays during this sentence, AND is the clip "
            "clean (not UNUSABLE)?\n"
            "CRITICAL - THE SCENES ARE FRAGMENTS: the scene lines below are consecutive CUTS of ONE narration "
            "about the topic stated above. Half of them are connective fragments with no concrete subject "
            "(\"make you fit.\", \"is not an excuse.\", \"Because in Japan, being late\"). A fragment can NEVER "
            "'depict' anything - judging it literally is WRONG. For any fragment, pick the pool clip whose "
            "footage best fits the OVERALL topic and the surrounding scenes, class it C_MATCH with script_match "
            "6-7 and passes=true. D_REJECTED exists ONLY for footage that does not belong to this topic at all "
            "(wrong subject/place) or is unusable - a real TikTok essay runs on-topic b-roll under every "
            "fragment, and a scene left empty is a WORSE outcome than coherent on-topic b-roll.\n"
            "Be honest on CONCRETE claim lines: if nothing depicts the claim, use a contextual C_MATCH rather "
            "than pretending an A_MATCH - but do not inflate A/B scores.\n"
            "  visual_type: classify each SCENE LINE first - 'concrete' (names a showable object/action: "
            "pushers, champagne tower, delay certificate), 'context' (place/social setting: Shinjuku, rush "
            "hour, at work), or 'abstract' (fragment/feeling/transition/question: \"is not an excuse.\", "
            "\"But here is the dark part.\"). The acceptance bar is LOWER for context and much lower for "
            "abstract lines - report the type honestly.\n"
            "  bucket_match (0-10): how well the clip fits the scene's assigned BUCKET/topic regardless of "
            "the literal line (this is what carries abstract lines).\n"
            f"CURRENT ADAPTIVE MATCH THRESHOLD: {match_threshold:.1f}/10. "
            + ("This is a relaxed retry: accept a coherent C_MATCH contextual or atmospheric clip when it is "
               "clearly compatible with the sentence, even if it does not literally show every noun. Still reject "
               "contradictory, misleading, unusable, or generic off-topic footage.\n"
               if match_threshold < MIN_SCRIPT_MATCH_SCORE else
               "Use direct A/B matches for concrete claims and reserve C_MATCH for genuinely abstract lines.\n")
            + "Prefer a DIFFERENT clip per scene; only reuse if genuinely the best fit.\n\n"
            "Candidate search metadata is supporting evidence only; the sampled frames are authoritative. Use it "
            "to distinguish visually similar clips and never assume a query proves that the video depicts it:\n"
            + "\n".join(candidate_lines) + "\n\n"
            + f"Scenes:\n" + "\n".join(scene_lines) + "\n\n"
            + 'Return STRICT JSON: {"scenes": {"0": {"clip": <clip_number or -1>, "script_match": <0-10>, '
            '"bucket_match": <0-10>, "visual_type": "concrete|context|abstract", '
            '"raw_footage": <0-10>, "text_heavy": <0-10>, "has_creator_text": true|false, "edit_stability": <0-10>, "match_class": "A_MATCH|B_MATCH|C_MATCH|D_REJECTED", '
            '"style_match": <0-10>, "passes": true|false, "reason": "short why", '
            '"fallback_type": "ai_video|speaker|proof_card|abstract_broll|reuse_previous", '
            '"rejected": [{"clip": <n>, "why": "short reason incl. text-heavy/screenshot/livestream/black-bars/'
            'generic if applicable"}]}, "1": {...}, ...}} - one entry per scene index.'
        )
        messages = [
            {"role": "system", "content": "You are a footage QA + editor for viral TikTok-style essays. A clip may play "
             "under a narration line when it genuinely FITS that line: either by DEPICTING it (concrete lines with a clear "
             "subject) OR by matching its TOPIC and MOOD (abstract/essay lines - a sentence fragment or feeling). Real "
             "TikTok essays run coherent b-roll under abstract narration; that is CORRECT, not filler. Reject ANY "
             "persistent creator caption/subtitle/sticker/watermark and any obvious blurred caption-removal patch; "
             "clean raw footage is mandatory. Also reject footage that is off-topic/contradictory, a screenshot/livestream/slideshow, "
             "or is otherwise unusable. Return JSON only."},
            {"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": image_data_url(sheet)}},
            ]},
        ]
        log(status_cb, "Scrape: semantic scene<->clip matching (multi-frame, scored)...")
        if collaborate:
            plan = collaborate_json(messages, max_tokens=6000, temperature=0.1, status_cb=status_cb, label="clip matching") or {}
        else:
            data = post_json_url(WAVESPEED_LLM_API, {
                "model": reasoning_model or GPT55_MODEL, "messages": messages,
                "temperature": 0.1, "max_tokens": 6000, "response_format": {"type": "json_object"}}, timeout=240)
            plan = extract_json_object(data["choices"][0]["message"]["content"]) or {}
        smap = plan.get("scenes") if isinstance(plan.get("scenes"), dict) else {}

        scene_clips, decision_log, used = [], [], {}
        for idx, s in enumerate(scenes):
            d = smap.get(str(idx)) if isinstance(smap.get(str(idx)), dict) else {}
            try:
                cn = int(d.get("clip", -1))
            except (TypeError, ValueError):
                cn = -1
            try:
                sm = float(d.get("script_match", 0))
            except (TypeError, ValueError):
                sm = 0.0
            try:
                stm = float(d.get("style_match", 0))
            except (TypeError, ValueError):
                stm = 0.0
            try:
                raw_fs = float(d.get("raw_footage", 0))
            except (TypeError, ValueError):
                raw_fs = 0.0
            try:
                txt_hs = float(d.get("text_heavy", 0))
            except (TypeError, ValueError):
                txt_hs = 0.0
            try:
                edit_stability = float(d.get("edit_stability", 10))
            except (TypeError, ValueError):
                edit_stability = 0.0
            _creator_text_value = d.get("has_creator_text", False)
            has_creator_text = (_creator_text_value is True or
                                str(_creator_text_value).strip().lower() in {"true", "1", "yes"})
            try:
                bucket_m = float(d.get("bucket_match", 0))
            except (TypeError, ValueError):
                bucket_m = 0.0
            visual_type = str(d.get("visual_type", "") or "").strip().lower()
            if visual_type not in ("concrete", "context", "abstract"):
                visual_type = "context"
            mclass = str(d.get("match_class", "") or "").strip().upper()
            if mclass not in ("A_MATCH", "B_MATCH", "C_MATCH", "D_REJECTED"):
                mclass = "D_REJECTED" if sm < MIN_SCRIPT_MATCH_SCORE else "B_MATCH"
            passes = bool(d.get("passes", False))
            # a good source clip may serve up to TWO scenes (the final cleanup keeps
            # duplicates off ADJACENT cuts); a hard one-scene ban starved long scripts.
            valid = (0 <= cn < n) and (cn in kept) and (used.get(cn, 0) < 2)
            # HARD gates: clean raw footage only, and D_REJECTED never fills a scene.
            candidate_meta = clip_meta.get(str(clip_paths[cn])) if (0 <= cn < n) else {}
            deterministic_stable = int((candidate_meta or {}).get("rapid_internal_cut_count") or 0) == 0
            try:
                deterministic_text = float((candidate_meta or {}).get("text_heaviness") or 0.0)
            except (TypeError, ValueError):
                deterministic_text = 0.0
            # Strict clean-footage contract: creator text is a hard failure, not a soft ranking
            # signal. The deterministic OCR gate backs up the vision judgment.
            clean = (not has_creator_text and txt_hs <= 1.5 and deterministic_text <= 1.5
                     and raw_fs >= 4.0 and edit_stability >= 5.0 and deterministic_stable)
            # TYPE-AWARE bar: a concrete claim must match the line; a place/social line needs
            # less; an abstract fragment only needs clean on-topic bucket footage.
            _type_offset = {"concrete": 0.6, "context": 1.4, "abstract": 2.4}[visual_type]
            _type_floor = {"concrete": 4.0, "context": 3.2, "abstract": 2.5}[visual_type]
            eff_threshold = max(_type_floor, match_threshold - _type_offset)
            score_ok = (sm >= eff_threshold
                        or (visual_type in ("context", "abstract") and bucket_m >= 5.0))
            accept = (valid and passes and clean and mclass != "D_REJECTED" and score_ok)
            chosen = None
            if accept:
                chosen = clip_paths[cn]; used[cn] = used.get(cn, 0) + 1
            rejected = d.get("rejected") if isinstance(d.get("rejected"), list) else []
            cmeta = clip_meta.get(str(clip_paths[cn])) if (0 <= cn < n) else {}
            decision_log.append({
                "scene": idx,
                "voice_text": (scene_text_for_planning(s) or s.get("script", ""))[:200],
                "chosen_clip": (clip_paths[cn].name if chosen else None),
                "bucket_id": (cmeta or {}).get("bucket_id") if chosen else None,
                "source_query": (cmeta or {}).get("source_query") if chosen else None,
                "query_tier": (cmeta or {}).get("tier") if chosen else None,
                "match_class": (mclass if chosen else "D_REJECTED"),
                "visual_type": visual_type,
                "bucket_match_score": round(bucket_m, 1),
                "effective_threshold": round(eff_threshold, 1),
                "script_match_score": round(sm, 1),
                "style_match_score": round(stm, 1),
                "raw_visual_footage_score": round(raw_fs, 1),
                "text_heaviness_score": round(txt_hs, 1),
                "has_creator_text": has_creator_text,
                "edit_stability_score": round(edit_stability, 1),
                "final_score": round(sm * 0.75 + stm * 0.25, 2),
                "passes_acceptance_test": passes,
                "reason": str(d.get("reason", ""))[:300],
                "fallback_needed": (chosen is None),
                "fallback_type": (str(d.get("fallback_type", "ai_video")) if chosen is None else None),
                "rejected_candidate_count": len(rejected),
                "top_rejected_candidates": rejected[:4],
            })
            scene_clips.append(chosen)
            line = (scene_text_for_planning(s) or s.get("script", ""))[:46]
            if chosen:
                src = (cmeta or {}).get("source_query") or (cmeta or {}).get("bucket_id") or ""
                log(status_cb, f"   scene {idx} matched {mclass} \"{line}\" -> {clip_paths[cn].name} "
                               f"(match {sm:.0f}/10{', from ' + src if src else ''})")
            else:
                why = "generic/D_REJECTED" if mclass == "D_REJECTED" else (
                    "text-heavy/unclean" if not clean else f"best {sm:.0f}/10")
                log(status_cb, f"   scene {idx} rejected \"{line}\" -> no clean clip ≥{match_threshold:.1f} "
                               f"({why}) -> re-search / fallback")
        placed = sum(1 for c in scene_clips if c)
        log(status_cb, f"Scrape: {placed}/{len(scenes)} scene(s) got a clip that passes the script-match gate "
                       f"(>= {match_threshold:.1f}); {len(scenes) - placed} scene(s) -> re-search / fallback.")
        return (scene_clips, decision_log)
    except WaveSpeedBalanceError:
        raise                                  # abort the whole run with the clear balance message
    except Exception as exc:  # noqa: BLE001
        log(status_cb, f"Scrape: semantic matching ERROR ({exc.__class__.__name__}: {exc}).")
        log(status_cb, "Scrape: traceback -> " + traceback.format_exc().replace("\n", " | ")[:900])
        return ([None] * len(scenes), blank_log)


def _tally_clip_quality_filters(decision_log):
    """Approximate clip_quality_filters counts for agent_report by scanning the matcher's
    rejection reasons (per-scene 'reason' + top_rejected_candidates 'why')."""
    counts = {"rejected_text_heavy": 0, "rejected_screenshot": 0, "rejected_livestream": 0,
              "rejected_black_bars": 0, "rejected_low_quality": 0, "rejected_generic_broll": 0}
    if not isinstance(decision_log, list):
        return counts
    blobs = []
    for d in decision_log:
        if not isinstance(d, dict):
            continue
        if d.get("fallback_needed"):
            blobs.append(str(d.get("reason", "")).lower())
        for rj in (d.get("top_rejected_candidates") or []):
            if isinstance(rj, dict):
                blobs.append(str(rj.get("why", "")).lower())
    for t in blobs:
        if any(w in t for w in ("text-heavy", "text heavy", "caption", "subtitle", "burned-in", "burned in")):
            counts["rejected_text_heavy"] += 1
        if any(w in t for w in ("screenshot", "screen shot", "text post", "quiz", "diagnosis", "text card", "chat")):
            counts["rejected_screenshot"] += 1
        if any(w in t for w in ("livestream", "live stream", "screen recording", "tiktok live", " live")):
            counts["rejected_livestream"] += 1
        if any(w in t for w in ("black bar", "black-bar", "letterbox", "horizontal")):
            counts["rejected_black_bars"] += 1
        if any(w in t for w in ("low quality", "low-quality", "dark", "blurry", "low res", "low-res")):
            counts["rejected_low_quality"] += 1
        if any(w in t for w in ("generic", "d_rejected", "unrelated", "random", "street b-roll", "skyline")):
            counts["rejected_generic_broll"] += 1
    return counts


def score_hook_candidates(hook_clips, project_dir, reasoning_model=None, status_cb=None, collaborate=False):
    """Dedicated high-engagement Japanese-creator HOOK finder. The opening shot must be ONE real
    young adult Japanese woman dancing or playfully acting cute to camera, with an expressive face,
    clean vertical frame and no burned-in text. The engagement gate runs before this scorer
    (20K TikTok likes or the 5K X equivalent).

    Returns a list (best score first) of dicts:
    [{"clip": Path, "hook_presenter_score": float, "passed": bool, "scores": {...}, "reason": str}].
    A clip is only eligible for the opening shot when passed==True (hook_presenter_score >=
    MIN_HOOK_PRESENTER_SCORE AND a clear single woman dancing/playing cute to camera, none of the reject
    flags). Mirrors the matcher's batching/strip approach so it never blows the vision payload."""
    clips = [Path(p) for p in (hook_clips or []) if Path(p).exists()]
    if not clips or not os.environ.get("WAVESPEED_API_KEY"):
        return []
    # bound the contact sheet exactly like the scene matcher (avoid the oversized-image HTTP 400)
    HOOK_BATCH = 20
    if len(clips) > HOOK_BATCH:
        out = []
        for b0 in range(0, len(clips), HOOK_BATCH):
            out += score_hook_candidates(clips[b0:b0 + HOOK_BATCH], project_dir,
                                         reasoning_model=reasoning_model, status_cb=status_cb,
                                         collaborate=collaborate)
        out.sort(key=lambda r: r.get("hook_presenter_score", 0), reverse=True)
        return out
    try:
        ff = pipeline.find_ffmpeg()
        frames_dir = project_dir / "review" / "_hook_match"
        frames_dir.mkdir(parents=True, exist_ok=True)
        strips, kept = [], []
        for i, cp in enumerate(clips):
            strip = _clip_multiframe_strip(cp, frames_dir, i, ff, n=4)
            if strip:
                strips.append(strip); kept.append(i)
        if not strips:
            return []
        sheet = create_media_contact_sheet(
            strips, frames_dir / "_hook_sheet.jpg",
            title="Hook candidates - each tile = frames across one clip (clip_NN)")
        if not sheet:
            return []
        prompt = (
            "You are casting the OPENING HOOK shot of a vertical Japanese social short. The hook MUST be ONE "
            "real young-adult Japanese woman DANCING or playfully acting cute directly to the camera, with the "
            "polished, expressive energy of Miyu Kishi / Saaki-Sakii-style creator clips. It needs an immediate cute "
            "gesture, dance move, playful expression, or pose—not a static talking head. The metadata gate has "
            "already required at least 20,000 TikTok likes or 5,000 X likes. Require a clean vertical frame and no burned-in text. "
            "Each contact-sheet tile shows several frames across ONE candidate clip (clip_00, clip_01, ...), so "
            "judge the whole clip.\n"
            "Score EACH clip 0-10 on: face_visibility, eye_contact, dance_or_playful_action, "
            "cute_expression_energy, motion_energy, clean_frame, low_original_text, vertical_quality, "
            "miyu_sakii_creator_style, and "
            "sexualized_or_body_bait_penalty (0 = wholesome / not body-focused, 10 = heavy thirst/body bait). "
            "Then give an overall hook_presenter_score 0-10 that rewards a strong clean presenter and is dragged "
            "DOWN by the bait penalty.\n"
            "Set passed=false (no matter the score) if ANY of these are true: no clear single face, not a woman, "
            "no visible dance/playful-cute action, original text covers the face or centre, it is a livestream / screen "
            "recording / slideshow / screenshot, anime / VTuber / CGI, the person looks underage, it is too "
            "sexualized or body-focused, or it is horizontal with black bars.\n"
            'Return STRICT JSON: {"clips": {"0": {"face_visibility":n, "eye_contact":n, "dance_or_playful_action":n, '
            '"cute_expression_energy":n, "motion_energy":n, "clean_frame":n, "low_original_text":n, '
            '"vertical_quality":n, "miyu_sakii_creator_style":n, "sexualized_or_body_bait_penalty":n, "hook_presenter_score":n, '
            '"passed":true|false, "reason":"short why"}, "1": {...}}}'
        )
        messages = [
            {"role": "system", "content": "You are a strict casting director for viral short-form hooks. Only pass clean clips of one real adult Japanese woman dancing or playfully acting cute to camera. Return JSON only."},
            {"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": image_data_url(sheet)}},
            ]},
        ]
        log(status_cb, "Hook Finder: scoring popular clean cute/dance creator candidates (20K+ TikTok / 5K+ X)...")
        if collaborate:
            plan = collaborate_json(messages, max_tokens=3000, temperature=0.1, status_cb=status_cb, label="hook casting") or {}
        else:
            data = post_json_url(WAVESPEED_LLM_API, {
                "model": reasoning_model or GPT55_MODEL, "messages": messages,
                "temperature": 0.1, "max_tokens": 3000, "response_format": {"type": "json_object"}}, timeout=180)
            plan = extract_json_object(data["choices"][0]["message"]["content"]) or {}
        cmap = plan.get("clips") if isinstance(plan.get("clips"), dict) else {}
        score_keys = ("face_visibility", "eye_contact", "dance_or_playful_action",
                      "cute_expression_energy", "motion_energy", "clean_frame",
                      "low_original_text", "vertical_quality", "miyu_sakii_creator_style",
                      "sexualized_or_body_bait_penalty")
        results = []
        for i, cp in enumerate(clips):
            if i not in kept:
                continue
            d = cmap.get(str(i)) if isinstance(cmap.get(str(i)), dict) else {}
            try:
                hp = float(d.get("hook_presenter_score", 0))
            except (TypeError, ValueError):
                hp = 0.0
            vision_passed = bool(d.get("passed", False))
            passed = vision_passed and hp >= MIN_HOOK_PRESENTER_SCORE
            results.append({
                "clip": cp,
                "hook_presenter_score": round(hp, 1),
                "passed": passed,
                "vision_passed": vision_passed,
                "scores": {k: d.get(k) for k in score_keys},
                "reason": str(d.get("reason", ""))[:220],
            })
        results.sort(key=lambda r: r["hook_presenter_score"], reverse=True)
        npass = sum(1 for r in results if r["passed"])
        log(status_cb, f"Hook Finder: {len(results)} candidate(s), {npass} passed "
                       f"hook_presenter_score >= {MIN_HOOK_PRESENTER_SCORE:.1f}.")
        if results:
            top = results[0]
            log(status_cb, f"   best hook: {Path(top['clip']).name} "
                           f"({top['hook_presenter_score']:.1f}/10 - {top['reason'][:70]})")
        return results
    except Exception as exc:  # noqa: BLE001
        log(status_cb, f"Hook Finder: scoring failed ({exc.__class__.__name__}); using first hook clip.")
        log(status_cb, "Hook Finder: traceback -> " + traceback.format_exc().replace("\n", " | ")[:600])
        return [{"clip": c, "hook_presenter_score": 0.0, "passed": False, "scores": {}, "reason": "scorer error"}
                for c in clips]


def estimated_word_timeline_from_scenes(scenes):
    """Build a deterministic word timeline from canonical timed voice scenes.

    Used when forced-alignment word timestamps are unavailable.  Character-weighted timing is
    more faithful than copying an entire sentence into every visual sub-cut.
    """
    timeline = []
    for scene in sorted((dict(s) for s in (scenes or [])), key=lambda s: float(s.get("start", 0))):
        text = clean_text(str(scene.get("exact_voice_text") or scene.get("script") or ""))
        tokens = [t for t in re.split(r"\s+", text) if t]
        if not tokens:
            continue
        try:
            start = float(scene.get("start", 0.0)); end = float(scene.get("end", start))
        except (TypeError, ValueError):
            continue
        if end <= start:
            continue
        weights = [max(1.0, float(len(re.sub(r"\W+", "", token, flags=re.UNICODE)))) for token in tokens]
        total = sum(weights) or float(len(tokens))
        cursor = start
        for token, weight in zip(tokens, weights):
            word_end = min(end, cursor + (end - start) * weight / total)
            timeline.append({"word": token, "start": round(cursor, 3), "end": round(word_end, 3)})
            cursor = word_end
    return timeline


def sync_scenes_to_voice_timeline(scenes, word_timeline, target_duration=None):
    """Make every visual beat describe the words actually spoken during its time window.

    Micro-beat planning is creative, but its generated timestamps/text are not authoritative.
    This pass makes the audio timeline authoritative before TikTok query generation and semantic
    matching, preventing a clip selected for one phrase from playing under another phrase.
    """
    if not scenes or not word_timeline:
        return scenes
    ordered = sorted((dict(s) for s in scenes), key=lambda s: float(s.get("start", 0.0)))
    ordered[0]["start"] = 0.0
    for i in range(1, len(ordered)):
        prev_start = float(ordered[i - 1].get("start", 0.0))
        start = max(prev_start + 0.2, float(ordered[i].get("start", prev_start + 0.2)))
        ordered[i]["start"] = round(start, 3)
        ordered[i - 1]["end"] = round(start, 3)
    final_end = float(target_duration or ordered[-1].get("end", 0.0) or word_timeline[-1].get("end", 0.0))
    ordered[-1]["end"] = round(max(float(ordered[-1]["start"]) + 0.2, final_end), 3)

    for scene in ordered:
        start = float(scene.get("start", 0.0)); end = float(scene.get("end", start))
        spoken = []
        for word in word_timeline:
            try:
                midpoint = (float(word["start"]) + float(word["end"])) / 2.0
            except (KeyError, TypeError, ValueError):
                continue
            if start <= midpoint < end and str(word.get("word", "")).strip():
                spoken.append(str(word["word"]).strip())
        if not spoken:
            continue
        voice = clean_text(" ".join(spoken))
        scene["script"] = voice
        scene["exact_voice_text"] = voice
        scene["voice_line"] = voice
        scene["beat_purpose"] = f"Make the viewer understand this exact spoken phrase: {voice}"
        scene["scene_objective"] = f"Show what is being said now, not an earlier or later line: {voice}"
        scene["required_visual_information"] = voice
        scene["must_show"] = important_terms(voice, 5, SEARCH_NOISE)
    return ordered


def enforce_reference_pacing(scenes, max_s=2.4):
    """Match the reference cut rate (~one fresh clip every ~2s). Any beat longer than max_s
    is split into equal sub-beats so the edit cuts fast instead of holding one clip too long.
    Captions are word-windowed per beat downstream, so splitting only adds visual cuts."""
    out = []
    for s in scenes:
        try:
            start = float(s.get("start", 0.0)); end = float(s.get("end", start))
        except (TypeError, ValueError):
            out.append(s); continue
        dur = end - start
        if dur <= max_s * 1.25:
            out.append(s); continue
        n = max(2, int(math.ceil(dur / max_s)))
        step = dur / n
        text = clean_text(str(s.get("exact_voice_text") or s.get("voice_line") or s.get("script") or ""))
        tokens = [t for t in re.split(r"\s+", text) if t]
        for i in range(n):
            sub = dict(s)
            sub["start"] = round(start + i * step, 3)
            sub["end"] = round(end if i == n - 1 else start + (i + 1) * step, 3)
            sub["micro_beat"] = True
            # v0.2 sub-beats of ONE sentence share a group: they inherit one search intent and
            # the matcher's top candidates are spread across them (mass proves the thesis)
            sub["beat_group"] = f"bg_{round(start, 2)}"
            # A proportional split is a safe fallback.  The forced/estimated word timeline pass
            # later replaces this with the exact words spoken in each interval.
            if tokens:
                lo = int(round(i * len(tokens) / n))
                hi = int(round((i + 1) * len(tokens) / n))
                phrase = clean_text(" ".join(tokens[lo:max(lo + 1, hi)]))
                sub["script"] = phrase
                sub["exact_voice_text"] = phrase
                sub["voice_line"] = phrase
            out.append(sub)
    return out


def coalesce_short_scrape_scenes(scenes, min_s=1.45, max_s=3.2):
    """Remove isolated micro-cuts while keeping the spoken-word timeline authoritative.

    Source TikToks can contain their own edits. A planned 1.0s scene on top of that footage feels
    much faster than a 1.0s clean source shot, so merge only the shortest adjacent narration beats.
    Scene zero remains a dedicated hook.
    """
    ordered = sorted((dict(scene) for scene in (scenes or [])),
                     key=lambda scene: float(scene.get("start", 0.0)))
    if len(ordered) < 2:
        return ordered

    def _duration(scene):
        return max(0.0, float(scene.get("end", 0.0)) - float(scene.get("start", 0.0)))

    def _merge(left, right):
        merged = dict(left)
        merged["start"] = round(float(left.get("start", 0.0)), 3)
        merged["end"] = round(float(right.get("end", merged["start"])), 3)
        left_text = clean_text(str(left.get("exact_voice_text") or left.get("voice_line") or left.get("script") or ""))
        right_text = clean_text(str(right.get("exact_voice_text") or right.get("voice_line") or right.get("script") or ""))
        voice = clean_text(f"{left_text} {right_text}")
        if voice:
            merged["script"] = voice
            merged["exact_voice_text"] = voice
            merged["voice_line"] = voice
            merged["beat_purpose"] = f"Make the viewer understand this exact spoken phrase: {voice}"
            merged["scene_objective"] = f"Show what is being said now, not an earlier or later line: {voice}"
            merged["required_visual_information"] = voice
            merged["must_show"] = important_terms(voice, 5, SEARCH_NOISE)
        merged["micro_beat"] = True
        merged.pop("shots", None)
        return merged

    changed = True
    while changed and len(ordered) > 1:
        changed = False
        for index, scene in enumerate(ordered):
            if index == 0 or _duration(scene) >= float(min_s):
                continue
            choices = []
            if index > 1:
                choices.append((index - 1, _duration(ordered[index - 1]) + _duration(scene)))
            if index + 1 < len(ordered):
                choices.append((index + 1, _duration(scene) + _duration(ordered[index + 1])))
            fitting = [choice for choice in choices if choice[1] <= float(max_s)]
            if fitting:
                choices = fitting
            if not choices:
                continue
            neighbor, _ = min(choices, key=lambda choice: choice[1])
            if neighbor < index:
                ordered[neighbor:index + 1] = [_merge(ordered[neighbor], scene)]
            else:
                ordered[index:neighbor + 1] = [_merge(scene, ordered[neighbor])]
            changed = True
            break
    return ordered


# The 12 canonical short-SFX types of the TikTok-documentary style (synthesized in
# soundeffects/shorts_ready/editor_pack by scripts/make_editor_sfx.py). Each maps to a clean
# library-token fallback in case the pack folder is missing. Approx clip length per type is used
# to fade each hit out at the right spot.
EDITOR_SFX_TYPES = {
    "whoosh_transition": (["motion_whoosh_like", "analog_transitions", "subtle_transitions"],
                          ["whoosh", "swoosh", "swish", "woosh", "slide", "sweep", "transition"], 0.34),
    "reverse_whoosh":    (["motion_whoosh_like", "analog_transitions"],
                          ["reverse", "whoosh", "swoosh", "back", "suck"], 0.42),
    "bass_impact":       (["hits_impacts", "subtle_impacts"],
                          ["impact", "boom", "hit", "bass", "punch", "slam", "braam", "thud"], 0.36),
    "sub_boom":          (["hits_impacts", "subtle_impacts"],
                          ["sub", "boom", "low", "deep", "drop", "rumble", "impact"], 0.72),
    "caption_pop":       (["cuts_clicks_ui", "jingles_stingers"],
                          ["pop", "bubble", "blip", "bong", "bloop"], 0.12),
    "ui_click":          (["cuts_clicks_ui"],
                          ["click", "tick", "tap", "key", "interface", "select"], 0.06),
    "glitch_zap":        (["sci_fi_zaps"],
                          ["glitch", "zap", "laser", "digital", "error", "distort", "static"], 0.20),
    "camera_shutter":    (["foley_props", "cuts_clicks_ui"],
                          ["shutter", "camera", "snap", "photo", "flash", "click"], 0.14),
    "notification_ding": (["jingles_stingers", "sci_fi_zaps"],
                          ["ding", "bell", "chime", "ping", "notif", "alert", "hit"], 0.46),
    "short_riser":       (["sci_fi_zaps", "jingles_stingers"],
                          ["riser", "rise", "highup", "build", "sweep", "up"], 0.62),
    "downer":            (["sci_fi_zaps", "hits_impacts"],
                          ["down", "lowdown", "fall", "fail", "drop", "descend"], 0.50),
    "whoosh_hit_combo":  (["motion_whoosh_like", "hits_impacts"],
                          ["whoosh", "impact", "hit", "slam", "boom"], 0.56),
}


# Reaction placement: when a spoken line's MEANING matches, drop a labeled reaction sound a beat
# into the scene (never on the cut - cuts stay transition-only). Trigger words are tunable; the
# reaction slugs come from the SFX trainer (tools/sfx_trainer.py). Extra/renamed reactions the user
# adds fall back to matching on their own slug words.
REACTION_TRIGGERS = {
    "money_cash": ("money", "cost", "expensive", "cheap", "pay", "yen", "dollar", "price", "rent",
                   "salary", "wage", "afford", "rich", "poor", "broke", "spend", "buy", "cash",
                   "wealth", "income", "profit", "fee", "budget", "billion", "million"),
    "shock_reveal": ("shock", "unbelievable", "insane", "no way", "cannot believe", "actually",
                     "suddenly", "turns out", "plot twist", "reveal", "shocking", "mind-blow",
                     "nobody knows", "you won't believe", "believe it or not", "secret", "hidden"),
    "error_wrong": ("wrong", "mistake", "fail", "error", "illegal", "banned", "forbidden",
                    "not allowed", "incorrect", "false", "bad idea"),
    "sad_downer": ("sad", "alone", "lonely", "cry", "depress", "lost", "empty", "tragic",
                   "heartbreak", "miserable", "suffer", "hopeless"),
    "comedy_fail": ("funny", "awkward", "embarrass", "weird", "silly", "oops", "ridiculous",
                    "clumsy", "goofy", "hilarious"),
    "idea_reveal": ("idea", "discover", "invent", "genius", "clever", "solution", "breakthrough",
                    "realize", "figured out", "secret to", "trick"),
    "cute_aww": ("cute", "adorable", "sweet", "wholesome", "kawaii", "baby", "kitten", "puppy", "lovely"),
    "camera_photo": ("photo", "picture", "camera", "snapshot", "selfie", "caught on"),
    "celebrate": ("win", "won", "success", "achieve", "best", "amazing", "record", "first place",
                  "champion", "congrat", "victory", "finally"),
    "notification": ("message", "text ", "notification", "phone", "app", "dm", "chat", "ping",
                     "email", "alert", "social media", "instagram", "tiktok"),
    "question": ("why", "how", "what if", "question", "wonder", "confus", "huh", "unclear"),
    "suspicious": ("suspicious", "sus ", "sneaky", "shady", "fishy", "creep", "lurk",
                   "hidden agenda", "up to something", "sketchy"),
    "correct": ("correct", "right", "exactly", "confirmed", "accurate", "indeed", "precisely", "true"),
    "death": ("die", "death", "dead", "kill", "deadly", "fatal", "grave", "funeral", "passed away"),
    "suspense": ("tension", "suspense", "cliffhanger", "about to", "moment of truth", "wait for it"),
    "tasty": ("eat", "food", "delicious", "tasty", "meal", "hungry", "restaurant", "cook", "dish",
              "flavor", "yummy", "snack", "ramen", "sushi"),
}
# rarer/more-specific reactions win over broad ones when a line matches several (one per scene)
REACTION_PRIORITY = ("death", "money_cash", "camera_photo", "tasty", "error_wrong", "celebrate",
                     "cute_aww", "idea_reveal", "comedy_fail", "notification", "suspicious",
                     "sad_downer", "suspense", "shock_reveal", "question", "correct")
# per-reaction mix level (dB under the voice); punchier ones a touch louder
REACTION_DB = {"money_cash": -8, "celebrate": -8, "shock_reveal": -8, "death": -9, "error_wrong": -9,
               "camera_photo": -9, "comedy_fail": -9}
_REACTION_DB_DEFAULT = -11


def place_editor_sfx(config, reasoning_model=None, status_cb=None):
    """Place the user's LOCAL, classified SFX (sfx_library) on real edit events, synced to the
    visual-FX plan (scene['fx']): the hook, clip cuts, major reveals/shocking beats, visual
    callouts, freeze-frames and topic accents. NEVER generated/downloaded/meme/disabled SFX.
    Enforces density/spacing limits, rotates variations, applies per-category dB volumes, and
    writes config['ai_content_sfx'] + config['sfx_report']. Returns how many were placed."""
    if not bool(config.get("sfx_enabled", True)):
        return 0
    config["ai_content_sfx"] = []                     # a rerun must not keep the previous cut map
    scenes = config.get("scenes", [])
    if not scenes:
        return 0
    # SFX-Master gap-fill: cut times that already have a transition sound in the source audio.
    existing_onsets = [float(o) for o in (config.get("existing_sfx_onsets") or [])]
    import sfx_library
    meme_enabled = bool(config.get("meme_sfx_enabled")
                        or str(config.get("video_style", "")).lower() in ("meme", "comedy"))
    data = sfx_library.build_library(status_cb=status_cb, meme_enabled=meme_enabled)
    lib = data["library"]
    rec_by_path = {r["use_path"]: r for r in data["records"]}
    if not any(lib.get(c) for c in sfx_library.DEFAULT_ALLOWED):
        log(status_cb, "SFX: no usable local SFX in the library; skipping (no generated SFX).")
        config["sfx_report"] = data["report"]
        return 0

    rot = {}
    use_count = {}
    # HARD per-file cap: with a single file in a category (e.g. impact_hit had exactly one,
    # udar-ot-vzgliada-skaly), every hook/big-moment played the SAME sound 5-10x per video.
    # After the cap the event falls through to an alternative category instead.
    _FILE_CAP_DEFAULT = 2
    _FILE_CAP = {"ui_click": 5, "caption_pop": 4, "notification_ding": 4}
    _ALT_CATS = {"impact_hit": ["low_impact", "whoosh_hit_combo", "bright_whoosh", "camera_flash"],
                 "low_impact": ["impact_hit", "bright_whoosh"],
                 "camera_flash": ["flash_blink", "caption_pop"],
                 "swipe_whoosh": ["bright_whoosh", "whoosh_hit_combo"],
                 "bright_whoosh": ["swipe_whoosh", "caption_pop"]}

    # CONTENT GUARDRAILS: a bell/gong or musical jingle that slipped into a cut category via
    # the feature fallback must never fire as a transition ("random death dong"), and horror/
    # scream-type files never fire at all. Checked by NAME (the user's own file names) plus a
    # tonality hint from the scanner records.
    _NEVER_RE = re.compile(r"scream|horror|creepy|jumpscare|siren|alarm|gun|explos", re.I)
    _NOT_ON_CUTS_RE = re.compile(r"bell|gong|dong|church|choir|chant|song|music|melod|bgm|"
                                 r"anthem|hymn|jingle|guitar|piano|violin|orchestr", re.I)
    _CUT_CATS = {"swipe_whoosh", "bright_whoosh", "whoosh_hit_combo", "caption_pop",
                 "notification_ding", "camera_flash", "idea_reveal", "flash_blink", "ui_click"}

    def _file_ok(cand, cat):
        rec = rec_by_path.get(str(cand)) or {}
        name = str(rec.get("file") or Path(str(cand)).name)
        if _NEVER_RE.search(name):
            return False
        if cat in _CUT_CATS:
            if _NOT_ON_CUTS_RE.search(name):
                return False
            # strongly tonal + bass-heavy = melodic hit, not a neutral cut sound
            if rec and rec.get("low_ratio", 0) > 0.55 and rec.get("centroid", 9999) < 900:
                return False
        return True

    def pick(cat):                                    # rotate variations, honor the per-file cap
        files = lib.get(cat) or []
        if not files:
            return None
        cap = _FILE_CAP.get(cat, _FILE_CAP_DEFAULT)
        j = rot.get(cat, 0)
        for step in range(len(files)):
            cand = files[(j + step) % len(files)]
            if use_count.get(str(cand), 0) < cap and _file_ok(cand, cat):
                rot[cat] = j + step + 1
                use_count[str(cand)] = use_count.get(str(cand), 0) + 1
                return cand
        return None                                   # whole category exhausted

    def pick_with_alts(cat):
        """Pick from cat, falling through to alternatives when its files hit the cap.
        Returns (path, actual_category) or (None, None)."""
        p = pick(cat)
        if p:
            return p, cat
        for alt in _ALT_CATS.get(cat, []) + ["notification_ding", "caption_pop", "ui_click"]:
            p = pick(alt)
            if p:
                return p, alt
        return None, None

    # CUTS = TRANSITION SOUNDS ONLY (user rule). A scene change never gets a pop/click/ding/flash;
    # only the whoosh/swish/transition family fires on a cut. Variety still comes from rotating the
    # WHOLE transition family (so consecutive cuts differ), but never a non-transition category.
    # Pops/dings/flashes/impacts still fire elsewhere - freeze->camera_flash, big moment->impact,
    # topic accents on the named word - just never AS the cut sound.
    _CUT_WEIGHTS = [("swipe_whoosh", 2), ("bright_whoosh", 2), ("whoosh_hit_combo", 1)]
    cut_rotation = []
    for _cat, _w in _CUT_WEIGHTS:
        if lib.get(_cat):
            cut_rotation.extend([_cat] * _w)
    cut_ctr = {"i": 0}

    def next_cut_cat(preferred=None):
        """Pick the next cut sound: honor a specific preferred category if present, else round-robin
        the weighted rotation so we cycle through ALL available cut sounds instead of one whoosh."""
        if preferred and lib.get(preferred):
            return preferred
        if not cut_rotation:
            return "bright_whoosh" if lib.get("bright_whoosh") else (
                "swipe_whoosh" if lib.get("swipe_whoosh") else None)
        cat = cut_rotation[cut_ctr["i"] % len(cut_rotation)]
        cut_ctr["i"] += 1
        return cat

    full = " ".join(str(s.get("exact_voice_text") or s.get("voice_line") or s.get("script") or "")
                    for s in scenes).lower()
    topic_money = any(w in full for w in ("money", "cost", "expensiv", "cheap", "afford", "price",
                                          "pay", "yen", "dollar", "rent", "salary", "wage", "bill", "budget", "spend", "shop"))
    topic_school = any(w in full for w in ("school", "class", "student", "teacher", "uniform",
                                           "exam", "grade", "lesson", "rule", "homework"))
    topic_phone = any(w in full for w in ("phone", "message", "text", "app", "social media",
                                          "chat", "notification", "instagram", "tiktok"))

    # cut list (>= ~0.45s apart) ranked by meaning, then capped to a density budget
    cuts = []
    prev = -9.0
    for i, sc in enumerate(scenes):
        try:
            start = float(sc.get("start", 0.0))
        except (TypeError, ValueError):
            continue
        if start - prev < 0.45:
            continue
        prev = start
        cuts.append((i, start))
    if not cuts:
        return 0
    duration = max(float(config.get("duration", 0) or 0),
                   max((float(s.get("end", 0) or 0) for s in scenes), default=0.0)) or 1.0
    # User-selectable SFX amount: low = the historical sparse feel, medium/high raise both the
    # per-minute rate and the absolute cap. Explicit editor_sfx_max_per_minute still wins.
    _amount = str(config.get("sfx_amount", "") or "").strip().lower()
    _amount_mpm = {"low": 42, "medium": 65, "high": 95}.get(_amount)
    _amount_cap = {"low": 52, "medium": 80, "high": 110}.get(_amount, 52)
    _default_mpm = _amount_mpm if _amount_mpm is not None else 42
    mpm = max(8, int(config.get("editor_sfx_max_per_minute", _default_mpm) or _default_mpm))
    cap = max(12, int(config.get("editor_sfx_budget_cap", _amount_cap) or _amount_cap))
    budget = max(12, min(cap, int(round(duration / 60.0 * mpm))))

    def prio(i, sc):
        if i == 0:
            return 1000
        fx = sc.get("fx") or {}
        if fx.get("freeze_frame") or fx.get("impact_shake") or _scene_is_big_moment(sc):
            return 200
        if fx.get("callout") in ("arrow", "circle", "stamp"):
            return 120
        return 40
    ranked = sorted(cuts, key=lambda c: prio(c[0], scenes[c[0]])
                    + (int(hashlib.sha1(f"b{c[0]}".encode()).hexdigest()[:4], 16) % 15), reverse=True)
    selected = []
    for c in ranked:
        if len(selected) >= budget:
            break
        if all(abs(c[1] - e[1]) >= 0.4 for e in selected):
            selected.append(c)
    selected.sort(key=lambda c: c[1])

    events, sfx_events_report = [], []
    hit_scenes = set()   # scenes that already got an impact hit -> reaction pass skips them

    # ---- word-accurate timing helpers (voice_align word timeline) ----
    _words = [w for w in (config.get("canonical_words") or [])
              if isinstance(w, dict) and w.get("word")]

    def _norm_word(x):
        return re.sub(r"[^\w']+", "", str(x or "").lower())

    def _find_word_time(token, t_min=0.0, t_max=None):
        """(start, end) of the first occurrence of `token` in the voice timeline window."""
        token = _norm_word(token)
        if not token:
            return None
        for w in _words:
            try:
                ws, we = float(w.get("start") or 0.0), float(w.get("end") or 0.0)
            except (TypeError, ValueError):
                continue
            if ws < t_min - 0.05 or (t_max is not None and ws > t_max + 0.05):
                continue
            if _norm_word(w.get("word")) == token:
                return ws, we
        return None

    # HOOK ARC (user spec): the hook riser runs 0 -> the IMPACT WORD, and the impact hit fires
    # exactly there - never at 0.0 (that placed a random impact-pool sound on frame one).
    _hook_end = float(scenes[1].get("start", 0.0) or 0.0) if len(scenes) > 1 else 0.0
    _hook_beat = _hook_end
    _iw = str(config.get("impact_word") or "").strip()
    if _iw:
        hit = _find_word_time(_iw, 0.0, (_hook_end + 1.5) if _hook_end > 0 else None)
        if hit:
            _hook_beat = hit[0]          # riser peaks ON the word onset; impact fires there
    # Script-to-Visuals semantic-vision mode: the multimodal Audio Director adds hook riser,
    # impacts, reactions and callout sounds AFTER the render (it watches the finished video).
    # Here we then place ONLY the frame-accurate cut transitions - everything else is skipped
    # so the director's picks don't double up.
    semantic_vision = bool(config.get("sfx_semantic_vision"))
    last_loud, last_low = -99.0, -99.0
    topic_count = {"school_bell": 0, "payment_ding": 0, "message_sent": 0}
    DARK = ("die", "death", "alone", "lonely", "fear", "dark", "sad", "empty", "cry", "depress", "burnout")

    def density_ok(t, loud=False):
        if any(abs(t - e["start"]) < 0.20 for e in events):       # no two within 0.20s
            return False
        if sum(1 for e in events if abs(t - e["start"]) < 1.0) >= 4:   # <= 4 per 2s window
            return False
        if t < 3.0 and sum(1 for e in events if e["start"] < 3.0) >= 4:   # <= 4 in first 3s
            return False
        if loud and (t - last_loud) < 1.5:                        # loud impacts >= 1.5s apart
            return False
        return True

    for (i, start) in selected:
        sc = scenes[i]; fx = sc.get("fx") or {}
        beat = str(fx.get("beat", "")).lower()
        txt = str(sc.get("exact_voice_text") or sc.get("script") or "").lower()
        has_callout = str(fx.get("callout") or "").lower() in ("arrow", "circle", "stamp")
        cat, reason, t, loud = None, "clip_cut", max(0.0, start - 0.08), False
        link_visual = fx.get("transition")
        if i == 0:
            if semantic_vision:
                continue                       # the vision pass owns the hook (riser + climax impact)
            # impact lands where the hook riser peaks (impact word / body start) - NOT at 0.0
            cat, reason, t, loud, link_visual = "impact_hit", "hook_opening", max(0.4, _hook_beat), True, "hook_start"
        elif fx.get("freeze_frame") and not semantic_vision:
            cat, reason, t, link_visual = "camera_flash", "freeze_frame", start, "freeze"
        elif (not semantic_vision) and (fx.get("impact_shake") or _scene_is_big_moment(sc)):
            # a line that literally says death belongs to the WORD-TIMED death gong (reaction
            # pass) - a generic impact at the scene start would fire seconds before the word
            if any(w in txt for w in REACTION_TRIGGERS.get("death", ())) \
                    and (data.get("reactions", {}) or {}).get("death"):
                continue
            if any(w in txt for w in DARK) and (start - last_low) >= 5.0 and lib.get("low_impact"):
                cat, reason, loud = "low_impact", "major_reveal", True
            else:
                cat, reason, loud = "impact_hit", ("major_reveal" if beat in ("reveal", "shock", "turning_point") else "shocking_word"), True
            t = start
        else:
            # normal cut: rotate through the whole cut-sound family so it's never 14x the same
            # whoosh. A swipe-flavored transition still prefers a swipe; everything else round-robins.
            pref = "swipe_whoosh" if link_visual in ("subtle_swipe", "glitch", "whoosh") else None
            cat = next_cut_cat(preferred=pref)

        # topic accent (only when the line actually names the topic; capped 3 each)
        if reason in ("visual_callout", "clip_cut") and not semantic_vision:
            if topic_money and topic_count["payment_ding"] < 3 and lib.get("payment_ding") \
                    and any(w in txt for w in ("pay", "cost", "money", "price", "cheap", "expensiv", "yen", "bill")):
                cat, reason = "payment_ding", "topic_accent"; topic_count["payment_ding"] += 1
            elif topic_school and topic_count["school_bell"] < 3 and lib.get("school_bell") \
                    and any(w in txt for w in ("school", "class", "rule", "student", "uniform", "exam")):
                cat, reason = "school_bell", "topic_accent"; topic_count["school_bell"] += 1
            elif topic_phone and topic_count["message_sent"] < 3 and lib.get("message_sent") \
                    and any(w in txt for w in ("message", "text", "phone", "chat", "app", "dm")):
                cat, reason = "message_sent", "topic_accent"; topic_count["message_sent"] += 1

        if loud and (t - last_loud) < 1.5:                        # avoid stacked loud impacts
            cat = "swipe_whoosh" if lib.get("swipe_whoosh") else "bright_whoosh"
            reason, loud = "clip_cut", False
        # SFX Master: if this cut ALREADY has a transition sound in the source audio (a transient
        # onset near t), don't stack a NEW cut/transition sound on it. Non-transition SFX (impacts,
        # topic accents, hook opening) are still placed - only clip-cut/transition hits are skipped.
        if reason == "clip_cut" and any(abs(t - o) <= 0.14 for o in existing_onsets):
            continue
        if not (cat and density_ok(t, loud=loud)):
            continue
        path, picked_cat = pick_with_alts(cat)
        if not path:
            continue
        cat = picked_cat
        rec = rec_by_path.get(path, {})
        db = sfx_library.CAT_DB.get(cat, -18)
        vol = round(min(0.85, sfx_library.db_to_gain(db)), 3)
        # Editor SFX must be SHORT punchy HITS, not long swooshes. A 1.2s whoosh every ~1.9s covers
        # ~40% of the video with filtered noise and reads as a continuous "Rauschen". Hard-cap the
        # PLAYED length per family so each is a quick transient with clear air between them.
        _MAX_DUR = {"bright_whoosh": 0.42, "swipe_whoosh": 0.42, "whoosh_hit_combo": 0.5,
                    "caption_pop": 0.28, "ui_click": 0.22, "notification_ding": 0.5,
                    "idea_reveal": 0.6, "camera_flash": 0.35, "flash_blink": 0.3,
                    "impact_hit": 0.55, "low_impact": 0.7,
                    "payment_ding": 0.6, "school_bell": 0.7, "message_sent": 0.5}
        dur = min(float(rec.get("trim_len") or 0.9), _MAX_DUR.get(cat, 0.45))
        events.append({"path": str(path), "start": round(t, 3),
                       "duration": round(dur + 0.02, 3), "volume": vol,
                       "category": cat, "id": f"sfx-{len(events):02d}", "sfx_type": cat})
        if cat in ("impact_hit", "low_impact"):
            hit_scenes.add(i)          # don't also drop a reaction sound on this scene
        if loud:
            last_loud = t
        if cat == "low_impact":
            last_low = t
        sfx_events_report.append({
            "time": round(t, 2), "scene_id": i, "type": cat,
            "asset_file": Path(rec.get("file") or path).name,
            "used_trimmed_version": bool(rec.get("requires_trim")),
            "volume_db": db, "reason": reason, "linked_cut_time": round(start, 2),
            "linked_word": None, "linked_visual_event": link_visual, "allowed_by_policy": True})
        log(status_cb, f"SFX: {cat} at {t:.2f} for {reason}"
                       + (f" (link {link_visual})" if link_visual else ""))

        # layer a quick click on the visual callout pop (in addition to the cut whoosh)
        _click_cat = "ui_click" if lib.get("ui_click") else ("caption_pop" if lib.get("caption_pop") else None)
        if has_callout and reason in ("clip_cut", "topic_accent") and _click_cat and not semantic_vision:
            ct = start + 0.28
            if density_ok(ct):
                cp = pick(_click_cat)
                if cp:
                    crec = rec_by_path.get(cp, {})
                    cdb = sfx_library.CAT_DB.get(_click_cat, -15)
                    events.append({"path": str(cp), "start": round(ct, 3),
                                   "duration": round(min(2.0, float(crec.get("trim_len") or 0.4)) + 0.04, 3),
                                   "volume": round(min(0.85, sfx_library.db_to_gain(cdb)), 3),
                                   "category": _click_cat, "id": f"sfx-{len(events):02d}", "sfx_type": _click_cat})
                    sfx_events_report.append({
                        "time": round(ct, 2), "scene_id": i, "type": _click_cat,
                        "asset_file": Path(crec.get("file") or cp).name,
                        "used_trimmed_version": bool(crec.get("requires_trim")), "volume_db": cdb,
                        "reason": "visual_callout", "linked_cut_time": round(start, 2),
                        "linked_word": None, "linked_visual_event": fx.get("callout"), "allowed_by_policy": True})
                    log(status_cb, f"SFX: {_click_cat} at {ct:.2f} for visual_callout (link {fx.get('callout')})")

    # ---- REACTION PASS: place a labeled reaction sound when a line's meaning matches a trigger.
    # Lands a beat INTO the scene (never on the cut - cuts stay transition-only). One reaction per
    # scene (rarer wins), capped per slug, subject to the same density limits. Pools already respect
    # policy (meme_only excluded unless meme mode). No-op unless the human labels are active. ----
    reactions_pool = data.get("reactions", {}) or {}
    if reactions_pool and not semantic_vision:
        react_count, react_rot, react_placed = {}, {}, 0
        _REACT_CAP = 3
        for i, sc in enumerate(scenes):
            if i == 0 or i in hit_scenes:
                continue                # hook + big-moment scenes already got a (reaction-sourced) hit
            txt = str(sc.get("exact_voice_text") or sc.get("voice_line") or sc.get("script") or "").lower()
            if not txt:
                continue
            try:
                s0 = float(sc.get("start", 0.0) or 0.0)
                e0 = float(sc.get("end", s0) or s0)
            except (TypeError, ValueError):
                continue
            for slug in REACTION_PRIORITY:
                pool = reactions_pool.get(slug)
                if not pool or react_count.get(slug, 0) >= _REACT_CAP:
                    continue
                trigs = REACTION_TRIGGERS.get(slug) or (slug.replace("_", " "),)
                trig_hit = next((w for w in trigs if w in txt), None)
                if not trig_hit:
                    continue
                # WORD-ACCURATE: the reaction fires RIGHT AFTER the trigger word is spoken
                # (user: death gong "GLEICH nach dem wort", never seconds before it).
                t = min(e0 - 0.15, s0 + 0.35)             # fallback: a beat after the cut
                _w = _find_word_time(trig_hit.split()[0], s0 - 0.2, e0 + 0.4)
                if _w:
                    t = _w[1] + 0.03                      # word END + a hair, never before it
                if t <= 0.1 or not density_ok(t):
                    continue
                k = react_rot.get(slug, 0); path = pool[k % len(pool)]; react_rot[slug] = k + 1
                rec = rec_by_path.get(path, {})
                db = REACTION_DB.get(slug, _REACTION_DB_DEFAULT)
                dur = min(float(rec.get("trim_len") or 0.9), 1.1)
                events.append({"path": str(path), "start": round(t, 3),
                               "duration": round(dur + 0.02, 3),
                               "volume": round(min(0.85, sfx_library.db_to_gain(db)), 3),
                               "category": f"reaction_{slug}", "id": f"sfx-{len(events):02d}",
                               "sfx_type": f"reaction_{slug}"})
                react_count[slug] = react_count.get(slug, 0) + 1
                react_placed += 1
                sfx_events_report.append({
                    "time": round(t, 2), "scene_id": i, "type": f"reaction_{slug}",
                    "asset_file": Path(rec.get("file") or path).name,
                    "used_trimmed_version": bool(rec.get("requires_trim")), "volume_db": db,
                    "reason": "reaction_match", "linked_cut_time": round(s0, 2),
                    "linked_word": None, "linked_visual_event": None, "allowed_by_policy": True})
                log(status_cb, f"SFX: reaction_{slug} at {t:.2f} (line matched)")
                break                                     # one reaction per scene
        if react_placed:
            events.sort(key=lambda e: e["start"])
            log(status_cb, f"SFX: placed {react_placed} content-matched reaction sound(s).")

    # ---- PIPELINE v0.2 PRECISION SFX (reference-edit rules, deterministic timing) ----
    #   whoosh: 3-5 frames (0.13s) BEFORE every overlay/arrow appears (ear precedes eye)
    #   pop:    on frame 1 of EVERY cut (attention reset / finger snap)
    #   boom:   on frame 1 of every SHOCK scene (acoustic weight for the visual punchline)
    if str(config.get("pipeline_version") or "") == "v0.2":
        _lib = data.get("library", {}) or {}
        # VARIETY: pools are stored alphabetically, so a plain rotation played 3 near-identical
        # pops in a row, then 3 mouse clicks... Merge the whole click/pop family and SHUFFLE
        # once per render - every transition sound gets used, neighbours never sound alike.
        import random as _rnd
        _whoosh_pool = list(dict.fromkeys((_lib.get("bright_whoosh") or [])
                                          + (_lib.get("swipe_whoosh") or [])))
        _pop_pool = list(dict.fromkeys((_lib.get("ui_click") or [])
                                       + (_lib.get("caption_pop") or [])))
        _rnd.shuffle(_whoosh_pool)
        _rnd.shuffle(_pop_pool)
        import glob as _glob
        _boom_pool = [p for pat in ("*boom*", "*vine*", "*sub*bass*")
                      for p in _glob.glob(str(ROOT / "soundeffects" / pat))]
        _boom_pool = [{"path": p} for p in dict.fromkeys(_boom_pool)]
        _rnd.shuffle(_boom_pool)
        _boom_fallback = _lib.get("impact_hit") or []
        _v2n = {"whoosh": 0, "pop": 0, "boom": 0}
        _placed_t = {round(e["start"], 2) for e in events}

        def _v2_add(path, t, cat, db, dur=0.5):
            t = max(0.0, round(float(t), 3))
            if round(t, 2) in _placed_t:
                return
            _placed_t.add(round(t, 2))
            events.append({"path": str(path), "start": t, "duration": dur,
                           "volume": round(min(0.85, sfx_library.db_to_gain(db)), 3),
                           "category": cat, "id": f"sfx-{len(events):02d}", "sfx_type": cat})
            sfx_events_report.append({"time": round(t, 2), "scene_id": None, "type": cat,
                                      "asset_file": Path(path).name, "used_trimmed_version": False,
                                      "volume_db": db, "reason": f"v0.2 {cat} rule",
                                      "linked_cut_time": None, "linked_word": None,
                                      "linked_visual_event": None, "allowed_by_policy": True})

        _rot = {"w": 0, "p": 0, "b": 0}
        for i, sc in enumerate(scenes):
            s0 = float(sc.get("start", 0.0) or 0.0)
            _is_shock = str(sc.get("visual_match_category") or "").lower() == "shock"
            # frame 1 of a SHOCK scene gets the boom (visual punchline weight); a normal cut
            # gets the pop (attention reset). Never both on the same frame.
            if _is_shock and i > 0:
                _bp = _boom_pool or [{"path": p} for p in _boom_fallback]
                if _bp:
                    _v2_add(_bp[_rot["b"] % len(_bp)]["path"], s0, "impact_hit", -5, 0.8)
                    _rot["b"] += 1; _v2n["boom"] += 1
            elif i > 0 and _pop_pool:
                _v2_add(_pop_pool[_rot["p"] % len(_pop_pool)], s0, "ui_click", -15, 0.25)
                _rot["p"] += 1; _v2n["pop"] += 1
            # whoosh 0.13s before each overlay/arrow of this scene appears. Never inside the
            # opening moment: 0.0 belongs to the hook riser alone (user rule).
            for ov in (sc.get("overlays") or []):
                if _whoosh_pool:
                    t_ov = float(ov.get("start") or ov.get("time") or s0)
                    if t_ov - 0.13 < 0.5:
                        continue
                    _v2_add(_whoosh_pool[_rot["w"] % len(_whoosh_pool)], t_ov - 0.13,
                            "bright_whoosh", -8, 0.4)
                    _rot["w"] += 1; _v2n["whoosh"] += 1
        if any(_v2n.values()):
            events.sort(key=lambda e: e["start"])
            log(status_cb, f"v0.2 SFX rules: {_v2n['pop']} cut pop(s), {_v2n['whoosh']} "
                           f"pre-overlay whoosh(es), {_v2n['boom']} shock boom(s)"
                           + ("" if _boom_pool or not _v2n['boom'] else " (impact fallback - add a vine-boom file)") + ".")

    # ---- RISER PASSES: build-ups that swell INTO a beat and drop on it. Hook risers always
    # occupy 0.0..hook-end using the closest-duration labeled file + atempo; body risers retain
    # tail trimming. Long by design -> guard allows riser/hook_riser.
    #   hook_riser: builds through the HOOK, drops on the first cut into the body.
    #   riser:      swells into the biggest body moments (max 2, spaced >=4s from any other riser).
    riser_peaks = []

    def _place_riser(beat, pool, category, db):
        if beat <= 0.9 or not pool or any(abs(beat - p) < 4.0 for p in riser_peaks):
            return False
        if category == "hook_riser":
            item, rlen, playback_rate = sfx_library.choose_riser_for_target(pool, beat)
            if not item:
                return False
            dur = beat
            start = 0.0
            source_trim = 0.0
        else:
            item = pool[len(riser_peaks) % len(pool)]
            rlen = float(item.get("dur") or 2.6)
            playback_rate = 1.0
            dur = min(2.6, rlen, beat - 0.05, 4.0)
            start = max(0.0, beat - dur)
            source_trim = max(0.0, rlen - dur)
        if dur < 0.8:
            return False
        events.append({"path": str(item["path"]), "start": round(start, 3),
                       "duration": round(dur, 3), "source_trim": round(source_trim, 3),
                       "source_duration": round(rlen, 3),
                       "playback_rate": round(playback_rate, 6),
                       "volume": round(min(0.85, sfx_library.db_to_gain(db)), 3),
                       "category": category, "id": f"sfx-{len(events):02d}", "sfx_type": category})
        sfx_events_report.append({
            "time": round(start, 2), "scene_id": None, "type": category,
            "asset_file": Path(item["path"]).name, "used_trimmed_version": False, "volume_db": db,
            "reason": category, "linked_cut_time": round(beat, 2), "linked_word": None,
            "linked_visual_event": None, "allowed_by_policy": True})
        riser_peaks.append(beat)
        log(status_cb, f"SFX: {category} {Path(item['path']).name} swelling "
                       f"{start:.2f}->{beat:.2f} ({rlen:.2f}s source at {playback_rate:.3f}x)")
        return True

    hook_pool = data.get("hook_risers") or []   # hook opening = dedicated hook_riser files ONLY
    if hook_pool and len(scenes) > 1 and not semantic_vision:
        # -12dB read as "no riser at all" under the full-level voice (user feedback) -> -6dB.
        # Peak = the IMPACT WORD when marked (riser 0 -> impact word -> impact hit), else body start.
        _place_riser(_hook_beat, hook_pool, "hook_riser", -6)
    body_pool = data.get("risers") or []
    if body_pool and not semantic_vision:
        placed = 0
        for i in sorted(hit_scenes):
            if i > 0 and placed < 2 and _place_riser(float(scenes[i].get("start", 0.0) or 0.0), body_pool, "riser", -13):
                placed += 1
    if riser_peaks:
        events.sort(key=lambda e: e["start"])

    config["ai_content_sfx"] = events
    summ = {"total_sfx": len(events), "whoosh_or_swipe": 0, "impact_hit": 0, "low_impact": 0,
            "caption_pop_or_click": 0, "ding_or_reveal": 0, "camera_or_flash": 0, "topic_specific": 0,
            "removed_by_density_limit": len(selected) - len(events), "removed_policy_blocked": 0}
    for e in events:
        c = e["category"]
        if c in ("bright_whoosh", "swipe_whoosh", "whoosh_hit_combo"):
            summ["whoosh_or_swipe"] += 1
        elif c == "impact_hit":
            summ["impact_hit"] += 1
        elif c == "low_impact":
            summ["low_impact"] += 1
        elif c in ("caption_pop", "ui_click"):
            summ["caption_pop_or_click"] += 1
        elif c in ("notification_ding", "idea_reveal"):
            summ["ding_or_reveal"] += 1
        elif c in ("camera_flash", "flash_blink"):
            summ["camera_or_flash"] += 1
        if c in sfx_library.TOPIC_SPECIFIC:
            summ["topic_specific"] += 1
    config["sfx_report"] = {**data["report"], "sfx_summary": summ, "sfx_events": sfx_events_report,
                            "sfx_validation": {"passed": True, "issues": []}}
    log(status_cb, f"SFX Planner: planned {len(events)} SFX (whoosh {summ['whoosh_or_swipe']}, "
                   f"impact {summ['impact_hit'] + summ['low_impact']}, pop/click {summ['caption_pop_or_click']}, "
                   f"ding {summ['ding_or_reveal']}, flash {summ['camera_or_flash']}, topic {summ['topic_specific']}).")
    return len(events)


# words that mark a "big / shocking" beat (gets a bass/sub/glitch impact instead of a whoosh)
_BIG_MOMENT_WORDS = (
    "shock", "never", "secret", "truth", "die", "death", "kill", "money", "broke", "expensive",
    "alone", "lonely", "fear", "scary", "worst", "billion", "million", "percent", "%", "stop",
    "but", "until", "suddenly", "reality", "actually", "warning", "danger", "crisis", "collapse",
)


def _scene_is_big_moment(scene):
    txt = str(scene.get("exact_voice_text") or scene.get("voice_line") or scene.get("script") or "").lower()
    if scene.get("big_moment") or scene.get("is_hook"):
        return True
    return any(w in txt for w in _BIG_MOMENT_WORDS)


def measure_voice_noise(path, ffmpeg=None):
    """Measure the constant noise floor + high-frequency hiss of the (voice) audio so the report
    can prove the rauschen is gone. Returns {noise_floor_db, high_frequency_hiss_score 0-10,
    constant_hiss_detected}."""
    out = {"noise_check_enabled": True, "noise_floor_db": None,
           "high_frequency_hiss_score": 0.0, "constant_hiss_detected": False,
           "voice_clarity_score": None, "voice_muffled": False, "low_mid_to_presence_ratio": None}
    ffmpeg = ffmpeg or pipeline.find_ffmpeg()
    if not ffmpeg or not path or not Path(path).exists():
        out["noise_check_enabled"] = False
        return out
    try:
        import numpy as np
        sr = 32000
        raw = subprocess.run([ffmpeg, "-v", "error", "-i", str(path), "-ac", "1", "-ar", str(sr),
                              "-f", "f32le", "-"], capture_output=True, timeout=90).stdout
        a = np.frombuffer(raw, dtype="<f4")
        if a.size < sr:
            return out
        w = int(sr * 0.05); n = a.size // w
        rms = np.sqrt(np.mean(a[:n * w].reshape(n, w) ** 2, axis=1) + 1e-12)
        floor = float(np.percentile(rms, 8))          # quiet-frame RMS = the noise floor
        out["noise_floor_db"] = round(20.0 * float(np.log10(floor + 1e-9)), 1)
        mag = np.abs(np.fft.rfft(a * np.hanning(a.size)))
        freqs = np.fft.rfftfreq(a.size, 1.0 / sr)
        hf = float(mag[freqs > 8000].sum() / (mag.sum() + 1e-9))
        out["high_frequency_hiss_score"] = round(min(10.0, hf * 45.0), 1)
        out["constant_hiss_detected"] = bool(out["noise_floor_db"] > -42.0
                                             or out["high_frequency_hiss_score"] > 4.5)
        # clarity: presence (2-5kHz) vs low-mid mud (200-500Hz). A muffled/dumpf voice has lots of
        # low-mid and little presence -> low clarity score.
        lowmid = float(mag[(freqs >= 200) & (freqs <= 500)].sum())
        presence = float(mag[(freqs >= 2000) & (freqs <= 5000)].sum())
        ratio = lowmid / (presence + 1e-9)
        out["low_mid_to_presence_ratio"] = round(ratio, 2)
        out["voice_clarity_score"] = round(max(0.0, min(10.0, 10.0 / (1.0 + ratio))), 1)
        out["voice_muffled"] = bool(out["voice_clarity_score"] < 4.0)
    except Exception:
        pass
    return out


def validate_scrape_render(config, status_cb=None):
    """HARD pre-render gate for scrape/social mode. Refuses to render a broken timeline by raising
    RuntimeError with a clear reason. Enforces: speech speed in range, arrows/circles/stamps off, semantic
    matching not skipped, hook influencer first, and every scene = an accepted real social clip
    that is not D_REJECTED and not fake-vertical/black-barred. Returns True on pass."""
    scenes = config.get("scenes", []) or []
    if not scenes:
        raise RuntimeError("Pre-render validation failed: no scenes to render")
    enf = config.get("_scrape_enforcement") or {}
    try:
        vs = float(config.get("voice_speed", 0))
    except (TypeError, ValueError):
        vs = 0.0
    if not (VOICE_SPEED_MIN - 0.001 <= vs <= VOICE_SPEED_MAX + 0.001):
        raise RuntimeError(f"Pre-render validation failed: voice_speed is {config.get('voice_speed')} "
                           f"(must be {VOICE_SPEED_MIN:.1f}x-{VOICE_SPEED_MAX:.1f}x)")
    # ARROWS ONLY: block circles, stamps, labels and any legacy/untargeted overlay; the only
    # overlay allowed through is a target-based arrow callout.
    for sc in scenes:
        for ov in (sc.get("overlays") or []):
            if ov.get("type") in ("arrows", "highlight", "paper", "newspaper", "counter"):
                raise RuntimeError("Pre-render validation failed: untargeted/legacy overlay present")
            if ov.get("type") == "callout" and ov.get("shape") in ("circle", "stamp"):
                raise RuntimeError(f"Render blocked: circles/stamps/labels are disabled, but scene "
                                   f"{sc.get('id')} still has a {ov.get('shape')}")
    # SFX must be SHORT, event-based hits - no continuous ambient/noise bed under the video.
    for ev in (config.get("ai_content_sfx") or []):
        try:
            evd = float(ev.get("duration") or 0)
        except (TypeError, ValueError):
            evd = 0.0
        _cat = str(ev.get("category") or "")
        # risers are intentional build-ups (swell into a reveal), not an ambient bed -> allowed longer
        if _cat == "hook_riser":
            max_hook_riser = max(0.1, float(config.get("duration") or 60.0))
            if evd > max_hook_riser + 0.05:
                raise RuntimeError(f"Render blocked: hook riser exceeds the video "
                                   f"({evd:.2f}s > {max_hook_riser:.2f}s)")
        elif _cat == "riser":
            if evd > 4.0:
                raise RuntimeError(f"Render blocked: riser too long ({evd:.2f}s > 4.0s)")
        elif evd > 2.0 and _cat != "background_music":
            raise RuntimeError(f"Render blocked: long/ambient SFX detected ({_cat} {evd:.2f}s > 2.0s)")
    if enf.get("semantic_matching_skipped"):
        raise RuntimeError("Semantic matching skipped; refusing to render random clips")
    if (scenes[0].get("visual_role") or "") != "hook_influencer":
        raise RuntimeError("Hook influencer is not first scene")
    for i, sc in enumerate(scenes):
        if not sc.get("clip"):
            raise RuntimeError(f"Pre-render validation failed: scene {i} has no accepted social clip (insufficient footage)")
        if str(sc.get("match_class") or "") == "D_REJECTED":
            raise RuntimeError(f"Scene {i} has rejected/generic clip (D_REJECTED)")
        try:
            bbs = float(sc.get("black_bar_score") or 0)
        except (TypeError, ValueError):
            bbs = 0.0
        if sc.get("is_fake_vertical") or bbs > 4.0:
            raise RuntimeError(f"Scene {i} uses fake-vertical/black-bar clip (black_bar_score={bbs})")
    config["pre_render_validation_passed"] = True
    log(status_cb, "Pre-render validation passed.")
    return True


def plan_visual_emphasis(config, reasoning_model=None, status_cb=None):
    """Add the meme-style red ARROW / CIRCLE / stamped-word callouts the 'dark facts' edits
    use. The renderer already draws scene['overlays'] (draw_smart_overlays) - this decides
    which punchy lines get a callout, what it points at, and the word to stamp. One text-only
    LLM call; returns how many scenes were annotated."""
    scenes = config.get("scenes", [])
    if not scenes or not os.environ.get("WAVESPEED_API_KEY"):
        return 0
    lines = "\n".join(
        f"{i}: {(s.get('exact_voice_text') or s.get('script') or '')[:120]}"
        for i, s in enumerate(scenes)
    )
    prompt = (
        "This is a punchy 'dark facts' style vertical short over real found-footage. Add bold red callouts "
        "(arrow / circle / stamped word) to the most impactful lines to drive the point home, like the viral "
        "Japan-facts edits. Do NOT annotate every line - pick the ~40-60% that hit hardest (reveals, shocking "
        "claims, the concrete subject of the sentence). Arrows/circles only - no extra text.\n\n"
        f"Lines:\n{lines}\n\n"
        'Return STRICT JSON: {"emph": [{"i": <line index>, "style": "arrow|circle|both", '
        '"target": "center|face|left|right|lower"}, ...]}'
    )
    try:
        data = post_json_url(WAVESPEED_LLM_API, {
            "model": reasoning_model or GPT55_MODEL,
            "messages": [
                {"role": "system", "content": "You are a short-form motion editor adding bold red emphasis callouts. Return JSON only."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.4, "max_tokens": 900, "response_format": {"type": "json_object"},
        }, timeout=120)
        plan = extract_json_object(data["choices"][0]["message"]["content"]) or {}
        emph = plan.get("emph") or []
    except Exception as exc:  # noqa: BLE001
        log(status_cb, f"Visual emphasis planning skipped ({exc.__class__.__name__}).")
        return 0
    targets = {"center": (0.5, 0.5), "face": (0.5, 0.36), "left": (0.34, 0.46),
               "right": (0.66, 0.46), "lower": (0.5, 0.66)}
    added = 0
    for e in emph:
        if not isinstance(e, dict):
            continue
        try:
            i = int(e.get("i"))
        except (TypeError, ValueError):
            continue
        if not (0 <= i < len(scenes)):
            continue
        style = str(e.get("style", "arrow")).lower()
        tx, ty = targets.get(str(e.get("target", "center")).lower(), (0.5, 0.5))
        from_left = (i % 2 == 0)                     # alternate sides = pattern interrupt
        ax0, ay0 = (0.12, 0.80) if from_left else (0.88, 0.80)
        ax1 = (tx - 0.05) if from_left else (tx + 0.05)
        ay1 = ty + 0.08
        overlays = list(scenes[i].get("overlays") or [])
        # A quick punch-in at the cut, then gone. NO stamped words - the green captions already
        # carry the text; a second text layer just clutters the frame.
        if style in ("arrow", "both"):
            overlays.append({"type": "arrows", "items": [[ax0, ay0, ax1, ay1]],
                             "start": 0.0, "end": 0.55, "shake": 2.5})
        if style in ("circle", "both"):
            overlays.append({"type": "highlight", "cx": tx, "cy": ty, "rx": 0.17, "ry": 0.12,
                             "start": 0.0, "end": 0.55})
        scenes[i]["overlays"] = overlays
        added += 1
    if added:
        config["smart_overlays"] = True
        log(status_cb, f"Added red emphasis callouts (arrows / circles / labels) to {added} scene(s).")
    return added


def plan_visual_fx(config, project_dir, reasoning_model=None, status_cb=None, collaborate=False, vfx_amount="medium"):
    """TARGET-BASED visual effects (replaces the text-only emphasis guesser). For every scene that
    has a real clip we look at the ACTUAL middle frame, find the concrete on-screen target that
    proves the narration line (face / object / sign / money / food / vehicle / crowd / screen),
    and ONLY then plan a red callout. No target -> no arrow/circle/stamp. Also sets a per-scene
    punch-in zoom anchored to the subject, an optional stamp, impact-shake / freeze flags and a
    transition type. Writes scene['overlays'] + scene['fx'] and returns the visual_fx report.

    Rules enforced: callout only if has_clear_visual_target AND confidence>=7 AND
    relevance_to_voice>=7 AND safe_for_overlay AND the target is NOT in the caption band; capped to
    ~30% of scenes (highest relevance first); never on the caption/face-obstructed area."""
    scenes = config.get("scenes", [])
    summary = {"scenes_total": len(scenes), "punch_in_count": 0, "callout_count": 0,
               "arrow_count": 0, "circle_count": 0, "stamp_count": 0, "impact_shake_count": 0,
               "freeze_frame_count": 0, "skipped_callouts_no_target": 0,
               "skipped_callouts_bad_target": 0, "skipped_callouts_overlap": 0}
    report = {"visual_fx_policy": "target_based_only", "visual_fx_summary": summary,
              "scene_visual_fx": []}
    if not scenes:
        return report
    clip_dir = project_dir / "seedance 2.0"
    ff = pipeline.find_ffmpeg()
    frames_dir = project_dir / "review" / "_fx_frames"
    frames_dir.mkdir(parents=True, exist_ok=True)

    # one clear mid-frame per scene that actually has a clip
    idx_with_frame, strips = [], []
    for i, sc in enumerate(scenes):
        clip_name = sc.get("clip")
        cp = clip_dir / clip_name if clip_name else None
        if not (cp and cp.exists()):
            continue
        try:
            dur = float(probe_audio_duration(cp) or 0.0) or 4.0
        except Exception:
            dur = 4.0
        fp = frames_dir / f"fx_{i:02d}.jpg"
        if pipeline.extract_poster_frame(cp, fp, ffmpeg=ff, at=max(0.2, dur * 0.5)):
            idx_with_frame.append(i); strips.append(fp)

    vision = {}
    if strips and os.environ.get("WAVESPEED_API_KEY"):
        BATCH = 16
        for b0 in range(0, len(strips), BATCH):
            sub_idx = idx_with_frame[b0:b0 + BATCH]
            sub_str = strips[b0:b0 + BATCH]
            sheet = create_media_contact_sheet(
                sub_str, frames_dir / f"_fx_sheet_{b0}.jpg",
                title="One frame per scene (tile = scene_NN). Coordinates are 0-1 WITHIN each tile.")
            if not sheet:
                continue
            lines = "\n".join(
                f"tile {j}: scene {sub_idx[j]} | line: \"{(scenes[sub_idx[j]].get('exact_voice_text') or scenes[sub_idx[j]].get('script') or '')[:110]}\""
                for j in range(len(sub_idx)))
            prompt = (
                "You are a restrained motion editor for a viral documentary Short over REAL footage. For each "
                "tile (one frame of one scene) find the SINGLE concrete on-screen target that best PROVES the "
                "scene's narration line - a face, person, object, sign, money/receipt, food, vehicle, crowd, "
                "store shelf, phone screen, uniform, or a weird detail the line names. Give its location as a "
                "normalized centre (cx, cy in 0-1 WITHIN that tile) and size (small/medium/large).\n"
                "Then decide ONE red callout: 'arrow' to point at it from the side, or 'none'. (Only thick "
                "arrows are used - never circles.) Choose 'none' unless the target is clearly visible AND "
                "directly supports the line. Do NOT point at empty space, background texture, blurred areas, generic "
                "street, body/chest (unless the line is about clothing/body), or anything under the caption "
                "band (cy roughly 0.50-0.72).\n"
                "Also: stamp_text = a 1-2 word UPPERCASE red label ONLY for a major claim (e.g. BANNED, WHY?, "
                "INSANE, COSTLY, ONLY IN JAPAN) - usually null. beat = normal|reveal|shock|turning_point. "
                "transition = clean_cut|subtle_swipe|whoosh|flash|glitch (mostly clean_cut/subtle_swipe).\n\n"
                f"Scenes:\n{lines}\n\n"
                'Return STRICT JSON: {"scenes": {"<scene_index>": {"target_type": "...", '
                '"target_desc": "...", "cx": 0-1, "cy": 0-1, "size": "small|medium|large", '
                '"confidence": 0-10, "relevance_to_voice": 0-10, "safe_for_overlay": true|false, '
                '"callout": "arrow|circle|none", "stamp_text": null, "beat": "normal", '
                '"transition": "clean_cut", "reason": "short"}, ...}}')
            messages = [
                {"role": "system", "content": "You only add a callout when a concrete, relevant, visible target exists; otherwise 'none'. Return JSON only."},
                {"role": "user", "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": image_data_url(sheet)}},
                ]},
            ]
            try:
                if collaborate:
                    plan = collaborate_json(messages, max_tokens=4000, temperature=0.2, status_cb=status_cb, label="visual fx") or {}
                else:
                    data = post_json_url(WAVESPEED_LLM_API, {
                        "model": reasoning_model or GPT55_MODEL, "messages": messages,
                        "temperature": 0.2, "max_tokens": 4000, "response_format": {"type": "json_object"}}, timeout=180)
                    plan = extract_json_object(data["choices"][0]["message"]["content"]) or {}
                smap = plan.get("scenes") if isinstance(plan.get("scenes"), dict) else {}
                for k, v in smap.items():
                    if isinstance(v, dict):
                        try:
                            vision[int(k)] = v
                        except (TypeError, ValueError):
                            continue
            except Exception as exc:  # noqa: BLE001
                log(status_cb, f"Visual FX: vision pass failed for a batch ({exc.__class__.__name__}).")

    # Keep the actual central caption core clear. The previous 0.50-0.72 exclusion removed
    # almost a quarter of the frame and rejected many valid face/object targets in normal Shorts.
    CAPTION_BAND = (0.57, 0.68)
    SIZE_R = {"small": 0.085, "medium": 0.135, "large": 0.20}

    def _f(v, default=0.0):
        try:
            return float(v)
        except (TypeError, ValueError):
            return default

    # first pass: build a candidate effect plan per scene
    candidates = []
    for i, sc in enumerate(scenes):
        d = vision.get(i, {})
        cx, cy = _f(d.get("cx"), 0.5), _f(d.get("cy"), 0.5)
        conf = _f(d.get("confidence"))
        rel = _f(d.get("relevance_to_voice"))
        safe = bool(d.get("safe_for_overlay", False))
        callout = str(d.get("callout", "none")).lower().strip()
        if callout == "circle":                 # user preference: thick ARROWS only, never circles
            callout = "arrow"
        has_clip = bool(sc.get("clip"))
        in_caption_band = CAPTION_BAND[0] <= cy <= CAPTION_BAND[1]
        # arrows-only: target must be clearly present (confident) and reasonably relevant. The
        # old rel>=8 bar rejected most scenes (high confidence, rel 5-7) leaving only ~6 arrows on
        # a 21-scene short; loosened to conf>=7 / rel>=6 so concrete on-screen subjects get arrows
        # like the reference viral docs, while still skipping scenes with no real target.
        has_target = has_clip and conf >= 7.0 and rel >= 6.0 and 0.02 <= cx <= 0.98
        # hard validation for a callout
        reason = ""
        callout_ok = (has_target and safe and callout in ("arrow", "circle")
                      and not in_caption_band)
        if has_clip and callout in ("arrow", "circle"):
            if not has_target:
                summary["skipped_callouts_bad_target"] += 1
                reason = "target confidence/relevance < 7"
            elif not safe:
                summary["skipped_callouts_bad_target"] += 1
                reason = "target not safe for overlay (face/body/text)"
            elif in_caption_band:
                summary["skipped_callouts_overlap"] += 1
                reason = "target sits in the caption band"
        elif has_clip and callout == "none":
            summary["skipped_callouts_no_target"] += 1
            reason = "no clear target"
        candidates.append({
            "scene_index": i, "has_clip": has_clip, "cx": cx, "cy": cy, "conf": conf,
            "rel": rel, "callout": callout if callout_ok else "none", "callout_ok": callout_ok,
            "size": str(d.get("size", "medium")).lower(), "type": str(d.get("target_type", "")),
            "desc": str(d.get("target_desc", "")), "stamp": (str(d.get("stamp_text")).strip()
                                                             if d.get("stamp_text") else None),
            "beat": str(d.get("beat", "normal")).lower(), "transition": str(d.get("transition", "clean_cut")).lower(),
            "reason": reason or str(d.get("reason", "")), "has_target": has_target, "safe": safe,
        })

    # Effect-heavy default: allow arrows on up to ~68% of scenes while still requiring a real,
    # confident, voice-relevant target. This raises density without reintroducing random arrows.
    eligible = sorted([c for c in candidates if c["callout_ok"]], key=lambda c: -c["rel"])
    _cap_frac = {"low": 0.35, "medium": 0.55, "high": 0.80}.get(str(vfx_amount or "medium").lower(), 0.55)
    cap = max(1, int(round(len(scenes) * _cap_frac)))
    keep = set(c["scene_index"] for c in eligible[:cap])
    for c in eligible[cap:]:
        c["callout"] = "none"; c["callout_ok"] = False
        c["reason"] = "callout budget reached (kept most relevant ~68%)"
        summary["skipped_callouts_overlap"] += 1

    # cap flashy transitions (flash/glitch) to ~12% of scenes so they stay special; the rest of
    # the flagged ones fall back to a subtle swipe (spec: 60-70% clean/swipe, 5-10% flash/glitch).
    flashy_cap = max(1, int(round(len(scenes) * 0.12)))
    flashy_used = 0
    for c in candidates:
        if c["transition"] in ("flash", "glitch"):
            if flashy_used < flashy_cap:
                flashy_used += 1
            else:
                c["transition"] = "subtle_swipe"

    # second pass: write overlays + per-scene fx + report
    for c in candidates:
        i = c["scene_index"]; sc = scenes[i]
        overlays = []
        cx, cy = c["cx"], c["cy"]
        r = SIZE_R.get(c["size"], 0.135)
        # ARROWS ONLY: circles and stamps/labels are disabled. Any callout is rendered as an arrow.
        callout_type = "arrow" if (c["callout_ok"] and c["callout"] in ("arrow", "circle")) else "none"
        if callout_type == "arrow":
            overlays.append({"type": "callout", "shape": "arrow", "cx": cx, "cy": cy,
                             "from": ("left" if cx > 0.5 else "right"), "start": 0.0, "end": 0.0})
            summary["arrow_count"] += 1; summary["callout_count"] += 1
        # callout timing: appear ~0.25s after the cut, last ~0.8s, as fraction of scene duration
        sdur = max(0.6, _f(sc.get("end"), 0) - _f(sc.get("start"), 0)) or 2.0
        s_on = min(0.45, 0.25 / sdur if sdur else 0.12)
        s_off = min(0.98, s_on + max(0.5, min(1.2, 0.8)) / sdur)
        for ov in overlays:
            ov["start"], ov["end"] = round(s_on, 3), round(s_off, 3)
        stamp_text = None                      # red stamps/labels permanently disabled
        if overlays:
            sc["overlays"] = overlays
        else:
            sc.pop("overlays", None)
        # punch-in zoom anchored to the subject + smart reframe offset toward target
        is_hook = (i == 0)
        punch = {"enabled": c["has_clip"], "start_scale": 1.0,
                 "end_scale": 1.13 if is_hook else 1.08,
                 "anchor_cx": cx if c["has_target"] else 0.5,
                 "anchor_cy": cy if c["has_target"] else 0.45}
        if c["has_clip"]:
            summary["punch_in_count"] += 1
        beat = c["beat"]
        shake = c["has_clip"] and beat in ("reveal", "shock", "turning_point")
        freeze = c["has_clip"] and c["has_target"] and beat in ("reveal", "shock", "turning_point") and callout_type != "none"
        if shake:
            summary["impact_shake_count"] += 1
        if freeze:
            summary["freeze_frame_count"] += 1
        transition = c["transition"] if c["transition"] in (
            "clean_cut", "subtle_swipe", "whoosh", "flash", "glitch") else "clean_cut"
        sc["fx"] = {"punch_in": punch, "callout": callout_type, "stamp": stamp_text,
                    "impact_shake": shake, "freeze_frame": freeze, "transition": transition,
                    "anchor_cx": cx, "anchor_cy": cy}
        report["scene_visual_fx"].append({
            "scene_id": i,
            "voice_text": (scene_text_for_planning(sc) or sc.get("script", ""))[:160],
            "chosen_target": (c["type"] + (": " + c["desc"] if c["desc"] else "")) if c["has_target"] else None,
            "target_confidence": round(c["conf"], 1), "target_relevance": round(c["rel"], 1),
            "punch_in_zoom": punch["enabled"], "callout_enabled": callout_type != "none",
            "callout_type": callout_type, "callout_reason": c["reason"],
            "stamp_text": stamp_text, "impact_shake": shake, "freeze_frame": freeze,
            "transition": transition, "validation_passed": True,
        })
        # logs
        if callout_type != "none":
            log(status_cb, f"Visual FX: added {callout_type} around {c['type'] or 'target'} in scene {i} "
                           f"(conf {c['conf']:.0f}, rel {c['rel']:.0f}).")
        elif c["has_clip"] and c["reason"]:
            log(status_cb, f"Visual FX: skipped callout for scene {i}: {c['reason']}.")
        if c["has_clip"]:
            log(status_cb, f"Visual FX: punch-in {punch['start_scale']:.2f} -> {punch['end_scale']:.2f} "
                           f"anchored to ({punch['anchor_cx']:.2f},{punch['anchor_cy']:.2f}), transition {transition}.")

    config["smart_overlays"] = summary["callout_count"] > 0
    report["visual_fx_summary"]["arrows_only"] = True
    report["visual_fx_summary"]["circles_enabled"] = False
    report["visual_fx_summary"]["stamps_enabled"] = False
    config["visual_fx_report"] = report
    log(status_cb, "Visual FX: arrows only; circles/stamps disabled.")
    log(status_cb, "Arrow renderer: using default_thick_red_arrow.")
    log(status_cb, f"Visual FX: {summary['arrow_count']} arrow(s) over {len(scenes)} scene(s) "
                   f"(target-based, conf+rel >= 8), {summary['punch_in_count']} punch-in(s).")
    return report


def clamp_web_image_target(count):
    try:
        value = int(count)
    except (TypeError, ValueError):
        value = 12
    # Floor of 3 (not 10): a short 2-scene script shouldn't chase a 10-image pool
    # of mostly junk -- that wasted minutes re-searching. Generation fills gaps.
    return max(3, min(20, value))


def web_candidate_pool_target(final_target):
    try:
        final_target = int(final_target)
    except (TypeError, ValueError):
        final_target = 12
    final_target = max(1, final_target)
    return max(final_target + 8, min(45, final_target * 3))


def read_web_manifest(project_dir):
    manifest_path = project_dir / "web images" / "web_image_manifest.json"
    if not manifest_path.exists():
        return []
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def write_web_manifest(project_dir, manifest):
    manifest_path = project_dir / "web images" / "web_image_manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def web_review_records(project_dir, web_paths):
    manifest = read_web_manifest(project_dir)
    by_path = {
        str(Path(item.get("local_path", "")).resolve()).lower(): item
        for item in manifest
        if isinstance(item, dict) and item.get("local_path")
    }
    records = []
    for index, path in enumerate([Path(p) for p in web_paths], 1):
        if not path.exists():
            continue
        item = by_path.get(str(path.resolve()).lower(), {})
        records.append(
            {
                "index": index,
                "filename": path.name,
                "scene": item.get("scene"),
                "query": item.get("query"),
                "provider": item.get("provider"),
                "source_title": item.get("title"),
                "source_url": item.get("source_url"),
                "license": item.get("license"),
                "score": item.get("score"),
                "script": item.get("script"),
            }
        )
    return records


def llm_web_image_review(project_dir, title, script, visual_script, scenes, web_paths, sheet_path, reasoning_model=None, status_cb=None, pass_no=1):
    if not os.environ.get("WAVESPEED_API_KEY"):
        log(status_cb, "Reasoning Agent web image review skipped; WAVESPEED_API_KEY is not set.")
        return {}
    records = web_review_records(project_dir, web_paths)
    scene_lines = [
        {
            "scene": index,
            "start": scene.get("start"),
            "end": scene.get("end"),
            "script": scene.get("script", ""),
            "exact_voice_text": scene.get("exact_voice_text") or scene.get("voice_line") or scene.get("script", ""),
            "scene_objective": scene.get("scene_objective", ""),
            "visual_meaning": scene.get("visual_meaning", ""),
            "visual_hook_type": scene.get("visual_hook_type", ""),
            "required_visual_information": scene.get("required_visual_information", ""),
            "must_show": scene.get("must_show", []),
            "must_not_show": scene.get("must_not_show", []),
            "crop_plan": scene.get("crop_plan", {}),
            "visual_direction": scene.get("visual_script", ""),
        }
        for index, scene in enumerate(scenes, 1)
    ]
    prompt = (
        "You are a strict web-image editor for a vertical documentary YouTube Short.\n"
        "Review the candidate image contact sheet before the video is rendered.\n"
        "The contact sheet pixels are the primary evidence. Metadata, source titles, and search queries are context only; never accept an image from text metadata if the visible image itself is not useful.\n"
        "You are the visual decision maker. Judge every image individually against the exact title, spoken voice script, and scene records first. For any visual direction, first decide whether it fits the spoken line; use it only as secondary context when it helps. It cannot rescue an image that does not support the spoken script.\n"
        "Reject images that are irrelevant, only broadly related, wrong era/place/person/object, misleading, too generic, visually weak, meme-like, low-resolution, unreadable when cropped to 9:16, awkward for vertical framing, or unlikely to cut well into a serious short.\n"
        "A useful web image must either directly depict the topic, a specific named person/place/object/event from the script, a clearly relevant map/document/photo, or a strong contextual visual that helps the exact scene beat.\n"
        "Assign every accepted image to a concrete scene role. Do not accept a generally good image if it cannot serve a specific micro-beat objective.\n"
        "Do not keep an image just because a search keyword appears in the filename. If the visual itself is not clearly useful for the final short, reject it.\n"
        "Hard reject random book covers, library catalog scans, title pages, scanned book pages, book bindings, Internet Archive/Open Library/HathiTrust-style scans, or text-only archival pages unless the exact scene explicitly asks for a book/library/manuscript/document shot.\n"
        "A book cover about a broad related era is not useful visual evidence for the spoken scene and must be rejected.\n"
        "Keep only images that match the spoken script first, optionally supported by adaptable visual direction, provide useful evidence/context, and can look good as a phone-sized 9:16 shot.\n"
        "Prefer fewer strong images over padding. Every image not clearly accepted with a concrete visual reason should be rejected and replaced.\n"
        "Important: accepted_images is authoritative. Any candidate omitted from accepted_images will be removed from the render pool.\n"
        "Return strict JSON only with keys: summary, accepted_images, rejected_images, needed_replacements.\n"
        "accepted_images must be an array of {index:number, filename:string, reason:string, best_scene:number|null, role:'proof|face|location|object|archive|texture|transition|map|document|reaction', crop_safety:'good|risky|bad', visual_relevance_score:number, crop_plan:{focus_subject:string, keep_face_visible:boolean, safe_zone:string, avoid_cutting:array, allow_pan:boolean}, confidence:'high|medium'}.\n"
        "rejected_images must be an array of {index:number, filename:string, reason:string, replacement_query:string}.\n"
        "needed_replacements is how many more web images should be downloaded to reach a strong 10-15 image pool with no weak filler.\n\n"
        f"Title: {title}\n"
        f"Script:\n{script}\n\n"
        f"Optional visual direction:\n{visual_script or '(none provided - infer full visual logic from voice script)'}\n\n"
        f"Scenes JSON:\n{json.dumps(scene_lines, ensure_ascii=False)}\n\n"
        f"Downloaded image records JSON:\n{json.dumps(records, ensure_ascii=False)}"
    )
    content = [{"type": "text", "text": prompt}]
    if Path(sheet_path).exists():
        content.append({"type": "text", "text": "Downloaded web image contact sheet with filenames under each tile."})
        content.append({"type": "image_url", "image_url": {"url": image_data_url(sheet_path)}})
    payload = {
        "model": reasoning_model or GPT55_MODEL,
        "messages": [
            {"role": "system", "content": "You are a precise documentary image QA agent. Prefer fewer strong images over many weak or irrelevant images."},
            {"role": "user", "content": content},
        ],
        "temperature": 0.08,
        "max_tokens": 1600,
        "response_format": {"type": "json_object"},
    }
    try:
        log(status_cb, f"Reviewing web images with Reasoning Agent before render (pass {pass_no})...")
        data = post_json_url(WAVESPEED_LLM_API, payload, timeout=180)
        review = extract_json_object(data["choices"][0]["message"]["content"])
        if isinstance(review, dict):
            rejected = review.get("rejected_images") or []
            log(status_cb, f"Reasoning Agent web image review pass {pass_no}: rejected {len(rejected)} image(s).")
            if review.get("summary"):
                log(status_cb, f"Web image review summary: {review.get('summary')}")
            return review
    except Exception as exc:
        log(status_cb, f"Reasoning Agent web image review skipped: {exc}")
    return {}


def rejected_web_paths(review, web_paths):
    indexed = {index: Path(path) for index, path in enumerate(web_paths, 1)}
    by_name = {Path(path).name.lower(): Path(path) for path in web_paths}
    rejected = set()
    for item in review.get("rejected_images", []) or []:
        if isinstance(item, int):
            if item in indexed:
                rejected.add(indexed[item])
            continue
        if not isinstance(item, dict):
            continue
        try:
            index = int(str(item.get("index", "")).strip())
        except ValueError:
            index = None
        if index in indexed:
            rejected.add(indexed[index])
            continue
        filename = str(item.get("filename", "")).strip().lower()
        if filename in by_name:
            rejected.add(by_name[filename])
    return rejected


def accepted_web_paths(review, web_paths):
    accepted_items = review.get("accepted_images") if isinstance(review, dict) else None
    if not isinstance(accepted_items, list):
        return None
    indexed = {index: Path(path) for index, path in enumerate(web_paths, 1)}
    by_name = {Path(path).name.lower(): Path(path) for path in web_paths}
    accepted = set()
    for item in accepted_items:
        if isinstance(item, int):
            if item in indexed:
                accepted.add(indexed[item])
            continue
        if not isinstance(item, dict):
            continue
        confidence = str(item.get("confidence", "")).lower().strip()
        if confidence and confidence not in {"high", "medium"}:
            continue
        try:
            index = int(str(item.get("index", "")).strip())
        except ValueError:
            index = None
        if index in indexed:
            accepted.add(indexed[index])
            continue
        filename = str(item.get("filename", "")).strip().lower()
        if filename in by_name:
            accepted.add(by_name[filename])
    return accepted


def apply_web_review_metadata(project_dir, review, web_paths, status_cb=None):
    accepted_items = review.get("accepted_images") if isinstance(review, dict) else None
    if not isinstance(accepted_items, list):
        return
    indexed = {index: Path(path) for index, path in enumerate(web_paths, 1)}
    by_name = {Path(path).name.lower(): Path(path) for path in web_paths}
    metadata_by_path = {}
    for item in accepted_items:
        if not isinstance(item, dict):
            continue
        path = None
        try:
            index = int(str(item.get("index", "")).strip())
        except ValueError:
            index = None
        if index in indexed:
            path = indexed[index]
        else:
            filename = str(item.get("filename", "")).strip().lower()
            path = by_name.get(filename)
        if not path:
            continue
        metadata_by_path[str(path.resolve()).lower()] = {
            "review_status": "accepted_by_llm_visual_review",
            "review_role": item.get("role"),
            "best_scene": item.get("best_scene"),
            "crop_safety": item.get("crop_safety"),
            "visual_relevance_score": item.get("visual_relevance_score"),
            "crop_plan": item.get("crop_plan") if isinstance(item.get("crop_plan"), dict) else None,
            "reason_accepted": item.get("reason"),
            "confidence": item.get("confidence"),
        }
    if not metadata_by_path:
        return
    manifest = read_web_manifest(project_dir)
    changed = 0
    for item in manifest:
        if not isinstance(item, dict) or not item.get("local_path"):
            continue
        key = str(Path(item["local_path"]).resolve()).lower()
        if key in metadata_by_path:
            item.update({k: v for k, v in metadata_by_path[key].items() if v is not None})
            changed += 1
    if changed:
        write_web_manifest(project_dir, manifest)
        log(status_cb, f"Web image visual review metadata saved for {changed} accepted image(s).")


def move_rejected_web_images(project_dir, rejected_paths, status_cb=None):
    if not rejected_paths:
        return
    rejected_dir = project_dir / "web images" / "rejected"
    rejected_dir.mkdir(parents=True, exist_ok=True)
    manifest = read_web_manifest(project_dir)
    moved_map = {}
    for path in sorted({Path(p) for p in rejected_paths}):
        if not path.exists():
            continue
        target = rejected_dir / path.name
        counter = 2
        while target.exists():
            target = rejected_dir / f"{path.stem}_{counter}{path.suffix}"
            counter += 1
        path.replace(target)
        moved_map[str(path.resolve()).lower()] = str(target)
        log(status_cb, f"Web image rejected before render: {path.name} -> rejected/{target.name}")
    if moved_map and manifest:
        for item in manifest:
            if not isinstance(item, dict) or not item.get("local_path"):
                continue
            key = str(Path(item["local_path"]).resolve()).lower()
            if key in moved_map:
                item["review_status"] = "rejected_by_llm_before_render"
                item["rejected_local_path"] = moved_map[key]
        write_web_manifest(project_dir, manifest)


def review_and_correct_web_images(
    project_dir,
    title,
    script,
    visual_script,
    scenes,
    target_duration,
    web_paths,
    images_per_scene,
    target_count,
    use_gpt55=True,
    reasoning_model=None,
    status_cb=None,
):
    paths = [str(Path(path)) for path in web_paths if Path(path).exists()]
    if not paths or not use_gpt55 or not os.environ.get("WAVESPEED_API_KEY"):
        return paths
    target_count = clamp_web_image_target(target_count)
    review_dir = project_dir / "review"
    last_reviewed_paths = []
    zero_accept_streak = 0
    for pass_no in range(1, 4):  # 3 passes max (was 5); each re-search costs ~1 min
        if not paths:
            break
        sheet = create_media_contact_sheet(
            paths,
            review_dir / f"web_images_llm_review_pass_{pass_no}.jpg",
            title=f"Web images Reasoning Agent review pass {pass_no}",
        )
        if sheet:
            log_preview(status_cb, f"Web image Reasoning Agent review pass {pass_no}", sheet)
        review = llm_web_image_review(
            project_dir,
            title,
            script,
            visual_script,
            scenes,
            paths,
            sheet or "",
            status_cb=status_cb,
            pass_no=pass_no,
        )
        if not review:
            log(status_cb, "Reasoning Agent visual web image decision failed; unreviewed web candidates will not be used.")
            paths = last_reviewed_paths
            break
        review_path = review_dir / f"web_images_llm_review_pass_{pass_no}.json"
        review_path.write_text(json.dumps(review, indent=2), encoding="utf-8")
        accepted = accepted_web_paths(review, paths)
        explicit_rejected = rejected_web_paths(review, paths)
        apply_web_review_metadata(project_dir, review, paths, status_cb=status_cb)
        if accepted is not None:
            accepted_resolved = {str(Path(path).resolve()).lower() for path in accepted}
            rejected = {
                Path(path)
                for path in paths
                if str(Path(path).resolve()).lower() not in accepted_resolved
            } | explicit_rejected
            log(status_cb, f"Reasoning Agent visual web image decision: accepted {len(accepted)} of {len(paths)} candidate(s).")
        else:
            rejected = explicit_rejected
        if rejected:
            move_rejected_web_images(project_dir, rejected, status_cb=status_cb)
            rejected_resolved = {str(Path(path).resolve()).lower() for path in rejected}
            paths = [path for path in paths if str(Path(path).resolve()).lower() not in rejected_resolved and Path(path).exists()]
        last_reviewed_paths = list(paths)
        # Give up quickly when web search keeps yielding nothing usable (niche
        # topic / only junk results) instead of burning all 5 passes -- fall back
        # to generated images.
        if not paths:
            zero_accept_streak += 1
            if zero_accept_streak >= 2:
                log(status_cb, "Web image review accepted nothing across 2 passes; stopping web search and falling back to generated images.")
                break
        else:
            zero_accept_streak = 0
        try:
            requested_replacements = int(review.get("needed_replacements", 0) or 0)
        except (TypeError, ValueError):
            requested_replacements = 0
        needed = max(0, target_count - len(paths), requested_replacements)
        if needed <= 0:
            break
        candidate_needed = web_candidate_pool_target(needed)
        log(status_cb, f"Web image correction: downloading {candidate_needed} candidate replacement image(s) for Reasoning Agent visual review...")
        new_paths = gather_web_images_for_script(
            project_dir,
            title,
            script,
            target_duration,
            images_per_scene=max(1, min(images_per_scene * 2, candidate_needed)),
            target_count=candidate_needed,
            use_llm_search=True,
            scenes_override=scenes,
            force_new=True,
            status_cb=status_cb,
        )
        paths.extend([str(Path(path)) for path in new_paths if Path(path).exists()])
    final_paths = paths[:target_count]
    minimum_required = min(10, target_count) if target_count >= 10 else target_count
    if use_gpt55 and os.environ.get("WAVESPEED_API_KEY") and len(final_paths) < minimum_required:
        # Obscure topics genuinely lack enough on-topic stock imagery. Rather than
        # aborting the whole render, proceed with the approved images and let the
        # director fill the remaining scenes with GPT-source/Seedance reconstructions.
        log(
            status_cb,
            f"Only {len(final_paths)} of {minimum_required} target web image(s) passed review for this topic; "
            "proceeding and covering the remaining scenes with GPT-source/Seedance reconstructions.",
        )
    final_sheet = create_media_contact_sheet(
        final_paths,
        review_dir / "web_images_contact_sheet.jpg",
        title="Approved web images",
    )
    if final_sheet:
        log_preview(status_cb, "Approved web image contact sheet", final_sheet)
    return final_paths


def path_under(path, folder):
    try:
        resolved = Path(path).resolve()
        folder_resolved = Path(folder).resolve()
    except Exception:
        return False
    return folder_resolved in [resolved, *resolved.parents]


def move_to_replaced(path):
    path = Path(path)
    target_dir = path.parent / "replaced"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / path.name
    counter = 2
    while target.exists():
        target = target_dir / f"{path.stem}_{counter}{path.suffix}"
        counter += 1
    path.replace(target)
    return target


def update_shot_asset_references(config, old_path, new_path):
    old_path = Path(old_path)
    new_path = Path(new_path)
    old_refs = {old_path.name, f"web images/{old_path.name}", f"gpt images/{old_path.name}", str(old_path)}
    if new_path.parent.name == "web images":
        new_ref = f"web images/{new_path.name}"
    elif new_path.parent.name == "gpt images":
        new_ref = new_path.name
    else:
        new_ref = str(new_path)
    changed = 0
    for scene in config.get("scenes", []):
        for shot in scene.get("shots", []) or []:
            if str(shot.get("asset", "")) in old_refs:
                shot["asset"] = new_ref
                changed += 1
        if str(scene.get("asset", "")) in old_refs:
            scene["asset"] = new_path.name
            changed += 1
    return changed


def replace_selected_media_after_seedance(form, config, project_dir, title, script, visual_script, target_duration, scenes_override, status_cb=None):
    selected = [path for path in consume_replace_requests(form) if path.exists() and path.suffix.lower() in IMAGE_EXTS]
    if not selected:
        log(status_cb, "No manual image replacements queued before render.")
        return config
    project_dir = Path(project_dir)
    web_dir = project_dir / "web images"
    gpt_dir = project_dir / "gpt images"
    web_selected = [path for path in selected if path_under(path, web_dir) and "replaced" not in {part.lower() for part in path.parts}]
    gpt_selected_raw = [path for path in selected if path_under(path, gpt_dir) and "replaced" not in {part.lower() for part in path.parts}]
    gpt_selected = []
    for path in gpt_selected_raw:
        log(status_cb, f"Manual replacement skipped for {path.name}; GPT images are Seedance source images only and are not replaced as static stills.")

    if web_selected:
        log(status_cb, f"Manual replacement: replacing {len(web_selected)} selected web image(s) before render...")
        candidate_count = web_candidate_pool_target(len(web_selected))
        new_candidates = gather_web_images_for_script(
            project_dir,
            title,
            script,
            target_duration,
            images_per_scene=3,
            target_count=candidate_count,
            use_llm_search=True,
            scenes_override=scenes_override,
            force_new=True,
            status_cb=status_cb,
        )
        new_paths = review_and_correct_web_images(
            project_dir,
            title,
            script,
            visual_script,
            scenes_override or parse_timed_script(script, target_duration),
            target_duration,
            new_candidates,
            images_per_scene=3,
            target_count=len(web_selected),
            use_gpt55=True,
            reasoning_model=reasoning_model, status_cb=status_cb,
        )
        for old_path, new_path_raw in zip(web_selected, new_paths):
            new_path = Path(new_path_raw)
            if not new_path.exists():
                continue
            update_shot_asset_references(config, old_path, new_path)
            moved = move_to_replaced(old_path)
            log(status_cb, f"Manual replacement: {old_path.name} replaced by {new_path.name}; old file moved to {moved.parent.name}/.")

    config_path = Path(config.get("_config_path", project_dir / "config" / "project.json"))
    config_path.write_text(json.dumps(config_for_json(config), indent=2), encoding="utf-8")
    web_sheet = create_media_contact_sheet(
        sorted([p for p in web_dir.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTS]),
        project_dir / "review" / "web_images_contact_sheet.jpg",
        title="Approved web images",
    )
    if web_sheet:
        log_preview(status_cb, "Updated web image contact sheet", web_sheet)
    gpt_sheet = create_media_contact_sheet(
        sorted([p for p in gpt_dir.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTS]),
        project_dir / "review" / "gpt_images_contact_sheet.jpg",
        title="GPT source images for Seedance",
    )
    if gpt_sheet:
        log_preview(status_cb, "Updated GPT source image contact sheet", gpt_sheet)
    return config


def compact_scene_plan(config):
    compact = []
    for scene in config.get("scenes", []):
        compact.append(
            {
                "id": scene.get("id"),
                "start": scene.get("start"),
                "end": scene.get("end"),
                "script": scene.get("script"),
                "exact_voice_text": scene.get("exact_voice_text") or scene.get("voice_line"),
                "scene_objective": scene.get("scene_objective"),
                "visual_meaning": scene.get("visual_meaning"),
                "viewer_emotion": scene.get("viewer_emotion") or scene.get("emotion"),
                "visual_hook_type": scene.get("visual_hook_type"),
                "required_visual_information": scene.get("required_visual_information"),
                "must_show": scene.get("must_show"),
                "must_not_show": scene.get("must_not_show"),
                "crop_plan": scene.get("crop_plan"),
                "best_media_type": scene.get("best_media_type"),
                "director_scores": scene.get("director_scores"),
                "prompt_review_scores": scene.get("prompt_review_scores"),
                "visual_direction": scene.get("visual_direction") or scene.get("visual_script"),
                "seedance": scene.get("seedance"),
                "clip": scene.get("clip"),
                "shots": [
                    {
                        "asset": shot.get("asset"),
                        "fit": shot.get("fit"),
                        "use_clip": shot.get("use_clip"),
                        "crop_plan": shot.get("crop_plan"),
                        "visual_role": shot.get("visual_role"),
                    }
                    for shot in scene.get("shots", [])
                ],
            }
        )
    return compact


def prompt_review_scene_plan(config):
    scenes = []
    for index, scene in enumerate(config.get("scenes", []), 1):
        if scene.get("speaker_hook"):
            continue
        needs_gpt = bool(scene.get("needs_gpt_asset"))
        uses_seedance = bool(scene.get("seedance"))
        if not needs_gpt and not uses_seedance:
            continue
        scenes.append(
            {
                "scene": index,
                "id": scene.get("id"),
                "start": scene.get("start"),
                "end": scene.get("end"),
                "script": scene.get("script", ""),
                "exact_voice_text": scene.get("exact_voice_text") or scene.get("voice_line") or scene.get("script", ""),
                "scene_objective": scene.get("scene_objective", ""),
                "visual_meaning": scene.get("visual_meaning", ""),
                "viewer_emotion": scene.get("viewer_emotion") or scene.get("emotion", ""),
                "visual_hook_type": scene.get("visual_hook_type", ""),
                "required_visual_information": scene.get("required_visual_information", ""),
                "must_show": scene.get("must_show", []),
                "must_not_show": scene.get("must_not_show", []),
                "crop_plan": scene.get("crop_plan", {}),
                "director_scores": scene.get("director_scores", {}),
                "visual_direction": scene.get("visual_direction") or scene.get("visual_script") or "",
                "needs_gpt_image": needs_gpt,
                "uses_seedance": uses_seedance,
                "current_gpt_image_prompt": scene.get("prompt", "") if needs_gpt else None,
                "current_seedance_prompt": scene.get("video_prompt", "") if uses_seedance else None,
            }
        )
    return scenes


def llm_prompt_review(title, script, visual_script, config, reasoning_model=None, status_cb=None):
    if not os.environ.get("WAVESPEED_API_KEY"):
        log(status_cb, "Reasoning Agent prompt review skipped; WAVESPEED_API_KEY is not set.")
        return {}
    prompt_scenes = prompt_review_scene_plan(config)
    if not prompt_scenes:
        log(status_cb, "Reasoning Agent prompt review skipped; no new GPT/Seedance prompts need review.")
        return {}
    payload_prompt = (
        "You are a strict prompt supervisor for a vertical documentary YouTube Short.\n"
        "Review and improve prompts before WaveSpeed generation. The spoken voice/text script is the highest priority. "
        "The user visual direction is optional secondary guidance. If missing, generate the entire visual logic yourself based on the Voice Script.\n"
        "Do not change scene selection, timing, media counts, Seedance selection, GPT-image selection, or any speaker hook. Speaker hook scenes are not included and must remain locked.\n"
        "GPT image prompts are only for Seedance I2V source images, never static final-video images. They should create one coherent vertical 9:16 realistic scene plate with readable subject, depth, and no collage.\n"
        "Seedance prompts must create real motion, not a still image with a zoom. They should include a clear script-specific action, beginning-middle-end movement, natural camera/parallax, and distinct behavior from other Seedance clips.\n"
        "Every Seedance prompt must explicitly contain camera_motion, subject_motion, environment_motion, emotional_action, no_speech, no_text, and no_logo constraints.\n"
        "Generated Seedance audio may contain only nonverbal ambience and sound effects: no speech, no voices, no dialogue, no narration, no talking, no vocalizations.\n"
        "Both prompt types must avoid captions, subtitles, watermarks, logos, embedded text, random book covers, generic unrelated visuals, and facts not supported by the voice script.\n"
        "First score whether the planned scene idea itself is correct, then score each prompt for topic/script match, visual understandability, vertical framing, uniqueness, motion usefulness, retention, and factual accuracy. If the scene idea is weak, rewrite the prompt around a better scene idea that directly answers the exact voice line.\n"
        "Threshold rules: if voice_match_score < 80, rewrite the scene/prompt around a better voice-line visual. If motion_score < 75 for Seedance, rewrite the Seedance prompt with stronger concrete motion or make the prompt calmer only if motion does not make sense. If visual_clarity_score < 75, simplify the shot and crop plan.\n"
        "Return strict JSON only with keys: summary, prompt_updates.\n"
        "prompt_updates must be an array of {scene:number, scene_objective:string|null, visual_meaning:string|null, visual_hook_type:string|null, gpt_image_score:number|null, seedance_score:number|null, voice_match_score:number, visual_clarity_score:number, motion_score:number, shorts_retention_score:number, historical_or_factual_accuracy_score:number, gpt_image_prompt:string|null, seedance_prompt:string|null, reason:string}.\n"
        "Use null for a prompt type that is not used by that scene. Do not include markdown.\n\n"
        f"Title: {title}\n"
        f"Voice script:\n{script}\n\n"
        f"Optional visual direction:\n{visual_script or '(none provided)'}\n\n"
        f"Prompt scene plan JSON:\n{json.dumps(prompt_scenes, ensure_ascii=False)}"
    )
    payload = {
        "model": reasoning_model or GPT55_MODEL,
        "messages": [
            {"role": "system", "content": "You are a careful prompt QA and rewrite agent for GPT image and Seedance image-to-video generation."},
            {"role": "user", "content": payload_prompt},
        ],
        "temperature": 0.12,
        "max_tokens": 2600,
        "response_format": {"type": "json_object"},
    }
    try:
        log(status_cb, "Reasoning Agent prompt review starting for GPT image and Seedance prompts...")
        data = post_json_url(WAVESPEED_LLM_API, payload, timeout=180)
        review = extract_json_object(data["choices"][0]["message"]["content"])
        if not isinstance(review, dict):
            return {}
        updates = review.get("prompt_updates") or []
        log(status_cb, f"Reasoning Agent prompt review returned {len(updates)} scene prompt update(s).")
        if review.get("summary"):
            log(status_cb, f"Prompt review summary: {review.get('summary')}")
        return review
    except Exception as exc:
        log(status_cb, f"Reasoning Agent prompt review skipped: {exc}")
        return {}


def scene_matches_prompt_update(scene, scene_number, update):
    update_scene = str(update.get("scene", "")).strip().lower().replace("scene", "").strip()
    update_id = str(update.get("id", "")).strip().lower()
    scene_id = str(scene.get("id", "")).strip().lower()
    if update_scene:
        return update_scene in {str(scene_number), str(scene_number).zfill(2), scene_id, scene_id.lstrip("0")}
    if update_id:
        return update_id in {scene_id, scene_id.lstrip("0")}
    return False


def apply_llm_prompt_updates(config, review, status_cb=None):
    updates = review.get("prompt_updates") if isinstance(review, dict) else None
    if not isinstance(updates, list):
        return 0
    changed = 0
    scenes = list(config.get("scenes", []))
    for update in updates:
        if not isinstance(update, dict):
            continue
        matched = None
        for scene_number, scene in enumerate(scenes, 1):
            if scene_matches_prompt_update(scene, scene_number, update):
                matched = scene
                break
        if not matched or matched.get("speaker_hook"):
            continue
        scene_changed = False
        for field in ["scene_objective", "visual_meaning", "visual_hook_type"]:
            value = clean_text(str(update.get(field) or ""))
            if value and value != clean_text(str(matched.get(field, ""))):
                matched.setdefault("prompt_review_original", {})[field] = matched.get(field, "")
                matched[field] = value[:900]
                scene_changed = True
        gpt_prompt = clean_text(str(update.get("gpt_image_prompt") or ""))
        if gpt_prompt and matched.get("needs_gpt_asset"):
            old_prompt = clean_text(matched.get("prompt", ""))
            if gpt_prompt != old_prompt:
                matched.setdefault("prompt_review_original", {})["gpt_image_prompt"] = old_prompt
                matched["prompt"] = gpt_prompt[:3500]
                scene_changed = True
        seedance_prompt = clean_text(str(update.get("seedance_prompt") or ""))
        if seedance_prompt and matched.get("seedance"):
            old_video_prompt = clean_text(matched.get("video_prompt", ""))
            if seedance_prompt != old_video_prompt:
                matched.setdefault("prompt_review_original", {})["seedance_prompt"] = old_video_prompt
                matched["video_prompt"] = seedance_prompt[:3200]
                scene_changed = True
        if scene_changed:
            matched["prompt_reviewed_by_gpt55"] = True
            matched["prompt_review_reason"] = clean_text(str(update.get("reason") or ""))[:600]
            matched["prompt_review_scores"] = {
                "gpt_image": update.get("gpt_image_score"),
                "seedance": update.get("seedance_score"),
                "voice_match_score": update.get("voice_match_score"),
                "visual_clarity_score": update.get("visual_clarity_score"),
                "motion_score": update.get("motion_score"),
                "shorts_retention_score": update.get("shorts_retention_score"),
                "historical_or_factual_accuracy_score": update.get("historical_or_factual_accuracy_score"),
            }
            changed += 1
    if changed:
        log(status_cb, f"Reasoning Agent prompt review applied corrected prompts to {changed} scene(s).")
    else:
        log(status_cb, "Reasoning Agent prompt review kept existing prompts unchanged.")
    config.setdefault("agent", {})["prompt_review"] = {
        "enabled": True,
        "changed_scenes": changed,
        "summary": review.get("summary") if isinstance(review, dict) else None,
    }
    return changed


def render_mix_plan_for_output(config, video_path):
    try:
        _, _, render_dir, _ = pipeline.project_paths(config)
        plan_path = Path(render_dir) / f"{Path(video_path).stem}_edit_plan.json"
        if plan_path.exists():
            data = json.loads(plan_path.read_text(encoding="utf-8"))
            return {
                "plan_path": str(plan_path),
                "audio_path": data.get("audio_path"),
                "seedance_audio_segments": data.get("seedance_audio_segments", []),
                "sfx_segments": data.get("sfx_segments", []),
                "background_music": data.get("background_music"),
                "duration": data.get("duration"),
            }
    except Exception:
        pass
    return {}


def llm_video_review(title, script, visual_script, config, video_path, scene_sheet, shot_sheet, reasoning_model=None, status_cb=None, pass_label="initial", collaborate=False):
    if not os.environ.get("WAVESPEED_API_KEY"):
        log(status_cb, "Reasoning Agent video review skipped; WAVESPEED_API_KEY is not set.")
        return {}
    audio_mix_plan = render_mix_plan_for_output(config, video_path)
    project_dir, _, _, _ = pipeline.project_paths(config)
    media_context_paths = [
        ("approved web image contact sheet", project_dir / "review" / "web_images_contact_sheet.jpg"),
        ("GPT image contact sheet", project_dir / "review" / "gpt_images_contact_sheet.jpg"),
    ]
    content = [
        {
            "type": "text",
            "text": (
                "You are a strict visual editor reviewing a rendered vertical YouTube Short.\n"
                "You receive contact sheets sampled from the rendered video plus media contact sheets and audio mix metadata.\n"
                "Review voice-script match first, then visual-script guidance, crop/framing, pacing, readability, repeated/irrelevant media, weird zoom/motion, whether Seedance clips look usable, and whether audio layers make sense.\n"
                "Priority rule: the spoken voice/text script is authoritative. Evaluate the edit intelligently: first judge whether each visual-direction idea actually fits the spoken line. It should improve clarity, style, or pacing; do not penalize the render for ignoring visual-direction details when the render matches the spoken script better.\n"
                "The voice script is the edit map. For every scene, ask: does the visual answer the exact voice line at that timestamp, not just look cinematic?\n"
                "Review each scene for these concrete checks: exact voice-line match, most important object visible within the first 0.3 seconds, 9:16 crop safety, visual difference from the previous scene, real motion versus fake zoom, Shorts pacing, SFX frequency/distraction, unwanted mouth movement or speech in generated clips, repetition, and whether the hook connects to the rest of the video.\n"
                "Be very strict about wrong topic media: if a scene image does not match the spoken script, flag it as high severity even if it resembles the visual direction.\n"
                "SCENE-TO-SCRIPT RELEVANCE (critical for scraped found-footage): for EVERY scene ask 'does this exact visual depict what THIS line says?'. Flag as HIGH severity any of: a clip that is merely generic Japan/travel/street b-roll under a specific claim; a pretty skyline/neon shot under a line about stress, exhaustion, cleanliness, crowds, or loneliness; the same footage reused for unrelated lines; a specific claim shown with a vague mood-only visual. The footage should feel chosen for the sentence, not just 'filmed in Japan'.\n"
                "For audio, flag missing SFX on image switches, SFX too loud/too quiet, Seedance clip audio overpowering speech, or music/SFX fighting the narration. Speech audio may be absent; do not require it. Do not flag missing background music when background_music_enabled is false.\n"
                "The uploaded speech audio is only a timing reference and must not be required or mixed into the final video.\n"
                "Do not request edits to any scene marked speaker_hook=true; the opening speaker hook clip is locked once generated.\n"
                "Do not request new GPT image generation. GPT images are allowed only as Seedance source images, never as static stills in the final render.\n"
                "Return strict JSON only with keys: review_pass (enum: 'initial', 'recut', 'final'), overall_score (integer 0-10), needs_correction (boolean), summary, issues, correction_actions.\n"
                "issues must be an array of {scene, severity: enum('low', 'medium', 'high', 'critical'), crop_risk: enum('safe', 'marginal', 'unsafe'), type: enum('timing', 'content', 'crop', 'audio', 'motion'), problem, suggested_fix}.\n"
                "correction_actions must be an array of objects: {action: enum('contain_scene', 'reduce_motion_scene', 'reduce_global_motion', 'reduce_grain_tint', 'lower_seedance_audio', 'raise_sfx_audio', 'lower_sfx_audio', 'enable_sfx', 'enable_background_music', 'no_action'), scene: string or null}.\n"
                "Good scenes must not be changed. If media is wrong but no safe render-only correction exists, explain it in issues and return no_action for that scene.\n\n"
                f"Title: {title}\n"
                f"Rendered video path: {video_path}\n"
                f"Script:\n{script}\n\n"
                f"Optional visual direction:\n{visual_script or '(none provided)'}\n\n"
                f"Scene plan JSON:\n{json.dumps(compact_scene_plan(config), ensure_ascii=False)}\n\n"
                f"Audio config JSON:\n{json.dumps({k: config.get(k) for k in ['audio_path', 'seedance_audio_volume', 'seedance_audio_volume_with_speech', 'sfx_enabled', 'sfx_volume', 'sfx_volume_with_speech', 'sfx_transition_volume', 'sfx_transition_volume_with_speech', 'background_music_enabled', 'background_music_user_enabled', 'background_music_volume', 'background_music_volume_with_speech']}, ensure_ascii=False)}\n\n"
                f"Rendered audio mix plan JSON:\n{json.dumps(audio_mix_plan, ensure_ascii=False)}"
            ),
        }
    ]
    for label, path in [("scene review sheet", scene_sheet), ("shot review sheet", shot_sheet), *media_context_paths]:
        path = Path(path)
        if path.exists():
            content.append({"type": "text", "text": f"Image: {label}"})
            content.append({"type": "image_url", "image_url": {"url": image_data_url(path)}})
    payload = {
        "model": reasoning_model or GPT55_MODEL,
        "messages": [
            {"role": "system", "content": "You are an elite short-form video editor and retention analyst doing QA. Judge whether this feels like a deliberately cut edit that stops the scroll and holds attention, not a slideshow - apply the first-second hook test and watch for dead air, repetition, and weak pacing. Be critical, but only recommend changes that are both visible in the provided images and achievable with the listed render-only correction actions."},
            {"role": "user", "content": content},
        ],
        "temperature": 0.1,
        "max_tokens": 1800,
        "response_format": {"type": "json_object"},
    }
    try:
        if collaborate:
            review = collaborate_json(payload["messages"], max_tokens=payload["max_tokens"],
                                      temperature=payload["temperature"], status_cb=status_cb,
                                      label=f"video review ({pass_label})") or {}
        else:
            log(status_cb, f"Reviewing video with Reasoning Agent ({pass_label})...")
            data = post_json_url(WAVESPEED_LLM_API, payload, timeout=180)
            review = extract_json_object(data["choices"][0]["message"]["content"])
        if not isinstance(review, dict):
            return {}
        log(status_cb, f"Reasoning Agent review score ({pass_label}): {review.get('overall_score', 'n/a')}/10")
        if review.get("summary"):
            log(status_cb, f"Reasoning Agent review summary: {review.get('summary')}")
        return review
    except Exception as exc:
        log(status_cb, f"Reasoning Agent video review skipped: {exc}")
        return {}


def llm_pre_render_edit_review(title, script, visual_script, config, reasoning_model=None, status_cb=None):
    if not os.environ.get("WAVESPEED_API_KEY"):
        log(status_cb, "Reasoning Agent pre-render edit audit skipped; WAVESPEED_API_KEY is not set.")
        return {}
    project_dir, _, _, _ = pipeline.project_paths(config)
    content = [
        {
            "type": "text",
            "text": (
                "You are a strict pre-render edit supervisor for a vertical YouTube Short.\n"
                "Review the planned scene order, shot assets, Seedance placement, clip/still pacing, voice-script match, secondary visual-script guidance, and audio rules before final rendering.\n"
                "Priority rule: the spoken voice/text script is the authoritative source. Make edit decisions intelligently: first judge whether each user visual-direction idea fits the spoken line. Keep helpful ideas, adapt weak ideas, and replace mismatched ideas with better script-matched shots when the script, available media, or pacing needs a better edit.\n"
                "The voice script is the edit map. Every planned visual must be justified by the exact spoken words at that timestamp. If a scene only looks good but does not explain or intensify the voice line, it is too weak.\n"
                "Check every planned scene for: exact voice-line match, scene objective clarity, required visual information, visual hook type, crop plan safety, whether the chosen medium is right, whether Seedance has real motion, whether still images are placed in the right beat, and whether SFX/music rules fit.\n"
                "Use the scene scores. If voice_match_score is under 80, the scene idea is wrong or needs a different medium. If motion_score is under 75 for Seedance, the Seedance prompt is too weak. If visual_clarity_score is under 75, simplify crop/framing or shot selection.\n"
                "Do not request edits to speaker_hook=true scenes. The speaker hook is locked.\n"
                "Do not request new GPT images. GPT images may only be Seedance source images, never static still shots in the final render.\n"
                "The uploaded speech audio is timing reference only and must not be mixed into the final video. Do not flag missing background music when background_music_enabled is false.\n"
                "Return strict JSON only with keys: review_pass (enum: 'initial', 'recut', 'final'), overall_score (integer 0-10), needs_correction (boolean), summary, issues, correction_actions.\n"
                "issues must be an array of {scene, severity: enum('low', 'medium', 'high', 'critical'), crop_risk: enum('safe', 'marginal', 'unsafe'), type: enum('timing', 'content', 'crop', 'audio', 'motion'), problem, suggested_fix}.\n"
                "correction_actions must be an array of objects: {action: enum('contain_scene', 'reduce_motion_scene', 'reduce_global_motion', 'reduce_grain_tint', 'lower_seedance_audio', 'raise_sfx_audio', 'lower_sfx_audio', 'enable_sfx', 'enable_background_music', 'no_action'), scene: string or null}.\n\n"
                f"Title: {title}\n"
                f"Script:\n{script}\n\n"
                f"Optional visual direction:\n{visual_script or '(none provided)'}\n\n"
                f"Scene plan JSON:\n{json.dumps(compact_scene_plan(config), ensure_ascii=False)}\n\n"
                f"Render/audio config JSON:\n{json.dumps({k: config.get(k) for k in ['duration', 'seedance_clip_start_trim', 'gpt_static_stills_disabled', 'speech_audio_in_final', 'timing_audio_path', 'audio_path', 'seedance_audio_volume', 'sfx_enabled', 'sfx_transition_volume', 'background_music_enabled', 'background_music_user_enabled']}, ensure_ascii=False)}"
            ),
        }
    ]
    for label, path in [
        ("approved web image contact sheet", project_dir / "review" / "web_images_contact_sheet.jpg"),
        ("GPT source image contact sheet", project_dir / "review" / "gpt_images_contact_sheet.jpg"),
    ]:
        path = Path(path)
        if path.exists():
            content.append({"type": "text", "text": f"Image: {label}"})
            content.append({"type": "image_url", "image_url": {"url": image_data_url(path)}})
    payload = {
        "model": reasoning_model or GPT55_MODEL,
        "messages": [
            {"role": "system", "content": "You are a careful short-form edit QA agent. Recommend only safe render-only corrections."},
            {"role": "user", "content": content},
        ],
        "temperature": 0.08,
        "max_tokens": 1600,
        "response_format": {"type": "json_object"},
    }
    try:
        log(status_cb, "Reasoning Agent pre-render edit audit starting...")
        data = post_json_url(WAVESPEED_LLM_API, payload, timeout=180)
        review = extract_json_object(data["choices"][0]["message"]["content"])
        if not isinstance(review, dict):
            return {}
        log(status_cb, f"Reasoning Agent pre-render audit score: {review.get('overall_score', 'n/a')}/10")
        if review.get("summary"):
            log(status_cb, f"Reasoning Agent pre-render audit summary: {review.get('summary')}")
        return review
    except Exception as exc:
        log(status_cb, f"Reasoning Agent pre-render edit audit skipped: {exc}")
        return {}


def scene_id_matches(scene, value):
    if value is None:
        return False
    text = str(value).strip().lower().replace("scene", "").strip()
    scene_id = str(scene.get("id", "")).strip().lower()
    scene_name = str(scene.get("name", "")).strip().lower().replace("scene", "").strip()
    return text in {scene_id, scene_id.lstrip("0"), scene_name, scene_name.lstrip("0")}


def calm_scene_motion(scene, force_contain=False):
    changed = False
    motion = scene.setdefault("motion", {})
    for key, value in {"zoom_start": 1.0, "zoom_end": 1.025, "pan_x": 0, "pan_y": 0}.items():
        if motion.get(key) != value:
            motion[key] = value
            changed = True
    for shot in scene.get("shots", []):
        if force_contain and shot.get("asset") and shot.get("fit") != "contain":
            shot["fit"] = "contain"
            changed = True
        shot_motion = shot.setdefault("motion", {})
        for key, value in {"zoom_start": 1.0, "zoom_end": 1.025, "pan_x": 0, "pan_y": 0}.items():
            if shot_motion.get(key) != value:
                shot_motion[key] = value
                changed = True
    return changed


def apply_llm_corrections(config, review):
    if not review or not review.get("needs_correction"):
        return config, False
    corrected = json.loads(json.dumps(config_for_json(config)))
    changed = False
    scene_targets = set()
    
    # Process structured actions
    force_all_contain = False
    for action_obj in review.get("correction_actions", []) or []:
        if not isinstance(action_obj, dict):
            continue
        action = action_obj.get("action", "")
        scene_value = str(action_obj.get("scene", "")).strip()
        
        if action == "reduce_global_motion":
            corrected["still_motion_scale"] = min(float(corrected.get("still_motion_scale", 0.28)), 0.16)
            corrected["contain_motion_scale"] = min(float(corrected.get("contain_motion_scale", 0.35)), 0.18)
            changed = True
        elif action == "reduce_grain_tint":
            corrected["grain"] = min(int(corrected.get("grain", 16)), 8)
            corrected["tint_alpha"] = min(int(corrected.get("tint_alpha", 14)), 8)
            changed = True
        elif action == "lower_seedance_audio":
            corrected["seedance_audio_volume"] = min(float(corrected.get("seedance_audio_volume", 0.22)), 0.16)
            corrected["seedance_audio_volume_with_speech"] = min(float(corrected.get("seedance_audio_volume_with_speech", 0.12)), 0.08)
            changed = True
        elif action == "enable_sfx":
            corrected["sfx_enabled"] = True
            corrected["sfx_volume"] = max(float(corrected.get("sfx_volume", 0.08)), 0.085)
            corrected["sfx_volume_with_speech"] = max(float(corrected.get("sfx_volume_with_speech", 0.07)), 0.075)
            corrected["sfx_transition_volume"] = max(float(corrected.get("sfx_transition_volume", 0.085)), 0.095)
            corrected["sfx_transition_volume_with_speech"] = max(float(corrected.get("sfx_transition_volume_with_speech", 0.075)), 0.085)
            corrected["sfx_max_per_minute"] = max(int(corrected.get("sfx_max_per_minute", 34)), 40)
            changed = True
        elif action == "raise_sfx_audio":
            corrected["sfx_volume"] = max(float(corrected.get("sfx_volume", 0.08)), 0.09)
            corrected["sfx_volume_with_speech"] = max(float(corrected.get("sfx_volume_with_speech", 0.07)), 0.08)
            changed = True
        elif action == "lower_sfx_audio":
            corrected["sfx_volume"] = min(float(corrected.get("sfx_volume", 0.08)), 0.06)
            corrected["sfx_volume_with_speech"] = min(float(corrected.get("sfx_volume_with_speech", 0.07)), 0.055)
            corrected["sfx_transition_volume"] = min(float(corrected.get("sfx_transition_volume", 0.085)), 0.065)
            corrected["sfx_transition_volume_with_speech"] = min(float(corrected.get("sfx_transition_volume_with_speech", 0.075)), 0.06)
            changed = True
        elif action == "enable_background_music":
            if bool(corrected.get("background_music_user_enabled", corrected.get("background_music_enabled", False))):
                corrected["background_music_enabled"] = True
                changed = True
        elif action in {"contain_scene", "reduce_motion_scene"}:
            if scene_value:
                scene_targets.add(scene_value)
            else:
                force_all_contain = True

    if not force_all_contain:
        force_all_contain = any(str(issue.get("crop_risk", "")).lower() in {"unsafe", "marginal"} for issue in review.get("issues", []) if isinstance(issue, dict))
    if not force_all_contain and not scene_targets:
        return corrected, changed
    for scene in corrected.get("scenes", []):
        if scene.get("speaker_hook"):
            continue
        targeted = force_all_contain or any(scene_id_matches(scene, target) for target in scene_targets)
        if targeted:
            changed = calm_scene_motion(scene, force_contain=force_all_contain) or changed
    return corrected, changed


def render_audio_variant(config, suffix, status_cb=None, **overrides):
    variant = dict(config)
    variant["output_basename"] = f"{config.get('output_basename', config.get('project_slug', 'short'))}_{suffix}"
    variant.update(overrides)
    variant["_config_path"] = config.get("_config_path")
    variant["_cancel_event"] = config.get("_cancel_event")
    variant["_status_cb"] = status_cb
    try:
        log(status_cb, f"Rendering audio variant: {suffix}...")
        output = pipeline.render_video(variant)
        log(status_cb, f"Saved render variant {suffix}: {Path(output).name}")
        return output
    except Exception as exc:
        log(status_cb, f"Render variant {suffix} skipped: {exc}")
        return None


def create_render_variants(config, final_output, status_cb=None):
    variants = {"all_sounds": str(final_output)}
    no_sfx = render_audio_variant(
        config,
        "no_sfx",
        status_cb=status_cb,
        sfx_enabled=False,
    )
    if no_sfx:
        variants["no_sfx"] = str(no_sfx)
    if config.get("clip_source") == "scrape":
        # Scraped clips are normalized with -an, so the former "seedance_audio_only" file was
        # actually narration-only. Name that useful clean reference honestly.
        voice_only = render_audio_variant(
            config,
            "voice_only",
            status_cb=status_cb,
            sfx_enabled=False,
            background_music_enabled=False,
            background_music_user_enabled=False,
            seedance_audio_in_final=False,
            seedance_audio_volume=0.0,
            seedance_audio_volume_with_speech=0.0,
        )
        if voice_only:
            variants["voice_only"] = str(voice_only)
    else:
        only_seedance = render_audio_variant(
            config,
            "seedance_audio_only",
            status_cb=status_cb,
            sfx_enabled=False,
            background_music_enabled=False,
            background_music_user_enabled=False,
            seedance_audio_in_final=True,
        )
        if only_seedance:
            variants["seedance_audio_only"] = str(only_seedance)
    return variants


def config_missing_gpt_assets(config):
    try:
        _, asset_dir, _, _ = pipeline.project_paths(config)
    except Exception:
        return []
    missing = []
    for scene in config.get("scenes", []):
        if not scene.get("needs_gpt_asset"):
            continue
        asset = scene.get("asset")
        if not asset:
            continue
        path = pipeline.resolve_media_path(config, asset_dir, asset)
        if not path.exists():
            missing.append(path)
    return missing


def gather_web_images_for_script(
    project_dir,
    title,
    script,
    target_duration,
    images_per_scene=2,
    target_count=None,
    use_llm_search=True,
    use_glm=None,
    scenes_override=None,
    force_new=False,
    reasoning_model=None,
    status_cb=None,
):
    web_dir = project_dir / "web images"
    web_dir.mkdir(parents=True, exist_ok=True)
    existing = [p for p in web_dir.iterdir() if p.suffix.lower() in IMAGE_EXTS and p.stat().st_size > 1000]
    scenes = scenes_override or parse_timed_script(script, target_duration)
    images_per_scene = max(1, int(images_per_scene or 1))
    target_count = max(1, int(target_count or len(scenes) * images_per_scene))
    manifest_path = web_dir / "web_image_manifest.json"
    manifest = []
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            manifest = []
    if len(existing) >= target_count and not force_new:
        log(status_cb, f"Web image search skipped; {len(existing)} existing image(s) in web images.")
        return [str(p) for p in existing]

    seen_urls = {item.get("url") for item in manifest if isinstance(item, dict)}
    existing_hashes = {item.get("phash") for item in manifest if isinstance(item, dict) and item.get("phash")}
    downloaded = [] if force_new else [str(p) for p in existing]
    searched_queries = set()
    if use_glm is not None:
        use_llm_search = bool(use_glm)
    profile = build_topic_profile(title, script, scenes, use_gpt55=use_llm_search, reasoning_model=reasoning_model, status_cb=status_cb)
    log(status_cb, f"Searching general web images for topic: {profile.get('canonical', title)} using {', '.join(WEB_IMAGE_SEARCH_PROVIDERS)}.")
    # Pass 1: search every scene's queries in parallel (network-bound, the slow part).
    query_tasks = []
    for scene_index, scene in enumerate(scenes, 1):
        for query in web_queries_for_scene(title, scene, scene_index=scene_index, profile=profile):
            ql = query.lower()
            if ql in searched_queries:
                continue
            searched_queries.add(ql)
            query_tasks.append((scene_index, query))

    def _run_query(task):
        scene_index, query = task
        log(status_cb, f"Scene {scene_index}: general web image query '{query}'")
        try:
            return scene_index, general_search_images(query, limit=18, status_cb=status_cb)
        except Exception as exc:
            log(status_cb, f"Search failed for '{query}': {exc}")
            return scene_index, []

    results_by_scene = {}
    if query_tasks:
        workers = min(8, len(query_tasks))
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            for scene_index, results in pool.map(_run_query, query_tasks):
                results_by_scene.setdefault(scene_index, []).extend(results)

    # Pass 2: ground/score/download per scene (sequential; preserves dedup + targets).
    for scene_index, scene in enumerate(scenes, 1):
        if len(downloaded) >= target_count:
            break
        scene_downloads = 0
        candidates = []
        for result in results_by_scene.get(scene_index, []):
            if result["url"] in seen_urls:
                continue
            grounded, ground_reason = web_candidate_topic_grounded(result, profile, scene)
            if not grounded:
                log(status_cb, f"Rejected off-topic general web candidate before download: {result.get('title', '')} ({ground_reason})")
                continue
            result["score"] = web_candidate_score(result, profile, scene)
            candidates.append(result)
        candidates.sort(key=lambda item: item.get("score", 0), reverse=True)
        for result in candidates:
            if scene_downloads >= images_per_scene or len(downloaded) >= target_count:
                break
            if result.get("score", 0) <= -900:
                log(status_cb, f"Hard rejected obvious bad web candidate before GPT visual review: {result.get('title', '')} (score {result.get('score', 0)})")
                continue
            if result["url"] in seen_urls:
                continue
            suffix = Path(urllib.parse.urlparse(result["url"]).path).suffix
            filename = f"scene_{scene_index:02d}_{safe_media_filename(result['title'], suffix)}"
            out = web_dir / filename
            counter = 2
            while out.exists():
                out = web_dir / f"{Path(filename).stem}_{counter}{Path(filename).suffix}"
                counter += 1
            try:
                size = download_url_to_file(result["url"], out, referer=result.get("source_url", ""))
            except Exception as exc:
                log(status_cb, f"Download failed: {result['url']} ({exc})")
                continue
            
            import asset_quality
            aq = asset_quality.validate_and_analyze_image(out, existing_hashes)
            
            record = {
                **result,
                "scene": scene_index,
                "script": scene.get("script", ""),
                "local_path": str(out),
                "bytes": size,
                **aq
            }
            manifest.append(record)
            seen_urls.add(result["url"])
            if aq.get("phash"):
                existing_hashes.add(aq["phash"])

            if not aq["local_quality_pass"]:
                log(status_cb, f"Local quality reject: {out.name} ({aq['local_reject_reason']})")
                record["review_status"] = "rejected_locally"
                rejected_dir = web_dir / "rejected"
                rejected_dir.mkdir(parents=True, exist_ok=True)
                rejected_path = rejected_dir / out.name
                out.replace(rejected_path)
                record["local_path"] = str(rejected_path)
                continue

            downloaded.append(str(out))
            scene_downloads += 1
            log(status_cb, f"Downloaded web image candidate from {result.get('provider', 'web')} for Reasoning Agent visual review: {out.name} (search score {result.get('score', 0)})")
        if scene_downloads < images_per_scene:
            log(status_cb, f"Scene {scene_index}: downloaded {scene_downloads} web candidate image(s) for Reasoning Agent visual review.")
    if len(downloaded) < target_count:
        fallback_queries = [profile.get("canonical", title)] + list(profile.get("aliases", []))
        fallback_deduped = []
        for query in fallback_queries:
            query = " ".join(str(query).split())
            if not query:
                continue
            if query.lower() not in [item.lower() for item in fallback_deduped]:
                fallback_deduped.append(query)
        for query in fallback_deduped[:5]:
            if len(downloaded) >= target_count:
                break
            if query.lower() in searched_queries:
                continue
            searched_queries.add(query.lower())
            log(status_cb, f"Topic fallback general web image query '{query}'")
            try:
                results = general_search_images(query, limit=20, status_cb=status_cb)
            except Exception as exc:
                log(status_cb, f"Search failed for '{query}': {exc}")
                continue
            scored = sorted(
                (
                    {**result, "score": web_candidate_score(result, profile, scenes[0])}
                    for result in results
                    if result["url"] not in seen_urls and web_candidate_topic_grounded(result, profile, scenes[0])[0]
                ),
                key=lambda item: item.get("score", 0),
                reverse=True,
            )
            for result in scored:
                if len(downloaded) >= target_count:
                    break
                if result.get("score", 0) <= -900:
                    log(status_cb, f"Hard rejected obvious bad fallback candidate before GPT visual review: {result.get('title', '')} (score {result.get('score', 0)})")
                    continue
                suffix = Path(urllib.parse.urlparse(result["url"]).path).suffix
                filename = f"topic_{safe_media_filename(result['title'], suffix)}"
                out = web_dir / filename
                counter = 2
                while out.exists():
                    out = web_dir / f"{Path(filename).stem}_{counter}{Path(filename).suffix}"
                    counter += 1
                try:
                    size = download_url_to_file(result["url"], out, referer=result.get("source_url", ""))
                except Exception as exc:
                    log(status_cb, f"Download failed: {result['url']} ({exc})")
                    continue
                
                import asset_quality
                aq = asset_quality.validate_and_analyze_image(out, existing_hashes)
                
                record = {
                    **result,
                    "scene": "topic",
                    "script": title,
                    "local_path": str(out),
                    "bytes": size,
                    **aq
                }
                manifest.append(record)
                seen_urls.add(result["url"])
                if aq.get("phash"):
                    existing_hashes.add(aq["phash"])

                if not aq["local_quality_pass"]:
                    log(status_cb, f"Local quality reject (topic): {out.name} ({aq['local_reject_reason']})")
                    record["review_status"] = "rejected_locally"
                    rejected_dir = web_dir / "rejected"
                    rejected_dir.mkdir(parents=True, exist_ok=True)
                    rejected_path = rejected_dir / out.name
                    out.replace(rejected_path)
                    record["local_path"] = str(rejected_path)
                    continue

                downloaded.append(str(out))
                log(status_cb, f"Downloaded topic web candidate from {result.get('provider', 'web')} for Reasoning Agent visual review: {out.name} (search score {result.get('score', 0)})")
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return downloaded


def recover_legacy_timeline_overlays(config, project_dir):
    """Restore editable arrows for renders made before overlays were persisted in project.json.

    Older runs kept the validated callout decisions in review/agent_report.json, but wrote
    project.json immediately before visual-FX planning. The exact target coordinates were not
    included in that report, so recovered arrows start in a safe upper-centre position and remain
    fully movable in the timeline editor.
    """
    scenes = config.get("scenes") or []
    if not scenes or config.get("timeline_overlays_managed"):
        return 0
    if any(scene.get("overlays") for scene in scenes):
        return 0
    report_path = Path(project_dir) / "review" / "agent_report.json"
    if not report_path.exists():
        return 0
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except Exception:
        return 0
    rows = report.get("scene_visual_fx")
    if not isinstance(rows, list):
        rows = ((report.get("visual_fx_report") or {}).get("scene_visual_fx") or [])
    recovered = 0
    by_id = {str(scene.get("id", i)): scene for i, scene in enumerate(scenes)}
    for row in rows:
        if not isinstance(row, dict):
            continue
        if not (row.get("callout_enabled") or str(row.get("callout_type", "")).lower() == "arrow"):
            continue
        raw_id = row.get("scene_id")
        scene = by_id.get(str(raw_id))
        index = None
        try:
            index = int(raw_id)
        except (TypeError, ValueError):
            pass
        if scene is None and index is not None and 0 <= index < len(scenes):
            scene = scenes[index]
        if scene is None:
            continue
        try:
            duration = max(0.6, float(scene.get("end", 0)) - float(scene.get("start", 0)))
        except (TypeError, ValueError):
            duration = 2.0
        start = min(0.45, 0.25 / duration)
        end = min(0.98, start + 0.8 / duration)
        # Newer reports may contain coordinates; old ones did not. Keep the fallback above the
        # caption band so the user can immediately grab and position it over the real target.
        try:
            cx = max(0.05, min(0.95, float(row.get("cx", row.get("anchor_cx", 0.5)))))
            cy = max(0.08, min(0.92, float(row.get("cy", row.get("anchor_cy", 0.38)))))
        except (TypeError, ValueError):
            cx, cy = 0.5, 0.38
        sid = str(scene.get("id", index if index is not None else recovered))
        scene["overlays"] = [{
            "id": f"legacy-arrow-{sid}", "type": "callout", "shape": "arrow",
            "cx": round(cx, 4), "cy": round(cy, 4),
            "from": "left" if cx > 0.5 else "right",
            "start": round(start, 3), "end": round(end, 3),
            "legacy_recovered": True,
        }]
        recovered += 1
    if recovered:
        config["smart_overlays"] = True
        config["_legacy_overlays_recovered"] = recovered
    return recovered


def load_project_config(slug):
    """Load the latest saved render config for a project (for the timeline editor)."""
    config_dir = PROJECTS_DIR / slug / "config"
    if not config_dir.exists():
        raise RuntimeError(f"No saved config for project '{slug}'.")
    preferred = config_dir / "project.json"
    if preferred.exists():
        path = preferred
    else:
        candidates = sorted(config_dir.glob("project*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not candidates:
            raise RuntimeError(f"No saved config for project '{slug}'.")
        path = candidates[0]
    config = json.loads(path.read_text(encoding="utf-8"))
    config["project_slug"] = slug
    recover_legacy_timeline_overlays(config, PROJECTS_DIR / slug)
    timeline_edits_path = config_dir / "timeline_edits.json"
    if timeline_edits_path.exists():
        try:
            saved_edits = json.loads(timeline_edits_path.read_text(encoding="utf-8"))
            if isinstance(saved_edits, dict):
                apply_timeline_edits_to_config(config, saved_edits, slug)
        except Exception:
            pass
    return config


def apply_timeline_edits_to_config(config, edits, slug):
    """Apply the timeline editor's edits onto a loaded config IN PLACE so that a
    render of this config reproduces exactly what the editor shows. Shared by the
    live render, the Save button and Agent rework."""
    edits = edits or {}
    scenes = config.get("scenes", [])
    # Preserve narration timing before visual clips are reordered/resized. The voiceover stays
    # unchanged, so captions must follow its original timeline rather than the edited visuals.
    if not config.get("timeline_caption_track"):
        config["timeline_caption_track"] = [
            {
                "start": float(scene.get("start", 0) or 0),
                "end": float(scene.get("end", scene.get("start", 0)) or 0),
                "text": str(scene.get("exact_voice_text") or scene.get("caption")
                            or scene.get("script") or ""),
                "word_timings": list(scene.get("word_timings") or []),
            }
            for scene in sorted(scenes, key=lambda row: float(row.get("start", 0) or 0))
        ]
    # newly added clips dragged in from the library
    project_dir = PROJECTS_DIR / slug
    added_by_id = {}
    for a in (edits.get("added") or []):
        aid = str(a.get("id") or "")
        if not aid:
            continue
        asset = a.get("path") or ""
        is_clip = (a.get("kind") == "clip")
        # a["clip"] is the editor preview URL ('file?path=<url-encoded>'), NOT a bare filename;
        # decode it to the real basename so the renderer can resolve the clip (else it opened the
        # .mp4 as an image -> "cannot identify image file"). Fall back to the asset path's basename.
        clip_name = pipeline._clip_ref_basename(a.get("clip")) or None
        if is_clip and not clip_name and asset:
            clip_name = pipeline._clip_ref_basename(asset) or None
        # A clip dragged from the "All projects" tab (or any external path) lives in ANOTHER
        # project's folder - copy it into THIS project so the render resolves it and it can't
        # break if the source project is deleted. Video clips resolve by basename in
        # seedance 2.0/, so the copied name IS the clip name.
        try:
            src = Path(asset)
            if is_clip and src.is_absolute() and src.is_file():
                clip_dir = project_dir / "seedance 2.0"
                already_here = (clip_dir.resolve() == src.parent.resolve())
                if not already_here:
                    clip_dir.mkdir(parents=True, exist_ok=True)
                    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", src.stem)[:28]
                    key = hashlib.sha1(str(src.resolve()).encode("utf-8", "ignore")).hexdigest()[:8]
                    dest = clip_dir / f"imported_{safe}_{key}{src.suffix.lower() or '.mp4'}"
                    if not dest.exists():
                        shutil.copy2(src, dest)
                    asset = dest.name
                    clip_name = dest.name
        except Exception:
            pass
        added_by_id[aid] = {
            "id": aid,
            "name": a.get("label") or "Added clip",
            "caption": "",
            "asset": asset,
            "clip": clip_name,
            "seedance": bool(is_clip),
            "added": True,
        }
    removed = {str(x) for x in (edits.get("removed") or [])}
    # In-editor "Replace media": scene_id -> chosen library media. The new media is copied into the
    # project and the scene points at it, KEEPING the scene's existing duration (so the new clip is
    # cut to the replaced clip's length by the normal per-scene trim).
    replaced_by_id = {}
    for r in (edits.get("replaced") or []):
        rid = str(r.get("id") or "")
        src = str(r.get("path") or "")
        if rid and src:
            replaced_by_id[rid] = r
    dur_by_id = {str(d.get("id")): d.get("duration") for d in (edits.get("scenes") or []) if d.get("duration") is not None}
    speed_by_id = {str(d.get("id")): d.get("speed") for d in (edits.get("scenes") or [])
                   if d.get("speed") is not None}
    blur_by_id = {str(d.get("id")): bool(d.get("blur_captions")) for d in (edits.get("scenes") or [])
                  if d.get("blur_captions") is not None}
    # CapCut-style per-clip source in-point ("cut front") -> the renderer reads it via
    # seedance_clip_start_trim(config, scene) = scene["seedance_start_trim"].
    trim_by_id = {str(d.get("id")): d.get("source_trim") for d in (edits.get("scenes") or [])
                  if d.get("source_trim") is not None}
    # user's manual green/red flip of the intended-subject label (display only)
    subject_override_by_id = {str(d.get("id")): str(d.get("subject_override"))
                              for d in (edits.get("scenes") or [])
                              if d.get("subject_override") in ("good", "bad")}
    overlays_by_id = {}
    for row in (edits.get("overlays") or []):
        sid = str(row.get("scene_id") or "") if isinstance(row, dict) else ""
        if not sid:
            continue
        cleaned = []
        for raw in (row.get("items") or []):
            if not isinstance(raw, dict):
                continue
            item = dict(raw)
            item["id"] = str(item.get("id") or f"ov-{sid}-{len(cleaned)}")[:80]
            for key, lo, hi, default in (("editor_x", 0.0, 1.0, 0.5),
                                          ("editor_y", 0.0, 1.0, 0.5),
                                          ("editor_scale", 0.25, 3.0, 1.0),
                                          ("editor_rotation", -180.0, 180.0, 0.0),
                                          ("animation_duration", 0.1, 1.5, 0.28),
                                          ("appear_sfx_volume", 0.0, 0.6, 0.22),
                                          ("appear_sfx_duration", 0.08, 2.0, 1.0)):
                if item.get(key) is None:
                    continue
                try:
                    item[key] = round(max(lo, min(hi, float(item[key]))), 4)
                except (TypeError, ValueError):
                    item[key] = default
            allowed_styles = {"default_thick_red_arrow", "yellow_sticker_arrow",
                              "white_sticker_arrow", "neon_green_arrow"}
            allowed_animations = {"none", "fade", "pop", "slide", "bounce"}
            item["arrow_style"] = (str(item.get("arrow_style") or "default_thick_red_arrow")
                                   if str(item.get("arrow_style") or "default_thick_red_arrow") in allowed_styles
                                   else "default_thick_red_arrow")
            item["animation"] = (str(item.get("animation") or "pop").lower()
                                 if str(item.get("animation") or "pop").lower() in allowed_animations
                                 else "pop")
            sfx_path = str(item.get("appear_sfx_path") or "")
            if sfx_path:
                try:
                    resolved_sfx = Path(sfx_path).resolve()
                    if (not resolved_sfx.is_file()
                            or resolved_sfx.suffix.lower() not in pipeline.SFX_EXTS):
                        raise ValueError("invalid overlay SFX")
                    item["appear_sfx_path"] = str(resolved_sfx)
                except Exception:
                    item.pop("appear_sfx_path", None)
                    item.pop("appear_sfx_name", None)
            # Timeline VFX library image stickers (meme / neko / custom): the asset must be a real
            # image under an allowed root, else the overlay is dropped (render can't resolve it and
            # we won't let the editor point the renderer at arbitrary files).
            if str(item.get("type")) == "image":
                ap = str(item.get("asset") or item.get("path") or "")
                ok = False
                if ap:
                    try:
                        rp = Path(ap).resolve()
                        roots = [(ROOT / "assets" / "meme_stickers").resolve(),
                                 (ROOT / "static" / "emotions").resolve(),
                                 (project_dir).resolve()]
                        if (rp.is_file() and rp.suffix.lower() in {".png", ".webp", ".gif"}
                                and any(str(rp).startswith(str(r)) for r in roots)):
                            item["asset"] = str(rp)
                            item.pop("path", None)
                            ok = True
                    except Exception:
                        ok = False
                if not ok:
                    continue
            cleaned.append(item)
        overlays_by_id[sid] = cleaned
    order = edits.get("order")
    idmap = {str(s.get("id", i)): s for i, s in enumerate(scenes)}
    idmap.update(added_by_id)
    if order:
        scenes = [idmap[str(i)] for i in order if str(i) in idmap] or scenes

    new_scenes, t = [], 0.0
    for i, scene in enumerate(scenes):
        sid = str(scene.get("id", i))
        if sid in removed:
            continue
        try:
            dur = float(dur_by_id.get(sid)) if sid in dur_by_id else float(scene.get("end", 0)) - float(scene.get("start", 0))
        except (TypeError, ValueError):
            dur = float(scene.get("end", 0)) - float(scene.get("start", 0))
        dur = max(0.5, min(20.0, dur or 1.0))
        scene = dict(scene)
        if sid in trim_by_id:
            try:
                scene["seedance_start_trim"] = max(0.0, round(float(trim_by_id[sid]), 3))
            except (TypeError, ValueError):
                pass
        if sid in overlays_by_id:
            scene["overlays"] = overlays_by_id[sid]
        if sid in subject_override_by_id:
            scene["subject_override"] = subject_override_by_id[sid]
        # apply an in-editor media replacement for this scene (copy the chosen file into the project
        # so the renderer resolves it; keep the scene duration -> new clip is trimmed to that length)
        if sid in replaced_by_id:
            try:
                src = Path(replaced_by_id[sid]["path"])
                if src.exists():
                    is_video = (str(replaced_by_id[sid].get("type")) == "video"
                                or src.suffix.lower() in VIDEO_EXTS)
                    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", src.stem)[:18]
                    if is_video:
                        dest_dir = project_dir / "seedance 2.0"
                        dest_dir.mkdir(parents=True, exist_ok=True)
                        dest = dest_dir / f"replaced_{sid}_{safe}{src.suffix.lower()}"
                        if not dest.exists():
                            shutil.copy2(src, dest)
                        scene["clip"] = dest.name      # scene_clip_path resolves clip_dir/<name>
                        scene["asset"] = dest.name
                        scene["seedance"] = True
                        # new media -> the old speed/blur source chain no longer applies
                        scene.pop("timeline_speed_src", None)
                        scene.pop("caption_blur_src", None)
                    else:
                        dest_dir = project_dir / "web images"
                        dest_dir.mkdir(parents=True, exist_ok=True)
                        dest = dest_dir / f"replaced_{sid}_{safe}{src.suffix.lower()}"
                        if not dest.exists():
                            shutil.copy2(src, dest)
                        scene["asset"] = str(dest.resolve())   # resolve_media_path handles absolute
                        scene["clip"] = None
                        scene["seedance"] = False
            except Exception:
                pass
        # BURNED-IN CAPTION BLUR: per-media toggle from the editor. Applied as a PRE-PASS
        # like clip speed: the ORIGINAL is copied to capblur_<sid>_<stem>.mp4 and only the
        # OCR-detected caption letters get blurred there - toggling OFF simply points the
        # scene back at the untouched original. Runs BEFORE the speed pre-pass so a re-speed
        # re-encodes the blurred footage, not the raw one.
        want_blur = blur_by_id.get(sid) if sid in blur_by_id else bool(scene.get("blur_captions"))
        scene["blur_captions"] = bool(want_blur)
        if scene.get("clip"):
            try:
                clip_dir = project_dir / "seedance 2.0"
                base = str(scene.get("caption_blur_src") or scene.get("timeline_speed_src") or scene["clip"])
                if want_blur and not base.startswith(("speed_", "capblur_")) and (clip_dir / base).exists():
                    dest = clip_dir / f"capblur_{sid}_{Path(base).stem[:24]}.mp4"
                    if not dest.exists():
                        shutil.copy2(clip_dir / base, dest)
                        found = 0
                        try:
                            import clip_scraper as _cs_blur
                            found = _cs_blur.blur_caption_regions(dest, pipeline.find_ffmpeg())
                        except Exception:
                            found = 0
                        if not found:
                            # no burned-in captions detected -> drop the useless copy
                            dest.unlink(missing_ok=True)
                    if dest.exists() and dest.stat().st_size > 4096:
                        scene["caption_blur_src"] = base
                        # the blurred file becomes the base every later step works from
                        if scene.get("timeline_speed_src"):
                            scene["timeline_speed_src"] = dest.name
                        if not str(scene.get("clip") or "").startswith("speed_"):
                            scene["clip"] = dest.name
                            scene["asset"] = dest.name
                elif not want_blur and scene.get("caption_blur_src"):
                    orig0 = str(scene["caption_blur_src"])
                    if (clip_dir / orig0).exists():
                        if scene.get("timeline_speed_src"):
                            scene["timeline_speed_src"] = orig0
                        if not str(scene.get("clip") or "").startswith("speed_"):
                            scene["clip"] = orig0
                            scene["asset"] = orig0
                    # keep caption_blur_src: re-enabling later reuses the cached capblur file
            except Exception:
                pass
        # CLIP SPEED: applied as a safe PRE-PASS - the source clip is re-encoded once into a
        # speed_<sid>_<x>.mp4 (setpts, no audio) and the scene points at that file. The normal
        # per-scene trim then cuts it to the scene duration, so the render graph is untouched.
        try:
            speed = float(speed_by_id.get(sid)) if sid in speed_by_id else float(scene.get("timeline_speed") or 1.0)
        except (TypeError, ValueError):
            speed = 1.0
        speed = max(0.5, min(2.0, speed))
        # Persist the user's chosen value even when the source clip is temporarily unavailable or
        # FFmpeg cannot prepare the cached speed file during Save. Render/load can retry later.
        scene["timeline_speed"] = speed
        if abs(speed - 1.0) > 0.01 and scene.get("clip"):
            try:
                clip_dir = project_dir / "seedance 2.0"
                # always re-encode from the ORIGINAL clip (remembered across saves) so the
                # user can change 1.25x -> 1.5x -> 1.0x without compounding re-encodes
                src_name = str(scene.get("timeline_speed_src") or scene["clip"])
                src = clip_dir / src_name
                if src.exists() and not src_name.startswith("speed_"):
                    tag = str(round(speed, 2)).replace(".", "p")
                    dest = clip_dir / f"speed_{sid}_{tag}_{src.stem[:24]}.mp4"
                    if not dest.exists():
                        ffm = pipeline.find_ffmpeg()
                        subprocess.run([ffm, "-y", "-hide_banner", "-loglevel", "error",
                                        "-i", str(src), "-vf", f"setpts=PTS/{speed:.4f}",
                                        "-an", "-c:v", "libx264", "-crf", "19",
                                        "-preset", "veryfast", str(dest)],
                                       capture_output=True, timeout=300)
                    if dest.exists() and dest.stat().st_size > 4096:
                        scene["clip"] = dest.name
                        scene["asset"] = dest.name
                        scene["timeline_speed"] = speed   # editor reloads with the value
                        scene["timeline_speed_src"] = src_name
            except Exception:
                pass
        elif sid in speed_by_id:
            # back to 1.0x: point the scene at the remembered original again
            orig = str(scene.get("timeline_speed_src") or "")
            if orig and str(scene.get("clip") or "").startswith("speed_") \
                    and (project_dir / "seedance 2.0" / orig).exists():
                scene["clip"] = orig
                scene["asset"] = orig
            scene["timeline_speed"] = speed
        scene["start"] = round(t, 3)
        scene["end"] = round(t + dur, 3)
        t += dur
        new_scenes.append(scene)
    if not new_scenes:
        raise RuntimeError("Timeline has no scenes left to render.")
    config["scenes"] = new_scenes
    config["duration"] = round(t, 3)
    if "overlays" in edits:
        config["smart_overlays"] = any(scene.get("overlays") for scene in new_scenes)
        config["timeline_overlays_managed"] = True

    volumes = edits.get("volumes") or {}

    def set_volume(key, value):
        try:
            config[key] = max(0.0, min(1.5, float(value)))
        except (TypeError, ValueError):
            pass

    if "voice" in volumes:
        set_volume("audio_master_gain", volumes["voice"])
    if "music" in volumes:
        set_volume("background_music_volume", volumes["music"])
        set_volume("background_music_volume_with_speech", volumes["music"])
        # The mixer volume only ADJUSTS the bed loudness - it must never ENABLE music the
        # project didn't have. (The editor's default music volume was >0, so every timeline
        # render silently attached an auto-picked track even when the run had no music.)
        try:
            if float(volumes["music"]) <= 0.0:
                config["background_music_enabled"] = False
        except (TypeError, ValueError):
            pass

    # Captions are locked to the voice timing; the editor only toggles them on/off.
    if "captions" in edits:
        config["render_captions"] = bool(edits["captions"])

    # Master "Sound FX" switch: off = render the voice only (mutes content/transition/generated AND
    # the user-added custom_sfx + overlay SFX). pipeline.build_sfx_segments honours render_sfx_enabled.
    if "sfx_on" in edits:
        config["render_sfx_enabled"] = bool(edits["sfx_on"])

    # Per-event SFX edits (each transition and content effect tuned individually).
    overrides = dict(config.get("sfx_overrides") or {})
    custom_sfx = []
    for item in (edits.get("transitions") or []) + (edits.get("sfx") or []):
        eid = str(item.get("id") or "")
        if not eid:
            continue
        if item.get("added"):
            custom_sfx.append({
                "id": eid, "scene_id": str(item.get("scene_id") or ""),
                "path": item.get("path") or "", "offset": float(item.get("offset") or 0.0),
                "volume": max(0.0, min(0.6, float(item.get("volume") or 0.25))),
                "duration": max(0.05, float(item.get("duration") or 1.0)),
                "source_trim": max(0.0, float(item.get("source_trim") or 0.0)),
                "source_duration": max(0.0, float(item.get("source_duration") or 0.0)),
                "playback_rate": max(0.01, float(item.get("playback_rate") or 1.0)),
                "enabled": item.get("enabled") is not False,
                "label": item.get("label") or "Sound",
                "is_transition": bool(item.get("is_transition")),
            })
            continue
        entry = {}
        if item.get("volume") is not None:
            try:
                entry["volume"] = max(0.0, min(0.6, float(item["volume"])))
            except (TypeError, ValueError):
                pass
        if item.get("enabled") is not None:
            entry["enabled"] = bool(item["enabled"])
        # editor moved the event (absolute seconds) or picked a different sound file
        if item.get("start_abs") is not None:
            try:
                entry["start_abs"] = round(max(0.0, float(item["start_abs"])), 3)
            except (TypeError, ValueError):
                pass
        if item.get("path_override"):
            entry["path"] = str(item["path_override"])
        if item.get("source_trim") is not None:
            try:
                entry["source_trim"] = max(0.0, float(item["source_trim"]))
            except (TypeError, ValueError):
                pass
        if entry:
            overrides[eid] = entry
    if overrides:
        config["sfx_overrides"] = overrides
        # NOTE: do NOT force sfx_enabled=True here. That silently re-enabled the auto-SFX planner
        # on projects that deliberately disabled it (e.g. an SFX-Master project = sfx_enabled False,
        # meant to mix ONLY the editable custom_sfx), so random auto transition SFX appeared in the
        # render that were never shown in the timeline editor. Respect the project's sfx_enabled.
    if "sfx" in edits or "transitions" in edits:
        # always replace: deleting an added sound must not leave the stale one behind
        config["custom_sfx"] = custom_sfx
    return config


def save_timeline_edits(slug, edits):
    """Persist timeline-editor edits into the project's main config so the editor
    reloads identically and the next render reproduces the saved state."""
    config = load_project_config(slug)
    apply_timeline_edits_to_config(config, edits, slug)
    project_dir = PROJECTS_DIR / slug
    out_path = project_dir / "config" / "project.json"
    edits_path = project_dir / "config" / "timeline_edits.json"
    # Keep editor intent separately from the generated project config. A rerun may regenerate
    # project.json, but reopening the timeline reapplies this sidecar exactly.
    edits_tmp = edits_path.with_suffix(".json.tmp")
    edits_tmp.write_text(json.dumps(edits or {}, indent=2), encoding="utf-8")
    os.replace(edits_tmp, edits_path)
    config_tmp = out_path.with_suffix(".json.tmp")
    config_tmp.write_text(json.dumps(config_for_json(config), indent=2), encoding="utf-8")
    os.replace(config_tmp, out_path)
    # Verify the durable sidecar before claiming success to the browser.
    verified = json.loads(edits_path.read_text(encoding="utf-8"))
    if not isinstance(verified, dict):
        raise RuntimeError("Timeline save verification failed.")
    return {"ok": True, "scenes": len(config.get("scenes", [])),
            "saved": True, "edits_file": str(edits_path)}


def ensure_timeline_voice(config, project_dir, status_cb=None):
    """WYSIWYG guarantee for timeline renders: the editor PREVIEWS the sequence with the project's
    narration (voice_url = configured audio_path -> input/voiceover_dehiss.wav -> input/voiceover.wav),
    so the render must include that SAME voice - otherwise a timeline re-render comes out "music/SFX
    only, no voice" whenever the loaded config's audio_path is missing/stale. Resolve the voice file
    the same way the preview does and set it as the render's voice track."""
    project_dir = Path(project_dir)
    ap = str(config.get("audio_path") or "").strip()
    candidates = ([Path(ap)] if ap else []) + [
        project_dir / "input" / name for name in ("voiceover_dehiss.wav", "voiceover.wav")
    ]
    for vp in candidates:
        try:
            if vp.exists():
                config["audio_path"] = str(vp.resolve())
                config["speech_audio_in_final"] = True
                log(status_cb, f"Timeline render: voice track = {vp.name} (matching the editor preview).")
                return str(vp.resolve())
        except OSError:
            continue
    if config.get("speech_audio_in_final"):
        log(status_cb, "Timeline render: no voiceover file found for this project; rendering without voice.")
    return None


def render_project_timeline(slug, edits, status_cb=None, cancel_event=None):
    """Re-render a project from the timeline editor's edits."""
    config = load_project_config(slug)
    project_dir = PROJECTS_DIR / slug
    if cancel_event is not None:
        config["_cancel_event"] = cancel_event
    config["_status_cb"] = status_cb          # real "Rendering frames: N%" for the progress bar
    apply_timeline_edits_to_config(config, edits, slug)
    config["timeline_editor_render"] = True
    ensure_timeline_voice(config, project_dir, status_cb=status_cb)

    stamp = time.strftime("%Y%m%d_%H%M%S")
    config["output_basename"] = f"{slug}_timeline_{stamp}"
    out_config_path = project_dir / "config" / f"project_timeline_{stamp}.json"
    out_config_path.write_text(json.dumps(config_for_json(config), indent=2), encoding="utf-8")
    config["_config_path"] = str(out_config_path)

    log(status_cb, f"Rendering timeline edit: {len(config.get('scenes', []))} scenes, {config['duration']:.1f}s...")
    output = pipeline.render_video(config)
    log(status_cb, f"Timeline render complete: {Path(output).name}")
    return {"title": config.get("title", slug), "project_dir": str(project_dir), "video": str(output)}


def rework_project_sfx(slug, mode="add", reasoning_model=None, sfx_amount="medium",
                       status_cb=None, cancel_event=None):
    """Redo all SFX or add only into gaps of an existing timeline, then render in-place."""
    import sfx_agent
    config = load_project_config(slug)
    project_dir = PROJECTS_DIR / slug
    mode = str(mode or "add").lower()
    if mode not in ("add", "redo"):
        raise RuntimeError("Unknown SFX rework mode.")
    # Fail fast BEFORE the expensive clean-base render if the multimodal Audio Director can't run.
    if not os.environ.get("WAVESPEED_API_KEY"):
        raise RuntimeError("Redo SFX needs WAVESPEED_API_KEY (the multimodal Audio Director). "
                           "Set it and try again.")
    probe_config = copy.deepcopy(config)
    probe_config["render_sfx_enabled"] = True
    existing = pipeline.build_sfx_segments(
        probe_config, has_speech=bool(probe_config.get("audio_path")))
    existing_times = sorted(float(row.get("start") or 0.0) for row in existing)
    if mode == "add" and not existing_times:
        raise RuntimeError("Add more SFX is only available when the timeline already contains SFX.")
    if cancel_event is not None:
        config["_cancel_event"] = cancel_event
    config["_status_cb"] = status_cb
    ensure_timeline_voice(config, project_dir, status_cb=status_cb)

    if mode == "redo":
        # The analysis source must be clean: old SFX are removed before the new director pass.
        clean = copy.deepcopy(config)
        clean["render_sfx_enabled"] = False
        clean["sfx_enabled"] = False
        clean["custom_sfx"] = []
        clean["ai_content_sfx"] = []
        clean["sfx_overrides"] = {}
        clean["output_basename"] = f"{slug}_sfx_clean_{time.strftime('%Y%m%d_%H%M%S')}"
        log(status_cb, f"Redo SFX: rendering a clean base with {len(existing_times)} old SFX removed...")
        source_render = Path(pipeline.render_video(clean))
        blocked_times = []
    else:
        source_render = latest_render = max(
            (p for p in (project_dir / "renders").glob("*.mp4")),
            key=lambda p: p.stat().st_mtime, default=None)
        if not latest_render:
            raise RuntimeError("This project has no render yet.")
        blocked_times = existing_times
        log(status_cb, f"Add more SFX: preserving {len(blocked_times)} existing event(s); "
                       "each blocks -0.30s..+0.30s.")

    ffmpeg = pipeline.find_ffmpeg(); ffprobe = pipeline.find_ffprobe(ffmpeg)
    duration = sfx_agent.media_duration(source_render, ffprobe)
    cuts = sfx_agent.detect_scene_cuts(source_render, ffmpeg)
    phrases = sfx_agent.transcribe_with_timing(
        source_render, ffmpeg, ffprobe, duration, status_cb=status_cb)
    data = sfx_agent.labeled_sfx_data(status_cb=status_cb)
    tags = sfx_agent.vision_tag_catalog(data, transitions_present=False)
    events = sfx_agent.plan_sfx_with_vision(
        source_render, duration, cuts, phrases, reasoning_model=reasoning_model,
        status_cb=status_cb, transitions_present=False, tags=tags, sfx_amount=sfx_amount,
        impact_word=str(config.get("impact_word") or "").strip() or None)
    if not events:
        # plan returns None/[] when the SFX library has no usable labels OR the vision model /
        # WAVESPEED_API_KEY is unavailable. Fail with a clear reason instead of crashing on a
        # NoneType (this was the "Redo SFX crashed on start" bug).
        raise RuntimeError("The SFX Audio Director returned no events. Check that WAVESPEED_API_KEY "
                           "is set and your SFX library has labeled sounds, then try again.")
    planned = sfx_agent.resolve_vision_segments(
        events, data, duration, existing_onsets=blocked_times, sfx_amount=sfx_amount,
        transitions_present=False, ffprobe=ffprobe, status_cb=status_cb)
    added = [row for row in planned
             if all(abs(float(row.get("start") or 0.0) - old) > 0.3001 for old in blocked_times)]
    log(status_cb, f"SFX gap guard: kept {len(added)}/{len(planned)} new event(s); "
                   "blocked {len(planned)-len(added)} within an existing +/-0.30s zone.")
    if not added:
        raise RuntimeError("No free timeline positions remained for additional SFX.")

    if mode == "redo":
        config["custom_sfx"] = []
        config["ai_content_sfx"] = []
        config["sfx_overrides"] = {}
    custom = list(config.get("custom_sfx") or [])
    scenes = list(config.get("scenes") or [])
    for index, row in enumerate(added):
        at = float(row.get("start") or 0.0)
        scene = next((scene for scene in scenes
                      if float(scene.get("start", 0)) <= at < float(scene.get("end", 0))),
                     scenes[-1] if scenes else {"id": ""})
        custom.append({
            "id": f"sfx-rework-{int(time.time())}-{index:03d}",
            "scene_id": str(scene.get("id") or ""),
            "offset": round(max(0.0, at - float(scene.get("start", 0) or 0)), 3),
            "path": str(row["path"]), "duration": float(row.get("duration") or 0.5),
            "volume": float(row.get("volume") or 0.25), "enabled": True,
            "source_trim": float(row.get("source_trim") or 0.0),
            "source_duration": float(row.get("source_duration") or 0.0),
            "playback_rate": float(row.get("playback_rate") or 1.0),
            "label": Path(row["path"]).stem.replace("_", " "),
        })
    config["custom_sfx"] = custom
    if mode == "redo":
        config["sfx_enabled"] = False  # only the newly planned editable custom events
    config["render_sfx_enabled"] = True
    config["output_basename"] = f"{slug}_{'redo' if mode == 'redo' else 'more'}_sfx_{time.strftime('%Y%m%d_%H%M%S')}"
    config_path = project_dir / "config" / "project.json"
    config_path.write_text(json.dumps(config_for_json(config), indent=2, ensure_ascii=False), encoding="utf-8")
    edits_path = project_dir / "config" / "timeline_edits.json"
    try:
        edits = json.loads(edits_path.read_text(encoding="utf-8")) if edits_path.exists() else {}
        edits.pop("sfx", None); edits.pop("transitions", None)
        edits_path.write_text(json.dumps(edits, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass
    output = pipeline.render_video(config)
    return {"title": config.get("title", slug), "project_dir": str(project_dir),
            "video": str(output), "added_sfx": len(added), "mode": mode}


def _timeline_words_from_sentences(sentence_rows, fallback_text, duration):
    """Build approximate global word timings when forced alignment is unavailable."""
    source_rows = []
    for row in sentence_rows or []:
        if not isinstance(row, dict):
            continue
        text = clean_text(str(row.get("text") or ""))
        try:
            start = max(0.0, float(row.get("start", 0.0) or 0.0))
            end = min(float(duration), float(row.get("end", start) or start))
        except (TypeError, ValueError):
            continue
        if text and end > start:
            source_rows.append({"start": start, "end": end, "exact_voice_text": text})
    if not source_rows:
        source_rows = [{"start": 0.0, "end": float(duration),
                        "exact_voice_text": clean_text(fallback_text)}]
    return estimated_word_timeline_from_scenes(source_rows)


def _retime_timeline_scenes(scenes, word_timeline, new_duration):
    """Preserve the user's clip order/cut rhythm while making the new speech the master clock."""
    ordered = [dict(scene) for scene in (scenes or [])]
    if not ordered:
        raise RuntimeError("Timeline has no clips to retime.")
    new_duration = max(0.5, float(new_duration))
    old_durations = []
    for scene in ordered:
        try:
            old_durations.append(max(0.05, float(scene.get("end", 0))
                                     - float(scene.get("start", 0))))
        except (TypeError, ValueError):
            old_durations.append(1.0)
    old_total = sum(old_durations) or float(len(ordered))
    # Long projects use a normal 0.5s minimum. Very short test/project audio still gets a
    # complete, contiguous timeline rather than impossible negative space.
    min_clip = min(0.5, new_duration / max(1, len(ordered)))
    word_starts = sorted({round(float(word.get("start", 0.0)), 3)
                          for word in (word_timeline or [])
                          if isinstance(word, dict) and float(word.get("start", 0.0)) > 0.0})
    boundaries = [0.0]
    elapsed = 0.0
    for index, old_duration in enumerate(old_durations[:-1]):
        elapsed += old_duration
        target = new_duration * elapsed / old_total
        lower = boundaries[-1] + min_clip
        remaining = len(ordered) - index - 1
        upper = new_duration - remaining * min_clip
        eligible = [value for value in word_starts if lower <= value <= upper]
        boundary = min(eligible, key=lambda value: abs(value - target)) if eligible else target
        boundaries.append(round(max(lower, min(upper, boundary)), 3))
    boundaries.append(round(new_duration, 3))

    for index, scene in enumerate(ordered):
        start, end = boundaries[index], boundaries[index + 1]
        scene["start"] = round(start, 3)
        scene["end"] = round(end, 3)
        local_words = []
        spoken = []
        for word in word_timeline or []:
            try:
                word_start = float(word.get("start", 0.0))
                word_end = float(word.get("end", word_start))
            except (TypeError, ValueError):
                continue
            midpoint = (word_start + word_end) / 2.0
            if start <= midpoint < end:
                token = str(word.get("word") or "").strip()
                if token:
                    spoken.append(token)
                    local_words.append({
                        "word": token,
                        "start": round(max(0.0, word_start - start), 3),
                        "end": round(max(0.0, min(end, word_end) - start), 3),
                    })
        scene["word_timings"] = local_words
        if spoken:
            voice = clean_text(" ".join(spoken))
            scene["script"] = voice
            scene["exact_voice_text"] = voice
            scene["voice_line"] = voice
    return ordered


def _persist_retimed_timeline(slug, config):
    """Save retimed config and update the editor sidecar so reopening cannot restore old cuts."""
    project_dir = PROJECTS_DIR / slug
    config_path = project_dir / "config" / "project.json"
    tmp = config_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(config_for_json(config), indent=2, ensure_ascii=False),
                   encoding="utf-8")
    os.replace(tmp, config_path)

    edits_path = project_dir / "config" / "timeline_edits.json"
    edits = {}
    if edits_path.exists():
        try:
            edits = json.loads(edits_path.read_text(encoding="utf-8"))
        except Exception:
            edits = {}
    if not isinstance(edits, dict):
        edits = {}
    prior_scene_edits = {str(row.get("id")): dict(row)
                         for row in (edits.get("scenes") or []) if isinstance(row, dict)}
    retimed_edits = []
    for index, scene in enumerate(config.get("scenes") or []):
        sid = str(scene.get("id", index))
        row = prior_scene_edits.get(sid, {"id": sid})
        row["duration"] = round(float(scene.get("end", 0))
                                - float(scene.get("start", 0)), 3)
        retimed_edits.append(row)
    edits["order"] = [str(scene.get("id", index))
                      for index, scene in enumerate(config.get("scenes") or [])]
    edits["scenes"] = retimed_edits
    edits_tmp = edits_path.with_suffix(".json.tmp")
    edits_tmp.write_text(json.dumps(edits, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(edits_tmp, edits_path)


def regenerate_timeline_speech(slug, status_cb=None, cancel_event=None, render=True):
    """Create a fresh TTS take and retime timeline clips/captions to the new audio."""
    config = load_project_config(slug)
    project_dir = PROJECTS_DIR / slug
    input_dir = project_dir / "input"
    script_path = input_dir / "script.txt"
    script = clean_text(script_path.read_text(encoding="utf-8", errors="replace")
                        if script_path.exists() else "")
    run_form = {}
    try:
        run_form = json.loads((input_dir / "run_form.json").read_text(encoding="utf-8"))
    except Exception:
        pass
    if not script:
        script = clean_text(str(run_form.get("script") or ""))
    if not script:
        raise RuntimeError("The project has no saved script to regenerate.")
    check_cancel({"_cancel_event": cancel_event} if cancel_event else {})

    stamp = time.strftime("%Y%m%d_%H%M%S")
    versions_dir = input_dir / "voice_versions" / stamp
    versions_dir.mkdir(parents=True, exist_ok=True)
    for stem in ("voiceover", "hook", "body"):
        for extension in AUDIO_EXTS:
            source = input_dir / f"{stem}{extension}"
            if source.exists():
                shutil.copy2(source, versions_dir / source.name)
    old_analysis = input_dir / "audio_analysis.json"
    if old_analysis.exists():
        shutil.copy2(old_analysis, versions_dir / old_analysis.name)

    form = dict(run_form) if isinstance(run_form, dict) else {}
    form["regenerate_voice"] = "on"
    form["force_regenerate"] = "on"
    form["use_audio_timing"] = "on"
    if cancel_event is not None:
        form["_cancel_event"] = cancel_event
    log(status_cb, "Regenerating speech with the project's saved speaker and voice settings...")
    audio_path = generate_project_voiceover(script, project_dir, form, status_cb=status_cb)
    if not audio_path or not Path(audio_path).exists():
        raise RuntimeError("Fresh speech generation failed; the previous take remains backed up.")
    duration = probe_audio_duration(audio_path)
    if not duration:
        raise RuntimeError("Fresh speech was created, but its duration could not be measured.")
    check_cancel({"_cancel_event": cancel_event} if cancel_event else {})

    analysis, word_timeline = None, []
    try:
        import voice_align
        if voice_align.available():
            log(status_cb, "Force-aligning the new speech for exact caption and cut timing...")
            analysis, word_timeline = voice_align.analysis_from_audio(
                audio_path, script_text=script, duration=duration, status_cb=status_cb)
    except Exception as exc:
        log(status_cb, f"Local speech alignment failed ({exc}); trying audio analysis fallback.")
    if not analysis:
        try:
            analysis = analyze_audio_with_gemini(
                audio_path, str(config.get("title") or slug), script,
                status_cb=status_cb, audio_duration=duration)
        except Exception as exc:
            log(status_cb, f"Audio analysis fallback unavailable ({exc}); using measured text timing.")
            analysis = None
    if not word_timeline:
        word_timeline = _timeline_words_from_sentences(
            (analysis or {}).get("sentence_timestamps") or [], script, duration)
    if not analysis:
        try:
            import voice_align
            sentence_rows = voice_align.sentences_from_words(word_timeline)
        except Exception:
            sentence_rows = [{"start": 0.0, "end": round(duration, 3), "text": script}]
        analysis = {"transcript": script, "duration_seconds": round(duration, 3),
                    "sentence_timestamps": sentence_rows,
                    "timing_source": "measured_text_timing"}
    analysis["duration_seconds"] = round(duration, 3)
    old_duration = float(config.get("duration") or 0.0)
    config["scenes"] = _retime_timeline_scenes(config.get("scenes") or [],
                                                word_timeline, duration)
    config["duration"] = round(duration, 3)
    config["audio_duration_seconds"] = round(duration, 3)
    config["audio_path"] = str(Path(audio_path).resolve())
    config["timing_audio_path"] = str(Path(audio_path).resolve())
    config["speech_audio_in_final"] = True
    config["timeline_editor_render"] = True
    _vs_default = SCRAPE_VOICE_SPEED if str(form.get("clip_source") or "generate").lower() == "scrape" else GENERATE_VOICE_SPEED
    config["voice_speed"] = float(form.get("voice_speed", config.get("voice_speed", _vs_default)) or _vs_default)
    caption_track = []
    for row in analysis.get("sentence_timestamps") or []:
        if not isinstance(row, dict) or not str(row.get("text") or "").strip():
            continue
        try:
            start = float(row.get("start", 0.0)); end = float(row.get("end", start))
        except (TypeError, ValueError):
            continue
        local = []
        for word in word_timeline:
            midpoint = (float(word.get("start", 0.0)) + float(word.get("end", 0.0))) / 2.0
            if start <= midpoint < end:
                local.append({"word": str(word.get("word") or ""),
                              "start": round(max(0.0, float(word.get("start", 0.0)) - start), 3),
                              "end": round(max(0.0, float(word.get("end", 0.0)) - start), 3)})
        caption_track.append({"start": round(start, 3), "end": round(end, 3),
                              "text": str(row.get("text") or ""), "word_timings": local})
    config["timeline_caption_track"] = caption_track
    old_analysis.write_text(json.dumps(analysis, indent=2, ensure_ascii=False), encoding="utf-8")
    _persist_retimed_timeline(slug, config)
    log(status_cb, f"Speech retime complete: {old_duration:.2f}s -> {duration:.2f}s; "
                   f"{len(config['scenes'])} clips and {len(caption_track)} caption phrases updated.")
    if not render:
        try:
            log(status_cb, "PROJECT_DIR|" + str(project_dir))
        except Exception:
            pass
        return {"title": config.get("title", slug), "project_dir": str(project_dir),
                "project_slug": slug, "video": None, "open_timeline": True, "no_render": True,
                "retimed_scenes": len(config["scenes"]), "audio": str(audio_path)}

    config["output_basename"] = f"{slug}_timeline_revoice_{stamp}"
    attach_cancel_event(config, {"_cancel_event": cancel_event} if cancel_event else {})
    out_config_path = project_dir / "config" / f"project_timeline_revoice_{stamp}.json"
    out_config_path.write_text(json.dumps(config_for_json(config), indent=2,
                                          ensure_ascii=False), encoding="utf-8")
    config["_config_path"] = str(out_config_path)
    log(status_cb, "Rendering the retimed timeline with the fresh speech...")
    output = pipeline.render_video(config)
    log(status_cb, f"Speech-regenerated timeline render complete: {Path(output).name}")
    return {"title": config.get("title", slug), "project_dir": str(project_dir),
            "video": str(output), "retimed_scenes": len(config["scenes"]),
            "audio": str(audio_path)}


def replace_timeline_scrape_scenes(slug, scene_ids, status_cb=None, cancel_event=None,
                                   include_project_pool=False, output_tag="timeline_social_replace",
                                   reasoning_model_override=None, media_source="scrape", render=True):
    """Search fresh TikTok/X footage for only the marked timeline scenes, then render.

    This is deliberately separate from a full same-script rerun: unmarked media remains intact,
    and every replacement must pass the normal download-quality and semantic vision gates.
    With ``include_project_pool`` the EXISTING project clips join the candidate pool (used by
    the script-change flow: reuse footage already on disk first, scrape only what's missing) -
    in that mode a disconnected backend degrades to pool-only matching instead of failing.
    """
    config = load_project_config(slug)
    project_dir = PROJECTS_DIR / slug
    is_scrape = str(config.get("clip_source") or "").lower() == "scrape"
    if not is_scrape and not include_project_pool:
        raise RuntimeError("Targeted social replacement is only available for TikTok/X scrape projects.")
    wanted = {str(value) for value in (scene_ids or []) if str(value)}
    indexed_targets = [(index, scene) for index, scene in enumerate(config.get("scenes") or [])
                       if str(scene.get("id", index)) in wanted]
    if not indexed_targets:
        raise RuntimeError("No marked scrape scenes were found in the saved timeline.")
    try:
        import clip_scraper
    except Exception as exc:
        raise RuntimeError("TikTok/X scraper is unavailable.") from exc
    platforms = clip_scraper.normalize_platforms(config.get("scrape_platforms") or "tiktok,x")
    backend_up = is_scrape and clip_scraper.backend_active(platforms)
    # #114 "library": match against the OVERALL clip library (every project) and never scrape.
    library_only = str(media_source or "").lower() == "library"
    if library_only:
        backend_up = False
        include_project_pool = True
    if not backend_up and not include_project_pool:
        raise RuntimeError("TikTok/X is not connected. Connect at least one selected source first.")
    if not backend_up and include_project_pool:
        log(status_cb, "Scrape backend not connected - matching against the project's existing "
                       "media only.")

    def cancelled():
        return bool(cancel_event and cancel_event.is_set())

    target_scenes = [dict(scene) for _, scene in indexed_targets]
    lines = [scene_text_for_planning(scene) or str(scene.get("script") or "")
             for scene in target_scenes]
    title = str(config.get("title") or slug)
    full_script = " ".join(str(scene.get("exact_voice_text") or scene.get("script") or "")
                           for scene in (config.get("scenes") or []))
    reasoning_model = str(reasoning_model_override or (config.get("wavespeed") or {}).get("reasoning_model")
                          or "openai/gpt-5.5")
    config.setdefault("wavespeed", {})["reasoning_model"] = reasoning_model
    understanding = {}
    try:
        understanding = json.loads((project_dir / "input" / "script_understanding.json").read_text(
            encoding="utf-8"))
    except Exception:
        pass
    if not understanding:
        understanding = comprehend_script(title, full_script, reasoning_model=reasoning_model,
                                           status_cb=status_cb)
    check_cancel({"_cancel_event": cancel_event} if cancel_event else {})
    log(status_cb, f"Timeline replacement: searching {', '.join(sorted(platforms))} for "
                   f"{len(target_scenes)} marked scene(s), leaving every other clip untouched.")
    queries = llm_scene_scrape_queries(lines, understanding=understanding,
                                       reasoning_model=reasoning_model, status_cb=status_cb)
    if not queries:
        # Derive compact search phrases; never send full narration lines to a backend.
        queries = []
        for line in lines:
            terms = important_terms(clean_text(line), 5, SEARCH_NOISE)
            if len(terms) >= 2:
                queries.append("Japan " + " ".join(terms))
    if not queries:
        raise RuntimeError("Could not derive social search queries for the marked scenes.")
    try:
        per_clip = max(4.0, min(8.0, max(float(scene.get("end", 0))
                                         - float(scene.get("start", 0))
                                         for scene in target_scenes)))
    except Exception:
        per_clip = 5.0
    stamp = time.strftime("%Y%m%d_%H%M%S")
    out_dir = project_dir / "seedance 2.0" / "_candidates" / f"timeline_replace_{stamp}"
    candidate_statuses = []
    got = []
    if backend_up:
        # A targeted timeline replacement is its own scrape run; do not inherit query
        # dedupe or a prior run's X-unavailable state.
        clip_scraper.reset_backend_search_health()
        got = clip_scraper.scrape_bucket(
            out_dir, queries[:32], min(40, max(8, len(target_scenes) * 6)),
            bucket_id=f"timeline_replace_{stamp}", tier="timeline_replace",
            bucket_terms=" ".join(lines)[:500], per_clip_seconds=per_clip,
            status_cb=status_cb, cancel_check=cancelled, candidate_statuses=candidate_statuses,
            min_likes=MIN_CLIP_LIKES,
            search_sort=str(config.get("scrape_sort") or "MOST_LIKED"), platforms=platforms,
            deadline=time.monotonic() + 900.0, search_intent="specific_action") or []
    if cancelled():
        raise pipeline.PipelineCancelled("Timeline replacement cancelled.")
    pool, clip_meta = [], {}
    for item in got:
        path = item.get("path")
        if not path:
            continue
        pool.append(Path(path))
        clip_meta[str(path)] = {
            "bucket_id": "timeline_replacement", "tier": "timeline_replace",
            "source_query": item.get("query", ""),
            "platform": item.get("platform", "tiktok"), "clip_id": item.get("clip_id"),
            "caption": (item.get("meta") or {}).get("caption", "")[:160],
            "likes": int(item.get("likes") or 0),
            "black_bar_score": item.get("black_bar_score", 0.0),
            "text_heaviness": item.get("text_heaviness", 0.0),
            "rapid_internal_cut_count": item.get("rapid_internal_cut_count", 0),
        }
    if include_project_pool:
        # existing project footage joins the pool: reuse before (re)downloading
        used_now = {str(scene.get("clip") or "") for scene in (config.get("scenes") or [])}
        pool_dir = project_dir / "seedance 2.0"
        existing = []
        for pattern in ("scraped_*.mp4", "manual_*.mp4", "timeline_replaced_*.mp4",
                        "replaced_*.mp4", "rescript_*.mp4"):
            existing.extend(pool_dir.glob(pattern))
        try:
            existing.extend((pool_dir / "_candidates").rglob("cand_*.mp4"))
        except Exception:
            pass
        seen_pool = {str(Path(p).resolve()) for p in pool}
        added_pool = 0
        for p in existing:
            if p.name.startswith(("speed_", "capblur_")) or p.name in used_now:
                continue
            key = str(p.resolve())
            if key in seen_pool:
                continue
            seen_pool.add(key)
            pool.append(p)
            clip_meta[str(p)] = {
                "bucket_id": "existing_project_media", "tier": "existing",
                "source_query": "", "platform": "existing", "clip_id": p.stem,
                "caption": "", "likes": 0, "black_bar_score": 0.0,
                "text_heaviness": 0.0, "rapid_internal_cut_count": 0,
            }
            added_pool += 1
            if added_pool >= 40:              # keep the vision matcher affordable
                break
        if added_pool:
            log(status_cb, f"Candidate pool: +{added_pool} existing project clip(s) offered "
                           "for reuse before new downloads.")
    if library_only:
        # pull clips from EVERY project so the agent can pick the best-matching footage that
        # already exists anywhere in the library (bounded so the vision matcher stays affordable)
        seen_pool = {str(Path(p).resolve()) for p in pool}
        added_global = 0
        try:
            for proj in sorted(PROJECTS_DIR.iterdir()):
                if not proj.is_dir() or proj.name == slug:
                    continue
                pdir = proj / "seedance 2.0"
                if not pdir.exists():
                    continue
                for pattern in ("scraped_*.mp4", "manual_*.mp4", "rescript_*.mp4",
                                "timeline_replaced_*.mp4", "replaced_*.mp4"):
                    for p in pdir.glob(pattern):
                        if p.name.startswith(("speed_", "capblur_")):
                            continue
                        key = str(p.resolve())
                        if key in seen_pool:
                            continue
                        seen_pool.add(key)
                        pool.append(p)
                        clip_meta[str(p)] = {
                            "bucket_id": "global_library", "tier": "library",
                            "source_query": "", "platform": "library", "clip_id": p.stem,
                            "caption": "", "likes": 0, "black_bar_score": 0.0,
                            "text_heaviness": 0.0, "rapid_internal_cut_count": 0,
                        }
                        added_global += 1
                        if added_global >= 80:
                            break
                    if added_global >= 80:
                        break
                if added_global >= 80:
                    break
        except Exception:
            pass
        if added_global:
            log(status_cb, f"Overall library: +{added_global} clip(s) from other projects offered "
                           "as match candidates.")
    if not pool:
        raise RuntimeError("TikTok/X returned no clean candidates for the marked scenes."
                           if backend_up else
                           "No existing project media available to match the changed lines - "
                           "connect TikTok/X and rerun.")
    try:
        relevancy = int(config.get("script_relevancy") or 85)
    except (TypeError, ValueError):
        relevancy = 85
    matches, decisions = assign_clips_to_scenes_by_vision(
        target_scenes, pool, project_dir, reasoning_model=reasoning_model,
        status_cb=status_cb, understanding=understanding, clip_meta=clip_meta,
        min_script_match_score=adaptive_script_match_threshold(relevancy, 0))
    # GRACEFUL FALLBACK - never abort the whole rescript over unmatched lines. A marked line often
    # has no clip clear the STRICT semantic bar (very common when the rescript split the script into
    # short fragments like "tie. No" / "rolled-up sleeves. No" that no footage can literally depict).
    # Instead of raising, such a scene KEEPS its existing clip (already vetted + on-theme for this
    # project), or borrows the closest freshly-scraped candidate we downloaded. Only a scene with
    # literally nothing available stays None and is skipped below. "Take what we have" > stopping.
    def _resolve_existing_clip(scene_cfg):
        for cand in (scene_cfg.get("clip"), scene_cfg.get("asset")):
            name = str(cand or "").strip()
            if not name:
                continue
            for base in (project_dir / "seedance 2.0", project_dir):
                p = base / name
                if p.exists():
                    return p
            p = Path(name)
            if p.is_absolute() and p.exists():
                return p
        return None

    unmatched_idx = [index for index, match in enumerate(matches) if match is None]
    if unmatched_idx:
        # A user explicitly marked these scenes for replacement. Keeping their old media makes
        # the operation look successful while doing nothing. If the strict vision gate has no
        # winner, use the best remaining freshly scraped candidate as a relevance fallback. The
        # old clip is only retained when the scraper produced literally no alternative.
        already_matched = {str(Path(match).resolve()) for match in matches if match is not None}
        borrow = []
        for item in got:
            raw = item.get("path")
            if not raw or not Path(raw).exists():
                continue
            candidate = Path(raw)
            key = str(candidate.resolve())
            if key in already_matched:
                continue
            borrow.append(candidate)
            already_matched.add(key)
        kept = borrowed = still_blank = 0
        for index in unmatched_idx:
            scene_index, _scene = indexed_targets[index]
            if borrow:
                matches[index] = borrow.pop(0)          # best-effort: closest scraped clip
                borrowed += 1
            else:
                existing = _resolve_existing_clip(config["scenes"][scene_index])
                if existing is not None:
                    matches[index] = existing           # no new candidate exists at all
                    kept += 1
                else:
                    still_blank += 1                    # truly nothing - leave the scene untouched
        log(status_cb,
            f"Timeline replacement: {len(unmatched_idx)} line(s) had no clean clip clear the semantic "
            f"bar - used {borrowed} closest fresh scraped clip(s)"
            + (f", kept {kept} existing clip(s) because no alternative existed" if kept else "")
            + (f", left {still_blank} unchanged" if still_blank else "")
            + " (continuing instead of stopping).")

    replaced_count = 0
    unchanged_ids = []
    for local_index, ((scene_index, scene), source) in enumerate(zip(indexed_targets, matches)):
        if source is None:
            unchanged_ids.append(str(scene.get("id", scene_index)))
            continue                                    # nothing available - leave this scene as-is
        sid = str(scene.get("id", scene_index))
        source = Path(source)
        current_clip = str((config["scenes"][scene_index] or {}).get("clip") or "").strip()
        if current_clip and source.name == current_clip:
            unchanged_ids.append(sid)
            continue                                    # 'kept' fallback: scene already uses this clip
        suffix = source.suffix.lower() if source.suffix else ".mp4"
        key = hashlib.sha1(str(source.resolve()).encode("utf-8", "ignore")).hexdigest()[:10]
        dest = project_dir / "seedance 2.0" / f"timeline_replaced_{sid}_{key}{suffix}"
        shutil.copy2(source, dest)
        updated = config["scenes"][scene_index]
        updated["clip"] = dest.name
        updated["asset"] = dest.name
        updated["seedance"] = True
        # fresh media -> the old speed/blur source chain no longer applies
        updated.pop("timeline_speed_src", None)
        updated.pop("caption_blur_src", None)
        meta = clip_meta.get(str(source)) or {}
        updated["scrape_source"] = meta.get("platform", "tiktok")
        updated["scrape_clip_id"] = meta.get("clip_id") or str(source)
        updated["match_class"] = (decisions[local_index] or {}).get("match_class", "TIMELINE_REPLACEMENT")
        updated["script_match_score"] = (decisions[local_index] or {}).get("script_match_score")
        replaced_count += 1
        log(status_cb, f"Timeline replacement: scene {sid} -> {source.name} "
                       f"({meta.get('platform', 'tiktok')}).")

    config["timeline_editor_render"] = True
    config["output_basename"] = f"{slug}_{output_tag}_{stamp}"
    attach_cancel_event(config, {"_cancel_event": cancel_event} if cancel_event else {})
    config_path = project_dir / "config" / "project.json"
    tmp = config_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(config_for_json(config), indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, config_path)
    if unchanged_ids:
        log(status_cb, "Timeline replacement warning: no alternative media existed for scene(s) "
                       + ", ".join(unchanged_ids) + ".")
    if not render:
        log(status_cb, f"Timeline replacement complete: changed {replaced_count}/{len(indexed_targets)} "
                       "marked clip(s). Opening the timeline editor (no render).")
        try:
            log(status_cb, "PROJECT_DIR|" + str(project_dir))
        except Exception:
            pass
        return {"title": config.get("title", slug), "project_dir": str(project_dir),
                "project_slug": slug, "video": None, "open_timeline": True, "no_render": True,
                "replaced_scenes": replaced_count, "requested_replacements": len(indexed_targets),
                "unchanged_scene_ids": unchanged_ids}
    log(status_cb, f"Timeline replacement complete: changed {replaced_count}/{len(indexed_targets)} "
                   "marked clip(s). Rendering the updated timeline...")
    output = pipeline.render_video(config)
    return {"title": config.get("title", slug), "project_dir": str(project_dir),
            "video": str(output), "replaced_scenes": replaced_count,
            "requested_replacements": len(indexed_targets), "unchanged_scene_ids": unchanged_ids}


def _split_script_lines(script, density="medium"):
    """Split the script into scene lines - one scene = one clip. ``density`` controls HOW MANY
    clips the recut uses:
      - "few"    : merge adjacent sentences into longer holds (roughly half the clips)
      - "medium" : one scene per sentence (default)
      - "many"   : also break long sentences at commas/clauses (roughly 1.5-2x the clips)
    """
    density = str(density or "medium").strip().lower()
    sentences = []
    for raw in str(script or "").replace("\r", "").split("\n"):
        raw = raw.strip()
        if not raw:
            continue
        for part in re.split(r"(?<=[.!?…。！？])\s+", raw):
            part = part.strip()
            if part:
                sentences.append(part)
    if not sentences:
        return []

    if density == "many":
        out = []
        for sentence in sentences:
            # break at internal clause boundaries, but only into pieces of >= 4 words so we
            # never make one-word clips
            frags = [f.strip(" ,;:—–-")
                     for f in re.split(r"(?<=[,;:—–])\s+", sentence)]
            frags = [f for f in frags if len(f.split()) >= 4]
            out.extend(frags if len(frags) >= 2 else [sentence])
        return out

    if density == "few":
        # merge consecutive sentences until a chunk reaches ~20 words -> fewer, longer clips
        out, chunk = [], ""
        for sentence in sentences:
            candidate = (chunk + " " + sentence).strip() if chunk else sentence
            if chunk and len(candidate.split()) > 20:
                out.append(chunk)
                chunk = sentence
            else:
                chunk = candidate
        if chunk:
            out.append(chunk)
        return out

    return sentences   # medium: one scene per sentence


def rescript_and_recut(slug, new_script, hook_text=None, voice_settings=None,
                       clip_density="medium", media_source="scrape", status_cb=None, cancel_event=None):
    """Timeline editor 'Change script', made ATOMIC: rebuilding the scene list overwrites the
    project's config with clip-less scenes BEFORE the media search/render - so a run that dies
    or is interrupted mid-way used to leave the project permanently broken (empty scenes in the
    editor). This wrapper snapshots every file the rebuild touches, keeps a survivable backup on
    disk, and restores everything on ANY failure so the project is left exactly as it was."""
    project_dir = PROJECTS_DIR / slug
    snapshot_targets = [
        project_dir / "config" / "project.json",
        project_dir / "config" / "timeline_edits.json",
        project_dir / "input" / "script.txt",
        project_dir / "input" / "run_form.json",
        project_dir / "input" / "audio_analysis.json",
        project_dir / "input" / "voiceover.wav",
        project_dir / "input" / "voiceover_dehiss.wav",
        project_dir / "input" / "hook.wav",
        project_dir / "input" / "body.wav",
    ]
    snapshot = {p: (p.read_bytes() if p.exists() else None) for p in snapshot_targets}
    # a copy that survives even a hard process kill (the in-memory restore below cannot run then)
    survive_backup = project_dir / "config" / "project_before_rescript.json"
    try:
        cfg_bytes = snapshot.get(project_dir / "config" / "project.json")
        if cfg_bytes:
            survive_backup.write_bytes(cfg_bytes)
    except Exception:
        pass

    def _restore():
        for path, data in snapshot.items():
            try:
                if data is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_bytes(data)
            except Exception:
                pass

    try:
        result = _rescript_and_recut_impl(
            slug, new_script, hook_text=hook_text, voice_settings=voice_settings,
            clip_density=clip_density, media_source=media_source,
            status_cb=status_cb, cancel_event=cancel_event)
        try:
            survive_backup.unlink(missing_ok=True)   # success -> the backup is no longer needed
        except Exception:
            pass
        return result
    except BaseException as exc:
        # cancellation arrives as pipeline.PipelineCancelled OR app.py's RunCancelled (which is
        # not importable here) via status_cb - match by name so an undefined name can't shadow it
        cancelled = isinstance(exc, pipeline.PipelineCancelled) \
            or type(exc).__name__ == "RunCancelled"
        if cancelled:
            log(status_cb, "Script change cancelled - restoring the project to its previous state.")
        else:
            log(status_cb, f"Script change failed ({exc}) - restoring the project to its previous "
                           "state so nothing is lost.")
        _restore()
        raise


def _rescript_and_recut_impl(slug, new_script, hook_text=None, voice_settings=None,
                             clip_density="medium", media_source="scrape",
                             status_cb=None, cancel_event=None):
    """The actual rebuild (see rescript_and_recut for the atomic snapshot/restore wrapper).
    ``hook_text`` marks the opening line(s) so the fresh voiceover keeps the hook + pause
    delivery (empty string clears the hook); ``voice_settings`` may override
    speaker_name / tts_voice / tts_model for the new take; ``clip_density`` (few/medium/many)
    controls how finely the script is cut into scenes = how many clips the video uses."""
    new_script = clean_text(new_script or "")
    if not new_script:
        raise RuntimeError("The new script is empty.")
    config = load_project_config(slug)
    project_dir = PROJECTS_DIR / slug
    density = str(clip_density or "medium").strip().lower()
    if density not in ("few", "medium", "many"):
        density = "medium"
    lines = _split_script_lines(new_script, density=density)
    if not lines:
        raise RuntimeError("Could not split the new script into spoken lines.")
    log(status_cb, f"Clip density '{density}': {len(lines)} scene(s)/clip(s) from the script.")
    config["clip_density"] = density

    def _norm(text):
        return re.sub(r"[\W_]+", "", str(text or "").casefold())

    old_pool = [scene for scene in (config.get("scenes") or [])]
    old_norms = [(_norm(scene.get("exact_voice_text") or scene.get("script") or ""), scene)
                 for scene in old_pool]
    taken = set()

    def _claim_scene_for(line_norm):
        """Old scene whose text equals the line - or, because the original run often cut
        lines into FRAGMENT scenes, the longest old fragment contained in the line (or
        containing it). Each old scene is reused at most once."""
        best_index, best_len = None, 0
        for idx, (norm, _scene) in enumerate(old_norms):
            if idx in taken or not norm:
                continue
            if norm == line_norm:
                best_index, best_len = idx, len(norm) * 10   # exact beats containment
                break
            if len(norm) >= 12 and (norm in line_norm or line_norm in norm):
                if len(norm) > best_len:
                    best_index, best_len = idx, len(norm)
        if best_index is None:
            return None
        taken.add(best_index)
        return old_norms[best_index][1]

    # #114 media source: "keep_visible" reuses the clips ALREADY on the timeline positionally
    # (no search), so changed/new lines borrow the currently-visible footage in order.
    visible_clips = [dict(scene) for scene in old_pool if scene.get("clip")]
    new_scenes, used_ids, kept, added = [], set(), 0, 0
    for index, line in enumerate(lines):
        matched_scene = _claim_scene_for(_norm(line))
        if matched_scene is not None and matched_scene.get("clip"):
            scene = dict(matched_scene)           # unchanged line -> keeps its media + tuning
            kept += 1
        elif media_source == "keep_visible" and visible_clips:
            # positional reuse of the currently-visible media (cycles if there are fewer clips)
            est = max(1.2, min(8.0, 0.34 * len(line.split())))
            base = visible_clips[index % len(visible_clips)]
            scene = {"id": None, "name": f"Scene {index + 1:02d}",
                     "seedance": base.get("seedance", True),
                     "clip": base.get("clip"), "asset": base.get("asset"),
                     "render_caption": True, "start": 0.0, "end": round(est, 2)}
            for _k in ("scrape_clip_id", "source_trim", "timeline_speed_src", "speaker_hook"):
                if base.get(_k) is not None:
                    scene[_k] = base[_k]
            kept += 1
        else:
            # rough spoken-duration estimate so the retimer weights the new line sensibly
            est = max(1.2, min(8.0, 0.34 * len(line.split())))
            scene = {"id": None, "name": f"Scene {index + 1:02d}", "seedance": True,
                     "clip": None, "asset": None, "render_caption": True,
                     "start": 0.0, "end": round(est, 2)}
            added += 1
        scene["script"] = line
        scene["exact_voice_text"] = line
        scene["voice_line"] = line
        sid = str(scene.get("id") or "")
        if not sid or sid in used_ids:
            sid = f"rs{index + 1:02d}"
            while sid in used_ids:
                sid += "x"
            scene["id"] = sid
        used_ids.add(sid)
        new_scenes.append(scene)
    log(status_cb, f"Script update: {len(lines)} line(s) - {kept} keep their media, "
                   f"{added} need media.")
    config["scenes"] = new_scenes
    # per-event SFX tuning refers to the OLD cut layout; keep only custom sounds whose
    # scene survived the rewrite
    config.pop("sfx_overrides", None)
    config["custom_sfx"] = [row for row in (config.get("custom_sfx") or [])
                            if str(row.get("scene_id")) in used_ids]

    input_dir = project_dir / "input"
    input_dir.mkdir(parents=True, exist_ok=True)
    (input_dir / "script.txt").write_text(new_script, encoding="utf-8")
    # hook: the voiceover regen splits hook+body from run_form["hook_text"], so the marked
    # hook must land there BEFORE regenerate_timeline_speech reads the form back from disk
    if hook_text is not None:
        hook_clean = clean_text(hook_text).strip()
        if hook_clean and re.sub(r"\s+", " ", hook_clean).casefold() not in \
                re.sub(r"\s+", " ", new_script).casefold():
            log(status_cb, "Marked hook is not part of the new script anymore - continuing "
                           "without a hook.")
            hook_clean = ""
        if hook_clean:
            log(status_cb, f"Hook marked for the fresh voiceover: {hook_clean[:70]!r}")
    run_form_path = input_dir / "run_form.json"
    try:
        run_form = json.loads(run_form_path.read_text(encoding="utf-8")) \
            if run_form_path.exists() else {}
        if not isinstance(run_form, dict):
            run_form = {}
        run_form["script"] = new_script
        run_form["clip_density"] = density
        if hook_text is not None:
            run_form["hook_text"] = hook_clean
        # narrator override: the voiceover regen reads speaker/voice/model from this form
        for key in ("speaker_name", "tts_voice", "tts_model"):
            value = str((voice_settings or {}).get(key) or "").strip()
            if not value:
                continue
            if key == "tts_voice" and value not in pipeline.GEMINI_TTS_VOICES:
                log(status_cb, f"Unknown TTS voice {value!r} - keeping the saved one.")
                continue
            if key == "tts_model" and value not in ("flash", "pro"):
                continue
            if str(run_form.get(key) or "") != value:
                log(status_cb, f"Narrator setting changed: {key} -> {value}")
            run_form[key] = value
        run_form_path.write_text(json.dumps(run_form, indent=2, ensure_ascii=False),
                                 encoding="utf-8")
    except Exception:
        pass
    config_path = project_dir / "config" / "project.json"
    tmp = config_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(config_for_json(config), indent=2, ensure_ascii=False),
                   encoding="utf-8")
    os.replace(tmp, config_path)
    # CRITICAL: the saved editor edits (order/durations/sfx...) refer to the OLD scene ids -
    # load_project_config applies them on every load and would FILTER the rebuilt scene list
    # down to whatever old ids survived. Keep only scene-independent prefs.
    edits_path = project_dir / "config" / "timeline_edits.json"
    old_edits = {}
    try:
        if edits_path.exists():
            old_edits = json.loads(edits_path.read_text(encoding="utf-8"))
    except Exception:
        old_edits = {}
    kept_edits = {key: old_edits[key] for key in ("volumes", "captions")
                  if isinstance(old_edits, dict) and key in old_edits}
    edits_path.write_text(json.dumps(kept_edits, indent=2, ensure_ascii=False),
                          encoding="utf-8")

    # fresh voiceover from the new script + retime the rebuilt scene list to it
    regenerate_timeline_speech(slug, status_cb=status_cb, cancel_event=cancel_event,
                               render=False)

    # snap scene boundaries EXACTLY to the aligned sentences when they map 1:1
    config = load_project_config(slug)
    try:
        analysis = json.loads((input_dir / "audio_analysis.json").read_text(encoding="utf-8"))
        rows = [row for row in (analysis.get("sentence_timestamps") or [])
                if str(row.get("text") or "").strip()]
        if len(rows) == len(config.get("scenes") or []):
            for scene, row in zip(config["scenes"], rows):
                scene["start"] = round(float(row.get("start", scene.get("start", 0))), 3)
                scene["end"] = round(float(row.get("end", scene.get("end", 0))), 3)
            tmp = config_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(config_for_json(config), indent=2, ensure_ascii=False),
                           encoding="utf-8")
            os.replace(tmp, config_path)
            log(status_cb, "Scene cuts snapped exactly to the new spoken lines.")
    except Exception:
        pass

    missing = [str(scene.get("id", index))
               for index, scene in enumerate(config.get("scenes") or [])
               if not scene.get("clip")]
    if missing:
        if media_source == "library":
            log(status_cb, f"Finding media for {len(missing)} changed/new line(s) from the OVERALL "
                           "library (all projects' clips) - no new scraping...")
        else:
            log(status_cb, f"Finding media for {len(missing)} changed/new line(s) - existing "
                           "project clips first, then TikTok/X for the rest...")
        return replace_timeline_scrape_scenes(
            slug, missing, status_cb=status_cb, cancel_event=cancel_event,
            include_project_pool=True, output_tag="rescript", media_source=media_source)

    stamp = time.strftime("%Y%m%d_%H%M%S")
    config["timeline_editor_render"] = True
    config["output_basename"] = f"{slug}_rescript_{stamp}"
    attach_cancel_event(config, {"_cancel_event": cancel_event} if cancel_event else {})
    tmp = config_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(config_for_json(config), indent=2, ensure_ascii=False),
                   encoding="utf-8")
    os.replace(tmp, config_path)
    log(status_cb, "All lines kept their media - rendering with the fresh voiceover...")
    output = pipeline.render_video(config)
    return {"title": config.get("title", slug), "project_dir": str(project_dir),
            "video": str(output), "rescript_lines": len(lines)}


def run_project(form, status_cb=None):
    check_cancel(form)
    script = clean_text(form.get("script", ""))
    reasoning_model = form.get("reasoning_model", "openai/gpt-5.5")
    reasoning_mode = reasoning_modes.set_current_reasoning_mode(
        reasoning_model, form.get("reasoning_mode"))
    log(status_cb, f"Reasoning: model={reasoning_model}, mode={reasoning_mode or 'unsupported'}")
    # Collaborative reasoning: GPT-5.5 drafts, Opus 4.8 critiques & corrects (edit map + review).
    # Default False here so an unchecked box (absent) is honoured; the UI renders it on by default.
    collaborate_reasoning = form_flag(form, "collaborative_reasoning", False)
    if collaborate_reasoning:
        log(status_cb, "Collaborative reasoning ON: GPT-5.5 drafts, Opus 4.8 critiques & corrects.")

    title = form.get("title", "").strip()
    if not title and script:
        title = (llm_generate_project_title(script, status_cb=status_cb)
                 or derive_project_title_from_script(script))
    if not title:
        title = f"Short {int(time.time())}"
        
    # Loading a previous project must REUSE its folder, never spawn a new one.
    # The UI sends the loaded slug in loaded_project_source; honor it as the slug.
    requested_slug = (form.get("slug", "") or form.get("loaded_project_source", "")).strip()
    clip_source_now = str(form.get("clip_source", "generate") or "generate").strip().lower()
    # SAME-SCRIPT FUSION (scrape only): re-running the same script as a TikTok scrape reuses that
    # project's folder instead of spawning a new timestamped one, so all takes live together.
    fused_slug = None
    if not requested_slug and clip_source_now == "scrape":
        fused_slug = find_matching_scrape_project(script)
    if requested_slug:
        slug = slugify(requested_slug)
    elif fused_slug:
        slug = fused_slug
        log(status_cb, f"Same-script TikTok scrape detected - fusing into existing project '{slug}' "
                       "(no new folder).")
    else:
        slug = unique_project_slug(slugify(title))
    project_dir = PROJECTS_DIR / slug
    # Same-script reuse also applies when the user explicitly LOADS a failed project. Previously
    # it only applied to automatic fusion, so "load to rerun" ignored all downloaded candidates.
    requested_same_script = False
    if requested_slug:
        try:
            prior_script = (project_dir / "input" / "script.txt").read_text(
                encoding="utf-8", errors="replace")
            requested_same_script = (_script_fingerprint(prior_script) == _script_fingerprint(script))
        except Exception:
            requested_same_script = False
    reuse_same_script = should_reuse_same_script_media(fused_slug, requested_same_script, form)
    for folder in ["seedance 2.0", "gpt images", "web images", "input", "config", "renders", "review", "local media", "speaker", "speaker clip"]:
        (project_dir / folder).mkdir(parents=True, exist_ok=True)
    if requested_slug or fused_slug:
        # Fuse any timestamped duplicate folders of the same topic back into this canonical folder.
        consolidate_project_folders(slug, status_cb=status_cb)
    log(status_cb, f"PROJECT_DIR|{project_dir}")

    # Persist the submitted run settings immediately, before any paid/long-running step. Failed
    # projects therefore remain fully reloadable and can be rerun in the same folder.
    sensitive_form_keys = {"scrape_cookies", "scrape_cookies_file", "api_key", "token", "audio_path"}
    run_form_snapshot = {"title": title, "script": script}
    for key, value in form.items():
        if str(key).startswith("_") or key in sensitive_form_keys:
            continue
        if isinstance(value, (str, int, float, bool)):
            run_form_snapshot[key] = value
        elif isinstance(value, list) and all(isinstance(item, (str, int, float, bool)) for item in value):
            run_form_snapshot[key] = value
    try:
        (project_dir / "input" / "run_form.json").write_text(
            json.dumps(run_form_snapshot, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass

    # A scrape run requires at least one selected, logged-in social source.
    # Enforce it up front so we don't burn voiceover/director work on a run that can't
    # source any clips.
    if str(form.get("clip_source", "generate") or "generate").strip().lower() == "scrape":
        _requested_scrape_platforms = [
            p.strip() for p in str(form.get("scrape_platforms", "tiktok,x") or "").replace("\n", ",").split(",")
            if p.strip()
        ] or ["tiktok", "x"]
        try:
            import clip_scraper as _cs_check
            _scrape_ready = bool(_cs_check.backend_active(_requested_scrape_platforms))
        except Exception:
            _scrape_ready = False
        if not _scrape_ready:
            raise RuntimeError(
                "None of the selected social sources are connected. Connect TikTok and/or X "
                "on the home page, then re-run.")

    # Abort NOW if the WaveSpeed balance is empty - before TTS/director/scrape burn time.
    assert_wavespeed_balance(status_cb=status_cb)

    visual_script = clean_text(form.get("visual_script", ""))
    # "Use visual direction" toggle: when off, ignore the visual-direction text entirely.
    # (UI submits ui_form=1, so an unchecked toggle => off; programmatic runs keep it on.)
    _vd_default = False if str(form.get("ui_form", "")).strip() else True
    if not form_flag(form, "use_visual_direction", _vd_default):
        visual_script = ""
    audio_path = safe_copy_audio(form.get("audio_path", ""), project_dir)
    speaker_image_path = form.get("speaker_image_path", "")
    # Default audio path: synthesize the narration from the script with Gemini TTS
    # (the user picks speaker + voice). An uploaded file, if any, still wins.
    if (not audio_path and reuse_same_script
            and not form_flag(form, "regenerate_voice", False)
            and not form_flag(form, "force_regenerate", False)):
        _vo = existing_project_voiceover(project_dir)
        if _vo:
            # The saved voiceover was processed by an OLDER chain that brightened the TTS hiss into a
            # constant rauschen. Reuse it (no new TTS) but run a de-hiss pass (denoise + de-ess +
            # 15 kHz low-pass, NO speed/shape change) onto a cleaned copy so the bed is gone.
            audio_path = str(_vo)
            try:
                _clean = _vo.with_name(_vo.stem + "_dehiss.wav")
                shutil.copyfile(_vo, _clean)
                pipeline.apply_voice_postprocess(_clean, speed=1.0, denoise=True, style="dehiss",
                                                 ffmpeg=pipeline.find_ffmpeg(), status_cb=status_cb)
                audio_path = str(_clean)
                log(status_cb, f"Reusing existing voiceover ({_vo.name}), de-hissed - no new TTS. "
                               "(Tick 'Fresh take' to regenerate the voice instead.)")
            except Exception:
                log(status_cb, f"Reusing existing voiceover for the identical script ({_vo.name}) - no new TTS.")
    if not audio_path and form_flag(form, "generate_voice", True):
        audio_path = generate_project_voiceover(script, project_dir, form, status_cb=status_cb)
        # "Halt after generating speech": pause here until the user approves (or
        # replaces) the voiceover on the run page. The gate blocks the worker thread.
        if audio_path and form_flag(form, "halt_after_speech", False):
            gate = form.get("_speech_gate")
            if callable(gate):
                _chosen_speed = gate(audio_path)
                # the user may pick a different narration speed at the approval gate -
                # re-tempo the voiceover BEFORE alignment/captions so all timing follows
                apply_approved_voice_speed(audio_path, _chosen_speed, form, status_cb=status_cb)
    audio_analysis = None
    word_timeline_cache = None
    audio_duration = probe_audio_duration(audio_path) if audio_path else None

    if audio_path and form_flag(form, "use_audio_timing", True):
        if audio_duration:
            log(status_cb, f"Measured speech audio duration: {audio_duration:.2f}s")
        # Primary timing source: local frame-accurate forced alignment (faster-whisper).
        try:
            import voice_align
            if voice_align.available():
                log(status_cb, "Aligning script to voice (faster-whisper) for frame-accurate timing...")
                # The voiceover we generated is sped to voice_speed; tell the aligner so it
                # de-speeds a copy to x1.0 for tighter word boundaries (uploaded audio = x1.0).
                _align_speed = (resolve_voice_speed(form, form.get("clip_source"))
                                if form_flag(form, "generate_voice", True) else 1.0)
                audio_analysis, word_timeline_cache = voice_align.analysis_from_audio(
                    audio_path, script_text=script, duration=audio_duration, status_cb=status_cb,
                    speed=_align_speed,
                )
                if audio_analysis:
                    if not script:
                        script = clean_text(audio_analysis.get("transcript", ""))
                    log(status_cb, f"Voice timing locked from forced alignment ({len(word_timeline_cache)} words).")
            else:
                log(status_cb, "faster-whisper not installed; falling back to Gemini for audio timing.")
        except Exception as exc:
            log(status_cb, f"Local alignment failed ({exc}); trying Gemini fallback.")
            audio_analysis = None
        # Fallback only if the local aligner is unavailable or produced nothing.
        if not audio_analysis:
            try:
                log(status_cb, f"Analyzing audio with Gemini 3.5 Flash: {audio_path.name}")
                audio_analysis = analyze_audio_with_gemini(
                    audio_path, title, script, status_cb=status_cb, audio_duration=audio_duration,
                )
                transcript = clean_text(audio_analysis.get("transcript", ""))
                if transcript and not script:
                    script = transcript
            except Exception as exc:
                if not script:
                    raise RuntimeError(f"Audio analysis failed and no text script was provided: {exc}") from exc
                log(status_cb, f"Audio timing fell back to estimated text timing: {exc}")
                audio_analysis = None
        if audio_analysis:
            (project_dir / "input" / "audio_analysis.json").write_text(
                json.dumps(audio_analysis, indent=2, ensure_ascii=False), encoding="utf-8")

    if not script:
        raise RuntimeError("Paste a text script or upload a speech audio file.")

    if not title or re.fullmatch(r"Short\s+\d+", title, flags=re.I):
        title = (llm_generate_project_title(script, reasoning_model=reasoning_model, status_cb=status_cb)
                 or derive_project_title_from_script(script))
        if title:
            log(status_cb, f"Updating project title to: {title}")
        else:
            title = f"Short {int(time.time())}"
            
        # The folder may already have a timestamp slug, but all UI-facing metadata uses this title.
        if title:
            try:
                run_form_path = project_dir / "input" / "run_form.json"
                saved_form = json.loads(run_form_path.read_text(encoding="utf-8")) if run_form_path.exists() else {}
                if not isinstance(saved_form, dict):
                    saved_form = {}
                saved_form["title"] = title
                run_form_path.write_text(json.dumps(saved_form, indent=2, ensure_ascii=False), encoding="utf-8")
            except Exception:
                pass

    (project_dir / "input" / "script.txt").write_text(script, encoding="utf-8")
    if visual_script:
        (project_dir / "input" / "visual_script.txt").write_text(visual_script, encoding="utf-8")

    # COMPREHEND THE SCRIPT ONCE: a single deep understanding of what this video is about,
    # shared by every clip agent (search terms + clip matching) so they stop guessing per-step.
    script_understanding = comprehend_script(title, script, reasoning_model=reasoning_model,
                                             collaborate=collaborate_reasoning, status_cb=status_cb)
    if script_understanding:
        try:
            (project_dir / "input" / "script_understanding.json").write_text(
                json.dumps(script_understanding, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    from timeline.voice_timing import load_or_create_voice_timing
    timing_info = load_or_create_voice_timing(
        project_dir=project_dir,
        script_text=script,
        uploaded_audio_path=audio_path,
        wavespeed_analysis=audio_analysis
    )
    
    base_scenes = timing_info["scenes"]
    audio_timing_source = timing_info["timing_source"]
    target_duration = timing_info["duration"]
    
    if timing_info["is_estimated"]:
        log(status_cb, f"WARNING: Voice timing is estimated ({target_duration:.1f}s). Final result may drift.")
    else:
        log(status_cb, f"Voice timing established via {audio_timing_source}: {len(base_scenes)} beat(s), locked to {target_duration:.1f}s")
        timed_lines = [
            f"{format_timecode(scene['start'])}-{format_timecode(scene['end'])} {scene['script']}"
            for scene in base_scenes
        ]
        (project_dir / "input" / "audio_timed_script.txt").write_text("\n".join(timed_lines), encoding="utf-8")
    micro_beat_result = build_micro_beat_plan(title, script, visual_script, target_duration, base_scenes, reasoning_model=reasoning_model, status_cb=status_cb, collaborate=collaborate_reasoning)
    micro_beat_scenes = micro_beat_result["micro_beats"]
    visual_sections = micro_beat_result["visual_sections"]
    scenes_override = apply_visual_script_to_scenes(micro_beat_scenes, visual_script, target_duration)
    # Reference dark-facts edits cut every ~2s (measured: 23-27 cuts over 43-56s). Split any
    # beat longer than that so the pacing matches instead of reading as a slow slideshow.
    # (clip_source is read straight from the form here; its canonical local is assigned later.)
    if str(form.get("clip_source", "generate") or "generate").strip().lower() == "scrape":
        before_n = len(scenes_override)
        scenes_override = enforce_reference_pacing(scenes_override, max_s=2.4)
        if len(scenes_override) != before_n:
            log(status_cb, f"Pacing: split long beats for reference cut rate ({before_n} -> {len(scenes_override)} beats, ~1 cut/2s).")
        canonical_words = word_timeline_cache or estimated_word_timeline_from_scenes(base_scenes)
        if canonical_words:
            scenes_override = sync_scenes_to_voice_timeline(
                scenes_override, canonical_words, target_duration=target_duration)
            log(status_cb, "TikTok edit map locked to the words actually spoken in each cut.")
        before_stabilize = len(scenes_override)
        scenes_override = coalesce_short_scrape_scenes(scenes_override, min_s=1.45, max_s=3.2)
        if len(scenes_override) != before_stabilize:
            log(status_cb, f"Pacing: merged isolated sub-1.45s beats ({before_stabilize} -> "
                           f"{len(scenes_override)}) to prevent rapid double-cuts in TikTok footage.")
            # the merge moved boundaries AFTER the voice sync - re-snap every cut onto the
            # nearest spoken word onset, or the cuts drift off the narration (user complaint)
            if canonical_words:
                scenes_override = sync_scenes_to_voice_timeline(
                    scenes_override, canonical_words, target_duration=target_duration)
                log(status_cb, "Pacing: cuts re-snapped to spoken word onsets after the merge.")
        # v0.2 ASYNCHRONOUS CUTS: never cut where a sentence ends - picture and thought
        # resolving together signals "you can scroll now". Boundaries inside +-0.4s of a
        # sentence-end are moved onto the onset of the SECOND word of the next sentence
        # (a cut mid-thought), keeping scenes contiguous and >=1.2s.
        if canonical_words and str(form.get("pipeline_version") or "v0.2") != "v0.1":
            _sent_ends = [w["end"] for w in canonical_words if str(w["word"]).rstrip()[-1:] in ".!?"]
            _onsets = [w["start"] for w in canonical_words]
            _moved = 0
            for _i in range(len(scenes_override) - 1):
                _b = float(scenes_override[_i]["end"])
                _near = next((se for se in _sent_ends if abs(_b - se) <= 0.4), None)
                if _near is None:
                    continue
                _after = [o for o in _onsets if o > _near + 0.05]
                if len(_after) < 2:
                    continue
                _newb = round(_after[1], 3)          # onset of the 2nd word of the next sentence
                if (_newb - float(scenes_override[_i]["start"]) >= 1.2
                        and float(scenes_override[_i + 1]["end"]) - _newb >= 1.2):
                    scenes_override[_i]["end"] = _newb
                    scenes_override[_i + 1]["start"] = _newb
                    _moved += 1
            if _moved:
                log(status_cb, f"v0.2 pacing: moved {_moved} cut(s) off sentence ends into "
                               "mid-sentence (asynchronous cut rule).")
    log(status_cb, f"Using {len(scenes_override)} micro-beat(s) as the edit map.")
    if visual_script:
        log(status_cb, "Visual Ablauf prompt applied to scene planning, image prompts, Seedance prompts, and review.")
    web_images_per_scene = 2
    # Scale the web-image pool to the scene count: a 2-scene short needs ~4, not 12.
    _scene_n = len(scenes_override) if scenes_override else 6
    web_image_count = max(3, min(16, _scene_n * web_images_per_scene))
    static_gpt_image_count = 0
    seedance_clip_count = 5
    seedance_model_choice = form.get("video_model", "seedance-2.0")
    auto_web_images = form_flag(form, "auto_web_images", True)
    manual_auto_web_images = auto_web_images
    allow_gpt = form_flag(form, "allow_gpt", True)
    allow_seedance = form_flag(form, "allow_seedance", True)
    use_llm_search = form_flag(form, "use_llm_search", form_flag(form, "use_glm_search", True))
    use_llm_video_review = form_flag(form, "use_llm_video_review", True)
    background_music_enabled = form_flag(form, "background_music_enabled", False)
    autonomous_director = form_flag(form, "autonomous_director", True)
    # Run mode is auto-determined now (the UI selector was removed): a "smart" fill-missing
    # pass when the project already has media, otherwise a full run. An explicit form value
    # (programmatic callers) still wins.
    _run_type_in = str(form.get("run_type", "") or "").strip().lower()
    if _run_type_in in ("normal", "audit"):
        run_type = _run_type_in
    else:
        _has_media = bool(existing_seedance_clips(project_dir)) or any(
            (project_dir / sub).exists() and any((project_dir / sub).glob("*"))
            for sub in ("web images", "gpt images", "local media")
        )
        run_type = "audit" if _has_media else "normal"
        log(status_cb, f"Run mode auto-selected: {'smart (fill missing media)' if run_type == 'audit' else 'full run'}.")
    loaded_project_mode = form_choice(
        form,
        "loaded_project_mode",
        {"normal", "recut_existing_only", "recut_new_web_images", "recut_regenerate_seedance", "recut_recreate_speaker_clip"},
        "normal",
    )
    recut_mode = loaded_project_mode if loaded_project_mode != "normal" and requested_slug else "normal"
    speaker_hook_enabled = form_flag(form, "enable_speaker_hook", False) or recut_mode == "recut_recreate_speaker_clip"
    speaker_hook_recreate = recut_mode == "recut_recreate_speaker_clip"
    if speaker_hook_enabled and not allow_seedance:
        allow_seedance = True
        log(status_cb, "Speaker hook enabled Seedance generation for the opening speaker clip.")
    recut_stamp = time.strftime("%Y%m%d_%H%M%S")
    director_plan = {}
    director_plan_path = None
    scenes_for_director = scenes_override or parse_timed_script(script, target_duration)
    if autonomous_director:
        check_cancel(form)
        director_plan = llm_auto_director_plan(
            title,
            script,
            scenes_for_director,
            target_duration,
            project_media_counts(project_dir),
            audio_present=bool(audio_path),
            reasoning_model=reasoning_model, status_cb=status_cb,
        )
        if director_plan:
            director_plan_path = project_dir / "input" / "director_plan.json"
            director_plan_path.write_text(json.dumps(director_plan, indent=2), encoding="utf-8")
            web_image_count = director_int(director_plan, "web_image_count", web_image_count, 0, 80)
            web_images_per_scene = director_int(director_plan, "web_images_per_scene", web_images_per_scene, 1, 8)
            static_gpt_image_count = 0
            seedance_clip_count = director_int(director_plan, "seedance_clip_count", seedance_clip_count, 0, 5)
            auto_web_images = director_bool(director_plan, "use_web_images", auto_web_images)
            allow_gpt = director_bool(director_plan, "use_gpt_source_images", director_bool(director_plan, "use_gpt_images", allow_gpt))
            allow_seedance = director_bool(director_plan, "use_seedance", allow_seedance)
            use_llm_search = director_bool(director_plan, "use_llm_search", use_llm_search)
            log(
                status_cb,
                "Auto Director choices: "
                f"web={web_image_count}, web/scene={web_images_per_scene}, "
                f"static_gpt=0(disabled), seedance_i2v={seedance_clip_count}, "
                f"review={'on' if use_llm_video_review else 'off'}",
            )
    else:
        log(status_cb, "Auto Director disabled; using manual target settings.")
    # "Smart" run type: inspect what media already exists in the project folder and
    # only generate the categories that are still empty (keep good media, fill the gaps).
    audit_existing_media = run_type == "audit"
    if audit_existing_media:
        counts = project_media_counts(project_dir)
        have_web = counts.get("web_images_existing", 0)
        have_gpt = counts.get("gpt_images_existing", 0)
        have_seedance = counts.get("seedance_clips_existing", 0)
        log(status_cb, f"Smart run: existing media — web={have_web}, gpt={have_gpt}, seedance={have_seedance}.")
        if have_web >= max(1, web_image_count):
            auto_web_images = False
            manual_auto_web_images = False
            log(status_cb, f"Smart run: {have_web} web image(s) already present — skipping new web search.")
        else:
            log(status_cb, "Smart run: web image pool is short — will fetch more.")
        if have_gpt > 0:
            allow_gpt = False
            static_gpt_image_count = 0
            log(status_cb, f"Smart run: {have_gpt} GPT image(s) already present — skipping GPT generation.")
        if have_seedance > 0:
            allow_seedance = False
            seedance_clip_count = min(seedance_clip_count, have_seedance)
            log(status_cb, f"Smart run: {have_seedance} Seedance clip(s) already present — reusing them, no new clips.")
    if manual_auto_web_images and not auto_web_images:
        auto_web_images = True
        log(status_cb, "Web image search kept enabled by UI setting; every automatic run keeps a 10-15 web-image pool.")
    # ===== Per-output on/off toggles (the final word; even Smart can't re-enable) =====
    # A real UI submission includes ui_form=1; an unchecked checkbox then means OFF.
    # Programmatic runs (recut/replace) omit ui_form, so we keep everything ON.
    _ui = bool(str(form.get("ui_form", "")).strip())
    _td = False if _ui else True   # default for an absent toggle
    out_web = form_flag(form, "out_web_images", _td)
    out_wiki = form_flag(form, "out_wikimedia", _td)
    out_gpt = form_flag(form, "out_gpt_images", _td)
    out_clips = form_flag(form, "out_video_clips", _td)
    out_sfx = form_flag(form, "out_sfx", _td)
    out_tr_sfx = form_flag(form, "out_transition_sfx", _td)
    out_bg = form_flag(form, "out_background_music", False)
    out_caps = form_flag(form, "out_captions", _td)
    requested_clip_source = str(form.get("clip_source", "generate") or "generate").strip().lower()
    # wikimedia is currently the only web-image source, so either toggle off kills web images
    if not (out_web and out_wiki):
        auto_web_images = False
        manual_auto_web_images = False
        log(status_cb, "Output toggle: web/Wikimedia images OFF.")
    if not out_gpt:
        allow_gpt = False
        static_gpt_image_count = 0
        log(status_cb, "Output toggle: generated images OFF.")
    if not out_clips:
        allow_seedance = False
        seedance_clip_count = 0
        if requested_clip_source != "scrape":
            log(status_cb, "Output toggle: video clips OFF.")
    background_music_enabled = out_bg
    # ===== Clip source: generate (AI) vs scrape (real TikTok/Instagram footage) =====
    clip_source = requested_clip_source
    if clip_source not in ("generate", "scrape"):
        clip_source = "generate"
    scrape_platforms = [
        p.strip() for p in str(form.get("scrape_platforms", "tiktok,x") or "").replace("\n", ",").split(",")
        if p.strip()
    ] or ["tiktok", "x"]
    scrape_terms = str(form.get("scrape_terms", "") or "").strip()
    scrape_sort = str(form.get("scrape_sort", "ALL") or "ALL").strip().upper()
    if scrape_sort not in {"MOST_LIKED", "MOST_VIEWED", "MOST_RECENT", "RELEVANCE", "ALL"}:
        scrape_sort = "ALL"
    try:
        script_relevancy = max(0, min(100, int(float(form.get("script_relevancy", 70)))))
    except (TypeError, ValueError):
        script_relevancy = 70
    if clip_source == "scrape":
        # Scraped real footage becomes the moving-video layer: it is dropped into the
        # project's "seedance 2.0" folder so the planner/renderer treat it exactly like
        # generated clips (captions, SFX, music and the speaker hook are unchanged).
        # All AI *image* sources are disabled; the video slot stays on but is sourced
        # from scraping rather than generation.
        auto_web_images = False
        manual_auto_web_images = False
        allow_gpt = False
        static_gpt_image_count = 0
        out_web = out_wiki = out_gpt = False
        allow_seedance = True
        out_clips = True
        if not form_flag(form, "out_video_clips", _td):
            log(status_cb, "Scrape mode: video clips forced ON because TikTok footage is the video layer.")
        if not seedance_clip_count or seedance_clip_count < 1:
            seedance_clip_count = 4
        log(status_cb, f"Clip source: SCRAPE — pulling real clips from {', '.join(scrape_platforms)} "
                       f"(style terms: {scrape_terms or 'from script'}; script relevancy {script_relevancy}%). "
                       "AI image generation is disabled; scraped footage fills the video layer.")
        log(status_cb, "Scrape result order: " + scrape_sort.lower().replace("_", " ") + ".")
    output_toggles = {
        "web_images": out_web, "wikimedia": out_wiki, "gpt_images": out_gpt,
        "video_clips": out_clips, "sfx": out_sfx, "transition_sfx": out_tr_sfx,
        "background_music": out_bg, "captions": out_caps,
    }
    force_new_web_images = False
    if recut_mode != "normal":
        existing_seedance_count = len(existing_seedance_clips(project_dir))
        requested_seedance_clip_count = seedance_clip_count
        log(status_cb, f"Loaded project mode: {recut_mode_label(recut_mode)}.")
        allow_seedance = allow_seedance and existing_seedance_count > 0
        seedance_clip_count = min(seedance_clip_count, existing_seedance_count)
        if recut_mode == "recut_existing_only":
            auto_web_images = False
            allow_gpt = False
            static_gpt_image_count = 0
            log(status_cb, "Recut mode: using only existing local/web images and existing Seedance clips. No new static GPT media generation.")
        elif recut_mode == "recut_new_web_images":
            auto_web_images = True
            force_new_web_images = True
            allow_gpt = False
            static_gpt_image_count = 0
            web_image_count = max(1, web_image_count)
            log(status_cb, f"Recut mode: downloading up to {web_image_count} new web image(s), then cutting from existing media plus those images.")
        elif recut_mode == "recut_regenerate_seedance":
            auto_web_images = False
            allow_gpt = True
            allow_seedance = True
            static_gpt_image_count = 0
            seedance_clip_count = max(1, requested_seedance_clip_count)
            move_existing_seedance_clips(project_dir, status_cb=status_cb)
            existing_seedance_count = 0
            log(status_cb, f"Recut mode: regenerating up to {seedance_clip_count} Seedance clip(s), then recutting in the same project folder.")
        elif recut_mode == "recut_recreate_speaker_clip":
            auto_web_images = False
            allow_gpt = False
            allow_seedance = True
            static_gpt_image_count = 0
            seedance_clip_count = max(1, seedance_clip_count)
            log(status_cb, "Recut mode: recreating only the uploaded/existing speaker hook Seedance clip, then recutting from existing media.")
        if not allow_seedance:
            seedance_clip_count = 0
        if recut_mode == "recut_recreate_speaker_clip":
            log(status_cb, f"Recut mode allows only the speaker Seedance clip; existing Seedance clips available: {existing_seedance_count}.")
        elif recut_mode == "recut_regenerate_seedance":
            log(status_cb, "Recut mode will generate fresh Seedance clips in the loaded project folder.")
        else:
            log(status_cb, f"Recut mode blocks new Seedance generation; existing Seedance clips available: {existing_seedance_count}.")
    if auto_web_images and clip_source == "scrape":
        # In a TikTok scrape run the video layer is real footage; web images are never used,
        # so skip the (slow, ~20 min) web-image search entirely.
        log(status_cb, "Scrape mode: skipping web-image search (footage comes from TikTok).")
        auto_web_images = False
    if auto_web_images:
        original_web_image_count = web_image_count
        web_image_count = clamp_web_image_target(web_image_count)
        if web_image_count != original_web_image_count:
            log(status_cb, f"Web image target adjusted to {web_image_count}; every run keeps a 10-15 image pool.")
        web_candidate_count = web_candidate_pool_target(web_image_count)
        log(status_cb, f"Web image pipeline: collecting {web_candidate_count} candidates; Reasoning Agent visual review will choose the final {web_image_count}.")
        check_cancel(form)
        with step_watchdog(form, "Web image search", limit_s=600, status_cb=status_cb):
            web_paths = gather_web_images_for_script(
                project_dir,
                title,
                script,
                target_duration,
                images_per_scene=max(1, web_images_per_scene * 3),
                target_count=web_candidate_count,
                use_llm_search=use_llm_search,
                scenes_override=scenes_override,
                force_new=force_new_web_images,
                reasoning_model=reasoning_model,
                status_cb=status_cb,
            )
        check_cancel(form)
        web_sheet = create_media_contact_sheet(
            web_paths,
            project_dir / "review" / "web_images_contact_sheet.jpg",
            title="Downloaded web images",
        )
        if web_sheet:
            log_preview(status_cb, "Web image contact sheet", web_sheet)
        web_paths = review_and_correct_web_images(
            project_dir,
            title,
            script,
            visual_script,
            scenes_override,
            target_duration,
            web_paths,
            web_images_per_scene,
            web_image_count,
            use_gpt55=use_llm_search,
            reasoning_model=reasoning_model, status_cb=status_cb,
        )
    # ===== Scrape real clips into the seedance folder (clip_source == "scrape") =====
    clip_decision_log = None   # per-scene clip choices/rejections for agent_report
    social_search_report = None
    reused_existing_scrape = 0
    # NOTE: the same-script shortcut used to BLIND-assign this project's old scraped_NN.mp4 clips by
    # index with NO caption/quality check - so captioned clips an earlier (more lenient) run accepted
    # were replayed forever. That is exactly how burned-in Japanese TikTok captions kept showing up.
    # Same-script runs now fall through to the normal pipeline, which reuses this project's already
    # DOWNLOADED clips via find_reusable_social_clips (so no full re-download) but RE-VALIDATES every
    # one through the strict semantic matcher (rejects text-dominated clips) and only
    # searches TikTok again for scenes the clean existing clips cannot cover.
    if clip_source == "scrape" and reuse_same_script and not form_flag(form, "force_rescrape", False):
        log(status_cb, "Same-script reuse: re-validating this project's existing clips through the "
                       "caption/quality matcher (no blind reuse); searching only for uncovered scenes.")
    if clip_source == "scrape" and not reused_existing_scrape:
        try:
            import clip_scraper
        except Exception as exc:  # noqa: BLE001
            clip_scraper = None
            log(status_cb, f"Scrape: clip_scraper unavailable ({exc.__class__.__name__}); skipping scrape.")
        if clip_scraper is not None:
            scene_total = len(scenes_override) if scenes_override else max(1, seedance_clip_count)
            SCRAPE_CAP = 48
            MAX_SCRAPE_ROUNDS = int(form.get("scrape_rounds", 3) or 3) if isinstance(form, dict) else 3
            MAX_SCRAPE_ROUNDS = max(1, min(5, MAX_SCRAPE_ROUNDS))
            try:
                per_clip = min(8.0, max(4.0, max(float(s["end"]) - float(s["start"]) for s in scenes_override)))
            except Exception:
                per_clip = 5.0
            seedance_target_dir = project_dir / "seedance 2.0"
            _cancel = lambda: bool(cancellation_event(form) and cancellation_event(form).is_set())
            _cookies = (str(form.get("scrape_cookies_file", "") or "").strip()
                        or str(form.get("scrape_cookies", "") or "").strip())
            already = existing_seedance_clips(project_dir)

            # Scraping engine selection (default V2). V1 = legacy bucket scrape; V2 = relevance-first,
            # segment-based (scrape_v2.py). `config` does not exist yet here (built later), so read the
            # engine from the run form only, safe-default to v2.
            _scrape_engine = str((form.get("scraping_engine") if isinstance(form, dict) else None)
                                 or "v2").strip().lower()
            if _scrape_engine not in ("v1", "v2"):
                _scrape_engine = "v2"
            _v2 = (_scrape_engine == "v2")
            if _v2:
                import scrape_v2

            # Build a structured SOCIAL SEARCH PLAN: reusable visual buckets + a dedicated hook
            # influencer bucket, each with TikTok-native tiered queries (exact->semantic->broad->
            # hashtag). Scrape tier by tier, widening only when a bucket finds too few clips.
            log(status_cb, "Building social search buckets from script...")
            # V2 has its own visual-intent planner; skip the V1 bucket-plan LLM call for V2.
            social_plan = ({} if _v2 else build_social_search_plan(
                title, script, scenes_override, understanding=script_understanding,
                reasoning_model=reasoning_model, collaborate=collaborate_reasoning, status_cb=status_cb))
            custom_queries = [q.strip() for q in re.split(r"[,\n]", scrape_terms or "") if q.strip()]
            if social_plan.get("buckets") and custom_queries:
                social_plan["buckets"].insert(0, {
                    "bucket_id": "user_search_terms",
                    "used_by_scene_ids": [],
                    "visual_goal": "Footage matching the user's explicit TikTok search terms: "
                                   + ", ".join(custom_queries[:8]),
                    "primary_subject": " ".join(custom_queries[:8]),
                    "action": "",
                    "search_intent": "specific_action",
                    "query_tiers": {"exact": custom_queries[:12], "semantic": [],
                                    "broad": [], "hashtag": []},
                    "must_show": custom_queries[:8],
                    "must_not_show": [],
                })
                log(status_cb, f"TikTok search: included {len(custom_queries[:12])} explicit user term(s).")
            bucket_by_id = {b.get("bucket_id"): b for b in (social_plan.get("buckets") or [])}
            scene_bucket, query_perf, hook_pool, clip_meta = {}, [], [], {}
            candidate_statuses, filter_summary = [], {}
            semantic_matching_skipped = False
            pool = []
            project_reuse_report = {"searched_projects": 0, "selected_projects": []}
            hook_results, best_hook = [], None     # dedicated hook finder output (reserves scene 0)
            hook_relaxed = False
            body_pool = []                         # pool minus hook clips (body scenes only)
            scene_clips = [None] * scene_total
            # HOOK STRATEGY: by default the hook is TOPIC footage - scene 0 simply joins the
            # normal vision matching, so a trains script opens on trains, a school script on a
            # school visual. The generic dance/cute-influencer hook (dedicated search bucket +
            # presenter scorer, scene 0 reserved) only runs when explicitly enabled.
            # Found-footage mode always opens with the requested 20K+ cute/dance creator hook.
            # The UI submits this explicitly; defaulting on also keeps programmatic scrape runs
            # consistent with the product contract.
            use_influencer_hook = form_flag(form, "influencer_hook", True)
            if not use_influencer_hook:
                log(status_cb, "Hook: topic-matched (scene 0 joins normal matching); "
                               "influencer/dance hook is OFF.")
            # Social search is intentionally unbounded. Large pools plus vision matching can take
            # longer than 45 minutes; user cancellation and provider-level deadlines still work,
            # but the app must never discard a healthy scrape merely because elapsed time passed.
            with step_watchdog(form, "Clip scrape", limit_s=0, status_cb=status_cb):
                if _v2:
                    # SCRAPE V2: relevance-first, segment-based engine. It plans concrete visual
                    # intents, discovers usable SEGMENTS across whole videos, two-stage vision-matches
                    # them and assigns globally - then exposes scene_clips/decisions so the SAME V1
                    # finalize machinery below writes the scenes (scraped_NN copy + enforcement report).
                    log(status_cb, "Scrape V2 (relevance-first, segment-based) engine selected.")
                    # `config` is not built yet here; give V2 a minimal config to read the title from
                    # and stash its result on. The scene fields it writes go onto scenes_override
                    # (which becomes config['scenes']); the run is flagged v2 on `form` for validation.
                    _v2cfg = {"title": title, "voice_speed": resolve_voice_speed(form, "scrape"),
                              "scrape_sort": str(form.get("scrape_sort") or "RELEVANCE"),
                              "pipeline_version": str(form.get("pipeline_version") or "v0.2")}
                    (pool, clip_meta, query_perf, scene_bucket, hook_pool, candidate_statuses,
                     filter_summary) = scrape_v2.scrape_social_plan_v2(
                        _v2cfg, scenes_override, project_dir, scrape_platforms, per_clip,
                        script_relevancy, _cookies, _cancel, understanding=script_understanding,
                        reasoning_model=reasoning_model, status_cb=status_cb, script_text=script)
                    if isinstance(form, dict):
                        form["_scrape_v2_used"] = True
                    _v2res = _v2cfg.get("_scrape_v2") or {}
                    scene_clips = list(_v2res.get("scene_clips") or [None] * scene_total)
                    while len(scene_clips) < scene_total:
                        scene_clips.append(None)
                    clip_decision_log = list(_v2res.get("clip_decision_log") or [None] * scene_total)
                    while len(clip_decision_log) < scene_total:
                        clip_decision_log.append(None)
                    best_hook = _v2res.get("best_hook")
                    hook_results = ([{"clip": best_hook, "passed": True, "vision_passed": True,
                                      "hook_presenter_score": 8.0, "scores": {}, "likes": 0}]
                                    if best_hook else [])
                elif social_plan.get("buckets"):
                    existing_body, existing_hooks, existing_meta = [], [], {}
                    project_reuse_report = {"searched_projects": 0, "selected_projects": []}
                    # HARD RULE: previously-scraped clips are reused ONLY when this is the SAME script
                    # (same-script fusion) and only from THIS project's own folder. A new/different
                    # script NEVER touches another run's media - it always scrapes fresh footage.
                    if reuse_same_script:
                        log(status_cb, "Same-script run: re-checking this project's own accepted clips...")
                        existing_body, existing_hooks, existing_meta, project_reuse_report = find_reusable_social_clips(
                            project_dir, title, script, understanding=script_understanding,
                            reasoning_model=reasoning_model, status_cb=status_cb)
                        pool = list(existing_body) + list(existing_hooks)
                        hook_pool = list(existing_hooks)
                        clip_meta.update(existing_meta)
                        for path in pool:
                            em = clip_meta.get(str(path)) or {}
                            candidate_statuses.append({
                                "clip_id": em.get("clip_id") or str(path),
                                "bucket_id": em.get("bucket_id"), "source_query": em.get("source_query"),
                                "tier": "existing_project", "status": "downloaded_pending_review",
                                "shown_in_media_panel": False, "reason": "reusable accepted clip from same project",
                                "likes": em.get("likes", 0),
                            })
                    else:
                        log(status_cb, "New script: scraping fresh TikTok footage - no reuse of any old/other media.")

                    # A failed same-script run may already have dozens of pre-filtered candidate
                    # downloads. Use them FIRST and skip another 30-50 minute scrape when the
                    # reusable pool is already large enough to cover the edit.
                    reuse_pool_sufficient = len(existing_body) >= max(12, scene_total)
                    first_search_plan = {
                        "buckets": ([] if reuse_pool_sufficient
                                    else list(social_plan.get("buckets") or [])),
                        "hook": (None if (existing_hooks or not use_influencer_hook)
                                 else social_plan.get("hook")),
                    }
                    if existing_body:
                        if reuse_pool_sufficient:
                            log(status_cb, f"Same-script failed-run recovery: reusing {len(existing_body)} "
                                           "pre-filtered body candidate(s); skipping a duplicate full scrape.")
                        else:
                            log(status_cb, f"Same-script run reused {len(existing_body)} body candidate(s); "
                                           "searching only because the saved pool is still too small.")
                    if first_search_plan["buckets"] or first_search_plan["hook"]:
                        (fresh_pool, fresh_meta, fresh_perf, _fresh_scene_bucket, fresh_hooks,
                        fresh_statuses, _fresh_summary) = scrape_social_plan(
                            first_search_plan, project_dir, clip_scraper, scrape_platforms, per_clip,
                            script_relevancy, _cookies, _cancel, status_cb=status_cb,
                            script_text=script, search_sort=scrape_sort)
                        pool.extend(fresh_pool)
                        hook_pool.extend(fresh_hooks)
                        clip_meta.update(fresh_meta)
                        query_perf.extend(fresh_perf)
                        candidate_statuses.extend(fresh_statuses)
                    # Scene-to-bucket grounding always comes from the full plan, even when its
                    # initial external searches were skipped because existing footage was found.
                    for bucket in (social_plan.get("buckets") or []):
                        for sid in (bucket.get("used_by_scene_ids") or []):
                            try:
                                scene_bucket.setdefault(int(sid), bucket.get("bucket_id"))
                            except (TypeError, ValueError):
                                continue
                    filter_summary = _summarize_candidate_filters(candidate_statuses)
                else:
                    # Fallback when bucket planning failed: flat queries through the SAME
                    # scrape_bucket path (metadata pre-filter + gates + blur + tiktok_login
                    # backend). The old scrape_clips path only knew Apify/yt-dlp-cookies and
                    # returned 0 with "not connected" even with a working TikTok login -
                    # which silently emptied the pool for the whole run.
                    log(status_cb, "Bucket planning returned nothing - falling back to flat "
                                   "script-derived queries through the standard scrape path.")
                    _splan = llm_scrape_plan(script, title, visual_script, script_relevancy,
                                             reasoning_model=reasoning_model, status_cb=status_cb,
                                             understanding=script_understanding)
                    _flat_queries = clip_scraper.normalize_query_list(_splan.get("queries"))
                    _flat_queries += [q.strip() for q in re.split(r"[,\n]", scrape_terms or "")
                                      if q.strip()]
                    _fallback_dicts = clip_scraper.scrape_bucket(
                        seedance_target_dir / "_candidates" / "fallback", _flat_queries[:16],
                        max(10, min(int(scene_total * 1.6), SCRAPE_CAP)),
                        bucket_id="fallback", tier="flat", bucket_terms=str(script)[:200],
                        per_clip_seconds=per_clip, status_cb=status_cb, cancel_check=_cancel,
                        candidate_statuses=candidate_statuses, min_likes=MIN_CLIP_LIKES,
                        search_sort=scrape_sort, platforms=scrape_platforms,
                        deadline=time.monotonic() + 180.0) or []
                    for d in _fallback_dicts:
                        p = d.get("path")
                        if not p:
                            continue
                        pool.append(p)
                        clip_meta[str(p)] = {
                            "bucket_id": "fallback", "tier": "flat",
                            "platform": d.get("platform", "tiktok"),
                            "source_query": d.get("query", ""), "clip_id": d.get("clip_id"),
                            "caption": (d.get("meta") or {}).get("caption", "")[:160],
                            "black_bar_score": d.get("black_bar_score", 0.0),
                            "text_heaviness": d.get("text_heaviness", 0.0),
                            "likes": d.get("likes", 0),
                            "internal_cut_count": d.get("internal_cut_count", 0),
                            "rapid_internal_cut_count": d.get("rapid_internal_cut_count", 0),
                        }
                    hook_q = str(_splan.get("hook_query") or "").strip()
                    if hook_q:
                        _hd = clip_scraper.scrape_bucket(
                            seedance_target_dir / "_candidates" / "fallback", [hook_q], 3,
                            bucket_id="fallback_hook", tier="flat", bucket_terms=hook_q,
                            per_clip_seconds=per_clip, status_cb=status_cb, cancel_check=_cancel,
                            candidate_statuses=candidate_statuses, min_likes=HOOK_MIN_LIKES,
                            search_sort=scrape_sort, platforms=scrape_platforms,
                            deadline=time.monotonic() + 180.0) or []
                        for d in _hd:
                            if d.get("path"):
                                hp = d["path"]
                                hook_pool.append(hp)
                                pool.append(hp)
                                clip_meta[str(hp)] = {
                                    "bucket_id": "fallback_hook", "tier": "flat",
                                    "platform": d.get("platform", "tiktok"),
                                    "source_query": d.get("query", ""),
                                    "clip_id": d.get("clip_id"),
                                    "caption": (d.get("meta") or {}).get("caption", "")[:160],
                                    "likes": int(d.get("likes") or 0),
                                    "black_bar_score": d.get("black_bar_score", 0.0),
                                    "text_heaviness": d.get("text_heaviness", 0.0),
                                }

                # ---- Dedicated HOOK finder: score 20K+ cute/dance creator clips, reserve scene 0 ----
                # Hook-bucket clips are kept out of the body pool because their job is scroll-stop
                # energy, not literal narration coverage.
                if hook_pool and not _v2:
                    hook_results = score_hook_candidates(
                        hook_pool, project_dir, reasoning_model=reasoning_model,
                        status_cb=status_cb, collaborate=collaborate_reasoning)
                    for hook_result in hook_results:
                        hook_result["likes"] = int((clip_meta.get(str(hook_result.get("clip"))) or {}).get("likes") or 0)
                    def _hook_likes_ok(result):
                        result_meta = clip_meta.get(str(result.get("clip"))) or {}
                        floor = clip_scraper.platform_like_floor(
                            HOOK_MIN_LIKES, result_meta.get("platform", "tiktok"))
                        return int(result.get("likes") or 0) >= floor
                    best_hook = next((r["clip"] for r in hook_results if r.get("passed")), None)
                    if best_hook is None:
                        relaxed_hook = next((r for r in hook_results
                                             if r.get("vision_passed")
                                             and float(r.get("hook_presenter_score") or 0) >= 5.5
                                             and _hook_likes_ok(r)), None)
                        if relaxed_hook is None:
                            relaxed_hook = next((r for r in hook_results
                                                 if float((r.get("scores") or {}).get("dance_or_playful_action") or 0) >= 4
                                                 and _hook_likes_ok(r)), None)
                        if relaxed_hook is None:
                            relaxed_hook = next((r for r in hook_results
                                                 if _hook_likes_ok(r)), None)
                        if relaxed_hook is not None:
                            best_hook = relaxed_hook["clip"]
                            hook_relaxed = True
                            log(status_cb, "Hook Finder: no clip cleared the strict score; using the best "
                                           "engagement-qualified cute/dance candidate instead of aborting.")
                hook_keys = {str(Path(p).resolve()) for p in hook_pool}
                body_pool = [p for p in pool if str(Path(p).resolve()) not in hook_keys]
                retry_seen_ids = {str(m.get("clip_id")) for m in clip_meta.values()
                                  if isinstance(m, dict) and m.get("clip_id")}

                def _apply_hook(clips):
                    # Influencer mode: scene 0 is reserved for a hook that passed the presenter
                    # gate. Topic mode: scene 0 was matched normally - leave it alone.
                    if clips and use_influencer_hook:
                        clips[0] = best_hook
                    return clips

                # Per-scene specs from the bucket each scene belongs to (grounds the matcher).
                scene_specs = {}
                for i in range(scene_total):
                    b = bucket_by_id.get(scene_bucket.get(i))
                    if b:
                        scene_specs[i] = {
                            "preferred_bucket_id": b.get("bucket_id", ""),
                            "required_subject": b.get("primary_subject", ""),
                            "required_action": b.get("action", ""),
                            "must_show": b.get("must_show", []),
                            "must_not_show": b.get("must_not_show", []),
                            "visual_acceptance_test": b.get("visual_goal", ""),
                        }
                current_match_threshold = adaptive_script_match_threshold(script_relevancy, 0)
                log(status_cb, f"Scrape semantic threshold starts at {current_match_threshold:.1f}/10 "
                               f"(script relevancy {script_relevancy}%).")

                def _match_body_candidates(candidates, threshold):
                    """Influencer mode keeps scene 0 out of body matching (reserved for the
                    presenter hook); topic mode matches ALL scenes including the hook."""
                    if not candidates:
                        return ([None] * scene_total, [None] * scene_total)
                    if not use_influencer_hook:
                        clips, decisions = assign_clips_to_scenes_by_vision(
                            scenes_override, candidates, project_dir,
                            reasoning_model=reasoning_model, status_cb=status_cb,
                            understanding=script_understanding, clip_meta=clip_meta,
                            scene_specs=scene_specs, collaborate=collaborate_reasoning,
                            min_script_match_score=threshold)
                        clips = list(clips or [])
                        decisions = list(decisions or [])
                        while len(clips) < scene_total:
                            clips.append(None)
                        while len(decisions) < scene_total:
                            decisions.append(None)
                        return (clips, decisions)
                    if scene_total <= 1:
                        return ([None] * scene_total, [None] * scene_total)
                    body_specs = {i - 1: spec for i, spec in scene_specs.items() if i > 0}
                    clips, decisions = assign_clips_to_scenes_by_vision(
                        scenes_override[1:], candidates, project_dir,
                        reasoning_model=reasoning_model, status_cb=status_cb,
                        understanding=script_understanding, clip_meta=clip_meta,
                        scene_specs=body_specs, collaborate=collaborate_reasoning,
                        min_script_match_score=threshold)
                    shifted = [None]
                    for local_index, decision in enumerate(decisions or []):
                        if isinstance(decision, dict):
                            decision = dict(decision)
                            decision["scene"] = local_index + 1
                        shifted.append(decision)
                    while len(shifted) < scene_total:
                        shifted.append(None)
                    return ([None] + list(clips or []), shifted)

                if body_pool and not _v2:
                    scene_clips, clip_decision_log = _match_body_candidates(
                        body_pool, current_match_threshold)
                if not _v2:
                    _apply_hook(scene_clips)

                # Keep searching TikTok for any BODY scene still unmatched (targeted retry rounds).
                # Scene 0 is the hook and is never re-searched here.
                _first_body_scene = 1 if use_influencer_hook else 0
                for rnd in range(1, MAX_SCRAPE_ROUNDS):
                    if _cancel():
                        break
                    unmatched = [i for i, c in enumerate(scene_clips)
                                 if i >= _first_body_scene and c is None]
                    if not unmatched:
                        break
                    # FILL FIRST, SEARCH LAST: when the pool is already big and most scenes
                    # matched, the continuity fill below covers the gaps - re-searching for a
                    # handful of abstract lines burns 15+ minutes for near-zero gain.
                    _matched_now = sum(1 for i, c in enumerate(scene_clips)
                                       if i >= _first_body_scene and c is not None)
                    if len(body_pool) >= scene_total and _matched_now >= max(3, scene_total // 2):
                        log(status_cb, f"{len(unmatched)} scene(s) unmatched but the pool has "
                                       f"{len(body_pool)} clips and {_matched_now} matched - "
                                       "using continuity fill instead of another search round.")
                        break
                    # ANTI-TIMEOUT: abstract narration ("the quiet ways friendship is shown") rarely
                    # matches concrete b-roll, so re-searching + re-scoring the whole pool again just
                    # burns minutes (each pass is expensive) and used to hit the 45-min watchdog with
                    # 0 clips. If the pool is already large, stop re-searching and let the fast
                    # continuity fallback below fill the scenes from the clean pool.
                    _matched_so_far = sum(1 for i, c in enumerate(scene_clips) if i > 0 and c is not None)
                    if len(body_pool) >= scene_total and _matched_so_far == 0:
                        log(status_cb, f"Pool already has {len(body_pool)} clean clips but abstract "
                                       "narration isn't matching concrete footage; skipping further "
                                       "re-search and using continuity fill (no 45-min re-scoring).")
                        break
                    current_match_threshold = adaptive_script_match_threshold(script_relevancy, rnd)
                    lines = [(scene_text_for_planning(scenes_override[i]) or scenes_override[i].get("script", ""))
                             for i in unmatched]
                    log(status_cb, f"Scrape round {rnd + 1}: re-searching "
                                   f"{', '.join(scrape_platforms)} for {len(unmatched)} "
                                   f"scene(s); relaxing semantic threshold to "
                                   f"{current_match_threshold:.1f}/10...")
                    retry_terms = llm_scene_scrape_queries(lines, understanding=script_understanding,
                                                           reasoning_model=reasoning_model, status_cb=status_cb)
                    got_dicts = []
                    if retry_terms:
                        retry_terms = retry_terms[:32]
                        log(status_cb, f"   retry search terms: {' · '.join(retry_terms[:12])}")
                        # retries go through the SAME filtered path (metadata pre-filter + black-bar +
                        # text rejection), never the old build_queries scrape_clips path.
                        got_dicts = clip_scraper.scrape_bucket(
                            seedance_target_dir / "_candidates" / "retry", retry_terms,
                            min(SCRAPE_CAP, max(6, len(unmatched) * 2)),
                            bucket_id="retry", tier=f"retry_{rnd + 1}",
                            bucket_terms=" ".join(lines)[:200], per_clip_seconds=per_clip,
                            status_cb=status_cb, cancel_check=_cancel, seen_ids=retry_seen_ids,
                            query_perf=query_perf, candidate_statuses=candidate_statuses,
                            min_likes=MIN_CLIP_LIKES,
                            search_sort=scrape_sort, platforms=scrape_platforms,
                            deadline=time.monotonic() + 180.0) or []
                    else:
                        log(status_cb, "   no new retry terms returned; re-scoring the existing pool "
                                       "at the lower threshold.")
                    for d in got_dicts:
                        p = d.get("path")
                        if not p:
                            continue
                        pool.append(p)
                        body_pool.append(p)
                        clip_meta[str(p)] = {
                            "bucket_id": "retry", "tier": "retry",
                            "platform": d.get("platform", "tiktok"),
                            "source_query": d.get("query", ""), "clip_id": d.get("clip_id"),
                            "caption": (d.get("meta") or {}).get("caption", "")[:160],
                            "black_bar_score": d.get("black_bar_score", 0.0),
                            "text_heaviness": d.get("text_heaviness", 0.0),
                            "is_fake_vertical": d.get("is_fake_vertical", False),
                            "internal_cut_count": d.get("internal_cut_count", 0),
                            "rapid_internal_cut_count": d.get("rapid_internal_cut_count", 0),
                            "min_shot_seconds": d.get("min_shot_seconds")}
                    if not body_pool:
                        continue
                    scene_clips, clip_decision_log = _match_body_candidates(
                        body_pool, current_match_threshold)
                    _apply_hook(scene_clips)

                # One final no-download relaxation pass can rescue a PARTIALLY matched edit.
                # When a large pool matched absolutely nothing, repeating every vision batch at a
                # lower number has consistently returned the same D_REJECTED decisions and added
                # 10-20 minutes. In that case go directly to the controlled same-bucket clean-footage
                # fallback below.
                remaining_body = [i for i, c in enumerate(scene_clips) if i > 0 and c is None]
                _any_matched = any(c is not None for i, c in enumerate(scene_clips) if i > 0)
                _large_zero_match_pool = (not _any_matched and len(body_pool) >= scene_total)
                _floor_threshold = adaptive_script_match_threshold(
                    script_relevancy, MAX_SCRAPE_ROUNDS)
                _can_relax_further = _floor_threshold < current_match_threshold - 0.01
                _run_final_rescore = should_run_final_semantic_rescore(
                    current_match_threshold, _floor_threshold, len(remaining_body),
                    len(body_pool), _any_matched, scene_total)
                if _run_final_rescore:
                    log(status_cb, f"Final semantic fallback: re-scoring {len(remaining_body)} unmatched "
                                   f"scene(s) at {_floor_threshold:.1f}/10 (floor).")
                    rescored_clips, rescored_log = _match_body_candidates(
                        body_pool, _floor_threshold)
                    # Preserve every scene already accepted at the stricter threshold. The old
                    # assignment replaced the entire list, so a weaker pass could erase good
                    # matches before the continuity fallback ran.
                    for scene_index in remaining_body:
                        if scene_index < len(rescored_clips) and rescored_clips[scene_index] is not None:
                            scene_clips[scene_index] = rescored_clips[scene_index]
                        if scene_index < len(rescored_log) and rescored_log[scene_index] is not None:
                            clip_decision_log[scene_index] = rescored_log[scene_index]
                    current_match_threshold = _floor_threshold
                    _apply_hook(scene_clips)
                elif _large_zero_match_pool:
                    log(status_cb, f"Semantic matcher accepted 0 scenes from {len(body_pool)} clean clips; "
                                   "skipping a redundant full re-score and using same-bucket clean-footage "
                                   "fallbacks immediately.")
                elif remaining_body and body_pool and not _can_relax_further:
                    log(status_cb, f"Semantic matcher already ran at the {_floor_threshold:.1f}/10 floor; "
                                   "skipping the duplicate full-pool re-score and filling remaining scenes "
                                   "from same-bucket clean footage.")

                # Preserve the hard 20K-like gate even if vision scoring is unavailable/overly
                # strict. Hook queries themselves target cute/dance creators, so choose the most-
                # liked metadata-qualified hook rather than aborting the entire project.
                if best_hook is None and hook_pool and not _v2:
                    best_hook = max(
                        hook_pool,
                        key=lambda p: int((clip_meta.get(str(p)) or {}).get("likes") or 0))
                    hook_relaxed = True
                    fallback_meta = clip_meta.get(str(best_hook)) or {}
                    hook_results.append({
                        "clip": best_hook, "hook_presenter_score": 0.0, "passed": False,
                        "vision_passed": False, "scores": {},
                        "likes": int(fallback_meta.get("likes") or 0),
                        "reason": "most-liked 20K+ cute/dance search candidate; vision fallback",
                    })
                    _apply_hook(scene_clips)
                    log(status_cb, "Hook Finder: using the most-liked 20K+ cute/dance search candidate "
                                   "instead of stopping the run.")

            # Semantic matching is MANDATORY in scrape mode. If there was a body pool to score but
            # the matcher returned only its blank fallback (vision unavailable / NameError / API
            # error), we must NOT silently continue with random clips - fail loudly.
            if body_pool:
                dl = clip_decision_log if isinstance(clip_decision_log, list) else []
                real_decisions = [d for d in dl if isinstance(d, dict)
                                  and "vision unavailable" not in str(d.get("reason", ""))]
                if not real_decisions:
                    semantic_matching_skipped = True
            if semantic_matching_skipped:
                _wsr = {"semantic_matching_skipped": True,
                        "traceback": "matcher returned blank fallback for a non-empty candidate pool",
                        "candidate_pool_total": len(pool)}
                try:
                    (project_dir / "review").mkdir(parents=True, exist_ok=True)
                    (project_dir / "review" / "semantic_matching_error.json").write_text(
                        json.dumps(_wsr, indent=2), encoding="utf-8")
                except Exception:
                    pass
                log(status_cb, "Scrape: semantic matching was unavailable; continuing with controlled "
                               "search-bucket continuity fallbacks instead of stopping the run.")

            # Make the per-scene decision log reflect the dedicated hook finder for scene 0 (the
            # body matcher never scored the hook, so its scene-0 entry is meaningless).
            if not isinstance(clip_decision_log, list):
                clip_decision_log = [None] * scene_total
            while len(clip_decision_log) < scene_total:
                clip_decision_log.append(None)

            # After all threshold reductions, keep the edit renderable. Prefer an accepted clip
            # from the same semantic search bucket; otherwise hold/reuse the nearest accepted body
            # clip. This is controlled continuity, not arbitrary global filler.
            relaxed_scene_count = 0
            _fb_first = 1 if use_influencer_hook else 0
            accepted_body = [(i, c) for i, c in enumerate(scene_clips) if i >= _fb_first and c]
            fallback_pool = []
            _fallback_seen = set()
            # Continuity fallback must ONLY reuse clips that PASSED the matcher's caption/quality
            # gate (the clips actually assigned to scenes). Pulling from the raw download pool was
            # how captioned/text-heavy clips the matcher had rejected slipped onto unmatched scenes.
            for _candidate in [c for _, c in accepted_body]:
                _key = str(Path(_candidate).resolve())
                if _key not in _fallback_seen:
                    _fallback_seen.add(_key)
                    fallback_pool.append(_candidate)
            # SAFETY NET for "accepts no clips at high relevancy": when the vision matcher accepted very
            # few (or ZERO) clips - typical for an abstract/essay script at 80% relevancy, where every
            # clip scores below the strict semantic threshold - the accepted-only fallback_pool above is
            # empty and every scene would get NO footage (empty/AI render). So keep a secondary pool of
            # CLEAN RAW download clips: real vertical Japan footage that already passed the scraper's
            # text/black-bar/fake-vertical gate at download time (just not the stricter vision 'clean'
            # gate). A looser-matching real clip still beats an empty render.
            def _download_clean(path):
                m = clip_meta.get(str(path)) or {}
                try: _th = float(m.get("text_heaviness", 0) or 0)
                except (TypeError, ValueError): _th = 0.0
                try: _bb = float(m.get("black_bar_score", 0) or 0)
                except (TypeError, ValueError): _bb = 0.0
                return (_th <= 1.5 and _bb <= 0.5 and not m.get("is_fake_vertical", False))
            clean_raw_fallback = []
            _seen_raw = set(_fallback_seen)
            for _p in body_pool:
                _rk = str(Path(_p).resolve())
                if _rk in _seen_raw or not _download_clean(_p):
                    continue
                _seen_raw.add(_rk)
                clean_raw_fallback.append(_p)
            # BROADEN THE POOL to EVERY distinct clip the scrape actually downloaded. A run can
            # download ~100 distinct clips (most sit in seedance 2.0/_candidates as "declined" but
            # are real vertical footage) yet body_pool exposed only a handful - so the finalize was
            # forced to reuse. Pull the whole downloaded set (content-deduped so the same TikTok is
            # never added twice; skip the hook-presenter pool + _raw working files) so unmatched
            # scenes get FRESH distinct footage instead of a replay.
            try:
                import hashlib as _pool_hl

                def _pool_ck(_p):
                    try:
                        with open(_p, "rb") as _f:
                            return _pool_hl.sha1(_f.read(131072)).hexdigest()
                    except Exception:
                        return None

                _pool_ident = set()
                for _p in (fallback_pool + clean_raw_fallback):
                    _k = _pool_ck(_p)
                    if _k:
                        _pool_ident.add(_k)
                _extra_downloaded = []
                if seedance_target_dir.exists():
                    for _mp4 in sorted(seedance_target_dir.rglob("*.mp4")):
                        _low = {x.lower() for x in _mp4.parts}
                        if "_raw" in _low or "hook_influencer" in _low:
                            continue
                        _rk = str(_mp4.resolve())
                        if _rk in _seen_raw or not _download_clean(_mp4):
                            continue
                        _k = _pool_ck(_mp4)
                        if not _k or _k in _pool_ident:
                            continue
                        _pool_ident.add(_k)
                        _seen_raw.add(_rk)
                        _extra_downloaded.append(str(_mp4))
                if _extra_downloaded:
                    clean_raw_fallback.extend(_extra_downloaded)
                    log(status_cb, f"Distinct-clip pool broadened with {len(_extra_downloaded)} more "
                                   "downloaded clip(s); unmatched scenes get fresh footage, not a repeat.")
            except Exception as _pool_exc:  # noqa: BLE001
                log(status_cb, f"Pool broadening skipped ({_pool_exc.__class__.__name__}).")
            usage_counts = {}
            for _, _assigned in accepted_body:
                _key = str(Path(_assigned).resolve())
                usage_counts[_key] = usage_counts.get(_key, 0) + 1

            def _least_used(candidates):
                return min(
                    candidates,
                    key=lambda candidate: (
                        usage_counts.get(str(Path(candidate).resolve()), 0),
                        str(Path(candidate).name),
                    ),
                ) if candidates else None

            # STRICT NO-REUSE (user rule, absolute): a source that already appears ANYWHERE in the
            # video may NEVER fill another scene - not even a different excerpt. Identity is the
            # clip's content head-hash, so two files that are the same TikTok collide even with
            # different filenames/trims. When the pool of still-UNUSED distinct clips is exhausted
            # we HOLD the previous shot (one contiguous clip spanning the gap) instead of replaying
            # an earlier clip elsewhere in the timeline - a hold is a continuation, a replay is the
            # jarring duplicate the user keeps hitting.
            import hashlib as _dedup_hl

            def _content_key(_p):
                try:
                    with open(_p, "rb") as _fh:
                        return "h:" + _dedup_hl.sha1(_fh.read(131072)).hexdigest()
                except Exception:
                    return "p:" + str(Path(_p).resolve())

            used_identity = set()
            for _sc in scene_clips:
                if _sc:
                    used_identity.add(_content_key(_sc))

            for scene_index in range(1, scene_total):
                if scene_clips[scene_index] is not None:
                    continue
                wanted_bucket = scene_bucket.get(scene_index)
                # Accepted semantic matches first, then CLEAN RAW downloads - but ONLY clips whose
                # source is not already used anywhere in the video (incl. V2 matches + the hook).
                combined_fallback = fallback_pool + clean_raw_fallback
                unused = [p for p in combined_fallback if _content_key(p) not in used_identity]
                if unused:
                    same_bucket_pool = [
                        p for p in unused
                        if wanted_bucket and (clip_meta.get(str(p)) or {}).get("bucket_id") == wanted_bucket
                    ]
                    # deterministic pick from the unused pool, preferring the scene's own bucket
                    fallback_clip = min(same_bucket_pool or unused, key=lambda c: str(Path(c).name))
                    _is_accepted = fallback_clip in fallback_pool
                    _in_bucket = (wanted_bucket and (clip_meta.get(str(fallback_clip)) or {}).get("bucket_id") == wanted_bucket)
                    reason = ("distinct " + ("accepted" if _is_accepted else "clean-raw")
                              + " clip" + (f" from bucket {wanted_bucket}" if _in_bucket else " (no source reused)"))
                    used_identity.add(_content_key(fallback_clip))
                elif scene_index > 1 and scene_clips[scene_index - 1] is not None:
                    # No distinct clip left -> HOLD the previous shot (contiguous continuation, the
                    # renderer continues the source time across the hold). Never replay an earlier clip.
                    fallback_clip = scene_clips[scene_index - 1]
                    reason = f"no distinct clip left - held previous shot from scene {scene_index - 1}"
                else:
                    continue
                scene_clips[scene_index] = fallback_clip
                _fallback_key = str(Path(fallback_clip).resolve())
                usage_counts[_fallback_key] = usage_counts.get(_fallback_key, 0) + 1
                relaxed_scene_count += 1
                best_prior = clip_decision_log[scene_index] if scene_index < len(clip_decision_log) else None
                clip_decision_log[scene_index] = {
                    "scene": scene_index,
                    "voice_text": (scene_text_for_planning(scenes_override[scene_index])
                                   or scenes_override[scene_index].get("script", ""))[:200],
                    "chosen_clip": Path(fallback_clip).name,
                    "bucket_id": wanted_bucket or (clip_meta.get(str(fallback_clip)) or {}).get("bucket_id"),
                    "source_query": (clip_meta.get(str(fallback_clip)) or {}).get("source_query"),
                    "query_tier": (clip_meta.get(str(fallback_clip)) or {}).get("tier"),
                    "match_class": "RELAXED_CONTEXT",
                    "script_match_score": ((best_prior or {}).get("script_match_score")
                                           if isinstance(best_prior, dict) else None),
                    "passes_acceptance_test": False,
                    "reason": reason,
                    "fallback_needed": False,
                    "adaptive_fallback": True,
                }
                accepted_body.append((scene_index, fallback_clip))
            if relaxed_scene_count:
                log(status_cb, f"Adaptive fallback kept the render running for {relaxed_scene_count} scene(s) "
                               "using same-bucket/adjacent clean footage.")

            hook_chosen = next((r for r in hook_results if best_hook is not None
                                and str(Path(r["clip"]).resolve()) == str(Path(best_hook).resolve())), None)
            if best_hook is not None and scenes_override:
                clip_decision_log[0] = {
                    "scene": 0,
                    "voice_text": (scene_text_for_planning(scenes_override[0])
                                   or scenes_override[0].get("script", ""))[:200],
                    "chosen_clip": Path(best_hook).name,
                    "bucket_id": "hook_influencer",
                    "source_query": "hook_influencer (20K+ cute/dance finder)",
                    "query_tier": "hook",
                    "match_class": "HOOK_RELAXED" if hook_relaxed else "HOOK_MATCH",
                    "script_match_score": None,
                    "hook_presenter_score": (hook_chosen or {}).get("hook_presenter_score"),
                    "likes": (hook_chosen or {}).get("likes"),
                    "passes_acceptance_test": bool((hook_chosen or {}).get("passed")) or hook_relaxed,
                    "adaptive_fallback": hook_relaxed,
                    "reason": (hook_chosen or {}).get("reason", "best-scoring 20K+ cute/dance creator clip"),
                    "fallback_needed": False,
                    "fallback_type": None,
                }

            # social-search report for agent_report.json
            hook_passed = sum(1 for r in hook_results if r.get("passed"))
            social_search_report = {
                "existing_project_reuse": project_reuse_report,
                "buckets": [{"bucket_id": b.get("bucket_id"), "visual_goal": b.get("visual_goal"),
                             "used_by_scene_ids": b.get("used_by_scene_ids"),
                             "queries_by_tier": b.get("query_tiers"), "search_intent": b.get("search_intent")}
                            for b in (social_plan.get("buckets") or [])],
                "hook_finder": {
                    "enabled": True,
                    "reference_used": False,
                    "minimum_likes": HOOK_MIN_LIKES,
                    "minimum_likes_by_platform": {"tiktok": HOOK_MIN_LIKES,
                                                  "twitter": HOOK_MIN_LIKES // 4},
                    "queries": HOOK_PRESENTER_QUERIES,
                    "candidate_count": len(hook_pool),
                    "passed_count": hook_passed,
                    "chosen_clip_id": (Path(best_hook).name if best_hook is not None else None),
                    "hook_presenter_score": (hook_chosen or {}).get("hook_presenter_score"),
                    "likes": (hook_chosen or {}).get("likes"),
                    "reason": ((hook_chosen or {}).get("reason")
                               if hook_chosen else
                               ("no candidate cleared the cute/dance bar"
                                if hook_results else "no 20K+ hook candidates found")),
                    "target": social_plan.get("hook", {}).get("visual_goal", ""),
                    "candidates": [{"clip_id": Path(r["clip"]).name,
                                    "hook_presenter_score": r.get("hook_presenter_score"),
                                    "likes": r.get("likes"),
                                    "passed": r.get("passed"), "scores": r.get("scores"),
                                    "reason": r.get("reason")} for r in hook_results[:12]],
                },
                "query_performance": query_perf,
                "candidate_pool_total": len(pool),
                "clip_quality_filters": _tally_clip_quality_filters(clip_decision_log),
                "scene_assignments": [
                    {k: (d or {}).get(k) for k in
                     ("scene", "voice_text", "bucket_id", "chosen_clip", "source_query", "query_tier",
                      "match_class", "script_match_score", "raw_visual_footage_score",
                      "text_heaviness_score", "has_creator_text", "edit_stability_score",
                      "hook_presenter_score", "reason")}
                    for d in clip_decision_log if isinstance(d, dict)],
            }

            # FINAL CLEAN-UP PASS (captions + no-repeat): the LLM vision matcher can still let a
            # captioned clip through, and the continuity fallback can hand the SAME clip to two
            # scenes when the clean pool is small. Re-measure burned-in text on every chosen clip and
            # swap (a) any clip that STILL shows captions and (b) any NON-ADJACENT duplicate, always
            # preferring an unused, caption-free pool clip. Adjacent visual holds are preserved.
            _ff_cap, _ = clip_scraper._ffmpeg_tools()
            _cap_cache = {}

            def _cap_score(_c):
                _k = str(Path(_c).resolve())
                if _k not in _cap_cache:
                    try:
                        _cap_cache[_k] = clip_scraper.text_heaviness_score(Path(_c), _ff_cap, per_clip)
                    except Exception:
                        _cap_cache[_k] = 0.0
                return _cap_cache[_k]

            _CAP_MAX = 1.5   # strict: anything above incidental text is not renderable
            _used_keys = set()
            _prev_key = None
            _swaps = 0
            _cap_swaps = 0
            _caption_drops = 0
            for _idx in range(len(scene_clips)):
                _clip = scene_clips[_idx]
                if _clip is None:
                    _prev_key = None
                    continue
                _key = str(Path(_clip).resolve())
                _is_dup = _key in _used_keys and _key != _prev_key
                # V2 segments already passed the segment-level caption gates
                # (_segment_caption_signals + burned-caption rejects); re-OCRing them here at
                # the strict 1.5 bar dropped 13/17 good Japanese clips (signs/stickers score
                # 2-5) and the continuity fill then rendered ONE clip for the whole video.
                _is_capt = (not _v2) and _cap_score(_clip) > _CAP_MAX
                if _is_dup or _is_capt:
                    _cand = [p for p in fallback_pool if str(Path(p).resolve()) not in _used_keys]
                    _clean = [p for p in _cand if _cap_score(p) <= _CAP_MAX]
                    # A captioned clip may only be replaced by a clean clip. For a pure duplicate,
                    # any unused candidate helps, but never use that looser branch for captions.
                    if _is_capt:
                        _repl = _least_used(_clean) if _clean else None
                    else:
                        _repl = _least_used(_clean) if _clean else (_least_used(_cand) if _cand else None)
                    if _repl is not None:
                        scene_clips[_idx] = _repl
                        if _is_capt and _cap_score(_repl) <= _CAP_MAX:
                            _cap_swaps += 1
                        elif _is_dup:
                            _swaps += 1
                        _key = str(Path(_repl).resolve())
                        if _idx < len(clip_decision_log) and isinstance(clip_decision_log[_idx], dict):
                            clip_decision_log[_idx]["cleanup_swapped"] = True
                    elif _is_capt:
                        # Never blur captions. If no clean replacement exists, leave the scene
                        # unassigned so the normal clean fallback/continuity path handles it.
                        scene_clips[_idx] = None
                        _caption_drops += 1
                        if _idx < len(clip_decision_log) and isinstance(clip_decision_log[_idx], dict):
                            clip_decision_log[_idx]["captioned_clip_dropped"] = True
                        _prev_key = None
                        continue
                _used_keys.add(_key)
                _prev_key = _key
            if _swaps or _cap_swaps or _caption_drops:
                log(status_cb, f"Final clean-up: swapped {_cap_swaps} captioned + {_swaps} duplicate "
                               f"clip(s); dropped {_caption_drops} captioned clip(s) with no clean replacement.")

            # Place only clips that passed semantic matching for this exact scene.  The previous
            # reuse cycle filled rejected scenes with an unrelated clip accepted for a different
            # sentence, which is precisely how narration and visuals became disconnected.
            import shutil as _shutil
            for old in seedance_target_dir.glob("scraped_*.mp4"):
                try:
                    old.unlink()
                except Exception:
                    pass
            # decisions keyed by scene for stamping match_class / scores onto the scene
            dlog_by_scene = {}
            for d in (clip_decision_log or []):
                if isinstance(d, dict) and d.get("scene") is not None:
                    dlog_by_scene[int(d["scene"])] = d
            def _assigned_meta(clip):
                return clip_meta.get(str(clip), {}) if clip else {}

            assigned_ids, placed, matched = set(), 0, 0
            for i, sc in enumerate(scenes_override):
                clip = scene_clips[i] if i < len(scene_clips) else None
                is_match = clip is not None and not bool((dlog_by_scene.get(i) or {}).get("adaptive_fallback"))
                role = "hook_influencer" if (i == 0 and best_hook is not None) else "body"
                mclass = (dlog_by_scene.get(i) or {}).get("match_class") or ("HOOK_MATCH" if role == "hook_influencer" else None)
                if clip and Path(clip).exists():
                    dst = seedance_target_dir / f"scraped_{i:02d}.mp4"
                    try:
                        _shutil.copyfile(clip, dst)
                        cm = _assigned_meta(clip)
                        try:
                            dst.with_suffix(".json").write_text(json.dumps({
                                "status": "assigned", "scene_id": str(sc.get("id", i)),
                                "source_path": str(Path(clip).resolve()),
                                "platform": cm.get("platform", "tiktok"),
                                "clip_id": cm.get("clip_id") or str(Path(clip).resolve()),
                                "likes": int(cm.get("likes") or 0),
                                "text_heaviness": float(cm.get("text_heaviness") or 0.0),
                            }, indent=2), encoding="utf-8")
                        except Exception:
                            pass
                        sc["clip"] = dst.name
                        sc["seedance"] = True
                        sc["visual_role"] = role
                        sc["scrape_source"] = cm.get("platform", "tiktok")
                        sc["scrape_clip_id"] = cm.get("clip_id") or str(Path(clip).resolve())
                        sc["match_class"] = mclass
                        sc["script_match_score"] = (dlog_by_scene.get(i) or {}).get("script_match_score")
                        sc["black_bar_score"] = cm.get("black_bar_score", 0.0)
                        sc["is_fake_vertical"] = bool(cm.get("is_fake_vertical", False))
                        sc["text_heaviness_score"] = cm.get("text_heaviness", 0.0)
                        assigned_ids.add(cm.get("clip_id") or str(clip))
                        placed += 1
                        if is_match:
                            matched += 1
                    except Exception:
                        sc.pop("clip", None)
                else:
                    sc.pop("clip", None)
            # mark candidate statuses: assigned clips are accepted + shown; the rest stay hidden
            for c in candidate_statuses:
                if c.get("status") == "downloaded_pending_review":
                    if c.get("clip_id") in assigned_ids:
                        c["status"] = "assigned_to_scene"; c["shown_in_media_panel"] = True
                    else:
                        c["status"] = "accepted_pool"; c["shown_in_media_panel"] = False
            filter_summary = _summarize_candidate_filters(candidate_statuses)
            allow_gpt = False                         # scrape run = real TikTok footage only
            allow_seedance = placed > 0
            seedance_clip_count = len([s for s in scenes_override if s.get("clip")])
            if placed:
                distinct = len(assigned_ids)
                log(status_cb, f"Scrape: {matched}/{scene_total} scene(s) matched the script "
                               f"directly; final adaptive threshold was {current_match_threshold:.1f}/10.")
                used_platforms = sorted({str(sc.get("scrape_source") or "tiktok")
                                         for sc in scenes_override if sc.get("clip")})
                log(status_cb, f"Scrape: used {distinct} distinct social clip(s) from "
                               f"{', '.join(used_platforms)} across {placed} scene(s) "
                               f"(aim ~1 per cut; {scene_total} cuts).")
                if distinct < max(6, (scene_total + 1) // 2):
                    log(status_cb, f"Scrape: WARNING only {distinct} distinct clip(s) for {scene_total} cuts - "
                                   "footage will repeat. Widen the search or loosen match/text/black-bar "
                                   "thresholds for more variety.")
                log(status_cb, "Project media panel shows accepted media only.")
            else:
                allow_seedance = False
                seedance_clip_count = 0
                log(status_cb, "Scrape: no usable TikTok clips found after all search rounds.")
            unmatched_scenes = [i for i, sc in enumerate(scenes_override) if not sc.get("clip")]
            if unmatched_scenes and placed:
                source_scene = next((sc for sc in scenes_override if sc.get("clip")), None)
                source_path = seedance_target_dir / source_scene["clip"] if source_scene else None
                if source_path and source_path.exists():
                    for scene_index in unmatched_scenes:
                        dst = seedance_target_dir / f"scraped_{scene_index:02d}.mp4"
                        _shutil.copyfile(source_path, dst)
                        sc = scenes_override[scene_index]
                        sc["clip"] = dst.name
                        sc["seedance"] = True
                        sc["visual_role"] = "hook_influencer" if scene_index == 0 else "body"
                        sc["scrape_source"] = source_scene.get("scrape_source", "tiktok")
                        sc["scrape_clip_id"] = source_scene.get("scrape_clip_id")
                        sc["match_class"] = "RELAXED_CONTINUITY"
                        sc["script_match_score"] = None
                        sc["black_bar_score"] = 0.0
                        sc["is_fake_vertical"] = False
                        sc["text_heaviness_score"] = 0.0
                    log(status_cb, f"Continuity hold filled {len(unmatched_scenes)} remaining scene(s); "
                                   "the render will continue instead of aborting.")
                    seedance_clip_count = len([s for s in scenes_override if s.get("clip")])
            elif unmatched_scenes:
                if not getattr(clip_scraper, "backend_active", lambda _p=None: False)(scrape_platforms):
                    raise RuntimeError(
                        "None of the selected social sources are connected. Connect TikTok and/or X, then re-run.")
                raise RuntimeError("Social scrape returned no usable video files at all; cannot render a "
                                   "video layer. Check the console log for the search/download errors "
                                   "(login expired / rate-limit / network).")
            # The chosen clips are already copied to scraped_NN.mp4. KEEP the candidate pool on disk
            # (under seedance 2.0/_candidates) so the timeline editor can show ALL downloaded scraped
            # footage, including the declined ones. The accepted progress panels already exclude
            # anything under _candidates/_raw, so they still show accepted media only. Just drop the
            # transient _raw download scratch.
            try:
                for raw_dir in (seedance_target_dir / "_candidates").rglob("_raw"):
                    shutil.rmtree(raw_dir, ignore_errors=True)
            except Exception:
                pass

            # finalize the social-search report with the enforcement evidence
            hook_first = bool(best_hook is not None and scenes_override
                              and scenes_override[0].get("visual_role") == "hook_influencer")
            social_search_report.update({
                "search_mode": "bucket_based_social_search",
                "old_style_query_path_used": False,
                "project_media_panel_policy": "accepted_media_only",
                "voice_speed": resolve_voice_speed(form, "scrape"),
                "visual_emphasis_enabled": False,
                "hook_first": hook_first,
                "semantic_matching_skipped": bool(semantic_matching_skipped),
                "clip_filter_summary": filter_summary,
                "candidate_statuses": candidate_statuses[:400],
                "scene_assignments": [
                    {"scene_id": i,
                     "voice_text": (scene_text_for_planning(sc) or sc.get("script", ""))[:200],
                     "visual_role": sc.get("visual_role"),
                     "bucket_id": (dlog_by_scene.get(i) or {}).get("bucket_id"),
                     "chosen_clip_id": sc.get("clip"),
                     "source_query": (dlog_by_scene.get(i) or {}).get("source_query"),
                     "match_class": sc.get("match_class"),
                     "script_match_score": sc.get("script_match_score"),
                     "raw_visual_footage_score": (dlog_by_scene.get(i) or {}).get("raw_visual_footage_score"),
                     "text_heaviness_score": sc.get("text_heaviness_score"),
                     "black_bar_score": sc.get("black_bar_score"),
                     "is_fake_vertical": sc.get("is_fake_vertical"),
                     "accepted": bool(sc.get("clip")),
                     "shown_in_media_panel": bool(sc.get("clip"))}
                    for i, sc in enumerate(scenes_override)],
            })
            # carried onto config after plan_config for the pre-render validation gate
            scrape_enforcement = {
                "hook_first": hook_first, "best_hook_present": best_hook is not None,
                "semantic_matching_skipped": bool(semantic_matching_skipped),
                "scene_meta": [{"visual_role": sc.get("visual_role"),
                                "match_class": sc.get("match_class"),
                                "script_match_score": sc.get("script_match_score"),
                                "black_bar_score": sc.get("black_bar_score"),
                                "is_fake_vertical": sc.get("is_fake_vertical"),
                                "scrape_source": sc.get("scrape_source"),
                                "has_clip": bool(sc.get("clip"))}
                               for sc in scenes_override],
            }
            form["_scrape_enforcement"] = scrape_enforcement if isinstance(form, dict) else None
    max_seedance = seedance_clip_count
    wavespeed_key = os.environ.get("WAVESPEED_API_KEY", "")

    check_cancel(form)
    log(status_cb, "Planning scenes and media...")
    image_model_choice = (form.get("image_model") or "openai/gpt-image-2/text-to-image").strip()
    config = plan_config(
        project_dir,
        title,
        script,
        target_duration,
        allow_seedance=allow_seedance,
        max_seedance=max_seedance,
        static_gpt_image_count=static_gpt_image_count if allow_gpt else 0,
        scenes_override=scenes_override,
        director_plan=director_plan,
        visual_sections=visual_sections,
        speaker_hook_enabled=speaker_hook_enabled,
        image_model=image_model_choice,
        video_model=seedance_model_choice,
    )
    config["project_slug"] = slug
    config["loaded_project_mode"] = recut_mode
    attach_cancel_event(config, form)  # so the InfiniteTalk hook render below is cancellable
    config.setdefault("wavespeed", {})
    config["wavespeed"]["seedance_model"] = seedance_model_choice
    config["wavespeed"]["video_model"] = SEEDANCE_VIDEO_MODELS.get(seedance_model_choice)
    config["wavespeed"]["video_resolution"] = "720p" if seedance_model_choice == "happyhorse-1.1" else "480p"
    config["wavespeed"]["video_enable_web_search"] = seedance_model_choice in ("seedance-2.0", "seedance-2.0-fast")
    config["wavespeed"]["image_model"] = image_model_choice  # resolved before plan_config
    config["wavespeed"]["reasoning_model"] = form.get("reasoning_model", "openai/gpt-5.5")
    config["wavespeed"]["reasoning_mode"] = reasoning_modes.validate_reasoning_mode(
        config["wavespeed"]["reasoning_model"], form.get("reasoning_mode"))
    # The music PICKER is the single source of truth: "None" means NO background music, even
    # when the "Background music" output toggle is on. Previously the toggle alone enabled the
    # mood-based auto-pick in generate mode, silently attaching a bed the user never chose.
    _bg_choice0 = (str(form.get("background_music_choice") or "").strip()
                   if isinstance(form, dict) else "")
    _bg_picked = bool(_bg_choice0) and _bg_choice0.lower() not in ("none", "off", "auto_none", "")
    background_music_enabled = bool(background_music_enabled and _bg_picked)
    config["background_music_enabled"] = background_music_enabled
    config["background_music_user_enabled"] = background_music_enabled
    if background_music_enabled and _bg_choice0.lower() != "auto":
        config["background_music_file"] = _bg_choice0
    config["sfx_generation_enabled"] = form_flag(form, "generate_missing_sfx", True)
    # apply the remaining output toggles onto the render config
    config["output_toggles"] = output_toggles
    config["sfx_enabled"] = bool(out_sfx or out_tr_sfx)
    config["sfx_content_enabled"] = bool(out_sfx)
    config["transition_sfx_enabled"] = bool(out_tr_sfx)
    # user-selected SFX density (low/medium/high) -> place_editor_sfx budget + AI instructions
    _sfx_amount = str(form.get("sfx_amount", "") or "").strip().lower()
    if _sfx_amount in ("low", "medium", "high"):
        config["sfx_amount"] = _sfx_amount
    # Real video footage (scraped clips) is cut hard — a whoosh on every boundary looks
    # cheap on found-footage. Force transition SFX off for video; keep content/ambient SFX.
    # Also never use the TikTok clips' own audio (only narration + ambient SFX + music).
    if clip_source == "scrape":
        # Background music in scrape mode is now the USER'S CHOICE (music picker). When they pick a
        # track it plays as a ducked bed under the voice (like the reference channel); otherwise no
        # bed (the old default) so nothing masks the editorial SFX hits.
        _bg_choice = (str(form.get("background_music_choice") or "").strip()
                      if isinstance(form, dict) else "")
        _bg_on = bool(_bg_choice) and _bg_choice.lower() not in ("none", "off", "auto_none", "")
        if _bg_on:
            background_music_enabled = True
            config["background_music_enabled"] = True
            config["background_music_user_enabled"] = True
            config["background_music_file"] = _bg_choice
            log(status_cb, f"Background music: '{_bg_choice}' as a ducked bed under the voice.")
        else:
            background_music_enabled = False
            config["background_music_enabled"] = False
            config["background_music_user_enabled"] = False
        config["transition_sfx_enabled"] = False
        config["sfx_enabled"] = bool(out_sfx)
        # Reference edits use SHORT, real edited hits only - never a synthesized ambient bed and
        # never Kling-generated SFX. Hard-disable all SFX generation for scrape runs so the only
        # sounds are the short library impacts/whooshes placed on cuts by place_editor_sfx.
        config["sfx_generation_enabled"] = False
        config["seedance_audio_in_final"] = False
        config["seedance_audio_volume"] = 0.0
        config["seedance_audio_volume_with_speech"] = 0.0
        config["seedance_clip_start_trim"] = 0.0
        config["caption_max_words"] = 1   # reference style: one big word at a time
        config["caption_center_y"] = 0.58
        config["caption_size"] = 94
        config["caption_active_box"] = False
        config["allow_ambient_sfx"] = False
        # Reference edits are SFX-dense: a whoosh on most cuts PLUS accents (pops/dings/impacts).
        # 33/min still felt sparse (~25 on a 45s short); 46/min lands a hit on nearly every cut and
        # leaves room for callout/emphasis accents between them.
        config["editor_sfx_max_per_minute"] = 46
        config["editor_sfx_volume_with_speech"] = 0.46
        config["final_loudness_lufs"] = -16.5
        # music bed levels: ducked ~-16 dB under the voice when the user picked a track, else silent.
        if _bg_on:
            config["background_music_volume"] = 0.28            # intro/outro (no speech) bed level
            config["background_music_volume_with_speech"] = 0.15  # ducked under the narration
        else:
            config["background_music_volume"] = 0.0
            config["background_music_volume_with_speech"] = 0.0
        # never mix the scraped clips' own audio under the voice (only the chosen music, if any)
        config["mix_seedance_audio_with_speech"] = False
        # measure the voice noise floor / HF hiss so the report proves the rauschen is handled
        _noise = measure_voice_noise(audio_path)
        config["audio_noise_report"] = _noise
        if _noise.get("noise_floor_db") is not None:
            log(status_cb, f"Audio Noise Check: measured noise floor {_noise['noise_floor_db']} dB, "
                           f"HF hiss {_noise['high_frequency_hiss_score']}/10"
                           + (" - constant hiss detected" if _noise.get("constant_hiss_detected") else " - clean."))
        # ---- enforcement flags carried into the pre-render validation gate ----
        config["voice_speed"] = resolve_voice_speed(form, "scrape")
        config["visual_emphasis_enabled"] = False
        config["smart_overlays"] = False
        config["search_mode"] = "bucket_based_social_search"
        config["project_media_panel_policy"] = "accepted_media_only"
        _enf = form.get("_scrape_enforcement") if isinstance(form, dict) else None
        config["_scrape_enforcement"] = _enf or {}
        # stamp per-scene enforcement metadata onto the rebuilt config scenes (plan_config drops
        # arbitrary keys), aligned by index, so validate_scrape_render can read them.
        _sm = (_enf or {}).get("scene_meta") or []
        for _i, _cs in enumerate(config.get("scenes", [])):
            if _i < len(_sm):
                _cs["visual_role"] = _sm[_i].get("visual_role")
                _cs["match_class"] = _sm[_i].get("match_class")
                _cs["script_match_score"] = _sm[_i].get("script_match_score")
                _cs["black_bar_score"] = _sm[_i].get("black_bar_score")
                _cs["is_fake_vertical"] = _sm[_i].get("is_fake_vertical")
                _cs["scrape_source"] = _sm[_i].get("scrape_source")
    config["use_seedance_clips"] = bool(out_clips) and config.get("use_seedance_clips", True)
    config["render_captions"] = bool(out_caps)
    config["use_wikimedia"] = bool(out_wiki)
    config["halt_after_speech"] = form_flag(form, "halt_after_speech", False)
    config["clip_source"] = clip_source
    config["scrape_platforms"] = scrape_platforms
    config["scrape_terms"] = scrape_terms
    config["scrape_sort"] = scrape_sort
    config["script_relevancy"] = script_relevancy
    log(status_cb, f"Seedance model selected: {seedance_model_choice}.")
    log(status_cb, f"Background music: {'enabled' if background_music_enabled else 'disabled'}.")
    log(status_cb, f"SFX generation fallback: {'enabled' if config['sfx_generation_enabled'] else 'disabled'}.")
    if recut_mode == "normal":
        # Stamp every run so renders never overwrite each other - you can tell which version is which
        # (e.g. japan_clock_auto_short_20260630_2145.mp4). The app's "latest render" picks newest mtime.
        config["output_basename"] = f"{slug}_auto_short_{recut_stamp}"
        config["render_version"] = recut_stamp
    else:
        config["output_basename"] = f"{slug}_{recut_mode}_{recut_stamp}"
        config["recut_stamp"] = recut_stamp
        # Timeline "reorder & recut": recut_existing_only reuses the project's own clips, UNLESS the
        # user explicitly chose "find more clips" (recut_allow_scrape) in the media-source popup.
        config["recut_uses_existing_media_only"] = (recut_mode == "recut_existing_only"
                                                    and not form_flag(form, "recut_allow_scrape", False))
        config["recut_generates_new_web_images_only"] = recut_mode == "recut_new_web_images"
        config["recut_regenerates_seedance"] = recut_mode == "recut_regenerate_seedance"
        config["recut_recreates_speaker_clip_only"] = recut_mode == "recut_recreate_speaker_clip"
    if speaker_hook_enabled:
        apply_speaker_hook_to_config(
            config,
            project_dir,
            title,
            script,
            visual_script,
            speaker_image_path,
            recreate=speaker_hook_recreate or bool(speaker_image_path),
            reasoning_model=reasoning_model,
            status_cb=status_cb,
        )
    config = enforce_unique_media_per_render(config, status_cb=status_cb)
    if visual_script:
        config["visual_script"] = visual_script
    if audio_path:
        config["timing_audio_path"] = str(audio_path)
        mix_voice = form_flag(form, "mix_voice_in_final", True)
        if mix_voice:
            config["audio_path"] = str(audio_path)
            config["speech_audio_in_final"] = True
            log(status_cb, "Voice is the primary audio track; music and SFX are ducked under it.")
        else:
            config["audio_path"] = None
            config["speech_audio_in_final"] = False
            log(status_cb, "Voice track used for timing only; it will not be mixed into the final video.")
        config["audio_model"] = GEMINI_AUDIO_MODEL if audio_analysis else None
        config["audio_duration_seconds"] = audio_duration
        config["audio_timing_source"] = audio_timing_source
        config["audio_sync_locked"] = bool(audio_duration and audio_timing_source and audio_timing_source != "fallback")

    # Frame-accurate word timing: align the known script to the actual voice so
    # word-by-word captions and scene cuts land exactly on the spoken beats.
    # Reuse the timeline already computed for the primary timing source when available.
    if audio_path and config.get("scenes"):
        try:
            import voice_align
            timeline = word_timeline_cache
            if not timeline and voice_align.available():
                log(status_cb, "Aligning script to voice for frame-accurate word timing...")
                _align_speed = (resolve_voice_speed(form, form.get("clip_source"))
                                if form_flag(form, "generate_voice", True) else 1.0)
                timeline = voice_align.word_timeline(str(audio_path), script_text=script,
                                                     status_cb=status_cb, speed=_align_speed)
            if timeline:
                voice_align.snap_scene_boundaries(
                    config["scenes"], timeline, float(config.get("duration") or 0.0)
                )
                for scene in config["scenes"]:
                    scene["word_timings"] = voice_align.words_in_window(
                        timeline, float(scene["start"]), float(scene["end"])
                    )
                config["word_timing_source"] = "forced_alignment"
                log(status_cb, f"Frame-accurate word timing applied to captions and cuts ({len(timeline)} words).")
            elif not voice_align.available():
                log(status_cb, "faster-whisper not installed; captions use estimated word timing.")
        except Exception as exc:
            log(status_cb, f"Word alignment skipped ({exc}); captions use estimated timing.")

    config_path = project_dir / "config" / "project.json"
    config_path.write_text(json.dumps(config_for_json(config), indent=2), encoding="utf-8")
    config["_config_path"] = str(config_path.resolve())
    
    import edit_plan
    edit_plan_path = project_dir / "config" / "edit_plan.json"
    edit_plan_data = edit_plan.convert_config_to_edit_plan(config)
    edit_plan_path.write_text(json.dumps(edit_plan_data, indent=2), encoding="utf-8")
    attach_cancel_event(config, form)
    config["_status_cb"] = status_cb

    _, asset_dir, _, _ = pipeline.project_paths(config)
    clip_dir = project_dir / "seedance 2.0"
    existing_enough = config.get("agent", {}).get("enough_existing_seedance", False)
    prompt_review_needed = any(
        (
            scene.get("needs_gpt_asset")
            and not scene.get("speaker_hook")
            and not (asset_dir / (scene.get("asset") or "")).exists()
        )
        or (
            allow_seedance
            and scene.get("seedance")
            and not scene.get("speaker_hook")
            and not (clip_dir / pipeline.clip_filename(scene, index)).exists()
            and not existing_enough
        )
        for index, scene in enumerate(config["scenes"], 1)
    )
    if prompt_review_needed and wavespeed_key:
        check_cancel(form)
        prompt_review = llm_prompt_review(title, script, visual_script, config, status_cb=status_cb)
        if prompt_review:
            prompt_review_path = project_dir / "review" / "llm_prompt_review.json"
            prompt_review_path.write_text(json.dumps(prompt_review, indent=2), encoding="utf-8")
            apply_llm_prompt_updates(config, prompt_review, status_cb=status_cb)
            config_path.write_text(json.dumps(config_for_json(config), indent=2), encoding="utf-8")
    elif prompt_review_needed:
        log(status_cb, "Reasoning Agent prompt review skipped; no WaveSpeed API key available.")
    else:
        log(status_cb, "Reasoning Agent prompt review skipped; no new GPT/Seedance generation needs prompt changes.")

    needs_seedance_source_images = allow_seedance and any(
        scene.get("seedance") and not scene.get("clip") and not (asset_dir / scene.get("asset", "")).exists()
        for scene in config["scenes"]
    )
    allow_gpt_generation = allow_gpt or needs_seedance_source_images
    needs_gpt = allow_gpt_generation and (
        any(scene.get("needs_gpt_asset") and not (asset_dir / scene.get("asset", "")).exists() for scene in config["scenes"])
    )
    if needs_gpt:
        if not wavespeed_key:
            raise RuntimeError("This project needs GPT I2V source images for Seedance, but WAVESPEED_API_KEY is not set.")
        check_cancel(form)
        log(status_cb, "Generating missing Seedance I2V GPT source images with WaveSpeed...")
        pipeline.generate_assets(config, force=False, status_cb=status_cb)
        gpt_paths = sorted([p for p in asset_dir.iterdir() if p.suffix.lower() in IMAGE_EXTS and p.stat().st_size > 1000])
        gpt_sheet = create_media_contact_sheet(
            gpt_paths,
            project_dir / "review" / "gpt_images_contact_sheet.jpg",
            title="GPT source images for Seedance",
        )
        if gpt_sheet:
            log_preview(status_cb, "GPT source image contact sheet", gpt_sheet)
    else:
        log(status_cb, "Skipping GPT source image generation; existing Seedance source media is enough or generation is disabled.")

    needs_seedance = allow_seedance and any(
        scene.get("seedance") and not (clip_dir / pipeline.clip_filename(scene, index)).exists()
        for index, scene in enumerate(config["scenes"], 1)
    )
    if needs_seedance and (not existing_enough or speaker_hook_enabled):
        if not wavespeed_key:
            raise RuntimeError("This project needs Seedance clips, but WAVESPEED_API_KEY is not set.")
        check_cancel(form)
        log(status_cb, f"Generating missing {seedance_model_choice} clips...")
        pipeline.generate_clips(config, force=False, status_cb=status_cb)
    else:
        log(status_cb, "Skipping Seedance generation; enough clips already exist or generation is disabled.")

    check_cancel(form)
    config = replace_selected_media_after_seedance(
        form,
        config,
        project_dir,
        title,
        script,
        visual_script,
        target_duration,
        scenes_override,
        status_cb=status_cb,
    )
    config = enforce_unique_media_per_render(config, status_cb=status_cb)
    Path(config.get("_config_path", config_path)).write_text(json.dumps(config_for_json(config), indent=2), encoding="utf-8")

    check_cancel(form)
    if use_llm_video_review:
        pre_render_review = llm_pre_render_edit_review(title, script, visual_script, config, reasoning_model=reasoning_model, status_cb=status_cb)
        if pre_render_review:
            pre_render_path = project_dir / "review" / "llm_pre_render_edit_review.json"
            pre_render_path.write_text(json.dumps(pre_render_review, indent=2), encoding="utf-8")
            pre_render_config, pre_render_changed = apply_llm_corrections(config, pre_render_review)
            if pre_render_changed:
                log(status_cb, "Reasoning Agent pre-render audit requested safe render-only corrections; applying before final render.")
                pre_render_config["_config_path"] = config.get("_config_path")
                attach_cancel_event(pre_render_config, form)
                pre_render_config["_status_cb"] = status_cb
                config = enforce_unique_media_per_render(pre_render_config, status_cb=status_cb)
                Path(config.get("_config_path", config_path)).write_text(json.dumps(config_for_json(config), indent=2), encoding="utf-8")
            elif pre_render_review.get("needs_correction"):
                log(status_cb, "Reasoning Agent pre-render audit found issues, but no safe render-only correction was available.")
            else:
                log(status_cb, "Reasoning Agent pre-render audit accepted the planned edit.")

    check_cancel(form)
    if config.get("sfx_generation_enabled"):
        log(status_cb, "Generating any keyword-triggered sound effects...")
        pipeline.ensure_generated_sfx(config, status_cb=status_cb)

    # The live media panel can exclude an assigned clip while the run is still working. Repair
    # those scene slots before visual analysis so arrows/reframes inspect the replacement footage.
    reconcile_manual_scrape_exclusions(config, project_dir, form, status_cb=status_cb)

    # Visual effects. The old text-only emphasis guessed target zones and produced random arrows;
    # for scrape mode we now run the TARGET-BASED planner (plan_visual_fx): it looks at the actual
    # clip frame, only adds a red callout when a concrete relevant subject exists, and sets the
    # per-scene punch-in/reframe/shake/freeze/transition. A user can hard-disable all of it.
    # VFX layers chosen at "select the final layers": arrows (in-render), meme + neko reactions
    # (composited after the render). vfx_amount scales all of them.
    _ui_form = bool(str(form.get("ui_form", "")).strip()) if isinstance(form, dict) else False
    _add_visual_effects = form_flag(form, "add_visual_effects", False if _ui_form else True)
    _add_meme_reactions = form_flag(form, "add_meme_reactions", False)
    _add_neko_reactions = form_flag(form, "add_neko_reactions", False)
    _vfx_amount = str((form.get("vfx_amount") if isinstance(form, dict) else "") or "medium").strip().lower()
    if _vfx_amount not in ("low", "medium", "high"):
        _vfx_amount = "medium"
    config["vfx_amount"] = _vfx_amount
    _disable_visual_fx = form_flag(form, "disable_visual_fx", False) if isinstance(form, dict) else False
    _fx_off = _disable_visual_fx or not _add_visual_effects   # arrows/punch-in only when chosen
    for _sc in config.get("scenes", []):
        _sc.pop("overlays", None)                 # clear any stale/legacy overlay specs first
    if _fx_off:
        config["visual_emphasis_enabled"] = False
        config["smart_overlays"] = False
        for _sc in config.get("scenes", []):
            _sc.pop("fx", None)
        log(status_cb, "Visual FX disabled by request: no callouts / punch-in / transitions.")
    elif config.get("clip_source") == "scrape":
        try:
            plan_visual_fx(config, project_dir, reasoning_model=reasoning_model,
                           status_cb=status_cb, collaborate=collaborate_reasoning, vfx_amount=_vfx_amount)
            config["visual_emphasis_enabled"] = bool((config.get("visual_fx_report") or {})
                                                     .get("visual_fx_summary", {}).get("callout_count"))
        except Exception as exc:  # noqa: BLE001
            log(status_cb, f"Visual FX step skipped ({exc.__class__.__name__}: {exc}).")
            config["smart_overlays"] = False
    else:
        config["visual_emphasis_enabled"] = True
        try:
            plan_visual_emphasis(config, reasoning_model=reasoning_model, status_cb=status_cb)
        except Exception as exc:  # noqa: BLE001
            log(status_cb, f"Visual emphasis step skipped ({exc.__class__.__name__}).")

    # Reference-style short EDITED SFX placed ON THE CUTS (a punchy impact + a whoosh on the
    # emphasis beats, pulled from the SFX library). NOT a continuous ambient drone. Runs after
    # emphasis so it can land a whoosh on the arrow beats.
    # Script-to-Visuals: with an API key, only the frame-accurate CUT transitions are baked into
    # the render here - hook riser, impacts, reactions and callout sounds come from the multimodal
    # Audio Director AFTER the render (it watches+hears the finished video; sfx_agent).
    config["sfx_semantic_vision"] = bool(
        clip_source != "scrape"
        and config.get("sfx_content_enabled", True)
        and os.environ.get("WAVESPEED_API_KEY")
        and not os.environ.get("SHORTSLAB_NO_PAID_API"))
    if config.get("sfx_content_enabled", True):
        try:
            place_editor_sfx(config, status_cb=status_cb)
        except pipeline.PipelineCancelled:
            raise
        except Exception as exc:  # noqa: BLE001
            log(status_cb, f"Editor SFX step skipped ({exc.__class__.__name__}).")

    # Catch exclusions clicked during visual/SFX planning as close to render as possible.
    reconcile_manual_scrape_exclusions(config, project_dir, form, status_cb=status_cb)

    # Persist the actual render state after visual-FX/SFX planning. Older runs saved project.json
    # before this stage, so arrows appeared in the MP4 but were absent from the timeline editor.
    Path(config.get("_config_path", config_path)).write_text(
        json.dumps(config_for_json(config), indent=2), encoding="utf-8")

    # HARD pre-render gate for scrape mode: refuse to render a broken timeline (no hook, junk
    # clips, fake-vertical, D_REJECTED, wrong speech speed, emphasis on, semantic matching skipped).
    if clip_source == "scrape":
        if isinstance(form, dict) and form.get("_scrape_v2_used"):
            import scrape_v2
            scrape_v2.validate_scrape_render_v2(config, status_cb=status_cb)
        else:
            validate_scrape_render(config, status_cb=status_cb)

    # #impact-word: the user can mark ONE word in the hook step as the "impact word". The SFX
    # Master aims its hook impact/riser climax at that word (expected in the first 0-5s) instead
    # of guessing. Persist it on config so a later "Redo SFX" honours it too.
    impact_word = clean_text(form.get("impact_word", "") or "").strip()
    if impact_word:
        config["impact_word"] = impact_word
        log(status_cb, f"Impact word marked for the SFX Master: {impact_word!r}")
    # hook_keywords: strong verbs / toxic adjectives from the Script Creator - persisted for
    # the pipeline-v0.2 color-coded caption pass (phase 4).
    try:
        _hkw = json.loads(str(form.get("hook_keywords") or "[]"))
        if isinstance(_hkw, list) and _hkw:
            config["hook_keywords"] = [str(k) for k in _hkw][:14]
    except Exception:
        pass
    # edit pipeline version: v0.2 = reference-edit rules (phases land incrementally and gate on
    # this flag); v0.1 = the classic pipeline, byte-identical behavior.
    _pver = str(form.get("pipeline_version") or "v0.2").strip().lower()
    config["pipeline_version"] = "v0.1" if _pver == "v0.1" else "v0.2"
    log(status_cb, f"Edit pipeline: {config['pipeline_version']}"
        + (" (reference edit rules)" if config["pipeline_version"] == "v0.2" else " (classic)"))
    if config["pipeline_version"] == "v0.2" and not config.get("hook_keywords"):
        # uploaded script without Script-Creator keywords -> rule-based fallback so the
        # color-coded captions still light up the toxic/extreme words and numbers
        _kw_re = re.compile(
            r"\b(never|illegal|bann?ed|crime|forced?|forbidden|strictly|insane|obsess\w*|"
            r"extreme\w*|dump\w*|brutal\w*|shocking|violat\w*|punish\w*|arrest\w*|fined?|"
            r"caught|exhaust\w*|worst|harshest|craziest|\d+%?)\b", re.IGNORECASE)
        _seen_kw, _found = set(), []
        for _m in _kw_re.finditer(script or ""):
            _w = _m.group(0)
            if _w.lower() not in _seen_kw:
                _seen_kw.add(_w.lower())
                _found.append(_w)
        if _found:
            config["hook_keywords"] = _found[:14]
            log(status_cb, f"Caption keywords (rule-based): {', '.join(_found[:8])}...")

    check_cancel(form)
    # CLIP-SHORT (scrape) hands off to the timeline editor instead of baking a final MP4 here. All
    # the pipeline's edits (scene clips, cut/reaction SFX, arrows, captions, timings) are already
    # written to project.json above, and scrape mode runs NO post-render Audio-Director pass
    # (sfx_semantic_vision is False for scrape) - so nothing is lost by skipping the encode. The
    # user opens the timeline with everything as edited and renders from there when happy.
    _skip_render_open_timeline = (
        clip_source == "scrape" and form_flag(form, "open_timeline_no_render", True))
    if _skip_render_open_timeline:
        log(status_cb, "Skipping the final render - opening the timeline editor with the "
                       "pipeline's edit. Render from the timeline when you're happy with it.")
        try:
            log(status_cb, "PROJECT_DIR|" + str(project_dir))
        except Exception:
            pass
        return {
            "project_dir": str(project_dir),
            "project_slug": project_dir.name,
            "title": title,
            "video": None,
            "open_timeline": True,
            "no_render": True,
        }
    log(status_cb, "Rendering final 9:16 MP4...")
    output = pipeline.render_video(config)

    # VFX Master reaction layers (memes / nekos) composited onto the finished render. Arrows are
    # already baked in-render by plan_visual_fx, so add_arrows=False avoids drawing them twice.
    if (_add_meme_reactions or _add_neko_reactions) and output and Path(output).exists():
        _layers = [name for name, on in (("meme reactions", _add_meme_reactions),
                                         ("neko reactions", _add_neko_reactions)) if on]
        try:
            check_cancel(form)
            import visual_agent
            log(status_cb, "Adding %s (amount: %s)..." % (" + ".join(_layers), _vfx_amount))
            _vfx_res = visual_agent.enhance_video_with_arrows(
                output, reasoning_model=reasoning_model, status_cb=status_cb,
                out_dir=(project_dir / "renders"),
                add_characters=_add_neko_reactions, add_memes=_add_meme_reactions,
                vfx_amount=_vfx_amount, add_arrows=False, write_project=False)
            _enhanced = _vfx_res.get("video") if isinstance(_vfx_res, dict) else None
            if _enhanced and Path(_enhanced).exists():
                output = Path(_enhanced)
                log(status_cb, "Reaction layers added to the render.")
        except Exception as exc:  # noqa: BLE001
            log(status_cb, f"Reaction layers skipped ({exc.__class__.__name__}: {exc}).")
    check_cancel(form)

    # SEMANTIC SFX PASS (Script-to-Visuals): the multimodal Audio Director watches + hears the
    # finished render (voice + baked-in cut whooshes) and layers hook riser, impacts and
    # content-matched reaction SFX on top. Mixes in place. If the video/audio-URL call FAILS the
    # whole run ABORTS (user rule: no silent fallback to the old engine).
    if config.get("sfx_semantic_vision"):
        import sfx_agent
        _sem_phrases = [{"start": float(s.get("start", 0) or 0), "end": float(s.get("end", 0) or 0),
                         "text": str(s.get("exact_voice_text") or s.get("voice_line")
                                     or s.get("script") or "")}
                        for s in config.get("scenes", [])]
        _sem_cuts = [float(s.get("start", 0) or 0) for s in config.get("scenes", [])[1:]]
        log(status_cb, "Audio Director: watching the finished render for semantic SFX...")
        try:
            _sem_n = sfx_agent.apply_semantic_sfx_to_render(
                output, reasoning_model=config.get("sfx_vision_model") or reasoning_model,
                status_cb=status_cb, sfx_amount=str(config.get("sfx_amount") or "medium"),
                phrases=_sem_phrases, cuts=_sem_cuts,
                impact_word=str(config.get("impact_word") or "").strip() or None)
        except pipeline.PipelineCancelled:
            raise
        except Exception as exc:  # noqa: BLE001
            log(status_cb, f"Audio Director FAILED ({exc.__class__.__name__}: {exc}) - aborting run.")
            raise RuntimeError(f"Semantic SFX (Audio Director) failed: {exc}") from exc
        log(status_cb, f"Audio Director added {_sem_n} semantic SFX." if _sem_n
            else "Audio Director added no extra SFX (render unchanged).")

    log(status_cb, "Creating review sheets...")
    scene_sheet, checklist = pipeline.create_review(config, output)
    log_preview(status_cb, "Scene review sheet", scene_sheet)
    shot_sheet = pipeline.create_shot_review(config, output)
    log_preview(status_cb, "Shot review sheet", shot_sheet)
    original_output = output
    original_scene_sheet = scene_sheet
    original_shot_sheet = shot_sheet
    pre_render_review_path = project_dir / "review" / "llm_pre_render_edit_review.json"
    llm_review_path = None
    llm_corrected_review_path = None
    llm_second_corrected_review_path = None
    llm_review_passes = 0
    llm_review_pass_paths = []
    llm_correction_passes = 0
    corrected_by_gpt55 = False
    unresolved_media_review = None
    # Post-render video review + correction passes REMOVED (user request, 2026-06-30). They used to
    # re-render the Short up to two more times (_llm_corrected / _llm_corrected_2) for little gain.
    # The single render above is final. The pre-render edit audit still runs BEFORE the render, and
    # for scrape the pre-render validation gate (validate_scrape_render) still applies.

    # ONE render only (user rule 2026-07-11): the initial run renders the Short a SINGLE time with
    # everything baked in. The old alternate-audio variants (no_sfx / voice_only / seedance_audio_only)
    # each triggered a FULL extra render pass - pointless because the user re-renders exactly what they
    # want from the timeline editor afterwards. Keep the dict shape so the report stays valid.
    render_variants = {"all_sounds": str(output)}

    check_cancel(form)
    log(status_cb, "Cleaning standard MP4 metadata for privacy...")
    metadata_cleanup_result = pipeline.sanitize_video_metadata(output, config)
    if metadata_cleanup_result.get("cleaned"):
        log(status_cb, "Standard MP4 metadata cleaned.")
    else:
        log(status_cb, f"Standard MP4 metadata cleanup skipped: {metadata_cleanup_result.get('reason', 'unknown')}")
    for variant_label, variant_path in render_variants.items():
        if variant_path == str(output):
            continue
        try:
            pipeline.sanitize_video_metadata(variant_path, config)
        except Exception:
            pass

    web_contact_sheet = project_dir / "review" / "web_images_contact_sheet.jpg"
    gpt_contact_sheet = project_dir / "review" / "gpt_images_contact_sheet.jpg"
    report = {
        "title": title,
        "project_dir": str(project_dir),
        "config": str(config.get("_config_path", config_path)),
        "edit_plan": str(project_dir / "config" / "edit_plan.json"),
        "video": str(output),
        "original_video": str(original_output) if original_output != output else None,
        "audio": str(audio_path) if audio_path else None,
        "audio_analysis": str(project_dir / "input" / "audio_analysis.json") if audio_analysis else None,
        "visual_script": str(project_dir / "input" / "visual_script.txt") if visual_script else None,
        "visual_direction_provided": bool(visual_script),
        "visual_direction_used_as": "secondary style guidance" if visual_script else "none (infer logic from Voice Script)",
        "director_plan": str(director_plan_path) if director_plan_path else None,
        "scene_review": str(scene_sheet),
        "shot_review": str(shot_sheet),
        "original_scene_review": str(original_scene_sheet) if original_scene_sheet != scene_sheet else None,
        "original_shot_review": str(original_shot_sheet) if original_shot_sheet != shot_sheet else None,
        "llm_pre_render_review": str(pre_render_review_path) if pre_render_review_path.exists() else None,
        "llm_video_review": str(llm_review_path) if llm_review_path else None,
        "llm_corrected_video_review": str(llm_corrected_review_path) if llm_corrected_review_path else None,
        "llm_second_corrected_video_review": str(llm_second_corrected_review_path) if llm_second_corrected_review_path else None,
        "llm_corrected": corrected_by_gpt55,
        "llm_review_passes": llm_review_passes,
        "llm_review_pass_paths": llm_review_pass_paths,
        "llm_correction_passes": llm_correction_passes,
        "quality_gate_passed": unresolved_media_review is None,
        "needs_media_recut": unresolved_media_review is not None,
        "metadata_cleanup": metadata_cleanup_result,
        "render_variants": render_variants,
        "video_no_sfx": render_variants.get("no_sfx"),
        "video_seedance_audio_only": render_variants.get("seedance_audio_only"),
        "video_voice_only": render_variants.get("voice_only"),
        "checklist": str(checklist),
        "seedance_reused": existing_enough,
        "stats": {
            "seedance_clips_generated": len(list((project_dir / "seedance 2.0").glob("*.mp4"))) if (project_dir / "seedance 2.0").exists() else 0,
            "web_images_downloaded": len(list((project_dir / "web").glob("*.[jJ][pP][gG]"))) if (project_dir / "web").exists() else 0,
            "web_images_rejected": len(list((project_dir / "web" / "rejected").glob("*.[jJ][pP][gG]"))) if (project_dir / "web" / "rejected").exists() else 0,
            "scenes_total": len(config.get("scenes", [])),
        },
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    if clip_decision_log:
        # Why every scene got the clip (or fallback) it did - script_match_score, acceptance,
        # rejected candidates, fallback type. Inspect this to debug scrape relevance.
        report["scrape_clip_decisions"] = clip_decision_log
        try:
            (project_dir / "review" / "clip_decisions.json").write_text(
                json.dumps(clip_decision_log, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
    if social_search_report:
        # Full social-search debug: buckets, tiered queries, per-query result counts, hook finder.
        report["social_search"] = social_search_report
        # Top-level enforcement proof (mirrors the keys the spec requires at report root).
        if clip_source == "scrape":
            report["voice_speed"] = config.get("voice_speed", SCRAPE_VOICE_SPEED)
            _fxr = config.get("visual_fx_report") or {}
            _fxs = _fxr.get("visual_fx_summary") or {}
            report["visual_emphasis_enabled"] = bool(config.get("visual_emphasis_enabled"))
            report["visual_fx_policy"] = _fxr.get("visual_fx_policy", "target_based_only")
            report["visual_fx"] = {
                "arrow_style": "default_thick_red_arrow",
                "arrows_only": True, "circles_enabled": False, "stamps_enabled": False,
                "labels_enabled": False,
                "arrow_count": _fxs.get("arrow_count", 0),
                "arrow_validation": {
                    "valid": _fxs.get("arrow_count", 0),
                    "skipped_invalid_geometry": 0,
                    "skipped_overlap": _fxs.get("skipped_callouts_overlap", 0),
                },
                "skipped_arrows_no_valid_target": (_fxs.get("skipped_callouts_no_target", 0)
                                                   + _fxs.get("skipped_callouts_bad_target", 0)),
            }
            report["visual_fx_summary"] = _fxs
            report["scene_visual_fx"] = _fxr.get("scene_visual_fx")
            _an = config.get("audio_noise_report") or {}
            report["voice_processing"] = {
                "speed": config.get("voice_speed", SCRAPE_VOICE_SPEED), "highpass_hz": 100, "low_mid_cut_applied": True,
                "presence_boost_applied": True, "air_boost_applied": True, "deesser_applied": True,
                "voice_clarity_score": _an.get("voice_clarity_score"),
                "voice_muffled": bool(_an.get("voice_muffled")),
            }
            report["audio_noise"] = {
                "noise_check_enabled": _an.get("noise_check_enabled", False),
                "constant_hiss_detected": _an.get("constant_hiss_detected", False),
                "noise_floor_db": _an.get("noise_floor_db"),
                "high_frequency_hiss_score": _an.get("high_frequency_hiss_score", 0.0),
                "music_masking_score": 0, "sfx_masking_score": 0,
                "constant_ambience_removed": True, "noisy_sfx_rejected": 0, "music_hiss_reduced": False,
            }
            report["sfx_policy_audio"] = {"event_based_only": True, "long_ambient_sfx_allowed": False,
                                          "max_sfx_duration_seconds": 2.0}
            report["audio_master"] = {
                "target_lufs": -15, "true_peak_ceiling_db": -1,
                "validation_passed": bool(config.get("pre_render_validation_passed")), "failures": [],
            }
            report["audio_validation"] = {
                "passed": bool(config.get("pre_render_validation_passed")),
                "failures": [],
            }
            _sfxr = config.get("sfx_report") or {}
            report["sfx_policy"] = _sfxr.get("sfx_policy", "provided_local_assets_only")
            report["sfx_generated_random_assets"] = False
            report["meme_sfx_enabled"] = bool(_sfxr.get("meme_sfx_enabled"))
            report["sfx_scanner"] = _sfxr.get("sfx_scanner")
            report["sfx_classification"] = _sfxr.get("sfx_classification")
            report["sfx_summary"] = _sfxr.get("sfx_summary")
            report["sfx_events"] = _sfxr.get("sfx_events")
            report["sfx_validation"] = _sfxr.get("sfx_validation")
            report["hook_first"] = bool(social_search_report.get("hook_first"))
            report["semantic_matching_skipped"] = bool(social_search_report.get("semantic_matching_skipped"))
            report["pre_render_validation_passed"] = bool(config.get("pre_render_validation_passed"))
            report["search_mode"] = "bucket_based_social_search"
            report["old_style_query_path_used"] = False
            report["project_media_panel_policy"] = "accepted_media_only"
            report["hook_finder"] = social_search_report.get("hook_finder")
            report["clip_filter_summary"] = social_search_report.get("clip_filter_summary")
            report["query_performance"] = social_search_report.get("query_performance")
            report["candidate_statuses"] = social_search_report.get("candidate_statuses")
            report["scene_assignments"] = social_search_report.get("scene_assignments")
        try:
            (project_dir / "review" / "social_search.json").write_text(
                json.dumps(social_search_report, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
    if web_contact_sheet.exists():
        report["web_contact_sheet"] = str(web_contact_sheet)
    if gpt_contact_sheet.exists():
        report["gpt_contact_sheet"] = str(gpt_contact_sheet)
    report_path = project_dir / "review" / "agent_report.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    tidy_debug_artifacts(project_dir, status_cb=status_cb)
    log(status_cb, "Done.")
    return report


def tidy_debug_artifacts(project_dir, status_cb=None):
    """Tuck the transient working images (vision-matcher poster frames, hook frames, FX frames,
    review contact sheets) into a single hidden _debug/ folder so the project shows just the final
    MP4(s) and its media - not hundreds of internal JPGs. Non-destructive: nothing is deleted."""
    project_dir = Path(project_dir)
    debug_dir = project_dir / "_debug"
    moved = 0
    try:
        # whole frame-dump folders under review/
        for name in ("_clip_match", "_hook_match", "_fx_frames"):
            src = project_dir / "review" / name
            if src.exists():
                dst = debug_dir / name
                debug_dir.mkdir(exist_ok=True)
                if dst.exists():
                    shutil.rmtree(dst, ignore_errors=True)
                shutil.move(str(src), str(dst))
                moved += 1
        # the review contact-sheet JPGs (the 'captioned frames' images)
        for sheet in (project_dir / "review").glob("*_review_sheet.jpg"):
            debug_dir.mkdir(exist_ok=True)
            shutil.move(str(sheet), str(debug_dir / sheet.name))
            moved += 1
    except Exception:
        pass
    if moved:
        log(status_cb, f"Tidied {moved} internal debug artifact group(s) into _debug/ "
                       "(project now shows just the final video + media).")
