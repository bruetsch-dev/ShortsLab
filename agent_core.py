import json
import math
import base64
import html
import mimetypes
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import wave
from pathlib import Path

import pipeline


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
# DuckDuckGo's unofficial image endpoint constantly returns 403 / times out (bot
# blocking), which stalled every search. Bing + Wikimedia are reliable, so DDG is
# dropped from the active providers (the duckduckgo_* helpers are kept but unused).
WEB_IMAGE_SEARCH_PROVIDERS = ("bing", "wikimedia")
GPT55_MODEL = "openai/gpt-5.5"
GEMINI_AUDIO_MODEL = "google/gemini-3.5-flash"
SEEDANCE_VIDEO_MODELS = {
    "seedance-2.0": "bytedance/seedance-2.0/image-to-video-spicy",
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


def form_int(form, key, default, minimum=0, maximum=99):
    try:
        value = int(str(form.get(key, default)).strip())
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def form_choice(form, key, allowed, default):
    value = str(form.get(key, default) or default).strip()
    return value if value in set(allowed) else default


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
            if "rejected" in {part.lower() for part in path.parts}:
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


def scene_prompt(title, scene):
    visual_direction = scene.get("visual_script", "")
    voice_line = scene.get("exact_voice_text") or scene.get("voice_line") or scene["script"]
    objective = scene.get("scene_objective") or scene.get("beat_purpose") or f"Visually answer this voice line: {voice_line}"
    visual_meaning = scene.get("visual_meaning") or scene.get("required_visual_information") or voice_line
    hook_type = scene.get("visual_hook_type") or infer_visual_hook_type(voice_line)
    must_show = scene.get("must_show") or important_terms(voice_line, 5, SEARCH_NOISE)
    must_not_show = scene.get("must_not_show") or ["generic cinematic filler", "off-topic symbols", "random book cover", "caption text"]
    crop_plan = scene.get("crop_plan") or default_crop_plan(hook_type)
    prompt = (
        "Use case: historical-scene\n"
        "Asset type: vertical YouTube Short scene plate\n"
        f"Short topic: {title}\n"
        f"Topic lock: every visible subject, object, place, era, and action must clearly support this Short topic: {humanize_title(title)}.\n"
        f"Exact voice line: {voice_line}\n"
        f"Scene objective: {objective}\n"
        f"Visual meaning that must be understood: {visual_meaning}\n"
        f"Visual hook type: {hook_type}\n"
        f"Must show: {json.dumps(must_show, ensure_ascii=False)}\n"
        f"Must not show: {json.dumps(must_not_show, ensure_ascii=False)}\n"
        f"9:16 crop plan: {json.dumps(crop_plan, ensure_ascii=False)}\n"
        f"Primary request: create one realistic documentary visual that directly answers this exact voice-script beat: {voice_line}\n"
    )
    if visual_direction:
        prompt += (
            "Voice/text script priority: the script beat above is authoritative. "
            "First judge whether the visual direction actually fits this spoken line. "
            "Use it only if it makes the voice script clearer or more cinematic; adapt weak ideas, and ignore/replace anything that feels mismatched, too literal, off-topic, or less understandable than a better script-matched shot. "
            f"Visual direction: {visual_direction}\n"
        )
    prompt += (
        "Composition/framing: vertical 9:16, readable on phone, main subject clear, no embedded captions.\n"
        "Seedance I2V readiness: stage the scene with clear foreground/midground/background depth, a subject frozen at the start of a visible action, environmental elements that can move naturally, and camera parallax potential.\n"
        "Quality rule: the image must answer the voice line, not just create mood. If it would look good but not explain or intensify the spoken sentence, it is wrong.\n"
        "Style/medium: realistic cinematic mini-documentary reconstruction, serious, not meme-like.\n"
        "Constraints: one coherent scene or subject, no collage, no split-screen, no grid, no scrapbook, no many inset images, "
        "no watermark, no logo, no gore, no generated text unless the scene clearly requires a document."
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


def scene_video_prompt(scene, scene_index=None, total_scenes=None, visual_intent="", title=""):
    visual_direction = scene.get("visual_script", "")
    script_beat = str(scene.get("exact_voice_text") or scene.get("voice_line") or scene.get("script", "")).strip().rstrip(".!?")
    position = f"Scene {scene_index}/{total_scenes}. " if scene_index and total_scenes else ""
    objective = scene.get("scene_objective") or scene.get("beat_purpose") or f"Visually answer this voice line: {script_beat}"
    hook_type = scene.get("visual_hook_type") or infer_visual_hook_type(script_beat)
    camera_motion = scene.get("camera_motion") or "purposeful handheld/parallax camera motion"
    subject_motion = scene.get("subject_motion") or "script-specific visible subject action"
    environment_motion = scene.get("environment_motion") or "natural ambient motion"
    emotional_action = scene.get("emotional_action") or scene.get("emotion") or infer_emotion(script_beat)
    must_show = scene.get("must_show") or important_terms(script_beat, 5, SEARCH_NOISE)
    must_not_show = scene.get("must_not_show") or ["generic cinematic filler", "off-topic media", "caption text"]
    prompt = (
        f"Animate this vertical documentary scene for Seedance I2V. {position}Script beat: \"{script_beat}\". "
        f"The action must clearly answer this voice line: {objective}. "
        f"Must show: {', '.join(str(item) for item in must_show[:5])}. "
    )
    if visual_direction:
        prompt += f"Use this visual idea only if it fits the voice line: {visual_direction}. "
    if visual_intent:
        prompt += f"Director intent: {visual_intent}. "
    prompt += (
        f"camera_motion: {camera_motion}. "
        f"subject_motion: {subject_motion}. "
        f"environment_motion: {environment_motion}. "
        f"emotional_action: {emotional_action}. "
        f"motion_focus: {seedance_motion_focus(scene)} "
        "Make real physical motion with a clear beginning, action change, and end pose; avoid a still image with only zoom. "
        "no_speech: no speech, no voices, no dialogue, no narration, no talking, no vocalizations. "
        "no_text: no captions, subtitles, readable added text, or labels. "
        "no_logo: no watermark, logo, insignia, or brand mark. "
        "Keep it realistic, serious, vertical 9:16, and not a montage."
    )
    return prompt[:1400]


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
    try:
        visual_beats = parse_timed_script(visual_script, target_duration)
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
    for index, scene in enumerate(enriched):
        note_index = min(len(notes) - 1, int(index * len(notes) / max(1, len(enriched))))
        scene["visual_script"] = notes[note_index]
    return enriched


def scene_text_for_planning(scene):
    script = scene.get("script", "")
    visual = scene.get("visual_script", "")
    return " ".join(part for part in [script, script, visual] if part).strip()


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
def llm_micro_beat_plan(title, script, visual_script, target_duration, base_scenes, reasoning_model=None, status_cb=None):
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
        "max_tokens": 4200,
        "response_format": {"type": "json_object"},
    }
    try:
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


def build_micro_beat_plan(title, script, visual_script, target_duration, base_scenes, reasoning_model=None, status_cb=None):
    result = llm_micro_beat_plan(title, script, visual_script, target_duration, base_scenes, reasoning_model=reasoning_model, status_cb=status_cb)
    beats = result.get("micro_beats", [])
    if not beats:
        beats = fallback_micro_beat_plan(title, script, target_duration, base_scenes=base_scenes)
        log(status_cb, f"Micro-Beat Planner fallback: created {len(beats)} beat(s).")
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


def plan_config(project_dir, title, script, target_duration, allow_seedance=True, max_seedance=3, static_gpt_image_count=4, scenes_override=None, director_plan=None, visual_sections=None, speaker_hook_enabled=False):
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
            "prompt": scene_prompt(title, scene) + (f"\nDirector visual intent: {visual_intent}" if visual_intent else ""),
            "video_prompt": scene_video_prompt(scene, index, len(scenes), visual_intent=visual_intent, title=title),
            "motion": scene_motion,
            "needs_gpt_asset": wants_gpt_asset,
        }
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
        "render_captions": False,
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
        "global_constraints": "No embedded captions, no watermark, no logo, no gore. GPT images are only Seedance I2V source images and must never appear as static stills in the final render. Avoid collage/multi-panel layouts except for at most 1-2 deliberate document-board images across the Short.",
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
            "video_generate_audio": True,
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
    seen_static_assets = set()
    seen_seedance_clips = set()
    for index, scene in enumerate(config.get("scenes", []), 1):
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
        match = re.search(r"\{.*\}", text, flags=re.S)
        if not match:
            raise
        return json.loads(match.group(0))


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
    global LAST_WEB_REQUEST_AT
    elapsed = time.monotonic() - LAST_WEB_REQUEST_AT
    if elapsed < min_interval:
        time.sleep(min_interval - elapsed)
    req = urllib.request.Request(url, headers={"User-Agent": "autonomous-shorts-agent/1.1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    finally:
        LAST_WEB_REQUEST_AT = time.monotonic()


def request_text_url(url, timeout=45, min_interval=1.1, referer=""):
    global LAST_WEB_REQUEST_AT
    elapsed = time.monotonic() - LAST_WEB_REQUEST_AT
    if elapsed < min_interval:
        time.sleep(min_interval - elapsed)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json;q=0.8,*/*;q=0.7",
        "Accept-Language": "en-US,en;q=0.9,de;q=0.8",
    }
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            return response.read().decode(charset, errors="replace")
    finally:
        LAST_WEB_REQUEST_AT = time.monotonic()


def post_json_url(url, payload, timeout=75):
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
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


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
        counts[key] = len([p for p in folder.rglob("*") if p.suffix.lower() in exts and p.stat().st_size > 1000])
    return counts


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
        "max_tokens": 2400,
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
    first["seedance"] = True
    first["asset"] = asset.name
    first["clip"] = "speaker_hook.mp4"
    first["needs_gpt_asset"] = False
    first["prompt"] = (
        "Uploaded speaker image used as Seedance I2V source for the opening hook. "
        f"Hook line: {plan.get('hook_line') or first_hook_line(script)}"
    )
    first["video_prompt"] = clean_text(plan.get("seedance_prompt") or "")
    first["video_model"] = "bytedance/seedance-2.0/image-to-video-spicy"
    first["video_resolution"] = "480p"
    first["video_enable_web_search"] = True
    first["max_duration"] = 9.0
    first["shots"] = [{"at": 0.0, "use_clip": True}]
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
        "prompt": first["video_prompt"],
        "recreated": bool(recreate),
    }
    log(status_cb, "Speaker hook: configured first scene for Seedance speaker clip.")
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
        data = request_json_url(url, min_interval=1.25)
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
    text = request_text_url(url, min_interval=1.3)
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


def download_url_to_file(url, path, referer=""):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,de;q=0.8",
    }
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=90) as response:
        content_type = (response.headers.get("Content-Type") or "").lower()
        if content_type and "image/" not in content_type and "octet-stream" not in content_type:
            raise RuntimeError(f"URL did not return an image content type: {content_type}")
        path.write_bytes(response.read())
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


def clamp_web_image_target(count):
    try:
        value = int(count)
    except (TypeError, ValueError):
        value = 12
    return max(10, min(15, value))


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
    for pass_no in range(1, 6):
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
        raise RuntimeError(
            f"Reasoning Agent accepted only {len(final_paths)} web image(s), below the required {minimum_required}. "
            "Render stopped so the app does not cut with off-topic web media. Try a more specific title/script or rerun web search."
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


def llm_video_review(title, script, visual_script, config, video_path, scene_sheet, shot_sheet, reasoning_model=None, status_cb=None, pass_label="initial"):
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
    
    # Process structured issues
    for issue in review.get("issues", []) or []:
        if isinstance(issue, dict) and str(issue.get("severity", "")).lower() in {"medium", "high", "critical"}:
            scene_value = str(issue.get("scene", "")).strip()
            if scene_value:
                scene_targets.add(scene_value)
                
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
    for scene in corrected.get("scenes", []):
        if scene.get("speaker_hook"):
            continue
        targeted = not scene_targets or any(scene_id_matches(scene, target) for target in scene_targets)
        if targeted:
            changed = calm_scene_motion(scene, force_contain=force_all_contain) or changed
    return corrected, changed


def create_no_audio_variant(video_path, status_cb=None):
    video_path = Path(video_path)
    ffmpeg = pipeline.find_ffmpeg()
    if not ffmpeg or not video_path.exists():
        return None
    out = video_path.with_name(f"{video_path.stem}_no_audio{video_path.suffix}")
    cmd = [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-i",
        str(video_path),
        "-map",
        "0:v:0",
        "-c:v",
        "copy",
        "-an",
        str(out),
    ]
    try:
        subprocess.run(cmd, check=True)
        log(status_cb, f"Saved render variant without audio: {out.name}")
        return out
    except Exception as exc:
        log(status_cb, f"No-audio render variant skipped: {exc}")
        return None


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
    no_audio = create_no_audio_variant(final_output, status_cb=status_cb)
    if no_audio:
        variants["no_audio"] = str(no_audio)
    no_sfx = render_audio_variant(
        config,
        "no_sfx",
        status_cb=status_cb,
        sfx_enabled=False,
    )
    if no_sfx:
        variants["no_sfx"] = str(no_sfx)
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
    for scene_index, scene in enumerate(scenes, 1):
        if len(downloaded) >= target_count:
            break
        scene_downloads = 0
        candidates = []
        for query in web_queries_for_scene(title, scene, scene_index=scene_index, profile=profile):
            if query.lower() in searched_queries:
                continue
            searched_queries.add(query.lower())
            if len(downloaded) + scene_downloads >= target_count:
                break
            log(status_cb, f"Scene {scene_index}: general web image query '{query}'")
            try:
                results = general_search_images(query, limit=18, status_cb=status_cb)
            except Exception as exc:
                log(status_cb, f"Search failed for '{query}': {exc}")
                continue
            for result in results:
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


def run_project(form, status_cb=None):
    check_cancel(form)
    script = clean_text(form.get("script", ""))
    reasoning_model = form.get("reasoning_model", "openai/gpt-5.5")
    
    title = form.get("title", "").strip()
    if not title and script:
        title = llm_generate_project_title(script, status_cb=status_cb)
    if not title:
        title = f"Short {int(time.time())}"
        
    requested_slug = form.get("slug", "").strip()
    slug = slugify(requested_slug or title)
    if not requested_slug:
        slug = unique_project_slug(slug)
    project_dir = PROJECTS_DIR / slug
    for folder in ["seedance 2.0", "gpt images", "web images", "input", "config", "renders", "review", "local media", "speaker", "speaker clip"]:
        (project_dir / folder).mkdir(parents=True, exist_ok=True)
    log(status_cb, f"PROJECT_DIR|{project_dir}")

    visual_script = clean_text(form.get("visual_script", ""))
    audio_path = safe_copy_audio(form.get("audio_path", ""), project_dir)
    speaker_image_path = form.get("speaker_image_path", "")
    audio_analysis = None
    word_timeline_cache = None
    audio_duration = probe_audio_duration(audio_path) if audio_path else None

    if audio_path and form.get("use_audio_timing", "on") == "on":
        if audio_duration:
            log(status_cb, f"Measured speech audio duration: {audio_duration:.2f}s")
        # Primary timing source: local frame-accurate forced alignment (faster-whisper).
        try:
            import voice_align
            if voice_align.available():
                log(status_cb, "Aligning script to voice (faster-whisper) for frame-accurate timing...")
                audio_analysis, word_timeline_cache = voice_align.analysis_from_audio(
                    audio_path, script_text=script, duration=audio_duration, status_cb=status_cb,
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

    if not title:
        title = llm_generate_project_title(script, reasoning_model=reasoning_model, status_cb=status_cb)
        if title:
            log(status_cb, f"Updating project title to: {title}")
        else:
            title = f"Short {int(time.time())}"
            
        # Update config with new title if it was generated late
        if title:
            # We already created the directory, but the title in config should be accurate
            pass

    (project_dir / "input" / "script.txt").write_text(script, encoding="utf-8")
    if visual_script:
        (project_dir / "input" / "visual_script.txt").write_text(visual_script, encoding="utf-8")

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
    micro_beat_result = build_micro_beat_plan(title, script, visual_script, target_duration, base_scenes, reasoning_model=reasoning_model, status_cb=status_cb)
    micro_beat_scenes = micro_beat_result["micro_beats"]
    visual_sections = micro_beat_result["visual_sections"]
    scenes_override = apply_visual_script_to_scenes(micro_beat_scenes, visual_script, target_duration)
    log(status_cb, f"Using {len(scenes_override)} micro-beat(s) as the edit map.")
    if visual_script:
        log(status_cb, "Visual Ablauf prompt applied to scene planning, image prompts, Seedance prompts, and review.")
    web_image_count = 12
    web_images_per_scene = 2
    static_gpt_image_count = 0
    seedance_clip_count = 5
    seedance_model_choice = form.get("video_model", "seedance-2.0")
    auto_web_images = form.get("auto_web_images", "on") == "on"
    manual_auto_web_images = auto_web_images
    allow_gpt = form.get("allow_gpt", "on") == "on"
    allow_seedance = form.get("allow_seedance", "on") == "on"
    use_llm_search = form.get("use_llm_search", form.get("use_glm_search", "on")) == "on"
    use_llm_video_review = form.get("use_llm_video_review", "on") == "on"
    background_music_enabled = form.get("background_music_enabled", "") == "on"
    autonomous_director = form.get("autonomous_director", "on") == "on"
    loaded_project_mode = form_choice(
        form,
        "loaded_project_mode",
        {"normal", "recut_existing_only", "recut_new_web_images", "recut_regenerate_seedance", "recut_recreate_speaker_clip"},
        "normal",
    )
    recut_mode = loaded_project_mode if loaded_project_mode != "normal" and requested_slug else "normal"
    speaker_hook_enabled = form.get("enable_speaker_hook", "") == "on" or recut_mode == "recut_recreate_speaker_clip"
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
    if manual_auto_web_images and not auto_web_images:
        auto_web_images = True
        log(status_cb, "Web image search kept enabled by UI setting; every automatic run keeps a 10-15 web-image pool.")
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
    if auto_web_images:
        original_web_image_count = web_image_count
        web_image_count = clamp_web_image_target(web_image_count)
        if web_image_count != original_web_image_count:
            log(status_cb, f"Web image target adjusted to {web_image_count}; every run keeps a 10-15 image pool.")
        web_candidate_count = web_candidate_pool_target(web_image_count)
        log(status_cb, f"Web image pipeline: collecting {web_candidate_count} candidates; Reasoning Agent visual review will choose the final {web_image_count}.")
        check_cancel(form)
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
    max_seedance = seedance_clip_count
    wavespeed_key = os.environ.get("WAVESPEED_API_KEY", "")

    check_cancel(form)
    log(status_cb, "Planning scenes and media...")
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
    )
    config["project_slug"] = slug
    config["loaded_project_mode"] = recut_mode
    config.setdefault("wavespeed", {})
    config["wavespeed"]["seedance_model"] = seedance_model_choice
    config["wavespeed"]["video_model"] = SEEDANCE_VIDEO_MODELS.get(seedance_model_choice)
    config["wavespeed"]["video_resolution"] = "720p" if seedance_model_choice == "happyhorse-1.1" else "480p"
    config["wavespeed"]["video_enable_web_search"] = (seedance_model_choice == "seedance-2.0")
    config["wavespeed"]["reasoning_model"] = form.get("reasoning_model", "openai/gpt-5.5")
    config["background_music_enabled"] = background_music_enabled
    config["background_music_user_enabled"] = background_music_enabled
    log(status_cb, f"Seedance model selected: {seedance_model_choice}.")
    log(status_cb, f"Background music: {'enabled' if background_music_enabled else 'disabled'}.")
    if recut_mode == "normal":
        config["output_basename"] = f"{slug}_auto_short"
    else:
        config["output_basename"] = f"{slug}_{recut_mode}_{recut_stamp}"
        config["recut_stamp"] = recut_stamp
        config["recut_uses_existing_media_only"] = recut_mode == "recut_existing_only"
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
        mix_voice = str(form.get("mix_voice_in_final", "on")).lower() == "on"
        if mix_voice:
            config["audio_path"] = str(audio_path)
            config["speech_audio_in_final"] = True
            log(status_cb, "Uploaded voice is the primary audio; music and SFX are ducked under it.")
        else:
            config["audio_path"] = None
            config["speech_audio_in_final"] = False
            log(status_cb, "Uploaded speech audio is used for timing only; it will not be mixed into the final video.")
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
                timeline = voice_align.word_timeline(str(audio_path), script_text=script, status_cb=status_cb)
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
    config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
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
    log(status_cb, "Rendering final 9:16 MP4...")
    output = pipeline.render_video(config)
    check_cancel(form)
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
    if use_llm_video_review:
        review_rounds = [
            {
                "number": 1,
                "label": "pass_1_of_2",
                "review_name": "llm_video_review.json",
                "config_name": "project_llm_corrected.json",
                "basename": f"{slug}_auto_short_llm_corrected",
                "scene_label": "Corrected scene review sheet",
                "shot_label": "Corrected shot review sheet",
            },
            {
                "number": 2,
                "label": "pass_2_of_2",
                "review_name": "llm_video_review_pass_2.json",
                "config_name": "project_llm_corrected_2.json",
                "basename": f"{slug}_auto_short_llm_corrected_2",
                "scene_label": "Second corrected scene review sheet",
                "shot_label": "Second corrected shot review sheet",
            },
        ]
        for review_round in review_rounds:
            check_cancel(form)
            log(status_cb, f"Reasoning Agent review pass {review_round['number']}/2 starting...")
            review = llm_video_review(
                title,
                script,
                visual_script,
                config,
                output,
                scene_sheet,
                shot_sheet,
                reasoning_model=reasoning_model,
                status_cb=status_cb,
                pass_label=review_round["label"],
            )
            if not review:
                log(status_cb, f"Reasoning Agent review pass {review_round['number']}/2 did not return a usable review.")
                continue
            llm_review_passes += 1
            review_path = project_dir / "review" / review_round["review_name"]
            review_path.write_text(json.dumps(review, indent=2), encoding="utf-8")
            llm_review_pass_paths.append(str(review_path))
            if review_round["number"] == 1:
                llm_review_path = review_path
            elif review_round["number"] == 2:
                llm_corrected_review_path = review_path
            corrected_config, changed = apply_llm_corrections(config, review)
            if changed:
                llm_correction_passes += 1
                corrected_by_gpt55 = True
                if review_round["number"] == 1:
                    log(status_cb, "Reasoning Agent requested correction; rendering corrected version...")
                else:
                    log(status_cb, "Reasoning Agent second review requested correction; rendering second corrected version...")
                corrected_config["output_basename"] = review_round["basename"]
                corrected_config_path = project_dir / "config" / review_round["config_name"]
                corrected_config_path.write_text(json.dumps(config_for_json(corrected_config), indent=2), encoding="utf-8")
                corrected_config["_config_path"] = str(corrected_config_path.resolve())
                attach_cancel_event(corrected_config, form)
                corrected_config["_status_cb"] = status_cb
                corrected_config = enforce_unique_media_per_render(corrected_config, status_cb=status_cb)
                corrected_config_path.write_text(json.dumps(config_for_json(corrected_config), indent=2), encoding="utf-8")
                check_cancel(form)
                missing_review_assets = config_missing_gpt_assets(corrected_config)
                if missing_review_assets:
                    log(status_cb, f"Reasoning Agent correction: skipped {len(missing_review_assets)} missing GPT still request(s); review corrections cannot generate new GPT still media.")
                output = pipeline.render_video(corrected_config)
                config = corrected_config
                scene_sheet, checklist = pipeline.create_review(config, output)
                log_preview(status_cb, review_round["scene_label"], scene_sheet)
                shot_sheet = pipeline.create_shot_review(config, output)
                log_preview(status_cb, review_round["shot_label"], shot_sheet)
            elif review.get("needs_correction"):
                log(status_cb, f"Reasoning Agent review pass {review_round['number']}/2 found issues, but no safe render-only correction was available.")
            else:
                log(status_cb, f"Reasoning Agent review pass {review_round['number']}/2 accepted the render.")
    else:
        log(status_cb, "Skipping Reasoning Agent video review; option disabled.")

    check_cancel(form)
    log(status_cb, "Saving audio render variants...")
    render_variants = create_render_variants(config, output, status_cb=status_cb)

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
        "metadata_cleanup": metadata_cleanup_result,
        "render_variants": render_variants,
        "video_no_audio": render_variants.get("no_audio"),
        "video_no_sfx": render_variants.get("no_sfx"),
        "video_seedance_audio_only": render_variants.get("seedance_audio_only"),
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
    if web_contact_sheet.exists():
        report["web_contact_sheet"] = str(web_contact_sheet)
    if gpt_contact_sheet.exists():
        report["gpt_contact_sheet"] = str(gpt_contact_sheet)
    report_path = project_dir / "review" / "agent_report.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    log(status_cb, "Done.")
    return report
