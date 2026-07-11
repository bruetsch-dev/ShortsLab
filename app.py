import argparse
import datetime
import faulthandler
import html
import json
import os
import re
import shutil
import sys
import threading
import time
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# --------------------------------------------------------------------------- crash logging
# The launcher runs the app with a hidden window and no stderr redirection, so a crash used to
# vanish with no trace. Persist everything: faulthandler dumps native faults (onnxruntime/opencv
# segfaults, C-level aborts) and the excepthooks below capture uncaught exceptions in ANY thread
# (the job workers are daemon threads - their uncaught errors would otherwise be silent).
_CRASH_LOG = Path(__file__).resolve().parent / "logs" / "crash.log"
try:
    _CRASH_LOG.parent.mkdir(parents=True, exist_ok=True)
    _CRASH_FH = open(_CRASH_LOG, "a", buffering=1, encoding="utf-8", errors="replace")
    faulthandler.enable(file=_CRASH_FH, all_threads=True)
except Exception:
    _CRASH_FH = None


def _log_crash(kind, exc_type, exc_value, exc_tb, thread_name=None):
    try:
        stamp = datetime.datetime.now().isoformat(timespec="seconds")
        who = f" [{thread_name}]" if thread_name else ""
        header = f"\n===== {kind}{who} @ {stamp} =====\n"
        body = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        (_CRASH_FH or sys.stderr).write(header + body)
        if _CRASH_FH:
            _CRASH_FH.flush()
    except Exception:
        pass


def _main_excepthook(exc_type, exc_value, exc_tb):
    _log_crash("UNCAUGHT (main)", exc_type, exc_value, exc_tb)
    sys.__excepthook__(exc_type, exc_value, exc_tb)


def _thread_excepthook(args):
    _log_crash("UNCAUGHT (thread)", args.exc_type, args.exc_value, args.exc_traceback,
               thread_name=getattr(args.thread, "name", None))


sys.excepthook = _main_excepthook
try:
    threading.excepthook = _thread_excepthook   # Python 3.8+
except Exception:
    pass

import agent_core
import chat_ui
import pipeline
import sfx_agent
import caption_agent
import visual_agent
import viral_transformation
import reasoning_modes
import scrape_browser_preview
try:
    from reddit_story_mode import story_generator as reddit_stories
    from reddit_story_mode import orchestrator as reddit_orchestrator
except Exception:  # keep the main app working even if the optional mode fails to import
    reddit_stories = None
    reddit_orchestrator = None


def snapshot_timeline_version(slug, reason):
    """Persist the complete editable timeline state before a destructive rework."""
    project_dir = safe_project_dir(slug)
    if not project_dir:
        raise RuntimeError("Unknown project.")
    config_dir = project_dir / "config"
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    version_dir = config_dir / "timeline_versions" / stamp
    version_dir.mkdir(parents=True, exist_ok=False)
    source = config_dir / "project.json"
    if not source.exists():
        config = agent_core.load_project_config(slug)
        source.write_text(json.dumps(agent_core.config_for_json(config), indent=2,
                                     ensure_ascii=False), encoding="utf-8")
    shutil.copy2(source, version_dir / "project.json")
    edits = config_dir / "timeline_edits.json"
    if edits.exists():
        shutil.copy2(edits, version_dir / "timeline_edits.json")
    metadata = {"id": stamp, "reason": str(reason),
                "created_at": datetime.datetime.now().isoformat(timespec="seconds")}
    (version_dir / "version.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    return metadata


def timeline_versions(slug):
    project_dir = safe_project_dir(slug)
    root = project_dir / "config" / "timeline_versions" if project_dir else None
    rows = []
    for folder in (sorted(root.iterdir(), reverse=True) if root and root.exists() else []):
        try:
            row = json.loads((folder / "version.json").read_text(encoding="utf-8"))
            if (folder / "project.json").exists():
                rows.append(row)
        except Exception:
            continue
    return rows


def restore_timeline_version(slug, version_id):
    project_dir = safe_project_dir(slug)
    if not project_dir or not re.fullmatch(r"[0-9_]+", str(version_id or "")):
        raise RuntimeError("Unknown timeline version.")
    config_dir = project_dir / "config"
    source = config_dir / "timeline_versions" / str(version_id)
    if not (source / "project.json").exists():
        raise RuntimeError("Timeline version no longer exists.")
    snapshot_timeline_version(slug, "Before restoring an older version")
    shutil.copy2(source / "project.json", config_dir / "project.json")
    old_edits = source / "timeline_edits.json"
    current_edits = config_dir / "timeline_edits.json"
    if old_edits.exists():
        shutil.copy2(old_edits, current_edits)
    elif current_edits.exists():
        current_edits.unlink()


ROOT = Path(__file__).resolve().parent
SPEAKER_GALLERY_DIR = ROOT / "speaker images"
SPEAKER_UPLOADED_DIR = ROOT / "speaker" / "uploaded"
SPEAKER_GALLERY_DIR.mkdir(exist_ok=True)
SPEAKER_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
JOBS = {}
JOB_LOCK = threading.Lock()
DEV_TRAINER_LOCK = threading.Lock()
DEV_TRAINER_SERVERS = {}
# TikTok + X logins (the clip-scrape backends, no API keys): a single headed login at a
# time per platform. busy = a login window is open and we're waiting for the user to sign in.
TIKTOK_LOGIN = {"busy": False, "thread": None}
TWITTER_LOGIN = {"busy": False, "thread": None}
HIGGSFIELD_LOGIN = {"busy": False, "thread": None, "error": ""}
TIKTOK_LOCK = threading.Lock()
HIGGSFIELD_LOCK = threading.Lock()
STATIC_DIR = ROOT / "static"
UI_STATE_PATH = ROOT / "ui_state.json"
UI_TEXT_DEFAULTS = {
    "script": "",
    "visual_script": "",
    "title": "The Great Emu War",
    "slug": "",
    "web_image_count": "14",
    "web_images_per_scene": "2",
    "static_gpt_image_count": "0",
    "seedance_clip_count": "4",
    "seedance_model": "seedance-2.0",
    "reasoning_model": "openai/gpt-5.5",
    "reasoning_mode": "medium",
    "loaded_project_mode": "normal",
    "run_type": "normal",
    "speaker_name": "Narrator",
    "tts_voice": "Achernar",
    "tts_model": "flash",
    "image_model": "openai/gpt-image-2/text-to-image",
    "hook_text": "",
    "impact_word": "",
    "hook_pause_s": "0.45",
    "speaker_image_path": "",
    "clip_source": "generate",
    "scrape_platforms": "tiktok,x",
    "scrape_terms": "",
    "background_music_choice": "none",
    "script_relevancy": "70",
    "scrape_sort": "ALL",
    "scraping_engine": "v2",
    "sfx_amount": "medium",
    "scrape_cookies": "",
    "scrape_cookies_file": "",
}
UI_CHECKBOX_DEFAULTS = {
    "autonomous_director": True,
    "use_audio_timing": True,
    "mix_voice_in_final": True,
    "use_llm_search": True,
    "use_llm_video_review": True,
    "enable_speaker_hook": False,
    "auto_web_images": True,
    "background_music_enabled": False,
    "generate_missing_sfx": True,
    "allow_gpt": True,
    "allow_seedance": True,
    "use_visual_direction": True,
    "halt_after_speech": False,
    # per-output on/off toggles (default everything ON)
    "out_web_images": True,
    "out_wikimedia": True,
    "out_gpt_images": True,
    "out_video_clips": True,
    "out_sfx": True,
    "out_transition_sfx": True,
    "out_background_music": False,
    "out_captions": True,
}
UI_PERSIST_SKIP_FIELDS = {"slug", "loaded_project_mode"}


class RunCancelled(RuntimeError):
    pass


def esc(value):
    return html.escape(str(value or ""), quote=True)


def _svg(path, size=18):
    return (f'<svg class="ico" width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" '
            f'stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" '
            f'aria-hidden="true">{path}</svg>')


ICON_NEW = _svg('<path d="M12 5v14"/><path d="M5 12h14"/>')
ICON_FOLDER = _svg('<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>')
ICON_SFX = _svg('<path d="M11 5 6 9H3v6h3l5 4z"/><path d="M16 9a4 4 0 0 1 0 6"/><path d="M19 6.5a8 8 0 0 1 0 11"/>')
ICON_GRID = _svg('<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/>')
ICON_SCRIPT = _svg('<path d="M8 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8l-5-5z"/><path d="M14 3v5h5"/><path d="M8 13h8"/><path d="M8 17h6"/>')
ICON_CHILL = _svg('<path d="M4 8h13v4a5 5 0 0 1-5 5H9a5 5 0 0 1-5-5z"/><path d="M17 9h2a2 2 0 0 1 0 4h-2"/><path d="M8 2.5c0 1-1 1.5-1 2.5"/><path d="M12 2.5c0 1-1 1.5-1 2.5"/>')
ICON_UPLOAD = _svg('<path d="M12 16V4"/><path d="m7 9 5-5 5 5"/><path d="M5 20h14"/>')
ICON_ARROW = _svg('<path d="M5 12h14"/><path d="m13 6 6 6-6 6"/>', 15)
ICON_BACK = _svg('<path d="M19 12H5"/><path d="m11 18-6-6 6-6"/>')


def top_nav():
    """One consistent top nav, identical on every page."""
    return (
        '<nav class="nav-actions">'
        f'<a class="button" href="/?new=1">{ICON_NEW}<span>New project</span></a>'
        f'<a class="button secondary" href="/assets">{ICON_GRID}<span>Assets</span></a>'
        '<span class="nav-sep" aria-hidden="true"></span>'
        '<button type="button" class="theme-toggle" onclick="toggleTheme()" title="Toggle dark mode" aria-label="Toggle dark mode">'
        '<span class="tt-knob"><span class="tt-sun">&#9728;</span><span class="tt-moon">&#9789;</span></span></button>'
        '</nav>'
    )


STEPS_HTML = (
    '<div class="steps" aria-label="script then chill then upload">'
    f'<span class="step">{ICON_SCRIPT}<span>script</span></span>'
    f'<span class="arrow">{ICON_ARROW}</span>'
    f'<span class="step">{ICON_CHILL}<span>chill</span></span>'
    f'<span class="arrow">{ICON_ARROW}</span>'
    f'<span class="step">{ICON_UPLOAD}<span>upload</span></span>'
    '</div>'
)


def brand_header(back=False):
    """The one shared top header — identical Shortslab brand on every page.

    `back=True` adds a subtle arrow-only back control on the far left (timeline).
    """
    back_html = (
        '<button type="button" class="back-arrow" aria-label="Back" title="Back" '
        'onclick="(history.length&gt;1)?history.back():(location.href=\'/assets\')">' + ICON_BACK + '</button>'
    ) if back else ""
    return (
        '<div class="top">'
        '<div class="top-left">'
        f'{back_html}'
        '<div class="brand">'
        '<img class="brand-mark" src="/static/app_icon.png" alt="" width="52" height="52">'
        f'<div><h1>Shortslab</h1></div>'
        '</div>'
        '</div>'
        f'{top_nav()}'
        '</div>'
    )


ICON_HELP = _svg('<circle cx="12" cy="12" r="10"/><path d="M9.1 9a3 3 0 0 1 5.8 1c0 2-3 2-3 4"/><path d="M12 17h.01"/>', 15)
ICON_CLOSE = _svg('<path d="M18 6 6 18"/><path d="m6 6 12 12"/>', 14)


def help_tip(text):
    """A small question-mark whose explanation only shows on hover/focus."""
    return f'<span class="help" tabindex="0" data-tip="{esc(text)}" aria-label="{esc(text)}">{ICON_HELP}</span>'


PRESETS_PATH = ROOT / "presets.json"
CUSTOM_PRESET_PATH = ROOT / "custom_preset.json"   # legacy single file (migrated in)

# Read-only built-in preset: "found-footage" style like the Japan dark-facts short —
# ALL AI generation off, clips come from a TikTok/Instagram scrape, loose relevancy.
BUILTIN_PRESETS = {
    "\U0001F1EF\U0001F1F5 Facts about Japan": {
        "speaker_name": "Narrator", "tts_voice": "Charon", "tts_model": "pro",
        "video_model": "seedance-2.0", "image_model": "openai/gpt-image-2/text-to-image",
        "reasoning_model": "openai/gpt-5.5",
        "speaker_image_path": "", "visual_script": "",
        "use_visual_direction": False, "enable_speaker_hook": False,
        # no generated images — the video layer is filled by scraped real clips
        "out_web_images": False, "out_wikimedia": False, "out_gpt_images": False,
        "out_video_clips": True, "out_sfx": True, "out_transition_sfx": True,
        "out_background_music": True, "out_captions": True, "halt_after_speech": False,
        "clip_source": "scrape", "scrape_platforms": "tiktok,x",
        "scrape_terms": "japan, japanese women, tokyo street style, salaryman commute, japan daily life, kimono",
        "script_relevancy": "30",
    },
}


def load_presets():
    """User-saved presets as {name: data}. Migrates the legacy single file once."""
    out = {}
    try:
        if PRESETS_PATH.exists():
            data = json.loads(PRESETS_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                out = {str(k): v for k, v in data.items() if isinstance(v, dict)}
    except Exception:
        out = {}
    if not out and CUSTOM_PRESET_PATH.exists():
        try:
            legacy = json.loads(CUSTOM_PRESET_PATH.read_text(encoding="utf-8"))
            if isinstance(legacy, dict) and legacy:
                out = {"My preset": legacy}
                _write_presets(out)
        except Exception:
            pass
    return out


def _write_presets(presets):
    tmp = PRESETS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(presets, indent=2), encoding="utf-8")
    tmp.replace(PRESETS_PATH)


def save_named_preset(name, data):
    name = str(name or "").strip()[:60]
    if not name or not isinstance(data, dict) or name in BUILTIN_PRESETS:
        return False
    try:
        presets = load_presets()
        presets[name] = data
        _write_presets(presets)
        return True
    except Exception:
        return False


def delete_named_preset(name):
    name = str(name or "").strip()
    try:
        presets = load_presets()
        if name in presets:
            del presets[name]
            _write_presets(presets)
            return True
    except Exception:
        pass
    return False


# These were the old "Advanced options" toggles. They are always on in practice,
# so the panel is gone and the values ride along as hidden inputs (behaviour kept).
ADVANCED_ALWAYS_ON = (
    "autonomous_director", "use_audio_timing", "use_llm_search",
    "use_llm_video_review", "auto_web_images", "generate_missing_sfx",
    "allow_gpt", "allow_seedance",
)


def advanced_hidden_inputs(state):
    parts = []
    for key in ADVANCED_ALWAYS_ON:
        on = "on" if state.get(key, True) else ""
        parts.append(f'<input type="hidden" name="{key}" value="{on}">')
    # background music keeps its persisted value (off by default)
    bg = "on" if state.get("background_music_enabled") else ""
    parts.append(f'<input type="hidden" name="background_music_enabled" value="{bg}">')
    return "".join(parts)


def normalize_ui_state(raw):
    state = dict(UI_TEXT_DEFAULTS)
    state.update(UI_CHECKBOX_DEFAULTS)
    if isinstance(raw, dict):
        for key in UI_TEXT_DEFAULTS:
            if key in UI_PERSIST_SKIP_FIELDS:
                continue
            if key in raw and raw[key] is not None:
                value = str(raw[key])
                # Normalize line endings so CRLF doesn't accumulate across save/load
                # round-trips (was injecting extra blank paragraphs in the script).
                value = value.replace("\r\n", "\n").replace("\r", "\n")
                state[key] = value
        for key in UI_CHECKBOX_DEFAULTS:
            if key in raw:
                value = raw[key]
                if isinstance(value, str):
                    state[key] = value.lower() in {"1", "true", "yes", "on", "checked"}
                else:
                    state[key] = bool(value)
    return state


def load_ui_state():
    if not UI_STATE_PATH.exists():
        return normalize_ui_state({})
    try:
        return normalize_ui_state(json.loads(UI_STATE_PATH.read_text(encoding="utf-8")))
    except Exception:
        return normalize_ui_state({})


def save_ui_state(raw):
    state = normalize_ui_state(raw)
    tmp = UI_STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(UI_STATE_PATH)
    return state


def ui_state_from_form_fields(fields):
    state = load_ui_state()
    for key in UI_TEXT_DEFAULTS:
        if key in UI_PERSIST_SKIP_FIELDS:
            continue
        if key in fields:
            state[key] = fields[key]
    for key in UI_CHECKBOX_DEFAULTS:
        value = fields.get(key)
        state[key] = str(value).lower() in {"1", "true", "yes", "on", "checked"}
    return state


def safe_project_dir(slug):
    if not slug:
        return None
    projects_root = agent_core.PROJECTS_DIR.resolve()
    candidate = (projects_root / Path(slug).name).resolve()
    if projects_root not in [candidate, *candidate.parents] or not candidate.exists() or not candidate.is_dir():
        return None
    return candidate


def read_json_file(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return {}


def read_text_file(path):
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace").strip()
    except Exception:
        return ""


def list_previous_projects(limit=80):
    projects_root = agent_core.PROJECTS_DIR
    if not projects_root.exists():
        return []
    projects = [path for path in projects_root.iterdir() if path.is_dir()]
    projects.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return projects[:limit]


def project_title_from_files(project_dir):
    report = read_json_file(project_dir / "review" / "agent_report.json")
    if report.get("title"):
        return str(report["title"])
    config = read_json_file(project_dir / "config" / "project.json")
    if config.get("title"):
        return str(config["title"])
    run_form = read_json_file(project_dir / "input" / "run_form.json")
    if run_form.get("title"):
        return str(run_form["title"])
    return project_dir.name.replace("_", " ").strip().title()


def project_options_html():
    items = []
    for project_dir in list_previous_projects():
        created = time.strftime("%Y-%m-%d %H:%M", time.localtime(project_dir.stat().st_mtime))
        title = project_title_from_files(project_dir)
        slug = project_dir.name
        report = read_json_file(project_dir / "review" / "agent_report.json")
        failed = not any((project_dir / "renders").glob("*.mp4")) if (project_dir / "renders").exists() else True
        failed = failed or bool(report.get("needs_media_recut"))
        status = " &bull; failed/incomplete — load to rerun" if failed else ""
        items.append(f'<div class="project-list-item" style="padding: 13px 15px; border: 1px solid var(--line); border-radius: var(--r-md); cursor: pointer; background: var(--bg-input); color: var(--text); transition: background .16s, border-color .16s;" onmouseover="this.style.background=\'var(--bg-overlay)\';this.style.borderColor=\'var(--accent)\'" onmouseout="this.style.background=\'var(--bg-input)\';this.style.borderColor=\'var(--line)\'" onclick="window.loadProject(\'{esc(slug)}\')"><div style="font-weight: 600; margin-bottom: 4px;">{esc(title)}</div><div style="font-size: 12px; color: var(--faint);">{esc(slug)} &bull; {esc(created)}{status}</div></div>')
    return "".join(items)


def project_form_state(project_dir):
    state = load_ui_state()
    saved_run = read_json_file(project_dir / "input" / "run_form.json")
    if saved_run:
        normalized_saved = normalize_ui_state(saved_run)
        for key in (*UI_TEXT_DEFAULTS.keys(), *UI_CHECKBOX_DEFAULTS.keys()):
            if key in saved_run:
                state[key] = normalized_saved[key]
    config = read_json_file(project_dir / "config" / "project.json")
    report = read_json_file(project_dir / "review" / "agent_report.json")
    script = read_text_file(project_dir / "input" / "script.txt")
    visual_script = read_text_file(project_dir / "input" / "visual_script.txt")
    if script:
        state["script"] = script
    if visual_script:
        state["visual_script"] = visual_script
    state["title"] = str(report.get("title") or config.get("title") or project_title_from_files(project_dir))
    state["slug"] = project_dir.name
    agent = config.get("agent") if isinstance(config.get("agent"), dict) else {}
    wavespeed = config.get("wavespeed") if isinstance(config.get("wavespeed"), dict) else {}
    if agent:
        state["static_gpt_image_count"] = "0"
        if agent.get("selected_seedance_count") is not None:
            state["seedance_clip_count"] = str(agent.get("selected_seedance_count"))
    state["allow_seedance"] = bool(config.get("use_seedance_clips", True))
    state["allow_gpt"] = True
    video_model = str(wavespeed.get("video_model") or "")
    if wavespeed.get("seedance_model"):
        state["seedance_model"] = str(wavespeed.get("seedance_model"))
        state["video_model"] = state["seedance_model"]
    elif "v1.5" in video_model or "1.5" in video_model:
        state["seedance_model"] = "seedance-v1.5-pro"
        state["video_model"] = "seedance-v1.5-pro"
    else:
        state["seedance_model"] = "seedance-2.0"
        state["video_model"] = "seedance-2.0" 
    if wavespeed.get("reasoning_model"):
        state["reasoning_model"] = str(wavespeed.get("reasoning_model"))
        state["reasoning_mode"] = reasoning_modes.validate_reasoning_mode(
            state["reasoning_model"], wavespeed.get("reasoning_mode")) or ""
    state["autonomous_director"] = bool(agent.get("director_enabled", True))
    state["use_audio_timing"] = True
    state["use_llm_search"] = True
    state["use_llm_video_review"] = True
    state["background_music_enabled"] = bool(config.get("background_music_enabled", False))
    speaker_hook = config.get("speaker_hook") if isinstance(config.get("speaker_hook"), dict) else {}
    state["enable_speaker_hook"] = bool(speaker_hook.get("enabled"))
    state["auto_web_images"] = True
    if wavespeed.get("video_model") is None and not state["allow_seedance"]:
        state["seedance_clip_count"] = "0"
    # Incomplete/failed projects have no final config: rerun their original workflow in the same
    # folder. Completed projects retain the media-only recut default.
    rerun_failed = not any((project_dir / "renders").glob("*.mp4")) if (project_dir / "renders").exists() else True
    rerun_failed = rerun_failed or bool(report.get("needs_media_recut"))
    state["loaded_project_mode"] = "normal" if rerun_failed else "recut_existing_only"
    has_saved_scrape_media = (any((project_dir / "seedance 2.0").glob("scraped_*.mp4"))
                              or any((project_dir / "seedance 2.0" / "_candidates").rglob("cand_*.mp4")))
    if rerun_failed and has_saved_scrape_media:
        state["clip_source"] = "scrape"
        state["out_video_clips"] = True
        state["out_web_images"] = False
        state["out_wikimedia"] = False
        state["out_gpt_images"] = False
    normalized = normalize_ui_state(state)
    normalized["slug"] = project_dir.name
    normalized["loaded_project_mode"] = "normal" if rerun_failed else "recut_existing_only"
    normalized["rerun_failed_project"] = rerun_failed
    return normalized


_LAB_W, _LAB_H = 384, 1012          # one-side canvas (flat front-view)


def _lab_scene_inner():
    """Inner SVG markup for ONE side of a flat FRONT-VIEW pixel-art laboratory in the blue/white/
    grey clinical style: a framed wall poster, two loaded wall shelves (reagent bottles, jars,
    Erlenmeyer flask + beaker with blue liquid, test-tube racks), a dark-topped counter over a
    two-door base cabinet, a microscope + a test-tube rack on the bench, and a tiled floor. Drawn
    on a 384x1012 canvas; the opposite screen edge just mirrors this. Colours are baked in (they
    read on both themes - dark outlines + blue pop on the light parchment, white bodies + blue pop
    on the dark canvas); overall subtlety comes from the ::before opacity."""
    OUT = "#39435c"       # dark navy outline (the pixel-sticker edge)
    WH = "#f7f9fc"        # white equipment body
    WH2 = "#e7ecf3"       # off-white / label plate / shaded face
    GR = "#d6dce6"        # light grey (shelf plank, cabinet trim)
    GRD = "#c0c9d6"       # darker grey (edges, handles, brackets)
    CT = "#333b4d"        # dark counter top / rack base / microscope
    BL = "#3b7ae0"        # primary blue accent (caps, liquid, labels)
    BLD = "#2757ad"       # deep blue
    BLL = "#c3d8f6"       # pale blue (water body)
    FL = "#dfe4ec"        # floor tile grid line
    S = []

    def r(x, y, w, h, f, rx=0, st=OUT, sw=3):
        S.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" rx="{rx}" fill="{f}" stroke="{st}" stroke-width="{sw}"/>')

    def rn(x, y, w, h, f, rx=0):     # fill only, no stroke
        S.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" rx="{rx}" fill="{f}"/>')

    def ln(x1, y1, x2, y2, st=OUT, sw=3):
        S.append(f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" stroke="{st}" stroke-width="{sw}" stroke-linecap="round"/>')

    def cr(cx, cy, rd, f, st=OUT, sw=3):
        S.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{rd:.1f}" fill="{f}" stroke="{st}" stroke-width="{sw}"/>')

    def pg(pts, f, st=OUT, sw=3):
        pp = " ".join(f"{a:.1f},{b:.1f}" for a, b in pts)
        S.append(f'<polygon points="{pp}" fill="{f}" stroke="{st}" stroke-width="{sw}" stroke-linejoin="round"/>')

    # ---------- item drawers (x = left, by = bottom baseline) ----------
    def bottle(x, by, w, h, cap=BL):
        bt = by - h
        r(x, bt + w * 0.30, w, h - w * 0.30, WH, rx=6)                    # body
        nw = w * 0.46
        r(x + (w - nw) / 2, bt + w * 0.05, nw, w * 0.32, WH, rx=2)        # neck
        r(x + (w - nw) / 2 - 3, bt - 3, nw + 6, w * 0.18 + 3, cap, rx=3)  # cap
        r(x + w * 0.15, by - h * 0.52, w * 0.70, h * 0.32, WH2, rx=2)     # label plate
        ln(x + w * 0.26, by - h * 0.42, x + w * 0.74, by - h * 0.42, BL, 4)
        ln(x + w * 0.26, by - h * 0.31, x + w * 0.60, by - h * 0.31, GRD, 4)

    def jar(x, by, w, h, cap=BLD):
        bt = by - h
        r(x, bt + w * 0.20, w, h - w * 0.20, WH, rx=5)                    # body
        r(x - 3, bt, w + 6, w * 0.26, cap, rx=3)                          # wide screw cap
        r(x + w * 0.18, by - h * 0.5, w * 0.64, h * 0.30, WH2, rx=2)      # label

    def beaker(x, by, w, h):
        bt = by - h
        r(x, bt, w, h, WH, rx=3)                                          # glass
        rn(x + 3, by - h * 0.52, w - 6, h * 0.52 - 3, BLL)               # water body
        rn(x + 3, by - h * 0.52, w - 6, 5, BL)                            # water surface line
        for i in range(1, 4):                                            # graduation ticks
            ln(x + w * 0.60, bt + h * 0.2 * i, x + w * 0.82, bt + h * 0.2 * i, BLD, 2)
        ln(x - 4, bt, x + w * 0.34, bt, OUT, 3)                          # pour lip

    def flask(x, by, w, h):                                              # Erlenmeyer
        bt = by - h
        nw = w * 0.28
        pg([(x, by), (x + w, by), (x + w * 0.64, bt + h * 0.30), (x + w * 0.36, bt + h * 0.30)], WH)  # cone
        pg([(x + w * 0.16, by - 4), (x + w * 0.84, by - 4), (x + w * 0.60, by - h * 0.34), (x + w * 0.40, by - h * 0.34)], BLL, st="none", sw=0)  # liquid
        r(x + (w - nw) / 2, bt, nw, h * 0.34, WH, rx=2)                   # neck
        ln(x + (w - nw) / 2 - 3, bt + h * 0.08, x + (w + nw) / 2 + 3, bt + h * 0.08, OUT, 3)  # rim ring

    def rack(x, by, w, h, n=4):
        bt = by - h
        gap = w / n
        for i in range(n):                                               # test tubes
            tx = x + gap * i + gap * 0.5
            r(tx - 6, bt, 12, h * 0.82, WH, rx=4)
            rn(tx - 3, by - h * 0.55, 6, h * 0.22, BL)
        r(x, by - h * 0.30, w, h * 0.30, CT, rx=3)                       # rack base
        rn(x + 4, by - h * 0.30 + 3, w - 8, 5, "#4a566e")               # base highlight

    def microscope(x, by, s):
        r(x, by - s * 0.14, s, s * 0.14, WH, rx=4)                       # foot
        r(x + s * 0.16, by - s * 0.9, s * 0.26, s * 0.78, WH, rx=4)      # pillar
        pg([(x + s * 0.42, by - s * 0.86), (x + s * 0.9, by - s * 0.66),
            (x + s * 0.84, by - s * 0.5), (x + s * 0.36, by - s * 0.7)], CT)  # angled body
        cr(x + s * 0.9, by - s * 0.62, s * 0.1, BL)                      # eyepiece
        r(x + s * 0.34, by - s * 0.36, s * 0.2, s * 0.12, CT, rx=2)      # stage
        r(x + s * 0.4, by - s * 0.5, s * 0.1, s * 0.16, GRD, rx=2)       # objective

    def scale(x, by, w, h):
        r(x, by - h * 0.46, w, h * 0.46, WH, rx=5)                       # body
        r(x + w * 0.14, by - h, w * 0.72, h * 0.5, WH2, rx=3)            # weighing pan
        r(x + w * 0.2, by - h * 0.38, w * 0.4, h * 0.16, BL, rx=2)       # display

    def poster(x, y, w, h):
        r(x, y, w, h, WH, rx=4)                                          # frame
        rn(x + 7, y + 7, w - 14, h - 14, WH2)                            # mat
        pg([(x + w * 0.42, y + h * 0.18), (x + w * 0.58, y + h * 0.18),
            (x + w * 0.72, y + h * 0.6), (x + w * 0.28, y + h * 0.6)], WH, BL, 4)  # flask icon
        rn(x + w * 0.34, y + h * 0.44, w * 0.32, h * 0.16, BL)           # icon liquid
        ln(x + w * 0.44, y + h * 0.12, x + w * 0.56, y + h * 0.12, BL, 4)
        for i in range(3):
            ln(x + w * 0.22, y + h * 0.72 + i * (h * 0.09), x + w * 0.78, y + h * 0.72 + i * (h * 0.09), GRD, 3)

    def shelf(y):
        r(0, y, 372, 15, GR)                                             # plank
        rn(0, y + 12, 372, 4, GRD)                                       # front shadow
        for bx in (26, 320):                                            # brackets
            pg([(bx, y + 15), (bx + 24, y + 15), (bx, y + 42)], GRD)

    # ================= compose the side (top -> bottom) =================
    poster(40, 62, 176, 150)
    shelf(300)
    bottle(34, 300, 68, 104); jar(122, 300, 62, 86); jar(200, 300, 52, 70, cap=BL)
    shelf(492)
    flask(28, 492, 96, 112); beaker(142, 492, 60, 98); rack(216, 492, 120, 96, n=4)
    r(0, 690, 374, 30, CT, rx=3)                                         # counter top
    rn(5, 693, 364, 5, "#4a566e")                                       # counter highlight
    r(10, 720, 356, 190, WH, rx=5)                                       # cabinet body
    ln(10, 744, 366, 744, GRD, 3)                                        # top drawer seam
    ln(188, 750, 188, 902, GRD, 3)                                       # door seam
    r(167, 802, 9, 44, GRD, rx=3); r(201, 802, 9, 44, GRD, rx=3)         # door handles
    r(28, 910, 320, 22, GRD, rx=2)                                       # kickplate
    microscope(38, 690, 122)
    rack(206, 690, 128, 100, n=4)
    rn(0, 932, 384, 80, WH2)                                             # floor base
    for fy in (932, 964, 996):
        ln(0, fy, 384, fy, FL, 2)
    for fx in (52, 130, 208, 286, 360):
        ln(fx, 932, fx, 1012, FL, 2)
    return "".join(S)


def _lab_window_svg(w=1600, h=1000):
    """Compose the FULL-WINDOW front-view lab: the side scene at the left edge + a mirrored copy at
    the right edge (centre left transparent/clean), a faint full-width tiled floor, and a ceiling
    light fixture up top. Returned as a plain SVG string (transparent background) - it is rasterised
    to a small PNG (static/lab_bg.png) that the page then upscales with image-rendering:pixelated so
    the whole window reads as chunky pixel art."""
    OUT, WH, FL = "#39435c", "#f7f9fc", "#dfe4ec"
    inner = _lab_scene_inner()
    sc = h / _LAB_H
    s = []
    # full-width faint tiled floor band across the bottom
    s.append(f'<rect x="0" y="{h-58:.0f}" width="{w}" height="58" fill="#eef1f6"/>')
    for fx in range(0, w + 1, 92):
        s.append(f'<line x1="{fx}" y1="{h-58:.0f}" x2="{fx}" y2="{h}" stroke="{FL}" stroke-width="2"/>')
    s.append(f'<line x1="0" y1="{h-30:.0f}" x2="{w}" y2="{h-30:.0f}" stroke="{FL}" stroke-width="2"/>')
    # ceiling light fixture (top centre)
    cx0, cx1 = w * 0.42, w * 0.58
    s.append(f'<rect x="{cx0:.0f}" y="12" width="{cx1-cx0:.0f}" height="34" rx="8" fill="{WH}" stroke="{OUT}" stroke-width="3"/>')
    s.append(f'<rect x="{cx0+16:.0f}" y="20" width="{cx1-cx0-32:.0f}" height="18" rx="6" fill="#eef3fb"/>')
    # left + right furniture, scaled to full height
    s.append(f'<g transform="translate(0,0) scale({sc:.4f})">{inner}</g>')
    s.append(f'<g transform="translate({w},0) scale({-sc:.4f},{sc:.4f})">{inner}</g>')
    return (f"<svg xmlns='http://www.w3.org/2000/svg' width='{w}' height='{h}' "
            f"viewBox='0 0 {w} {h}'>{''.join(s)}</svg>")


def app_style():
    return ("""
    <style>""" + """
      /* full-window pixel-art LAB backdrop (rasterised low-res PNG upscaled -> chunky pixels; the
         scene centre is transparent so the working area stays clean) */
      body::before {
        content: ""; position: fixed; inset: 0; z-index: 0; pointer-events: none;
        background-image: url("/static/lab_bg.png");
        background-repeat: no-repeat; background-position: center bottom;
        background-size: cover;
        image-rendering: pixelated;
        opacity: .12;
        transform: translateY(7vh);   /* sit the lab art a bit lower, away from the header */
      }
      .theme-dark body::before { opacity: .14; }
      /* the lab backdrop is distracting behind the timeline editor - hide it there */
      body.page-timeline::before { display: none; }""" + """
      /* ===== OVERWORLD — pixel-art neo-brutalist editorial on warm parchment ===== */
      :root {
        color-scheme: light;
        font-family: "Space Mono", ui-monospace, "Courier New", monospace;
        --display: "Press Start 2P", "Space Mono", monospace;   /* chunky pixel titles */
        --pixel: "Silkscreen", "Space Mono", monospace;          /* UI chrome / labels / buttons */
        --mono: "Space Mono", ui-monospace, monospace;            /* body + code */
        /* ink + paper */
        --ink: #17150f;
        --bg-base: #efe7d6;        /* warm parchment */
        --bg-raised: #f6f0e3;      /* lighter card */
        --bg-overlay: #e7ddc8;
        --bg-input: #fbf7ee;       /* near-white field */
        --text: #17150f;
        --muted: #4f4a3c;
        --faint: #837c68;
        /* borders — bold ink for the brutalist frames, soft for dividers */
        --line: #d8cdb4;
        --line-strong: #17150f;
        /* accent: editorial blue, with red/green/yellow semantics */
        --accent: #2f6fd6;
        --accent-hover: #2660c2;
        --accent-active: #1f54ad;
        --accent-subtle: #dbe6fa;
        --accent-fg: #ffffff;
        --accent-2: #e8472b;       /* signal red */
        --success: #2f8a52;
        --warning: #e0951a;
        --danger: #e8472b;
        /* legacy aliases so older rules still resolve */
        --bg: var(--bg-base); --bg-panel: var(--bg-raised); --bg-panel-2: var(--bg-overlay);
        --accent-ink: #ffffff; --accent-3: var(--danger); --go: var(--accent);
        /* radius — small/square brutalist */
        --r-sm: 2px; --r-md: 4px; --r-lg: 6px; --r-full: 4px;
        --radius: var(--r-lg); --radius-sm: var(--r-md);
        /* HARD offset shadows (no blur) — the brutalist signature */
        --sh-1: 3px 3px 0 var(--ink);
        --sh-2: 5px 5px 0 var(--ink);
        --sh-3: 8px 8px 0 var(--ink);
        --shadow: var(--sh-1);
        --dur: 120ms; --ease: cubic-bezier(.2,.8,.2,1);
        --blur: blur(0);
      }
      /* ===== Dark mode — same neo-brutalist look, warm-charcoal paper, light ink.
         Everything keyed off --ink flips together (text, borders, hard shadows), so the
         offset-shadow signature stays coherent as light-on-dark. ===== */
      html.theme-dark {
        color-scheme: dark;
        --ink: #ece4d2;
        --bg-base: #14120d;
        --bg-raised: #201c14;
        --bg-overlay: #2a2418;
        --bg-input: #1a1610;
        --text: #ece4d2;
        --muted: #b8b09b;
        --faint: #8c8470;
        --line: #3a3426;
        --line-strong: #ece4d2;
        --accent: #5b93ec;
        --accent-hover: #6c9fef;
        --accent-active: #4a82e0;
        --accent-subtle: #1c2740;
        --accent-2: #ff5b3f;
        --success: #46b06e;
        --warning: #f0a830;
        --danger: #ff5b3f;
      }
      * { scrollbar-color: var(--ink) transparent; }
      *::-webkit-scrollbar { width: 12px; height: 12px; }
      *::-webkit-scrollbar-thumb { background: var(--ink); border-radius: 0; border: 3px solid var(--bg-base); background-clip: padding-box; }
      *::-webkit-scrollbar-track { background: transparent; }
      body {
        margin: 0; min-height: 100vh;
        background:
          radial-gradient(800px 800px at 92% 4%, rgba(47,111,214,.06), transparent 60%),
          radial-gradient(620px 620px at 3% 96%, rgba(232,71,43,.05), transparent 60%),
          var(--bg-base);
        color: var(--text);
        position: relative; overflow-x: hidden;
      }
      main { max-width: 1320px; margin: 0 auto; padding: 16px clamp(18px, 3.5vw, 44px) 24px; position: relative; z-index: 1; }
      h1 {
        font-family: var(--display);
        font-size: 19px; margin: 0; letter-spacing: 0; font-weight: 400; line-height: 1.15;
        color: var(--ink); text-shadow: 2px 2px 0 rgba(232,71,43,.28);
      }
      h2 { font-family: var(--pixel); font-size: 15px; margin: 0 0 13px; color: var(--ink); font-weight: 400; letter-spacing: .3px; text-transform: uppercase; }
      ::selection { background: var(--accent-2); color: #fff; }
      :focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
      .top { display: flex; justify-content: space-between; gap: 24px; align-items: center; padding-bottom: 10px; margin-bottom: 14px; border-bottom: 3px solid var(--ink); }
      .brand { display: flex; gap: 13px; align-items: center; min-width: 0; }
      /* the real app logo (static/app_icon.png) as the brand mark - it already has the pixel badge,
         ink border and hard shadow baked in, so render it crisp (pixelated) at header size. */
      .brand-mark { display: block; flex: 0 0 auto; width: 46px; height: 46px; image-rendering: pixelated; }
      .sub { color: var(--muted); margin-top: 7px; max-width: 760px; line-height: 1.5; font-size: 14px; }
      .ico { flex: 0 0 auto; vertical-align: middle; }
      .brand > div { position: relative; }
      .nav-actions { display: flex; gap: 9px; flex-wrap: wrap; justify-content: flex-end; align-items: center; }
      .nav-actions .button {
        width: auto; min-width: 0; display: inline-flex; align-items: center; gap: 8px;
        padding: 9px 14px; font-size: 11px; font-weight: 400; white-space: nowrap;
        font-family: var(--pixel); text-transform: uppercase; letter-spacing: .3px;
      }
      .nav-actions .button .ico { opacity: 1; width: 15px; height: 15px; }
      .nav-sep { display: none; }
      /* high specificity so it beats .button.secondary (which was leaving a light bg
         under the white icon -> the icon was invisible). SFX master is the red button. */
      .nav-actions .button.nav-sfx { background: var(--accent-2); color: #fff; border-color: var(--ink); }
      .nav-actions .button.nav-sfx:hover { background: var(--accent-active); }
      .nav-actions .button.nav-sfx .ico { color: #fff; }
      /* dark-mode switch, humbly stacked above the SFX master button */
      .nav-sfx-wrap { display: flex; flex-direction: column; align-items: flex-end; gap: 6px; }
      /* dark-mode switch: identical apple toggle to the green on/off ones, turns green
         when dark; the sliding knob carries the sun (light) / moon (dark) glyph */
      .nav-actions .theme-toggle {
        appearance: none; -webkit-appearance: none; flex: 0 0 auto;
        width: 46px; height: 24px; min-width: 46px; padding: 0; margin: 0; position: relative;
        border: 2px solid var(--ink); border-radius: 999px; background: var(--bg-overlay);
        box-shadow: none; cursor: pointer; transition: background var(--dur) var(--ease);
      }
      .nav-actions .theme-toggle::after { display: none; }
      .nav-actions .theme-toggle:hover { box-shadow: 2px 2px 0 var(--ink); transform: none; background: var(--bg-overlay); }
      .theme-toggle .tt-knob {
        position: absolute; top: 1px; left: 1px; width: 18px; height: 18px; border-radius: 50%;
        background: var(--ink); display: flex; align-items: center; justify-content: center;
        transition: transform var(--dur) var(--ease), background var(--dur) var(--ease);
      }
      html.theme-dark .nav-actions .theme-toggle { background: var(--success); }
      html.theme-dark .nav-actions .theme-toggle:hover { background: var(--success); }
      html.theme-dark .theme-toggle .tt-knob { transform: translateX(22px); background: #fff; }
      .theme-toggle .tt-sun, .theme-toggle .tt-moon { font-size: 10px; line-height: 1; pointer-events: none; transition: opacity var(--dur) var(--ease); }
      .theme-toggle .tt-sun { opacity: 1; color: var(--warning); }
      .theme-toggle .tt-moon { opacity: 0; position: absolute; color: var(--ink); }
      html.theme-dark .theme-toggle .tt-sun { opacity: 0; position: absolute; }
      html.theme-dark .theme-toggle .tt-moon { opacity: 1; position: static; }
      .top-left { display: flex; align-items: center; gap: 0; min-width: 0; }
      .top-left .back-arrow {
        flex: 0 0 auto; display: inline-flex; align-items: center; justify-content: center;
        width: 40px; height: 40px; min-width: 0; padding: 0; margin-right: 14px; border-radius: var(--r-md);
        color: var(--ink); background: var(--bg-raised); border: 2px solid var(--ink); text-decoration: none;
        box-shadow: var(--sh-1); cursor: pointer;
        transition: transform var(--dur) var(--ease), box-shadow var(--dur) var(--ease);
      }
      .top-left .back-arrow:hover { transform: translate(-2px,-2px); box-shadow: 5px 5px 0 var(--ink); background: var(--bg-raised); }
      .top-left .back-arrow:active { transform: translate(0,0); box-shadow: 1px 1px 0 var(--ink); }
      /* breadcrumb pipeline indicator: script // chill // upload */
      .steps { display: inline-flex; align-items: center; gap: 0; margin-top: 10px; font-family: var(--pixel); }
      .steps .step {
        display: inline-flex; align-items: center; gap: 7px;
        font-size: 11px; font-weight: 400; letter-spacing: .4px; color: var(--faint); text-transform: uppercase;
        padding: 4px 9px; border-radius: 0;
      }
      .steps .step .ico { width: 14px; height: 14px; color: var(--faint); }
      .steps .step:first-child { color: var(--accent-2); }
      .steps .step:first-child .ico { color: var(--accent-2); }
      .steps .arrow { display: inline-flex; color: var(--faint); padding: 0 2px; font-weight: 700; }
      .steps .arrow .ico { display: none; }
      .steps .arrow::before { content: "//"; font-family: var(--mono); font-size: 12px; }
      .create-bar {
        grid-column: 1 / -1;
        position: relative; z-index: 1;
        display: flex; flex-direction: column; gap: 12px;
        padding: 14px 18px; border-radius: var(--r-lg);
        margin-bottom: 14px; border: 3px solid var(--ink);
        background: var(--bg-raised);
        box-shadow: var(--sh-2);
        box-sizing: border-box;
        max-height: calc(100vh - 148px);   /* keep the whole create step inside the window: top row + Create button stay pinned, settings scroll (border-box so padding+border are inside the cap) */
      }
      /* the settings sections (clip source + outputs) live in a bounded scroll region so the
         page itself never scrolls and the Create button is always visible */
      /* NO visible scrollbar here (same policy as the wizard): content still wheel-scrolls
         as a safety net when the window is too small, but no bar is ever shown. */
      .cbar-scroll { display: flex; flex-direction: column; gap: 12px; flex: 1 1 auto; min-height: 0; overflow-y: auto; overflow-x: hidden; margin: 0 -4px; padding: 0 4px; scrollbar-width: none; }
      .cbar-scroll::-webkit-scrollbar { display: none; }
      .cbar-row { display: flex; gap: 18px; flex-wrap: wrap; align-items: flex-end; }
      .cbar-cell { display: flex; flex-direction: column; gap: 8px; min-width: 0; }
      .cbar-cell.tiers, .cbar-cell.runtype { flex: 1 1 300px; }
      .cbar-models .cbar-cell { flex: 1 1 200px; }
      .cbar-cap { font-family: var(--pixel); font-size: 11px; text-transform: uppercase; letter-spacing: .4px; color: var(--muted); font-weight: 400; display: inline-flex; align-items: center; gap: 6px; }
      .cbar-cap::before { content: "\\025A0"; color: var(--accent-2); font-size: 9px; }
      .cbar-cap .cbar-save-btn { width: auto; min-width: 0; padding: 0; margin: 0; line-height: 1; font-size: 14px; background: transparent; color: var(--muted); border: 0; border-radius: 0; box-shadow: none; cursor: pointer; }
      .cbar-cap .cbar-save-btn:hover { background: transparent; box-shadow: none; transform: none; color: var(--accent); }
      .cbar-cell select { padding: 10px 12px; font-size: 13px; }
      .cbar-divider { align-self: stretch; width: 3px; background: var(--ink); margin: 2px 2px; flex: 0 0 auto; }
      @media (max-width: 760px) { .cbar-divider { display: none; } }
      /* horizontal separator between the moved-in sections (clip source, outputs) */
      .cbar-sep { border: 0; border-top: 2px dashed var(--line-strong); margin: 4px 0 2px; width: 100%; }
      .cbar-section { display: block; }
      .cbar-section .cbar-cap { margin-bottom: 10px; }
      .cbar-section .csrc-btns, .cbar-section .otoggles, .cbar-section .scrape-settings { margin-top: 2px; }
      /* grayed-out controls (AI models + AI-image outputs when scraping) */
      .ctrl-disabled { opacity: .4; pointer-events: none; }
      /* segmented control — chunky pixel cards */
      .tier-btns { display: flex; gap: 7px; }
      .tier-btns .tier-btn {
        flex: 1 1 0; width: auto; min-width: 0;
        display: flex; flex-direction: column; align-items: center; gap: 2px;
        padding: 8px 8px 7px; line-height: 1.15; font-weight: 700; font-size: 12px; cursor: pointer;
        border: 2px solid var(--ink); border-radius: var(--r-md);
        background: var(--bg-input); color: var(--muted); box-shadow: var(--sh-1);
        transition: transform var(--dur) var(--ease), background var(--dur) var(--ease), color var(--dur) var(--ease), box-shadow var(--dur) var(--ease);
      }
      .tier-btns .tier-btn::after { display: none; }
      .tier-btns .tier-btn b { display: block; font-family: var(--pixel); font-weight: 400; font-size: 11px; line-height: 1.2; text-transform: uppercase; }
      .tier-btns .tier-btn small { display: block; font-weight: 700; font-size: 11px; color: var(--faint); line-height: 1.1; margin-top: 2px; }
      .tier-btns .tier-btn:hover { color: var(--ink); transform: translate(-1px,-1px); box-shadow: 4px 4px 0 var(--ink); }
      .tier-btn.tier-active, .runtype-btns .tier-btn.run-active {
        background: var(--accent); color: #fff; border-color: var(--ink);
        box-shadow: var(--sh-1); transform: none;
      }
      .tier-btn.tier-active small, .runtype-btns .tier-btn.run-active small { color: #fff !important; }
      /* cheap -> best price ramp (preset only) */
      .cbar-cell.tiers .tier-btn:nth-child(1) small { color: var(--success); }
      .cbar-cell.tiers .tier-btn:nth-child(2) small { color: var(--warning); }
      .cbar-cell.tiers .tier-btn:nth-child(3) small { color: var(--accent-2); }
      .create-bar .create-short-btn {
        flex: 0 0 auto; align-self: flex-end; width: auto; min-width: 0;
        padding: 13px 26px; font-size: 12px; font-weight: 400; letter-spacing: .4px;
        font-family: var(--pixel); text-transform: uppercase;
        border-radius: var(--r-md); color: #fff; border: 2px solid var(--ink);
        background: var(--accent); box-shadow: var(--sh-2);
      }
      .create-bar .create-short-btn::after { display: none; }
      .create-bar .create-short-btn:hover {
        transform: translate(-2px,-2px); background: var(--accent-hover);
        box-shadow: 7px 7px 0 var(--ink);
      }
      .create-bar .create-short-btn:active { transform: translate(0,0); box-shadow: 2px 2px 0 var(--ink); }
      /* big primary CTA at the bottom of the settings bar */
      .create-bar .create-short-btn.create-short-big {
        align-self: stretch; width: 100%; margin-top: 4px; padding: 17px 26px; font-size: 15px;
      }
      .create-bar .create-short-btn.create-short-big:hover { transform: translate(-2px,-2px); box-shadow: 8px 8px 0 var(--ink); }
      /* top row: preset symbols + reasoning model + halt */
      .cbar-top { display: grid; grid-template-columns: auto auto; justify-content: space-between; align-items: start; gap: 12px 28px; }
      .cbar-top .reasoning-cell select { width: 320px; max-width: 100%; }
      .preset-actions { display: flex; align-items: center; gap: 18px; min-height: 42px; }
      @media (max-width: 600px) { .cbar-top { grid-template-columns: 1fr; justify-content: stretch; } .cbar-top .reasoning-cell select { width: 100%; } }
      /* borderless preset symbols (high specificity to beat the global button rule) */
      .cbar-cap .cbar-icon-btn, .preset-actions .cbar-icon-btn { width: auto; min-width: 0; height: auto; margin: 0; padding: 0 2px; line-height: 1; font-size: 20px; background: transparent; color: var(--muted); border: 0; border-radius: 0; box-shadow: none; cursor: pointer; }
      .cbar-cap .cbar-icon-btn::after, .preset-actions .cbar-icon-btn::after { display: none; }
      .cbar-cap .cbar-icon-btn:hover, .preset-actions .cbar-icon-btn:hover { background: transparent; box-shadow: none; transform: translateY(-1px); color: var(--accent); }
      /* clip-source segmented switch — labels sit inside, a thumb slides under the active one */
      .seg-switch {
        position: relative; display: flex; width: fit-content; margin: 9px 0 4px; padding: 3px; isolation: isolate;
        border: 2px solid var(--ink); border-radius: 999px; background: var(--bg-input); box-shadow: var(--sh-1);
      }
      .seg-switch .seg-opt {
        position: relative; z-index: 1; width: auto; min-width: 0; margin: 0; border: 0; box-shadow: none;
        background: transparent; cursor: pointer; padding: 7px 16px; border-radius: 999px;
        font-family: var(--pixel); font-size: 11px; text-transform: uppercase; letter-spacing: .3px;
        color: var(--muted); transition: color var(--dur) var(--ease);
      }
      .seg-switch .seg-opt::after { display: none; }
      .seg-switch .seg-opt:hover { background: transparent; box-shadow: none; transform: none; color: var(--ink); }
      .seg-switch .seg-opt.on { color: #fff; }
      .seg-switch .seg-thumb {
        position: absolute; z-index: 0; top: 3px; bottom: 3px; left: 3px; width: calc(50% - 3px);
        border-radius: 999px; background: var(--accent); transition: transform var(--dur) var(--ease);
      }
      .seg-switch[data-src="scrape"] .seg-thumb { transform: translateX(100%); }
      /* Scraping-engine toggle (data-eng): pure-CSS active state, v2 = left (default), v1 = right */
      .seg-switch[data-eng="v1"] .seg-thumb { transform: translateX(100%); }
      .seg-switch[data-eng="v2"] .seg-opt[data-eng="v2"],
      .seg-switch[data-eng="v1"] .seg-opt[data-eng="v1"] { color: #fff; }
      /* AI model pickers (generate mode only) */
      .ai-models { display: grid; grid-template-columns: repeat(2, minmax(0,1fr)); gap: 14px; margin-top: 10px; }
      @media (max-width: 560px) { .ai-models { grid-template-columns: 1fr; } }
      /* finished-render view: just the final video + open timeline editor */
      .done-wrap { max-width: 720px; margin: 0 auto; text-align: center; padding: 8px 0 36px; }
      .done-title { margin: 6px 0 20px; }
      .done-stage { display: flex; justify-content: center; }
      .done-video { width: min(380px, 86vw); height: auto; aspect-ratio: 9 / 16; background: #000; border: 3px solid var(--ink); border-radius: var(--r-lg); box-shadow: var(--sh-2); }
      .done-actions { display: flex; gap: 12px; justify-content: center; flex-wrap: wrap; margin-top: 24px; }
      .done-actions .button { width: auto; min-width: 0; margin: 0; }
      /* ===== Onboarding wizard ===== */
      form#short-form { display: block; }                 /* single-column wizard flow */
      #wizard { max-width: 780px; margin: 0 auto; position: relative; }
      [data-step] { will-change: transform, opacity; }
      [data-step].wiz-anim { animation: wizIn .3s var(--ease) both; }
      @keyframes wizIn { from { opacity: 0; transform: translateX(46px); } to { opacity: 1; transform: translateX(0); } }
      .wiz-headline { text-align: center; padding: 14px 16px 8px; margin-bottom: 4px; }
      .wiz-type { font-family: var(--display); font-size: clamp(17px, 3.2vw, 28px); color: var(--ink); line-height: 1.45; text-shadow: 2px 2px 0 rgba(232,71,43,.22); }
      .wiz-type::after { content: "\\2588"; margin-left: 2px; color: var(--accent-2); animation: wizCaret 1s steps(1) infinite; }
      @keyframes wizCaret { 50% { opacity: 0; } }
      .wiz-nav { display: flex; justify-content: flex-end; gap: 10px; margin: 14px 0 6px; }
      .wiz-nav .button { width: auto; min-width: 0; margin: 0; }
      /* Back / Continue sit beside the menu column (not above it), vertically centred. Anchored
         to the menu column itself (right:100% / left:100%), so they hug the panel regardless of
         where the column lands on screen. */
      .wiz-topnav { display: block; }
      .wiz-topnav .button { position: absolute; top: 0; z-index: 40; width: auto; min-width: 0; margin: 0; white-space: nowrap; }
      #wiz-back-btn { right: 100%; margin-right: 16px; }   /* just left of the menu column, top corner */
      #wiz-cont-btn { left: 100%; margin-left: 16px; }     /* just right of it, top corner */
      .wiz-topnav .wiz-back-btn[data-hidden="1"] { visibility: hidden; }
      /* ===== Mode-selection menu (step 0) ===== */
      /* bounded like the wizard steps: 4 big cards fit fully at normal viewports; on a very short
         window the menu scrolls WITHIN itself instead of page-scrolling. Padding gives the cards'
         hover shadow room so overflow:auto doesn't clip it. */
      .wiz-modemenu { max-width: 980px; margin: 8px auto 0; max-height: calc(100vh - 255px); overflow: hidden auto; padding: 4px 12px 14px; }
      /* two labelled rows: "make a new video" (creation modes) and "polish a finished video" (Masters) */
      .modemenu-group + .modemenu-group { margin-top: 18px; }
      .modemenu-grouplabel { font-family: var(--pixel); font-size: 10px; letter-spacing: .08em; text-transform: uppercase; color: var(--muted); margin: 0 0 9px 3px; }
      .modemenu-cards { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 14px; }
      /* scoped under .modemenu-cards (0,2,0) so display:flex BEATS the global button rule
         (button:not(.preview-button) is 0,1,1 and was forcing display:block -> the icon/title/desc
         spans rendered inline and collided with the title's underline). */
      .modemenu-cards .modemenu-card { display: flex; flex-direction: column; align-items: flex-start; gap: 4px; text-align: left; width: 100%; min-height: 138px; padding: 15px 17px 16px; background: var(--bg-raised); border: 2px solid var(--ink); border-radius: var(--r-md); box-shadow: var(--sh-2); cursor: pointer; transition: transform .1s var(--ease), box-shadow .1s var(--ease), background .1s var(--ease); }
      .modemenu-cards .modemenu-card .mm-desc { display: block; }
      /* ICON sits NEXT TO the title on one row; the dashed rule separates that head from the subtitle */
      .modemenu-cards .modemenu-card .mm-head { display: flex; align-items: center; gap: 9px; width: 100%; margin: 1px 0 11px; padding-bottom: 11px; border-bottom: 2px dashed var(--line-strong); }
      .modemenu-card::after { display: none; }
      .modemenu-card:hover { transform: translate(-2px,-2px); border-color: var(--ink); background: var(--bg-input); box-shadow: 8px 8px 0 var(--ink); }
      .modemenu-card:active { transform: translate(2px,2px); box-shadow: 1px 1px 0 var(--ink); }
      .modemenu-card .mm-ico { font-size: 28px; line-height: 1; flex: 0 0 auto; }
      /* the display font (Press Start 2P) is wide + cards are now 3-across, so titles WRAP instead of truncating */
      .modemenu-card .mm-title { font-family: var(--display); font-size: 10.5px; line-height: 1.55; color: var(--ink); text-shadow: 2px 2px 0 rgba(232,71,43,.22); letter-spacing: .2px; white-space: normal; overflow-wrap: anywhere; min-width: 0; }
      .modemenu-card .mm-desc { font-family: var(--mono); font-size: 12px; font-weight: 600; color: var(--muted); line-height: 1.45; }
      @media (max-width: 820px) { .modemenu-cards { grid-template-columns: repeat(2, minmax(0,1fr)); } }
      @media (max-width: 540px) { .modemenu-cards { grid-template-columns: 1fr; } }
      @media (max-width: 1120px) {
        /* not enough side room: pin to the bottom corners instead */
        .wiz-topnav .button { position: fixed; top: auto; bottom: 16px; transform: none; }
        #wiz-back-btn { right: auto; left: 14px; margin: 0; }
        #wiz-cont-btn { left: auto; right: 14px; margin: 0; }
      }
      .wiz-backbar { margin-bottom: 14px; }
      .wiz-backbar .button { width: auto; min-width: 0; margin: 0; padding: 7px 13px; font-size: 10px; }
      .help { display: inline-flex; align-items: center; justify-content: center; color: var(--faint); cursor: help; vertical-align: middle; position: relative; }
      .help:hover, .help:focus { color: var(--accent); outline: none; }
      .help::after {
        content: attr(data-tip);
        position: absolute; left: 50%; top: calc(100% + 8px); transform: translateX(-50%) translateY(-4px);
        width: max-content; max-width: 260px; padding: 10px 13px; z-index: 80;
        background: var(--ink);
        color: var(--bg-base); border: 2px solid var(--ink); border-radius: var(--r-md);
        font-family: var(--mono); font-size: 12px; font-weight: 400; line-height: 1.45; letter-spacing: 0; text-transform: none; text-align: left;
        box-shadow: var(--sh-2); white-space: normal;
        /* display:none while hidden - an opacity-0 tooltip still counts into the ancestor
           scroller's scrollHeight, which put a phantom scrollbar on the wizard script step.
           allow-discrete + @starting-style keep the fade/slide animation in Chromium. */
        display: none; opacity: 0; pointer-events: none;
        transition: opacity var(--dur) var(--ease), transform var(--dur) var(--ease),
                    display var(--dur) allow-discrete;
      }
      .help:hover::after, .help:focus::after { display: block; opacity: 1; transform: translateX(-50%) translateY(0); }
      @starting-style {
        .help:hover::after, .help:focus::after { opacity: 0; transform: translateX(-50%) translateY(-4px); }
      }
      .script-wrap { position: relative; }
      /* highlight overlay + textarea MUST share identical metrics (font/size/padding)
         so the colored hook lines up with the textarea's caret and wrapping. */
      .script-wrap textarea, .script-highlight {
        font-family: var(--mono);
        font-size: 13.5px; line-height: 1.6; letter-spacing: 0;
      }
      .script-wrap textarea { position: relative; background: transparent; color: transparent; -webkit-text-fill-color: transparent; caret-color: var(--accent); }
      /* the textarea text is transparent (the highlight layer renders it) so we must
         re-show the placeholder explicitly, otherwise -webkit-text-fill-color hides it. */
      .script-wrap textarea::placeholder { color: var(--faint); -webkit-text-fill-color: var(--faint); }
      .script-wrap textarea::selection { background: var(--accent-subtle); -webkit-text-fill-color: var(--text); }
      .script-highlight {
        position: absolute; inset: 0; margin: 0; pointer-events: none; overflow: hidden;
        box-sizing: border-box; border: 1px solid transparent; border-radius: var(--r-md);
        padding: 11px 13px; min-height: 260px;
        white-space: pre-wrap; overflow-wrap: break-word; word-break: break-word; color: var(--text);
      }
      .script-highlight .hook-mark { color: #fff; -webkit-text-fill-color: #fff; font-weight: 700; background: var(--accent-2); border-radius: 2px; box-shadow: 0 0 0 1px var(--ink); }
      .hook-controls { display: flex; gap: 8px; align-items: center; margin-top: 12px; }
      .hook-controls .hook-btn { width: auto; min-width: 0; flex: 0 0 auto; padding: 8px 13px; font-size: 10px; }
      @media (max-width: 820px) { .script-highlight { min-height: 300px; } }
      /* Onboarding SCRIPT step: the editor height scales with the window so the whole step fits a
         short laptop/browser viewport with NO page scroll; a long script scrolls inside the textarea.
         The clamp keeps the step's OTHER content (~277px of label/buttons/slider) + textarea inside
         the .stack bound below at every viewport height, so neither element ever shows a scrollbar.
         (#wizard scope wins over the global textarea min-height and the .script-highlight min-height.) */
      #wizard .script-wrap textarea { min-height: 0; height: clamp(150px, calc(100vh - 460px), 520px); max-height: none; }
      #wizard .script-highlight { min-height: 0; }
      /* Safety net: the onboarding step container is bounded to the window, so ANY active step
         (the script step, or the voice step's two stacked panels) scrolls WITHIN itself instead of
         page-scrolling on short laptop/browser viewports. create-bar (step 4) has its own bound +
         pinned Create button and is NOT a .stack child, so it is unaffected. */
      .stack { max-height: calc(100vh - 140px); overflow-y: auto; overflow-x: hidden; }
      /* NO visible scrollbars in the wizard: content still wheel/touch-scrolls when a tiny window
         forces overflow, but the chrome (incl. the pixel-art arrow bars) is never drawn. */
      #wizard .stack, #wizard .script-wrap textarea { scrollbar-width: none; }
      #wizard .stack::-webkit-scrollbar, #wizard .script-wrap textarea::-webkit-scrollbar { display: none; }
      .action-btns { display: grid; grid-template-columns: repeat(auto-fit, minmax(124px, 1fr)); gap: 7px; }
      .action-btns .action-btn {
        width: auto; min-width: 0;
        display: flex; flex-direction: column; gap: 2px; align-items: flex-start;
        padding: 9px 13px; border: 2px solid var(--ink); border-radius: var(--r-md);
        background: var(--bg-input); color: var(--ink); font-weight: 700; cursor: pointer; line-height: 1.25; box-shadow: var(--sh-1);
        transition: transform var(--dur) var(--ease), box-shadow var(--dur) var(--ease), background var(--dur) var(--ease), color var(--dur) var(--ease);
      }
      .action-btns .action-btn small { font-weight: 700; font-size: 11px; color: var(--faint); }
      .action-btns .action-btn:hover { transform: translate(-1px,-1px); box-shadow: 4px 4px 0 var(--ink); }
      .action-btns .action-btn.active { border-color: var(--ink); background: var(--accent); color: #fff; }
      .action-btns .action-btn.active small { color: #fff; }
      /* Loaded-project ("main interface"): the run-mode actions panel appears BELOW the create-bar and
         shares the viewport, so keep it compact and let the create-bar shrink (its settings scroll
         internally, Create button stays pinned) so the whole loaded view fits with NO page scroll. */
      #loaded-actions-panel { padding: 12px 16px; max-height: 190px; overflow-y: auto; }
      #loaded-actions-panel > button:first-child { margin-bottom: 8px !important; padding: 9px 14px; }
      #loaded-actions-panel > label { margin-bottom: 6px; }
      .action-btns .action-btn { padding: 7px 9px; }
      body.project-loaded .create-bar { max-height: calc(100vh - 345px); }
      .speaker-gallery { display: grid; grid-template-columns: repeat(auto-fill, minmax(72px, 1fr)); gap: 8px; margin-bottom: 10px; max-height: 150px; overflow-y: auto; padding: 2px; }
      .speaker-tile { width: 100%; padding: 0; border: 2px solid var(--ink); border-radius: var(--r-md); overflow: hidden; background: var(--bg-input); cursor: pointer; aspect-ratio: 3 / 4; box-shadow: var(--sh-1); transition: transform var(--dur) var(--ease), box-shadow var(--dur) var(--ease); }
      .speaker-tile img { width: 100%; height: 100%; object-fit: cover; display: block; }
      .speaker-tile:hover { transform: translate(-1px,-1px); box-shadow: 3px 3px 0 var(--ink); }
      .speaker-tile.selected { box-shadow: 0 0 0 3px var(--accent), var(--sh-1); }
      .speaker-upload { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; margin: 4px 0 8px; }
      .speaker-upload .hint { margin: 0; }
      /* "+" upload tile inside the speaker gallery */
      .speaker-tile.speaker-add { display: grid; place-items: center; background: transparent; border: 2px dashed var(--ink); box-shadow: none; }
      .speaker-tile.speaker-add:hover { background: var(--bg-input); transform: translate(-1px,-1px); box-shadow: 3px 3px 0 var(--ink); }
      .speaker-tile.speaker-add.dragover { background: var(--accent-subtle); border-color: var(--accent); }
      .speaker-add-plus { font-family: var(--mono); font-size: 34px; font-weight: 700; color: var(--ink); line-height: 1; }
      /* panel header with a top-right on/off switch */
      .panel-head { display: flex; align-items: center; justify-content: space-between; gap: 14px; margin-bottom: 12px; }
      .panel-head label { margin: 0; }
      .toggle-panel.off .panel-body { opacity: .42; pointer-events: none; filter: grayscale(.4); }
      /* a label-wrapped checkbox is just the toggle switch (no box around it) */
      label.switch { display: inline-flex; padding: 0; margin: 0; cursor: pointer; }
      label.switch input[type="checkbox"] { margin: 0; }
      .otoggles { display: grid; grid-template-columns: repeat(2, minmax(0,1fr)); gap: 12px 34px; }
      .otoggle { display: flex; align-items: center; justify-content: flex-start; gap: 12px; margin: 0; font-family: var(--mono); text-transform: none; letter-spacing: 0; font-size: 14px; font-weight: 700; color: var(--ink); cursor: pointer; }
      .otoggle input[type="checkbox"] { margin: 0; flex: 0 0 auto; }
      @media (max-width: 560px){ .otoggles { grid-template-columns: 1fr; } }
      /* --- Clip source --- */
      .csrc-btns { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
      .csrc-btn {
        width: auto; min-width: 0; margin: 0; padding: 11px 12px; text-align: left;
        background: var(--bg-input); color: var(--ink);
        border: 1px solid var(--line-strong); border-radius: var(--r-md); box-shadow: var(--sh-1); cursor: pointer;
      }
      .csrc-btn::after { display: none; }
      .csrc-btn b { display: block; font-family: var(--pixel); font-weight: 400; font-size: 11px; text-transform: uppercase; line-height: 1.2; }
      .csrc-btn small { display: block; font-weight: 700; font-size: 11px; color: var(--faint); margin-top: 3px; }
      .csrc-btn:hover { transform: translate(-1px,-1px); box-shadow: 4px 4px 0 var(--ink); }
      .csrc-btn.csrc-active { background: var(--accent); border-color: var(--accent-active); color: #fff; box-shadow: var(--sh-2); }
      .csrc-btn.csrc-active small { color: rgba(255,255,255,.82); }
      .scrape-settings { margin-top: 8px; padding-top: 8px; border-top: 1px dashed var(--line-strong); }
      .scrape-lbl { margin-top: 8px; }
      .scrape-lbl:first-child { margin-top: 0; }
      .scrape-terms { min-height: 64px; resize: vertical; }
      .scrape-auto-note { font-family: var(--mono); font-weight: 700; font-size: 12px; color: var(--ink); background: var(--bg-input); border: 1px solid var(--line-strong); border-radius: var(--r-sm); padding: 8px 10px; margin: 2px 0 6px; }
      .chip-add { display: flex; gap: 8px; }
      .chip-add #term-input { flex: 1; min-width: 0; margin: 0; }
      .chip-add .button { width: auto; min-width: 0; margin: 0; padding: 0 16px; font-size: 18px; line-height: 1; }
      .chips { display: flex; flex-wrap: wrap; gap: 6px; margin: 8px 0 2px; }
      .chips:empty { margin: 0; }
      .chip { display: inline-flex; align-items: center; gap: 7px; white-space: nowrap; font-family: var(--mono); font-weight: 700; font-size: 12px; color: var(--ink); background: var(--accent-subtle); border: 1px solid var(--line-strong); border-radius: 999px; padding: 4px 11px; }
      .chips .chip button {
        width: auto; min-width: 0; height: auto; margin: 0; padding: 0;
        border: 0; background: transparent; box-shadow: none; border-radius: 0;
        color: var(--muted); font-size: 16px; line-height: 1; cursor: pointer;
        display: inline-flex; align-items: center; justify-content: center;
      }
      .chips .chip button::after { display: none; }
      .chips .chip button:hover { background: transparent; box-shadow: none; transform: none; color: var(--accent-2); }
      .relv-val { color: var(--accent); font-weight: 700; }
      .req-tag { display: inline-block; font-family: var(--mono); font-weight: 700; font-size: 9px; text-transform: uppercase; letter-spacing: .08em; color: #fff; background: var(--accent-2); border-radius: var(--r-sm); padding: 1px 5px; vertical-align: middle; }
      .conn-warn { font-family: var(--mono); font-weight: 700; font-size: 12px; color: var(--accent-2); background: rgba(232,71,43,.08); border: 1px dashed var(--accent-2); border-radius: var(--r-sm); padding: 7px 10px; margin: 2px 0 4px; }
      /* single-line connect rows: status text truncates, button stays right - keeps the
         settings column short enough that it never needs to scroll */
      .tiktok-connect { display: flex; align-items: center; gap: 8px; flex-wrap: nowrap; font-family: var(--mono); font-weight: 700; font-size: 12px; color: var(--ink); background: var(--bg-input); border: 1px solid var(--line-strong); border-radius: var(--r-sm); padding: 5px 10px; margin: 4px 0 2px; }
      .tiktok-connect .tt-text { flex: 1 1 auto; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
      .tiktok-connect .tt-btn { flex: 0 0 auto; width: auto; min-width: 0; margin: 0; padding: 7px 12px; white-space: nowrap; }
      .tt-status { width: 9px; height: 9px; border-radius: 50%; flex: 0 0 auto; box-shadow: 0 0 0 3px rgba(0,0,0,.04); }
      .tt-status.on { background: #1fbf6b; }
      .tt-status.off { background: var(--accent-2); }
      .tt-status.busy { background: #f0a500; animation: ttpulse 1s ease-in-out infinite; }
      @keyframes ttpulse { 0%,100% { opacity: 1; } 50% { opacity: .35; } }
      .tt-btn { margin-left: auto; }
      .tt-btn:disabled { opacity: .55; cursor: default; }
      .bgm-pick { margin: 8px 0 4px; }
      .bgm-row { display: flex; align-items: center; gap: 8px; }
      .bgm-row select { flex: 1; min-width: 0; }
      /* !important because the global .button rule (width:100%; big padding) is defined later and
         would otherwise win the cascade and blow this preview button up to full width. */
      .bgm-play, .voice-play { width: 30px !important; height: 30px !important; min-width: 30px !important; padding: 0 !important; flex: 0 0 auto; font-size: 11px !important; line-height: 1; display: inline-flex !important; align-items: center; justify-content: center; }
      .bgm-play::after, .voice-play::after { display: none; }
      .bgm-play:disabled, .voice-play:disabled { opacity: .5; cursor: default; }
      .voice-play.loading { opacity: .6; }
      input[type="range"] {
        -webkit-appearance: none; appearance: none; width: 100%; height: 8px; padding: 0; margin: 6px 0 2px;
        background: var(--bg-input); border: 1px solid var(--line-strong); border-radius: 999px; box-shadow: none; cursor: pointer;
      }
      input[type="range"]::-webkit-slider-thumb {
        -webkit-appearance: none; appearance: none; width: 20px; height: 20px; border-radius: 50%;
        background: var(--accent); border: 2px solid var(--ink); box-shadow: var(--sh-1); cursor: pointer; margin-top: -1px;
      }
      input[type="range"]::-moz-range-thumb {
        width: 18px; height: 18px; border-radius: 50%; background: var(--accent); border: 2px solid var(--ink); cursor: pointer;
      }
      .cbar-halt { display: flex; align-items: center; gap: 9px; font-family: var(--pixel); font-size: 10px; text-transform: uppercase; color: var(--muted); margin: 0; cursor: pointer; }
      .cbar-halt input[type="checkbox"] { margin: 0; }
      form { display: grid; grid-template-columns: 1fr 1fr; gap: 24px; align-items: start; }
      label { display: block; font-family: var(--pixel); font-weight: 400; margin-bottom: 9px; color: var(--ink); font-size: 12px; text-transform: uppercase; letter-spacing: .3px; }
      input, textarea, select {
        width: 100%;
        box-sizing: border-box;
        border: 2px solid var(--ink);
        border-radius: var(--r-md);
        padding: 11px 13px;
        font-size: 14px;
        font-family: var(--mono);
        background: var(--bg-input);
        color: var(--text);
        outline: none;
        transition: box-shadow var(--dur) var(--ease);
      }
      select { appearance: none; -webkit-appearance: none; cursor: pointer;
        background-image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='14' height='14' viewBox='0 0 24 24' fill='none' stroke='%2317150f' stroke-width='3' stroke-linecap='round' stroke-linejoin='round'><path d='m6 9 6 6 6-6'/></svg>");
        background-repeat: no-repeat; background-position: right 12px center; padding-right: 34px; }
      select option { color: var(--text); background: var(--bg-input); }
      input::placeholder, textarea::placeholder { color: var(--faint); }
      input:focus, textarea:focus, select:focus {
        border-color: var(--ink);
        box-shadow: 3px 3px 0 var(--accent);
      }
      input[type="file"]::file-selector-button {
        border: 2px solid var(--ink);
        border-radius: var(--r-md);
        background: var(--accent); color: #fff;
        padding: 8px 14px;
        margin-right: 10px;
        font-family: var(--pixel); font-size: 10px; text-transform: uppercase;
        cursor: pointer;
      }
      /* English custom file picker (the native button text is browser/locale-controlled) */
      .filepick-input { position: absolute; width: 1px; height: 1px; opacity: 0; overflow: hidden; padding: 0; margin: -1px; border: 0; }
      .filepick { display: inline-flex; align-items: center; gap: 12px; cursor: pointer; margin: 0; padding: 0; text-transform: none; }
      .filepick .filepick-btn { display: inline-flex; align-items: center; gap: 7px; border: 2px solid var(--ink); border-radius: var(--r-md); background: var(--accent); color: #fff; padding: 9px 15px; font-family: var(--pixel); font-size: 10px; text-transform: uppercase; box-shadow: var(--sh-1); }
      .filepick:hover .filepick-btn { transform: translate(-2px,-2px); box-shadow: 5px 5px 0 var(--ink); }
      .filepick .filepick-name { font-family: var(--mono); font-size: 13px; color: var(--muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
      textarea { min-height: 260px; resize: vertical; line-height: 1.55; }
      textarea.visual-textarea { min-height: 210px; }
      .wide { grid-column: 1 / -1; }
      .panel {
        background: var(--bg-raised);
        border: 3px solid var(--ink);
        border-radius: var(--r-lg);
        padding: 18px;
        box-shadow: var(--sh-1);
        transition: transform var(--dur) var(--ease), box-shadow var(--dur) var(--ease);
      }
      .panel.accent { box-shadow: var(--sh-1); border-left: 7px solid var(--accent-2); }
      .stack { display: grid; gap: 16px; }
      .controls { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; }
      .project-load-controls { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 10px; align-items: end; }
      .project-load-controls .button { width: auto; min-width: 112px; padding: 12px 14px; }
      .checks { display: grid; gap: 11px; }
      .checks label { font-weight: 500; font-size: 13.5px; color: var(--muted); display: flex; gap: 9px; align-items: center; }
      .checks input { width: auto; }
      button:not(.preview-button), .button {
        position: relative;
        border: 2px solid var(--ink);
        border-radius: var(--r-md);
        background: var(--bg-input);
        color: var(--ink);
        padding: 11px 17px;
        font-family: var(--pixel); font-weight: 400;
        font-size: 11px; text-transform: uppercase; letter-spacing: .3px;
        cursor: pointer;
        width: 100%;
        text-decoration: none;
        display: inline-block;
        text-align: center;
        box-sizing: border-box;
        box-shadow: var(--sh-1);
        transition: transform var(--dur) var(--ease), box-shadow var(--dur) var(--ease), background var(--dur) var(--ease);
      }
      button:not(.preview-button):hover, .button:hover {
        transform: translate(-2px,-2px);
        box-shadow: 5px 5px 0 var(--ink);
      }
      button:not(.preview-button):active, .button:active, .tier-btn:active, .action-btn:active { transform: translate(0,0); box-shadow: 1px 1px 0 var(--ink); }
      button:disabled, .button:disabled { opacity: .45; cursor: not-allowed; transform: none; box-shadow: var(--sh-1); }
      /* opacity-only: the old translateY(4px) grew the page 4px during the fade and
         flashed a scrollbar on every page load */
      @keyframes page-fade { from { opacity: 0; } to { opacity: 1; } }
      main { animation: page-fade .22s var(--ease) both; }
      @media (prefers-reduced-motion: reduce) { main, * { animation: none !important; } }
      button[type="submit"] { background: var(--accent); color: #fff; }
      button[type="submit"]:hover { background: var(--accent-hover); }
      .button.secondary { background: var(--bg-raised); color: var(--ink); }
      .button.secondary:hover { background: var(--bg-input); }
      button.danger, .button.danger { background: var(--accent-2); border-color: var(--ink); color: #fff; }
      button.danger:hover, .button.danger:hover { background: #d63d22; }
      /* ===== Apple-style on/off toggle: ALL checkboxes render as switches ===== */
      input[type="checkbox"] {
        appearance: none; -webkit-appearance: none; -moz-appearance: none;
        flex: 0 0 auto; width: 40px; height: 22px; min-width: 40px; padding: 0; margin: 0;
        border: 2px solid var(--ink); border-radius: 999px; background: var(--bg-overlay);
        position: relative; cursor: pointer; box-shadow: none;
        transition: background var(--dur) var(--ease);
      }
      input[type="checkbox"]::before {
        content: ""; position: absolute; top: 1px; left: 1px; width: 16px; height: 16px;
        border-radius: 999px; background: var(--ink);
        transition: transform var(--dur) var(--ease);
      }
      input[type="checkbox"]:checked { background: var(--success); }
      input[type="checkbox"]:checked::before { transform: translateX(18px); background: #fff; }
      input[type="checkbox"]:hover { box-shadow: 2px 2px 0 var(--ink); }
      pre {
        white-space: pre-wrap;
        background: var(--bg-input);
        color: var(--muted);
        font-family: var(--mono);
        padding: 14px;
        border: 2px solid var(--ink);
        border-radius: var(--r-md);
        max-height: 480px;
        overflow: auto;
        line-height: 1.5; font-size: 12.5px;
      }
      a { color: var(--accent); }
      .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap: 14px; }
      .status { display: inline-block; border-radius: var(--r-md); padding: 4px 12px; font-family: var(--pixel); font-weight: 400; font-size: 10px; text-transform: uppercase; background: var(--bg-overlay); color: var(--ink); border: 2px solid var(--ink); }
      .done { background: var(--success); border-color: var(--ink); color: #fff; }
      .error { background: var(--accent-2); border-color: var(--ink); color: #fff; }
      .cancelled { background: var(--warning); border-color: var(--ink); color: var(--ink); }
      .cancelling { background: var(--warning); border-color: var(--ink); color: var(--ink); }
      .hint { color: var(--faint); font-size: 12.5px; margin-top: 6px; line-height: 1.5; }
      .job-head { display: flex; justify-content: space-between; gap: 16px; align-items: center; margin-bottom: 16px; }
      .job-actions { display: flex; gap: 10px; flex-wrap: wrap; justify-content: flex-end; }
      .job-actions .button { width: auto; min-width: 132px; }
      .inline-form { margin: 0; display: inline; }
      .inline-form button { width: auto; min-width: 132px; }
      .panel-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 10px; }
      .panel-head h2 { margin: 0; }
      .panel-head .button { width: auto; min-width: 94px; padding: 8px 11px; }
      .progress-wrap { margin: 16px 0 18px; }
      .elapsed-line { display: flex; justify-content: space-between; gap: 12px; margin-bottom: 8px; color: var(--muted); font-size: 13px; font-weight: 600; }
      .progress-label { display: flex; justify-content: space-between; gap: 12px; margin-bottom: 8px; color: #cbc7ba; font-weight: 800; }
      .progress-label span { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
      .progress-track { height: 12px; border-radius: 999px; overflow: hidden; background: var(--bg-base); border: 1px solid var(--line); }
      .progress-fill {
        height: 100%;
        width: 0;
        background:
          repeating-linear-gradient(45deg, rgba(255,255,255,.18) 0 8px, transparent 8px 16px),
          linear-gradient(90deg, var(--accent), var(--accent-2), var(--accent-3));
        background-size: 28px 28px, auto;
        transition: width .35s ease;
        animation: progress-stripes 1.2s linear infinite;
      }
      .steps-strip { display: flex; gap: 8px; flex-wrap: wrap; margin: 4px 0 10px; }
      .step-chip {
        position: relative; overflow: hidden; flex: 1 1 96px; min-width: 96px;
        display: flex; flex-direction: column; gap: 2px; justify-content: center;
        padding: 10px 13px; border-radius: var(--r-md); border: 1px solid var(--line);
        background: var(--bg-input); color: var(--faint); font-weight: 600; font-size: 12.5px;
        transition: color .25s var(--ease), border-color .25s var(--ease), background .25s var(--ease);
      }
      .step-chip .step-fill { position: absolute; left: 0; top: 0; bottom: 0; width: 0; z-index: 0;
        background: linear-gradient(90deg, var(--accent-subtle), rgba(124,92,255,.04)); }
      .step-chip .step-name, .step-chip .step-time { position: relative; z-index: 1; }
      .step-chip .step-name { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
      .step-chip .step-time { font-size: 11px; font-weight: 600; color: var(--muted); font-variant-numeric: tabular-nums; }
      .step-chip.pending { opacity: .5; }
      .step-chip.active { color: var(--text); border-color: var(--accent); background: var(--bg-overlay); }
      .step-chip.active .step-fill { animation: stepfill 45s linear forwards; animation-delay: calc(-1 * var(--el)); }
      .step-chip.done { color: var(--text); border-color: rgba(56,211,154,.45); background: rgba(56,211,154,.1); }
      .step-chip.done .step-fill { width: 100%; background: linear-gradient(90deg, rgba(56,211,154,.3), rgba(56,211,154,.08)); }
      .step-chip.done .step-time { color: var(--success); }
      .step-chip.stopped { color: var(--text); border-color: rgba(240,104,115,.5); background: rgba(240,104,115,.1); }
      .step-chip.stopped .step-fill { width: 100%; background: linear-gradient(90deg, rgba(240,104,115,.3), rgba(240,104,115,.08)); }
      @keyframes stepfill { from { width: 6%; } to { width: 94%; } }
      .progress-current { color: rgba(255,255,255,.78); font-weight: 600; font-size: 13px; min-height: 18px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
      .job-layout { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 16px; align-items: stretch; margin-top: 16px; }
      .job-layout > .panel, .job-layout > aside { min-height: min(76vh, 860px); min-width: 0; }
      .log-box { height: calc(min(78vh, 900px) - 70px); max-height: none; overflow: auto; font-size: 13px; color: var(--text); }
      /* JOB PAGE fits the viewport: console + media take the REMAINING height and scroll
         INTERNALLY (invisible scrollbars) - the page itself never shows a scrollbar. */
      body:has(#job-root) { height: 100vh; min-height: 0; overflow: hidden; }
      body:has(#job-root) main { height: 100%; box-sizing: border-box; display: flex; flex-direction: column; min-height: 0; overflow-y: auto; scrollbar-width: none; }
      body:has(#job-root) main::-webkit-scrollbar { display: none; }
      #job-root { flex: 1 1 auto; display: flex; flex-direction: column; min-height: 0; }
      #job-root .job-layout { flex: 1 1 auto; min-height: 260px; }
      #job-root .job-layout > .panel, #job-root .job-layout > aside { min-height: 0; }
      #job-root .job-layout > .panel { display: flex; flex-direction: column; overflow: hidden; }
      #job-root .log-box { flex: 1 1 auto; height: auto; min-height: 120px; scrollbar-width: none; }
      #job-root .log-box::-webkit-scrollbar { display: none; }
      #job-root #media-sidebar-container { min-height: 0; overflow-y: auto; scrollbar-width: none; }
      #job-root #media-sidebar-container::-webkit-scrollbar { display: none; }
      /* Timeline-editor render: just a clean animated progress bar, vertically centred */
      #job-root[data-minimal="1"] .tl-render-top { display: flex; align-items: center; justify-content: space-between; padding: 8px 2px 0; }
      #job-root[data-minimal="1"] .tl-render-brand { display: flex; align-items: center; gap: 10px; font-family: var(--display); font-size: 15px; color: var(--ink); }
      #job-root[data-minimal="1"] .tl-render-brand img { image-rendering: pixelated; border-radius: 6px; }
      #job-root[data-minimal="1"] .tl-render-center { flex: 1 1 auto; display: flex; flex-direction: column; justify-content: center; align-items: center; gap: 8px; }
      .tl-render-progress { width: min(640px, 92%); text-align: center; }
      .tl-render-title { font-family: var(--display); font-size: clamp(15px, 2.6vw, 24px); color: var(--ink); margin-bottom: 20px; }
      .tl-render-title .tl-render-pct { color: var(--accent); font-family: var(--mono); font-variant-numeric: tabular-nums; margin-left: 8px; }
      .tl-render-bar { position: relative; height: 16px; border-radius: 999px; overflow: hidden; background: var(--bg-input); box-shadow: var(--sh-1); }
      /* DETERMINATE: the fill width = the real render %, animated smoothly, with a moving
         stripe sheen + a soft leading glow (the "effects"). */
      .tl-render-bar.det span { position: absolute; left: 0; top: 0; bottom: 0; border-radius: 999px;
        background:
          repeating-linear-gradient(115deg, rgba(255,255,255,.22) 0 12px, rgba(255,255,255,0) 12px 26px),
          linear-gradient(90deg, var(--accent), var(--accent-2));
        background-size: 44px 100%, 100% 100%;
        box-shadow: 0 0 14px 1px color-mix(in srgb, var(--accent) 60%, transparent);
        transition: width .5s cubic-bezier(.3,.7,.4,1);
        animation: tlbarstripes 1s linear infinite; }
      .tl-render-bar.det::after { content: ""; position: absolute; top: 0; bottom: 0; width: 40%;
        background: linear-gradient(90deg, transparent, rgba(255,255,255,.28), transparent);
        animation: tlbarsheen 1.7s ease-in-out infinite; pointer-events: none; }
      @keyframes tlbarstripes { to { background-position: 44px 0, 0 0; } }
      @keyframes tlbarsheen { 0% { left: -40%; } 100% { left: 100%; } }
      /* INDETERMINATE (before any % is known): the old sliding block */
      .tl-render-bar.indet span { position: absolute; top: 0; bottom: 0; width: 38%; border-radius: 999px; background: linear-gradient(90deg, var(--accent), var(--accent-2)); animation: tlrenderbar 1.15s cubic-bezier(.5,.05,.5,.95) infinite; }
      @keyframes tlrenderbar { 0% { left: -40%; } 100% { left: 102%; } }
      .tl-render-sub { display: flex; justify-content: space-between; align-items: baseline; gap: 12px; margin-top: 12px; color: var(--muted); font-size: 13px; }
      .tl-render-sub strong { color: var(--ink); font-family: var(--mono); }
      @media (prefers-reduced-motion: reduce) {
        .tl-render-bar.indet span { animation-duration: 2.4s; }
        .tl-render-bar.det span { animation: none; } .tl-render-bar.det::after { animation: none; display: none; }
      }
      .preview-grid { display: grid; grid-template-columns: 1fr; gap: 12px; }
      .preview img { display: block; width: 100%; max-height: 420px; object-fit: contain; background: var(--bg-base); border: 1px solid var(--line); border-radius: var(--r-md); transition: transform .18s ease, filter .18s ease; }
      .preview:hover img { transform: scale(1.012); filter: contrast(1.04) saturate(1.02); }
      .preview strong { display: block; margin-bottom: 8px; }
      .preview-button { width: 100%; padding: 0; background: transparent; border: 0; color: inherit; text-align: left; }
      .preview-button, .preview-button:hover { background: transparent; box-shadow: none; transform: none; }
      .preview-button::after { display: none; }
      .media-sidebar { height: min(78vh, 900px); overflow: auto; }
      .media-sidebar form { display: block; background: transparent; border: 0; padding: 0; box-shadow: none; }
      .scrape-mini-progress { margin: 2px 0 12px; padding: 10px 11px; border: 1px solid var(--line); border-radius: var(--r-md); background: var(--bg-input); }
      .scrape-mini-head { display: flex; align-items: center; justify-content: space-between; gap: 10px; color: var(--text); font-size: 12px; font-weight: 750; }
      .scrape-mini-head strong { color: var(--accent); font-variant-numeric: tabular-nums; }
      .scrape-mini-track { height: 6px; margin: 7px 0; overflow: hidden; border-radius: var(--r-full); background: var(--bg-base); border: 1px solid var(--line); }
      .scrape-mini-fill { height: 100%; border-radius: inherit; background: linear-gradient(90deg, var(--accent), var(--accent-3)); transition: width .35s var(--ease); }
      .scrape-mini-progress.running .scrape-mini-fill { background-size: 22px 22px; animation: progress-stripes 1.2s linear infinite; }
      .scrape-mini-progress.done .scrape-mini-fill { background: var(--success); }
      .scrape-mini-progress.stopped .scrape-mini-fill { background: var(--accent-2); }
      .scrape-mini-status { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--muted); font-size: 11px; line-height: 1.35; }
      .scrape-mini-counts { display: flex; flex-wrap: wrap; gap: 5px; margin-top: 7px; }
      .scrape-mini-counts span { padding: 3px 6px; border: 1px solid var(--line); border-radius: var(--r-full); color: var(--faint); background: var(--bg-raised); font-size: 10px; font-weight: 700; font-variant-numeric: tabular-nums; }
      .media-toolbar { display: flex; gap: 8px; align-items: center; margin-bottom: 10px; }
      .media-toolbar button { width: 100%; padding: 9px 10px; }
      .media-tiny-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; }
      .media-tile {
        position: relative;
        border: 1px solid var(--line);
        background: var(--bg-input);
        border-radius: var(--r-md);
        padding: 6px;
        min-width: 0;
      }
      .media-tile.queued { border-color: var(--accent); box-shadow: inset 0 0 0 1px var(--accent-subtle); }
      .media-tile img { display: block; width: 100%; aspect-ratio: 9 / 14; object-fit: cover; border-radius: 9px; background: var(--bg-base); }
      .media-tile video { display: block; width: 100%; aspect-ratio: 9 / 14; object-fit: cover; border-radius: 9px; background: var(--bg-base); }
      .media-audio-tile { display: grid; place-items: center; width: 100%; aspect-ratio: 9 / 14; border-radius: 9px; background: var(--bg-base); color: var(--faint); border: 1px solid var(--line); font-weight: 700; }
      .media-tile label { display: flex; gap: 6px; align-items: center; margin-top: 6px; color: var(--text); font-size: 12px; font-weight: 600; }
      .media-tile input { margin: 0; }
      .media-tile small { display: block; color: var(--faint); font-size: 11px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; margin-top: 4px; }
      .media-kind { position: absolute; top: 10px; left: 10px; padding: 3px 6px; border-radius: 5px; background: rgba(8,9,12,.82); border: 1px solid var(--line); color: var(--muted); font-size: 10px; font-weight: 700; text-transform: uppercase; z-index: 2; }
      /* .media-tile .media-del: extra specificity beats the global button rule
         (which would otherwise force a full-width, filled, centered box). */
      .media-tile .media-del {
        position: absolute; top: 5px; right: 5px; z-index: 4;
        width: 20px; height: 20px; min-width: 0; padding: 0; border-radius: 6px;
        display: inline-flex; align-items: center; justify-content: center;
        font-size: 15px; line-height: 1; font-weight: 700; cursor: pointer;
        background: transparent; color: #fff; border: 0;
        box-shadow: none; text-shadow: 0 1px 4px rgba(0,0,0,.95); opacity: .6;
        transition: opacity var(--dur) var(--ease), color var(--dur) var(--ease), background var(--dur) var(--ease);
      }
      .media-tile .media-del::after { display: none; }
      .media-tile .media-del:hover { opacity: 1; color: #fff; background: rgba(240,104,115,.85); border: 0; transform: none; box-shadow: none; }
      /* green checkmark on DECLINED tiles: manually accept the clip into the project */
      .media-tile .media-accept { position: absolute; top: 6px; left: 6px; z-index: 3; width: 24px; height: 24px; min-width: 0; padding: 0; display: grid; place-items: center; border: 0; border-radius: 7px; background: rgba(31,191,107,.92); color: #fff; font-size: 14px; line-height: 1; cursor: pointer; }
      .media-tile .media-accept::after { display: none; }
      .media-tile .media-accept:hover { background: #1fbf6b; color: #fff; transform: none; box-shadow: none; }
      .media-tile.declined-tile { border-color: var(--accent-2); }
      /* Viral Transformation topic chips */
      .vt-topics { display: grid; grid-template-columns: repeat(auto-fill, minmax(170px, 1fr)); gap: 8px; margin-top: 4px; }
      .vt-topics .vt-topic { width: 100%; min-width: 0; padding: 9px 10px; font-size: 12px; font-weight: 700; text-transform: none; }
      .vt-topics .vt-topic.active { background: var(--accent); color: #fff; }
      /* no sticky: a sticky right column scrolls out of step with the left column. */
      .preview-section { grid-column: 2; align-self: start; }
      /* fixed 3 columns so tiles stay the SAME size on every tab (was auto-fit, which
         resized tiles when a tab like "web images" had a different item count). */
      .loaded-media-panel .media-tiny-grid { grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; }
      .loaded-media-panel .media-tab-panel { max-height: none; }
      .media-tabs { display: flex; flex-wrap: nowrap; overflow-x: auto; padding-bottom: 5px; gap: 6px; margin: 10px 0; }
      .media-tab {
        border: 1px solid var(--line); background: var(--bg-input); color: var(--muted); padding: 7px 13px;
        border-radius: var(--r-full); font-size: 12px; font-weight: 600; cursor: pointer;
      }
      .media-tab.active { border-color: var(--accent); background: var(--accent-subtle); color: var(--text); }
      .media-tab-panel[hidden] { display: none; }
      .output-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr)); gap: 12px; margin-bottom: 16px; }
      .output-card { display: grid; gap: 10px; align-content: start; border-top: 1px solid var(--line); }
      .output-card strong { font-size: 15px; }
      .output-card small { color: var(--faint); word-break: break-all; line-height: 1.35; }
      .output-actions { display: flex; gap: 8px; }
      .output-actions .button { width: auto; flex: 1; padding: 10px 12px; }
      .media-inline { width: 100%; max-height: 260px; border-radius: var(--r-md); background: var(--bg-base); border: 1px solid var(--line); object-fit: contain; }
      .asset-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(310px, 1fr)); gap: 16px; }
      /* min-width:0 so a long unbreakable title (e.g. SFX MASTER - SHORT_..._TIMELINE_...) can't
         force the card wider than its grid column and overflow into the neighbour */
      .asset-card { display: grid; gap: 12px; align-content: start; padding: 14px; min-width: 0; }
      .asset-card .asset-figure { display: block; padding: 0; margin: 0; border: 0; background: none; box-shadow: none; width: 100%; border-radius: var(--r-md); overflow: hidden; cursor: zoom-in; }
      .asset-card .asset-figure::after { display: none; }
      .asset-thumb { display: block; width: 100%; aspect-ratio: 16 / 10; object-fit: cover; background: var(--bg-base); border: 1px solid var(--line); border-radius: var(--r-md); }
      .asset-body { display: grid; gap: 6px; min-width: 0; }
      .asset-body h2 { margin: 0; font-size: 17px; line-height: 1.2; overflow-wrap: anywhere; word-break: break-word; }
      .asset-sub { color: var(--faint); font-size: 12px; }
      .asset-meta { display: flex; gap: 6px; flex-wrap: wrap; }
      .asset-pill { border: 1px solid var(--line); background: var(--bg-input); padding: 4px 11px; border-radius: var(--r-full); font-size: 11px; font-weight: 600; color: var(--muted); }
      .asset-primary { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
      .asset-primary .button { width: 100%; padding: 10px 12px; font-size: 13px; display: inline-flex; align-items: center; justify-content: center; gap: 6px; }
      .asset-primary .asset-go {
        color: #fff; border: 1px solid var(--accent-active);
        background: linear-gradient(180deg, var(--accent-hover), var(--accent));
        box-shadow: 0 6px 16px rgba(124,92,255,.32);
      }
      .asset-primary .asset-go:hover { background: linear-gradient(180deg, #a18dff, var(--accent-hover)); border-color: var(--accent-hover); }
      .asset-timeline { width: 100%; padding: 9px 12px; font-size: 13px; font-weight: 600; display: inline-flex; align-items: center; justify-content: center; gap: 6px; background: var(--bg-input); border: 1px solid var(--line); color: var(--muted); box-shadow: none; }
      .asset-timeline:hover { border-color: var(--accent); background: var(--bg-overlay); color: var(--text); box-shadow: none; }
      .asset-timeline-locked { text-align: center; padding: 9px 12px; font-size: 12px; color: var(--faint); border: 1px dashed var(--line); border-radius: var(--r-md); }
      .media-page img, .media-page video { width: 100%; max-height: 78vh; object-fit: contain; background: var(--bg-base); border: 1px solid var(--line); border-radius: var(--r-md); }
      .media-page audio { width: 100%; }
      .file-list { display: grid; gap: 8px; padding-left: 0; list-style: none; }
      .file-list a { display: block; padding: 10px 14px; border-radius: var(--r-md); background: var(--bg-input); border: 1px solid var(--line); text-decoration: none; color: var(--text); }
      .file-list a:hover { border-color: var(--accent); background: var(--bg-overlay); }
      .lightbox { position: fixed; inset: 0; display: none; place-items: center; padding: 24px; background: rgba(5,6,9,.82); backdrop-filter: blur(8px); -webkit-backdrop-filter: blur(8px); z-index: 50; }
      .lightbox.open { display: grid; }
      .lightbox-inner { width: min(1100px, 96vw); }
      .lightbox-bar { display: flex; justify-content: space-between; align-items: center; gap: 12px; margin-bottom: 10px; color: var(--text); }
      .lightbox-title { font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
      .lightbox-close { width: auto; padding: 8px 14px; background: var(--bg-overlay); color: var(--text); border: 1px solid var(--line-strong); }
      .lightbox img { display: block; width: 100%; max-height: 84vh; object-fit: contain; background: var(--bg-base); border: 1px solid var(--line); border-radius: var(--r-md); }
      @keyframes progress-stripes { from { background-position: 0 0, 0 0; } to { background-position: 28px 0, 0 0; } }
      @media (prefers-reduced-motion: reduce) {
        *, *::before, *::after { animation-duration: .01ms !important; transition-duration: .01ms !important; }
      }
      @media (max-width: 820px) {
        main { padding: 16px; }
        form, .controls, .top, .job-layout, .job-head { grid-template-columns: 1fr; display: grid; }
        .project-load-controls { grid-template-columns: 1fr; }
        .project-load-controls .button { width: 100%; }
        textarea { min-height: 300px; }
        textarea.visual-textarea { min-height: 180px; }
        .job-actions { justify-content: stretch; }
        .job-actions .button { width: 100%; }
        .job-layout { grid-template-columns: 1fr; }
        .log-box { height: 52vh; }
      }
      .modal-overlay {
        position: fixed; inset: 0; width: 100%; height: 100%;
        background: rgba(5,6,9,.7); backdrop-filter: blur(4px); -webkit-backdrop-filter: blur(4px); z-index: 1000;
        display: flex; justify-content: center; align-items: flex-start;
        padding: 7vh 16px 24px; box-sizing: border-box; overflow-y: auto;
        opacity: 0; pointer-events: none; transition: opacity 0.2s;
      }
      .modal-overlay.active { opacity: 1; pointer-events: auto; }
      .modal-content {
        position: relative; margin: auto;
        background: var(--bg-raised);
        padding: 26px 28px; border-radius: var(--r-lg);
        border: 1px solid var(--line-strong); width: 100%; max-width: 600px;
        max-height: 86vh; overflow-y: auto;
        box-shadow: var(--sh-3);
      }
      .modal-content h2 { margin-top: 0; padding-right: 36px; }
      /* high-specificity so it beats the global button rule (no full-width box) */
      .modal-content .modal-close {
        position: absolute; top: 12px; right: 14px;
        width: auto; min-width: 0; margin: 0; padding: 0 6px;
        border: none; background: none; box-shadow: none;
        color: var(--muted); font-size: 24px; line-height: 1; font-weight: 400; cursor: pointer;
      }
      .modal-content .modal-close::after { display: none; }
      .modal-content .modal-close:hover { color: var(--text); background: none; box-shadow: none; transform: none; }
      /* --- Preset popup --- */
      /* pin near the top instead of vertically centered (the inherited margin:auto
         centered this short popup, making it open too far down the viewport) */
      .preset-popup-card { max-width: 440px; padding: 22px 22px 20px; margin: 0 auto auto; }
      .pp-group-label { font-family: var(--pixel); text-transform: uppercase; font-size: 10px; letter-spacing: .12em; color: var(--faint); margin: 14px 0 6px; }
      .pp-group-label:first-child { margin-top: 2px; }
      .pp-empty { color: var(--faint); font-size: 13px; padding: 4px 2px 2px; }
      .pp-row { display: flex; align-items: stretch; gap: 6px; margin-bottom: 6px; }
      .pp-row .pp-load {
        flex: 1; text-align: left; justify-content: flex-start;
        width: auto; min-width: 0; margin: 0; padding: 10px 12px;
        background: var(--bg-input); color: var(--ink);
        border: 1px solid var(--line-strong); border-radius: var(--r-md);
        box-shadow: var(--sh-1); font-weight: 700; font-size: 14px; cursor: pointer;
      }
      .pp-row .pp-load::after { display: none; }
      .pp-row .pp-load:hover { transform: translate(-1px,-1px); box-shadow: 4px 4px 0 var(--ink); background: var(--bg-raised); }
      .pp-row .pp-del {
        width: 38px; min-width: 38px; margin: 0; padding: 0;
        background: var(--bg-input); color: var(--muted);
        border: 1px solid var(--line-strong); border-radius: var(--r-md);
        box-shadow: var(--sh-1); font-size: 18px; line-height: 1; cursor: pointer;
      }
      .pp-row .pp-del::after { display: none; }
      .pp-row .pp-del:hover { color: #fff; background: var(--accent-2); border-color: var(--accent-2); transform: translate(-1px,-1px); box-shadow: 3px 3px 0 var(--ink); }
      .pp-save { margin-top: 16px; padding-top: 14px; border-top: 1px solid var(--line); }
      .pp-save-label { font-family: var(--pixel); text-transform: uppercase; font-size: 10px; letter-spacing: .12em; color: var(--faint); margin-bottom: 8px; }
      .pp-save-row { display: flex; gap: 6px; }
      .pp-save-row #pp-name { flex: 1; min-width: 0; margin: 0; }
      .pp-save-row .button { width: auto; min-width: 0; margin: 0; white-space: nowrap; }
      .pp-overwrite {
        width: 100%; margin: 8px 0 0; padding: 9px 12px;
        background: var(--bg-input); color: var(--ink);
        border: 1px dashed var(--line-strong); border-radius: var(--r-md);
        box-shadow: none; font-weight: 700; font-size: 13px; cursor: pointer;
      }
      .pp-overwrite::after { display: none; }
      .pp-overwrite:hover { background: var(--accent); color: #fff; border-style: solid; border-color: var(--accent); }
      /* --- Form-page polish --- */
      .mode-selector { background: var(--bg-input); }
      .mode-selector > label { font-size: 16px; letter-spacing: .2px; }
      .run-mode-btn { min-width: 168px; }
      .button.primary { border-color: var(--accent-active); background: linear-gradient(180deg, var(--accent-hover), var(--accent)); color: #fff; box-shadow: 0 8px 22px rgba(124,92,255,.36); }
      .loaded-media-panel {
        background: var(--bg-raised) !important;
        border: 1px solid var(--line) !important; border-radius: var(--r-lg) !important;
        box-shadow: var(--sh-1);
      }
      #project-media-hint {
        text-align: center; min-height: 220px; padding: 64px 24px;
        border: 1px dashed var(--line-strong); border-radius: var(--r-md); color: var(--faint);
        background: repeating-linear-gradient(45deg, rgba(255,255,255,.012) 0 12px, transparent 12px 24px);
      }
      /* animated loading screen for the asset browser + timeline library (not a bare text line) */
      .media-loading {
        display: flex; flex-direction: column; align-items: center; justify-content: center;
        gap: 18px; min-height: 240px; padding: 48px 24px; text-align: center;
        border: 1px dashed var(--line-strong); border-radius: var(--r-md);
        background: repeating-linear-gradient(45deg, rgba(255,255,255,.012) 0 12px, transparent 12px 24px);
      }
      .media-loading .ml-spinner {
        width: 48px; height: 48px; border-radius: 50%;
        border: 4px solid var(--line-strong); border-top-color: var(--accent);
        animation: ml-spin .8s linear infinite;
      }
      @keyframes ml-spin { to { transform: rotate(360deg); } }
      .media-loading .ml-text {
        font-family: var(--pixel); font-size: 11px; letter-spacing: .12em; text-transform: uppercase; color: var(--muted);
      }
      .media-loading .ml-text::after { content: "\\2026"; display: inline-block; width: 1.2em; text-align: left; animation: ml-dots 1.1s steps(4, end) infinite; }
      @keyframes ml-dots { 0% { content: ""; } 25% { content: "."; } 50% { content: ".."; } 75% { content: "..."; } }
      .media-loading .ml-bar { width: min(240px, 70%); height: 8px; border: 1px solid var(--line-strong); border-radius: 6px; overflow: hidden; background: var(--bg-input); }
      .media-loading .ml-bar > i { display: block; height: 100%; width: 40%; background: linear-gradient(90deg, transparent, var(--accent), transparent); animation: ml-slide 1.1s ease-in-out infinite; }
      @keyframes ml-slide { 0% { transform: translateX(-120%); } 100% { transform: translateX(320%); } }
      /* full-screen loading overlay for slow PAGE navigations (Assets, Timeline editor, ...) -
         an in-page loading screen is impossible during a full navigation, so we cover the old
         page until the new one arrives */
      #nav-loading {
        position: fixed; inset: 0; z-index: 3000; display: none;
        align-items: center; justify-content: center; flex-direction: column; gap: 18px;
        background: color-mix(in srgb, var(--bg-base) 78%, transparent);
        backdrop-filter: blur(3px); -webkit-backdrop-filter: blur(3px);
      }
      #nav-loading.show { display: flex; animation: nav-fade .14s var(--ease) both; }
      @keyframes nav-fade { from { opacity: 0; } to { opacity: 1; } }
      #nav-loading .ml-spinner { width: 54px; height: 54px; border: 4px solid var(--line-strong); border-top-color: var(--accent); border-radius: 50%; animation: ml-spin .8s linear infinite; }
      #nav-loading .ml-bar { width: min(260px, 60vw); height: 8px; border: 1px solid var(--line-strong); border-radius: 6px; overflow: hidden; background: var(--bg-input); }
      #nav-loading .ml-bar > i { display: block; height: 100%; width: 40%; background: linear-gradient(90deg, transparent, var(--accent), transparent); animation: ml-slide 1.1s ease-in-out infinite; }
      #nav-loading .ml-text { font-family: var(--pixel); font-size: 12px; letter-spacing: .12em; text-transform: uppercase; color: var(--text); }
      #nav-loading .ml-text::after { content: "\\2026"; display: inline-block; width: 1.2em; text-align: left; animation: ml-dots 1.1s steps(4, end) infinite; }
      body { overflow-x: hidden; }
      @media (max-width: 820px) {
        .top { display: flex; flex-direction: column; align-items: stretch; gap: 14px; }
        .brand { gap: 10px; flex-direction: column; align-items: flex-start; min-width: 0; }
        .brand > div { min-width: 0; width: 100%; }
        .brand-mark { width: 46px; height: 46px; }
        h1 { font-size: 23px; }
        .sub { font-size: 13px; max-width: 100%; overflow-wrap: anywhere; }
        .nav-actions { justify-content: stretch; min-width: 0; }
        .nav-actions .button { flex: 1; min-width: 0; }
        .run-mode-btn { min-width: 0; }
      }
    </style>
    """)


def app_script():
    return """
    <script>
      (function () {
        window.toggleTheme = function () {
          var dark = !document.documentElement.classList.contains("theme-dark");
          document.documentElement.classList.toggle("theme-dark", dark);
          try { localStorage.setItem("shortslab-theme", dark ? "dark" : "light"); } catch (e) {}
        };
        // ===== full-screen loading overlay for slow PAGE navigations (Assets / Timeline / ...) =====
        // Those are full server-rendered navigations (1-2s): the browser shows the OLD page frozen
        // until the new one arrives, so an in-page loader is impossible. We cover it here instead.
        (function () {
          var overlay = null, showTimer = null;
          function build() {
            if (overlay) return overlay;
            overlay = document.createElement("div");
            overlay.id = "nav-loading";
            overlay.innerHTML = '<div class="ml-spinner"></div><div class="ml-bar"><i></i></div><div class="ml-text">Loading</div>';
            (document.body || document.documentElement).appendChild(overlay);
            return overlay;
          }
          function show(label) {
            clearTimeout(showTimer);
            // small delay so an instant/cached load never flashes the overlay
            showTimer = setTimeout(function () {
              var o = build();
              o.querySelector(".ml-text").textContent = label || "Loading";
              o.classList.add("show");
            }, 110);
          }
          function hide() { clearTimeout(showTimer); if (overlay) overlay.classList.remove("show"); }
          window.showNavLoading = show;
          // Reliable "Download" that works in the NATIVE window too (WebView2 ignores <a download>):
          // prefer the pywebview Save-As dialog, else copy the render into the Downloads folder.
          function saveRenderServer(pathEnc, done) {
            fetch("/save-render?path=" + pathEnc).then(function (r) { return r.json(); }).then(function (r) {
              if (r && r.ok) done("Saved to Downloads", true); else done("Save failed", false);
            }).catch(function () { done("Save failed", false); });
          }
          window.saveRender = function (pathEnc, btn) {
            function done(msg, ok) {
              if (!btn) return;
              var orig = btn.getAttribute("data-label") || btn.textContent;
              btn.setAttribute("data-label", orig);
              btn.textContent = (ok ? "✓ " : "⚠ ") + msg; btn.disabled = true;
              setTimeout(function () { btn.textContent = orig; btn.disabled = false; }, 3500);
            }
            try {
              if (window.pywebview && window.pywebview.api && window.pywebview.api.save_file) {
                window.pywebview.api.save_file(decodeURIComponent(pathEnc)).then(function (r) {
                  if (r && r.ok) done("Saved", true);
                  else if (r && r.cancelled) { /* user cancelled the dialog */ }
                  else saveRenderServer(pathEnc, done);
                }).catch(function () { saveRenderServer(pathEnc, done); });
                return;
              }
            } catch (e) {}
            // #121 - browser: let the user CHOOSE the destination (Save As), else copy to Downloads
            if (window.showSaveFilePicker) {
              var nm = (decodeURIComponent(pathEnc).split(/[\\\\/]/).pop()) || "video.mp4";
              (async function () {
                try {
                  var h = await window.showSaveFilePicker({ suggestedName: nm,
                    types: [{ description: "MP4 video", accept: { "video/mp4": [".mp4"] } }] });
                  var resp = await fetch("/file?path=" + pathEnc);
                  var w = await h.createWritable(); await resp.body.pipeTo(w);
                  done("Saved", true);
                } catch (e) {
                  if (e && e.name === "AbortError") return;   // user cancelled
                  saveRenderServer(pathEnc, done);
                }
              })();
              return;
            }
            saveRenderServer(pathEnc, done);
          };
          document.addEventListener("click", function (e) {
            if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
            var a = e.target.closest("a[href]");
            if (!a || a.target === "_blank" || a.hasAttribute("download")) return;
            var href = a.getAttribute("href") || "";
            if (!href || href.charAt(0) === "#" || href.indexOf("javascript:") === 0) return;
            var u; try { u = new URL(a.href, location.href); } catch (err) { return; }
            if (u.origin !== location.origin) return;
            if (u.pathname === location.pathname && u.search === location.search && u.hash) return; // in-page anchor
            var label = u.pathname.indexOf("/timeline") === 0 ? "Opening timeline editor"
                      : u.pathname.indexOf("/assets") === 0 ? "Loading assets"
                      : "Loading";
            show(label);
          }, true);
          // restored from bfcache / navigation cancelled -> clear it
          window.addEventListener("pageshow", hide);
          window.addEventListener("pagehide", hide);
        })();
        // ===== Onboarding wizard: headline types, then script -> visual -> voice+speaker -> main =====
        var WIZ_INTRO = "What are we creating today\\u2026?";
        var wizCur = null;
        // Step 0 = mode menu. Visuals-From-Script flow SKIPS the Visual Direction step (2):
        // script (1) -> voice/speaker (3) -> create (4). The voice script is the visual authority.
        var WIZ_STEPS = [1, 3, 4];
        function wizGoto(step) {
          var hl = document.getElementById("wiz-headline");
          if (hl) hl.style.display = (step === 0) ? "" : "none";   // "What are we creating today?" sits over the mode menu
          Array.prototype.forEach.call(document.querySelectorAll("[data-step]"), function (el) {
            var s = parseInt(el.getAttribute("data-step"), 10);
            if (s === step) { el.style.display = ""; el.classList.remove("wiz-anim"); void el.offsetWidth; el.classList.add("wiz-anim"); }
            else { el.style.display = "none"; el.classList.remove("wiz-anim"); }
          });
          wizCur = step;
          var tn = document.getElementById("wiz-topnav");
          var bb = document.getElementById("wiz-back-btn");
          var cc = document.getElementById("wiz-cont-btn");
          var inFlow = WIZ_STEPS.indexOf(step) !== -1;
          if (tn) tn.style.display = inFlow ? "block" : "none";                       // no topnav on the mode menu
          if (bb) bb.setAttribute("data-hidden", "0");                               // Back always available (first step -> mode menu)
          if (cc) cc.style.display = (step === WIZ_STEPS[WIZ_STEPS.length - 1]) ? "none" : "";  // no Continue on the create step
          try { if (typeof playClick === "function") playClick(); } catch (e) {}
          try { window.scrollTo({ top: 0, behavior: "smooth" }); } catch (e) { window.scrollTo(0, 0); }
        }
        window.wizNext = function () { var i = WIZ_STEPS.indexOf(wizCur); if (i === -1) i = 0; wizGoto(WIZ_STEPS[Math.min(WIZ_STEPS.length - 1, i + 1)]); };
        window.wizBack = function () { var i = WIZ_STEPS.indexOf(wizCur); if (i <= 0) { wizGoto(0); return; } wizGoto(WIZ_STEPS[i - 1]); };
        window.selectMode = function (mode) {
          try { if (typeof playClick === "function") playClick(); } catch (e) {}
          if (mode === "reddit") { window.location.href = "/reddit"; return; }
          if (mode === "sfx") { window.location.href = "/sfx"; return; }
          if (mode === "captions") { window.location.href = "/captions"; return; }
          if (mode === "visual") { window.location.href = "/visual"; return; }
          if (mode === "viraltrans") { window.location.href = "/viraltrans"; return; }
          if (mode === "longform") { window.location.href = "/longform"; return; }
          wizGoto(1);   // Visuals From Script -> existing script workflow
        };
        function wizTypeIntro(cb) {
          var el = document.getElementById("wiz-type"); if (!el) { if (cb) cb(); return; }
          var i = 0; el.textContent = "";
          (function tick() {
            if (i <= WIZ_INTRO.length) {
              el.textContent = WIZ_INTRO.slice(0, i);
              if (i > 0 && WIZ_INTRO.charAt(i - 1) !== " ") { try { if (typeof playKeyTick === "function") playKeyTick(); } catch (e) {} }
              i++; setTimeout(tick, 60);
            } else if (cb) cb();
          })();
        }
        function wizInit() {
          if (!document.getElementById("wizard")) return;
          var sf = document.getElementById("script-field");
          var loaded = (document.getElementById("loaded-project-source") || {}).value;
          var hasScript = sf && sf.value && sf.value.trim();
          // Show the "What are we creating today" intro on a FRESH app launch (even if a draft
          // script was restored), but skip it on a mid-session reload so it isn't repetitive.
          // sessionStorage clears when the app window closes, so each launch = a fresh session.
          var seen = false;
          try { seen = sessionStorage.getItem("shortslab-wiz-seen") === "1"; } catch (e) {}
          if (loaded || (hasScript && seen)) { wizGoto(4); return; }
          try { sessionStorage.setItem("shortslab-wiz-seen", "1"); } catch (e) {}
          // fresh start: show the headline, type it, then reveal the script step below it
          var hl = document.getElementById("wiz-headline"); if (hl) hl.style.display = "";
          Array.prototype.forEach.call(document.querySelectorAll("[data-step]"), function (el) { el.style.display = "none"; });
          // Browsers block Web Audio until the first user gesture, so the typewriter
          // ticks are silent if we type on load. Wait for the first real interaction
          // (which also unlocks the audio context) and type WITH sound; a short
          // fallback types it anyway so the headline never stays blank.
          var EVT = ["pointerdown", "keydown", "touchstart"];
          var started = false;
          function startIntro() {
            if (started) return; started = true;
            EVT.forEach(function (e) { window.removeEventListener(e, startIntro, true); });
            try { uiCtx(); } catch (e) {}
            wizTypeIntro(function () { setTimeout(function () { wizGoto(0); }, 500); });   // -> mode menu
          }
          EVT.forEach(function (e) { window.addEventListener(e, startIntro, true); });
          setTimeout(startIntro, 4000);
        }
        window.wizInit = wizInit;
        function termList() {
          var hid = document.getElementById("scrape-terms");
          if (!hid) return [];
          return hid.value.split(",").map(function (t) { return t.trim(); }).filter(Boolean);
        }
        function setTerms(list) {
          var hid = document.getElementById("scrape-terms"); if (!hid) return;
          hid.value = list.join(", ");
          renderTermChips();
          if (typeof saveFormStateSoon === "function") saveFormStateSoon();
        }
        function renderTermChips() {
          var box = document.getElementById("term-chips"); if (!box) return;
          var list = termList();
          box.innerHTML = list.map(function (t, i) {
            var safe = t.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
            return '<span class="chip">' + safe + '<button type="button" onclick="removeTerm(' + i + ')" aria-label="Remove">&times;</button></span>';
          }).join("");
        }
        window.renderTermChips = renderTermChips;
        window.addTerm = function () {
          var inp = document.getElementById("term-input"); if (!inp) return;
          var val = inp.value.trim(); if (!val) return;
          var list = termList();
          val.split(",").map(function (t) { return t.trim(); }).filter(Boolean).forEach(function (t) {
            if (list.indexOf(t) === -1) list.push(t);
          });
          inp.value = "";
          setTerms(list);
        };
        window.removeTerm = function (i) {
          var list = termList(); list.splice(i, 1); setTerms(list);
        };
        window.setProjectMode = function (mode) {
          var hidden = document.getElementById("loaded-project-mode");
          if (hidden) hidden.value = mode;
          document.querySelectorAll("#loaded-actions-panel .action-btn").forEach(function (b) {
            b.classList.toggle("active", b.getAttribute("data-mode") === mode);
          });
        };
        window.showLoadedActions = function () {
          var panel = document.getElementById("loaded-actions-panel");
          if (panel) panel.style.display = "";
          document.body.classList.add("project-loaded");   // lets the create-bar shrink to share the viewport
          window.setProjectMode("normal");
        };
        window.hideLoadedActions = function () {
          var panel = document.getElementById("loaded-actions-panel");
          if (panel) panel.style.display = "none";
          document.body.classList.remove("project-loaded");
          var hidden = document.getElementById("loaded-project-mode");
          if (hidden) hidden.value = "normal";
        };
        window.openTimeline = function () {
          var src = document.getElementById("loaded-project-source");
          var slug = src ? src.value : "";
          if (slug) window.location.href = "/timeline?slug=" + encodeURIComponent(slug);
        };

        function stickLogToBottom() {
          var log = document.getElementById("job-log");
          if (log) log.scrollTop = log.scrollHeight;
        }

        var autosaveTimer = null;
        // v2: advanced toggles became always-on hidden inputs; bump key so stale
        // boolean localStorage from the old checkboxes can't corrupt them to "true".
        var autosaveKey = "autonomous-shorts-agent-ui-state-v2";
        var autosaveSkip = {
          initial_replace_media_path: true,
          loaded_project_source: true,
          slug: true,
          loaded_project_mode: true,
          autonomous_director: true,
          use_audio_timing: true,
          use_llm_search: true,
          use_llm_video_review: true,
          auto_web_images: true,
          generate_missing_sfx: true,
          allow_gpt: true,
          allow_seedance: true,
          background_music_enabled: true
        };

        function collectFormState() {
          var form = document.getElementById("short-form");
          if (!form) return null;
          var state = {};
          Array.prototype.forEach.call(form.querySelectorAll("input[name], textarea[name], select[name]"), function (field) {
            if (field.type === "file") return;
            if (autosaveSkip[field.name]) return;
            state[field.name] = field.type === "checkbox" ? field.checked : field.value;
          });
          return state;
        }

        function applyFormState(state) {
          var form = document.getElementById("short-form");
          if (!form || !state) return;
          Array.prototype.forEach.call(form.querySelectorAll("input[name], textarea[name], select[name]"), function (field) {
            if (field.type === "file" || !(field.name in state)) return;
            if (field.type === "checkbox") {
              field.checked = !!state[field.name];
            } else {
              field.value = state[field.name] == null ? "" : String(state[field.name]);
            }
          });
        }

        function sendFormState(state) {
          if (!state) return;
          fetch("/ui-state", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(state),
            keepalive: true
          }).catch(function () {});
        }

        function saveFormStateSoon() {
          var state = collectFormState();
          if (!state) return;
          try { localStorage.setItem(autosaveKey, JSON.stringify(state)); } catch (err) {}
          clearTimeout(autosaveTimer);
          autosaveTimer = setTimeout(function () { sendFormState(state); }, 350);
        }

        function setupFormAutosave() {
          var form = document.getElementById("short-form");
          if (!form) return;
          try {
            var localState = JSON.parse(localStorage.getItem(autosaveKey) || "null");
            if (localState) applyFormState(localState);
          } catch (err) {}
          form.addEventListener("input", saveFormStateSoon);
          form.addEventListener("change", saveFormStateSoon);
          window.addEventListener("beforeunload", function () {
            var state = collectFormState();
            if (!state) return;
            try { localStorage.setItem(autosaveKey, JSON.stringify(state)); } catch (err) {}
            try {
              if (navigator.sendBeacon) {
                navigator.sendBeacon("/ui-state", new Blob([JSON.stringify(state)], { type: "application/json" }));
              } else {
                sendFormState(state);
              }
            } catch (err) {}
          });
        }

        function setupProjectLoader() {
          var status = document.getElementById("project-load-status");
          var mediaBox = document.getElementById("project-media-preview");
          var loadedSource = document.getElementById("loaded-project-source");
          function setStatus(text) {
            if (status) status.textContent = text || "";
          }
          var ttPollTimer = null;
          function ttRender(st) {
            var dot = document.getElementById("tt-status-dot");
            var txt = document.getElementById("tt-status-text");
            var btn = document.getElementById("tt-login-btn");
            if (st && st.ready) {
              window.SCRAPE_READY = true; window.TIKTOK_READY = true;
              if (dot) dot.className = "tt-status on";
              if (txt) txt.textContent = "TikTok connected";
              if (btn) { btn.innerHTML = "Reconnect"; btn.disabled = false; }
            } else {
              if (dot) dot.className = "tt-status off" + (st && st.busy ? " busy" : "");
              if (txt) txt.textContent = st && st.busy
                ? "Log in to TikTok in the opened window…"
                : (st && st.error ? ("Login failed: " + st.error) : "TikTok not connected");
              if (btn) { btn.disabled = !!(st && st.busy); }
            }
          }
          function ttPoll() {
            fetch("/tiktok-status").then(function (r) { return r.json(); }).then(function (st) {
              ttRender(st);
              if (st && st.busy) { ttPollTimer = setTimeout(ttPoll, 2000); }
              else if (ttPollTimer) { clearTimeout(ttPollTimer); ttPollTimer = null; }
            }).catch(function () {});
          }
          function connectTikTok() {
            if (window.TIKTOK_AVAIL === false) {
              setStatus("Browser engine missing — run: pip install playwright && playwright install chromium");
              return;
            }
            var btn = document.getElementById("tt-login-btn");
            if (btn) btn.disabled = true;
            setStatus("Opening a browser window — log in to TikTok in it (one time).");
            fetch("/tiktok-login", { method: "POST" }).then(function (r) { return r.json(); })
              .then(function () { ttPoll(); })
              .catch(function () { if (btn) btn.disabled = false; });
          }
          var xtPollTimer = null;
          function xtRender(st) {
            var dot = document.getElementById("xt-status-dot");
            var txt = document.getElementById("xt-status-text");
            var btn = document.getElementById("xt-login-btn");
            if (st && st.ready) {
              window.SCRAPE_READY = true;
              if (dot) dot.className = "tt-status on";
              if (txt) txt.textContent = "X connected";
              if (btn) { btn.innerHTML = "Reconnect"; btn.disabled = false; }
            } else {
              if (dot) dot.className = "tt-status off" + (st && st.busy ? " busy" : "");
              if (txt) txt.textContent = st && st.busy
                ? "Log in to X in the opened window…"
                : (st && st.error ? ("Login failed: " + st.error) : "X not connected (optional)");
              if (btn) { btn.disabled = !!(st && st.busy); }
            }
          }
          function xtPoll() {
            fetch("/twitter-status").then(function (r) { return r.json(); }).then(function (st) {
              xtRender(st);
              if (st && st.busy) { xtPollTimer = setTimeout(xtPoll, 2000); }
              else if (xtPollTimer) { clearTimeout(xtPollTimer); xtPollTimer = null; }
            }).catch(function () {});
          }
          function connectTwitter() {
            if (window.TIKTOK_AVAIL === false) {
              setStatus("Browser engine missing — run: pip install playwright && playwright install chromium");
              return;
            }
            var btn = document.getElementById("xt-login-btn");
            if (btn) btn.disabled = true;
            setStatus("Opening a browser window — log in to X/Twitter in it (one time).");
            fetch("/twitter-login", { method: "POST" }).then(function (r) { return r.json(); })
              .then(function () { xtPoll(); })
              .catch(function () { if (btn) btn.disabled = false; });
          }
          // onclick="" attributes run in GLOBAL scope, so these closures must be exposed on window.
          window.connectTikTok = connectTikTok;
          window.ttPoll = ttPoll;
          window.ttRender = ttRender;
          window.connectTwitter = connectTwitter;
          window.xtPoll = xtPoll;
          window.xtRender = xtRender;
          // ---- Background music picker + preview ----
          window.bgmInit = function () {
            var sel = document.getElementById("bgm-select");
            if (!sel) return;
            fetch("/music-list").then(function (r) { return r.json(); }).then(function (d) {
              (d && d.tracks || []).forEach(function (t) {
                var o = document.createElement("option");
                o.value = t.file; o.textContent = t.name; o.setAttribute("data-url", t.url);
                sel.appendChild(o);
              });
              if (window.BGM_SAVED && window.BGM_SAVED !== "none") { sel.value = window.BGM_SAVED; }
              window.bgmOnChange();
            }).catch(function () {});
          };
          window.bgmOnChange = function () {
            var sel = document.getElementById("bgm-select"), play = document.getElementById("bgm-play"),
                au = document.getElementById("bgm-audio");
            if (!sel) return;
            var opt = sel.options[sel.selectedIndex];
            var url = opt ? opt.getAttribute("data-url") : null;
            if (play) play.disabled = !url;
            if (au) { au.pause(); }
            if (play) play.innerHTML = "\\u25B6";
          };
          window.bgmPreview = function () {
            var sel = document.getElementById("bgm-select"), play = document.getElementById("bgm-play"),
                au = document.getElementById("bgm-audio");
            var opt = sel.options[sel.selectedIndex];
            var url = opt ? opt.getAttribute("data-url") : null;
            if (!url || !au) return;
            if (au.paused) {
              au.src = url; au.play().catch(function () {}); if (play) play.innerHTML = "\\u275A\\u275A";
              au.onended = function () { if (play) play.innerHTML = "\\u25B6"; };
            } else { au.pause(); if (play) play.innerHTML = "\\u25B6"; }
          };
          // ---- Voice preview: play a ~10s sample of the selected Gemini voice ----
          window.voicePreview = function () {
            var sel = document.getElementById("tts-voice-select"), btn = document.getElementById("voice-play"),
                au = document.getElementById("voice-audio");
            if (!sel || !au || !btn) return;
            var voice = sel.value;
            if (!au.paused && au.dataset.voice === voice) { au.pause(); btn.innerHTML = "\\u25B6"; return; }
            btn.classList.add("loading"); btn.innerHTML = "\\u2026"; btn.disabled = true;
            au.dataset.voice = voice;
            au.src = "/voice-preview?voice=" + encodeURIComponent(voice);
            au.onended = function () { btn.innerHTML = "\\u25B6"; };
            var p = au.play();
            (p && p.then ? p : Promise.resolve()).then(function () {
              btn.classList.remove("loading"); btn.disabled = false; btn.innerHTML = "\\u275A\\u275A";
            }).catch(function () {
              btn.classList.remove("loading"); btn.disabled = false; btn.innerHTML = "\\u25B6";
            });
          };
          var _vsel = document.getElementById("tts-voice-select");
          if (_vsel) _vsel.addEventListener("change", function () {
            var au = document.getElementById("voice-audio"), b = document.getElementById("voice-play");
            if (au) au.pause(); if (b) b.innerHTML = "\\u25B6";
          });
          function clearProjectMedia() {
            if (mediaBox) mediaBox.innerHTML = "";
            var mediaHint = document.getElementById("project-media-hint");
            if (mediaHint) mediaHint.style.display = 'block';
            if (loadedSource) loadedSource.value = "";
          }
          function htmlEscape(text) {
            return String(text == null ? "" : text)
              .replace(/&/g, "&amp;")
              .replace(/</g, "&lt;")
              .replace(/>/g, "&gt;")
              .replace(/"/g, "&quot;")
              .replace(/'/g, "&#39;");
          }
          function mediaTile(item) {
            var name = htmlEscape(item.name);
            var kind = htmlEscape(item.kind);
            var url = htmlEscape(item.url);
            var path = htmlEscape(item.path);
            var preview = item.type === "video"
              ? '<video data-lazy-src="' + url + '" muted preload="none" playsinline></video>'
              : item.type === "image"
                ? '<img loading="lazy" src="' + url + '" alt="' + name + '">'
                : '<div class="media-audio-tile">' + htmlEscape(item.type || "file") + '</div>';
            return [
              '<article class="media-tile">',
              '<span class="media-kind">' + kind + '</span>',
              '<button class="media-del" type="button" data-del-path="' + path + '" data-del-name="' + name + '" title="Delete this file" aria-label="Delete this file">&times;</button>',
              '<button class="preview-button" type="button" data-preview-src="' + url + '" data-preview-title="' + name + '">',
              preview + '</button>',
              '<small>' + name + '</small>',
              '</article>'
            ].join('');
          }
          function renderProjectMedia(items) {
            var groups = {};
            (items || []).forEach(function (item) {
              var key = item.kind || "media";
              if (!groups[key]) groups[key] = [];
              groups[key].push(item);
            });
            var order = ["web", "wikimedia", "seedance", "gpt source", "speaker", "render", "review", "local", "web rejected", "web replaced", "media"];
            var keys = order.filter(function (key) { return groups[key]; }).concat(Object.keys(groups).filter(function (key) { return order.indexOf(key) === -1; }).sort());
            if (!keys.length) return '<div class="hint">No media files found in this project.</div>';
            var tabs = keys.map(function (key, index) {
              var id = key.toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "") || ("tab_" + index);
              return '<button class="media-tab' + (index ? '' : ' active') + '" type="button" data-media-tab="' + id + '">' + htmlEscape(key) + ' <span>' + groups[key].length + '</span></button>';
            }).join('');
            var panels = keys.map(function (key, index) {
              var id = key.toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "") || ("tab_" + index);
              return '<div class="media-tab-panel" data-media-panel="' + id + '"' + (index ? ' hidden' : '') + '><div class="media-tiny-grid">' + groups[key].map(mediaTile).join('') + '</div></div>';
            }).join('');
            return '<div class="media-tabs">' + tabs + '</div>' + panels;
          }
          window.mediaLoadingHTML = function (label) {
            return '<div class="media-loading" role="status" aria-live="polite">'
              + '<div class="ml-spinner"></div>'
              + '<div class="ml-bar"><i></i></div>'
              + '<div class="ml-text">' + htmlEscape(label || "Loading media") + '</div>'
              + '</div>';
          };
          function loadProjectMedia(slug) {
            if (!mediaBox) return;
            if (!slug) {
              clearProjectMedia();
              return;
            }
            mediaBox.dataset.slug = slug;
            var mediaHint = document.getElementById("project-media-hint");
            if (mediaHint) mediaHint.style.display = 'none';
            mediaBox.innerHTML = window.mediaLoadingHTML("Loading media previews");
            fetch("/project-media?slug=" + encodeURIComponent(slug))
              .then(function (response) {
                if (!response.ok) throw new Error("Could not load media previews.");
                return response.json();
              })
              .then(function (data) {
                if (!data || !data.media || !data.media.length) {
                  mediaBox.innerHTML = '<div class="hint">No media files found in this project.</div>';
                  return;
                }
                mediaBox.innerHTML = renderProjectMedia(data.media);
                setupMediaTabs(mediaBox);
                window.LazyVideo.observe(mediaBox);
              })
              .catch(function (err) {
                mediaBox.innerHTML = '<div class="hint">' + (err.message || "Could not load media previews.") + '</div>';
              });
          }
          if (mediaBox) {
            mediaBox.addEventListener("click", function (ev) {
              var del = ev.target.closest(".media-del");
              if (!del) return;
              ev.preventDefault();
              ev.stopPropagation();
              var path = del.getAttribute("data-del-path");
              var name = del.getAttribute("data-del-name") || "this file";
              if (!path) return;
              if (!window.confirm('Delete "' + name + '" permanently? This cannot be undone.')) return;
              del.disabled = true;
              fetch("/delete-media", {
                method: "POST",
                headers: { "Content-Type": "application/x-www-form-urlencoded" },
                body: "path=" + encodeURIComponent(path)
              }).then(function (r) {
                if (!r.ok) throw new Error("Delete failed.");
                var tile = del.closest(".media-tile");
                if (tile) tile.remove();
              }).catch(function () {
                del.disabled = false;
                if (typeof setStatus === "function") setStatus("Could not delete the file.");
                else alert("Could not delete the file.");
              });
            });
          }
        window.loadProject = function (slug) {
            if (!slug) {
              setStatus("Choose a project first.");
              clearProjectMedia();
              return;
            }
            setStatus("Loading project...");
            fetch("/project-preset?slug=" + encodeURIComponent(slug))
              .then(function (response) {
                if (!response.ok) throw new Error("Project could not be loaded.");
                return response.json();
              })
              .then(function (data) {
                if (!data || !data.state) throw new Error("Project has no loadable state.");
                applyFormState(data.state);
                if (loadedSource) loadedSource.value = slug;
                if (typeof window.showLoadedActions === "function") window.showLoadedActions();
                document.getElementById('load-modal').classList.remove('active');
                var storedState = collectFormState();
                try { localStorage.setItem(autosaveKey, JSON.stringify(storedState)); } catch (err) {}
                sendFormState(storedState);
                setStatus("Loaded: " + (data.title || slug));
                loadProjectMedia(slug);
                var modal = document.getElementById('load-modal');
                if (modal) modal.classList.remove('active');
              })
              .catch(function (err) {
                setStatus("Error loading project: " + err.toString() + (err.stack ? " " + err.stack : ""));
              });
          };
          window.newProject = function () {
            var form = document.getElementById("short-form");
            if (form) {
              ["script", "visual_script", "title", "hook_text", "slug"].forEach(function (name) {
                var field = form.querySelector('[name="' + name + '"]');
                if (field) field.value = "";
              });
            }
            if (loadedSource) loadedSource.value = "";
            if (typeof window.hideLoadedActions === "function") window.hideLoadedActions();
            if (typeof window.clearHook === "function") window.clearHook();
            if (typeof clearProjectMedia === "function") clearProjectMedia();
            var state = collectFormState();
            if (state) {
              try { localStorage.setItem(autosaveKey, JSON.stringify(state)); } catch (e) {}
              sendFormState(state);
            }
            if (typeof setStatus === "function") setStatus("New project — fields cleared.");
            var ht = document.getElementById("speaker-image-path"); if (ht) ht.value = "";
            document.querySelectorAll(".speaker-tile").forEach(function (t) { t.classList.remove("selected"); });
          };
          window.selectSpeakerImage = function (el) {
            var hidden = document.getElementById("speaker-image-path");
            if (hidden) hidden.value = el.getAttribute("data-path") || "";
            document.querySelectorAll(".speaker-tile").forEach(function (t) { t.classList.toggle("selected", t === el); });
            var fileInput = document.querySelector('[name="speaker_image_file"]'); if (fileInput) fileInput.value = "";
            var cb = document.querySelector('[name="enable_speaker_hook"]'); if (cb) cb.checked = true;
            if (typeof saveFormStateSoon === "function") saveFormStateSoon();
          };
          window.onSpeakerUpload = function (input) {
            document.querySelectorAll(".speaker-tile").forEach(function (t) { t.classList.remove("selected"); });
            var hidden = document.getElementById("speaker-image-path"); if (hidden) hidden.value = "";
            var cb = document.querySelector('[name="enable_speaker_hook"]');
            if (cb && input.files && input.files.length) { cb.checked = true; if(typeof window.toggleSection==='function') window.toggleSection('speaker-panel', true); }
            var add = document.getElementById('speaker-add-tile');
            if (add && input.files && input.files.length) {
              add.classList.add('selected');
              try { var url = URL.createObjectURL(input.files[0]); add.innerHTML = '<img src="'+url+'" alt="">'; } catch(e){}
            }
          };
          window.toggleSection = function (panelId, on) {
            var p = document.getElementById(panelId); if (p) p.classList.toggle('off', !on);
            if (typeof saveFormStateSoon === 'function') saveFormStateSoon();
          };
          function setupToggleSections() {
            ['visual-panel','speaker-panel'].forEach(function (id) {
              var p = document.getElementById(id); if (!p) return;
              var cb = p.querySelector('.panel-head input[type="checkbox"]');
              if (cb) p.classList.toggle('off', !cb.checked);
            });
            // drag a file onto the "+" speaker tile
            var add = document.getElementById('speaker-add-tile');
            var file = document.getElementById('speaker-file');
            if (add && file) {
              ['dragenter','dragover'].forEach(function(ev){ add.addEventListener(ev, function(e){ e.preventDefault(); add.classList.add('dragover'); }); });
              ['dragleave','dragend'].forEach(function(ev){ add.addEventListener(ev, function(){ add.classList.remove('dragover'); }); });
              add.addEventListener('drop', function(e){
                e.preventDefault(); add.classList.remove('dragover');
                if (e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files.length) {
                  try { file.files = e.dataTransfer.files; } catch(err) {}
                  window.onSpeakerUpload(file);
                }
              });
            }
          }
          function syncClipSource() {
            var hid = document.getElementById("clip-source"); if (!hid) return;
            var plat = document.querySelector('[name="scrape_platforms"]');
            if (plat) plat.value = "tiktok,x";
            var scrape = hid.value === "scrape";
            var seg = document.getElementById("clip-seg");
            if (seg) {
              seg.setAttribute("data-src", scrape ? "scrape" : "generate");
              Array.prototype.forEach.call(seg.querySelectorAll(".seg-opt"), function (o) {
                o.classList.toggle("on", o.getAttribute("data-src") === (scrape ? "scrape" : "generate"));
              });
            }
            // AI model pickers only matter for Generate; hidden entirely in Scrape.
            var ai = document.getElementById("ai-models");
            if (ai) ai.style.display = scrape ? "none" : "grid";
            var box = document.getElementById("scrape-settings");
            if (box) box.style.display = scrape ? "block" : "none";
            // the script-relevancy slider lives in the Text script panel but only matters for scraping.
            var relBlock = document.getElementById("script-relevancy-block");
            if (relBlock) relBlock.style.display = scrape ? "block" : "none";
            var rv = document.getElementById("relv-val"); var rng = document.getElementById("script-relevancy");
            if (rv && rng) rv.textContent = rng.value + "%";
            // AI-image outputs don't apply when scraping — force them OFF + gray out.
            ["out_web_images", "out_wikimedia", "out_gpt_images"].forEach(function (n) {
              var f = document.querySelector('[name="' + n + '"]'); if (!f) return;
              var lbl = f.closest(".otoggle");
              if (scrape) {
                if (!f.disabled) f.dataset.prevChecked = f.checked ? "1" : "0";
                f.checked = false; f.disabled = true;
                if (lbl) lbl.classList.add("ctrl-disabled");
              } else {
                if (f.dataset.prevChecked !== undefined) { f.checked = (f.dataset.prevChecked === "1"); delete f.dataset.prevChecked; }
                f.disabled = false;
                if (lbl) lbl.classList.remove("ctrl-disabled");
              }
            });
            // Video clips are the scraped footage itself — force ON (and lock) when scraping.
            var vc = document.querySelector('[name="out_video_clips"]');
            if (vc) {
              if (scrape) { if (!vc.disabled) vc.dataset.prevChecked = vc.checked ? "1" : "0"; vc.checked = true; vc.disabled = true; }
              else { if (vc.dataset.prevChecked !== undefined) { vc.checked = (vc.dataset.prevChecked === "1"); delete vc.dataset.prevChecked; } vc.disabled = false; }
            }
          }
          window.syncClipSource = syncClipSource;
          window.setClipSource = function (src) {
            var hid = document.getElementById("clip-source"); if (!hid) return;
            hid.value = (src === "scrape") ? "scrape" : "generate";
            syncClipSource();
            var state = collectFormState();
            if (state) { try { localStorage.setItem(autosaveKey, JSON.stringify(state)); } catch (e) {} sendFormState(state); }
          };
          window.onClipToggle = function (cb) { window.setClipSource(cb.checked ? "scrape" : "generate"); };
          window.setScrapeEngine = function (eng) {
            eng = (eng === "v1") ? "v1" : "v2";
            var hid = document.getElementById("scraping-engine"); if (hid) hid.value = eng;
            var seg = document.getElementById("engine-seg"); if (seg) seg.setAttribute("data-eng", eng);
            try { if (typeof playClick === "function") playClick(); } catch (e) {}
            var state = collectFormState();
            if (state) { try { localStorage.setItem(autosaveKey, JSON.stringify(state)); } catch (e) {} sendFormState(state); }
          };
          var CUSTOM_PRESET_FIELDS =["speaker_name","tts_voice","tts_model","video_model","image_model","reasoning_model","reasoning_mode","speaker_image_path","visual_script","use_visual_direction","enable_speaker_hook","out_web_images","out_wikimedia","out_gpt_images","out_video_clips","out_sfx","out_transition_sfx","out_background_music","out_captions","halt_after_speech","clip_source","scrape_platforms","script_relevancy","scrape_sort","scrape_terms","background_music_choice","scraping_engine","sfx_amount"];
          var activePresetName = null;
          function collectPresetData() {
            var form = document.getElementById("short-form"); if (!form) return {};
            var data = {};
            CUSTOM_PRESET_FIELDS.forEach(function (name) {
              var f = form.querySelector('[name="' + name + '"]');
              if (f) data[name] = (f.type === "checkbox") ? f.checked : f.value;
            });
            return data;
          }
          function applyPresetData(data, label) {
            if (!data || !Object.keys(data).length) return;
            applyFormState(data);
            var hp = document.getElementById("speaker-image-path");
            if (hp && data.speaker_image_path) { document.querySelectorAll(".speaker-tile").forEach(function (t) { t.classList.toggle("selected", t.getAttribute("data-path") === data.speaker_image_path); }); }
            if (typeof setupToggleSections === "function") setupToggleSections();
            if (typeof syncClipSource === "function") syncClipSource();
            Array.prototype.forEach.call(document.querySelectorAll(".tier-btn"), function (b) { b.classList.remove("tier-active"); });
            var active = document.querySelector('.tier-btn[onclick*="applyCustomPreset"]'); if (active) active.classList.add("tier-active");
            var state = collectFormState();
            if (state) { try { localStorage.setItem(autosaveKey, JSON.stringify(state)); } catch (e) {} sendFormState(state); }
            if (typeof setStatus === "function") setStatus((label || "Preset") + " loaded.");
          }
          function presetSavePost(name, data) {
            return fetch("/save-preset", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: name, data: data }) })
              .then(function (r) { return r.json(); });
          }
          function renderPresetPopup(payload) {
            var builtin = (payload && payload.builtin) || {};
            var user = (payload && payload.user) || {};
            function row(name, data, isBuiltin) {
              var safe = name.replace(/"/g, "&quot;");
              var del = isBuiltin ? "" : '<button type="button" class="pp-del" title="Delete preset" data-name="' + safe + '">&times;</button>';
              return '<div class="pp-row" data-name="' + safe + '" data-builtin="' + (isBuiltin ? "1" : "0") + '">' +
                     '<button type="button" class="pp-load" data-name="' + safe + '">' + name + '</button>' + del + '</div>';
            }
            var html = '';
            html += '<div class="pp-group-label">Built-in</div>';
            var bk = Object.keys(builtin);
            html += bk.length ? bk.map(function (n) { return row(n, builtin[n], true); }).join("") : '<div class="pp-empty">none</div>';
            html += '<div class="pp-group-label">Your presets</div>';
            var uk = Object.keys(user);
            html += uk.length ? uk.map(function (n) { return row(n, user[n], false); }).join("") : '<div class="pp-empty">No saved presets yet.</div>';
            html += '<div class="pp-save">' +
                    '<div class="pp-save-label">Save current settings</div>' +
                    '<div class="pp-save-row"><input type="text" id="pp-name" placeholder="Preset name…" value="' + (activePresetName ? activePresetName.replace(/"/g, "&quot;") : "") + '">' +
                    '<button type="button" class="button" id="pp-save-as">Save as</button></div>' +
                    (activePresetName ? '<button type="button" class="pp-overwrite" id="pp-overwrite">Overwrite “' + activePresetName + '”</button>' : '') +
                    '</div>';
            var body = document.getElementById("preset-popup-body");
            if (body) { body.innerHTML = html; wirePresetPopup(builtin, user); }
          }
          function wirePresetPopup(builtin, user) {
            var body = document.getElementById("preset-popup-body"); if (!body) return;
            Array.prototype.forEach.call(body.querySelectorAll(".pp-load"), function (b) {
              b.addEventListener("click", function () {
                var n = b.getAttribute("data-name");
                var src = (user && user[n]) ? user[n] : (builtin && builtin[n]) ? builtin[n] : null;
                if (src) { activePresetName = (user && user[n]) ? n : null; applyPresetData(src, n); closePresetPopup(); }
              });
            });
            Array.prototype.forEach.call(body.querySelectorAll(".pp-del"), function (b) {
              b.addEventListener("click", function (ev) {
                ev.stopPropagation();
                var n = b.getAttribute("data-name");
                if (!window.confirm("Delete preset “" + n + "”?")) return;
                fetch("/delete-preset", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: n }) })
                  .then(function (r) { return r.json(); })
                  .then(function () { if (activePresetName === n) activePresetName = null; loadPresetList(); });
              });
            });
            var saveAs = document.getElementById("pp-save-as");
            if (saveAs) saveAs.addEventListener("click", function () {
              var inp = document.getElementById("pp-name");
              var name = inp ? inp.value.trim() : "";
              if (!name) { if (inp) inp.focus(); return; }
              presetSavePost(name, collectPresetData()).then(function (d) {
                if (d && d.ok) { activePresetName = name; if (typeof setStatus === "function") setStatus("Preset “" + name + "” saved."); loadPresetList(); }
                else if (typeof setStatus === "function") setStatus("Could not save preset (name may be reserved).");
              });
            });
            var ow = document.getElementById("pp-overwrite");
            if (ow) ow.addEventListener("click", function () {
              if (!activePresetName) return;
              presetSavePost(activePresetName, collectPresetData()).then(function (d) {
                if (d && d.ok) { if (typeof setStatus === "function") setStatus("Preset “" + activePresetName + "” overwritten."); loadPresetList(); }
              });
            });
          }
          function loadPresetList() {
            return fetch("/presets").then(function (r) { return r.json(); }).then(function (d) { renderPresetPopup(d); return d; });
          }
          function openPresetPopup() {
            var m = document.getElementById("preset-popup"); if (!m) return;
            m.classList.add("active");
            loadPresetList().catch(function () { if (typeof setStatus === "function") setStatus("Could not load presets."); });
          }
          function closePresetPopup() {
            var m = document.getElementById("preset-popup"); if (m) m.classList.remove("active");
          }
          window.closePresetPopup = closePresetPopup;
          window.applyCustomPreset = openPresetPopup;
          window.saveCustomPreset = openPresetPopup;
          clearProjectMedia();
        }

        var _uiCtx = null;
        function uiCtx() {
          try {
            var AudioCtx = window.AudioContext || window.webkitAudioContext;
            if (!AudioCtx) return null;
            if (!_uiCtx) _uiCtx = new AudioCtx();
            if (_uiCtx.state === "suspended") _uiCtx.resume();
            return _uiCtx;
          } catch (e) { return null; }
        }
        function playClick() {
          var ctx = uiCtx(); if (!ctx) return;
          try {
            var now = ctx.currentTime;
            var osc = ctx.createOscillator(), g = ctx.createGain();
            osc.type = "triangle";
            osc.frequency.setValueAtTime(540, now);
            osc.frequency.exponentialRampToValueAtTime(320, now + 0.05);
            g.gain.setValueAtTime(0.0001, now);
            g.gain.exponentialRampToValueAtTime(0.05, now + 0.005);
            g.gain.exponentialRampToValueAtTime(0.0001, now + 0.07);
            osc.connect(g); g.connect(ctx.destination);
            osc.start(now); osc.stop(now + 0.09);
          } catch (e) {}
        }
        function playKeyTick() {
          var ctx = uiCtx(); if (!ctx) return;
          try {
            var now = ctx.currentTime, dur = 0.028;
            var buf = ctx.createBuffer(1, Math.ceil(ctx.sampleRate * dur), ctx.sampleRate);
            var data = buf.getChannelData(0);
            for (var i = 0; i < data.length; i++) data[i] = (Math.random() * 2 - 1) * Math.pow(1 - i / data.length, 2);
            var src = ctx.createBufferSource(); src.buffer = buf;
            var hp = ctx.createBiquadFilter(); hp.type = "highpass"; hp.frequency.value = 1700;
            var g = ctx.createGain();
            g.gain.setValueAtTime(0.07, now);
            g.gain.exponentialRampToValueAtTime(0.0001, now + dur);
            src.connect(hp); hp.connect(g); g.connect(ctx.destination);
            src.start(now); src.stop(now + dur);
          } catch (e) {}
        }
        window.playKeyTick = playKeyTick;
        function setupUiSounds() {
          var sel = "button, a.button, .button, .tier-btn, .action-btn, .speaker-tile, .speaker-pick, .media-tab, .back-arrow, .preview-button, .asset-figure, [role='button']";
          document.addEventListener("pointerdown", function (ev) {
            // make sure the audio context is unlocked on the very first gesture so
            // even the first button click is audible.
            uiCtx();
            var el = ev.target.closest(sel);
            if (el && !el.disabled) playClick();
          }, true);
        }
        function playDoneSoundOnce() {
          var root = document.getElementById("job-root");
          if (!root || root.getAttribute("data-status") !== "done") return;
          var jobId = root.getAttribute("data-job-id") || "unknown";
          var key = "autonomous-shorts-agent-done-sound-" + jobId;
          try {
            if (localStorage.getItem(key)) return;
            localStorage.setItem(key, "1");
          } catch (err) {}
          try {
            var AudioCtx = window.AudioContext || window.webkitAudioContext;
            if (!AudioCtx) return;
            var ctx = new AudioCtx();
            var now = ctx.currentTime + 0.02;
            var master = ctx.createGain();
            master.gain.setValueAtTime(0.0001, now);
            master.gain.exponentialRampToValueAtTime(0.16, now + 0.02);
            master.gain.setValueAtTime(0.16, now + 0.42);
            master.gain.exponentialRampToValueAtTime(0.0001, now + 0.66);
            master.connect(ctx.destination);
            [523.25, 659.25, 783.99, 1046.5].forEach(function (freq, index) {
              var osc = ctx.createOscillator();
              var gain = ctx.createGain();
              osc.type = "sine";
              osc.frequency.setValueAtTime(freq, now + index * 0.07);
              gain.gain.setValueAtTime(0.0001, now + index * 0.07);
              gain.gain.exponentialRampToValueAtTime(1.0, now + index * 0.07 + 0.018);
              gain.gain.exponentialRampToValueAtTime(0.0001, now + index * 0.07 + 0.30);
              osc.connect(gain);
              gain.connect(master);
              osc.start(now + index * 0.07);
              osc.stop(now + index * 0.07 + 0.34);
            });
            setTimeout(function () { try { ctx.close(); } catch (err) {} }, 700);
          } catch (err) {}
        }

        function checkedMediaValues() {
          var values = {};
          Array.prototype.forEach.call(document.querySelectorAll('input[name="media_path"]:checked'), function (field) {
            values[field.value] = true;
          });
          return values;
        }

        function restoreCheckedMediaValues(values) {
          Array.prototype.forEach.call(document.querySelectorAll('input[name="media_path"]'), function (field) {
            if (values[field.value]) field.checked = true;
          });
        }

        function activeMediaTabId(container) {
          var active = container ? container.querySelector(".media-tab.active") : null;
          return active ? active.getAttribute("data-media-tab") : "";
        }

        function activateMediaTab(container, tabId) {
          if (!container) return;
          var tabs = Array.prototype.slice.call(container.querySelectorAll(".media-tab"));
          var panels = Array.prototype.slice.call(container.querySelectorAll(".media-tab-panel"));
          if (!tabs.length) return;
          var target = tabId && container.querySelector('.media-tab[data-media-tab="' + tabId + '"]') ? tabId : tabs[0].getAttribute("data-media-tab");
          tabs.forEach(function (tab) {
            tab.classList.toggle("active", tab.getAttribute("data-media-tab") === target);
          });
          panels.forEach(function (panel) {
            panel.hidden = panel.getAttribute("data-media-panel") !== target;
          });
        }

        function setupMediaTabs(scope) {
          var root = scope || document;
          Array.prototype.forEach.call(root.querySelectorAll(".media-tabs"), function (tabs) {
            var container = tabs.closest(".media-sidebar, .loaded-media-panel") || root;
            if (tabs.getAttribute("data-tabs-ready")) return;
            tabs.setAttribute("data-tabs-ready", "1");
            tabs.addEventListener("click", function (event) {
              var tab = event.target.closest(".media-tab");
              if (!tab) return;
              activateMediaTab(container, tab.getAttribute("data-media-tab"));
            });
            activateMediaTab(container, activeMediaTabId(container));
          });
        }

        function setupReplacementForms(scope) {
          var root = scope || document;
          Array.prototype.forEach.call(root.querySelectorAll('form[data-replace-media-form]'), function (form) {
            if (form.getAttribute("data-replace-ready")) return;
            form.setAttribute("data-replace-ready", "1");
            form.addEventListener("submit", function (event) {
              event.preventDefault();
              var checked = Array.prototype.slice.call(form.querySelectorAll('input[name="media_path"]:checked'));
              if (!checked.length) return;
              checked.forEach(function (field) {
                var tile = field.closest(".media-tile");
                if (tile) tile.remove();
              });
              fetch(form.action, {
                method: "POST",
                headers: { "Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded" },
                body: checked.map(function (field) {
                  return "media_path=" + encodeURIComponent(field.value);
                }).join("&")
              }).catch(function () {});
            });
          });
        }

        function updateJobFromStatus(data) {
          if (!data || !data.exists) return;
          var root = document.getElementById("job-root");
          if (!root) return;
          root.setAttribute("data-status", data.status || "");
          // surface the speech-approval panel as soon as the run pauses for it
          if (data.status === "awaiting_approval" && !document.getElementById("speech-approval")) { location.reload(); return; }
          // when the render finishes, reload so the server shows the clean "ready" view
          // (just the final video + Open timeline editor) instead of the live detail view
          if (data.status === "done" && !document.getElementById("done-stage")) { location.reload(); return; }
          var statusLabel = document.getElementById("job-status-label");
          if (statusLabel) {
            statusLabel.textContent = data.status || "";
            statusLabel.className = "status " + (data.klass || "");
          }
          var progress = document.getElementById("job-progress-wrap");
          if (progress && data.progress_html) progress.outerHTML = data.progress_html;
          var log = document.getElementById("job-log");
          if (log && typeof data.log_text === "string"
              && log.getAttribute("data-last-log") !== data.log_text) {
            // only rewrite the <pre> when the text actually changed - re-setting a big log
            // every poll forced a full reflow and froze the page
            log.textContent = data.log_text;
            log.setAttribute("data-last-log", data.log_text);
          }
          var outputs = document.getElementById("job-outputs");
          if (outputs && typeof data.outputs_html === "string") outputs.innerHTML = data.outputs_html;
          var errorBox = document.getElementById("job-error");
          if (errorBox && typeof data.error_html === "string") errorBox.innerHTML = data.error_html;
          var media = document.getElementById("media-sidebar-container");
          if (media && typeof data.media_html === "string") {
            var checked = checkedMediaValues();
            if (media.getAttribute("data-last-html") !== data.media_html) {
              var sidebar = media.querySelector(".media-sidebar");
              var scrollTop = sidebar ? sidebar.scrollTop : 0;
              var tabId = activeMediaTabId(media);
              media.innerHTML = data.media_html;
              media.setAttribute("data-last-html", data.media_html);
              restoreCheckedMediaValues(checked);
              setupMediaTabs(media);
              setupReplacementForms(media);
              activateMediaTab(media, tabId);
              window.LazyVideo.observe(media);
              sidebar = media.querySelector(".media-sidebar");
              if (sidebar) sidebar.scrollTop = scrollTop;
            }
          }
          playDoneSoundOnce();
          stickLogToBottom();
        }

        function setupJobPolling() {
          var root = document.getElementById("job-root");
          if (!root) return;
          var jobId = root.getAttribute("data-job-id");
          if (!jobId) return;
          function poll() {
            var status = root.getAttribute("data-status");
            if (["running", "cancelling"].indexOf(status) === -1) return;
            fetch("/job-status?id=" + encodeURIComponent(jobId), { cache: "no-store" })
              .then(function (response) { if (!response.ok) throw new Error("status"); return response.json(); })
              .then(updateJobFromStatus)
              .catch(function () {});
          }
          setInterval(poll, 5000);
          poll();
        }

        function ensureLightbox() {
          var box = document.getElementById("lightbox");
          if (box) return box;
          box = document.createElement("div");
          box.id = "lightbox";
          box.className = "lightbox";
          box.innerHTML = '<div class="lightbox-inner"><div class="lightbox-bar"><div class="lightbox-title"></div><button class="lightbox-close" type="button">Close</button></div><img alt=""></div>';
          document.body.appendChild(box);
          box.addEventListener("click", function (event) {
            if (event.target === box || event.target.classList.contains("lightbox-close")) {
              box.classList.remove("open");
            }
          });
          document.addEventListener("keydown", function (event) {
            if (event.key === "Escape") box.classList.remove("open");
          });
          return box;
        }

        document.addEventListener("click", function (event) {
          var toggle = event.target.closest("[data-toggle-log]");
          if (toggle) {
            var logTarget = document.getElementById(toggle.getAttribute("data-toggle-log"));
            if (logTarget) {
              logTarget.hidden = !logTarget.hidden;
              toggle.textContent = logTarget.hidden ? "Show logs" : "Hide logs";
            }
            return;
          }
          var remove = event.target.closest(".media-remove");
          if (remove) {
            event.preventDefault();
            var removePath = remove.getAttribute("data-remove-path");
            var removeJob = remove.getAttribute("data-remove-job");
            if (!removePath || !removeJob) return;
            if (!window.confirm("Remove this clip from the accepted/assigned pool for this run?")) return;
            remove.disabled = true;
            fetch("/exclude-run-media", {
              method: "POST",
              headers: {"Content-Type": "application/x-www-form-urlencoded"},
              body: "id=" + encodeURIComponent(removeJob) + "&path=" + encodeURIComponent(removePath)
            }).then(function (r) { return r.json(); }).then(function (d) {
              if (!d || !d.ok) throw new Error((d && d.error) || "Remove failed");
              var tile = remove.closest(".media-tile");
              if (tile) tile.remove();
            }).catch(function (err) {
              remove.disabled = false;
              alert(err.message || "Could not remove this clip.");
            });
            return;
          }
          var acc = event.target.closest(".media-accept");
          if (acc) {
            event.preventDefault();
            var acceptPath = acc.getAttribute("data-accept-path");
            fetch("/accept-media", {
              method: "POST",
              headers: { "Content-Type": "application/x-www-form-urlencoded" },
              body: "path=" + encodeURIComponent(acceptPath)
            }).then(function (r) { return r.json(); }).then(function (d) {
              if (d && d.ok) {
                var tile = acc.closest(".media-tile");
                if (tile) {
                  tile.classList.remove("declined-tile");
                  var k = tile.querySelector(".media-kind");
                  if (k) k.textContent = "clip";
                }
                acc.remove();
              }
            }).catch(function () {});
            return;
          }
          var trigger = event.target.closest("[data-preview-src]");
          if (!trigger) return;
          var box = ensureLightbox();
          box.querySelector("img").src = trigger.getAttribute("data-preview-src");
          box.querySelector("img").alt = trigger.getAttribute("data-preview-title") || "Preview";
          box.querySelector(".lightbox-title").textContent = trigger.getAttribute("data-preview-title") || "Preview";
          box.classList.add("open");
        });

        // ===== LAZY VIDEO: a media page can hold 100+ clips. Loading every <video> at once
        // exhausts the browser's media-element/decoder budget and CRASHES the renderer tab
        // ("Aw Snap"/Edge crash). Only videos scrolled into view get a src; those scrolled far
        // out release it again, capping live decoders to what's on screen. =====
        window.LazyVideo = (function () {
          var io = null;
          function load(v) {
            var s = v.getAttribute("data-lazy-src");
            if (s && v.getAttribute("src") !== s) { v.setAttribute("src", s); }
          }
          function unload(v) {
            if (!v.getAttribute("src")) return;
            try { v.pause(); } catch (e) {}
            v.removeAttribute("src");
            try { v.load(); } catch (e) {}   // free the decoder + network handle
          }
          function ensureIO() {
            if (io || !("IntersectionObserver" in window)) return io;
            io = new IntersectionObserver(function (entries) {
              entries.forEach(function (en) {
                if (en.isIntersecting) {
                  load(en.target);
                  // Timeline library thumbnails are small + few: once loaded, KEEP them (stop
                  // observing) so scrolling the library never blanks a tile. Larger run-page
                  // media grids still unload off-screen to cap live decoders.
                  if (en.target.closest(".tl-library")) { try { io.unobserve(en.target); } catch (e) {} }
                } else if (!en.target.closest(".tl-library")) {
                  unload(en.target);
                }
              });
            }, { root: null, rootMargin: "800px 0px", threshold: 0.01 });
            return io;
          }
          return {
            observe: function (root) {
              var vids = (root || document).querySelectorAll("video[data-lazy-src]");
              var o = ensureIO();
              if (!o) { Array.prototype.forEach.call(vids, load); return; }  // no IO -> load all
              Array.prototype.forEach.call(vids, function (v) { o.observe(v); });
            },
            ensure: load   // force-load one clip (e.g. on hover) before play
          };
        })();

        document.addEventListener("mouseover", function (event) {
          var tile = event.target.closest(".media-tile");
          if (!tile) return;
          var vid = tile.querySelector("video");
          if (vid) {
            window.LazyVideo.ensure(vid);   // make sure it has a src before playing
            if (vid.paused) {
              vid.muted = true;
              var pr = vid.play();
              if (pr && pr.catch) pr.catch(function () {});
            }
          }
        });
        document.addEventListener("mouseout", function (event) {
          var tile = event.target.closest(".media-tile");
          if (!tile) return;
          if (event.relatedTarget && tile.contains(event.relatedTarget)) return;
          var vid = tile.querySelector("video");
          if (vid && !vid.paused) {
            vid.pause();
            try { vid.currentTime = 0; } catch (e) {}
          }
        });

        window.addEventListener("load", function () {
          try { window.resizeTo(1920, 1080); window.moveTo(0, 0); } catch (e) {}
          stickLogToBottom();
          setupFormAutosave();
          setupProjectLoader();
          if (typeof window.bgmInit === "function") window.bgmInit();
          setupMediaTabs(document);
          setupReplacementForms(document);
          setupUiSounds();
          playDoneSoundOnce();
          setupJobPolling();
          window.LazyVideo.observe(document);   // lazy-load the media-grid videos
          if (typeof setupToggleSections === "function") setupToggleSections();
          if (typeof syncClipSource === "function") syncClipSource();
          if (typeof renderTermChips === "function") renderTermChips();
          if (typeof wizInit === "function") wizInit();
          var sform = document.getElementById("short-form");
          if (sform) sform.addEventListener("submit", function (ev) {
            var cs = document.getElementById("clip-source");
            if (!cs || cs.value !== "scrape") return;
            if (window.SCRAPE_READY) return;  // the TikTok-login clip source is ready
            ev.preventDefault();
            var panel = document.getElementById("scrape-settings");
            if (panel) panel.scrollIntoView({ behavior: "smooth", block: "center" });
            if (typeof window.connectTikTok === "function") window.connectTikTok();
          });
        });
        setInterval(stickLogToBottom, 5000);
      })();
    </script>
    """


def page(title, body, refresh=None, body_class=""):
    meta = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
    icons = (
        '<link rel="manifest" href="/manifest.webmanifest">'
        '<link rel="icon" href="/favicon.ico" sizes="any">'
        '<link rel="icon" href="/static/app_icon.png" type="image/png">'
        '<link rel="apple-touch-icon" href="/static/app_icon.png">'
        '<link rel="shortcut icon" href="/favicon.ico">'
        '<meta name="theme-color" content="#090d14">'
        '<meta name="application-name" content="Shortslab">'
        '<link rel="preconnect" href="https://fonts.googleapis.com">'
        '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
        '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?'
        'family=Press+Start+2P&family=Silkscreen:wght@400;700&family=Space+Mono:wght@400;700&display=swap">'
    )
    theme_boot = ('<script>try{if(localStorage.getItem("shortslab-theme")==="dark")'
                  'document.documentElement.classList.add("theme-dark");}catch(e){}</script>')
    return f"""<!doctype html>
    <html lang="en" translate="no"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><meta name="google" content="notranslate">{theme_boot}{meta}{icons}<title>{esc(title)}</title>{app_style()}</head>
    <body{f' class="{esc(body_class)}"' if body_class else ""}><main>{body}</main>{app_script()}{reasoning_selector_script()}</body></html>""".encode("utf-8")


def reasoning_selector_script():
    cfg = json.dumps(reasoning_modes.public_config(), ensure_ascii=False).replace("</", "<\\/")
    return f'''<script>
    (function(){{
      const CFG={cfg};
      function attach(model, index){{
        if(model.dataset.reasoningAttached)return;
        model.dataset.reasoningAttached='1';
        const wrap=document.createElement('label'); wrap.className='reasoning-mode-field';
        wrap.style.cssText='display:block;min-height:58px;margin-top:7px';
        const cap=document.createElement('span'); cap.textContent='Reasoning mode';
        cap.style.cssText='display:block;font-size:11px;font-weight:700;margin-bottom:4px';
        const select=document.createElement('select');
        select.name=model.name==='reasoning_model'?'reasoning_mode':'reasoning_mode_'+index;
        select.setAttribute('aria-label','Reasoning mode');
        const help=document.createElement('small');
        help.textContent='Higher reasoning can improve difficult tasks but may increase response time and cost.';
        help.style.cssText='display:block;opacity:.65;font-size:10px;margin-top:3px';
        wrap.append(cap,select,help); model.insertAdjacentElement('afterend',wrap);
        function sync(){{
          const c=CFG[model.value], previous=select.value;
          if(!c){{wrap.hidden=true;select.innerHTML='';select.disabled=true;return;}}
          wrap.hidden=false;select.disabled=false;select.innerHTML='';
          c.options.forEach(o=>{{const x=document.createElement('option');x.value=o.value;x.textContent=o.label;select.appendChild(x);}});
          const initial=model.dataset.reasoningValue||'';
          select.value=c.options.some(o=>o.value===previous)?previous:(c.options.some(o=>o.value===initial)?initial:c.defaultValue);
          model.dataset.reasoningValue='';
          help.textContent=(c.apiMode==='responses-pro'&&select.value==='pro')?'Pro mode uses deeper reasoning and may be slower and more expensive.':'Higher reasoning can improve difficult tasks but may increase response time and cost.';
        }}
        model.addEventListener('change',sync);select.addEventListener('change',sync);sync();
      }}
      document.querySelectorAll('select[name="reasoning_model"]').forEach(attach);
    }})();</script>'''


def scrape_trainer_runs():
    """Existing reviewable Scrape Trainer runs, newest first."""
    runs_root = ROOT / "scrape_training" / "runs"
    if not runs_root.exists():
        return []
    rows = []
    for directory in runs_root.iterdir():
        if directory.is_dir() and (directory / "trainer.db").exists():
            try:
                edited = directory.stat().st_mtime
            except OSError:
                edited = 0.0
            rows.append({"id": directory.name, "edited": edited})
    rows.sort(key=lambda row: row["edited"], reverse=True)
    return rows


def start_dev_trainer(tool, run_id=""):
    """Start an existing trainer UI on an ephemeral localhost port and return its URL."""
    tool = str(tool or "").strip().lower()
    run_id = str(run_id or "").strip()
    if tool == "sfx":
        key = ("sfx", "")
    elif tool == "scrape":
        allowed = {row["id"] for row in scrape_trainer_runs()}
        if run_id not in allowed:
            raise RuntimeError("Unknown or incomplete Scrape Trainer run.")
        key = ("scrape", run_id)
    else:
        raise RuntimeError("Unknown developer tool.")
    with DEV_TRAINER_LOCK:
        existing = DEV_TRAINER_SERVERS.get(key)
        if existing and existing[1].is_alive():
            return existing[2]
        if tool == "sfx":
            from tools import sfx_trainer_serve
            handler = sfx_trainer_serve._Handler
        else:
            from tools import scrape_trainer_serve
            handler = scrape_trainer_serve._Handler
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        server.daemon_threads = True
        if tool == "scrape":
            server.run_id = run_id
        port = int(server.server_address[1])
        url = (f"http://127.0.0.1:{port}/"
               if tool == "sfx" else
               f"http://127.0.0.1:{port}/?run={urllib.parse.quote(run_id)}")
        thread = threading.Thread(target=server.serve_forever,
                                  name=f"dev-{tool}-trainer", daemon=True)
        thread.start()
        DEV_TRAINER_SERVERS[key] = (server, thread, url)
        return url


def dev_tools_page():
    """Developer/trainer utilities, styled to match the general chat-shell UI (shares
    /static/chat-shell.css so it looks like the rest of the normal interface)."""
    runs = scrape_trainer_runs()
    scrape_rows = "".join(
        f'<form method="post" action="/dev-trainer-open" target="_blank" class="dev-run">'
        f'<input type="hidden" name="tool" value="scrape">'
        f'<input type="hidden" name="run_id" value="{esc(row["id"])}">'
        f'<code class="dev-code">{esc(row["id"])}</code>'
        f'<button class="btn small" type="submit">Open</button></form>'
        for row in runs
    ) or '<div class="card-note">No completed Scrape Trainer runs were found.</div>'
    ver = chat_ui._asset_ver()
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark light">
<title>Developer tools &middot; Shortslab</title>
<link rel="icon" href="/static/app_icon.png" type="image/png">
<link rel="stylesheet" href="/static/chat-shell.css?v={ver}">
<style>
  .dev-wrap {{ max-width: var(--chat-max); margin: 0 auto; padding: 22px 20px 60px; display: flex; flex-direction: column; gap: 16px; }}
  .dev-top {{ display: flex; align-items: center; gap: 12px; margin-bottom: 2px; }}
  .dev-top h1 {{ flex: 1; font-size: 20px; }}
  .dev-brand {{ display: flex; align-items: center; gap: 10px; }}
  .dev-brand img {{ border-radius: 9px; }}
  .dev-runs {{ display: flex; flex-direction: column; gap: 8px; margin-top: 10px; }}
  .dev-run {{ display: flex; align-items: center; justify-content: space-between; gap: 12px;
    padding: 9px 12px; border: 1px solid var(--border); border-radius: var(--r-md); background: var(--surface-raised); }}
  .dev-code {{ font-family: var(--mono); font-size: 12.5px; color: var(--text-secondary); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
</style>
</head>
<body>
<div class="dev-wrap">
  <div class="dev-top">
    <div class="dev-brand">
      <img src="/static/app_icon.png" alt="" width="30" height="30">
      <h1>Developer tools</h1>
    </div>
    <a class="btn ghost small" href="/">&#8592; Back to Shortslab</a>
    <button type="button" class="btn ghost small" id="dev-theme">&#9788; Theme</button>
  </div>
  <div class="chat-card">
    <div class="card-cap">Local utilities</div>
    <div class="card-note">Review and training tools that edit trainer ground truth on this machine.</div>
  </div>
  <div class="chat-card">
    <h3>SFX Trainer</h3>
    <div class="card-note">Listen to the local sound library, correct roles / reactions, and activate the labels.</div>
    <div class="card-foot">
      <form method="post" action="/dev-trainer-open" target="_blank">
        <input type="hidden" name="tool" value="sfx">
        <button class="btn primary" type="submit">Open SFX Trainer</button>
      </form>
    </div>
  </div>
  <div class="chat-card">
    <h3>Scrape Trainer</h3>
    <div class="card-note">Open an existing scrape-training run and rate every collected clip.</div>
    <div class="dev-runs">{scrape_rows}</div>
  </div>
</div>
<script>
  (function(){{
    var saved; try {{ saved = localStorage.getItem("sl-chat-theme"); }} catch(e) {{}}
    document.documentElement.dataset.theme = saved || "dark";
    var b = document.getElementById("dev-theme");
    if (b) b.addEventListener("click", function(){{
      var t = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
      document.documentElement.dataset.theme = t;
      try {{ localStorage.setItem("sl-chat-theme", t); }} catch(e) {{}}
    }});
  }})();
</script>
</body>
</html>"""
    return html.encode("utf-8")


def parse_content_disposition(value):
    result = {}
    for part in value.split(";"):
        part = part.strip()
        if "=" in part:
            key, val = part.split("=", 1)
            result[key.lower()] = val.strip().strip('"')
    return result


def parse_multipart(content_type, body):
    fields = {}
    files = {}
    marker = "boundary="
    if marker not in content_type:
        return fields, files
    boundary = content_type.split(marker, 1)[1].split(";", 1)[0].strip().strip('"').encode()
    for raw_part in body.split(b"--" + boundary):
        raw_part = raw_part.strip()
        if not raw_part or raw_part == b"--":
            continue
        if raw_part.endswith(b"--"):
            raw_part = raw_part[:-2].strip()
        if b"\r\n\r\n" not in raw_part:
            continue
        header_blob, data = raw_part.split(b"\r\n\r\n", 1)
        headers = {}
        for line in header_blob.decode("utf-8", errors="replace").split("\r\n"):
            if ":" in line:
                key, value = line.split(":", 1)
                headers[key.lower().strip()] = value.strip()
        disp = parse_content_disposition(headers.get("content-disposition", ""))
        name = disp.get("name")
        if not name:
            continue
        data = data.rstrip(b"\r\n")
        filename = disp.get("filename")
        if filename:
            files[name] = {"filename": filename, "data": data}
        else:
            value = data.decode("utf-8", errors="replace")
            if name in ("initial_replace_media_path", "initial_remove_media_path"):
                fields.setdefault(name, []).append(value)
            else:
                fields[name] = value
    return fields, files


def form_page(clear=False, open_load=False, load_slug=""):
    # "New project" starts from clean defaults; normal load restores last session.
    state = normalize_ui_state({}) if clear else load_ui_state()
    previous_project_options = project_options_html()
    # Scraping is "ready" when a TikTok (or X) login is saved (no API keys).
    # tiktok_ready / twitter_ready drive the Connect controls below.
    try:
        import tiktok_login
        tiktok_avail = tiktok_login.available()
        tiktok_ready = tiktok_login.is_ready()
    except Exception:
        tiktok_avail = tiktok_ready = False
    try:
        import twitter_login
        twitter_ready = twitter_login.is_ready()
    except Exception:
        twitter_ready = False
    try:
        import clip_scraper
        scrape_ready = bool(clip_scraper.backend_active())   # any login backend is ready
    except Exception:
        scrape_ready = tiktok_ready or twitter_ready
    scrape_ready_js = "true" if scrape_ready else "false"
    tiktok_ready_js = "true" if tiktok_ready else "false"

    voice_tones = {
        "Zephyr": "Bright", "Puck": "Upbeat", "Charon": "Informative",
        "Kore": "Firm", "Fenrir": "Excitable", "Leda": "Youthful",
        "Orus": "Firm", "Aoede": "Breezy", "Callirrhoe": "Easy-going",
        "Autonoe": "Bright", "Enceladus": "Breathy", "Iapetus": "Clear",
        "Umbriel": "Easy-going", "Algieba": "Smooth", "Despina": "Smooth",
        "Erinome": "Clear", "Algenib": "Gravelly", "Rasalgethi": "Informative",
        "Laomedeia": "Upbeat", "Achernar": "Soft", "Alnilam": "Firm",
        "Schedar": "Even", "Gacrux": "Mature", "Pulcherrima": "Forward",
        "Achird": "Friendly", "Zubenelgenubi": "Casual", "Vindemiatrix": "Gentle",
        "Sadachbia": "Lively", "Sadaltager": "Knowledgeable", "Sulafat": "Warm",
    }
    sel_voice = state.get("tts_voice", pipeline.DEFAULT_TTS_VOICE)
    voice_options = "".join(
        f'<option value="{esc(v)}"{" selected" if sel_voice == v else ""}>'
        f'{esc(v)}{" &mdash; " + voice_tones[v] if v in voice_tones else ""}</option>'
        for v in pipeline.GEMINI_TTS_VOICES
    )
    sel_tts_model = state.get("tts_model", "flash")

    def checked(name):
        return " checked" if state.get(name) else ""

    body = f"""
    {brand_header()}
    <form id="short-form" method="post" action="/run" enctype="multipart/form-data">
      <input id="loaded-project-source" type="hidden" name="loaded_project_source" value="">
      <input type="hidden" name="ui_form" value="1">

      <div id="wizard">
        <div class="wiz-topnav" id="wiz-topnav" style="display:none;">
          <button type="button" class="button secondary wiz-back-btn" id="wiz-back-btn" onclick="wizBack()">&#8592; Back</button>
          <button type="button" class="button wiz-cont" id="wiz-cont-btn" onclick="wizNext()">Continue &#8594;</button>
        </div>
        <div id="wiz-headline" class="wiz-headline"><span class="wiz-type" id="wiz-type" aria-live="polite"></span></div>

      <div class="wiz-modemenu" id="wiz-modemenu" data-step="0" style="display:none;">
        <div class="modemenu-group">
          <div class="modemenu-grouplabel">&#9679; Make a new video</div>
          <div class="modemenu-cards">
            <button type="button" class="modemenu-card" onclick="selectMode('visuals')">
              <span class="mm-head"><span class="mm-ico">&#127916;</span><span class="mm-title">A Video from a Script</span></span>
              <span class="mm-desc">Turn a voice script into a full visual Short with the app pipeline.</span>
            </button>
            <button type="button" class="modemenu-card" onclick="selectMode('viraltrans')">
              <span class="mm-head"><span class="mm-ico">&#129529;</span><span class="mm-title">A Viral Short from a Topic</span></span>
              <span class="mm-desc">Give one topic - the agents write, film, caption and voice it fully autonomously.</span>
            </button>
            <button type="button" class="modemenu-card" onclick="selectMode('reddit')">
              <span class="mm-head"><span class="mm-ico">&#128172;</span><span class="mm-title">A Reddit Story Video</span></span>
              <span class="mm-desc">A Reddit-style story over Minecraft parkour with an AI voiceover.</span>
            </button>
            <button type="button" class="modemenu-card" onclick="selectMode('longform')">
              <span class="mm-head"><span class="mm-ico">&#127912;</span><span class="mm-title">A Longform Image Set</span></span>
              <span class="mm-desc">Upload a prompt list - Higgsfield renders one 16:9 FLUX.2 Pro image per line, named by timestamp.</span>
            </button>
          </div>
        </div>
        <div class="modemenu-group">
          <div class="modemenu-grouplabel">&#9679; Polish a finished video &middot; Masters</div>
          <div class="modemenu-cards">
            <button type="button" class="modemenu-card" onclick="selectMode('sfx')">
              <span class="mm-head"><span class="mm-ico">&#128266;</span><span class="mm-title">Sound Effects</span></span>
              <span class="mm-desc">Upload a finished Short and add editor SFX from the local library.</span>
            </button>
            <button type="button" class="modemenu-card" onclick="selectMode('visual')">
              <span class="mm-head"><span class="mm-ico">&#10132;</span><span class="mm-title">Visual Effects</span></span>
              <span class="mm-desc">Upload a Short - Opus 4.8 adds animated red arrows + fitting SFX.</span>
            </button>
            <button type="button" class="modemenu-card" onclick="selectMode('captions')">
              <span class="mm-head"><span class="mm-ico">&#128172;&#65039;</span><span class="mm-title">Captions</span></span>
              <span class="mm-desc">Upload a video and burn in viral word-by-word captions, fully locally.</span>
            </button>
          </div>
        </div>
      </div>

      <div class="create-bar panel" data-step="4">
        <div class="cbar-row cbar-top">
          <div class="cbar-cell preset-cell">
            <span class="cbar-cap">Preset {help_tip("Save stores the current speaker, models, speaker image, visual direction and all toggles as a preset. Load opens your saved presets plus the built-in Japanese one. The agent decides automatically whether to do a full or a smart (fill-missing) run.")}</span>
            <div class="preset-actions">
              <button type="button" class="cbar-icon-btn" onclick="saveCustomPreset()" title="Save preset" aria-label="Save preset">&#128190;</button>
              <button type="button" class="cbar-icon-btn" onclick="applyCustomPreset()" title="Load / recall preset" aria-label="Load preset">&#128194;</button>
            </div>
          </div>
          <div class="cbar-cell reasoning-cell">
            <span class="cbar-cap">Reasoning model</span>
            <select name="reasoning_model" data-reasoning-value="{esc(state.get('reasoning_mode') or '')}">
              <option value="anthropic/claude-fable-5"{' selected' if state.get("reasoning_model") == "anthropic/claude-fable-5" else ""}>Claude Fable 5 (newest, top quality)</option>
              <option value="anthropic/claude-sonnet-5"{' selected' if state.get("reasoning_model") == "anthropic/claude-sonnet-5" else ""}>Claude Sonnet 5 (fast, high quality)</option>
              <option value="anthropic/claude-opus-4.8"{' selected' if state.get("reasoning_model") == "anthropic/claude-opus-4.8" else ""}>Claude Opus 4.8 (best 4.x quality)</option>
              <option value="openai/gpt-5.5"{' selected' if state.get("reasoning_model") == "openai/gpt-5.5" else ""}>GPT-5.5 (fast, standard)</option>
              <option value="openai/gpt-5.6-sol"{' selected' if state.get("reasoning_model") == "openai/gpt-5.6-sol" else ""}>GPT-5.6 Sol</option>
              <option value="openai/gpt-5.6-terra"{' selected' if state.get("reasoning_model") == "openai/gpt-5.6-terra" else ""}>GPT-5.6 Terra</option>
              <option value="openai/gpt-5.6-luna"{' selected' if state.get("reasoning_model") == "openai/gpt-5.6-luna" else ""}>GPT-5.6 Luna</option>
              <option value="google/gemini-3.5-flash"{' selected' if state.get("reasoning_model") == "google/gemini-3.5-flash" else ""}>Gemini 3.5 Flash (fastest, cheapest)</option>
              <option value="google/gemini-3.1-flash-lite"{' selected' if state.get("reasoning_model") == "google/gemini-3.1-flash-lite" else ""}>Gemini 3.1 Flash Lite</option>
              <option value="google/gemini-3.1-pro-preview"{' selected' if state.get("reasoning_model") == "google/gemini-3.1-pro-preview" else ""}>Gemini 3.1 Pro Preview (cheap)</option>
            </select>
          </div>
        </div>

        <div class="cbar-scroll">
        <div class="cbar-sep" aria-hidden="true"></div>
        <div class="cbar-section" id="clipsource-panel">
          <span class="cbar-cap">Clip source {help_tip("Where the moving footage comes from. Generate = AI video/images (Seedance, GPT-Image). Scrape = download real TikTok clips that match a visual style and cut them together. Scraping disables the AI video/image models and the AI-image outputs.")}</span>
          <input type="hidden" name="clip_source" id="clip-source" value="{esc(state.get('clip_source') or 'generate')}">
          <div class="seg-switch" id="clip-seg" data-src="{esc(state.get('clip_source') or 'generate')}">
            <span class="seg-thumb" aria-hidden="true"></span>
            <button type="button" class="seg-opt" data-src="generate" onclick="setClipSource('generate')">&#9881;&#65039; AI Generate</button>
            <button type="button" class="seg-opt" data-src="scrape" onclick="setClipSource('scrape')">&#127916; Scrape TikTok + X</button>
          </div>
          <div id="ai-models" class="ai-models">
            <div class="cbar-cell">
              <span class="cbar-cap">Video model</span>
              <select name="video_model">
                <option value="seedance-2.0"{' selected' if state.get("video_model", state.get("seedance_model", "seedance-2.0")) == "seedance-2.0" else ""}>Seedance 2.0 (Web Search + Audio)</option>
                <option value="seedance-2.0-fast"{' selected' if state.get("video_model") == "seedance-2.0-fast" else ""}>Seedance 2.0 Fast (Web Search + Audio)</option>
                <option value="seedance-v1.5-pro"{' selected' if state.get("video_model", state.get("seedance_model")) == "seedance-v1.5-pro" else ""}>Seedance 1.5 Pro</option>
                <option value="ltx-2.3"{' selected' if state.get("video_model") == "ltx-2.3" else ""}>LTX-2.3 (cheap)</option>
                <option value="happyhorse-1.1"{' selected' if state.get("video_model") == "happyhorse-1.1" else ""}>Happy Horse 1.1 (720p)</option>
              </select>
            </div>
            <div class="cbar-cell">
              <span class="cbar-cap">Image model</span>
              <select name="image_model">
                <option value="openai/gpt-image-2/text-to-image"{' selected' if state.get("image_model") == "openai/gpt-image-2/text-to-image" else ""}>GPT-Image-2 (best)</option>
                <option value="google/nano-banana-2/text-to-image"{' selected' if state.get("image_model") == "google/nano-banana-2/text-to-image" else ""}>Nano-Banana-2 (cheap)</option>
              </select>
            </div>
          </div>
          <div id="scrape-settings" class="scrape-settings" style="display:none;">
            <input type="hidden" name="scrape_platforms" value="tiktok,x">
            <input type="hidden" name="influencer_hook" value="on">
            <label class="scrape-lbl">Scraping engine {help_tip("V2 (Relevance-first) plans concrete visible situations, finds usable SEGMENTS anywhere inside a video, ranks by relevance (not likes) and matches per scene - fewer hard rejects, more on-topic footage. V1 (Legacy) is the original bucket + like-gated scrape. V2 is the default.")}</label>
            <div class="seg-switch" id="engine-seg" data-eng="{esc((state.get('scraping_engine') or 'v2'))}" style="margin-bottom:10px;">
              <span class="seg-thumb" aria-hidden="true"></span>
              <button type="button" class="seg-opt" data-eng="v2" onclick="setScrapeEngine('v2')">&#9889; Scrape V2 &middot; Relevance-first</button>
              <button type="button" class="seg-opt" data-eng="v1" onclick="setScrapeEngine('v1')">&#128230; Scrape V1 &middot; Legacy</button>
            </div>
            <input type="hidden" name="scraping_engine" id="scraping-engine" value="{esc((state.get('scraping_engine') or 'v2'))}">
            <div class="scrape-auto-note">&#129504; Search terms are derived from visual intent. TikTok handles native action/lifestyle searches; X receives only validated proof, event and exact-action phrases. The opening hook still requires 20K+ likes; body clips have no minimum-like gate. {help_tip("Body footage is ranked using your selected result order and still passes technical, caption and semantic quality gates.")}</div>
            <label class="scrape-lbl">Add your own terms (optional) {help_tip("Extra search terms on top of what the agent derives — they are ALWAYS included. Add English or Japanese words/hashtags (e.g. 原宿 ファッション). Press + or Enter to add.")}</label>
            <div class="chip-add">
              <input type="text" id="term-input" placeholder="e.g. 原宿 ファッション, neon alley" onkeydown="if(event.key==='Enter'){{event.preventDefault();addTerm();}}">
              <button type="button" class="button" onclick="addTerm()" title="Add term">+</button>
            </div>
            <div class="chips" id="term-chips"></div>
            <input type="hidden" name="scrape_terms" id="scrape-terms" value="{esc(state.get('scrape_terms'))}">
            <div class="tiktok-connect" id="tiktok-connect">
              <span class="tt-status {'on' if tiktok_ready else 'off'}" id="tt-status-dot"></span>
              <span class="tt-text" id="tt-status-text">{'TikTok connected' if tiktok_ready else 'TikTok not connected'}</span>
              {help_tip("Scraping downloads real TikTok clips using YOUR own logged-in TikTok account - no API key. Click Connect, a browser window opens, log in to TikTok once, and the session is saved for future runs. Modern Chrome encrypts its cookies (DPAPI), so this dedicated login is required.")}
              <button type="button" class="button secondary tt-btn" id="tt-login-btn" onclick="connectTikTok()">{'Reconnect' if tiktok_ready else '&#128279; Connect TikTok'}</button>
            </div>
            <div class="tiktok-connect" id="twitter-connect">
              <span class="tt-status {'on' if twitter_ready else 'off'}" id="xt-status-dot"></span>
              <span class="tt-text" id="xt-status-text">{'X connected' if twitter_ready else 'X not connected (optional)'}</span>
              {help_tip("Optional second source: when connected, every scrape searches X/Twitter videos IN PARALLEL with TikTok and merges the results through the same quality gates. Click Connect, log in to X once, done.")}
              <button type="button" class="button secondary tt-btn" id="xt-login-btn" onclick="connectTwitter()">{'Reconnect' if twitter_ready else '&#128279; Connect X'}</button>
            </div>
            {('' if tiktok_avail else '<div class="conn-warn">&#9888; Browser engine missing. Run: pip install playwright &amp;&amp; playwright install chromium</div>')}
            <div class="bgm-pick" id="bgm-pick">
              <label class="scrape-lbl">Background music {help_tip("Optional. Pick a track to play as a soft bed under the voice (like the reference channel). Press the play button to preview it before you generate. Leave on 'None' for voice + SFX only.")}</label>
              <div class="bgm-row">
                <select name="background_music_choice" id="bgm-select" onchange="bgmOnChange()">
                  <option value="none">None (voice + SFX only)</option>
                </select>
                <button type="button" class="button secondary bgm-play" id="bgm-play" onclick="bgmPreview()" title="Preview" disabled>&#9654;</button>
              </div>
              <audio id="bgm-audio" preload="none"></audio>
            </div>
            <script>window.SCRAPE_READY = {scrape_ready_js}; window.TIKTOK_READY = {tiktok_ready_js}; window.TIKTOK_AVAIL = {'true' if tiktok_avail else 'false'}; window.BGM_SAVED = {json.dumps(esc(state.get('background_music_choice') or 'none'))};</script>
          </div>
        </div>

        <div class="cbar-sep" aria-hidden="true"></div>
        <div class="cbar-section">
          <span class="cbar-cap">Outputs {help_tip("Turn individual parts of the pipeline on or off. Off = that part is skipped entirely, and a Smart run will not generate it either. Scraping disables the AI-image outputs.")}</span>
          <div class="otoggles">
            <label class="otoggle"><input type="checkbox" name="out_web_images"{checked("out_web_images")}><span>Web images</span></label>
            <label class="otoggle"><input type="checkbox" name="out_wikimedia"{checked("out_wikimedia")}><span>Wikimedia images</span></label>
            <label class="otoggle"><input type="checkbox" name="out_gpt_images"{checked("out_gpt_images")}><span>Generated images</span></label>
            <label class="otoggle"><input type="checkbox" name="out_video_clips"{checked("out_video_clips")}><span>Video clips</span></label>
            <label class="otoggle"><input type="checkbox" name="out_sfx"{checked("out_sfx")}><span>Sound effects</span></label>
            <label class="otoggle"><input type="checkbox" name="out_transition_sfx"{checked("out_transition_sfx")}><span>Transition SFX</span></label>
            <label class="otoggle"><input type="checkbox" name="out_background_music"{checked("out_background_music")}><span>Background music</span></label>
            <label class="otoggle"><input type="checkbox" name="out_captions"{checked("out_captions")}><span>Captions</span></label>
            <label class="otoggle"><input type="checkbox" name="halt_after_speech"{checked("halt_after_speech")}><span>Halt after speech</span></label>
          </div>
          <div class="cbar-cell" style="margin-top:10px;">
            <span class="cbar-cap">SFX amount {help_tip("How dense the sound design is. Low = the previous sparse feel (~1 effect every 2-4s). Medium = lively, every cut + every emphasised word (~1 per 1.5-2.5s). High = hyper-edited TikTok density (~1 per 0.8-1.5s). Applies to normal runs and the timeline SFX engine.")}</span>
            <select name="sfx_amount">
              <option value="low"{' selected' if state.get("sfx_amount") == "low" else ""}>Low (sparse, subtle)</option>
              <option value="medium"{' selected' if state.get("sfx_amount", "medium") == "medium" else ""}>Medium (lively, recommended)</option>
              <option value="high"{' selected' if state.get("sfx_amount") == "high" else ""}>High (hyper-edited, dense)</option>
            </select>
          </div>
        </div>
        </div>

        <button type="submit" class="create-short-btn create-short-big">&#9889; Create Short</button>
      </div>

      <section class="stack">
        {advanced_hidden_inputs(state)}
        <div class="panel accent" data-step="1">
          <label>Text script {help_tip("The voiceover is generated from this with Gemini TTS. Mark the opening line(s) as the hook: it is spoken first, then a short pause, then the rest. With a speaker image it drives the InfiniteTalk talking-head opening.")}</label>
          <div class="script-wrap">
            <div class="script-highlight" id="script-highlight" aria-hidden="true"></div>
            <textarea id="script-field" name="script" spellcheck="false" placeholder="Paste your script here. Write it as a punchy spoken narration — the AI generates the voiceover, finds visuals and cuts the Short from this text. Then select your opening line(s) and click &#8220;Mark hook&#8221;.">{esc(state.get("script"))}</textarea>
          </div>
          <input type="hidden" name="hook_text" id="hook-text" value="{esc(state.get('hook_text'))}">
          <input type="hidden" name="impact_word" id="impact-word" value="{esc(state.get('impact_word'))}">
          <div class="hook-controls">
            <button type="button" class="button secondary hook-btn" onclick="markHook()">&#9733; Mark hook</button>
            <button type="button" class="button secondary hook-btn" onclick="clearHook()">Clear</button>
            <span id="hook-indicator" class="hook-dot" hidden></span>
          </div>
          <div id="script-relevancy-block" style="display:none; margin-top:16px;">
            <label class="scrape-lbl">Script relevancy <span id="relv-val" class="relv-val">{esc(state.get('script_relevancy') or '70')}%</span> {help_tip("Scrape mode only: how tightly the downloaded clips must match this spoken script versus pure visual style. High = clips closely follow what's being said. Low = prioritize the look (more b-roll of the vibe), looser tie to the words.")}</label>
            <input type="range" name="script_relevancy" id="script-relevancy" min="0" max="100" step="5" value="{esc(state.get('script_relevancy') or '70')}" oninput="document.getElementById('relv-val').textContent=this.value+'%';">
            <label class="scrape-lbl" for="scrape-sort">Search result order</label>
            <select name="scrape_sort" id="scrape-sort">
              <option value="ALL"{' selected' if state.get('scrape_sort') in (None, '', 'ALL') else ''}>All sortings (liked &rarr; relevance &rarr; viewed &rarr; recent)</option>
              <option value="MOST_LIKED"{' selected' if state.get('scrape_sort') == 'MOST_LIKED' else ''}>Most liked</option>
              <option value="MOST_VIEWED"{' selected' if state.get('scrape_sort') == 'MOST_VIEWED' else ''}>Most viewed</option>
              <option value="MOST_RECENT"{' selected' if state.get('scrape_sort') == 'MOST_RECENT' else ''}>Most recent</option>
              <option value="RELEVANCE"{' selected' if state.get('scrape_sort') == 'RELEVANCE' else ''}>Platform relevance</option>
            </select>
          </div>
        </div>

        <div class="panel" id="visual-panel" data-step="2">
          <label>Optional Visual Direction {help_tip("Optional. The Voice Script is authoritative; this is secondary style guidance (e.g. darker documentary look, faster cuts, more maps). Leave empty to let the agent plan visuals from the script.")}</label>
          <input type="hidden" name="use_visual_direction" value="on">
          <textarea class="visual-textarea" name="visual_script" placeholder="Optional. Leave empty to let the agent plan visuals from the script. Use this only for style, e.g. darker documentary style, faster cuts, more maps.">{esc(state.get("visual_script"))}</textarea>
        </div>

        <div class="panel" id="loaded-actions-panel" style="display:none;">
          <button type="button" class="button" onclick="openTimeline()" style="margin-bottom:12px;">&#127902; Open timeline editor (trim, mix &amp; render)</button>
          <label>Loaded project &mdash; what should the run do? {help_tip("Only shown for a loaded project. Choose what to reuse versus regenerate before re-rendering.")}</label>
          <input type="hidden" name="loaded_project_mode" id="loaded-project-mode" value="{esc(state.get('loaded_project_mode') or 'normal')}">
          <div class="action-btns">
            <button type="button" class="action-btn" data-mode="normal" onclick="setProjectMode('normal')">Normal run<small>fresh pass in this folder</small></button>
            <button type="button" class="action-btn" data-mode="recut_existing_only" onclick="setProjectMode('recut_existing_only')">Recut existing<small>reuse current media, re-edit</small></button>
            <button type="button" class="action-btn" data-mode="recut_new_web_images" onclick="setProjectMode('recut_new_web_images')">New web images<small>refetch images + recut</small></button>
            <button type="button" class="action-btn" data-mode="recut_regenerate_seedance" onclick="setProjectMode('recut_regenerate_seedance')">Regenerate clips<small>new Seedance clips + recut</small></button>
            <button type="button" class="action-btn" data-mode="recut_recreate_speaker_clip" onclick="setProjectMode('recut_recreate_speaker_clip')">Recreate hook<small>new speaker hook + recut</small></button>
          </div>
        </div>

        <div class="panel" data-step="3">
          <label>Voice &amp; narration {help_tip("Narration is generated from your script with Gemini TTS (English). The voice is force-aligned for frame-accurate word-by-word captions and beat-synced cuts. Optionally add a speaker face below for a lip-synced talking-head opening.")}</label>
          <input type="hidden" name="speaker_name" value="{esc(state.get('speaker_name') or 'Narrator')}">
          <div style="display:flex; gap:10px; flex-wrap:wrap; align-items:center;">
            <select name="tts_voice" id="tts-voice-select" style="flex:1; min-width:170px;">{voice_options}</select>
            <button type="button" class="button secondary voice-play" id="voice-play" onclick="voicePreview()" title="Preview this voice (~10s)">&#9654;</button>
            <select name="tts_model" style="flex:1; min-width:170px;">
              <option value="flash"{' selected' if sel_tts_model == 'flash' else ''}>Gemini 2.5 Flash TTS (cheaper)</option>
              <option value="pro"{' selected' if sel_tts_model == 'pro' else ''}>Gemini 2.5 Pro TTS (higher quality)</option>
            </select>
          </div>
          <audio id="voice-audio" preload="none"></audio>
          <input type="hidden" name="mix_voice_in_final" value="on">
          <div class="checks" style="margin-top:18px;">
            <label><input type="checkbox" name="force_regenerate"{checked("force_regenerate")}> Fresh voice take (keep saved scrape) {help_tip("Generate a fresh voice take, but keep and re-check already downloaded TikTok/X media from the same script. This prevents a cancelled long scrape from being wasted.")}</label>
          </div>
        </div>
        <div class="panel toggle-panel" id="speaker-panel" data-step="3">
          <div class="panel-head">
            <label>Use generated speaker video clip {help_tip("Optional. When on, pick (or upload) a face below — the marked hook line is spoken by this person as a lip-synced talking-head opening clip (InfiniteTalk). When off, no talking-head clip is made.")}</label>
            <label class="switch" title="Generate a talking-head speaker clip on/off"><input type="checkbox" name="enable_speaker_hook"{checked("enable_speaker_hook")} onchange="toggleSection('speaker-panel', this.checked)"></label>
          </div>
          <div class="panel-body">
            <input type="hidden" name="speaker_image_path" id="speaker-image-path" value="{esc(state.get('speaker_image_path'))}">
            <input type="file" name="speaker_image_file" id="speaker-file" accept="image/*" onchange="onSpeakerUpload(this)" style="display:none">
            {speaker_gallery_html(state.get('speaker_image_path'))}
          </div>
        </div>
      </section>
      </div>
    </form>

    <div id="load-modal" class="modal-overlay">
      <div class="modal-content">
        <button class="modal-close" onclick="document.getElementById('load-modal').classList.remove('active')">&times;</button>
        <h2 style="margin-bottom: 15px;">Load previous project (Repair / Recut)</h2>
        <div>
          <div class="project-load-controls" style="display: flex; flex-direction: column; align-items: stretch; gap: 8px; max-height: 50vh; overflow-y: auto;">
            {previous_project_options}
          </div>
          <div id="project-load-status" class="hint" style="margin-top: 15px;"></div>
        </div>
      </div>
    </div>

    <div id="preset-popup" class="modal-overlay">
      <div class="modal-content preset-popup-card">
        <button class="modal-close" onclick="closePresetPopup()">&times;</button>
        <h2 style="margin-bottom: 4px;">Presets</h2>
        <p class="hint" style="margin: 0 0 14px;">Load a saved setup, or save the current settings. Save&nbsp;as makes a new one; Overwrite replaces the active preset.</p>
        <div id="preset-popup-body"></div>
      </div>
    </div>
    """
    body += """
    <script>
    (function(){
      function hesc(s){return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
      function renderHighlight(){
        var ta=document.getElementById('script-field');
        var hl=document.getElementById('script-highlight');
        var h=document.getElementById('hook-text');
        if(!ta||!hl) return;
        var text=ta.value||'';
        var hook=(h&&h.value||'').trim();
        var html;
        var idx=hook?text.indexOf(hook):-1;
        if(hook&&idx>=0){
          html=hesc(text.slice(0,idx))+'<mark class="hook-mark">'+hesc(text.slice(idx,idx+hook.length))+'</mark>'+hesc(text.slice(idx+hook.length));
        } else {
          html=hesc(text);
        }
        hl.innerHTML=html+'\\n';
        hl.scrollTop=ta.scrollTop; hl.scrollLeft=ta.scrollLeft;
      }
      function ind(){
        // the marked hook is shown by the amber highlight in the script itself,
        // so no separate status text is needed.
        renderHighlight();
      }
      window.markHook=function(){
        var ta=document.getElementById('script-field'); if(!ta) return;
        var sel=ta.value.substring(ta.selectionStart, ta.selectionEnd).trim();
        if(!sel){ alert('Select the opening line(s) inside the script first, then click \\u201cMark selection as hook\\u201d.'); return; }
        var h=document.getElementById('hook-text'); if(h){ h.value=sel; } ind();
      };
      window.clearHook=function(){ var h=document.getElementById('hook-text'); if(h){ h.value=''; } ind(); };
      document.addEventListener('DOMContentLoaded', function(){
        var ta=document.getElementById('script-field');
        if(ta){
          ta.addEventListener('input', renderHighlight);
          ta.addEventListener('scroll', function(){ var hl=document.getElementById('script-highlight'); if(hl){ hl.scrollTop=ta.scrollTop; hl.scrollLeft=ta.scrollLeft; } });
        }
        ind();
      });
      ind();
    })();
    </script>
    """
    if clear:
        # the server already rendered a clean form; also wipe the autosave so the
        # client-side restore can't refill it (otherwise "New project" keeps the old script).
        body += (
            "<script>try{Object.keys(localStorage).forEach(function(k){"
            "if(k.indexOf('autonomous-shorts-agent-ui-state')===0)localStorage.removeItem(k);});}catch(e){}</script>"
        )
    if open_load:
        body += (
            "<script>document.addEventListener('DOMContentLoaded',function(){"
            "var m=document.getElementById('load-modal');if(m){m.classList.add('active');}"
            "});</script>"
        )
    if load_slug:
        body += (
            "<script>(function(){var s=" + json.dumps(load_slug) + ";"
            "function go(){if(typeof window.loadProject==='function'){window.loadProject(s);}"
            "else{setTimeout(go,60);}}"
            "if(document.readyState==='loading'){document.addEventListener('DOMContentLoaded',go);}else{go();}"
            "})();</script>"
        )
    return page("Shortslab", body)


def sfx_page():
    body = f"""
    {brand_header()}
    <form method="post" action="/sfx-run" enctype="multipart/form-data">
      <section class="stack">
        <div class="panel accent">
          <label>Finished Short (video)</label>
          <label class="filepick" for="sfx-video-file"><span class="filepick-btn">&#128193; Choose video file</span><span class="filepick-name" id="sfx-video-name">No file chosen</span></label>
          <input type="file" name="video_file" id="sfx-video-file" class="filepick-input" accept="video/mp4,video/quicktime,video/webm,video/x-matroska,.mp4,.mov,.webm,.mkv" required onchange="var n=document.getElementById('sfx-video-name'); if(n) n.textContent=this.files.length?this.files[0].name:'No file chosen';">
          <div class="hint">Rendered vertical MP4 / MOV / WebM. The video is copied losslessly; only the audio gets the new sound effects mixed in.</div>
        </div>
        <div class="panel">
          <label>Planning agent</label>
          <select name="reasoning_model">
            <option value="anthropic/claude-fable-5">Claude Fable 5 (newest, top quality)</option>
            <option value="anthropic/claude-sonnet-5">Claude Sonnet 5 (fast, high quality)</option>
            <option value="anthropic/claude-opus-4.8" selected>Claude Opus 4.8 (recommended)</option>
            <option value="openai/gpt-5.5">GPT-5.5 (faster)</option>
            <option value="openai/gpt-5.6-sol">GPT-5.6 Sol</option>
            <option value="openai/gpt-5.6-terra">GPT-5.6 Terra</option>
            <option value="openai/gpt-5.6-luna">GPT-5.6 Luna</option>
            <option value="google/gemini-3.5-flash">Gemini 3.5 Flash (fastest)</option>
            <option value="google/gemini-3.1-flash-lite">Gemini 3.1 Flash Lite</option>
            <option value="google/gemini-3.1-pro-preview">Gemini 3.1 Pro Preview (cheap)</option>
          </select>
          <div class="hint">The agent detects scene changes, reads the timed transcript, and chooses sound effects from your local <code>soundeffects/</code> library.</div>
        </div>
        <div class="panel">
          <label>SFX amount {help_tip("How dense the sound design is. Low = the previous sparse feel (~1 effect every 2-4s). Medium = lively, every cut + every emphasised word (~1 per 1.5-2.5s). High = hyper-edited TikTok density (~1 per 0.8-1.5s).")}</label>
          <select name="sfx_amount">
            <option value="low">Low (sparse, subtle)</option>
            <option value="medium" selected>Medium (lively, recommended)</option>
            <option value="high">High (hyper-edited, dense)</option>
          </select>
        </div>
        <button type="submit">Add sound effects</button>
      </section>

      <section class="stack">
        <div class="loaded-media-panel">
          <h2 style="margin-bottom: 12px;">How the agent works</h2>
          <ol class="hint" style="margin: 0; padding-left: 18px; line-height: 1.9;">
            <li><strong>Scene detection</strong> &mdash; ffmpeg finds every hard image/scene change.</li>
            <li><strong>Transcription</strong> &mdash; Gemini&nbsp;3.5 Flash transcribes the speech with timing.</li>
            <li><strong>Opus&nbsp;4.8 planning</strong> &mdash; the agent places whooshes on cuts and impacts/stingers to emphasise key spoken words and reveals.</li>
            <li><strong>Lossless mix</strong> &mdash; the chosen effects are mixed quietly under your audio; the picture is copied bit-for-bit.</li>
          </ol>
          <div class="hint" style="margin-top: 16px;">You get the enhanced video plus the original and a JSON plan of every effect and why it was placed.</div>
        </div>
      </section>
    </form>
    """
    return page("AI Sound-Effect Pass", body)


def visual_page():
    body = f"""
    {brand_header()}
    <form method="post" action="/visual-run" enctype="multipart/form-data">
      <section class="stack">
        <div class="panel accent">
          <label>Finished Short (video)</label>
          <label class="filepick" for="vis-video-file"><span class="filepick-btn">&#128193; Choose video file</span><span class="filepick-name" id="vis-video-name">No file chosen</span></label>
          <input type="file" name="video_file" id="vis-video-file" class="filepick-input" accept="video/mp4,video/quicktime,video/webm,video/x-matroska,.mp4,.mov,.webm,.mkv" required onchange="var n=document.getElementById('vis-video-name'); if(n) n.textContent=this.files.length?this.files[0].name:'No file chosen';">
          <div class="hint">Rendered vertical MP4 / MOV / WebM. Opus&nbsp;4.8 DIRECTS a dense visual pass: thick red arrows fly in and point at the subject of each punchy line, plus cute neko reactions &mdash; each with its own animation and a fitting click/ding.</div>
        </div>
        <div class="panel">
          <label>Analysis agent</label>
          <select name="reasoning_model">
            <option value="anthropic/claude-fable-5">Claude Fable 5 (newest, top quality)</option>
            <option value="anthropic/claude-sonnet-5">Claude Sonnet 5 (fast, high quality)</option>
            <option value="anthropic/claude-opus-4.8" selected>Claude Opus 4.8 (recommended)</option>
            <option value="openai/gpt-5.5">GPT-5.5 (faster)</option>
            <option value="openai/gpt-5.6-sol">GPT-5.6 Sol</option>
            <option value="openai/gpt-5.6-terra">GPT-5.6 Terra</option>
            <option value="openai/gpt-5.6-luna">GPT-5.6 Luna</option>
            <option value="google/gemini-3.5-flash">Gemini 3.5 Flash (fastest)</option>
            <option value="google/gemini-3.1-flash-lite">Gemini 3.1 Flash Lite</option>
            <option value="google/gemini-3.1-pro-preview">Gemini 3.1 Pro Preview (cheap)</option>
          </select>
          <div class="hint">The agent looks at real frames + the timed transcript, finds the concrete on-screen target per punchy moment, and only then places an arrow at it.</div>
        </div>
        <div class="panel">
          <label>Effect amount {help_tip("How dense the arrow pass is. Low = the previous feel (up to ~18 moments, arrows on ~2 of 3). Medium = more moments checked (~28), arrows on ~3 of 4. High = hyper-dense (~42 moments, an arrow on practically every concrete target).")}</label>
          <select name="vfx_amount">
            <option value="low">Low (sparse)</option>
            <option value="medium" selected>Medium (dense, recommended)</option>
            <option value="high">High (hyper-dense)</option>
          </select>
        </div>
        <div class="panel">
          <label class="otoggle" style="margin:0;"><input type="checkbox" name="add_characters" value="on" checked><span>Add kawaii neko reactions (AI-directed)</span></label>
          <div class="hint">Optional. The same AI also bounces a cute pixel cat (shocked, laughing, love, angry, crying&hellip;) into a corner when a moment&rsquo;s mood calls for a reaction &mdash; matched to the spoken line.</div>
        </div>
        <button type="submit">&#10132; Add intelligent arrows</button>
      </section>

      <section class="stack">
        <div class="loaded-media-panel">
          <h2 style="margin-bottom: 12px;">How the agent works</h2>
          <ol class="hint" style="margin: 0; padding-left: 18px; line-height: 1.9;">
            <li><strong>Scene detection + transcription</strong> &mdash; ffmpeg finds cuts; the speech is transcribed with timing.</li>
            <li><strong>Opus&nbsp;4.8 directs</strong> &mdash; for each punchy moment it looks at the real frame and decides whether a <strong>red arrow</strong> should point at the concrete subject, plus an optional kawaii neko reaction. Dense, like the reference edits.</li>
            <li><strong>Animated overlays</strong> &mdash; arrows FLY IN straight and nudge-point at the target, nekos bounce in.</li>
            <li><strong>Fitting SFX</strong> &mdash; a click/ding/impact from your local library fires exactly when each overlay appears.</li>
          </ol>
          <div class="hint" style="margin-top: 16px;">You get the enhanced video plus the original and a JSON plan of every effect, its target and confidence.</div>
        </div>
      </section>
    </form>
    """
    return page("AI Visual-Arrow Pass", body)


def viraltrans_page():
    chips = "".join(
        f'<button type="button" class="vt-topic" data-topic="{esc(t)}" onclick="vtPick(this)">{esc(t)}</button>'
        for t in viral_transformation.TOPIC_PRESETS)
    body = f"""
    {brand_header()}
    <form method="post" action="/viraltrans-generate">
      <section class="stack">
        <div class="panel accent">
          <label>Viral Transformation Short &mdash; pick ONE topic. That's all.</label>
          <div class="hint" style="margin-bottom:10px;">The agents handle everything else autonomously: concept, the DECLARE &rarr; ASSESS &rarr; ISOLATE &rarr; PROCESS &rarr; BUILD &rarr; REVEAL structure, GPT-Image-2 reference images, Seedance&nbsp;2.0 clips, QA, captions, music/SFX, final MP4 and metadata. No voice script, no visual script, no prompts.</div>
          <div class="vt-topics">{chips}</div>
          <label style="margin-top:12px;">Or type your own topic</label>
          <input type="text" name="topic" id="vt-topic" placeholder="e.g. street dog salon" autocomplete="off">
          <button type="submit" style="margin-top:14px;">&#129529; Generate</button>
        </div>
      </section>
      <section class="stack">
        <div class="loaded-media-panel">
          <h2 style="margin-bottom: 12px;">What happens after Generate</h2>
          <ol class="hint" style="margin: 0; padding-left: 18px; line-height: 1.9;">
            <li><strong>Planning concept</strong> &mdash; the Topic Strategist narrows your topic into an "I Removed 5,000 Tangles From This Street Dog For This" style concept with an absurd metric (+ safety check).</li>
            <li><strong>Creating scenes</strong> &mdash; a strict 8&ndash;10 scene plan across the six phases, ~35&ndash;40s total (max ~40s of generated Seedance footage), PROCESS gets the most cuts.</li>
            <li><strong>Generating images &amp; WaveSpeed videos</strong> &mdash; one GPT-Image-2 reference per scene keeps the subject consistent; Seedance&nbsp;2.0 animates each one. Failures retry, then fall back to a zoom/pan motion clip.</li>
            <li><strong>Checking clips</strong> &mdash; vision QA rejects distorted/off-topic clips.</li>
            <li><strong>Editing final short</strong> &mdash; hard cuts, bold 1&ndash;5-word captions, music + satisfying SFX, riser + payoff hit on the reveal.</li>
            <li><strong>Export complete</strong> &mdash; MP4 + YouTube/TikTok titles, description and hashtags.</li>
          </ol>
        </div>
      </section>
    </form>
    <script>
      window.vtPick = function (btn) {{
        document.querySelectorAll(".vt-topic").forEach(function (b) {{ b.classList.remove("active"); }});
        btn.classList.add("active");
        var input = document.getElementById("vt-topic");
        if (input) input.value = btn.getAttribute("data-topic");
        try {{ if (typeof playClick === "function") playClick(); }} catch (e) {{}}
      }};
    </script>
    """
    return page("Viral Transformation Creator", body)


def caption_page():
    body = f"""
    {brand_header()}
    <form method="post" action="/captions-run" enctype="multipart/form-data">
      <section class="stack">
        <div class="panel accent">
          <label>Video to caption</label>
          <label class="filepick" for="cap-video-file"><span class="filepick-btn">&#128193; Choose video file</span><span class="filepick-name" id="cap-video-name">No file chosen</span></label>
          <input type="file" name="video_file" id="cap-video-file" class="filepick-input" accept="video/mp4,video/quicktime,video/webm,video/x-matroska,.mp4,.mov,.webm,.mkv" required onchange="var n=document.getElementById('cap-video-name'); if(n) n.textContent=this.files.length?this.files[0].name:'No file chosen';">
          <div class="hint">Any MP4 / MOV / WebM with speech. The exact same word-by-word green-box captions are burned on. Picture is re-encoded once at high quality; the original audio is kept.</div>
        </div>
        <div class="panel">
          <label>Words per caption</label>
          <select name="caption_max_words">
            <option value="1" selected>1 word at a time (viral karaoke)</option>
            <option value="2">2 words</option>
            <option value="3">3 words</option>
          </select>
          <div class="hint">1 word is the reference "dark facts" look. Transcription runs fully locally (faster-whisper) &mdash; no API keys needed.</div>
        </div>
        <div class="panel">
          <label>Caption height</label>
          <select name="caption_center_y">
            <option value="0.60" selected>Lower-middle (default)</option>
            <option value="0.50">Center</option>
            <option value="0.72">Lower third</option>
          </select>
        </div>
        <button type="submit">Add captions</button>
      </section>

      <section class="stack">
        <div class="loaded-media-panel">
          <h2 style="margin-bottom: 12px;">How it works</h2>
          <ol class="hint" style="margin: 0; padding-left: 18px; line-height: 1.9;">
            <li><strong>Audio extract</strong> &mdash; ffmpeg pulls the speech track.</li>
            <li><strong>Local transcription</strong> &mdash; faster-whisper times every word on your machine (no API).</li>
            <li><strong>Same captions</strong> &mdash; the identical animated green-box word-by-word captions used in a normal render.</li>
            <li><strong>One clean encode</strong> &mdash; frames are piped straight to H.264 and your original audio is muxed back.</li>
          </ol>
          <div class="hint" style="margin-top: 16px;">Output lands in <code>projects/_captioned/</code>.</div>
        </div>
      </section>
    </form>
    """
    return page("Caption master", body)


def longform_page():
    try:
        import higgsfield_login
        hf_avail = higgsfield_login.available()
        hf_ready = higgsfield_login.is_ready()
    except Exception:
        hf_avail = hf_ready = False
    body = f"""
    {brand_header()}
    <form method="post" action="/longform-run" enctype="multipart/form-data">
      <section class="stack">
        <div class="panel accent">
          <label>Your script</label>
          <textarea name="script" rows="14" placeholder="Paste your full longform script here..." spellcheck="false"></textarea>
          <div class="hint">Paste the narration script - that's all. The app generates the voiceover
            (Gemini TTS, stitched from parts), transcribes it with exact timestamps, writes one doodle
            image prompt per timestamp (reasoning model), renders every image on your Higgsfield account
            (FLUX.2 Pro, 16:9, 4 in flight), then cuts images + voiceover into the finished video.</div>
        </div>

        <div class="panel">
          <label>Voiceover TTS</label>
          <select name="tts_model">
            <option value="pro" selected>Gemini 2.5 Pro TTS (cleaner)</option>
            <option value="flash">Gemini 2.5 Flash TTS (cheaper)</option>
          </select>
        </div>
        <div class="panel">
          <label>Reasoning model {help_tip("Writes one doodle image prompt per timestamp (STAGE-3 prompt) and verifies the final image set before assembly.")}</label>
          <select name="reasoning_model">
            <option value="anthropic/claude-opus-4.8" selected>Claude Opus 4.8</option>
            <option value="anthropic/claude-fable-5">Claude Fable 5</option>
            <option value="anthropic/claude-sonnet-5">Claude Sonnet 5</option>
            <option value="openai/gpt-5.5">GPT-5.5</option>
            <option value="openai/gpt-5.6-sol">GPT-5.6 Sol</option>
            <option value="openai/gpt-5.6-terra">GPT-5.6 Terra</option>
            <option value="openai/gpt-5.6-luna">GPT-5.6 Luna</option>
            <option value="google/gemini-3.1-pro-preview">Gemini 3.1 Pro</option>
            <option value="google/gemini-3.5-flash">Gemini 3.5 Flash</option>
            <option value="google/gemini-3.1-flash-lite">Gemini 3.1 Flash Lite</option>
          </select>
        </div>

        <div class="panel">
          <label>Higgsfield account {help_tip("Images are rendered on YOUR logged-in Higgsfield account (FLUX.2 Pro, unlimited on your plan) - no API key. Click Connect, a browser window opens, log in to Higgsfield once, and the session is saved for future runs.")}</label>
          <div class="tiktok-connect" id="hf-connect">
            <span class="tt-status {'on' if hf_ready else 'off'}" id="hf-status-dot"></span>
            <span class="tt-text" id="hf-status-text">{'Higgsfield connected' if hf_ready else 'Higgsfield not connected'}</span>
            <button type="button" class="button secondary tt-btn" id="hf-login-btn" onclick="connectHiggsfield()">{'Reconnect' if hf_ready else '&#128279; Connect Higgsfield'}</button>
          </div>
          {('' if hf_avail else '<div class="conn-warn">&#9888; Browser engine missing. Run: pip install playwright &amp;&amp; playwright install chromium</div>')}
        </div>

        <button type="submit">Create longform video</button>
      </section>

      <section class="stack">
        <div class="loaded-media-panel">
          <h2 style="margin-bottom: 12px;">How it works</h2>
          <ol class="hint" style="margin: 0; padding-left: 18px; line-height: 1.9;">
            <li><strong>Voiceover</strong> &mdash; the script is split into sentence-safe parts, spoken with Gemini TTS, then stitched.</li>
            <li><strong>Timestamps</strong> &mdash; faster-whisper + script alignment produce an exact <code>[m:ss.d]</code> transcript.</li>
            <li><strong>Prompts</strong> &mdash; the reasoning model writes one doodle image prompt per timestamp (auto-batched).</li>
            <li><strong>Images</strong> &mdash; FLUX.2 Pro 16:9 on your Higgsfield account, 4 in flight, failed images auto-retried; each file is named with its timestamp + on-screen duration.</li>
            <li><strong>Assembly</strong> &mdash; every image is cut to its exact duration (black frame if one is missing) and muxed with the voiceover. A chime plays when it's done.</li>
          </ol>
          <div class="hint" style="margin-top: 16px;">Output lands in <code>projects/_longform/&lt;slug&gt;/</code>.</div>
        </div>
      </section>
    </form>
    <script>
      (function () {{
        var HF_AVAIL = {str(bool(hf_avail)).lower()};
        var pollTimer = null;
        function render(st) {{
          var dot = document.getElementById("hf-status-dot");
          var txt = document.getElementById("hf-status-text");
          var btn = document.getElementById("hf-login-btn");
          if (st && st.ready) {{
            if (dot) dot.className = "tt-status on";
            if (txt) txt.textContent = "Higgsfield connected";
            if (btn) {{ btn.innerHTML = "Reconnect"; btn.disabled = false; }}
          }} else {{
            if (dot) dot.className = "tt-status off" + (st && st.busy ? " busy" : "");
            if (txt) txt.textContent = st && st.busy
              ? "Log in to Higgsfield in the opened window…"
              : (st && st.error ? ("Login failed: " + st.error) : "Higgsfield not connected");
            if (btn) {{ btn.disabled = !!(st && st.busy); }}
          }}
        }}
        function poll() {{
          fetch("/higgsfield-status").then(function (r) {{ return r.json(); }}).then(function (st) {{
            render(st);
            if (st && st.busy) {{ pollTimer = setTimeout(poll, 2000); }}
            else if (pollTimer) {{ clearTimeout(pollTimer); pollTimer = null; }}
          }}).catch(function () {{}});
        }}
        window.connectHiggsfield = function () {{
          if (HF_AVAIL === false) {{ alert("Browser engine missing — run: pip install playwright && playwright install chromium"); return; }}
          var btn = document.getElementById("hf-login-btn");
          if (btn) btn.disabled = true;
          fetch("/higgsfield-login", {{ method: "POST" }}).then(function (r) {{ return r.json(); }})
            .then(function () {{ poll(); }})
            .catch(function () {{ if (btn) btn.disabled = false; }});
        }};
      }})();
    </script>
    """
    return page("Longform Image Set", body)


def save_upload(file_info, job_id):
    if not file_info or not file_info.get("data") or not file_info.get("filename"):
        return ""
    folder = agent_core.UPLOADS_DIR / job_id
    folder.mkdir(parents=True, exist_ok=True)
    safe_name = Path(file_info["filename"]).name
    path = folder / safe_name
    path.write_bytes(file_info["data"])
    return str(path)


def valid_replace_path_values(raw_paths):
    if not isinstance(raw_paths, list):
        raw_paths = [raw_paths] if raw_paths else []
    selected = []
    for raw_path in raw_paths:
        path = safe_requested_path(raw_path)
        if not path or not is_image_path(path):
            continue
        lower_parts = {part.lower() for part in path.parts}
        if "web images" not in lower_parts:
            continue
        if "rejected" in lower_parts or "replaced" in lower_parts:
            continue
        selected.append(str(path.resolve()))
    return list(dict.fromkeys(selected))


def _make_speech_gate(job_id, cancel_event, approval_event):
    """Blocks the run worker after voiceover generation until the user approves or
    replaces it on the run page (only used when 'Halt after generating speech' is on)."""
    def gate(audio):
        with JOB_LOCK:
            job = JOBS.get(job_id)
            if not job:
                raise RunCancelled("Run cancelled by user.")
            job["status"] = "awaiting_approval"
            job["speech_audio"] = str(audio)
            job["speech_decision"] = None
            job["logs"].append("Voiceover ready — listen, then Approve to continue or Replace the voice.")
            job.setdefault("log_times", []).append(time.time())
        while not approval_event.wait(timeout=0.5):
            if cancel_event.is_set():
                raise RunCancelled("Run cancelled by user.")
        approval_event.clear()
        with JOB_LOCK:
            job = JOBS.get(job_id)
            decision = (job.get("speech_decision") if job else "approve") or "approve"
            chosen_speed = job.get("speech_speed_choice") if job else None
        if decision == "replace":
            raise RunCancelled("Voice replaced — restarting with a new speaker.")
        with JOB_LOCK:
            job = JOBS.get(job_id)
            if job:
                job["status"] = "running"
                job["logs"].append("Voiceover approved — continuing the run.")
                job.setdefault("log_times", []).append(time.time())
        return chosen_speed        # the run re-tempos the voice if this differs
    return gate


def start_job(fields, files):
    job_id = str(int(time.time() * 1000))
    fields = dict(fields)
    audio_path = save_upload(files.get("audio_file"), job_id)
    speaker_image_path = save_upload(files.get("speaker_image_file"), job_id)
    cancel_event = threading.Event()
    replace_lock = threading.Lock()
    media_exclusion_lock = threading.Lock()
    media_exclusions = set()
    replace_requests = (
        valid_replace_path_values(fields.get("initial_replace_media_path", []))
        if fields.get("loaded_project_source")
        else []
    )
    remove_requests = (
        valid_replace_path_values(fields.get("initial_remove_media_path", []))
        if fields.get("loaded_project_source")
        else []
    )
    for path_to_remove in remove_requests:
        try:
            import os
            os.remove(path_to_remove)
        except Exception:
            pass
    approval_event = threading.Event()
    fields["_cancel_event"] = cancel_event
    fields["_replace_lock"] = replace_lock
    fields["_replace_requests"] = replace_requests
    fields["_media_exclusion_lock"] = media_exclusion_lock
    fields["_media_exclusions"] = media_exclusions
    fields["_speech_gate"] = _make_speech_gate(job_id, cancel_event, approval_event)
    restart_fields = {k: v for k, v in fields.items() if not k.startswith("_") and k not in ("audio_path",)}
    if audio_path:
        fields["audio_path"] = audio_path
    if speaker_image_path:
        # A freshly uploaded face wins over a gallery selection.
        fields["speaker_image_path"] = speaker_image_path
    elif fields.get("speaker_image_path") and not is_allowed_speaker_path(fields.get("speaker_image_path")):
        # Drop a gallery path that isn't a real image inside an allowed local folder.
        fields["speaker_image_path"] = ""
    with JOB_LOCK:
        initial_logs = ["Queued."]
        if replace_requests:
            initial_logs.append(f"Queued {len(replace_requests)} initial image replacement request(s) from loaded project media.")
        if speaker_image_path:
            initial_logs.append("Speaker hook image uploaded.")
        JOBS[job_id] = {
            "status": "running",
            "logs": initial_logs,
            "log_times": [time.time()] * len(initial_logs),
            "result": None,
            "error": None,
            "cancel_event": cancel_event,
            "replace_lock": replace_lock,
            "replace_requests": replace_requests,
            "media_exclusion_lock": media_exclusion_lock,
            "media_exclusions": media_exclusions,
            "approval_event": approval_event,
            "restart_fields": restart_fields,
            "project_dir": None,
            "created_at": time.time(),
        }

    def status_cb(message):
        with JOB_LOCK:
            job = JOBS.get(job_id)
            if not job:
                raise RunCancelled("Run cancelled by user.")
            if cancel_event.is_set():
                raise RunCancelled("Run cancelled by user.")
            job["logs"].append(message)
            job.setdefault("log_times", []).append(time.time())
            if isinstance(message, str) and message.startswith("PROJECT_DIR|"):
                job["project_dir"] = message.split("|", 1)[1]

    def worker():
        try:
            status_cb("Started.")
            result = agent_core.run_project(fields, status_cb)
            with JOB_LOCK:
                if cancel_event.is_set():
                    JOBS[job_id]["status"] = "cancelled"
                    JOBS[job_id]["logs"].append("Cancelled.")
                else:
                    JOBS[job_id]["status"] = "done"
                    JOBS[job_id]["result"] = result
        except Exception as exc:
            with JOB_LOCK:
                if cancel_event.is_set() or isinstance(exc, RunCancelled):
                    JOBS[job_id]["status"] = "cancelled"
                    JOBS[job_id]["error"] = None
                    if not JOBS[job_id]["logs"] or JOBS[job_id]["logs"][-1] != "Cancelled.":
                        JOBS[job_id]["logs"].append("Cancelled.")
                else:
                    JOBS[job_id]["status"] = "error"
                    JOBS[job_id]["error"] = f"{exc}\n\n{traceback.format_exc()}"
                    JOBS[job_id]["logs"].append(f"Error: {exc}")

    threading.Thread(target=worker, daemon=True).start()
    return job_id


def start_sfx_job(fields, files):
    job_id = str(int(time.time() * 1000))
    fields = dict(fields)
    video_path = save_upload(files.get("video_file"), job_id)
    reasoning_model = fields.get("reasoning_model") or "anthropic/claude-opus-4.8"
    reasoning_mode = fields.get("reasoning_mode")
    sfx_amount = str(fields.get("sfx_amount", "medium") or "medium").strip().lower()
    if sfx_amount not in ("low", "medium", "high"):
        sfx_amount = "medium"
    cancel_event = threading.Event()
    with JOB_LOCK:
        JOBS[job_id] = {
            "status": "running",
            "logs": ["Queued."],
            "log_times": [time.time()],
            "result": None,
            "error": None,
            "cancel_event": cancel_event,
            "project_dir": None,
            "created_at": time.time(),
            "job_kind": "sfx",
        }
    if not video_path:
        with JOB_LOCK:
            JOBS[job_id]["status"] = "error"
            JOBS[job_id]["error"] = "No video uploaded."
            JOBS[job_id]["logs"].append("Error: no video uploaded.")
        return job_id

    def status_cb(message):
        with JOB_LOCK:
            job = JOBS.get(job_id)
            if not job or cancel_event.is_set():
                raise RunCancelled("Run cancelled by user.")
            job["logs"].append(message)
            job.setdefault("log_times", []).append(time.time())

    def worker():
        try:
            reasoning_modes.set_current_reasoning_mode(reasoning_model, reasoning_mode)
            status_cb("Started.")
            result = sfx_agent.enhance_video_with_sfx(
                video_path, reasoning_model=reasoning_model, status_cb=status_cb,
                sfx_amount=sfx_amount,
            )
            with JOB_LOCK:
                if cancel_event.is_set():
                    JOBS[job_id]["status"] = "cancelled"
                    JOBS[job_id]["logs"].append("Cancelled.")
                else:
                    JOBS[job_id]["status"] = "done"
                    JOBS[job_id]["result"] = result
                    # the enhanced upload now lives in a real project -> timeline editor works
                    if result.get("project_dir"):
                        JOBS[job_id]["project_dir"] = result["project_dir"]
        except Exception as exc:
            with JOB_LOCK:
                if cancel_event.is_set() or isinstance(exc, RunCancelled):
                    JOBS[job_id]["status"] = "cancelled"
                    JOBS[job_id]["logs"].append("Cancelled.")
                else:
                    JOBS[job_id]["status"] = "error"
                    JOBS[job_id]["error"] = f"{exc}\n\n{traceback.format_exc()}"
                    JOBS[job_id]["logs"].append(f"Error: {exc}")

    threading.Thread(target=worker, daemon=True).start()
    return job_id


def start_redo_sfx_job(slug, reasoning_model=None, reasoning_mode=None, sfx_amount="medium", mode="redo", regen_captions=False):
    """Timeline "Redo SFX": run a NORMAL SFX-Master pass over the project's LATEST render
    (no upload). Same engine as /sfx-run - the multimodal Audio Director watches the render.
    ``regen_captions`` re-aligns captions FIRST (so a combined "redo captions + redo SFX" rework
    produces ONE render with both)."""
    job_id = str(int(time.time() * 1000))
    project_dir = safe_project_dir(slug)
    render = latest_media(project_dir / "renders", {".mp4", ".webm"}) if project_dir else None
    mode = "add" if str(mode).lower() == "add" else "redo"
    reasoning_model = (reasoning_model or "anthropic/claude-opus-4.8").strip()
    sfx_amount = str(sfx_amount or "medium").strip().lower()
    if sfx_amount not in ("low", "medium", "high"):
        sfx_amount = "medium"
    cancel_event = threading.Event()
    with JOB_LOCK:
        JOBS[job_id] = {
            "status": "running", "logs": ["Queued."], "log_times": [time.time()],
            "result": None, "error": None, "cancel_event": cancel_event,
            "project_dir": None, "created_at": time.time(), "job_kind": "sfx",
        }
    if not render:
        with JOB_LOCK:
            JOBS[job_id]["status"] = "error"
            JOBS[job_id]["error"] = "This project has no render yet - render once first."
            JOBS[job_id]["logs"].append("Error: no render found.")
        return job_id

    def status_cb(message):
        with JOB_LOCK:
            job = JOBS.get(job_id)
            if not job or cancel_event.is_set():
                raise RunCancelled("Run cancelled by user.")
            job["logs"].append(message)
            job.setdefault("log_times", []).append(time.time())

    def worker():
        try:
            reasoning_modes.set_current_reasoning_mode(reasoning_model, reasoning_mode)
            if regen_captions:
                _regenerate_project_captions(slug, status_cb=status_cb)
            status_cb(f"{'Add more' if mode == 'add' else 'Redo'} SFX on {Path(render).name}...")
            result = agent_core.rework_project_sfx(
                slug, mode=mode, reasoning_model=reasoning_model, status_cb=status_cb,
                sfx_amount=sfx_amount, cancel_event=cancel_event)
            with JOB_LOCK:
                if cancel_event.is_set():
                    JOBS[job_id]["status"] = "cancelled"
                    JOBS[job_id]["logs"].append("Cancelled.")
                else:
                    JOBS[job_id]["status"] = "done"
                    JOBS[job_id]["result"] = result
                    JOBS[job_id]["project_dir"] = result.get("project_dir") or str(project_dir)
        except Exception as exc:
            with JOB_LOCK:
                if cancel_event.is_set() or isinstance(exc, RunCancelled):
                    JOBS[job_id]["status"] = "cancelled"
                    JOBS[job_id]["logs"].append("Cancelled.")
                else:
                    JOBS[job_id]["status"] = "error"
                    JOBS[job_id]["error"] = f"{exc}\n\n{traceback.format_exc()}"
                    JOBS[job_id]["logs"].append(f"Error: {exc}")

    threading.Thread(target=worker, daemon=True).start()
    return job_id


def start_visual_job(fields, files):
    job_id = str(int(time.time() * 1000))
    fields = dict(fields)
    video_path = save_upload(files.get("video_file"), job_id)
    reasoning_model = fields.get("reasoning_model") or "anthropic/claude-opus-4.8"
    reasoning_mode = fields.get("reasoning_mode")
    add_characters = str(fields.get("add_characters", "")).lower() in ("on", "true", "1", "yes")
    vfx_amount = str(fields.get("vfx_amount", "medium") or "medium").strip().lower()
    if vfx_amount not in ("low", "medium", "high"):
        vfx_amount = "medium"
    cancel_event = threading.Event()
    with JOB_LOCK:
        JOBS[job_id] = {
            "status": "running", "logs": ["Queued."], "log_times": [time.time()],
            "result": None, "error": None, "cancel_event": cancel_event,
            "project_dir": None, "created_at": time.time(), "job_kind": "visual",
        }
    if not video_path:
        with JOB_LOCK:
            JOBS[job_id]["status"] = "error"
            JOBS[job_id]["error"] = "No video uploaded."
            JOBS[job_id]["logs"].append("Error: no video uploaded.")
        return job_id

    def status_cb(message):
        with JOB_LOCK:
            job = JOBS.get(job_id)
            if not job or cancel_event.is_set():
                raise RunCancelled("Run cancelled by user.")
            job["logs"].append(message)
            job.setdefault("log_times", []).append(time.time())

    def worker():
        try:
            reasoning_modes.set_current_reasoning_mode(reasoning_model, reasoning_mode)
            status_cb("Started.")
            result = visual_agent.enhance_video_with_arrows(
                video_path, reasoning_model=reasoning_model, status_cb=status_cb,
                add_characters=add_characters, vfx_amount=vfx_amount)
            with JOB_LOCK:
                if cancel_event.is_set():
                    JOBS[job_id]["status"] = "cancelled"
                    JOBS[job_id]["logs"].append("Cancelled.")
                else:
                    JOBS[job_id]["status"] = "done"
                    JOBS[job_id]["result"] = result
        except Exception as exc:
            with JOB_LOCK:
                if cancel_event.is_set() or isinstance(exc, RunCancelled):
                    JOBS[job_id]["status"] = "cancelled"
                    JOBS[job_id]["logs"].append("Cancelled.")
                else:
                    JOBS[job_id]["status"] = "error"
                    JOBS[job_id]["error"] = f"{exc}\n\n{traceback.format_exc()}"
                    JOBS[job_id]["logs"].append(f"Error: {exc}")

    threading.Thread(target=worker, daemon=True).start()
    return job_id


def start_viraltrans_job(fields):
    job_id = str(int(time.time() * 1000))
    raw_topic = fields.get("topic")
    topic = str(raw_topic[0] if isinstance(raw_topic, list) and raw_topic else raw_topic or "").strip()
    cancel_event = threading.Event()
    with JOB_LOCK:
        JOBS[job_id] = {
            "status": "running", "logs": ["Queued."], "log_times": [time.time()],
            "result": None, "error": None, "cancel_event": cancel_event,
            "project_dir": None, "created_at": time.time(), "job_kind": "viraltrans",
        }
    if not topic:
        with JOB_LOCK:
            JOBS[job_id]["status"] = "error"
            JOBS[job_id]["error"] = "Choose or enter a topic first."
            JOBS[job_id]["logs"].append("Error: no topic chosen.")
        return job_id

    def status_cb(message):
        with JOB_LOCK:
            job = JOBS.get(job_id)
            if not job or cancel_event.is_set():
                raise RunCancelled("Run cancelled by user.")
            job["logs"].append(message)
            job.setdefault("log_times", []).append(time.time())

    def worker():
        try:
            status_cb("Started.")
            result = viral_transformation.run_transformation_job(
                topic, status_cb=status_cb, cancel_event=cancel_event)
            with JOB_LOCK:
                if cancel_event.is_set():
                    JOBS[job_id]["status"] = "cancelled"
                    JOBS[job_id]["logs"].append("Cancelled.")
                else:
                    JOBS[job_id]["status"] = "done"
                    JOBS[job_id]["result"] = result
                    JOBS[job_id]["project_dir"] = result.get("project")
        except Exception as exc:
            with JOB_LOCK:
                if cancel_event.is_set() or isinstance(exc, RunCancelled):
                    JOBS[job_id]["status"] = "cancelled"
                    JOBS[job_id]["logs"].append("Cancelled.")
                else:
                    JOBS[job_id]["status"] = "error"
                    JOBS[job_id]["error"] = f"{exc}\n\n{traceback.format_exc()}"
                    JOBS[job_id]["logs"].append(f"Error: {exc}")

    threading.Thread(target=worker, daemon=True).start()
    return job_id


def start_caption_job(fields, files):
    job_id = str(int(time.time() * 1000))
    fields = dict(fields)
    video_path = save_upload(files.get("video_file"), job_id)
    try:
        max_words = int(fields.get("caption_max_words", 1) or 1)
    except (TypeError, ValueError):
        max_words = 1
    try:
        center_y = float(fields.get("caption_center_y", 0.60) or 0.60)
    except (TypeError, ValueError):
        center_y = 0.60
    cancel_event = threading.Event()
    with JOB_LOCK:
        JOBS[job_id] = {
            "status": "running",
            "logs": ["Queued."],
            "log_times": [time.time()],
            "result": None,
            "error": None,
            "cancel_event": cancel_event,
            "project_dir": None,
            "created_at": time.time(),
            "job_kind": "caption",
        }
    if not video_path:
        with JOB_LOCK:
            JOBS[job_id]["status"] = "error"
            JOBS[job_id]["error"] = "No video uploaded."
            JOBS[job_id]["logs"].append("Error: no video uploaded.")
        return job_id

    def status_cb(message):
        with JOB_LOCK:
            job = JOBS.get(job_id)
            if not job or cancel_event.is_set():
                raise RunCancelled("Run cancelled by user.")
            job["logs"].append(message)
            job.setdefault("log_times", []).append(time.time())

    def worker():
        try:
            status_cb("Started.")
            result = caption_agent.caption_video(
                video_path, max_words=max_words, caption_center_y=center_y,
                status_cb=status_cb, cancel_event=cancel_event,
            )
            with JOB_LOCK:
                if cancel_event.is_set():
                    JOBS[job_id]["status"] = "cancelled"
                    JOBS[job_id]["logs"].append("Cancelled.")
                else:
                    JOBS[job_id]["status"] = "done"
                    JOBS[job_id]["result"] = result
        except Exception as exc:
            with JOB_LOCK:
                if cancel_event.is_set() or isinstance(exc, RunCancelled):
                    JOBS[job_id]["status"] = "cancelled"
                    JOBS[job_id]["logs"].append("Cancelled.")
                else:
                    JOBS[job_id]["status"] = "error"
                    JOBS[job_id]["error"] = f"{exc}\n\n{traceback.format_exc()}"
                    JOBS[job_id]["logs"].append(f"Error: {exc}")

    threading.Thread(target=worker, daemon=True).start()
    return job_id


def start_longform_job(fields, files):
    """Longform Image Set: parse an uploaded prompt .txt (one '[m:ss] prompt' per line) and
    generate one 16:9 FLUX.2 Pro image per line on the user's logged-in Higgsfield account,
    saving each finished image named by its timestamp (e.g. '[0-06].png')."""
    import higgsfield_login
    job_id = str(int(time.time() * 1000))
    fields = dict(fields)
    txt_path = save_upload(files.get("prompt_file"), job_id)
    model = (fields.get("model") or higgsfield_login.DEFAULT_MODEL).strip()
    aspect = (fields.get("aspect") or higgsfield_login.DEFAULT_ASPECT).strip()
    try:
        concurrency = max(1, min(4, int(fields.get("concurrency", 4) or 4)))
    except (TypeError, ValueError):
        concurrency = 4
    cancel_event = threading.Event()
    with JOB_LOCK:
        JOBS[job_id] = {
            "status": "running",
            "logs": ["Queued."],
            "log_times": [time.time()],
            "result": None,
            "error": None,
            "cancel_event": cancel_event,
            "project_dir": None,
            "created_at": time.time(),
            "job_kind": "longform",
        }

    def fail(msg):
        with JOB_LOCK:
            JOBS[job_id]["status"] = "error"
            JOBS[job_id]["error"] = msg
            JOBS[job_id]["logs"].append(f"Error: {msg}")
        return job_id

    if not txt_path:
        return fail("No prompt .txt uploaded.")
    try:
        text = Path(txt_path).read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return fail(f"Could not read the prompt file: {exc}")
    items = higgsfield_login.parse_prompt_lines(text)
    if not items:
        return fail("No prompts found - each line must start with a timestamp like [0:06].")
    # keep filename stems unique within the run (duplicate timestamps would otherwise overwrite)
    seen = {}
    for it in items:
        key = it["key"]
        if key in seen:
            seen[key] += 1
            it["key"] = f"{key}_{seen[key]}"
        else:
            seen[key] = 1
    if not higgsfield_login.is_ready():
        return fail("Higgsfield is not connected. Open the Longform page and click Connect Higgsfield first.")

    stem = re.sub(r"[^A-Za-z0-9_-]+", "_", Path(txt_path).stem).strip("_") or "longform"
    out_dir = agent_core.PROJECTS_DIR / "_longform" / stem

    def status_cb(message):
        with JOB_LOCK:
            job = JOBS.get(job_id)
            if not job or cancel_event.is_set():
                raise RunCancelled("Run cancelled by user.")
            job["logs"].append(message)
            job.setdefault("log_times", []).append(time.time())

    def worker():
        try:
            status_cb(f"Parsed {len(items)} prompt(s). Output -> {out_dir}")
            status_cb("Opening your Higgsfield session (window stays hidden)...")
            results = higgsfield_login.generate_batch(
                items, out_dir, concurrency=concurrency, aspect=aspect, model=model, ext="png",
                status_cb=status_cb, cancel_check=cancel_event.is_set)
            ok = [r for r in results if r.get("path")]
            with JOB_LOCK:
                if cancel_event.is_set():
                    JOBS[job_id]["status"] = "cancelled"
                    JOBS[job_id]["logs"].append("Cancelled.")
                else:
                    JOBS[job_id]["status"] = "done"
                    JOBS[job_id]["logs"].append(
                        f"Done. {len(ok)}/{len(items)} image(s) saved to {out_dir}.")
                    JOBS[job_id]["result"] = {"project_dir": str(out_dir),
                                              "images_done": len(ok), "images_total": len(items)}
        except Exception as exc:
            with JOB_LOCK:
                if cancel_event.is_set() or isinstance(exc, RunCancelled):
                    JOBS[job_id]["status"] = "cancelled"
                    JOBS[job_id]["logs"].append("Cancelled.")
                else:
                    JOBS[job_id]["status"] = "error"
                    JOBS[job_id]["error"] = f"{exc}\n\n{traceback.format_exc()}"
                    JOBS[job_id]["logs"].append(f"Error: {exc}")

    threading.Thread(target=worker, daemon=True).start()
    return job_id


def start_longform_video_job(fields):
    """Longform VIDEO: paste a script -> voiceover (Gemini TTS) -> exact-timestamp transcript
    (faster-whisper + alignment) -> one doodle prompt per timestamp (reasoning model) ->
    FLUX.2 Pro 16:9 images on Higgsfield (4 in flight, retries) -> images + voiceover cut into
    the finished MP4 (black frame where an image is missing). See longform_video.py."""
    import longform_video
    job_id = str(int(time.time() * 1000))
    script = str(fields.get("script") or "").strip()
    tts_model = (fields.get("tts_model") or "pro").strip().lower()
    if tts_model not in ("pro", "flash"):
        tts_model = "pro"
    reasoning_model = (fields.get("reasoning_model") or "anthropic/claude-opus-4.8").strip()
    reasoning_mode = fields.get("reasoning_mode")
    cancel_event = threading.Event()
    with JOB_LOCK:
        JOBS[job_id] = {
            "status": "running",
            "logs": ["Queued."],
            "log_times": [time.time()],
            "result": None,
            "error": None,
            "cancel_event": cancel_event,
            "project_dir": None,
            "created_at": time.time(),
            "job_kind": "longform",
        }

    def fail(msg):
        with JOB_LOCK:
            JOBS[job_id]["status"] = "error"
            JOBS[job_id]["error"] = msg
            JOBS[job_id]["logs"].append(f"Error: {msg}")
        return job_id

    if len(script) < 40:
        return fail("Please paste the full script (at least a few sentences).")
    if not os.environ.get("WAVESPEED_API_KEY"):
        return fail("WAVESPEED_API_KEY missing - the voiceover + image prompts need it.")

    def status_cb(message):
        with JOB_LOCK:
            job = JOBS.get(job_id)
            if not job or cancel_event.is_set():
                raise RunCancelled("Run cancelled by user.")
            job["logs"].append(message)
            job.setdefault("log_times", []).append(time.time())

    def worker():
        try:
            reasoning_modes.set_current_reasoning_mode(reasoning_model, reasoning_mode)
            result = longform_video.run_longform_video(
                script, tts_model=tts_model, reasoning_model=reasoning_model,
                status_cb=status_cb, cancel_event=cancel_event)
            with JOB_LOCK:
                JOBS[job_id]["status"] = "done"
                JOBS[job_id]["result"] = result
                JOBS[job_id]["project_dir"] = result.get("project_dir")
                JOBS[job_id]["logs"].append(
                    f"Done. {result.get('images_done')}/{result.get('images_total')} images; "
                    f"final video: {result.get('video')}")
        except Exception as exc:
            with JOB_LOCK:
                if cancel_event.is_set() or isinstance(exc, (RunCancelled, pipeline.PipelineCancelled)):
                    JOBS[job_id]["status"] = "cancelled"
                    JOBS[job_id]["logs"].append("Cancelled.")
                else:
                    JOBS[job_id]["status"] = "error"
                    JOBS[job_id]["error"] = f"{exc}\n\n{traceback.format_exc()}"
                    JOBS[job_id]["logs"].append(f"Error: {exc}")

    threading.Thread(target=worker, daemon=True).start()
    return job_id


def cancel_job(job_id):
    with JOB_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return False, "missing"
        if job.get("status") not in {"running", "cancelling"}:
            return True, job.get("status", "done")
        event = job.get("cancel_event")
        if event:
            event.set()
        job["status"] = "cancelling"
        if not job["logs"] or "Cancel requested." not in job["logs"][-1]:
            job["logs"].append("Cancel requested. Stopping at the next safe checkpoint.")
        return True, "cancelling"


def queue_media_replacements(job_id, raw_paths):
    selected = valid_replace_path_values(raw_paths)
    if not selected:
        return False, "No valid image files selected."
    with JOB_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return False, "Job not found."
        if job.get("status") not in {"running", "cancelling"}:
            return False, "This job is no longer running."
        lock = job.get("replace_lock")
        requests = job.get("replace_requests")
    if lock and requests is not None:
        with lock:
            existing = set(requests)
            for path in selected:
                if path not in existing:
                    requests.append(path)
                    existing.add(path)
    with JOB_LOCK:
        job = JOBS.get(job_id)
        if job:
            job["logs"].append(f"Queued {len(selected)} image replacement request(s); they will be handled after Seedance generation.")
    return True, f"Queued {len(selected)} image replacement request(s)."


def link_for(path):
    p = Path(path)
    rel = urllib.parse.quote(str(p.resolve()))
    return f"/file?path={rel}"


def speaker_gallery_images():
    """Saved speaker images — ONLY the repo 'speaker images' folder (per user)."""
    out = []
    if SPEAKER_GALLERY_DIR.exists():
        files = [p for p in SPEAKER_GALLERY_DIR.iterdir() if p.is_file() and p.suffix.lower() in SPEAKER_IMAGE_EXTS]
        out = sorted(files, key=lambda x: x.stat().st_mtime, reverse=True)
    return out


def is_allowed_speaker_path(path):
    """True if a gallery-selected speaker path is inside an allowed local folder."""
    try:
        resolved = Path(path).resolve()
    except Exception:
        return False
    if not resolved.is_file() or resolved.suffix.lower() not in SPEAKER_IMAGE_EXTS:
        return False
    allowed = [SPEAKER_GALLERY_DIR.resolve(), (ROOT / "speaker").resolve(), (ROOT / "projects").resolve()]
    return any(str(resolved).startswith(str(base)) for base in allowed)


def speaker_gallery_html(selected=""):
    images = speaker_gallery_images()
    selected_resolved = ""
    try:
        if selected:
            selected_resolved = str(Path(selected).resolve())
    except Exception:
        selected_resolved = ""
    # the upload "+" tile lives inside the gallery (same size as a speaker tile);
    # click opens the file picker, drag-drop a file onto it also works.
    add_tile = (
        '<button type="button" class="speaker-tile speaker-add" id="speaker-add-tile" '
        'title="Upload a new face" aria-label="Upload a new face" '
        'onclick="document.getElementById(\'speaker-file\').click()">'
        '<span class="speaker-add-plus">+</span></button>'
    )
    tiles = [add_tile]
    for p in images:
        rp = str(p.resolve())
        sel = " selected" if selected_resolved and selected_resolved == rp else ""
        tiles.append(
            f'<button type="button" class="speaker-tile{sel}" data-path="{esc(rp)}" '
            f'onclick="selectSpeakerImage(this)" title="{esc(p.stem)}">'
            f'<img src="{link_for(p)}" alt="{esc(p.stem)}" loading="lazy"></button>'
        )
    return '<div class="speaker-gallery">' + "".join(tiles) + "</div>"


def view_for(path, job_id=""):
    p = Path(path)
    query = {"path": str(p.resolve())}
    if job_id:
        query["job"] = job_id
    return "/view?" + urllib.parse.urlencode(query)


def safe_requested_path(raw_path):
    path = Path(urllib.parse.unquote(raw_path or ""))
    root = ROOT.resolve()
    try:
        resolved = path.resolve()
    except Exception:
        return None
    if root not in [resolved, *resolved.parents] or not resolved.exists():
        return None
    return resolved


def is_image_path(path):
    return Path(path).suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}


def is_video_path(path):
    return Path(path).suffix.lower() in {".mp4", ".webm"}


def is_audio_path(path):
    return Path(path).suffix.lower() in {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}


def progress_state(status, logs):
    if status == "done":
        return 100, "Done."
    if status == "error":
        return 100, "Stopped with an error."
    if status == "cancelled":
        return 100, "Cancelled."
    steps = [
        ("Queued.", 2),
        ("Started.", 5),
        # SFX post-production pass (uploaded video -> Opus-planned sound effects)
        ("Loaded video:", 8),
        ("Detecting scene changes", 16),
        ("Transcribing speech with Gemini", 34),
        ("planning sound effects", 52),
        ("sound-effect event", 64),
        ("after spacing/dedup", 72),
        ("under the original audio", 84),
        ("SFX enhancement complete", 99),
        ("Estimated speaking time", 8),
        ("Analyzing audio", 10),
        ("Gemini audio timing", 14),
        ("Visual direction prompt applied", 15),
        ("Auto Director: asking", 18),
        ("Auto Director choices", 24),
        ("Searching general web images", 28),
        ("Searching web images", 30),
        ("general web image query", 31),
        ("Reviewing web images with Reasoning Agent", 36),
        ("Web image correction", 38),
        ("Web image contact sheet", 40),
        ("Planning scenes and media", 45),
        ("Speaker hook:", 47),
        ("Building social search buckets", 50),
        ("searching TikTok", 55),
        ("Social search", 56),
        ("Downloaded accepted candidate", 64),
        ("Scrape semantic threshold", 69),
        ("scene(s) matched across batches", 73),
        ("Generating missing Seedance I2V GPT source images", 50),
        ("GPT image", 56),
        ("Waiting for GPT Image", 60),
        ("GPT image contact sheet", 66),
        ("Generating missing Seedance", 68),
        ("Seedance clip", 72),
        ("Waiting for Seedance", 76),
        ("Skipping Seedance generation", 74),
        ("Reasoning Agent pre-render edit audit starting", 78),
        ("Reasoning Agent pre-render audit", 79),
        ("Rendering final 9:16 MP4", 80),
        ("Rendering frames", 80),
        ("Mixing audio", 93),
        ("Encoding final MP4", 94),
        ("Creating review sheets", 94),
        ("Scene review sheet", 95),
        ("Shot review sheet", 96),
        ("Reasoning Agent review pass 1/2 starting", 93),
        ("Reviewing video with Reasoning Agent", 94),
        ("Reasoning Agent requested correction", 96),
        ("Reasoning Agent review pass 2/2 starting", 96),
        ("Reasoning Agent second review requested correction", 97),
        ("Second corrected scene review sheet", 98),
        ("Second corrected shot review sheet", 98),
        ("Saving audio render variants", 98),
        ("Rendering audio variant", 98),
        ("Cleaning standard MP4 metadata", 99),
        ("Done.", 100),
    ]
    progress = 0
    activity = "Preparing run..."
    for line in logs:
        text = str(line)
        if text.startswith("PREVIEW_IMAGE|"):
            continue
        for marker, value in steps:
            if marker in text:
                progress = max(progress, value)
                activity = text
    for line in reversed(logs):
        text = str(line)
        if text and not text.startswith("PREVIEW_IMAGE|"):
            activity = text
            break
    # Frame rendering is the longest phase; let the bar climb with the real
    # frame percentage (80 -> 92) instead of sitting on a single fixed value.
    if progress < 93:
        for line in reversed(logs):
            match = re.search(r"Rendering frames:\s*(\d+)\s*%", str(line))
            if match:
                progress = max(progress, min(92, 80 + round(int(match.group(1)) * 0.12)))
                break
    activity = re.sub(r"\s+", " ", activity).strip()
    if len(activity) > 150:
        activity = activity[:147].rstrip() + "..."
    return min(progress, 99), activity


def progress_percent(status, logs):
    return progress_state(status, logs)[0]


def format_duration(seconds):
    seconds = max(0, int(seconds or 0))
    minutes, secs = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:d}:{secs:02d}"


# Ordered, high-level run phases shown as button-like step bars. Each phase is
# detected by any of its marker substrings appearing in the live log.
RUN_STEPS = [
    ("Voiceover", ("Generating voiceover", "Voiceover generated", "Reusing existing voiceover", "Generating hook + body")),
    ("Voice timing", ("Aligning script to voice", "Voice timing", "forced alignment", "Analyzing audio", "audio timing")),
    ("Director", ("Auto Director", "Director choices", "Planning scenes", "micro-beat", "edit map", "Estimated speaking")),
    ("Web search", ("Searching web images", "Searching general web images", "Reviewing web images", "Web image", "web image query")),
    ("Images", ("GPT image", "GPT source images", "Waiting for GPT Image", "nano-banana", "GPT Image")),
    ("Video clips", ("Seedance clip", "Generating missing Seedance", "Waiting for Seedance",
                     "InfiniteTalk", "Speaker hook", "Building social search buckets",
                     "Social search", "Downloaded accepted candidate", "Scrape semantic threshold")),
    ("Sound", ("missing sound effects", "Generating any missing sound", "fallback SFX", "background music")),
    ("Render", ("Rendering final", "Rendering frames", "Mixing audio", "Encoding final MP4")),
    ("Review", ("review sheet", "Reviewing video with Reasoning Agent", "review pass", "pre-render audit")),
]

SFX_STEPS = [
    ("Load video", ("Loaded video",)),
    ("Scene cuts", ("Detecting scene changes",)),
    ("Transcribe", ("Transcrib",)),
    ("Plan SFX", ("planning sound effects", "Planned")),
    ("Place SFX", ("sound-effect event", "after spacing/dedup", "Placed")),
    ("Mix", ("under the original audio", "Mixing", "into")),
    ("Finish", ("SFX enhancement complete",)),
]

CAPTION_STEPS = [
    ("Load video", ("Started", "Extracting audio")),
    ("Transcribe", ("Transcrib",)),
    ("Render captions", ("Rendering captions", "Captioning:")),
    ("Finish", ("Captioned video saved", "Done.")),
]


# When the run scrapes real TikTok/X footage there is no web-image search, no AI image
# generation and no post-render review pass, so those step chips just sit dead. Drop them.
SCRAPE_RUN_STEPS = [s for s in RUN_STEPS if s[0] not in ("Web search", "Images", "Review")]
_SCRAPE_LOG_MARKERS = ("scrape v2", "scrape v1", "social search", "searching tiktok",
                       "tiktok search", "building social search", "relevance-first")


def _is_scrape_run(logs):
    blob = " ".join(str(x) for x in (logs or [])[:120]).lower()
    return any(m in blob for m in _SCRAPE_LOG_MARKERS)


def _match_step_index(text, steps):
    for index, (_name, markers) in enumerate(steps):
        if any(marker in text for marker in markers):
            return index
    return -1


def compute_step_view(status, logs, log_times=None, job_kind=None):
    """Return per-step view: [{name, state, elapsed}] for the button-like bars.

    state is one of done / active / stopped / pending. elapsed (seconds) is the
    real per-step duration (from log timestamps) for started steps.
    """
    steps = (SFX_STEPS if job_kind == "sfx"
             else CAPTION_STEPS if job_kind == "caption"
             else SCRAPE_RUN_STEPS if _is_scrape_run(logs) else RUN_STEPS)
    log_times = log_times or []
    start_times = [None] * len(steps)
    active = -1
    for index, line in enumerate(logs):
        text = str(line)
        if text.startswith("PREVIEW_IMAGE|") or text.startswith("PROJECT_DIR|"):
            continue
        step_index = _match_step_index(text, steps)
        if step_index >= 0:
            active = max(active, step_index)
            ts = log_times[index] if index < len(log_times) else None
            if start_times[step_index] is None and ts is not None:
                start_times[step_index] = ts
    now = time.time()
    view = []
    for index, (name, _markers) in enumerate(steps):
        if status == "done":
            state = "done"
        elif status in ("error", "cancelled") and index == active:
            state = "stopped"
        elif index < active:
            state = "done"
        elif index == active and status in ("running", "cancelling"):
            state = "active"
        elif index == active:
            state = "done"
        else:
            state = "pending"
        start = start_times[index]
        elapsed = None
        if start is not None:
            end = next((start_times[j] for j in range(index + 1, len(steps)) if start_times[j] is not None), None)
            if state in ("done", "stopped") and end is not None:
                elapsed = max(0.0, end - start)
            else:
                elapsed = max(0.0, now - start)
        view.append({"name": name, "state": state, "elapsed": elapsed})
    return view


def _render_bar_percent(logs):
    """Real progress % for a render/longform bar, or None if not started yet.
    Uses the latest 'Rendering frames: N%' (ffmpeg), or 'image K/N' for longform."""
    for line in reversed(logs or []):
        m = re.search(r"Rendering frames:\s*(\d+)\s*%", str(line))
        if m:
            return max(0, min(99, int(m.group(1))))
    for line in reversed(logs or []):
        m = re.search(r"(?:image|clip|scene)\s+(\d+)\s*/\s*(\d+)", str(line), re.I)
        if m and int(m.group(2)) > 0:
            return max(0, min(99, round(int(m.group(1)) / int(m.group(2)) * 100)))
    return None


def render_progress(status, logs, created_at=None, log_times=None, job_kind=None):
    _percent, activity = progress_state(status, logs)
    elapsed = format_duration(time.time() - float(created_at or time.time()))
    # Timeline-editor + longform-image runs show ONLY a clean animated bar - no per-step chips
    # (they have no fixed pipeline steps; a longform run is just N image generations).
    if job_kind in ("timeline", "longform") and status not in {"done", "error", "cancelled"}:
        if job_kind == "longform":
            label = "Cancelling&hellip;" if status == "cancelling" else "Generating images&hellip;"
        else:
            label = "Cancelling render&hellip;" if status == "cancelling" else "Rendering your Short&hellip;"
        # DETERMINATE bar driven by the real render percentage (ffmpeg frame % for a render,
        # "image K/N" for longform). Before any percentage is known it stays indeterminate.
        pct = _render_bar_percent(logs)
        determinate = pct is not None
        fill = pct if determinate else 42
        pct_html = f'<strong class="tl-render-pct">{pct}%</strong>' if determinate else ""
        bar_cls = "det" if determinate else "indet"
        return f"""
    <div id="job-progress-wrap" class="progress-wrap tl-render-progress">
      <div class="tl-render-title">{label} {pct_html}</div>
      <div class="tl-render-bar {bar_cls}" role="progressbar" aria-valuenow="{fill}" aria-valuemin="0" aria-valuemax="100">
        <span style="width:{fill}%"></span></div>
      <div class="tl-render-sub"><span>{esc(activity)}</span><strong>{esc(elapsed)}</strong></div>
    </div>
    """
    # Terminal-state longform view: a clean summary, never the run-pipeline step chips.
    if job_kind == "longform":
        title = ("Images generated" if status == "done"
                 else "Cancelled" if status == "cancelled" else "Stopped with an error")
        return f"""
    <div id="job-progress-wrap" class="progress-wrap">
      <div class="elapsed-line"><span>{title}</span><strong>{esc(elapsed)}</strong></div>
      <div class="progress-current">{esc(activity)}</div>
    </div>
    """
    steps = compute_step_view(status, logs, log_times=log_times, job_kind=job_kind)
    chips = []
    for step in steps:
        seconds = step["elapsed"]
        time_html = f'<span class="step-time">{esc(format_duration(seconds))}</span>' if seconds is not None else ""
        delay = f"{seconds:.1f}" if (step["state"] == "active" and seconds is not None) else "0"
        chips.append(
            f'<div class="step-chip {step["state"]}" style="--el:{delay}s">'
            f'<span class="step-fill"></span>'
            f'<span class="step-name">{esc(step["name"])}</span>{time_html}'
            f'</div>'
        )
    steps_html = '<div class="steps-strip">' + "".join(chips) + "</div>"
    return f"""
    <div id="job-progress-wrap" class="progress-wrap">
      <div class="elapsed-line"><span>Total time elapsed</span><strong>{esc(elapsed)}</strong></div>
      {steps_html}
      <div class="progress-current">{esc(activity)}</div>
    </div>
    """


def render_previews(logs, result=None):
    previews = []
    seen = set()
    for line in logs:
        if not isinstance(line, str) or not line.startswith("PREVIEW_IMAGE|"):
            continue
        parts = line.split("|", 2)
        if len(parts) != 3:
            continue
        label, path = parts[1], parts[2]
        if path in seen or not Path(path).exists():
            continue
        seen.add(path)
        previews.append((label, path))
    if result:
        for label, key in [("Web images", "web_contact_sheet"), ("GPT source images", "gpt_contact_sheet"), ("Scene review", "scene_review"), ("Shot review", "shot_review")]:
            path = result.get(key)
            if path and path not in seen and Path(path).exists() and is_image_path(path):
                seen.add(path)
                previews.append((label, path))
    if not previews:
        return ""
    cards = []
    for label, path in previews:
        cards.append(
            f'<div class="panel preview"><strong>{esc(label)}</strong>'
            f'<button class="preview-button" type="button" data-preview-src="{link_for(path)}" data-preview-title="{esc(label)}">'
            f'<img src="{link_for(path)}" alt="{esc(label)}"></button></div>'
        )
    return '<section><h2>Visual Previews</h2><div class="preview-grid">' + "".join(cards) + "</div></section>"


def project_dir_for_job(job):
    result = job.get("result") or {}
    if result.get("project_dir") and Path(result["project_dir"]).exists():
        return Path(result["project_dir"])
    if job.get("project_dir") and Path(job["project_dir"]).exists():
        return Path(job["project_dir"])
    for line in job.get("logs", []):
        if isinstance(line, str) and line.startswith("PROJECT_DIR|"):
            path = Path(line.split("|", 1)[1])
            if path.exists():
                return path
    return None


def project_web_manifest_by_name(project_dir):
    manifest_path = project_dir / "web images" / "web_image_manifest.json"
    if not manifest_path.exists():
        return {}
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return {}
    result = {}
    for item in data if isinstance(data, list) else []:
        if isinstance(item, dict) and item.get("local_path"):
            result[Path(item["local_path"]).name.lower()] = item
    return result


def project_social_sources_by_name(project_dir):
    config = read_json_file(Path(project_dir) / "config" / "project.json")
    result = {}
    for scene in config.get("scenes", []) if isinstance(config, dict) else []:
        if not isinstance(scene, dict) or not scene.get("clip"):
            continue
        source = str(scene.get("scrape_source") or "tiktok").strip().lower()
        result[Path(str(scene["clip"])).name.lower()] = "twitter" if source in {"x", "twitter"} else "tiktok"
    return result


def media_kind_for_path(project_dir, path, manifest=None, social_sources=None):
    rel_parts = [part.lower() for part in Path(path).relative_to(project_dir).parts]
    if "web images" in rel_parts:
        item = (manifest or {}).get(path.name.lower(), {})
        provider = str(item.get("provider") or "").lower()
        source_url = str(item.get("source_url") or "").lower()
        if provider == "wikimedia" or "wikimedia.org" in source_url or "wikipedia.org" in source_url:
            return "wikimedia"
        if "rejected" in rel_parts:
            return "web rejected"
        if "replaced" in rel_parts:
            return "web replaced"
        return "web"
    if "seedance 2.0" in rel_parts:
        name = Path(path).name.lower()
        # scraped_NN = scene-assigned scrape clips; manual_ = declined clips the user
        # accepted by hand - both are real TikTok footage, not AI (seedance) clips.
        if name.startswith("scraped"):
            return "assigned"
        if name.startswith("manual_"):
            return "accepted_tiktok"
        return "seedance"
    if "gpt images" in rel_parts:
        return "gpt source"
    if "speaker" in rel_parts or "speaker clip" in rel_parts:
        return "speaker"
    if "renders" in rel_parts:
        return "render"
    if "review" in rel_parts:
        return "review"
    if "local media" in rel_parts:
        return "local"
    return "media"


def project_media_files(project_dir):
    if not project_dir:
        return []
    manifest = project_web_manifest_by_name(project_dir)
    social_sources = project_social_sources_by_name(project_dir)
    items = []
    folders = ["web images", "seedance 2.0", "gpt images", "speaker", "speaker clip", "renders", "review", "local media"]
    suffixes = {".jpg", ".jpeg", ".png", ".webp", ".mp4", ".webm", ".mov", ".m4v", ".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}
    for folder_name in folders:
        folder = project_dir / folder_name
        if not folder.exists():
            continue
        for path in sorted(folder.rglob("*"), key=lambda item: item.stat().st_mtime if item.exists() and item.is_file() else 0, reverse=True):
            if not path.is_file() or path.suffix.lower() not in suffixes:
                continue
            lower_parts = {p.lower() for p in path.parts}
            if "_raw" in lower_parts or "_manual_removed" in lower_parts:
                continue
            if "_candidates" in lower_parts:
                if not is_video_path(path):
                    continue
                meta = read_json_file(path.with_suffix(".json"))
                platform = str(meta.get("platform") or "tiktok").strip().lower()
                items.append(("accepted_twitter" if platform in {"x", "twitter"}
                              else "accepted_tiktok", path))
                continue
            # rejected-but-watchable clips: shown in the media panel with a checkmark so the
            # user can review and manually accept them into the project
            if "_declined" in lower_parts:
                items.append(("declined", path))
                continue
            # every other underscore-prefixed folder/file holds INTERNAL working artifacts
            # (review/_clip_match frames, _hook_match, _fx_frames, contact sheets, _debug...)
            # - hundreds of JPGs that are not project media and bloated the panel.
            rel_parts = Path(path).relative_to(project_dir).parts
            if any(p.startswith("_") for p in rel_parts):
                continue
            # derived poster thumbnails (clip.poster.jpg) are not real draggable media
            if path.name.lower().endswith(".poster.jpg"):
                continue
            items.append((media_kind_for_path(project_dir, path, manifest, social_sources), path))
    return items


def replaceable_media_files(project_dir):
    items = []
    for kind, path in project_media_files(project_dir):
        if kind not in {"web", "wikimedia"} or not is_image_path(path):
            continue
        lower_parts = {part.lower() for part in path.parts}
        if "rejected" in lower_parts or "replaced" in lower_parts:
            continue
        items.append((kind, path))
    return items


def queued_replacement_paths(job):
    requests = job.get("replace_requests")
    lock = job.get("replace_lock")
    if lock and requests is not None:
        with lock:
            return {str(Path(path).resolve()).lower() for path in requests}
    return set()


def media_preview_markup(kind, path, replaceable=False, queued=False, input_name="media_path",
                         removal_job_id=""):
    resolved = str(path.resolve())
    label = "Replace" if replaceable else kind.title()
    if is_video_path(path):
        # lazy: no eager src -> the LazyVideo observer attaches it only when scrolled into
        # view (a page can hold 100+ clips; loading them all crashes the browser renderer)
        poster = path.with_suffix(".poster.jpg")
        poster_attr = f' poster="{link_for(poster)}"' if poster.exists() else ""
        preview = f'<video data-lazy-src="{link_for(path)}"{poster_attr} muted preload="none" playsinline></video>'
    elif is_image_path(path):
        preview = f'<img loading="lazy" src="{link_for(path)}" alt="{esc(path.name)}">'
    elif is_audio_path(path):
        preview = '<div class="media-audio-tile">Audio</div>'
    else:
        preview = '<div class="media-audio-tile">File</div>'
    replace_html = (
        f'<label><input type="checkbox" name="{esc(input_name)}" value="{esc(resolved)}"{" checked" if queued else ""}> {esc(label)}</label>'
        if replaceable
        else f'<label>{esc(label)}</label>'
    )
    tile_cls = " queued" if queued else ""
    accept_html = ""
    if kind == "declined":
        tile_cls += " declined-tile"
        accept_html = (f'<button class="media-accept" type="button" title="Accept this clip into the project" '
                       f'data-accept-path="{esc(resolved)}">&#10003;</button>')
    remove_html = ""
    if removal_job_id and kind in {"accepted_tiktok", "accepted_twitter", "assigned"}:
        remove_html = (f'<button class="media-del media-remove" type="button" title="Remove from this run" '
                       f'data-remove-path="{esc(resolved)}" data-remove-job="{esc(removal_job_id)}" '
                       f'aria-label="Remove from this run">&times;</button>')
    return (
        f'<article class="media-tile{tile_cls}">'
        f'{accept_html}{remove_html}'
        f'<span class="media-kind">{esc(kind)}</span>'
        f'<button class="preview-button" type="button" data-preview-src="{link_for(path)}" data-preview-title="{esc(path.name)}">'
        f'{preview}</button>'
        f'{replace_html}'
        f'<small>{esc(path.name)}</small>'
        f'</article>'
    )


def media_tabs_html(items, replaceable_paths=None, queued_paths=None, input_name="media_path",
                    removal_job_id=""):
    replaceable_paths = replaceable_paths or set()
    queued_paths = queued_paths or set()
    groups = {}
    for kind, path in items:
        key = str(kind or "media")
        groups.setdefault(key, []).append(path)
    if not groups:
        return '<div class="hint">No media files found.</div>'
    # footage first, then supporting assets, then renders; declined and internals last
    order = ["accepted_tiktok", "accepted_twitter", "assigned", "tiktok", "twitter",
             "seedance", "web", "wikimedia", "gpt source", "speaker", "local",
             "render", "declined", "review", "web rejected", "web replaced", "media"]
    labels = {"accepted_tiktok": "TikTok accepted", "accepted_twitter": "X accepted",
              "assigned": "Assigned", "tiktok": "TikTok clips", "twitter": "X clips",
              "seedance": "AI clips", "web": "Web images",
              "wikimedia": "Wikimedia", "gpt source": "AI images", "speaker": "Speaker",
              "local": "Local", "render": "Renders", "declined": "Declined",
              "review": "Review", "web rejected": "Web rejected",
              "web replaced": "Web replaced", "media": "Other"}
    keys = [key for key in order if key in groups] + sorted([key for key in groups if key not in order])
    tabs = []
    panels = []
    for index, key in enumerate(keys):
        tab_id = re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_") or f"tab_{index}"
        active = " active" if index == 0 else ""
        tabs.append(f'<button class="media-tab{active}" type="button" data-media-tab="{esc(tab_id)}">{esc(labels.get(key, key))} <span>{len(groups[key])}</span></button>')
        cards = []
        for path in groups[key]:
            resolved_key = str(path.resolve()).lower()
            cards.append(
                media_preview_markup(
                    key,
                    path,
                    replaceable=resolved_key in replaceable_paths,
                    queued=resolved_key in queued_paths,
                    input_name=input_name,
                    removal_job_id=removal_job_id,
                )
            )
        panels.append(
            f'<div class="media-tab-panel" data-media-panel="{esc(tab_id)}"{" hidden" if index else ""}>'
            f'<div class="media-tiny-grid">{"".join(cards)}</div></div>'
        )
    return f'<div class="media-tabs">{"".join(tabs)}</div>' + "".join(panels)


def scrape_progress_html(project_dir, job):
    """Compact, live search-only progress shown above the job's media gallery."""
    project_dir = Path(project_dir)
    run_form = read_json_file(project_dir / "input" / "run_form.json")
    logs = [str(line) for line in (job.get("logs") or [])]
    scrape_markers = ("Building social search buckets", "Social search ",
                      "Downloaded accepted candidate", "Scrape semantic threshold")
    is_scrape = (str(run_form.get("clip_source") or "").lower() == "scrape"
                 or any(any(marker in line for marker in scrape_markers) for line in logs))
    if not is_scrape:
        return ""

    candidate_root = project_dir / "seedance 2.0" / "_candidates"
    declined_root = project_dir / "seedance 2.0" / "_declined"
    try:
        candidate_files = list(candidate_root.rglob("*.mp4")) if candidate_root.exists() else []
        usable = sum(1 for path in candidate_files
                     if "_raw" not in {part.lower() for part in path.parts})
        raw = sum(1 for path in candidate_root.rglob("*")
                  if path.is_file() and "_raw" in {part.lower() for part in path.parts}) \
            if candidate_root.exists() else 0
        declined = len(list(declined_root.glob("*.mp4"))) if declined_root.exists() else 0
        assigned = len(list((project_dir / "seedance 2.0").glob("scraped_*.mp4")))
    except OSError:
        usable = raw = declined = assigned = 0

    total_buckets = 0
    completed_buckets = set()
    current_status = "Preparing social search..."
    relevant = []
    for line in logs:
        planned = re.search(r"Built\s+(\d+)\s+social search bucket", line)
        if planned:
            total_buckets = max(total_buckets, int(planned.group(1)) + 1)  # + dedicated hook
        finished = re.search(r"Bucket\s+([^:]+):\s+final candidate pool", line)
        if finished:
            completed_buckets.add(finished.group(1).strip())
        if ("searching " in line.lower() or line.startswith("Social search ")
                or "Downloaded accepted candidate" in line or line.startswith("Bucket ")):
            relevant.append(line)

    search_complete = any(
        marker in line for line in logs
        for marker in ("Scrape semantic threshold", "Hook Finder: scoring",
                       "Scrape: used ", "Scrape: no usable")
    )
    status = str(job.get("status") or "running")
    stopped = status in {"error", "cancelled", "cancelling"} and not search_complete
    if search_complete:
        percent = 100
        current_status = "Search complete - matching and arranging footage."
        css_state = "done"
    elif stopped:
        base = (len(completed_buckets) / total_buckets * 100) if total_buckets else 0
        percent = max(2, min(99, round(base)))
        current_status = "Search stopped before completion."
        css_state = "stopped"
    else:
        active_fraction = 0.35 if relevant else 0.08
        if total_buckets:
            percent = min(96, max(3, round(
                (len(completed_buckets) + active_fraction) / total_buckets * 100)))
        else:
            percent = 8 if not relevant else min(85, 12 + usable)
        if relevant:
            current_status = re.sub(r"\s+", " ", relevant[-1]).strip()
        css_state = "running"
    if len(current_status) > 125:
        current_status = current_status[:122].rstrip() + "..."

    bucket_count = (f"<span>{len(completed_buckets)} / {total_buckets} buckets</span>"
                    if total_buckets else "")
    return (
        f'<div class="scrape-mini-progress {css_state}">'
        f'<div class="scrape-mini-head"><span>Search / scrape</span><strong>{percent}%</strong></div>'
        f'<div class="scrape-mini-track"><div class="scrape-mini-fill" style="width:{percent}%"></div></div>'
        f'<div class="scrape-mini-status" title="{esc(current_status)}">{esc(current_status)}</div>'
        f'<div class="scrape-mini-counts">{bucket_count}<span>{usable} usable</span>'
        f'<span>{declined} declined</span><span>{raw} processing</span>'
        f'<span>{assigned} assigned</span></div></div>'
    )


def render_media_replacer(job_id, job):
    # Read-only live gallery. The agent auto-checks each asset for topic relevance
    # and fit, so there are no manual replace/remove controls.
    project_dir = project_dir_for_job(job)
    if not project_dir:
        return '<section class="panel media-sidebar"><h2>Project media</h2><div class="hint">Media appears here once the project folder is created.</div></section>'
    scrape_progress = scrape_progress_html(project_dir, job)
    media_all = project_media_files(project_dir)
    if not media_all:
        return (f'<section class="panel media-sidebar"><div class="panel-head"><h2>Project media</h2></div>'
                f'{scrape_progress}<div class="hint">Media appears here as soon as files exist.</div></section>')
    tabs = media_tabs_html(media_all, removal_job_id=job_id)
    return f"""
    <section class="panel media-sidebar">
      <div class="panel-head"><h2>Project media</h2></div>
      {scrape_progress}
      <div class="hint">Accepted footage is grouped by platform. Assigned clips are the current scene choices. Use &times; to exclude a bad accepted or assigned clip from this run.</div>
      {tabs}
    </section>
    """


def project_media_payload(project_dir):
    media = []
    replaceable = {str(path.resolve()).lower() for _, path in replaceable_media_files(project_dir)}
    for kind, path in project_media_files(project_dir):
        media.append(
            {
                "kind": kind,
                "name": path.name,
                "path": str(path.resolve()),
                "url": link_for(path),
                "replaceable": str(path.resolve()).lower() in replaceable,
                "type": "video" if is_video_path(path) else "image" if is_image_path(path) else "audio" if is_audio_path(path) else "file",
            }
        )
    return {
        "slug": project_dir.name,
        "title": project_title_from_files(project_dir),
        "media": media,
    }


def visible_log_text(logs, limit=600):
    """Render the log to text, capped to the last ``limit`` lines. A long scrape produces
    thousands of lines; the poller re-sets the whole <pre> every second, and reflowing a
    giant element froze the page ("Diese Seite antwortet nicht"). Only the tail matters -
    the box auto-scrolls to the bottom - so send just the last ``limit`` lines."""
    lines = []
    for line in logs:
        if isinstance(line, str) and line.startswith("PREVIEW_IMAGE|"):
            parts = line.split("|", 2)
            if len(parts) == 3:
                lines.append(f"Preview ready: {parts[1]}")
            continue
        if isinstance(line, str) and line.startswith("PROJECT_DIR|"):
            continue
        lines.append(str(line))
    if limit and len(lines) > limit:
        hidden = len(lines) - limit
        lines = [f"... ({hidden} earlier line(s) hidden) ..."] + lines[-limit:]
    return chr(10).join(lines)


def output_card(label, path, job_id, primary=False):
    path_obj = Path(path)
    filename = path_obj.name or str(path_obj)
    preview = ""
    if path_obj.is_dir():
        preview = '<div class="hint">Browse project files and generated media.</div>'
    elif is_video_path(path_obj):
        preview = f'<video class="media-inline" controls preload="metadata" src="{link_for(path_obj)}"></video>'
    elif is_image_path(path_obj):
        preview = (
            f'<button class="preview-button" type="button" data-preview-src="{link_for(path_obj)}" data-preview-title="{esc(label)}">'
            f'<img class="media-inline" src="{link_for(path_obj)}" alt="{esc(label)}"></button>'
        )
    elif is_audio_path(path_obj):
        preview = f'<audio controls src="{link_for(path_obj)}"></audio>'
    classes = "panel output-card"
    if primary:
        classes += " accent"
    return (
        f'<div class="{classes}">'
        f'<strong>{esc(label)}</strong>'
        f'{preview}'
        f'<small>{esc(filename)}</small>'
        f'<div class="output-actions">'
        f'<a class="button" href="{view_for(path_obj, job_id)}">View</a>'
        + ("" if path_obj.is_dir() else f'<a class="button secondary" href="{link_for(path_obj)}">Raw</a>')
        + '</div>'
        f'</div>'
    )


def render_outputs(result, job_id):
    if not result:
        return ""
    items = [
        ("Final video", "video", True),
        ("SFX plan", "sfx_plan", False),
        ("No SFX render", "video_no_sfx", False),
        ("Seedance audio only render", "video_seedance_audio_only", False),
        ("Original video", "original_video", False),
        ("Speech audio", "audio", False),
        ("Audio analysis", "audio_analysis", False),
        ("Visual direction", "visual_script", False),
        ("Director plan", "director_plan", False),
        ("Scene review", "scene_review", False),
        ("Shot review", "shot_review", False),
        ("Original scene review", "original_scene_review", False),
        ("Original shot review", "original_shot_review", False),
        ("Reasoning Agent pre-render review", "llm_pre_render_review", False),
        ("Reasoning Agent review", "llm_video_review", False),
        ("Reasoning Agent corrected review", "llm_corrected_video_review", False),
        ("Reasoning Agent second corrected review", "llm_second_corrected_video_review", False),
        ("Web image sheet", "web_contact_sheet", False),
        ("GPT image sheet", "gpt_contact_sheet", False),
        ("Agent report", "checklist", False),
        ("Project folder", "project_dir", False),
    ]
    cards = []
    for label, key, primary in items:
        path = result.get(key)
        if path and Path(path).exists():
            cards.append(output_card(label, path, job_id, primary=primary))
    if not cards:
        return ""
    return '<section><h2>Outputs</h2><div class="output-grid">' + "".join(cards) + "</div></section>"


def count_media(folder, suffixes):
    path = Path(folder)
    if not path.exists() or not path.is_dir():
        return 0
    return len([p for p in path.rglob("*") if p.is_file() and p.suffix.lower() in suffixes])


def latest_media(folder, suffixes):
    path = Path(folder)
    if not path.exists() or not path.is_dir():
        return None
    files = [p for p in path.rglob("*") if p.is_file() and p.suffix.lower() in suffixes]
    if not files:
        return None
    return max(files, key=lambda item: item.stat().st_mtime)


def read_project_report(project_dir):
    report_path = project_dir / "review" / "agent_report.json"
    if not report_path.exists():
        return {}
    try:
        return json.loads(report_path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return {}


def existing_report_path(report, key):
    value = report.get(key)
    if not value:
        return None
    path = Path(value)
    return path if path.exists() else None


def project_edited_mtime(project_dir):
    """Most-recent EDIT time for a project. A folder's own mtime does NOT change when a file inside
    it is edited in place (e.g. a rescript / timeline save rewriting config/project.json), so a plain
    folder-mtime sort looks stale. Take the newest of the signals that actually move on an edit:
    the folder, the config + report + run_form JSON, and the newest render output."""
    times = []
    try:
        times.append(project_dir.stat().st_mtime)
    except OSError:
        pass
    for rel in ("config/project.json", "review/agent_report.json", "input/run_form.json"):
        p = project_dir / rel
        try:
            if p.exists():
                times.append(p.stat().st_mtime)
        except OSError:
            pass
    newest_render = latest_media(project_dir / "renders", {".mp4", ".webm"})
    if newest_render:
        try:
            times.append(Path(newest_render).stat().st_mtime)
        except OSError:
            pass
    return max(times) if times else 0.0


def project_preview_kind(project_dir, report):
    """Badge to overlay on the preview: 'sfx' / 'vfx' for master-tool projects, else ''."""
    slug = project_dir.name.lower()
    kind = str(report.get("job_kind") or report.get("mode") or "").lower()
    if slug.startswith("sfxmaster") or "sfx" in kind:
        return "sfx"
    if slug.startswith("visualmaster") or slug.endswith("_visual_enhanced") or "visual" in kind or "vfx" in kind:
        return "vfx"
    return ""


def project_preview_image(project_dir, video):
    """A SINGLE representative frame (the opening = hook), cached once, instead of a busy
    contact-sheet grid. Falls back to one generated image, never a sheet."""
    cache = project_dir / "review" / "_preview.jpg"
    try:
        if cache.exists() and cache.stat().st_size > 1024:
            # refresh if the render is newer than the cached poster
            if not (video and Path(video).exists() and Path(video).stat().st_mtime > cache.stat().st_mtime):
                return cache
        if video and Path(video).exists():
            cache.parent.mkdir(parents=True, exist_ok=True)
            if pipeline.extract_poster_frame(video, cache, at=1.0) and cache.exists():
                return cache
    except Exception:
        pass
    # no video yet: a single generated image (NOT a contact sheet)
    return (latest_media(project_dir / "gpt images", {".jpg", ".jpeg", ".png", ".webp"})
            or latest_media(project_dir / "web images", {".jpg", ".jpeg", ".png", ".webp"})
            or latest_media(project_dir / "speaker images", {".jpg", ".jpeg", ".png", ".webp"}))


def project_summary(project_dir):
    report = read_project_report(project_dir)
    video = existing_report_path(report, "video") or latest_media(project_dir / "renders", {".mp4", ".webm"})
    scene_review = existing_report_path(report, "scene_review")
    shot_review = existing_report_path(report, "shot_review")
    web_sheet = existing_report_path(report, "web_contact_sheet")
    gpt_sheet = existing_report_path(report, "gpt_contact_sheet")
    # single-frame preview (hook/opening) instead of a contact-sheet grid
    thumb = project_preview_image(project_dir, video)
    preview_kind = project_preview_kind(project_dir, report)
    created_at = report.get("created_at")
    if not created_at:
        created_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(project_dir.stat().st_mtime))
    edited_at = time.strftime("%Y-%m-%d %H:%M", time.localtime(project_edited_mtime(project_dir)))
    failed = not bool(video)
    return {
        "title": report.get("title") or project_title_from_files(project_dir),
        "slug": project_dir.name,
        "created_at": created_at,
        "edited_at": edited_at,
        "project_dir": project_dir,
        "video": video,
        "scene_review": scene_review,
        "shot_review": shot_review,
        "web_sheet": web_sheet,
        "gpt_sheet": gpt_sheet,
        "thumb": thumb,
        "preview_kind": preview_kind,
        "renders": project_dir / "renders",
        "review": project_dir / "review",
        "gpt_images": count_media(project_dir / "gpt images", {".jpg", ".jpeg", ".png", ".webp"}),
        "web_images": count_media(project_dir / "web images", {".jpg", ".jpeg", ".png", ".webp"}),
        "seedance": count_media(project_dir / "seedance 2.0", {".mp4", ".webm", ".mov", ".m4v"}),
        "failed": failed,
    }


def asset_card(summary, hidden_view=False):
    thumb = summary.get("thumb")
    has_video = bool(summary.get("video") and Path(summary["video"]).exists())
    preview_status = "failed" if summary.get("failed") else str(summary.get("preview_kind") or "")
    preview_label = {"sfx": "SFX", "vfx": "VFX", "failed": "FAILED"}.get(preview_status, "")
    status_class = f" preview-status preview-{preview_status}" if preview_label else ""
    badge_html = (f'<span class="asset-preview-label">{preview_label}</span>' if preview_label else "")
    if thumb and Path(thumb).exists() and is_image_path(thumb):
        thumb_html = (
            f'<button class="asset-figure preview-button{status_class}" type="button" data-preview-src="{link_for(thumb)}" data-preview-title="{esc(summary["title"])}">'
            f'<img class="asset-thumb" loading="lazy" src="{link_for(thumb)}" alt="{esc(summary["title"])}">{badge_html}</button>'
        )
    elif has_video:
        thumb_html = f'<div class="asset-figure{status_class}"><video class="asset-thumb" preload="none" muted data-lazy-src="{link_for(summary["video"])}"></video>{badge_html}</div>'
    else:
        thumb_html = f'<div class="asset-figure{status_class}"><div class="asset-thumb"></div>{badge_html}</div>'

    slug = summary["slug"]
    # "Check results" opens the final video if it exists, otherwise the project folder.
    results_href = view_for(summary["video"], "assets") if has_video else view_for(summary["project_dir"], "assets")
    failed = bool(summary.get("failed"))
    if has_video:
        primary_action = f'<a class="button asset-go" href="{results_href}">&#9654; Check results</a>'
        secondary_action = f'<a class="button secondary asset-timeline" href="/timeline?slug={esc(slug)}">&#127902; Edit</a>'
    elif failed:
        # Failed / incomplete run -> one-click CONTINUE (smart resume: reuse voiceover + existing
        # media, fill only what is missing, then render). "Open" loads it into the form to edit first.
        primary_action = (f'<button type="button" class="button asset-go" title="Continue this run from '
                          f'where it stopped - reuses what is already done" '
                          f'onclick="resumeAsset(\'{esc(slug)}\', this)">&#9654; Continue</button>')
        secondary_action = f'<a class="button secondary" href="/?project={esc(slug)}">&#8635; Open</a>'
    else:
        primary_action = f'<a class="button asset-go" href="{results_href}">&#9654; Check results</a>'
        secondary_action = f'<a class="button secondary" href="/?project={esc(slug)}">&#8635; Open</a>'
    if hidden_view:
        corner_btn = (f'<button type="button" class="asset-hide asset-unhide" title="Unhide - show in the '
                      f'library again" aria-label="Unhide project" '
                      f'onclick="setAssetHidden(\'{esc(slug)}\', false, this)">&#8634;</button>')
    else:
        corner_btn = (f'<button type="button" class="asset-hide" title="Hide from the library (does NOT '
                      f'delete anything)" aria-label="Hide project" '
                      f'onclick="setAssetHidden(\'{esc(slug)}\', true, this)">&#10005;</button>')
    return f"""
    <article class="panel asset-card{' project-' + preview_status if preview_label else ''}">
      {corner_btn}
      {thumb_html}
      <div class="asset-body">
        <div class="asset-titlerow">
          <h2>{esc(summary["title"])}</h2>
          <button type="button" class="asset-rename" title="Rename title" aria-label="Rename title" onclick="renameAsset('{esc(slug)}', this)">&#9998;</button>
        </div>
        <div class="asset-sub">Edited {esc(summary.get("edited_at") or summary["created_at"])}</div>
        <div class="asset-meta">
          <span class="asset-pill">{summary["web_images"]} web</span>
          <span class="asset-pill">{summary["gpt_images"]} GPT</span>
          <span class="asset-pill">{summary["seedance"]} clips</span>
        </div>
      </div>
      <div class="asset-primary">
        {primary_action}
        {secondary_action}
      </div>
    </article>
    """


def rename_project_title(slug, title):
    """Set a project's display title. Writes it to review/agent_report.json (highest precedence in
    project_title_from_files), plus config/project.json and input/run_form.json when they exist, so
    the new title shows everywhere the app reads a title from. Atomic per file."""
    project_dir = safe_project_dir(slug)
    if not project_dir:
        return {"ok": False, "error": "Project not found."}
    title = " ".join(str(title or "").split())[:200].strip()
    if not title:
        return {"ok": False, "error": "Title cannot be empty."}
    targets = [
        (project_dir / "review" / "agent_report.json", True),   # create if missing (top precedence)
        (project_dir / "config" / "project.json", False),
        (project_dir / "input" / "run_form.json", False),
    ]
    wrote = False
    for path, create in targets:
        if not path.exists() and not create:
            continue
        data = read_json_file(path) if path.exists() else {}
        if not isinstance(data, dict):
            data = {}
        data["title"] = title
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, path)
            wrote = True
        except Exception as exc:                # noqa: BLE001
            return {"ok": False, "error": f"Could not write {path.name}: {exc}"}
    if not wrote:
        return {"ok": False, "error": "No writable project metadata to rename."}
    return {"ok": True, "title": title, "slug": project_dir.name}


def resume_project(slug):
    """Continue a failed/incomplete run from where it stopped: reload the project's saved run form
    and continue the SAME project folder in smart (audit) mode - which reuses the existing voiceover
    and already downloaded/generated media and only fills what is missing, then renders. Returns
    {ok, job_id} or {ok:False, error[, fallback]}."""
    project_dir = safe_project_dir(slug)
    if not project_dir:
        return {"ok": False, "error": "Project not found."}
    run_form = read_json_file(project_dir / "input" / "run_form.json")
    if not isinstance(run_form, dict) or not run_form:
        # Older project without a saved run form - fall back to loading it into the form to re-run.
        return {"ok": False, "error": "No saved run form for this project.",
                "fallback": "/?project=" + urllib.parse.quote(project_dir.name)}
    fields = {k: v for k, v in run_form.items() if not str(k).startswith("_")}
    fields["loaded_project_source"] = project_dir.name      # continue THIS project folder
    fields["slug"] = project_dir.name
    fields["run_type"] = "audit"                            # smart: reuse existing + fill missing
    fields.pop("initial_replace_media_path", None)
    fields.pop("initial_remove_media_path", None)
    try:
        job_id = start_job(fields, {})
    except Exception as exc:                                # noqa: BLE001
        return {"ok": False, "error": f"Could not start: {exc}"}
    return {"ok": True, "job_id": job_id}


def is_project_hidden(project_dir):
    """A project is HIDDEN from the library (not deleted) when it carries a `.hidden` marker file.
    A dedicated marker is used instead of a config flag so nothing a run rewrites can clobber it."""
    try:
        return (Path(project_dir) / ".hidden").exists()
    except OSError:
        return False


def set_project_hidden(slug, hidden):
    """Hide/unhide a project by creating/removing its `.hidden` marker. Never deletes any media."""
    project_dir = safe_project_dir(slug)
    if not project_dir:
        return {"ok": False, "error": "Project not found."}
    marker = project_dir / ".hidden"
    try:
        if hidden:
            marker.write_text(time.strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8")
        elif marker.exists():
            marker.unlink()
    except Exception as exc:                    # noqa: BLE001
        return {"ok": False, "error": f"Could not update visibility: {exc}"}
    return {"ok": True, "slug": project_dir.name, "hidden": bool(hidden)}


def assets_page(show_hidden=False):
    projects_dir = agent_core.PROJECTS_DIR
    all_projects = [p for p in projects_dir.iterdir() if p.is_dir()] if projects_dir.exists() else []
    all_projects.sort(key=project_edited_mtime, reverse=True)   # most recently EDITED first
    hidden_projects = [p for p in all_projects if is_project_hidden(p)]
    visible_projects = [p for p in all_projects if not is_project_hidden(p)]
    shown = hidden_projects if show_hidden else visible_projects
    cards = "".join(asset_card(project_summary(project), hidden_view=show_hidden) for project in shown)
    if not cards:
        empty = ("No hidden projects." if show_hidden
                 else "Finished runs and generated project folders will appear here.")
        cards = f'<section class="panel"><h2>{"No hidden projects" if show_hidden else "No assets yet"}</h2><div class="hint">{empty}</div></section>'
    if show_hidden:
        toggle = '<div class="asset-hidden-bar"><a class="button secondary" href="/assets">&#8592; Back to visible projects</a></div>'
    elif hidden_projects:
        toggle = (f'<div class="asset-hidden-bar"><a class="button secondary" href="/assets?show_hidden=1">'
                  f'&#128065; Show hidden projects ({len(hidden_projects)})</a></div>')
    else:
        toggle = ""
    body = f"""
    {brand_header()}
    <section class="asset-grid">{cards}</section>
    {toggle}
    <style>
      .asset-card {{ position: relative; }}
      .asset-card .asset-figure.preview-status {{ position:relative; border-width:3px; border-style:solid;
        box-sizing:border-box; }}
      .asset-card .asset-figure.preview-sfx {{ border-color:#7c5cff; }}
      .asset-card .asset-figure.preview-vfx {{ border-color:#f0912b; }}
      .asset-card .asset-figure.preview-failed {{ border-color:var(--danger, #d84b4b); }}
      .asset-preview-label {{ position:absolute; z-index:5; left:8px; top:8px; padding:3px 8px;
        border-radius:5px; color:#fff; font-size:10px; line-height:1.25; font-weight:800;
        letter-spacing:.06em; box-shadow:0 1px 4px rgba(0,0,0,.45); }}
      .preview-sfx .asset-preview-label {{ background:#7c5cff; }}
      .preview-vfx .asset-preview-label {{ background:#f0912b; }}
      .preview-failed .asset-preview-label {{ background:var(--danger, #d84b4b); }}
      .asset-card.project-sfx {{ border-color:#7c5cff; }}
      .asset-card.project-vfx {{ border-color:#f0912b; }}
      .asset-card.project-failed {{ border-color:var(--danger, #d84b4b); }}
      .asset-titlerow {{ display: flex; align-items: flex-start; gap: 8px; }}
      .asset-titlerow h2 {{ flex: 1 1 auto; min-width: 0; margin: 0; }}
      /* Selector must out-specify the global `button:not(.preview-button)` rule (0,1,1) which sets
         width:100% + big padding - otherwise the icon button goes full-width and crushes the title. */
      .asset-titlerow .asset-rename {{ flex: 0 0 auto; width: auto; min-width: 0; margin: 0;
        padding: 5px 8px; background: var(--bg-input); border: 1px solid var(--line);
        color: var(--faint); border-radius: var(--r-sm); cursor: pointer; font-size: 13px;
        line-height: 1; box-shadow: none; transition: color .15s, border-color .15s, background .15s; }}
      .asset-titlerow .asset-rename:hover {{ color: var(--accent); border-color: var(--accent);
        background: var(--bg-overlay); box-shadow: none; transform: none; }}
      .asset-card .asset-hide {{ position: absolute; top: 10px; right: 10px; z-index: 6; width: auto;
        min-width: 0; margin: 0; padding: 3px 8px; background: var(--bg-input); border: 1px solid var(--line);
        color: var(--faint); border-radius: var(--r-sm); cursor: pointer; font-size: 13px; line-height: 1;
        box-shadow: none; opacity: .72; transition: color .15s, border-color .15s, background .15s, opacity .15s; }}
      .asset-card .asset-hide:hover {{ opacity: 1; color: var(--danger, #c0392b); border-color: var(--danger, #c0392b);
        background: var(--bg-overlay); box-shadow: none; transform: none; }}
      .asset-card .asset-unhide:hover {{ color: var(--accent); border-color: var(--accent); }}
      .asset-hidden-bar {{ display: flex; justify-content: center; margin: 22px 0 8px; }}
    </style>
    <script>
      window.renameAsset = function (slug, btn) {{
        try {{ if (typeof playClick === "function") playClick(); }} catch (e) {{}}
        var card = btn.closest(".asset-card");
        var h2 = card ? card.querySelector("h2") : null;
        var cur = h2 ? h2.textContent.trim() : "";
        var next = window.prompt("New title for this project:", cur);
        if (next === null) return;
        next = next.trim();
        if (!next || next === cur) return;
        btn.disabled = true;
        fetch("/rename-project", {{ method: "POST", headers: {{ "Content-Type": "application/json" }},
          body: JSON.stringify({{ slug: slug, title: next }}) }})
          .then(function (r) {{ return r.json(); }})
          .then(function (res) {{
            btn.disabled = false;
            if (res && res.ok) {{ if (h2) h2.textContent = res.title || next; }}
            else {{ alert((res && res.error) || "Rename failed."); }}
          }})
          .catch(function () {{ btn.disabled = false; alert("Rename failed."); }});
      }};
      window.setAssetHidden = function (slug, hidden, btn) {{
        try {{ if (typeof playClick === "function") playClick(); }} catch (e) {{}}
        var card = btn.closest(".asset-card");
        btn.disabled = true;
        fetch("/hide-project", {{ method: "POST", headers: {{ "Content-Type": "application/json" }},
          body: JSON.stringify({{ slug: slug, hidden: !!hidden }}) }})
          .then(function (r) {{ return r.json(); }})
          .then(function (res) {{
            if (res && res.ok) {{
              if (card) {{ card.style.transition = "opacity .2s"; card.style.opacity = "0";
                setTimeout(function () {{ card.remove(); }}, 200); }}
            }} else {{ btn.disabled = false; alert((res && res.error) || "Could not update visibility."); }}
          }})
          .catch(function () {{ btn.disabled = false; alert("Could not update visibility."); }});
      }};
      window.resumeAsset = function (slug, btn) {{
        try {{ if (typeof playClick === "function") playClick(); }} catch (e) {{}}
        btn.disabled = true;
        var label = btn.innerHTML; btn.innerHTML = "Continuing…";
        fetch("/resume-project", {{ method: "POST", headers: {{ "Content-Type": "application/json" }},
          body: JSON.stringify({{ slug: slug }}) }})
          .then(function (r) {{ return r.json(); }})
          .then(function (res) {{
            if (res && res.ok && res.job_id) {{ window.location.href = "/job?id=" + encodeURIComponent(res.job_id); }}
            else if (res && res.fallback) {{ window.location.href = res.fallback; }}
            else {{ btn.disabled = false; btn.innerHTML = label; alert((res && res.error) || "Could not continue this run."); }}
          }})
          .catch(function () {{ btn.disabled = false; btn.innerHTML = label; alert("Could not continue this run."); }});
      }};
    </script>
    """
    return page("Asset Library", body)


TIMELINE_SKELETON = """
<div id="timeline-root" data-slug="__SLUG__">
  <div class="tl-toolbar panel">
    <div class="tl-actions">
      <button type="button" class="button secondary tl-save-btn" id="tl-save" title="Save timeline changes">&#128190; Save</button>
      <div class="tl-popwrap">
        <button type="button" class="button secondary tl-save-btn" id="tl-versions-open" title="Load a timeline from before a redo or replacement">&#128337; Versions</button>
        <div class="tl-pop tl-version-pop" id="tl-versions-pop" hidden>
          <div class="tl-version-title">Older timelines</div>
          <div id="tl-version-list" class="tl-version-list"><span class="hint">No older versions yet.</span></div>
        </div>
      </div>
      <div class="tl-popwrap">
        <button type="button" class="button primary tl-render-btn" id="tl-render-open">&#11015; Render</button>
        <div class="tl-pop" id="tl-render-pop" hidden>
          <label class="tl-cap-toggle"><input type="checkbox" id="tl-captions"><span>Render with captions</span></label>
          <label class="tl-cap-toggle" title="Off = render with the voice only, no sound effects (content, transition, cut and added sounds are all muted)"><input type="checkbox" id="tl-sfx-toggle"><span>Render with sound effects</span></label>
          <button type="button" class="button primary tl-render-btn" id="tl-render" title="Saves your timeline automatically, then renders">&#11015; Render</button>
        </div>
      </div>
      <div class="tl-popwrap">
        <button type="button" class="button tl-rework-btn" id="tl-rework-open" title="Agent rework">&#129302; Rework</button>
        <div class="tl-pop" id="tl-rework-pop" hidden>
          <label class="tl-chk"><input type="checkbox" id="tl-rw-replace"><span id="tl-rw-replace-label">replace selected media</span></label>
          <label class="tl-chk"><input type="checkbox" id="tl-rw-recut"> reorder &amp; recut</label>
          <label class="tl-chk" title="Creates a fresh take with the saved voice, then force-aligns captions and clip cuts to it"><input type="checkbox" id="tl-rw-revoice"> regenerate speech + retime</label>
          <label class="tl-chk" id="tl-rw-redo-sfx-wrap"><input type="checkbox" id="tl-rw-redo-sfx"> redo SFX</label>
          <label class="tl-chk" id="tl-rw-add-sfx-wrap"><input type="checkbox" id="tl-rw-add-sfx"> add more SFX</label>
          <label class="tl-chk" id="tl-rw-redo-cap-wrap"><input type="checkbox" id="tl-rw-redo-captions"> redo captions</label>
          <label class="tl-pop-field" for="tl-rw-model">Rework model</label>
          <select id="tl-rw-model" name="reasoning_model">
            <option value="anthropic/claude-fable-5">Claude Fable 5</option>
            <option value="anthropic/claude-sonnet-5">Claude Sonnet 5</option>
            <option value="anthropic/claude-opus-4.8">Claude Opus 4.8</option>
            <option value="openai/gpt-5.5">GPT-5.5</option>
            <option value="openai/gpt-5.6-sol">GPT-5.6 Sol</option>
            <option value="openai/gpt-5.6-terra">GPT-5.6 Terra</option>
            <option value="openai/gpt-5.6-luna">GPT-5.6 Luna</option>
            <option value="google/gemini-3.5-flash">Gemini 3.5 Flash</option>
            <option value="google/gemini-3.1-flash-lite">Gemini 3.1 Flash Lite</option>
            <option value="google/gemini-3.1-pro-preview">Gemini 3.1 Pro</option>
          </select>
          <button type="button" id="tl-script-btn" hidden>Change script</button>
          <button type="button" class="button tl-rework-btn" id="tl-rework">&#129302; Start rework</button>
        </div>
      </div>
      <button type="button" class="button secondary tl-rework-btn" id="tl-script-open" title="Rewrite the narration - fresh voiceover + recut (its own workflow)">&#128221; Script</button>
    </div>
    <div class="tl-script-overlay" id="tl-script-modal" hidden>
      <div class="tl-script-box panel">
        <h2>&#128221; Change the script</h2>
        <div class="hint">Edit the narration below (one spoken line per row). Lines you leave
          UNCHANGED keep their current media. Changed or new lines get a fresh voiceover and
          media &mdash; matched from this project's existing clips first, then TikTok/X for
          whatever is still missing &mdash; and the whole Short is retimed and re-rendered.</div>
        <textarea id="tl-script-text" spellcheck="false"></textarea>
        <div class="tl-script-hookbar">
          <button type="button" class="button secondary" id="tl-script-markhook" title="Select the opening line(s) in the script above, then click to mark them as the hook (spoken with the hook pause)">&#9733; Mark hook</button>
          <button type="button" class="button secondary" id="tl-script-clearhook">Clear</button>
          <span class="tl-script-hookstatus" id="tl-script-hookstatus"></span>
        </div>
        <div class="tl-script-voicebar">
          <label>Narrator</label>
          <input type="text" id="tl-script-speaker" placeholder="Speaker name" title="Persona name used in the TTS prompt (e.g. Narrator)">
          <select id="tl-script-voice" title="Gemini TTS voice for the fresh voiceover"></select>
          <select id="tl-script-ttsmodel" title="TTS model quality">
            <option value="flash">Flash TTS (cheaper)</option>
            <option value="pro">Pro TTS (higher quality)</option>
          </select>
        </div>
        <div class="tl-script-densitybar">
          <label>Clips</label>
          <div class="tl-density-seg" id="tl-script-density" title="How many clips the video uses: Few = longer holds / slower cuts, Many = fast-paced montage with more cuts">
            <button type="button" data-density="few">Few</button>
            <button type="button" data-density="medium" class="on">Medium</button>
            <button type="button" data-density="many">Many</button>
          </div>
          <span class="tl-density-hint" id="tl-script-density-hint"></span>
        </div>
        <div class="tl-script-densitybar">
          <label>Media</label>
          <div class="tl-density-seg" id="tl-script-mediasrc" title="Where the media for changed / new lines comes from">
            <button type="button" data-media="scrape" class="on">Scrape new</button>
            <button type="button" data-media="keep_visible">Keep current</button>
            <button type="button" data-media="library">Best from library</button>
          </div>
          <span class="tl-density-hint" id="tl-script-mediasrc-hint"></span>
        </div>
        <div class="tl-script-actions">
          <button type="button" class="button secondary" id="tl-script-cancel">Cancel</button>
          <button type="button" class="button primary" id="tl-script-run">&#127908; Re-voice &amp; recut</button>
        </div>
      </div>
    </div>
    <div class="tl-meta">
      <div class="tl-histzoom">
        <button type="button" class="tl-ctrl tl-mini" id="tl-undo" title="Undo (Ctrl+Z)">&#8630;</button>
        <button type="button" class="tl-ctrl tl-mini" id="tl-redo" title="Redo (Ctrl+Y)">&#8631;</button>
        <span class="tl-sep"></span>
        <button type="button" class="tl-ctrl tl-mini" id="tl-zoom-out" title="Zoom out (-)">&#8722;</button>
        <button type="button" class="tl-ctrl tl-mini" id="tl-zoom-in" title="Zoom in (+)">+</button>
        <button type="button" class="tl-ctrl tl-mini" id="tl-zoom-fit" title="Fit timeline (F)">FIT</button>
      </div>
      <span class="tl-total" id="tl-total"></span>
    </div>
  </div>
  <div class="tl-grid tl-top">
    <div class="panel tl-player">
      <div class="tl-stage-view" id="tl-stage-view">
        <img id="tl-pimg" alt="">
        <video id="tl-pvid" muted playsinline></video>
        <div class="tl-preview-caption" id="tl-preview-caption" aria-live="off"></div>
        <div class="tl-overlay-layer" id="tl-overlay-layer"></div>
        <div class="tl-stage-empty" id="tl-stage-empty">Press play to preview</div>
        <div class="tl-player-bar">
          <button type="button" class="tl-ctrl" id="tl-back" title="Back 5s" aria-label="Back 5 seconds"><svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor"><path d="M6 6h2.2v12H6zM20 6v12L9.5 12z"/></svg></button>
          <button type="button" class="tl-ctrl tl-ctrl-main" id="tl-play" title="Play / Pause" aria-label="Play"><span id="tl-play-ico"><svg viewBox="0 0 24 24" width="22" height="22" fill="currentColor"><path d="M8 5v14l11-7z"/></svg></span></button>
          <button type="button" class="tl-ctrl" id="tl-fwd" title="Forward 5s" aria-label="Forward 5 seconds"><svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor"><path d="M15.8 6H18v12h-2.2zM4 6v12l10.5-6z"/></svg></button>
          <span class="tl-playtime" id="tl-playtime">0:00 / 0:00</span>
        </div>
      </div>
    </div>
    <div class="panel tl-inspector" id="tl-inspector">
      <h2>Inspector</h2>
      <div class="hint" id="tl-insp-empty">Click a clip, visual, transition or sound effect to edit it.</div>
      <div class="tl-insp-pane" id="tl-insp-clip" hidden>
        <div class="tl-insp-name" id="tl-insp-name"></div>
        <label>Duration (seconds)</label>
        <input type="number" id="tl-insp-dur" min="0.5" max="20" step="0.1">
        <label>Speed <span id="tl-insp-speed-val"></span></label>
        <input type="range" id="tl-insp-speed" min="0.5" max="2" step="0.05">
        <label class="tl-chk" id="tl-insp-blur-row"><input type="checkbox" id="tl-insp-blurcap"> Blur burned-in captions</label>
        <div class="hint" id="tl-insp-blur-hint" hidden>OCR finds the caption letters in this clip and blurs only those. Applied on Save / Render (can take ~20s per clip the first time). Only blur added here can be toggled off again &mdash; blur baked in by an older scrape run is part of the footage itself (right-click the clip and replace the media instead).</div>
        <div class="tl-insp-actions">
          <button type="button" class="button secondary" id="tl-insp-remove">Remove clip</button>
        </div>
        <div class="hint" id="tl-insp-note">Tip: click the red strip on top of a clip to mark its media for the agent to replace.</div>
      </div>
      <div class="tl-insp-pane" id="tl-insp-fx" hidden>
        <div class="tl-insp-name" id="tl-fx-name"></div>
        <label class="tl-fx-enable"><input type="checkbox" id="tl-fx-enabled"> Enabled</label>
        <label>Volume <span id="tl-fx-vol-val"></span></label>
        <input type="range" id="tl-fx-vol" min="0" max="0.6" step="0.01">
        <label>Start (seconds)</label>
        <input type="number" id="tl-fx-time" min="0" step="0.05">
        <label>Trim start of sound (seconds) <span id="tl-fx-trim-val"></span></label>
        <input type="number" id="tl-fx-trim" min="0" step="0.05" title="Skip the first N seconds of the sound file - e.g. cut the slow start off a riser so it hits sooner.">
        <label>Sound</label>
        <select id="tl-fx-sound"><option value="">(keep current)</option></select>
        <div class="tl-insp-actions">
          <button type="button" class="button secondary" id="tl-fx-preview">&#9654; Preview</button>
          <button type="button" class="button secondary" id="tl-fx-delete">Delete sound</button>
        </div>
        <div class="hint" id="tl-fx-note"></div>
      </div>
      <div class="tl-insp-pane" id="tl-insp-overlay" hidden>
        <div class="tl-insp-name" id="tl-ov-name"></div>
        <label>Horizontal position <span id="tl-ov-x-val"></span></label>
        <input type="range" id="tl-ov-x" min="0" max="1" step="0.005">
        <label>Vertical position <span id="tl-ov-y-val"></span></label>
        <input type="range" id="tl-ov-y" min="0" max="1" step="0.005">
        <label>Scale <span id="tl-ov-scale-val"></span></label>
        <input type="range" id="tl-ov-scale" min="0.25" max="3" step="0.05">
        <div id="tl-ov-arrow-controls">
          <label>Arrow style</label>
          <select id="tl-ov-style">
            <option value="default_thick_red_arrow">Red sticker</option>
            <option value="yellow_sticker_arrow">Yellow sticker</option>
            <option value="white_sticker_arrow">White / black</option>
            <option value="neon_green_arrow">Neon green</option>
          </select>
          <label>Rotation <span id="tl-ov-rotation-val"></span></label>
          <input type="range" id="tl-ov-rotation" min="-180" max="180" step="1">
        </div>
        <label>Entrance animation</label>
        <select id="tl-ov-animation">
          <option value="pop">Pop</option>
          <option value="bounce">Bounce</option>
          <option value="slide">Slide in</option>
          <option value="fade">Fade in</option>
          <option value="none">None</option>
        </select>
        <label>Animation duration <span id="tl-ov-animation-duration-val"></span></label>
        <input type="range" id="tl-ov-animation-duration" min="0.1" max="1.5" step="0.05">
        <label>Appear sound</label>
        <select id="tl-ov-sfx"><option value="">None</option></select>
        <label>Sound volume <span id="tl-ov-sfx-volume-val"></span></label>
        <input type="range" id="tl-ov-sfx-volume" min="0" max="0.6" step="0.01">
        <div class="tl-insp-actions">
          <button type="button" class="button secondary" id="tl-ov-replay">&#9654; Replay entrance</button>
          <button type="button" class="button secondary" id="tl-ov-delete">Delete visual</button>
        </div>
        <div class="hint">Drag in the preview to move, use the corner handle to scale, and replay to test animation + sound.</div>
      </div>
    </div>
  </div>
  <div class="panel tl-stage">
    <div class="tl-rows">
      <div class="tl-row-labels">
        <div class="tl-rlabel tl-rl-ruler"></div>
        <div class="tl-rlabel tl-rl-clips">Clips</div>
        <div class="tl-rlabel tl-rl-ov">Visuals</div>
        <div class="tl-rlabel tl-rl-voice">Voice</div>
        <div class="tl-rlabel tl-rl-sfx">Sound&nbsp;FX</div>
      </div>
      <div class="tl-scroll" id="tl-scroll">
        <div class="tl-playhead" id="tl-playhead"></div>
        <div class="tl-voice-end" id="tl-voice-end" hidden title="Voiceover length"><span class="tl-voice-end-lbl"></span></div>
        <div class="tl-ruler" id="tl-ruler"></div>
        <div class="tl-track tl-clips-track" id="tl-clips"></div>
        <div class="tl-track tl-ovtrack" id="tl-ovtrack"></div>
        <div class="tl-track tl-aud" id="tl-voice"></div>
        <div class="tl-track tl-sfx tl-trtrack" id="tl-sfx"></div>
      </div>
    </div>
  </div>
  <div class="panel tl-mixer-wrap">
    <h2>Audio mixer</h2>
    <div class="tl-mixer">
      <div class="tl-slider"><label>Voice <span id="tl-v-voice"></span></label><input type="range" id="tl-voice-vol" min="0" max="1.5" step="0.05"></div>
      <div class="tl-slider"><label>Music <span id="tl-v-music"></span></label><input type="range" id="tl-music-vol" min="0" max="0.6" step="0.01"></div>
    </div>
  </div>
  <div class="panel tl-library">
    <div class="tl-lib-head">
      <h2>Library</h2>
      <div class="tl-lib-tabs">
        <button type="button" class="tl-lib-tab active" data-lib="media">Project media</button>
        <button type="button" class="tl-lib-tab" data-lib="sfx">Sound&nbsp;FX</button>
      </div>
      <span class="tl-lib-hint">drag onto the timeline</span>
    </div>
    <div class="tl-lib-body" id="tl-lib-media"><div class="media-loading" role="status"><div class="ml-spinner"></div><div class="ml-bar"><i></i></div><div class="ml-text">Loading media</div></div></div>
    <div class="tl-lib-body" id="tl-lib-sfx" hidden><div class="media-loading" role="status"><div class="ml-spinner"></div><div class="ml-bar"><i></i></div><div class="ml-text">Loading sounds</div></div></div>
  </div>
</div>
"""

TIMELINE_ASSETS = """
<style>
  body:has(#timeline-root) main { max-width:none; width:100%; box-sizing:border-box; padding-left:12px; padding-right:12px; }
  #timeline-root { width:100%; }
  .tl-toolbar { display:flex; align-items:center; justify-content:space-between; gap:16px; flex-wrap:wrap; margin-bottom:16px; }
  .tl-actions { display:flex; align-items:center; gap:10px; flex-wrap:wrap; }
  /* scope width:auto under .tl-actions so it beats the global button{width:100%} */
  .tl-actions .button, .tl-actions .tl-render-btn, .tl-actions .tl-save-btn, .tl-actions .tl-rework-btn { width:auto; min-width:0; }
  .tl-render-btn { font-size:13.5px; padding:9px 16px; }
  .tl-save-btn { font-size:13.5px; padding:9px 14px; }
  .tl-save-btn.tl-unsaved { border-color:var(--accent); box-shadow:inset 0 0 0 1px var(--accent); color:var(--text); }
  .tl-rework-group { display:flex; align-items:center; gap:8px 12px; flex-wrap:wrap; padding-left:12px; margin-left:4px; border-left:1px solid var(--line-strong); }
  /* Change-script modal (scope everything - the global button rule is full-width) */
  .tl-script-overlay { position:fixed; inset:0; z-index:2147483001; background:rgba(5,8,14,.72); display:flex; align-items:center; justify-content:center; padding:4vh 16px; box-sizing:border-box; }
  .tl-script-overlay[hidden] { display:none; }
  .tl-script-box { width:min(760px, 94vw); max-height:88vh; display:flex; flex-direction:column; gap:12px; }
  .tl-script-box textarea { flex:1 1 auto; min-height:320px; max-height:56vh; resize:vertical; font-family:var(--mono); font-size:13.5px; line-height:1.55; }
  .tl-script-actions { display:flex; justify-content:flex-end; gap:10px; }
  .tl-script-actions .button { width:auto; min-width:0; margin:0; padding:10px 18px; }
  .tl-script-hookbar { display:flex; align-items:center; gap:10px; }
  .tl-script-hookbar .button { width:auto; min-width:0; margin:0; padding:7px 12px; font-size:12px; }
  .tl-script-hookstatus { flex:1 1 auto; min-width:0; font-size:12px; font-weight:700; color:var(--warning); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .tl-script-hookstatus.off { color:var(--muted); font-weight:600; }
  .tl-script-voicebar { display:flex; align-items:center; gap:10px; flex-wrap:wrap; }
  .tl-script-voicebar label { margin:0; font-size:12px; font-weight:700; color:var(--muted); text-transform:uppercase; letter-spacing:.05em; }
  .tl-script-voicebar input { flex:1 1 140px; min-width:120px; width:auto; margin:0; padding:8px 10px; font-size:13px; }
  .tl-script-voicebar select { flex:1 1 170px; min-width:150px; width:auto; margin:0; padding:8px 10px; font-size:13px; }
  .tl-script-densitybar { display:flex; align-items:center; gap:10px; flex-wrap:wrap; }
  .tl-script-densitybar label { margin:0; font-size:12px; font-weight:700; color:var(--muted); text-transform:uppercase; letter-spacing:.05em; }
  .tl-density-seg { display:inline-flex; border:1px solid var(--line-strong); border-radius:var(--r-sm); overflow:hidden; }
  .tl-density-seg button { width:auto; min-width:0; margin:0; border:0; border-radius:0; padding:7px 16px; font-size:12.5px; font-weight:700; background:var(--bg-input); color:var(--muted); box-shadow:none; }
  .tl-density-seg button + button { border-left:1px solid var(--line-strong); }
  .tl-density-seg button.on { background:var(--accent); color:#fff; }
  .tl-density-hint { font-size:12px; color:var(--muted); font-weight:600; }
  .tl-rework-btn { font-size:13px; padding:9px 14px; }
  .tl-chk { display:flex; align-items:center; gap:9px; font-size:12px; font-weight:600; color:#c9d2cf; margin:0; cursor:pointer; }
  .tl-chk input { appearance:none; -webkit-appearance:none; width:30px; height:17px; flex:0 0 30px; margin:0; border:1px solid #4b5753; border-radius:999px; background:#252d2a; position:relative; cursor:pointer; transition:.16s ease; }
  .tl-chk input::after { content:""; position:absolute; width:11px; height:11px; left:2px; top:2px; border-radius:50%; background:#8b9692; transition:.16s ease; }
  .tl-chk input:checked { border-color:#42d39a; background:#176d50; }
  .tl-chk input:checked::after { left:15px; background:#eafff6; }
  .tl-chk input:disabled { opacity:.38; cursor:not-allowed; }
  .tl-version-pop { min-width:270px; max-height:330px; overflow:auto; }
  .tl-version-title { font-size:12px; font-weight:800; margin-bottom:7px; }
  .tl-version-list { display:flex; flex-direction:column; gap:6px; }
  .tl-version-item { width:100%; text-align:left; padding:8px 10px; font-size:11.5px; line-height:1.3; }
  .tl-version-item small { display:block; opacity:.65; margin-top:2px; }
  .tl-meta { display:flex; align-items:center; gap:16px; }
  .tl-total { font-weight:700; color:var(--muted); font-variant-numeric:tabular-nums; }
  .tl-cap-toggle { display:flex; align-items:center; gap:8px; font-weight:700; margin:0; cursor:pointer; }
  .tl-cap-toggle input { margin:0; }
  .tl-render-cap-choice { padding:8px 10px; border:1px solid var(--line-strong); border-radius:var(--r-md); background:var(--bg-input); font-size:12px; }
  .tl-render-cap-choice { padding:8px 10px; border:1px solid var(--line-strong); border-radius:var(--r-md); background:var(--bg-input); font-size:12px; }
  .tl-grid { display:grid; grid-template-columns:minmax(300px,420px) minmax(340px,500px); gap:16px; align-items:start; justify-content:start; }
  .tl-top { margin-bottom:16px; }
  /* player fills its panel; the 9:16 stage uses all available height, controls FLOAT over it */
  .tl-player { display:flex; align-items:center; justify-content:center; padding:6px; overflow:hidden; }
  .tl-stage-view { position:relative; aspect-ratio:9/16; height:100%; max-height:100%; width:auto; max-width:100%; background:#000; border:1px solid var(--line); border-radius:12px; overflow:hidden; display:flex; align-items:center; justify-content:center; }
  .tl-stage-view img, .tl-stage-view video { width:100%; height:100%; object-fit:cover; display:none; background:#000; }
  .tl-preview-caption { position:absolute; left:7%; right:7%; top:72%; z-index:6; display:none; text-align:center; color:#fff; font-size:clamp(18px,3.1vh,31px); line-height:1.04; font-weight:950; letter-spacing:.02em; text-transform:uppercase; text-shadow:-2px -2px 0 #111,2px -2px 0 #111,-2px 2px 0 #111,2px 2px 0 #111,0 4px 8px rgba(0,0,0,.8); pointer-events:none; }
  .tl-overlay-layer { position:absolute; inset:0; z-index:4; pointer-events:none; }
  .tl-preview-overlay { position:absolute; transform:translate(-50%,-50%); pointer-events:auto; cursor:move; touch-action:none; color:#ed2f25; filter:drop-shadow(2px 2px 0 #fff) drop-shadow(3px 3px 0 #17150f); transform-origin:center; }
  .tl-preview-overlay.selected { outline:2px solid var(--accent); outline-offset:5px; }
  .tl-preview-overlay .tl-ov-glyph { display:flex; align-items:center; justify-content:center; min-width:58px; min-height:38px; font-size:58px; line-height:1; font-weight:900; }
  .tl-preview-overlay.style-yellow_sticker_arrow { color:#ffcd23; }
  .tl-preview-overlay.style-white_sticker_arrow { color:#f7f7f4; filter:drop-shadow(1px 1px 0 #111) drop-shadow(3px 3px 0 #111); }
  .tl-preview-overlay.style-neon_green_arrow { color:#52f25c; }
  .tl-preview-overlay.kind-highlight .tl-ov-glyph { width:100px; height:60px; min-width:0; min-height:0; border:8px solid #ed2f25; border-radius:50%; font-size:0; }
  .tl-preview-overlay.kind-paper .tl-ov-glyph, .tl-preview-overlay.kind-newspaper .tl-ov-glyph,
  .tl-preview-overlay.kind-counter .tl-ov-glyph, .tl-preview-overlay.kind-stamp .tl-ov-glyph { min-width:120px; min-height:52px; padding:7px 10px; background:#fff4dc; border:4px solid #ed2f25; color:#a91814; font-size:13px; text-align:center; }
  .tl-ov-scale-handle { position:absolute; right:-12px; bottom:-12px; width:18px; height:18px; border-radius:50%; background:var(--accent); border:2px solid #fff; cursor:nwse-resize; display:none; }
  .tl-preview-overlay.selected .tl-ov-scale-handle { display:block; }
  .tl-stage-empty { position:absolute; color:var(--faint); font-weight:600; }
  /* floating transport bar overlaying the bottom of the video */
  .tl-player-bar { position:absolute; left:0; right:0; bottom:0; z-index:6; display:flex; align-items:center; justify-content:center; gap:12px; padding:10px 10px 22px;
    background:linear-gradient(to top, rgba(0,0,0,.62), rgba(0,0,0,.28) 55%, transparent);
    opacity:0; transition:opacity .18s ease; }
  .tl-stage-view:hover .tl-player-bar, .tl-stage-view:focus-within .tl-player-bar { opacity:1; }
  /* time on its OWN centered line at the very bottom - never overlaps the transport buttons */
  .tl-player-bar .tl-playtime { position:absolute; left:0; right:0; bottom:4px; text-align:center; color:#fff; font-size:11px; text-shadow:0 1px 3px rgba(0,0,0,.7); font-variant-numeric:tabular-nums; pointer-events:none; }
  @media (prefers-reduced-motion: reduce) { .tl-player-bar { transition:none; } }
  .tl-player-bar .tl-ctrl { width:42px; height:42px; min-width:0; padding:0; border-radius:50%; display:inline-flex; align-items:center; justify-content:center; color:var(--text); background:var(--bg-overlay); border:1px solid var(--line-strong); box-shadow:none; }
  .tl-ctrl::after { display:none; }
  .tl-player-bar .tl-ctrl:hover { background:var(--bg-raised); border-color:var(--accent); transform:translateY(-1px); box-shadow:none; }
  .tl-player-bar .tl-ctrl-main { width:54px; height:54px; color:#fff; background:linear-gradient(180deg,var(--accent-hover),var(--accent)); border-color:var(--accent-active); }
  .tl-ctrl-main:hover { background:linear-gradient(180deg,#a18dff,var(--accent-hover)); border-color:var(--accent-hover); }
  /* toolbar undo/redo/zoom cluster - scope deep enough to beat the global button rule */
  .tl-meta .tl-histzoom { display:inline-flex; align-items:center; gap:6px; }
  .tl-meta .tl-histzoom .tl-ctrl.tl-mini { width:34px; height:30px; min-width:0; padding:0; border-radius:8px;
    display:inline-flex; align-items:center; justify-content:center; font-size:15px; line-height:1;
    color:var(--text); background:var(--bg-overlay); border:1px solid var(--line-strong); box-shadow:none; transform:none; }
  .tl-meta .tl-histzoom .tl-ctrl.tl-mini:hover { background:var(--bg-raised); border-color:var(--accent); }
  .tl-meta .tl-histzoom .tl-ctrl.tl-mini:disabled { opacity:.35; cursor:default; border-color:var(--line-strong); background:var(--bg-overlay); }
  .tl-meta .tl-histzoom #tl-zoom-fit { width:auto; padding:0 9px; font-size:11px; letter-spacing:.06em; }
  .tl-meta .tl-histzoom .tl-sep { width:1px; height:18px; background:var(--line-strong); margin:0 3px; }
  .tl-playtime { font-weight:700; color:var(--muted); font-variant-numeric:tabular-nums; margin-left:6px; }
  .tl-stage { overflow:hidden; }
  .tl-rows { display:flex; gap:10px; }
  .tl-row-labels { display:flex; flex-direction:column; gap:6px; flex:0 0 auto; }
  .tl-rlabel { display:flex; align-items:center; font-weight:600; color:var(--faint); font-size:11px; text-transform:uppercase; letter-spacing:.5px; }
  .tl-rl-ruler { height:22px; }
  .tl-rl-clips { height:74px; }
  .tl-rl-ov, .tl-rl-cap, .tl-rl-voice, .tl-rl-tr, .tl-rl-sfx { height:32px; }
  .tl-scroll { position:relative; overflow-x:auto; flex:1; min-width:0; padding-bottom:4px; user-select:none; -webkit-user-select:none; scrollbar-width:none; -ms-overflow-style:none; }
  .tl-scroll::-webkit-scrollbar { display:none; height:0; width:0; }
  .tl-ruler { position:relative; height:22px; cursor:pointer; }
  .tl-tick { position:absolute; top:0; height:22px; border-left:1px solid rgba(255,255,255,.2); padding-left:5px; font-size:10px; color:rgba(255,255,255,.6); }
  .tl-rubber { position:absolute; top:4px; bottom:4px; background:var(--accent-subtle); border:1px solid var(--accent); border-radius:5px; z-index:9; pointer-events:none; }
  .tl-playhead { position:absolute; top:0; bottom:12px; width:2px; background:#ff5d5d; z-index:8; pointer-events:none; box-shadow:0 0 6px rgba(255,93,93,.8); }
  .tl-playhead::before { content:''; position:absolute; top:0; left:-5px; border-left:6px solid transparent; border-right:6px solid transparent; border-top:8px solid #ff5d5d; }
  /* #119 - voiceover-length marker: a dashed line across all tracks where the voice ends */
  .tl-voice-end { position:absolute; top:16px; bottom:12px; width:0; border-left:2px dashed #6fd3ff; z-index:7; pointer-events:none; }
  .tl-voice-end.tl-voice-mismatch { border-left-color:#ffb64f; }
  .tl-voice-end-lbl { position:absolute; top:-14px; left:3px; font-size:9.5px; font-weight:700; white-space:nowrap; color:#6fd3ff; background:var(--bg-raised); padding:0 3px; border-radius:3px; }
  .tl-voice-end.tl-voice-mismatch .tl-voice-end-lbl { color:#ffb64f; }
  .tl-track { position:relative; margin-top:6px; background:var(--bg-base); border:1px solid var(--line); border-radius:var(--r-md); }
  .tl-clips-track { height:74px; }
  .tl-ovtrack, .tl-cap, .tl-aud, .tl-trtrack, .tl-sfx { height:32px; }
  .tl-track.tl-muted { opacity:.32; filter:grayscale(.6); }
  .tl-clip { position:absolute; top:4px; bottom:4px; border:2px solid var(--line-strong); border-radius:var(--r-md); overflow:hidden; cursor:grab; background:var(--bg-overlay); background-size:cover; background-position:center; touch-action:none; box-shadow:var(--sh-1); }
  .tl-clip.selected { border-color:var(--accent); box-shadow:0 0 0 2px var(--accent-subtle); z-index:4; }
  .tl-clip.dragging { opacity:.8; cursor:grabbing; z-index:9; }
  .tl-clip .tl-badge { position:absolute; top:5px; left:6px; font-size:11px; font-weight:900; color:#fff; text-shadow:0 1px 3px #000; background:rgba(0,0,0,.45); padding:1px 6px; border-radius:5px; }
  .tl-clip .tl-clip-label { position:absolute; left:0; right:0; bottom:0; padding:4px 8px; font-size:11px; font-weight:800; color:#fff; background:linear-gradient(transparent, rgba(0,0,0,.85)); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .tl-clip .tl-handle { position:absolute; top:0; right:0; bottom:0; width:13px; cursor:ew-resize; background:linear-gradient(90deg, transparent, rgba(124,92,255,.6)); }
  /* left handle trims the SOURCE in-point ("cut front", CapCut-style); only on video clips */
  .tl-clip .tl-handle-l { position:absolute; top:0; left:0; bottom:0; width:13px; cursor:ew-resize; z-index:6; background:linear-gradient(90deg, rgba(47,111,214,.6), transparent); }
  .tl-clip.trimmed-front::after { content:"\\2702"; position:absolute; top:5px; left:16px; font-size:10px; color:#fff; text-shadow:0 1px 3px #000; z-index:5; }
  .tl-seam { position:absolute; top:0; bottom:0; width:0; border-left:2px dashed rgba(124,92,255,.4); z-index:3; pointer-events:none; }
  .tl-ovitem { position:absolute; top:6px; bottom:6px; min-width:42px; padding:0 8px; display:flex; align-items:center; overflow:hidden; white-space:nowrap; box-sizing:border-box; border-radius:7px; border:1px solid rgba(237,47,37,.8); background:rgba(237,47,37,.16); color:var(--text); font-size:11px; font-weight:700; cursor:pointer; }
  .tl-ovitem.selected { border-color:var(--accent); box-shadow:0 0 0 2px var(--accent-subtle); z-index:4; }
  .tl-trans { position:absolute; transform:translate(-50%,-50%); z-index:6; width:24px; height:24px; cursor:pointer; display:flex; align-items:center; justify-content:center; touch-action:none; }
  .tl-trans .tl-diamond { width:16px; height:16px; transform:rotate(45deg); background:var(--accent-2); border:1px solid #fff; border-radius:4px; box-shadow:0 0 0 3px var(--bg-base); transition:transform .12s ease; }
  .tl-trans.selected .tl-diamond { background:var(--accent); border-color:#fff; }
  .tl-trans.disabled .tl-diamond { background:var(--faint); border-color:var(--line-strong); }
  .tl-trans:hover .tl-diamond { transform:rotate(45deg) scale(1.18); }
  /* hover a clip boundary in the Cut-SFX row to reveal a "+" that adds a transition sound there */
  /* Only the visible green circle is interactive. The old slot covered the full track height
     at z-index 5 and stole pointerdown from SFX blocks underneath it. */
  .tl-trans-slot { position:absolute; top:50%; width:26px; height:26px; transform:translate(-50%,-50%); z-index:2; display:flex; align-items:center; justify-content:center; pointer-events:none; }
  .tl-trans-add { width:22px; height:22px; border-radius:50%; border:2px solid var(--accent); background:var(--bg-raised); color:var(--accent); font-size:16px; font-weight:900; line-height:1; display:flex; align-items:center; justify-content:center; opacity:0; transform:scale(.55); transition:opacity .12s var(--ease), transform .12s var(--ease); box-shadow:var(--sh-1); pointer-events:auto; cursor:pointer; }
  .tl-trans-slot:hover .tl-trans-add, .tl-trans-slot.menu-open .tl-trans-add { opacity:1; transform:scale(1); }
  .tl-trans-slot.menu-open .tl-trans-add { background:var(--accent); color:var(--accent-ink); }
  .tl-trans-menu { position:fixed; z-index:60; width:242px; max-height:300px; overflow-y:auto; background:var(--bg-raised); border:2px solid var(--ink); border-radius:var(--r-md); box-shadow:var(--sh-3); padding:6px; }
  .tl-trans-menu .tl-tm-title { font-family:var(--pixel); font-size:9px; letter-spacing:.06em; text-transform:uppercase; color:var(--muted); padding:4px 6px 7px; }
  .tl-trans-menu .tl-tm-item { display:flex; align-items:center; gap:8px; padding:6px 8px; border-radius:6px; cursor:pointer; font-size:12px; color:var(--text); }
  .tl-trans-menu .tl-tm-item:hover { background:var(--accent-subtle); }
  .tl-trans-menu .tl-tm-play { flex:0 0 auto; width:22px; height:22px; border-radius:5px; border:1px solid var(--line-strong); background:var(--bg-input); color:var(--text); font-size:9px; display:flex; align-items:center; justify-content:center; cursor:pointer; }
  .tl-trans-menu .tl-tm-name { flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .tl-trans-menu .tl-tm-empty { padding:12px 8px; color:var(--faint); font-size:12px; text-align:center; }
  /* hierarchical replace-sound picker: category rows drill down to sounds */
  .tl-trans-menu.tl-sfx-picker .tl-tm-cat { font-weight:600; }
  .tl-trans-menu .tl-tm-arrow { flex:0 0 auto; color:var(--muted); font-size:14px; }
  .tl-trans-menu .tl-tm-title .tl-tm-back { width:20px; height:20px; margin-right:6px; padding:0; border:1px solid var(--line-strong);
    background:var(--bg-input); color:var(--text); border-radius:5px; cursor:pointer; font-size:12px; line-height:1; vertical-align:middle; }
  .tl-trans-menu .tl-tm-title .tl-tm-back:hover { border-color:var(--accent); }
  /* SFX library category filter chips - all on ONE horizontal row */
  .tl-sfx-cats { display:flex; flex-wrap:wrap; gap:5px; margin:0 0 8px; }
  .tl-sfx-cat { width:auto; min-width:0; margin:0; padding:3px 9px; font-size:11px; font-weight:600; border-radius:99px;
    border:1px solid var(--line); background:transparent; color:var(--muted); cursor:pointer; box-shadow:none; white-space:nowrap;
    display:inline-flex; align-items:center; gap:4px; }
  .tl-sfx-cat:hover { border-color:var(--accent); color:var(--text); }
  .tl-sfx-cat.on { background:var(--accent-subtle); border-color:var(--accent); color:var(--text); }
  .tl-sfx-caret { font-size:8px; opacity:.7; }
  .tl-sfx-subchip { font-size:10.5px; padding:2px 8px; }
  /* subcategory popup - only opens for a category that actually has subcategories (e.g. Reaction) */
  .tl-sfx-subpop { position:fixed; z-index:2147483600; max-width:min(320px,92vw); background:var(--bg-raised);
    border:1px solid var(--line-strong); border-radius:10px; box-shadow:var(--sh-3); padding:8px;
    display:flex; flex-wrap:wrap; gap:5px; }
  /* greyed-out (unavailable) rework toggles / render captions toggle */
  .tl-disabled { opacity:.42; cursor:not-allowed; }
  .tl-disabled input { cursor:not-allowed; }
  /* #113 - the low/medium/high SFX-amount popup that appears next to Redo SFX */
  .tl-amt-pop { position:fixed; z-index:2147483600; background:var(--bg-raised); border:1px solid var(--line-strong);
    border-radius:10px; box-shadow:var(--sh-3); padding:9px 10px; display:flex; flex-direction:column; gap:7px; }
  .tl-amt-lbl { font-size:10.5px; font-weight:700; text-transform:uppercase; letter-spacing:.05em; color:var(--muted); }
  .tl-amt-row { display:flex; gap:6px; }
  .tl-amt-btn { width:auto; min-width:0; margin:0; padding:5px 12px; font-size:12px; font-weight:600; border-radius:8px;
    border:1px solid var(--line); background:transparent; color:var(--muted); cursor:pointer; box-shadow:none; }
  .tl-amt-btn:hover { border-color:var(--accent); color:var(--text); }
  .tl-amt-btn.on { background:var(--accent-subtle); border-color:var(--accent); color:var(--text); }
  .tl-capbar { position:absolute; top:7px; bottom:7px; border-radius:7px; background:var(--accent-subtle); border:1px solid rgba(124,92,255,.4); display:flex; align-items:center; padding-left:9px; color:var(--text); font-size:11px; font-weight:600; white-space:nowrap; overflow:hidden; }
  .tl-capbar.off { opacity:.3; }
  .tl-capitem { position:absolute; top:6px; bottom:6px; min-width:22px; overflow:hidden; white-space:nowrap; text-overflow:ellipsis; padding:0 7px; display:flex; align-items:center; border-radius:6px; border:1px solid rgba(124,92,255,.45); background:var(--accent-subtle); color:var(--text); font-size:10.5px; font-weight:650; box-sizing:border-box; }
  .tl-cap.off .tl-capitem { opacity:.32; }
  .tl-audbar { position:absolute; top:7px; bottom:7px; left:0; right:0; border-radius:7px; background:rgba(111,211,255,.12); border:1px solid rgba(111,211,255,.3); }
  .tl-fx { position:absolute; top:6px; bottom:6px; z-index:8; border-radius:7px; background:var(--bg-overlay); border:1px solid var(--line-strong); cursor:pointer; touch-action:none; display:flex; align-items:center; padding:0 8px; font-size:11px; font-weight:600; color:var(--text); white-space:nowrap; overflow:hidden; box-sizing:border-box; }
  .tl-fx.selected { border-color:var(--accent); box-shadow:0 0 0 2px var(--accent-subtle); z-index:9; }
  .tl-fx.disabled { opacity:.4; }
  .tl-mixer-wrap { margin-top:16px; }
  .tl-mixer { display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:16px; }
  .tl-slider label { display:flex; justify-content:space-between; margin-bottom:6px; }
  .tl-insp-name { font-weight:700; color:var(--text); margin-bottom:12px; }
  .tl-insp-actions { display:flex; gap:8px; flex-wrap:wrap; margin:12px 0 8px; }
  .tl-insp-actions .button { width:auto; flex:1; min-width:140px; }
  .tl-fx-enable { display:flex; align-items:center; gap:8px; margin-bottom:10px; }
  .tl-fx-enable input { margin:0; }
  /* modern in-timeline replace selection: a thin red strip attached to the top of
     each clip; click to toggle. It lives inside the clip so it scrolls with it. */
  .tl-clip .tl-clip-mark { position:absolute; top:0; left:0; right:0; height:8px; z-index:7; cursor:pointer; background:transparent; transition:background var(--dur) var(--ease); }
  .tl-clip .tl-clip-mark:hover { background:rgba(232,71,43,.35); }
  .tl-clip .tl-clip-mark.on { height:4px; background:var(--accent-2); box-shadow:0 0 0 1px var(--ink), 0 0 10px rgba(232,71,43,.95); animation:markpulse 1.3s ease-in-out infinite; }
  @keyframes markpulse { 0%,100%{ opacity:1; } 50%{ opacity:.55; } }
  .tl-clip.marked-replace { box-shadow:0 0 0 2px var(--accent-2), var(--sh-1); }
  .tl-clip .tl-replace-tag { position:absolute; top:6px; right:6px; font-family:var(--pixel); font-size:8px; color:#fff; background:var(--accent-2); padding:2px 5px; border-radius:2px; z-index:5; border:1px solid var(--ink); }
  .tl-library { margin-top:16px; }
  .tl-lib-head { display:flex; align-items:center; gap:14px; flex-wrap:wrap; margin-bottom:12px; }
  .tl-lib-head h2 { margin:0; }
  .tl-lib-tabs { display:flex; gap:6px; }
  .tl-lib-tabs .tl-lib-tab { width:auto; min-width:0; padding:7px 14px; font-size:12.5px; font-weight:600; background:var(--bg-input); border:1px solid var(--line); color:var(--muted); box-shadow:none; }
  .tl-lib-tab::after { display:none; }
  .tl-lib-tab.active { border-color:var(--accent); background:var(--accent-subtle); color:var(--text); }
  .tl-lib-hint { color:var(--faint); font-size:12px; }
  .tl-lib-body { display:block; max-height:720px; overflow-y:auto; padding:2px; }
  /* no enclosing bar: the tabs float as standalone pills. The sticky wrapper is transparent
     and click-through (pointer-events:none) so clips scrolling underneath stay interactive;
     only the pills themselves capture clicks (pointer-events:auto below). */
  .tl-sublib-tabs { display:flex; gap:6px; flex-wrap:wrap; margin-bottom:10px; position:sticky; top:0; background:transparent; padding:2px 0; z-index:5; pointer-events:none; }
  .tl-sublib-tab { width:auto; min-width:0; padding:5px 10px; font-family:var(--pixel); font-size:9px; text-transform:uppercase; background:var(--bg-input); border:2px solid var(--ink); color:var(--ink); border-radius:var(--r-sm); box-shadow:none; }
  .tl-sublib-tab::after { display:none; }
  .tl-sublib-tab.active { background:var(--accent); color:#fff; }
  .tl-sublib-grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(118px,1fr)); gap:10px; }
  /* sub-tabs + search inside the library body: WITHOUT these rules the buttons/input inherit
     the global full-width pixel button style and wreck the panel layout. */
  .tl-sublib-tabs { display:flex; gap:6px; flex-wrap:wrap; margin:0 0 10px; }
  .tl-sublib-tabs .tl-sublib-tab { width:auto; min-width:0; flex:0 0 auto; padding:6px 12px; font-size:11.5px; font-weight:600; text-transform:none; letter-spacing:0; background:var(--bg-input); border:1px solid var(--line); color:var(--muted); box-shadow:var(--sh-1); border-radius:var(--r-md); pointer-events:auto; }
  .tl-sublib-tabs .tl-sublib-tab::after { display:none; }
  .tl-sublib-tabs .tl-sublib-tab:hover { transform:none; border-color:var(--accent); color:var(--text); }
  .tl-sublib-tab.active { border-color:var(--accent); background:var(--accent-subtle); color:var(--text); }
  .tl-sfx-search { width:100%; margin:0 0 10px; padding:8px 11px; font-size:12.5px; }
  .tl-sfx-search { width:100%; margin-bottom:10px; position:sticky; top:0; }
  .tl-lib-body[hidden] { display:none; }
  .tl-lib-loading { color:var(--faint); font-size:13px; }
  .tl-lib-item { position:relative; border:1px solid var(--line); border-radius:var(--r-md); overflow:hidden; background:var(--bg-input); cursor:grab; }
  .tl-lib-item:hover { border-color:var(--accent); }
  .tl-lib-item.dragging { opacity:.5; }
  .tl-lib-item img, .tl-lib-item video { display:block; width:100%; aspect-ratio:9/14; object-fit:cover; background:var(--bg-base); pointer-events:none; }
  .tl-lib-item .tl-lib-name { padding:4px 7px; font-size:11px; color:var(--muted); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .tl-lib-item .tl-lib-proj { padding:0 7px 5px; font-size:9.5px; font-weight:700; color:var(--accent); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; text-transform:uppercase; letter-spacing:.03em; }
  .tl-lib-item .tl-lib-proj[data-current="1"] { color:var(--muted); }
  .tl-sublib-search { width:100%; margin:0 0 10px; padding:8px 11px; font-size:12.5px; box-sizing:border-box; }
  .tl-lib-sound { display:flex; align-items:center; gap:8px; padding:9px 11px; }
  .tl-lib-sound .tl-lib-ico { flex:0 0 auto; color:var(--accent); }
  .tl-lib-vidwrap { position:relative; }
  .tl-lib-play { position:absolute; top:50%; left:50%; transform:translate(-50%,-50%); width:34px; height:34px; min-width:0; padding:0; border-radius:50%; display:flex; align-items:center; justify-content:center; font-size:13px; color:#fff; background:rgba(0,0,0,.55); border:1.5px solid #fff; box-shadow:none; cursor:pointer; z-index:2; }
  .tl-lib-play::after { display:none; }
  .tl-lib-play:hover { background:var(--accent); }
  .tl-lib-play.playing { background:var(--accent); }
  .tl-snd-play { position:static; transform:none; flex:0 0 auto; width:30px; height:30px; font-size:12px; background:var(--accent-subtle); border:1px solid var(--accent); color:var(--text); }
  .tl-snd-play.playing { background:var(--accent); color:#fff; }
  .tl-track.drop-ok { outline:2px dashed var(--accent); outline-offset:-2px; background:var(--accent-subtle); }
  .tl-ctxmenu { position:fixed; z-index:9999; min-width:170px; background:var(--bg-raised); border:1px solid var(--line-strong); border-radius:var(--r-md); box-shadow:0 8px 28px rgba(0,0,0,.45); padding:5px; }
  .tl-ctxitem { display:block; width:100%; text-align:left; padding:8px 12px; font-size:13px; font-weight:600; color:var(--text); background:transparent; border:0; border-radius:var(--r-sm); box-shadow:none; cursor:pointer; }
  .tl-ctxitem::after { display:none; }
  .tl-ctxitem:hover { background:var(--accent-subtle); }
  .tl-replace-banner { display:none; margin:0 0 12px; padding:9px 12px; background:var(--accent-subtle); border:1px solid var(--accent); border-radius:var(--r-md); font-size:12.5px; font-weight:600; color:var(--text); }
  .tl-replace-banner button { width:auto; min-width:0; margin-left:10px; padding:4px 12px; font-size:11.5px; background:var(--bg-input); border:1px solid var(--line-strong); color:var(--text); box-shadow:none; }
  .tl-replace-banner button::after { display:none; }
  .tl-library.replace-arming .tl-lib-item { outline:1px dashed var(--accent); cursor:pointer; }
  @media (max-width:760px){ .tl-grid{ grid-template-columns:1fr; } .tl-stage-view{ height:auto; width:100%; max-width:300px; } }
</style>
<script>
(function(){
  var rootEl = document.getElementById('timeline-root');
  if (!rootEl) return;
  var model;
  try { model = JSON.parse(document.getElementById('timeline-model').textContent); } catch(e){ return; }
  var replaceLabel=document.getElementById('tl-rw-replace-label');
  var addMoreSfx=document.getElementById('tl-rw-add-sfx');
  if(addMoreSfx && !model.has_sfx){
    addMoreSfx.disabled=true;
    document.getElementById('tl-rw-add-sfx-wrap').title='Add more SFX becomes available after this timeline has at least one sound effect.';
  }
  var versionsOpen=document.getElementById('tl-versions-open');
  var versionsPop=document.getElementById('tl-versions-pop');
  function loadVersions(){
    fetch('/timeline-versions?slug='+encodeURIComponent(slug)).then(function(r){return r.json();}).then(function(d){
      var box=document.getElementById('tl-version-list'), rows=(d&&d.versions)||[];
      if(!rows.length){box.innerHTML='<span class="hint">No older versions yet.</span>';return;}
      box.innerHTML=''; rows.forEach(function(v){
        var b=document.createElement('button'); b.type='button'; b.className='button secondary tl-version-item';
        b.innerHTML=(v.reason||'Earlier timeline')+'<small>'+(v.created_at||'')+'</small>';
        b.addEventListener('click',function(){
          if(!confirm('Load this older timeline? Your current timeline will be saved as another version first.'))return;
          b.disabled=true; fetch('/timeline-version-restore',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({slug:slug,version_id:v.id})})
            .then(function(r){return r.json();}).then(function(x){if(x&&x.ok)location.reload();else{b.disabled=false;alert((x&&x.error)||'Could not restore version.');}})
            .catch(function(){b.disabled=false;alert('Could not restore version.');});
        }); box.appendChild(b);
      });
    });
  }
  versionsOpen.addEventListener('click',function(){versionsPop.hidden=!versionsPop.hidden;if(!versionsPop.hidden)loadVersions();});
  var redoSfxToggle=document.getElementById('tl-rw-redo-sfx');
  var redoCapToggle=document.getElementById('tl-rw-redo-captions');
  var reworkSfxAmount='medium';   // #113 - low/medium/high chosen when redo-SFX is turned on
  // #108 - gate the redo controls to what is actually editable in this project.
  var captionsEditableRW=(model.captions_editable!==false)
      && (model.caption_track||[]).filter(function(r){return r&&String(r.text||'').trim();}).length>0;
  if(redoCapToggle && !captionsEditableRW){
    redoCapToggle.checked=false; redoCapToggle.disabled=true;
    var _rcw=document.getElementById('tl-rw-redo-cap-wrap');
    if(_rcw){ _rcw.classList.add('tl-disabled'); _rcw.title='Redo captions needs an editable caption track. This project has none (captions are baked into the video).'; }
  }
  if(redoSfxToggle && model.sfx_removable===false){
    redoSfxToggle.checked=false; redoSfxToggle.disabled=true;
    var _rsw=document.getElementById('tl-rw-redo-sfx-wrap');
    if(_rsw){ _rsw.classList.add('tl-disabled'); _rsw.title='Redo SFX needs removable sound effects. This project\\u2019s SFX are baked into the video and can\\u2019t be stripped.'; }
  }
  // #113 - a small low/medium/high popup appears next to Redo SFX when it is switched on.
  function showSfxAmountPopup(anchor){
    document.querySelectorAll('.tl-amt-pop').forEach(function(p){ p.remove(); });
    var pop=document.createElement('div'); pop.className='tl-amt-pop';
    pop.innerHTML='<div class="tl-amt-lbl">How many SFX?</div><div class="tl-amt-row">'
      +['low','medium','high'].map(function(a){ return '<button type="button" class="tl-amt-btn'+(reworkSfxAmount===a?' on':'')+'" data-a="'+a+'">'+a.charAt(0).toUpperCase()+a.slice(1)+'</button>'; }).join('')+'</div>';
    document.body.appendChild(pop);
    var r=anchor.getBoundingClientRect();
    pop.style.top=Math.min(window.innerHeight-pop.offsetHeight-8, r.bottom+6)+'px';
    pop.style.left=Math.min(window.innerWidth-pop.offsetWidth-8, Math.max(8, r.left))+'px';
    pop.querySelectorAll('.tl-amt-btn').forEach(function(b){ b.onclick=function(){ reworkSfxAmount=b.getAttribute('data-a');
      pop.querySelectorAll('.tl-amt-btn').forEach(function(x){x.classList.remove('on');}); b.classList.add('on'); setTimeout(function(){pop.remove();},180); }; });
    setTimeout(function(){ document.addEventListener('pointerdown', function h(ev){ if(!pop.contains(ev.target)&&ev.target!==anchor){ pop.remove(); document.removeEventListener('pointerdown',h); } }); },0);
  }
  if(redoSfxToggle && addMoreSfx){
    redoSfxToggle.addEventListener('change',function(){ if(this.checked){ addMoreSfx.checked=false; if(redoCapToggle)redoCapToggle.checked=false; showSfxAmountPopup(this); } });
    addMoreSfx.addEventListener('change',function(){if(this.checked)redoSfxToggle.checked=false;});
  }
  if(redoCapToggle){
    redoCapToggle.addEventListener('change',function(){ if(this.checked){ if(redoSfxToggle)redoSfxToggle.checked=false; if(addMoreSfx)addMoreSfx.checked=false; } });
  }
  if(replaceLabel&&model.clip_source==='scrape') replaceLabel.textContent='search TikTok/X for selected clips';
  var reworkModelSelect=document.getElementById('tl-rw-model');
  if(reworkModelSelect) reworkModelSelect.dataset.reasoningValue=model.reasoning_mode||'';
  if(reworkModelSelect) reworkModelSelect.value=model.reasoning_model||'openai/gpt-5.5';
  var slug = rootEl.getAttribute('data-slug');
  var scenes = (model.scenes||[]).map(function(s){ return Object.assign({}, s); });
  var transitions = (model.transitions||[]).map(function(t){ return Object.assign({}, t); });
  var sfx = (model.sfx||[]).map(function(s){ return Object.assign({}, s); });
  var volumes = Object.assign({voice:1, music:0}, model.volumes||{});
  var captionsOn = !!model.captions;
  var voiceDuration = Math.max(0, +(model.voice_duration||0));   // #119 - voiceover length (s)
  var sfxOn = model.sfx_on !== false;   // master "render sound FX" switch; off = voice only
  var captionTrack = (model.caption_track||[]).filter(function(row){return row&&String(row.text||'').trim();});
  var captionMaxWords = Math.max(1, Math.min(6, +(model.caption_max_words||3)));
  var captionUppercase = model.caption_uppercase !== false;
  var renderUrl = model.render_url || '';   // kept for result links; live preview follows the editable timeline
  var SCALE = 70;
  var sel = null;
  var selectedClipIds = [];
  var clipSelectionAnchor = null;
  var markedReplace = {};   // scene id -> true when its media is marked for agent replacement
  var replacedMap = {};     // scene id -> {path,type,name} chosen via right-click Replace
  var replacePickId = null; // scene id awaiting a library pick for in-editor replace
  var dirty = false;        // unsaved edits
  var libSounds = [];       // library SFX (filled by setupLibrary; feeds the sound dropdown)

  // ---- snapshot-based undo/redo: every committed edit pushes the FULL editor state ----
  var hist = [], hIdx = -1;
  function snapshotState(){
    return JSON.stringify({scenes:scenes, transitions:transitions, sfx:sfx, volumes:volumes,
                           captionsOn:captionsOn, sfxOn:sfxOn, markedReplace:markedReplace, replacedMap:replacedMap});
  }
  function pushHistory(){
    var snap = snapshotState();
    if (hist[hIdx] === snap) return;
    hist = hist.slice(0, hIdx + 1);
    hist.push(snap); if (hist.length > 80) hist.shift();
    hIdx = hist.length - 1; syncHistButtons();
  }
  function restoreSnap(snap){
    try{
      var st = JSON.parse(snap);
      scenes = st.scenes; transitions = st.transitions; sfx = st.sfx;
      volumes = st.volumes; captionsOn = st.captionsOn; if(st.sfxOn!==undefined) sfxOn = st.sfxOn;
      markedReplace = st.markedReplace || {}; replacedMap = st.replacedMap || {};
      sel = null; selectedClipIds=[]; clipSelectionAnchor=null; showPane(null); layout();
      var ct = document.getElementById('tl-captions'); if (ct) ct.checked = captionsOn;
      var sfxt = document.getElementById('tl-sfx-toggle'); if (sfxt) sfxt.checked = sfxOn;
      dirty = true; var b = document.getElementById('tl-save'); if (b) b.classList.add('tl-unsaved');
      // the restored timeline may have a different clip at the playhead - resync the player
      if (typeof seekTo === 'function') seekTo(Math.min(clock, totalDur()));
    }catch(e){}
  }
  function undoEdit(){ if (hIdx > 0){ hIdx--; restoreSnap(hist[hIdx]); syncHistButtons(); } }
  function redoEdit(){ if (hIdx < hist.length - 1){ hIdx++; restoreSnap(hist[hIdx]); syncHistButtons(); } }
  function syncHistButtons(){
    var u = document.getElementById('tl-undo'), r = document.getElementById('tl-redo');
    if (u) u.disabled = hIdx <= 0;
    if (r) r.disabled = hIdx >= hist.length - 1;
  }
  function markDirty(){
    leaveRenderMode();
    dirty = true; var b=document.getElementById('tl-save'); if(b){ b.classList.add('tl-unsaved'); }
    pushHistory();
    if(typeof seekTo==='function')seekTo(Math.min(clock,totalDur()));
  }

  // ---- zoom: SCALE is variable; buttons, +/-, F and Ctrl+wheel re-layout ----
  // the MIN scale = fit-to-window, so you can never zoom out until the timeline is smaller than
  // the window (and there is no scrollbar - see .tl-scroll CSS).
  function fitScale(){
    var scroll = document.getElementById('tl-scroll');
    var avail = (scroll && scroll.clientWidth) ? scroll.clientWidth : 900;
    return Math.max(4, avail / (totalDur() || 1));
  }
  function setZoom(px){
    SCALE = Math.max(fitScale(), Math.min(300, px));
    layout();
  }
  function fitZoom(){ setZoom(fitScale()); }

  var clipsEl=document.getElementById('tl-clips'), ovEl=document.getElementById('tl-ovtrack'), capEl=document.getElementById('tl-captrack'),
      voiceEl=document.getElementById('tl-voice'),
      sfxEl=document.getElementById('tl-sfx'), trEl=document.getElementById('tl-sfx'),  // ONE merged SFX track
      ruler=document.getElementById('tl-ruler'),
      playhead=document.getElementById('tl-playhead'), overlayLayer=document.getElementById('tl-overlay-layer');

  function esc(t){ return String(t==null?'':t).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }
  function visible(){ return scenes.filter(function(s){ return !s.removed; }); }
  function totalDur(){ return visible().reduce(function(a,s){ return a+s.dur; },0); }
  function fmt(t){ t=Math.max(0,t); var m=Math.floor(t/60), s=Math.floor(t%60); return m+':'+(s<10?'0':'')+s; }
  function startOf(id){ var vis=visible(), acc=0; for(var i=0;i<vis.length;i++){ if(vis[i].id===id) return acc; acc+=vis[i].dur; } return null; }
  function overlayLabel(ov){ var k=String((ov&&ov.type)||'visual'); return k==='callout'?'Arrow':k==='arrows'?'Arrows':k.charAt(0).toUpperCase()+k.slice(1); }
  function overlayRecord(id){
    for(var i=0;i<scenes.length;i++){ var list=scenes[i].overlays||[]; for(var j=0;j<list.length;j++){ if(String(list[j].id)===String(id)) return {scene:scenes[i], overlay:list[j]}; } }
    return null;
  }
  function overlayDefaults(ov){
    var x=ov.editor_x, y=ov.editor_y, scale=ov.editor_scale;
    if(x==null||y==null){
      if(ov.type==='callout'||ov.type==='highlight'){ x=ov.cx==null?0.5:ov.cx; y=ov.cy==null?0.5:ov.cy; }
      else if(ov.type==='arrows' && ov.items && ov.items.length){ var xs=[],ys=[]; ov.items.forEach(function(a){xs.push(+a[0],+a[2]);ys.push(+a[1],+a[3]);}); x=xs.reduce(function(a,b){return a+b;},0)/xs.length; y=ys.reduce(function(a,b){return a+b;},0)/ys.length; }
      else { var w=+(ov.w||0),h=+(ov.h||0); x=+(ov.x==null?0.5:ov.x)+(w?w/2:0); y=+(ov.y==null?0.5:ov.y)+(h?h/2:0); }
    }
    var xn=Number(x),yn=Number(y),sn=Number(scale);
    return {x:Number.isFinite(xn)?Math.max(0,Math.min(1,xn)):0.5,
      y:Number.isFinite(yn)?Math.max(0,Math.min(1,yn)):0.5,
      scale:Number.isFinite(sn)&&sn>0?Math.max(.25,Math.min(3,sn)):1};
  }

  function layout(){
    var vis=visible(), total=totalDur();
    var _sc=document.getElementById('tl-scroll'); var _cw=(_sc&&_sc.clientWidth)?_sc.clientWidth:720;
    var width=Math.max(_cw, total*SCALE);   // never narrower than the window (no empty gap)
    [clipsEl,ovEl,capEl,voiceEl,sfxEl,ruler].forEach(function(el){ if(el) el.style.width=width+'px'; });
    ruler.innerHTML='';
    var step = total>40?10:5;
    for(var t=0;t<=total+0.01;t+=step){ var d=document.createElement('div'); d.className='tl-tick'; d.style.left=(t*SCALE)+'px'; d.textContent=fmt(t); ruler.appendChild(d); }
    clipsEl.innerHTML='';
    var x=0;
    vis.forEach(function(s, i){
      var w=s.dur*SCALE;
      var b=document.createElement('div');
      b.className='tl-clip'+(selectedClipIds.indexOf(s.id)!==-1?' selected':'')+(markedReplace[s.id]?' marked-replace':'')+((s.clip && +(s.source_trim||0) > +(model.default_source_trim||0)+0.01)?' trimmed-front':'');
      b.style.left=x+'px'; b.style.width=w+'px'; b.setAttribute('data-id', s.id);
      if(s.poster) b.style.backgroundImage='url('+s.poster+')';
      var badge=(s.clip?'\\u25B6 ':'')+(s.speaker?'\\uD83C\\uDFA4 ':'');
      var repTag=markedReplace[s.id]?'<span class="tl-replace-tag">REPLACE</span>':'';
      var markCls='tl-clip-mark'+(markedReplace[s.id]?' on':'');
      var leftHandle=s.clip?'<span class="tl-handle-l" title="Trim the clip start - cut off the front (CapCut-style)"></span>':'';
      b.innerHTML='<span class="'+markCls+'" title="Mark this media for the agent to replace"></span>'+leftHandle+'<span class="tl-badge">'+badge+s.dur.toFixed(1)+'s</span>'+repTag+'<span class="tl-clip-label">'+esc(s.label)+'</span><span class="tl-handle" title="Trim the clip end - cut off the back"></span>';
      (function(scn){ b.querySelector('.tl-clip-mark').addEventListener('pointerdown', function(ev){ ev.stopPropagation(); ev.preventDefault(); if(markedReplace[scn.id]) delete markedReplace[scn.id]; else markedReplace[scn.id]=true; layout(); markDirty(); }); })(s);
      b.querySelector('.tl-handle').addEventListener('pointerdown', function(ev){ ev.stopPropagation(); startResize(ev, s); });
      var lh=b.querySelector('.tl-handle-l'); if(lh) lh.addEventListener('pointerdown', function(ev){ ev.stopPropagation(); startTrimFront(ev, s); });
      b.addEventListener('pointerdown', function(ev){ if(ev.target.classList.contains('tl-handle')||ev.target.classList.contains('tl-handle-l')||ev.target.classList.contains('tl-clip-mark')) return; startClipDrag(ev, s, b); });
      (function(scn){ b.addEventListener('contextmenu', function(ev){ ev.preventDefault(); ev.stopPropagation(); openClipMenu(ev.clientX, ev.clientY, scn); }); })(s);
      if(replacedMap[s.id]){ var rt=document.createElement('span'); rt.className='tl-replace-tag'; rt.style.background='var(--accent)'; rt.textContent='REPLACED'; b.appendChild(rt); }
      clipsEl.appendChild(b);
      if(i>0){ var seam=document.createElement('div'); seam.className='tl-seam'; seam.style.left=x+'px'; clipsEl.appendChild(seam); }
      x+=w;
    });
    ovEl.innerHTML='';
    vis.forEach(function(s){
      var sceneStart=startOf(s.id)||0;
      (s.overlays||[]).forEach(function(ov){
        var st=Math.max(0,Math.min(1,+(ov.start==null?0:ov.start)));
        var en=Math.max(st+.04,Math.min(1,+(ov.end==null?1:ov.end)));
        var node=document.createElement('div');
        node.className='tl-ovitem'+(sel&&sel.type==='overlay'&&String(sel.id)===String(ov.id)?' selected':'');
        node.style.left=((sceneStart+st*s.dur)*SCALE)+'px';
        node.style.width=Math.max(42,(en-st)*s.dur*SCALE)+'px';
        node.textContent='\u279C '+overlayLabel(ov); node.title='Select; right-click to open the inspector or delete';
        node.addEventListener('pointerdown',function(ev){ev.stopPropagation();selectOverlay(ov.id,s.id);});
        (function(o){ node.addEventListener('contextmenu',function(ev){ev.preventDefault();ev.stopPropagation();openOverlayMenu(ev.clientX,ev.clientY,o,s.id);}); })(ov);
        ovEl.appendChild(node);
      });
    });
    sfxEl.innerHTML='';                          // ONE track: transitions + sound FX together
    vis.forEach(function(s, i){
      if(i===0) return;                          // no boundary before the first clip
      var atSec=startOf(s.id), bx=atSec*SCALE;
      // hover-to-reveal "+" that adds a transition sound exactly at this cut
      var slot=document.createElement('div');
      slot.className='tl-trans-slot'; slot.style.left=bx+'px';
      slot.title='Add a transition sound at this cut';
      slot.innerHTML='<span class="tl-trans-add">+</span>';
      var addButton=slot.querySelector('.tl-trans-add');
      (function(at, slotEl, button){ button.addEventListener('click', function(ev){ ev.stopPropagation(); openTransMenu(ev.clientX, ev.clientY, at, slotEl); }); })(atSec, slot, addButton);
      trEl.appendChild(slot);
      // the existing transition diamond, only when a transition record exists for this boundary
      var tr=transitions.filter(function(t){ return t.scene_id===s.id; })[0];
      if(tr){
        var node=document.createElement('div');
        node.className='tl-trans'+(sel&&sel.type==='trans'&&sel.id===tr.id?' selected':'')+(tr.enabled===false?' disabled':'');
        node.style.left=bx+'px'; node.style.top='50%'; node.title='Transition between clips';
        node.innerHTML='<span class="tl-diamond"></span>';
        node.addEventListener('pointerdown', function(ev){ ev.stopPropagation(); selectTrans(tr.id); });
        (function(effect){ node.addEventListener('contextmenu', function(ev){ ev.preventDefault(); ev.stopPropagation(); openFxMenu(ev.clientX,ev.clientY,effect,'trans'); }); })(tr);
        trEl.appendChild(node);
      }
    });
    sfx.forEach(function(fx){                     // appended onto the SAME merged track (no re-clear)
      if(fx.deleted) return;
      var at=fxAbsStart(fx);
      if(at===null) return;
      var node=document.createElement('div');
      var isStart=at<=0.001;
      node.className='tl-fx'+(isStart?' at-start':'')+(sel&&sel.type==='fx'&&sel.id===fx.id?' selected':'')+(fx.enabled===false?' disabled':'');
      // Keep a true 0.00s event at 0.00s, but inset its block by two pixels so the track
      // border/playhead cannot visually cover its leading edge.
      node.style.left=((at*SCALE)+(isStart?2:0))+'px'; node.style.width=Math.max(60,(fx.duration||0.5)*SCALE)+'px';
      node.innerHTML='\\u266A '+esc(fx.label);
      node.title=(fx.label||'Sound')+' @ '+Number(at).toFixed(2)+'s';
      node.addEventListener('pointerdown', function(ev){ ev.stopPropagation(); startFxDrag(ev, fx, node); });
      (function(effect){ node.addEventListener('contextmenu', function(ev){ ev.preventDefault(); ev.stopPropagation(); openFxMenu(ev.clientX,ev.clientY,effect,'fx'); }); })(fx);
      sfxEl.appendChild(node);
    });
    // Captions timeline track removed (useless clutter) - captions still render, they just
    // aren't drawn as their own lane here. capEl is null now; guard every access.
    if(capEl){
      capEl.innerHTML=''; capEl.classList.toggle('off',!captionsOn);
      captionTrack.forEach(function(row){
        var st=Math.max(0,+(row.start||0)), en=Math.min(total,Math.max(st+.05,+(row.end||st)));
        if(st>=total||en<=st)return;
        var item=document.createElement('div');item.className='tl-capitem';
        item.style.left=(st*SCALE)+'px';item.style.width=Math.max(22,(en-st)*SCALE)+'px';
        item.textContent=String(row.text||'Caption');
        item.title=(captionsOn?'Caption':'Caption excluded from render')+' @ '+st.toFixed(2)+'s: '+String(row.text||'');
        capEl.appendChild(item);
      });
    }
    // #119 - the voice bar spans the actual voiceover length so clip overrun/underrun is visible
    voiceEl.innerHTML='';
    var _vb=document.createElement('div'); _vb.className='tl-audbar';
    if(voiceDuration>0.01){ _vb.style.right='auto'; _vb.style.width=Math.max(2,voiceDuration*SCALE)+'px'; _vb.title='Voiceover: '+voiceDuration.toFixed(1)+'s'; }
    voiceEl.appendChild(_vb);
    var _ve=document.getElementById('tl-voice-end');
    if(_ve){
      if(voiceDuration>0.01){
        _ve.hidden=false; _ve.style.left=(voiceDuration*SCALE)+'px';
        var _over=total-voiceDuration;
        var _lbl=_ve.querySelector('.tl-voice-end-lbl');
        if(_lbl) _lbl.textContent='\\uD83C\\uDFA4 '+voiceDuration.toFixed(1)+'s';
        _ve.classList.toggle('tl-voice-mismatch', Math.abs(_over)>0.2);
        _ve.title='Voiceover ends at '+voiceDuration.toFixed(1)+'s'+(Math.abs(_over)>0.2?(' \\u2014 clips '+(_over>0?'overrun by ':'underrun by ')+Math.abs(_over).toFixed(1)+'s'):' (clips match the voice)');
      } else { _ve.hidden=true; }
    }
    document.getElementById('tl-total').textContent='Total '+fmt(total)+'  -  '+vis.length+' clips';
    // "Sound FX" off -> dim the SFX + Cut-SFX rows so it's clear they won't be in the render
    if(sfxEl) sfxEl.classList.toggle('tl-muted', !sfxOn);
    if(trEl) trEl.classList.toggle('tl-muted', !sfxOn);
    updatePlayhead();
  }

  // #117 - a clip can only stretch up to the footage it actually has (from its current in-point),
  // so dragging it wider never freezes on the last frame. Images / unknown lengths keep the 20s cap.
  function clipMaxDur(s){
    if(s && s.clip && +(s.source_full||0) > 0.01){
      var avail=(+(s.source_full) - +(s.source_trim||0)) / Math.max(0.01, +(s.source_speed||1));
      return Math.max(0.5, +avail.toFixed(2));
    }
    return 20;
  }
  function startResize(ev, s){
    ev.preventDefault();
    var startX=ev.clientX, startDur=s.dur, maxD=clipMaxDur(s);
    function move(e){ var dd=(e.clientX-startX)/SCALE; s.dur=Math.max(0.5, Math.min(maxD, +(startDur+dd).toFixed(1))); layout(); if(sel&&sel.type==='clip'&&sel.id===s.id) syncInspector(); }
    function up(){
      document.removeEventListener('pointermove',move); document.removeEventListener('pointerup',up);
      if(s.dur!==startDur) markDirty();      // one history entry per resize gesture
    }
    document.addEventListener('pointermove',move); document.addEventListener('pointerup',up);
  }

  // CapCut-style "cut front": drag the LEFT edge to trim the clip's source in-point. The out-point
  // is kept fixed (source_trim grows, dur shrinks by the same amount), so following clips ripple.
  function startTrimFront(ev, s){
    ev.preventDefault();
    var startX=ev.clientX, startTrim=+(s.source_trim||0), startDur=s.dur, rate=Math.max(.01,+(s.source_speed||1)), changed=false;
    function move(e){
      var dt=(e.clientX-startX)/SCALE;             // timeline seconds the left edge moved right
      var maxDt=startDur-0.5;                       // keep >=0.5s of the clip
      var minDt=-(startTrim/rate);                  // can't pull the in-point before the source start
      dt=Math.max(minDt, Math.min(maxDt, dt));
      s.source_trim=+((startTrim+dt*rate).toFixed(3));
      s.dur=+((startDur-dt).toFixed(2));
      changed=true; layout();
      // show the new first frame of this clip while dragging
      if(!RENDER_MODE){ clock=startOf(s.id)||0; showScene(sceneAt(clock),true); updatePlayhead(); }
      if(sel&&sel.type==='clip'&&sel.id===s.id) syncInspector();
    }
    function up(){
      document.removeEventListener('pointermove',move); document.removeEventListener('pointerup',up);
      if(changed) markDirty();                      // one history entry per trim gesture
    }
    document.addEventListener('pointermove',move); document.addEventListener('pointerup',up);
  }

  function startFxDrag(ev, fx, node){
    ev.preventDefault();
    var startX=ev.clientX, startAbs=fxAbsStart(fx)||0, dragging=false;
    function mv(e){
      if(!dragging && Math.abs(e.clientX-startX) < 5) return;
      dragging=true;
      var t=Math.max(0, startAbs + (e.clientX-startX)/SCALE);
      if(fx.added){
        var vis=visible(), acc=0, sid=vis.length?vis[0].id:fx.scene_id;
        for(var i=0;i<vis.length;i++){ if(t < acc+vis[i].dur){ sid=vis[i].id; break; } acc+=vis[i].dur; if(i===vis.length-1){ sid=vis[i].id; } }
        fx.scene_id=sid; fx.offset=Math.max(0, t-startOf(sid));
      } else {
        fx.start_abs=t;
      }
      node.style.left=(t*SCALE)+'px';
    }
    function up(){
      document.removeEventListener('pointermove',mv); document.removeEventListener('pointerup',up);
      if(dragging){ layout(); markDirty(); selectFx(fx.id); }
      else selectFx(fx.id);
    }
    document.addEventListener('pointermove',mv); document.addEventListener('pointerup',up);
  }

  // Add a library sound as a new SFX at an absolute timeline position (used by drops + the
  // per-cut transition "+" popup). Keeps the sound scene-relative so the backend resolves it
  // through the same added:true / custom_sfx round-trip as a dropped sound.
  function addSfxAt(it, atSec, opts){
    opts=opts||{};
    var vis=visible(), best=vis.length?vis[0].id:null, acc=0;
    for(var i=0;i<vis.length;i++){ if(atSec < acc+vis[i].dur){ best=vis[i].id; break; } acc+=vis[i].dur; }
    if(best===null) return null;
    var off=Math.max(0, atSec-(startOf(best)||0));
    var nf={ id:'sfx-'+Date.now(), scene_id:best, label:it.name, category:it.category||opts.category||'transition',
             offset:+off.toFixed(2), duration:it.duration||1.0, volume:opts.volume==null?0.3:opts.volume,
             enabled:true, added:true, path:it.path, url:it.url||'' };
    sfx.push(nf); layout(); markDirty();
    if(opts.select!==false) selectFx(nf.id);
    return nf;
  }

  // The per-cut "add transition sound" popup: transition-ish sounds ranked first, each previewable.
  var transMenuEl=null;
  function closeTransMenu(){
    if(transMenuEl){ transMenuEl.remove(); transMenuEl=null; }
    var open=document.querySelector('.tl-trans-slot.menu-open'); if(open) open.classList.remove('menu-open');
    document.removeEventListener('pointerdown', transMenuOutside, true);
    document.removeEventListener('keydown', transMenuKey, true);
  }
  function transMenuOutside(ev){ if(transMenuEl && !transMenuEl.contains(ev.target)) closeTransMenu(); }
  function transMenuKey(ev){ if(ev.key==='Escape') closeTransMenu(); }
  function openTransMenu(x, y, atSec, slotEl, opts){
    opts=opts||{};
    closeTransMenu();
    if(slotEl) slotEl.classList.add('menu-open');
    var rankKeys=['whoosh','transition','swish','swoosh','swoop','riser','impact','hit','boom','cut','swipe','whip','woosh'];
    function score(it){ var c=((it.category||'')+' '+(it.name||'')).toLowerCase(); for(var k=0;k<rankKeys.length;k++){ if(c.indexOf(rankKeys[k])!==-1) return k; } return 999; }
    var list=libSounds.map(function(it,idx){ return {it:it,idx:idx,sc:score(it)}; }).sort(function(a,b){ return a.sc-b.sc || a.idx-b.idx; });
    var menu=document.createElement('div'); menu.className='tl-trans-menu';
    menu.innerHTML='<div class="tl-tm-title">'+esc(opts.title||'Add transition sound')+' @ '+Number(atSec).toFixed(2)+'s</div>';
    if(!list.length){ menu.innerHTML+='<div class="tl-tm-empty">No sounds loaded yet - open the Sound&nbsp;FX library once.</div>'; }
    list.forEach(function(row){
      var it=row.it, item=document.createElement('div'); item.className='tl-tm-item';
      item.innerHTML='<span class="tl-tm-play" title="Preview">\\u25B6</span><span class="tl-tm-name">'+esc(it.name)+'</span>';
      item.querySelector('.tl-tm-play').addEventListener('click', function(ev){ ev.stopPropagation(); if(!it.url) return; try{ var a=new Audio(it.url); a.volume=.5; a.play().catch(function(){}); }catch(e){} });
      item.addEventListener('click', function(){
        if(opts.replaceFx){
          var fx=opts.replaceFx;
          fx.label=it.name;fx.path=it.path;fx.path_override=it.path;fx.url=it.url||'';
          fx.duration=it.duration||fx.duration||1;fx.enabled=true;fx.deleted=false;
          layout();markDirty();if(opts.replaceType==='trans')selectTrans(fx.id);else selectFx(fx.id);
        }else addSfxAt(it, atSec, {category:opts.category||'transition'});
        closeTransMenu();
      });
      menu.appendChild(item);
    });
    document.body.appendChild(menu);
    var mw=menu.offsetWidth, mh=menu.offsetHeight;
    menu.style.left=Math.max(8, Math.min(x, window.innerWidth-mw-8))+'px';
    menu.style.top=Math.max(8, Math.min(y+10, window.innerHeight-mh-8))+'px';
    transMenuEl=menu;
    setTimeout(function(){ document.addEventListener('pointerdown', transMenuOutside, true); document.addEventListener('keydown', transMenuKey, true); }, 0);
  }

  function startClipDrag(ev, s, block){
    ev.preventDefault();
    var startX=ev.clientX, dragging=false, rangeSelect=!!ev.shiftKey;
    function move(e){
      if(!dragging && Math.abs(e.clientX-startX) < 6) return;
      dragging=true; block.classList.add('dragging');
      var rect=clipsEl.getBoundingClientRect();
      var px=e.clientX-rect.left+clipsEl.scrollLeft;
      var vis=visible(), acc=0, target=vis.length-1;
      for(var i=0;i<vis.length;i++){ var w=vis[i].dur*SCALE; if(px < acc+w/2){ target=i; break; } acc+=w; if(i===vis.length-1) target=vis.length-1; }
      var order=scenes.filter(function(x){return !x.removed;});
      var from=order.indexOf(s);
      if(from!==-1 && from!==target){
        order.splice(from,1); order.splice(target,0,s);
        scenes=order.concat(scenes.filter(function(x){return x.removed;}));
        layout(); block=clipsEl.querySelector('.tl-clip[data-id="'+s.id+'"]'); if(block) block.classList.add('dragging');
      }
    }
    function up(e){
      document.removeEventListener('pointermove',move); document.removeEventListener('pointerup',up);
      var b=clipsEl.querySelector('.tl-clip[data-id="'+s.id+'"]'); if(b) b.classList.remove('dragging');
      if(!dragging){ selectClip(s.id,rangeSelect); if(!rangeSelect&&e)seekFromClientX(e.clientX); }
      else markDirty();                     // one history entry per reorder gesture
    }
    document.addEventListener('pointermove',move); document.addEventListener('pointerup',up);
  }

  // #118 - rubberband: drag a box across the empty clips track to multi-select clips. The
  // existing Delete key / "Remove clip" already act on ALL selectedClipIds (delete together).
  function startRubberband(ev){
    if(ev.button!==0) return;
    var rect=clipsEl.getBoundingClientRect();
    var x0=ev.clientX, y0=ev.clientY, band=null, moved=false;
    function mv(e){
      if(!moved && Math.abs(e.clientX-x0)<5 && Math.abs(e.clientY-y0)<5) return;
      moved=true;
      if(!band){ band=document.createElement('div'); band.className='tl-rubber'; clipsEl.appendChild(band); }
      var left=Math.min(x0,e.clientX)-rect.left+clipsEl.scrollLeft, wid=Math.abs(e.clientX-x0);
      band.style.left=left+'px'; band.style.width=wid+'px';
      var lo=left, hi=left+wid, vis=visible(), acc=0, ids=[];
      for(var i=0;i<vis.length;i++){ var w=vis[i].dur*SCALE; if(acc+w>lo && acc<hi) ids.push(vis[i].id); acc+=w; }
      selectedClipIds=ids; clipSelectionAnchor=ids.length?ids[0]:null;
      Array.prototype.forEach.call(clipsEl.querySelectorAll('.tl-clip'),function(b){ b.classList.toggle('selected', ids.indexOf(b.getAttribute('data-id'))!==-1); });
    }
    function up(){
      document.removeEventListener('pointermove',mv); document.removeEventListener('pointerup',up);
      if(band) band.remove();
      if(moved && selectedClipIds.length){ sel={type:'clip',id:selectedClipIds[0]}; showPane('clip'); syncInspector(); }
    }
    document.addEventListener('pointermove',mv); document.addEventListener('pointerup',up);
  }
  clipsEl.addEventListener('pointerdown', function(ev){ if(ev.target===clipsEl) startRubberband(ev); });

  function showPane(which){
    // Only syncs WHICH pane's content is prepared. The inspector is a floating popup that is
    // only shown via an explicit right-click -> "Open inspector" (openInspectorAt). Selecting
    // (left-click) never pops it open. Deselecting (which=null) closes it.
    document.getElementById('tl-insp-empty').hidden = !!which;
    document.getElementById('tl-insp-clip').hidden = which!=='clip';
    document.getElementById('tl-insp-fx').hidden = which!=='fx';
    document.getElementById('tl-insp-overlay').hidden = which!=='overlay';
    var insp=document.getElementById('tl-inspector');
    if(insp && !which) insp.classList.remove('open');
  }
  var _lastPointer={x:null,y:null};
  document.addEventListener('pointerdown', function(ev){ _lastPointer={x:ev.clientX,y:ev.clientY}; }, true);
  function openInspectorAt(x,y){
    var insp=document.getElementById('tl-inspector'); if(!insp) return;
    if(x==null) x=_lastPointer.x; if(y==null) y=_lastPointer.y;
    insp.classList.add('open');
    var w=insp.offsetWidth||320, h=insp.offsetHeight||320;
    var left=(x!=null)? x+14 : (window.innerWidth-w-16);
    var top =(y!=null)? y-10 : 72;
    insp.style.left=Math.max(8, Math.min(left, window.innerWidth-w-8))+'px';
    insp.style.top =Math.max(8, Math.min(top,  window.innerHeight-h-8))+'px';
    insp.style.right='auto';
  }
  function selectClip(id,rangeSelect){
    var vis=visible();
    if(rangeSelect&&clipSelectionAnchor!==null){
      var a=vis.map(function(s){return s.id;}).indexOf(clipSelectionAnchor), b=vis.map(function(s){return s.id;}).indexOf(id);
      if(a>=0&&b>=0){var lo=Math.min(a,b),hi=Math.max(a,b);selectedClipIds=vis.slice(lo,hi+1).map(function(s){return s.id;});}
      else selectedClipIds=[id];
    }else{selectedClipIds=[id];clipSelectionAnchor=id;}
    sel={type:'clip',id:id}; layout(); syncInspector();
    if(!rangeSelect&&!playing)previewSceneById(id);
  }
  function clearClipSelection(){selectedClipIds=[];clipSelectionAnchor=null;}
  function selectTrans(id){ clearClipSelection(); sel={type:'trans',id:id}; layout(); syncFx(); var fx=currentFx(),at=fx?trAbsStart(fx):null;if(at!==null)seekTo(at); }
  function selectFx(id){ clearClipSelection(); sel={type:'fx',id:id}; layout(); syncFx(); var fx=currentFx(),at=fx?fxAbsStart(fx):null;if(at!==null)seekTo(at); }
  function selectOverlay(id, sceneId){
    clearClipSelection();
    sel={type:'overlay',id:String(id),scene_id:String(sceneId)};
    leaveRenderMode();
    var rec=overlayRecord(id),at=startOf(String(sceneId));
    if(rec&&at!==null)at+=Math.max(0,Math.min(1,+(rec.overlay.start||0)))*rec.scene.dur;
    if(at!==null)seekTo(at);else previewSceneById(String(sceneId));
    layout(); syncOverlayInspector();
  }

  function syncInspector(){
    var chosen=scenes.filter(function(x){return !x.removed&&selectedClipIds.indexOf(x.id)!==-1;});
    var s=scenes.filter(function(x){return x.id===(sel&&sel.id);})[0];
    if(!s||s.removed||!chosen.length){ sel=null;clearClipSelection();showPane(null);return; }
    showPane('clip');
    document.getElementById('tl-insp-name').textContent=chosen.length>1?(chosen.length+' clips selected'):s.label;
    var durInput=document.getElementById('tl-insp-dur'); durInput.disabled=chosen.length>1; durInput.value=chosen.length>1?'':s.dur;
    durInput.max=clipMaxDur(s);   // #117 - reflect the per-clip stretch ceiling in the number field
    var durHint=document.getElementById('tl-insp-note');
    if(durHint){ var cm=clipMaxDur(s); durHint.textContent=(s.clip&&+(s.source_full||0)>0.01)
      ? ('This clip has '+(+s.source_full).toFixed(1)+'s of footage - it can stretch to at most '+cm.toFixed(1)+'s (no freeze frames).')
      : 'Tip: click the red strip on top of a clip to mark its media for the agent to replace.'; }
    var sp=s.speed||1;
    document.getElementById('tl-insp-speed').value=sp;
    var mixed=chosen.some(function(x){return Math.abs((x.speed||1)-sp)>.001;});
    document.getElementById('tl-insp-speed-val').textContent=mixed?('Mixed \u2192 '+sp.toFixed(2)+'x'):sp.toFixed(2)+'x';
    document.getElementById('tl-insp-remove').textContent=chosen.length>1?('Remove '+chosen.length+' clips'):'Remove clip';
    // per-media caption blur: only video clips can be blurred
    var blurRow=document.getElementById('tl-insp-blur-row'), blurHint=document.getElementById('tl-insp-blur-hint');
    var canBlur=chosen.every(function(x){return !!x.clip;});
    blurRow.hidden=!canBlur; blurHint.hidden=!canBlur;
    var bc=document.getElementById('tl-insp-blurcap');
    bc.checked=!!s.blur_captions;
    bc.indeterminate=chosen.some(function(x){return !!x.blur_captions!==!!s.blur_captions;});
  }
  function currentFx(){
    if(!sel) return null;
    if(sel.type==='trans') return transitions.filter(function(t){return t.id===sel.id;})[0];
    if(sel.type==='fx') return sfx.filter(function(f){return f.id===sel.id;})[0];
    return null;
  }
  function currentOverlay(){ return sel&&sel.type==='overlay' ? overlayRecord(sel.id) : null; }
  function syncOverlayInspector(){
    var rec=currentOverlay(); if(!rec){sel=null;showPane(null);renderPreviewOverlays(null);return;}
    showPane('overlay'); var d=overlayDefaults(rec.overlay);
    rec.overlay.editor_x=d.x; rec.overlay.editor_y=d.y; rec.overlay.editor_scale=d.scale;
    var isArrow=rec.overlay.type==='callout'||rec.overlay.type==='arrows';
    document.getElementById('tl-ov-arrow-controls').hidden=!isArrow;
    document.getElementById('tl-ov-name').textContent=overlayLabel(rec.overlay)+' - '+(rec.scene.label||'scene');
    document.getElementById('tl-ov-x').value=d.x; document.getElementById('tl-ov-x-val').textContent=Math.round(d.x*100)+'%';
    document.getElementById('tl-ov-y').value=d.y; document.getElementById('tl-ov-y-val').textContent=Math.round(d.y*100)+'%';
    document.getElementById('tl-ov-scale').value=d.scale; document.getElementById('tl-ov-scale-val').textContent=Math.round(d.scale*100)+'%';
    var rotation=Math.max(-180,Math.min(180,+(rec.overlay.editor_rotation||0)));
    document.getElementById('tl-ov-rotation').value=rotation; document.getElementById('tl-ov-rotation-val').textContent=Math.round(rotation)+'\u00B0';
    document.getElementById('tl-ov-style').value=rec.overlay.arrow_style||'default_thick_red_arrow';
    document.getElementById('tl-ov-animation').value=rec.overlay.animation||'pop';
    var animDuration=Math.max(.1,Math.min(1.5,+(rec.overlay.animation_duration||.28)));
    document.getElementById('tl-ov-animation-duration').value=animDuration; document.getElementById('tl-ov-animation-duration-val').textContent=animDuration.toFixed(2)+'s';
    var sfxVolume=rec.overlay.appear_sfx_volume==null?.22:Math.max(0,Math.min(.6,+rec.overlay.appear_sfx_volume));
    document.getElementById('tl-ov-sfx-volume').value=sfxVolume; document.getElementById('tl-ov-sfx-volume-val').textContent=Math.round(sfxVolume*100)+'%';
    var sndSel=document.getElementById('tl-ov-sfx');
    sndSel.innerHTML='<option value="">None</option>'+libSounds.map(function(s,i){return '<option value="'+i+'">'+esc(s.name)+'</option>';}).join('');
    var soundIndex=-1; libSounds.forEach(function(s,i){if(String(s.path)===String(rec.overlay.appear_sfx_path||''))soundIndex=i;});
    sndSel.value=soundIndex>=0?String(soundIndex):'';
    renderPreviewOverlays(rec.scene);
  }
  function deleteCurrentOverlay(){
    var rec=currentOverlay(); if(!rec)return;
    rec.scene.overlays=(rec.scene.overlays||[]).filter(function(ov){return String(ov.id)!==String(rec.overlay.id);});
    sel=null; showPane(null); renderPreviewOverlays(rec.scene); layout(); markDirty();
  }
  function syncFx(){
    var fx=currentFx();
    if(!fx){ sel=null; showPane(null); return; }
    showPane('fx');
    document.getElementById('tl-fx-name').textContent=(sel.type==='trans'?'Cut SFX: ':'Sound: ')+(fx.label||'');
    document.getElementById('tl-fx-enabled').checked = fx.enabled!==false;
    var v=fx.volume||0; document.getElementById('tl-fx-vol').value=v;
    document.getElementById('tl-fx-vol-val').textContent=Math.round(v*100)+'%';
    var abs = (sel.type==='trans') ? trAbsStart(fx) : fxAbsStart(fx);
    document.getElementById('tl-fx-time').value = abs===null ? '' : abs.toFixed(2);
    var trim=Math.max(0,+(fx.source_trim||0));
    document.getElementById('tl-fx-trim').value = trim ? trim.toFixed(2) : '';
    document.getElementById('tl-fx-trim-val').textContent = trim>0 ? ('-'+trim.toFixed(2)+'s') : '';
    var sndSel=document.getElementById('tl-fx-sound');
    sndSel.innerHTML='<option value="">(keep current)</option>'+libSounds.map(function(s,i){
      return '<option value="'+i+'">'+esc(s.name)+'</option>'; }).join('');
    document.getElementById('tl-fx-note').textContent = sel.type==='trans'
      ? 'Sound-only cut effect - the video itself stays a hard cut.' : '';
  }
  function deleteCurrentFx(){
    var fx=currentFx();
    if(!fx) return;
    if(fx.added){
      sfx = sfx.filter(function(f){ return f.id!==fx.id; });
    } else {
      fx.enabled=false; fx.deleted=true;   // persisted as enabled:false -> removed from mix
    }
    sel=null; showPane(null); layout(); markDirty();
  }

  document.getElementById('tl-insp-dur').addEventListener('input', function(){ if(selectedClipIds.length!==1)return; var s=scenes.filter(function(x){return x.id===selectedClipIds[0];})[0]; if(s){ s.dur=Math.max(0.5,Math.min(clipMaxDur(s), parseFloat(this.value)||s.dur)); layout(); markDirty(); } });
  document.getElementById('tl-insp-speed').addEventListener('input', function(){
    var speed=Math.max(0.5,Math.min(2,parseFloat(this.value)||1));
    var chosen=scenes.filter(function(x){return !x.removed&&selectedClipIds.indexOf(x.id)!==-1;});
    if(chosen.length){ chosen.forEach(function(s){s.speed=speed;});
      document.getElementById('tl-insp-speed-val').textContent=speed.toFixed(2)+'x';
      if(!RENDER_MODE&&activeSceneInfo)showScene(sceneAt(clock),true);
      markDirty(); }
  });
  document.getElementById('tl-insp-remove').addEventListener('click', function(){ var chosen=scenes.filter(function(x){return selectedClipIds.indexOf(x.id)!==-1;}); if(chosen.length){ chosen.forEach(function(s){s.removed=true;}); sel=null;clearClipSelection();layout();showPane(null);markDirty(); } });
  document.getElementById('tl-insp-blurcap').addEventListener('change', function(){
    var on=this.checked;
    var chosen=scenes.filter(function(x){return !x.removed&&selectedClipIds.indexOf(x.id)!==-1&&x.clip;});
    if(chosen.length){ chosen.forEach(function(s){ s.blur_captions=on; }); this.indeterminate=false; markDirty(); }
  });
  document.getElementById('tl-fx-enabled').addEventListener('change', function(){ var fx=currentFx(); if(fx){ fx.enabled=this.checked; layout(); markDirty(); } });
  document.getElementById('tl-fx-vol').addEventListener('input', function(){ var fx=currentFx(); if(fx){ fx.volume=parseFloat(this.value); document.getElementById('tl-fx-vol-val').textContent=Math.round(fx.volume*100)+'%'; layout(); markDirty(); } });
  document.getElementById('tl-fx-trim').addEventListener('change', function(){
    var fx=currentFx(); if(!fx) return;
    var trim=Math.max(0, parseFloat(this.value)||0);
    fx.source_trim=+trim.toFixed(3);
    document.getElementById('tl-fx-trim-val').textContent = trim>0 ? ('-'+trim.toFixed(2)+'s') : '';
    markDirty();
  });
  document.getElementById('tl-fx-time').addEventListener('change', function(){
    var fx=currentFx(); if(!fx) return;
    var t=Math.max(0, parseFloat(this.value)||0);
    if(fx.added){
      // keep added sounds scene-relative so the backend resolves them the same way
      var vis=visible(), acc=0, sid=vis.length?vis[0].id:fx.scene_id;
      for(var i=0;i<vis.length;i++){ if(t < acc+vis[i].dur){ sid=vis[i].id; break; } acc+=vis[i].dur; if(i===vis.length-1){ sid=vis[i].id; } }
      fx.scene_id=sid; fx.offset=Math.max(0, t-startOf(sid));
    } else {
      fx.start_abs=t;
    }
    layout(); markDirty();
  });
  document.getElementById('tl-fx-sound').addEventListener('change', function(){
    var fx=currentFx(); if(!fx) return;
    var i=parseInt(this.value,10);
    if(isNaN(i) || !libSounds[i]) return;
    var snd=libSounds[i];
    fx.path_override=snd.path; fx.url=snd.url; fx.label=snd.name;
    if(fx.added){ fx.path=snd.path; }
    layout(); syncFx(); markDirty();
  });
  document.getElementById('tl-fx-preview').addEventListener('click', function(){
    var fx=currentFx(); if(!fx||!fx.url) return;
    try{ var a=new Audio(fx.url); a.volume=Math.max(0,Math.min(1,fx.volume||0.3)); a.playbackRate=Math.max(.25,Math.min(4,+(fx.playback_rate||1))); a.currentTime=Math.max(0,+(fx.source_trim||0)); a.play().catch(function(){}); }catch(e){}
  });
  document.getElementById('tl-fx-delete').addEventListener('click', deleteCurrentFx);
  function bindOverlayRange(id,key,out,fmtValue){
    document.getElementById(id).addEventListener('input',function(){
      var rec=currentOverlay(); if(!rec)return; rec.overlay[key]=parseFloat(this.value);
      document.getElementById(out).textContent=fmtValue(rec.overlay[key]);
      renderPreviewOverlays(rec.scene); markDirty();
    });
  }
  bindOverlayRange('tl-ov-x','editor_x','tl-ov-x-val',function(v){return Math.round(v*100)+'%';});
  bindOverlayRange('tl-ov-y','editor_y','tl-ov-y-val',function(v){return Math.round(v*100)+'%';});
  bindOverlayRange('tl-ov-scale','editor_scale','tl-ov-scale-val',function(v){return Math.round(v*100)+'%';});
  bindOverlayRange('tl-ov-rotation','editor_rotation','tl-ov-rotation-val',function(v){return Math.round(v)+'\u00B0';});
  bindOverlayRange('tl-ov-animation-duration','animation_duration','tl-ov-animation-duration-val',function(v){return v.toFixed(2)+'s';});
  bindOverlayRange('tl-ov-sfx-volume','appear_sfx_volume','tl-ov-sfx-volume-val',function(v){return Math.round(v*100)+'%';});
  document.getElementById('tl-ov-style').addEventListener('change',function(){var rec=currentOverlay();if(rec){rec.overlay.arrow_style=this.value;renderPreviewOverlays(rec.scene);markDirty();}});
  document.getElementById('tl-ov-animation').addEventListener('change',function(){var rec=currentOverlay();if(rec){rec.overlay.animation=this.value;markDirty();}});
  document.getElementById('tl-ov-sfx').addEventListener('change',function(){
    var rec=currentOverlay();if(!rec)return;var i=parseInt(this.value,10);
    if(isNaN(i)||!libSounds[i]){delete rec.overlay.appear_sfx_path;delete rec.overlay.appear_sfx_name;delete rec.overlay.appear_sfx_duration;}
    else{var snd=libSounds[i];rec.overlay.appear_sfx_path=snd.path;rec.overlay.appear_sfx_name=snd.name;rec.overlay.appear_sfx_duration=Math.max(.08,Math.min(2,+(snd.duration||1)));}
    markDirty();
  });
  function previewOverlaySound(ov){
    if(!ov||!ov.appear_sfx_path)return;
    var snd=libSounds.filter(function(s){return String(s.path)===String(ov.appear_sfx_path);})[0];
    if(!snd||!snd.url)return;
    try{var a=new Audio(snd.url);a.volume=Math.max(0,Math.min(1,ov.appear_sfx_volume==null?.22:+ov.appear_sfx_volume));a.play().catch(function(){});return a;}catch(e){}
  }
  function replayOverlayEntrance(){
    var rec=currentOverlay();if(!rec)return;renderPreviewOverlays(rec.scene);
    var el=overlayLayer.querySelector('.tl-preview-overlay.selected');if(!el)return;
    var base=el.style.transform,kind=String(rec.overlay.animation||'pop'),ms=Math.round(Math.max(.1,Math.min(1.5,+(rec.overlay.animation_duration||.28)))*1000),frames;
    if(kind==='bounce')frames=[{opacity:0,transform:base+' scale(.25)'},{opacity:1,transform:base+' scale(1.18)',offset:.72},{opacity:1,transform:base}];
    else if(kind==='slide')frames=[{opacity:0,transform:base+' translateX(-120px)'},{opacity:1,transform:base}];
    else if(kind==='fade')frames=[{opacity:0},{opacity:1}];
    else if(kind==='none')frames=[{opacity:1},{opacity:1}];
    else frames=[{opacity:0,transform:base+' scale(.3)'},{opacity:1,transform:base}];
    try{el.animate(frames,{duration:ms,easing:'cubic-bezier(.2,.8,.2,1)'});}catch(e){}
    previewOverlaySound(rec.overlay);
  }
  document.getElementById('tl-ov-replay').addEventListener('click',replayOverlayEntrance);
  document.getElementById('tl-ov-delete').addEventListener('click',deleteCurrentOverlay);

  var pimg=document.getElementById('tl-pimg'), pvid=document.getElementById('tl-pvid'), pempty=document.getElementById('tl-stage-empty'), pcaption=document.getElementById('tl-preview-caption');
  var playing=false, clock=0, lastTs=0, curIdx=-1, activeSceneInfo=null;
  var playAnchorClock=0, playAnchorPerf=0, pendingVideoTime=null;
  // The previous render is deliberately not used as the editor preview. It cannot represent
  // unsaved cuts. The editable timeline clock below is authoritative from the first frame.
  var RENDER_MODE = false;
  function sceneAt(t){
    var vis=visible(), acc=0, continuity=0, previousIdentity=null, previousDuration=0;
    for(var i=0;i<vis.length;i++){
      var identity=String(vis[i].media_identity||vis[i].clip||vis[i].poster||vis[i].id);
      continuity=(i>0&&identity===previousIdentity)?continuity+previousDuration:0;
      if(t < acc+vis[i].dur || i===vis.length-1){ return {scene:vis[i], idx:i, start:acc, local:Math.max(0,t-acc), continuity:continuity}; }
      acc+=vis[i].dur; previousIdentity=identity; previousDuration=vis[i].dur;
    }
    return null;
  }
  // Strip surrounding sentence punctuation from a caption word ("UNREAL." -> "UNREAL"); keep ? ! and
  // internal apostrophes/hyphens. Must match pipeline._caption_display_word so preview == render.
  function cleanCaptionWord(w){
    var P=' .,;:"\\'()[]{}\\u2026\\u201c\\u201d\\u2018\\u2019'; w=String(w||'');
    var s=0,e=w.length;
    while(s<e && P.indexOf(w.charAt(s))>-1)s++;
    while(e>s && P.indexOf(w.charAt(e-1))>-1)e--;
    return w.slice(s,e);
  }
  function captionAt(t){
    if(!captionsOn)return '';
    for(var i=0;i<captionTrack.length;i++){
      var row=captionTrack[i], start=+(row.start||0), end=+(row.end||start);
      if(t<start||t>=end)continue;
      var words=String(row.text||'').trim().split(/\\s+/).filter(Boolean);if(!words.length)return '';
      var idx=0, timings=row.word_timings||[];
      if(timings.length){
        var local=t-start;
        for(var j=0;j<timings.length;j++){if(local>=+(timings[j].start||0))idx=j;else break;}
      }else idx=Math.min(words.length-1,Math.floor(((t-start)/Math.max(.05,end-start))*words.length));
      var group=Math.floor(idx/captionMaxWords)*captionMaxWords;
      var text=words.slice(group,group+captionMaxWords).map(cleanCaptionWord).filter(Boolean).join(' ');
      return captionUppercase?text.toUpperCase():text;
    }
    return '';
  }
  function renderPreviewCaption(t){
    if(!pcaption)return;var text=captionAt(t);pcaption.textContent=text;
    pcaption.style.top=(Math.max(.1,Math.min(.9,+(model.caption_center_y||.72)))*100)+'%';
    pcaption.style.display=text?'block':'none';
  }
  function renderPreviewOverlays(scene){
    if(!overlayLayer)return; overlayLayer.innerHTML='';
    if(!scene || (RENDER_MODE && !(sel&&sel.type==='overlay')))return;
    (scene.overlays||[]).forEach(function(ov){
      var d=overlayDefaults(ov), el=document.createElement('div'), kind=String(ov.type||'visual');
      var selected=!!(sel&&sel.type==='overlay'&&String(sel.id)===String(ov.id));
      var arrowStyle=String(ov.arrow_style||'default_thick_red_arrow').replace(/[^a-z0-9_-]/gi,'');
      el.className='tl-preview-overlay kind-'+kind+' style-'+arrowStyle+(selected?' selected':'');
      el.style.left=(d.x*100)+'%'; el.style.top=(d.y*100)+'%';
      var angle=0;
      if(kind==='callout' && ov.from==='right') angle=180;
      else if(kind==='arrows'&&ov.items&&ov.items.length){var a=ov.items[0];angle=Math.atan2(+a[3]-+a[1],+a[2]-+a[0])*180/Math.PI;}
      angle+=+(ov.editor_rotation||0);
      var baseTransform='translate(-50%,-50%) scale('+d.scale+') rotate('+angle+'deg)';
      el.style.transform=baseTransform;
      if(playing){
        var sceneStart=startOf(scene.id)||0, sceneLocal=(clock-sceneStart)/Math.max(.01,scene.dur);
        var ovStart=Math.max(0,Math.min(1,+(ov.start==null?0:ov.start))),ovEnd=Math.max(ovStart,Math.min(1,+(ov.end==null?1:ov.end)));
        if(sceneLocal<ovStart||sceneLocal>ovEnd)return;
        var anim=String(ov.animation||'pop'),animSeconds=Math.max(.1,Math.min(1.5,+(ov.animation_duration||.28)));
        var progress=Math.max(0,Math.min(1,((sceneLocal-ovStart)*scene.dur)/animSeconds));
        var eased=1-Math.pow(1-progress,3),extraScale=1,opacity=1,slideX=0;
        if(anim==='fade')opacity=eased;
        else if(anim==='slide'){opacity=eased;slideX=(ov.from==='right'?1:-1)*(1-eased)*120;}
        else if(anim==='bounce')extraScale=.25+.75*eased+.24*Math.sin(progress*Math.PI);
        else if(anim==='pop')extraScale=.3+.7*eased;
        el.style.opacity=String(opacity);
        el.style.transform='translate(-50%,-50%) translateX('+slideX+'px) scale('+d.scale+') rotate('+angle+'deg) scale('+extraScale+')';
      }
      var text=(ov.text||((ov.values&&ov.values[0])||''));
      el.innerHTML='<span class="tl-ov-glyph">'+(kind==='callout'||kind==='arrows'?'\u279C':kind==='highlight'?'':esc(text||overlayLabel(ov)))+'</span><span class="tl-ov-scale-handle" title="Drag to scale"></span>';
      el.addEventListener('pointerdown',function(ev){
        ev.stopPropagation(); ev.preventDefault();
        if(!selected){ selectOverlay(ov.id,scene.id); }
        var scaling=ev.target.classList.contains('tl-ov-scale-handle');
        var rect=document.getElementById('tl-stage-view').getBoundingClientRect();
        var sx=ev.clientX,sy=ev.clientY,start=overlayDefaults(ov),changed=false;
        function mv(e){
          changed=true;
          if(scaling){ ov.editor_scale=Math.max(.25,Math.min(3,start.scale+(e.clientX-sx+e.clientY-sy)/180)); }
          else { ov.editor_x=Math.max(0,Math.min(1,start.x+(e.clientX-sx)/Math.max(1,rect.width))); ov.editor_y=Math.max(0,Math.min(1,start.y+(e.clientY-sy)/Math.max(1,rect.height))); }
          renderPreviewOverlays(scene);
        }
        function up(){document.removeEventListener('pointermove',mv);document.removeEventListener('pointerup',up);if(changed){syncOverlayInspector();markDirty();}}
        document.addEventListener('pointermove',mv);document.addEventListener('pointerup',up);
      });
      overlayLayer.appendChild(el);
    });
  }
  function desiredSourceTime(info){
    if(!info)return 0;
    var scene=info.scene, sourceRate=Math.max(.01,+(scene.source_speed||1));
    var previewRate=Math.max(.05,+(scene.speed||1)/sourceRate);
    return Math.max(0,+(scene.source_trim||0)+ +(info.continuity||0) + info.local*previewRate);
  }
  function syncPreviewVideo(info, force){
    if(!info||!info.scene.clip)return;
    var sourceRate=Math.max(.01,+(info.scene.source_speed||1));
    var rate=Math.max(.05,Math.min(4,+(info.scene.speed||1)/sourceRate));
    var wanted=desiredSourceTime(info);pendingVideoTime=wanted;
    try{pvid.playbackRate=rate;}catch(e){}
    if(pvid.readyState>=1){
      if(Number.isFinite(pvid.duration)&&pvid.duration>0)wanted=Math.min(wanted,Math.max(0,pvid.duration-.02));
      if(force||Math.abs((pvid.currentTime||0)-wanted)>.10){try{pvid.currentTime=wanted;}catch(e){}}
      pendingVideoTime=null;
    }
    if(playing&&pvid.paused)pvid.play().catch(function(){});
  }
  pvid.addEventListener('loadedmetadata',function(){if(!activeSceneInfo||!activeSceneInfo.scene.clip)return;syncPreviewVideo(activeSceneInfo,true);});
  function showScene(info, forceSync){
    renderPreviewCaption(clock);
    if(!info){ pimg.style.display='none'; pvid.style.display='none'; pempty.style.display='block'; renderPreviewOverlays(null); activeSceneInfo=null; return; }
    pempty.style.display='none';
    activeSceneInfo=info;
    renderPreviewOverlays(info.scene);
    if(curIdx===info.idx){
      // only trust the cached index while the loaded media still BELONGS to this scene -
      // after reorder/undo/replace the same index can hold a different clip
      var sameMedia = info.scene.clip
        ? (pvid.style.display!=='none' && pvid.getAttribute('src')===info.scene.clip)
        : (pimg.style.display!=='none' && pimg.getAttribute('src')===(info.scene.poster||''));
      if(sameMedia){
        if(info.scene.clip) syncPreviewVideo(info,!!forceSync);
        return;
      }
    }
    curIdx=info.idx; var s=info.scene;
    if(s.clip){
      pimg.style.display='none'; pvid.style.display='block';
      // show a still frame (poster) instead of a black box while the video decodes/seeks
      if(s.poster) pvid.setAttribute('poster', s.poster); else pvid.removeAttribute('poster');
      try{
        if(pvid.getAttribute('src')!==s.clip){pvid.src=s.clip;pvid.load();}
        syncPreviewVideo(info,true);
      }catch(e){}
    }
    else { pvid.pause(); pvid.style.display='none'; pimg.style.display='block'; pimg.src=s.poster||''; }
  }

  // ---- sequence-mode AUDIO: voice + music tracks follow the clock; SFX and cut sounds
  // fire at their timeline positions - editing no longer means silent previewing.
  var voiceA = model.voice_url ? new Audio(model.voice_url) : null;
  var musicA = model.music_url ? new Audio(model.music_url) : null;
  if(voiceA)voiceA.preload='auto';
  if(musicA)musicA.preload='auto';
  if (musicA) musicA.loop = true;
  var liveSfx = [];
  function audioVolumes(){
    if (voiceA) voiceA.volume = Math.max(0, Math.min(1, volumes.voice != null ? volumes.voice : 1));
    if (musicA) musicA.volume = Math.max(0, Math.min(1, volumes.music || 0));
  }
  function audioSeek(t){
    audioVolumes();
    if (voiceA){ try{ voiceA.currentTime = Math.max(0, Math.min(t, Number.isFinite(voiceA.duration)?Math.max(0,voiceA.duration-.02):t)); }catch(e){} }
    if (musicA){ try{ musicA.currentTime = Math.max(0, t % Math.max(1, musicA.duration || 9999)); }catch(e){} }
  }
  function syncAudioToClock(t, force){
    audioVolumes();
    if(voiceA&&voiceA.readyState>=1){var vt=Math.min(t,Number.isFinite(voiceA.duration)?Math.max(0,voiceA.duration-.02):t);if(force||Math.abs((voiceA.currentTime||0)-vt)>.12){try{voiceA.currentTime=vt;}catch(e){}}}
    if(musicA&&musicA.readyState>=1){var md=Math.max(1,musicA.duration||9999),mt=t%md;if(force||Math.abs((musicA.currentTime||0)-mt)>.18){try{musicA.currentTime=mt;}catch(e){}}}
  }
  if(voiceA)voiceA.addEventListener('loadedmetadata',function(){syncAudioToClock(clock,true);});
  if(musicA)musicA.addEventListener('loadedmetadata',function(){syncAudioToClock(clock,true);});
  function audioPlay(){
    audioVolumes();
    if (voiceA) voiceA.play().catch(function(){});
    if (musicA && (volumes.music || 0) > 0) musicA.play().catch(function(){});
  }
  function audioStop(){
    if (voiceA) voiceA.pause();
    if (musicA) musicA.pause();
    liveSfx.forEach(function(a){ try{ a.pause(); }catch(e){} });
    liveSfx = [];
  }
  function fxAbsStart(f){
    if (f.start_abs != null) return f.start_abs;
    var st = startOf(f.scene_id);
    return st === null ? null : st + (f.offset || 0);
  }
  function trAbsStart(t){
    if (t.start_abs != null) return t.start_abs;
    var st = startOf(t.scene_id);
    return st === null ? (t.at != null ? t.at : null) : st;
  }
  function fireSfxBetween(t0, t1){
    if(!sfxOn) return;   // "Sound FX" off -> preview the voice-only mix, matching the render
    function fire(list, absFn){
      list.forEach(function(f){
        if (f.enabled === false || f.deleted) return;
        var at = absFn(f);
        if (at === null || at < t0 || at >= t1) return;
        var url = f.url; if (!url) return;
        try{
          var a = new Audio(url);
          a.volume = Math.max(0, Math.min(1, f.volume != null ? f.volume : 0.25));
          a.playbackRate = Math.max(.25, Math.min(4, +(f.playback_rate || 1)));
          a.currentTime = Math.max(0, +(f.source_trim || 0));
          a.play().catch(function(){});
          liveSfx.push(a);
          if (liveSfx.length > 12) liveSfx.shift();
        }catch(e){}
      });
    }
    fire(sfx, fxAbsStart);
    fire(transitions, trAbsStart);
    visible().forEach(function(scene){
      var sceneStart=startOf(scene.id);if(sceneStart===null)return;
      (scene.overlays||[]).forEach(function(ov){
        if(!ov.appear_sfx_path)return;
        var at=sceneStart+Math.max(0,Math.min(1,+(ov.start||0)))*scene.dur;
        if(at<t0||at>=t1)return;
        var audio=previewOverlaySound(ov);if(audio){liveSfx.push(audio);if(liveSfx.length>12)liveSfx.shift();}
      });
    });
  }
  function fmtClock(t){t=Math.max(0,+t||0);var m=Math.floor(t/60),s=Math.floor(t%60),f=Math.floor((t-Math.floor(t))*30);return m+':'+(s<10?'0':'')+s+':'+(f<10?'0':'')+f;}
  function updatePlayhead(){ playhead.style.left=(clock*SCALE)+'px'; document.getElementById('tl-playtime').textContent=fmtClock(clock)+' / '+fmtClock(totalDur()); }
  var PLAY_ICO='<svg viewBox="0 0 24 24" width="22" height="22" fill="currentColor"><path d="M8 5v14l11-7z"/></svg>';
  var PAUSE_ICO='<svg viewBox="0 0 24 24" width="22" height="22" fill="currentColor"><path d="M6 5h4v14H6zM14 5h4v14h-4z"/></svg>';
  function setPlayIcon(p){ var el=document.getElementById('tl-play-ico'); if(el) el.innerHTML=p?PAUSE_ICO:PLAY_ICO; }
  function tick(ts){
    if(!playing||RENDER_MODE) return;
    var prev=clock;lastTs=ts;
    clock=playAnchorClock+(ts-playAnchorPerf)/1000;
    var total=totalDur();
    if(clock-prev<.75)fireSfxBetween(prev, clock);
    if(clock>=total){ clock=total; updatePlayhead(); stop(); return; }
    syncAudioToClock(clock,false);
    showScene(sceneAt(clock),false); updatePlayhead(); requestAnimationFrame(tick);
  }
  function play(){
    if(playing) return;
    if(RENDER_MODE){ playing=true; setPlayIcon(true); pvid.play().catch(function(){}); return; }
    if(clock>=totalDur()-0.05){ clock=0; curIdx=-1; }
    playing=true; lastTs=performance.now(); playAnchorClock=clock; playAnchorPerf=lastTs; setPlayIcon(true);
    audioSeek(clock); audioPlay();
    showScene(sceneAt(clock),true); requestAnimationFrame(tick);
  }
  function stop(){ playing=false; pvid.pause(); audioStop(); setPlayIcon(false); }
  function seekTo(t){
    if(RENDER_MODE){ pvid.currentTime=Math.max(0,Math.min(pvid.duration||t, t)); return; }
    clock=Math.max(0,Math.min(totalDur(), t)); curIdx=-1;
    playAnchorClock=clock;playAnchorPerf=performance.now();
    audioSeek(clock); if(playing) audioPlay();
    showScene(sceneAt(clock),true); updatePlayhead();
  }
  function seekBy(d){
    if(RENDER_MODE){ pvid.currentTime=Math.max(0,Math.min(pvid.duration||0, (pvid.currentTime||0)+d)); return; }
    seekTo(clock+d);
  }
  if(RENDER_MODE){
    // play the real rendered video, unmuted, with audio - this IS the last render
    pvid.src=renderUrl; pvid.muted=false; pvid.removeAttribute('muted'); pvid.controls=true; pvid.setAttribute('playsinline',''); pvid.style.display='block'; pempty.style.display='none';
    pvid.addEventListener('timeupdate', function(){ if(!RENDER_MODE) return; clock=pvid.currentTime||0; updatePlayhead(); });
    pvid.addEventListener('play', function(){ if(!RENDER_MODE) return; playing=true; setPlayIcon(true); });
    pvid.addEventListener('pause', function(){ if(!RENDER_MODE) return; playing=false; setPlayIcon(false); });
    pvid.addEventListener('ended', function(){ if(!RENDER_MODE) return; playing=false; setPlayIcon(false); });
  }
  // Once the user EDITS the sequence (e.g. replaces a clip's media), the baked render no
  // longer matches the timeline - switch the preview to the live scene sequence so the
  // change is visible immediately instead of silently keeping the old render.
  function leaveRenderMode(){
    if(!RENDER_MODE) return;
    RENDER_MODE=false;
    try{ pvid.pause(); }catch(e){}
    pvid.controls=false; pvid.muted=true; pvid.removeAttribute('src'); try{ pvid.load(); }catch(e){}
    playing=false; setPlayIcon(false); curIdx=-1;
  }
  function previewSceneById(id){
    var vis=visible(), acc=0, idx=-1;
    for(var i=0;i<vis.length;i++){ if(vis[i].id===id){ idx=i; break; } acc+=vis[i].dur; }
    if(idx<0) return;
    seekTo(acc+0.01);
  }
  document.getElementById('tl-play').addEventListener('click', function(){ if(playing) stop(); else play(); });
  document.getElementById('tl-back').addEventListener('click', function(){ seekBy(-5); });
  document.getElementById('tl-fwd').addEventListener('click', function(){ seekBy(5); });
  // ruler + playhead: click to seek, drag to scrub (works in render and sequence mode)
  function timelineTimeFromClientX(clientX){
    var rect=ruler.getBoundingClientRect();
    return Math.max(0,Math.min(totalDur(),(clientX-rect.left)/SCALE));
  }
  function seekFromClientX(clientX){ seekTo(timelineTimeFromClientX(clientX)); }
  function scrubFromEvent(e){
    seekFromClientX(e.clientX);
  }
  function startScrub(e){
    e.preventDefault();
    scrubFromEvent(e);
    function mv(ev){ scrubFromEvent(ev); }
    function up(){ document.removeEventListener('pointermove',mv); document.removeEventListener('pointerup',up); }
    document.addEventListener('pointermove',mv); document.addEventListener('pointerup',up);
  }
  ruler.addEventListener('pointerdown', startScrub);
  playhead.addEventListener('pointerdown', startScrub);
  [ovEl,capEl,voiceEl,trEl,sfxEl].forEach(function(track){
    if(!track)return;
    track.addEventListener('pointerdown',function(e){
      if(e.button!==0)return;
      if(e.target.closest&&e.target.closest('.tl-ovitem,.tl-fx,.tl-trans,.tl-trans-slot'))return;
      seekFromClientX(e.clientX);
    });
  });

  // ---- toolbar: undo/redo + zoom; keyboard shortcuts ----
  document.getElementById('tl-undo').addEventListener('click', undoEdit);
  document.getElementById('tl-redo').addEventListener('click', redoEdit);
  document.getElementById('tl-zoom-in').addEventListener('click', function(){ setZoom(SCALE*1.3); });
  document.getElementById('tl-zoom-out').addEventListener('click', function(){ setZoom(SCALE/1.3); });
  document.getElementById('tl-zoom-fit').addEventListener('click', fitZoom);
  var scrollEl=document.getElementById('tl-scroll');
  if (scrollEl) scrollEl.addEventListener('wheel', function(e){
    if (!e.ctrlKey) return;
    e.preventDefault();
    setZoom(SCALE * (e.deltaY < 0 ? 1.15 : 1/1.15));
  }, {passive:false});
  document.addEventListener('keydown', function(e){
    var tag=(e.target && e.target.tagName || '').toLowerCase();
    if (tag==='input' || tag==='textarea' || tag==='select' || (e.target && e.target.isContentEditable)) return;
    if (e.code==='Space'){ e.preventDefault(); if(playing) stop(); else play(); }
    else if ((e.key==='Delete'||e.key==='Backspace') && sel){
      e.preventDefault();
      if (sel.type==='clip'){ var chosen=scenes.filter(function(x){return selectedClipIds.indexOf(x.id)!==-1;}); if(chosen.length){ chosen.forEach(function(s){s.removed=true;}); sel=null;clearClipSelection();showPane(null);layout();markDirty(); } }
      else if(sel.type==='overlay'){ deleteCurrentOverlay(); }
      else { deleteCurrentFx(); }
    }
    else if ((e.ctrlKey||e.metaKey) && !e.shiftKey && e.key.toLowerCase()==='z'){ e.preventDefault(); undoEdit(); }
    else if (((e.ctrlKey||e.metaKey) && e.shiftKey && e.key.toLowerCase()==='z') || ((e.ctrlKey||e.metaKey) && e.key.toLowerCase()==='y')){ e.preventDefault(); redoEdit(); }
    else if (e.key==='+' || e.key==='='){ setZoom(SCALE*1.3); }
    else if (e.key==='-'){ setZoom(SCALE/1.3); }
    else if (e.key.toLowerCase()==='f'){ fitZoom(); }
    else if ((e.key==='ArrowLeft'||e.key==='ArrowRight'||e.key==='ArrowUp'||e.key==='ArrowDown') && sel&&sel.type==='overlay'){
      e.preventDefault(); var rec=currentOverlay(); if(rec){var d=overlayDefaults(rec.overlay),step=e.shiftKey ? 0.02 : 0.005;
        if(e.key==='ArrowLeft')d.x-=step;if(e.key==='ArrowRight')d.x+=step;if(e.key==='ArrowUp')d.y-=step;if(e.key==='ArrowDown')d.y+=step;
        rec.overlay.editor_x=Math.max(0,Math.min(1,d.x));rec.overlay.editor_y=Math.max(0,Math.min(1,d.y));syncOverlayInspector();markDirty();}
    }
    else if (e.key==='ArrowLeft'){ seekBy(e.shiftKey?-1:-0.05); }
    else if (e.key==='ArrowRight'){ seekBy(e.shiftKey?1:0.05); }
    else if (e.key==='Home'){ seekTo(0); }
    else if (e.key==='End'){ seekTo(totalDur()); }
  });

  function bindVol(id, key, out){
    var el=document.getElementById(id), o=document.getElementById(out);
    el.value=volumes[key]; o.textContent=Math.round(volumes[key]*100)+'%';
    el.addEventListener('input', function(){ volumes[key]=parseFloat(this.value); o.textContent=Math.round(volumes[key]*100)+'%'; markDirty(); });
  }
  bindVol('tl-voice-vol','voice','tl-v-voice');
  bindVol('tl-music-vol','music','tl-v-music');
  var capToggle=document.getElementById('tl-captions');
  // #109: "render with captions" defaults ON, but is only available when the project has a real
  // (still-unrendered) caption track. A baked-in-captions upload has none -> grey it out.
  var captionsEditable=(model.captions_editable!==false) && captionTrack.length>0;
  if(!captionsEditable){
    captionsOn=false; capToggle.checked=false; capToggle.disabled=true;
    var _capLbl=capToggle.closest('.tl-cap-toggle');
    if(_capLbl){ _capLbl.classList.add('tl-disabled'); _capLbl.title='This project has no separate caption track to render (e.g. captions are already baked into an uploaded video).'; }
  } else {
    captionsOn=true; capToggle.checked=true;   // default ON when captions exist
  }
  capToggle.addEventListener('change', function(){ if(this.disabled)return; captionsOn=this.checked; layout(); renderPreviewCaption(clock); markDirty(); });
  var sfxToggle=document.getElementById('tl-sfx-toggle');
  if(sfxToggle){ sfxToggle.checked=sfxOn;
    sfxToggle.addEventListener('change', function(){ sfxOn=this.checked; layout(); markDirty(); }); }

  function collectEdits(){
    var vis=visible();
    return {
      scenes: vis.map(function(s){return {id:s.id, duration:s.dur, speed:(s.speed&&Math.abs(s.speed-1)>0.01)?s.speed:1, blur_captions:!!s.blur_captions, source_trim:(s.clip?+(+(s.source_trim||0)).toFixed(3):undefined)};}),
      order: vis.map(function(s){return s.id;}),
      removed: scenes.filter(function(s){return s.removed;}).map(function(s){return s.id;}),
      added: scenes.filter(function(s){return s.added;}).map(function(s){return {id:s.id, kind:s.kind, path:s.path, clip:s.clip, poster:s.poster, dur:s.dur, after:s.id};}),
      replace: Object.keys(markedReplace),
      replaced: Object.keys(replacedMap).map(function(id){ return {id:id, path:replacedMap[id].path, type:replacedMap[id].type}; }),
      overlays: scenes.map(function(s){return {scene_id:s.id,items:(s.overlays||[])};}),
      volumes: volumes,
      captions: captionsOn,
      sfx_on: sfxOn,
      transitions: transitions.map(function(t){return {id:t.id, volume:t.volume, enabled:(t.deleted?false:t.enabled), start_abs:(t.start_abs!=null?t.start_abs:null), path_override:t.path_override||null, source_trim:+(+(t.source_trim||0)).toFixed(3)};}),
      sfx: sfx.filter(function(f){return !(f.added&&f.deleted);}).map(function(f){return {id:f.id, volume:f.volume, enabled:(f.deleted?false:f.enabled), added:!!f.added, path:f.path, label:f.label||'', scene_id:f.scene_id, offset:f.offset, duration:+(+(f.duration||1)).toFixed(3), source_duration:+(+(f.source_duration||0)).toFixed(3), playback_rate:+(+(f.playback_rate||1)).toFixed(6), start_abs:(f.start_abs!=null?f.start_abs:null), path_override:f.path_override||null, source_trim:+(+(f.source_trim||0)).toFixed(3)};})
    };
  }

  document.getElementById('tl-save').addEventListener('click', function(){
    var btn=this; btn.disabled=true;
    fetch('/timeline-save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({slug:slug, edits:collectEdits()})})
      .then(function(r){return r.json();})
      .then(function(d){ btn.disabled=false; if(d&&d.ok){ dirty=false; btn.classList.remove('tl-unsaved'); btn.innerHTML='\\u2713 Saved'; setTimeout(function(){ btn.innerHTML='\\uD83D\\uDCBE Save'; },1400); } else { alert((d&&d.error)||'Could not save.'); } })
      .catch(function(){ btn.disabled=false; alert('Could not save.'); });
  });

  // renders go to the FOCUSED progress view (big bar + elapsed/ETA + current action) - no chat UI
  function gotoProgress(jobUrl){
    try{ var id=new URL(jobUrl, location.href).searchParams.get('id'); if(id){ window.location.href='/progress?id='+encodeURIComponent(id); return; } }catch(e){}
    window.location.href=jobUrl;
  }
  document.getElementById('tl-render').addEventListener('click', function(){
    var btn=this; btn.disabled=true; var old=btn.innerHTML; btn.textContent='Saving…';
    var edits=collectEdits();
    // ALWAYS save the project before a render starts (user rule)
    fetch('/timeline-save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({slug:slug, edits:edits})})
      .then(function(){ dirty=false; try{ document.getElementById('tl-save').classList.remove('tl-unsaved'); }catch(e){} btn.textContent='Starting…';
        return fetch('/timeline-render',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({slug:slug, edits:edits})}); })
      .then(function(r){return r.json();})
      .then(function(d){ if(d&&d.ok&&d.job){ gotoProgress(d.job); } else { btn.disabled=false; btn.innerHTML=old; alert((d&&d.error)||'Could not start render.'); } })
      .catch(function(){ btn.disabled=false; btn.innerHTML=old; alert('Could not start render.'); });
  });

  // Redo SFX: a normal SFX-Master run over this project's LATEST render -> progress view
  var redoSfxBtn=document.getElementById('tl-redo-sfx');
  if(redoSfxBtn) redoSfxBtn.addEventListener('click', function(){
    var btn=this; btn.disabled=true; var old=btn.innerHTML; btn.textContent='Starting…';
    var model_=document.getElementById('tl-rw-model'); var rm=model_?model_.value:'';
    fetch('/timeline-redo-sfx',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({slug:slug, reasoning_model:rm})})
      .then(function(r){return r.json();})
      .then(function(d){ if(d&&d.ok&&d.job_id){ window.location.href='/progress?id='+encodeURIComponent(d.job_id); } else { btn.disabled=false; btn.innerHTML=old; alert((d&&d.error)||'Could not start Redo SFX.'); } })
      .catch(function(){ btn.disabled=false; btn.innerHTML=old; alert('Could not start Redo SFX.'); });
  });

  // ---- Change script: edit narration -> fresh voiceover + recut (unchanged lines keep media)
  var scriptModal=document.getElementById('tl-script-modal');
  // The toolbar panel uses backdrop-filter, which creates a containing block for fixed children.
  // Move the modal to body so inset:0 is the viewport and it always opens centered.
  if(scriptModal&&scriptModal.parentNode!==document.body)document.body.appendChild(scriptModal);
  var scriptHook = model.hook_text || '';
  function normWs(t){ return String(t||'').replace(/\\s+/g,' ').trim(); }
  function syncHookStatus(){
    var el=document.getElementById('tl-script-hookstatus');
    var text=(document.getElementById('tl-script-text').value||'');
    if(!scriptHook){ el.textContent='No hook marked - the whole script is spoken as one take.'; el.classList.add('off'); return; }
    var inScript = normWs(text).toLowerCase().indexOf(normWs(scriptHook).toLowerCase())>-1;
    el.classList.toggle('off', !inScript);
    el.textContent = (inScript ? '★ Hook: ' : '⚠ Hook no longer in the script: ')
      + scriptHook.slice(0,90) + (scriptHook.length>90?'…':'');
  }
  document.getElementById('tl-script-btn').addEventListener('click', function(){
    document.getElementById('tl-script-text').value = model.script_text || '';
    scriptHook = model.hook_text || '';
    // narrator: prefill from the project's saved run settings
    document.getElementById('tl-script-speaker').value = model.speaker_name || 'Narrator';
    var vsel=document.getElementById('tl-script-voice');
    vsel.innerHTML=(model.tts_voices||[]).map(function(v){
      return '<option value="'+esc(v)+'"'+(v===model.tts_voice?' selected':'')+'>'+esc(v)+'</option>';
    }).join('') || '<option value="">(default voice)</option>';
    document.getElementById('tl-script-ttsmodel').value = (model.tts_model==='flash')?'flash':'pro';
    setDensity(model.clip_density || 'medium');
    syncHookStatus();
    scriptModal.hidden=false;
    document.getElementById('tl-script-text').focus();
  });
  // dedicated toolbar "Script" button opens the change-script workflow directly (it is its OWN
  // flow, no longer a checkbox mixed into the combinable Agent-rework options).
  var _scriptOpenBtn=document.getElementById('tl-script-open');
  if(_scriptOpenBtn) _scriptOpenBtn.addEventListener('click', function(){ document.getElementById('tl-script-btn').click(); });
  // clip density: Few / Medium / Many -> how many clips the recut uses
  var scriptDensity = 'medium';
  var DENSITY_HINT = {few:'Fewer, longer clips - slower, cinematic cuts.',
                      medium:'One clip per sentence (default pacing).',
                      many:'More, shorter clips - fast-paced montage.'};
  function setDensity(d){
    scriptDensity = (['few','medium','many'].indexOf(d)!==-1)?d:'medium';
    Array.prototype.forEach.call(document.querySelectorAll('#tl-script-density button'),function(b){
      b.classList.toggle('on', b.getAttribute('data-density')===scriptDensity);
    });
    document.getElementById('tl-script-density-hint').textContent = DENSITY_HINT[scriptDensity]||'';
  }
  document.getElementById('tl-script-density').addEventListener('click', function(e){
    var b=e.target.closest('button[data-density]'); if(b) setDensity(b.getAttribute('data-density'));
  });
  // #114 - media source for changed / new lines
  var scriptMediaSource = 'scrape';
  var MEDIA_HINT = {scrape:'Search TikTok/X for fresh footage for changed lines.',
                    keep_visible:'Reuse the clips already on this timeline (no search).',
                    library:'Pick the best-matching clips from ALL your projects (no scraping).'};
  function setMediaSource(m){
    scriptMediaSource = (['scrape','keep_visible','library'].indexOf(m)!==-1)?m:'scrape';
    Array.prototype.forEach.call(document.querySelectorAll('#tl-script-mediasrc button'),function(b){
      b.classList.toggle('on', b.getAttribute('data-media')===scriptMediaSource);
    });
    document.getElementById('tl-script-mediasrc-hint').textContent = MEDIA_HINT[scriptMediaSource]||'';
  }
  document.getElementById('tl-script-mediasrc').addEventListener('click', function(e){
    var b=e.target.closest('button[data-media]'); if(b) setMediaSource(b.getAttribute('data-media'));
  });
  setMediaSource('scrape');
  document.getElementById('tl-script-markhook').addEventListener('click', function(){
    var ta=document.getElementById('tl-script-text');
    var sel=ta.value.substring(ta.selectionStart, ta.selectionEnd).trim();
    if(!sel){ alert('Select the opening line(s) inside the script first, then click ★ Mark hook.'); return; }
    scriptHook=sel; syncHookStatus();
  });
  document.getElementById('tl-script-clearhook').addEventListener('click', function(){ scriptHook=''; syncHookStatus(); });
  document.getElementById('tl-script-text').addEventListener('input', syncHookStatus);
  document.getElementById('tl-script-cancel').addEventListener('click', function(){ scriptModal.hidden=true; });
  scriptModal.addEventListener('pointerdown', function(e){ if(e.target===scriptModal) scriptModal.hidden=true; });
  document.getElementById('tl-script-run').addEventListener('click', function(){
    var text=(document.getElementById('tl-script-text').value||'').trim();
    if(!text){ alert('The script is empty.'); return; }
    var speaker=(document.getElementById('tl-script-speaker').value||'').trim();
    var voice=document.getElementById('tl-script-voice').value||'';
    var ttsModel=document.getElementById('tl-script-ttsmodel').value||'pro';
    var voiceUnchanged = speaker===(model.speaker_name||'') && voice===(model.tts_voice||'')
                         && ttsModel===(model.tts_model||'pro');
    if(text===(model.script_text||'').trim() && scriptHook===(model.hook_text||'') && voiceUnchanged){
      if(!confirm('Nothing changed - regenerate the voiceover and re-render anyway?')) return;
    }
    if(scriptHook && normWs(text).toLowerCase().indexOf(normWs(scriptHook).toLowerCase())===-1){
      if(!confirm('The marked hook is not part of the script anymore - continue WITHOUT a hook?')) return;
      scriptHook='';
    }
    var btn=this; btn.disabled=true; btn.textContent='Starting…';
    fetch('/timeline-rescript',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({slug:slug, script:text, hook_text:scriptHook,
                             speaker_name:speaker, tts_voice:voice, tts_model:ttsModel,
                             clip_density:scriptDensity, media_source:scriptMediaSource})})
      .then(function(r){return r.json();})
      .then(function(d){ if(d&&d.ok&&d.job){ dirty=false; window.location.href=d.job; }
        else { btn.disabled=false; btn.innerHTML='\\uD83C\\uDFA4 Re-voice & recut'; alert((d&&d.error)||'Could not start.'); } })
      .catch(function(){ btn.disabled=false; btn.innerHTML='\\uD83C\\uDFA4 Re-voice & recut'; alert('Could not start.'); });
  });

  document.getElementById('tl-rework').addEventListener('click', function(){
    var doReplace=document.getElementById('tl-rw-replace').checked;
    var doRecut=document.getElementById('tl-rw-recut').checked;
    var doRevoice=document.getElementById('tl-rw-revoice').checked;
    var doRedoSfx=document.getElementById('tl-rw-redo-sfx').checked;
    var doAddSfx=document.getElementById('tl-rw-add-sfx').checked;
    var doRedoCaptions=(redoCapToggle&&redoCapToggle.checked)||false;
    var reworkModel=document.getElementById('tl-rw-model').value||model.reasoning_model||'openai/gpt-5.5';
    var reworkReasoning=(document.querySelector('#tl-rw-model + .reasoning-mode-field select')||{}).value||'';
    if(!doReplace && !doRecut && !doRevoice && !doRedoSfx && !doAddSfx && !doRedoCaptions){ alert('Pick at least one rework option.'); return; }
    // Multiple options CAN be combined in one run. Only two genuine conflicts remain:
    if(doRedoSfx&&doAddSfx){ alert('Choose either redo SFX or add more SFX (not both).'); return; }
    // the heavy media/speech re-plans still can't share a run with an SFX redo
    if((doReplace||doRecut||doRevoice)&&(doRedoSfx||doAddSfx)){ alert('Run the SFX rework separately from media/speech changes.'); return; }
    if(doReplace && !Object.keys(markedReplace).length){ alert('Mark at least one clip\\'s media to replace (select a clip, then tick \"Mark this clip\\'s media to be replaced\").'); return; }
    var btn=this; btn.disabled=true; btn.textContent='Starting…';
    fetch('/timeline-rework',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({slug:slug, replace_media:doReplace, reorder_recut:doRecut, regenerate_speech:doRevoice, redo_sfx:doRedoSfx, add_more_sfx:doAddSfx, redo_captions:doRedoCaptions, sfx_amount:reworkSfxAmount, reasoning_model:reworkModel, reasoning_mode:reworkReasoning, replace_ids:Object.keys(markedReplace), edits:collectEdits()})})
      .then(function(r){return r.json();})
      .then(function(d){ if(d&&d.ok&&d.job){ window.location.href=d.job; } else { btn.disabled=false; btn.innerHTML='\\uD83E\\uDD16 Agent rework'; alert((d&&d.error)||'Could not start rework.'); } })
      .catch(function(){ btn.disabled=false; btn.innerHTML='\\uD83E\\uDD16 Agent rework'; alert('Could not start rework.'); });
  });

  // ---- Right-click a clip -> Replace media (pick from the library, cut to this clip's length) ----
  var clipMenuEl=null;
  function closeClipMenu(){ if(clipMenuEl){ clipMenuEl.remove(); clipMenuEl=null; } }
  function showContextMenu(x,y,items){
    if(playing) stop();   // right-clicking a timeline asset/SFX (to replace/remove) pauses playback
    closeClipMenu();
    clipMenuEl=document.createElement('div');clipMenuEl.className='tl-ctxmenu';
    items.forEach(function(it){var b=document.createElement('button');b.type='button';b.className='tl-ctxitem';b.textContent=it[0];b.addEventListener('click',function(){closeClipMenu();it[1]();});clipMenuEl.appendChild(b);});
    document.body.appendChild(clipMenuEl);
    var w=clipMenuEl.offsetWidth||180,h=clipMenuEl.offsetHeight||80;
    clipMenuEl.style.left=Math.max(8,Math.min(x,window.innerWidth-w-8))+'px';
    clipMenuEl.style.top=Math.max(8,Math.min(y,window.innerHeight-h-8))+'px';
  }
  function openClipMenu(x, y, scn){
    var items=[['Open inspector', function(){ selectClip(scn.id); openInspectorAt(x,y); }],
               ['Replace media\\u2026', function(){ if(playing) stop(); startReplacePick(scn); }],
               ['Remove clip', function(){ scn.removed=true; if(sel&&sel.id===scn.id){ sel=null; showPane(null);} layout(); markDirty(); }]];
    if(replacedMap[scn.id]) items.push(['Undo replace', function(){ delete replacedMap[scn.id]; layout(); markDirty(); }]);
    showContextMenu(x,y,items);
  }
  function openOverlayMenu(x,y,ov,sceneId){
    showContextMenu(x,y,[
      ['Open inspector', function(){ selectOverlay(ov.id, sceneId); openInspectorAt(x,y); }],
      ['Delete visual', function(){ var rec=overlayRecord(ov.id); if(rec){ rec.scene.overlays=(rec.scene.overlays||[]).filter(function(o){return String(o.id)!==String(ov.id);}); if(sel&&String(sel.id)===String(ov.id)){sel=null;showPane(null);} layout(); markDirty(); } }]
    ]);
  }
  function removeEffect(fx,type){
    if(type==='fx'&&fx.added)sfx=sfx.filter(function(item){return item.id!==fx.id;});
    else{fx.enabled=false;fx.deleted=true;}
    if(sel&&sel.id===fx.id){sel=null;showPane(null);}layout();markDirty();
  }
  // Swap the sound file behind an existing timeline SFX / transition (keeps its timing).
  function replaceFxWithSound(fx, type, it){
    fx.label=it.name; fx.path=it.path; fx.path_override=it.path; fx.url=it.url||'';
    fx.duration=it.duration||fx.duration||1; fx.enabled=true; fx.deleted=false;
    layout(); markDirty(); if(type==='trans') selectTrans(fx.id); else selectFx(fx.id);
  }
  // Hierarchical sound picker: categories -> (reaction subcategories) -> matching sounds.
  // Reuses the trans-menu shell (transMenuEl + outside-click/ESC handlers).
  function openSfxPicker(x, y, opts){
    opts=opts||{}; closeTransMenu();
    var tax=window.SFX_TAX||{roles:[],reactions:[]};
    var menu=document.createElement('div'); menu.className='tl-trans-menu tl-sfx-picker'; transMenuEl=menu;
    function reset(html){ menu.innerHTML=html; }
    function catsView(){
      var cats=(tax.roles||[]).filter(function(r){ return r[0]!=='skip'
        && libSounds.some(function(it){ return soundInCat(it,r[0],'all'); }); });
      reset('<div class="tl-tm-title">'+esc(opts.title||'Replace sound')+'</div>');
      if(!cats.length) menu.innerHTML+='<div class="tl-tm-empty">No sounds loaded yet - open the Sound&nbsp;FX library once.</div>';
      cats.forEach(function(c){ var b=document.createElement('div'); b.className='tl-tm-item tl-tm-cat';
        b.innerHTML='<span class="tl-tm-name">'+esc(c[1])+'</span><span class="tl-tm-arrow">\\u203A</span>';
        b.onclick=function(){ if(c[0]==='reaction' && (tax.reactions||[]).length) subsView(); else soundsView(c[0],'all',c[1],false); };
        menu.appendChild(b); });
      var all=document.createElement('div'); all.className='tl-tm-item tl-tm-cat';
      all.innerHTML='<span class="tl-tm-name">All sounds</span><span class="tl-tm-arrow">\\u203A</span>';
      all.onclick=function(){ soundsView('all','all','All sounds',false); }; menu.appendChild(all);
    }
    function subsView(){
      var subs=(tax.reactions||[]).filter(function(r){ return libSounds.some(function(it){ return soundInCat(it,'reaction',r[0]); }); });
      reset('<div class="tl-tm-title"><button type="button" class="tl-tm-back">\\u2039</button> Reaction</div>');
      menu.querySelector('.tl-tm-back').onclick=catsView;
      var allr=document.createElement('div'); allr.className='tl-tm-item tl-tm-cat';
      allr.innerHTML='<span class="tl-tm-name">All reactions</span><span class="tl-tm-arrow">\\u203A</span>';
      allr.onclick=function(){ soundsView('reaction','all','Reaction',true); }; menu.appendChild(allr);
      subs.forEach(function(s){ var b=document.createElement('div'); b.className='tl-tm-item tl-tm-cat';
        b.innerHTML='<span class="tl-tm-name">'+esc(s[1])+'</span><span class="tl-tm-arrow">\\u203A</span>';
        b.onclick=function(){ soundsView('reaction',s[0],s[1],true); }; menu.appendChild(b); });
    }
    function soundsView(cat, sub, label, fromReaction){
      reset('<div class="tl-tm-title"><button type="button" class="tl-tm-back">\\u2039</button> '+esc(label)+'</div>');
      menu.querySelector('.tl-tm-back').onclick=function(){ fromReaction?subsView():catsView(); };
      var list=libSounds.filter(function(it){ return soundInCat(it,cat,sub); });
      if(!list.length){ var e=document.createElement('div'); e.className='tl-tm-empty'; e.textContent='No sounds in this category.'; menu.appendChild(e); }
      list.forEach(function(it){ var item=document.createElement('div'); item.className='tl-tm-item';
        item.innerHTML='<span class="tl-tm-play" title="Preview">\\u25B6</span><span class="tl-tm-name">'+esc(it.name)+'</span>';
        item.querySelector('.tl-tm-play').addEventListener('click', function(ev){ ev.stopPropagation(); if(it.url){ try{ var a=new Audio(it.url); a.volume=.5; a.play().catch(function(){}); }catch(e){} } });
        item.addEventListener('click', function(){ if(opts.onPick) opts.onPick(it); closeTransMenu(); });
        menu.appendChild(item); });
    }
    catsView();
    document.body.appendChild(menu);
    menu.style.left=Math.max(8, Math.min(x, window.innerWidth-menu.offsetWidth-10))+'px';
    menu.style.top=Math.max(8, Math.min(y, window.innerHeight-menu.offsetHeight-10))+'px';
    document.addEventListener('pointerdown', transMenuOutside, true);
    document.addEventListener('keydown', transMenuKey, true);
  }
  function openFxMenu(x,y,fx,type){
    showContextMenu(x,y,[
      ['Open inspector',function(){ selectFx(fx.id); openInspectorAt(x,y); }],
      ['Replace sound\\u2026',function(){
        if(playing)stop();
        openSfxPicker(x,y,{title:'Replace sound', onPick:function(it){ replaceFxWithSound(fx,type,it); }});
      }],
      ['Remove sound',function(){removeEffect(fx,type);}]
    ]);
  }
  function addVisualAt(atSec){
    var info=sceneAt(Math.min(Math.max(0,atSec),Math.max(0,totalDur()-.001)));if(!info)return;
    var scene=info.scene,local=Math.max(0,Math.min(scene.dur-.05,atSec-info.start));
    var st=Math.max(0,Math.min(.96,local/Math.max(.01,scene.dur)));
    var en=Math.min(1,st+Math.min(.45,.9/Math.max(.01,scene.dur)));
    var ov={id:'ov-manual-'+Date.now(),type:'callout',shape:'arrow',from:'left',start:+st.toFixed(4),end:+en.toFixed(4),cx:.5,cy:.4,editor_x:.5,editor_y:.4,editor_scale:1,editor_rotation:0,arrow_style:'default_thick_red_arrow',animation:'pop',animation_duration:.28};
    scene.overlays=scene.overlays||[];scene.overlays.push(ov);layout();markDirty();selectOverlay(ov.id,scene.id);seekTo(atSec);
  }
  function openTrackMenu(x,y,kind,atSec){
    if(kind==='visual')showContextMenu(x,y,[['Add visual',function(){addVisualAt(atSec);}]]);
    else showContextMenu(x,y,[['Add sound effect\\u2026',function(){if(playing)stop();openTransMenu(x,y,atSec,null,{title:'Add sound effect',category:'custom'});}]]);
  }
  document.addEventListener('pointerdown', function(e){ if(clipMenuEl && !clipMenuEl.contains(e.target)) closeClipMenu(); });
  function startReplacePick(scn){
    if(playing)stop();
    // Flattened SFX/Captions-Master upload: captions are baked into each clip's pixels, so a
    // replacement can't keep them. Warn once so the lost caption isn't a surprise.
    if(model.captions_baked && !window._captionsBakedWarned){
      if(!confirm('Heads up: this project was made by uploading a finished video, so its captions '
        +'are baked into each clip. Replacing a clip removes that clip\\'s caption and the render '
        +'can\\'t re-add it to only that segment. Replace anyway?')){ return; }
      window._captionsBakedWarned=true;
    }
    replacePickId=scn.id;
    var lib=document.querySelector('.tl-library'); if(lib){ lib.scrollIntoView({behavior:'smooth', block:'nearest'}); lib.classList.add('replace-arming'); }
    // make sure the media tab is showing
    var mtab=rootEl.querySelector('.tl-lib-tab[data-lib="media"]'); if(mtab) mtab.click();
    var banner=document.getElementById('tl-replace-banner');
    if(!banner){ banner=document.createElement('div'); banner.id='tl-replace-banner'; banner.className='tl-replace-banner'; document.querySelector('.tl-library').insertBefore(banner, document.querySelector('.tl-library').firstChild.nextSibling); }
    banner.innerHTML='Pick a media below to replace the selected clip (it will be cut to '+(scenes.filter(function(x){return x.id===scn.id;})[0]||{dur:0}).dur.toFixed(1)+'s). <button type="button" id="tl-replace-cancel">Cancel</button>';
    banner.style.display='block';
    document.getElementById('tl-replace-cancel').addEventListener('click', cancelReplacePick);
    selectClip(scn.id);
  }
  ovEl.addEventListener('contextmenu',function(ev){ev.preventDefault();ev.stopPropagation();openTrackMenu(ev.clientX,ev.clientY,'visual',timelineTimeFromClientX(ev.clientX));});
  sfxEl.addEventListener('contextmenu',function(ev){if(ev.target!==sfxEl)return;ev.preventDefault();ev.stopPropagation();openTrackMenu(ev.clientX,ev.clientY,'sfx',timelineTimeFromClientX(ev.clientX));});
  function cancelReplacePick(){ replacePickId=null; var b=document.getElementById('tl-replace-banner'); if(b) b.style.display='none'; var lib=document.querySelector('.tl-library'); if(lib) lib.classList.remove('replace-arming'); }
  // Grab a first-frame thumbnail from a video URL (same-origin, so canvas isn't tainted). Used
  // when a replacement clip has no poster image so the timeline tile doesn't go blank/grey.
  function captureVideoPoster(url, cb){
    try{
      var v=document.createElement('video'); v.muted=true; v.preload='metadata'; v.src=url; var done=false;
      function fin(x){ if(!done){ done=true; try{ v.removeAttribute('src'); v.load(); }catch(e){} cb(x||''); } }
      v.addEventListener('loadeddata', function(){ try{ v.currentTime=Math.min(0.15, (v.duration||1)/3); }catch(e){ fin(''); } });
      v.addEventListener('seeked', function(){ try{ var c=document.createElement('canvas');
        c.width=v.videoWidth||144; c.height=v.videoHeight||256;
        c.getContext('2d').drawImage(v,0,0,c.width,c.height); fin(c.toDataURL('image/jpeg',0.72)); }catch(e){ fin(''); } });
      v.addEventListener('error', function(){ fin(''); });
      setTimeout(function(){ fin(''); }, 6000);
    }catch(e){ cb(''); }
  }
  function applyReplace(it){
    var id=replacePickId; if(id===null) return;
    var s=scenes.filter(function(x){return x.id===id;})[0]; if(!s){ cancelReplacePick(); return; }
    replacedMap[id]={path:it.path, type:it.type, name:it.name};
    s.clip=(it.type==='video')?it.url:''; s.path=it.path; s.kind=(it.type==='video')?'clip':'image';
    if(it.type==='video'){
      if(it.poster){ s.poster=it.poster; }
      else { s.poster=''; captureVideoPoster(it.url, function(dataUrl){ if(dataUrl && replacedMap[id]){ s.poster=dataUrl; layout(); } }); }
    } else { s.poster=it.url; }
    cancelReplacePick(); layout(); markDirty(); selectClip(id);
    // show the NEW media in the preview right away (the baked render can't reflect it)
    leaveRenderMode(); previewSceneById(id);
  }

  // ---- Library (drag media / sfx onto the timeline) ----
  function setupLibrary(){
    var tabs=rootEl.querySelectorAll('.tl-lib-tab');
    Array.prototype.forEach.call(tabs, function(tab){
      tab.addEventListener('click', function(){
        Array.prototype.forEach.call(tabs, function(t){ t.classList.remove('active'); });
        tab.classList.add('active');
        var which=tab.getAttribute('data-lib');
        document.getElementById('tl-lib-media').hidden = which!=='media';
        document.getElementById('tl-lib-sfx').hidden = which!=='sfx';
      });
    });
    fetch('/timeline-library?slug='+encodeURIComponent(slug)).then(function(r){return r.json();}).then(function(d){
      window.SFX_TAX = (d&&d.sfx_taxonomy) || {roles:[],reactions:[]};
      renderMediaLib(document.getElementById('tl-lib-media'), (d&&d.media)||[], (d&&d.global_media)||[]);
      renderSfxLib(document.getElementById('tl-lib-sfx'), (d&&d.sfx)||[]);
    }).catch(function(){
      document.getElementById('tl-lib-media').innerHTML='<div class="tl-lib-loading">Could not load media.</div>';
      document.getElementById('tl-lib-sfx').innerHTML='<div class="tl-lib-loading">Could not load sounds.</div>';
    });
  }
  // one shared audio player so previewing a sound stops the previous one
  var libAudio=null;
  function playSound(url, btn){
    try{
      if(libAudio){ libAudio.pause(); }
      if(libAudio && libAudio._btn){ libAudio._btn.classList.remove('playing'); }
      libAudio=new Audio(url); libAudio._btn=btn||null;
      if(btn) btn.classList.add('playing');
      libAudio.play().catch(function(){});
      libAudio.addEventListener('ended', function(){ if(btn) btn.classList.remove('playing'); });
    }catch(e){}
  }
  function libItemEl(it, kind){
    var el=document.createElement('div'); el.className='tl-lib-item'; el.draggable=true;
    if(kind==='media'){
      if(it.type==='video'){
        // lazy src (no eager load) - a library with 100+ clips otherwise crashes the renderer
        var poster=it.poster?(' poster="'+esc(it.poster)+'"'):'';
        var sub=it.project?('<div class="tl-lib-proj"'+(it.current?' data-current="1"':'')+' title="'+esc(it.project)+'">'+esc(it.project)+'</div>'):'';
        el.innerHTML='<div class="tl-lib-vidwrap"><video data-lazy-src="'+esc(it.url)+'"'+poster+' muted preload="none" playsinline></video></div>'
          +'<div class="tl-lib-name">'+esc(it.name)+'</div>'+sub;
        var vid=el.querySelector('video');
        // Keep the lightweight muted hover preview, but no play/pause control overlays the media.
        el.addEventListener('mouseenter', function(){ if(window.LazyVideo)window.LazyVideo.ensure(vid); vid.muted=true; vid.play().catch(function(){}); });
        el.addEventListener('mouseleave', function(){ vid.pause(); try{ vid.currentTime=0; }catch(e){} });
      } else {
        el.innerHTML='<img src="'+esc(it.url)+'" alt="">'+'<div class="tl-lib-name">'+esc(it.name)+'</div>';
      }
    } else {
      el.classList.add('tl-lib-sound');
      var catTxt=sfxCatText(it);
      var tagBtn=it.labelable?('<button type="button" class="tl-snd-cat" title="Change this sound\\u2019s category">'+esc(catTxt)+' \\u270E</button>'):('<span class="tl-snd-cat readonly">'+esc(catTxt)+'</span>');
      el.innerHTML='<button type="button" class="tl-lib-play tl-snd-play" title="Play sound">\\u25B6</button>'
        +'<div class="tl-snd-info"><div class="tl-lib-name">'+esc(it.name)+'</div>'+tagBtn+'</div>';
      var sb=el.querySelector('.tl-snd-play');
      sb.addEventListener('click', function(ev){ ev.stopPropagation(); playSound(it.url, sb); });
      var cb=el.querySelector('button.tl-snd-cat');
      if(cb) cb.addEventListener('click', function(ev){ ev.stopPropagation(); openSfxCatEditor(it, cb); });
    }
    if(kind==='media'){
      el.addEventListener('click', function(ev){ if(replacePickId!==null){ ev.preventDefault(); applyReplace(it); } });
    }
    el.addEventListener('dragstart', function(e){ el.classList.add('dragging'); e.dataTransfer.setData('text/plain', JSON.stringify({kind:kind, item:it})); e.dataTransfer.effectAllowed='copy'; });
    el.addEventListener('dragend', function(){ el.classList.remove('dragging'); });
    return el;
  }
  function renderMediaLib(box, items, globalItems){
    if(!box) return;
    globalItems = globalItems || [];
    if(!items.length && !globalItems.length){ box.innerHTML='<div class="tl-lib-loading">No project media.</div>'; return; }
    // Primary tabs by TYPE: Videos (mp4), Images (pictures), Hook (female-influencer clips
    // from the hook finder, pooled across ALL projects), Declined (passed-over scraped
    // clips), and All projects (scraped footage from EVERY project, newest first).
    var groups={videos:[], images:[], hook:[], declined:[], global:globalItems};
    items.forEach(function(it){
      if(it.kind==='hook'){ groups.hook.push(it); }
      else if(it.kind==='declined'){ groups.declined.push(it); }
      else if(it.type==='video'){ groups.videos.push(it); }
      else { groups.images.push(it); }
    });
    var defs=[['videos','\\uD83C\\uDFAC Videos'],['images','\\uD83D\\uDDBC Images'],['hook','\\uD83D\\uDC83 Hook'],['declined','\\uD83D\\uDEAB Declined'],['global','\\uD83C\\uDF10 All projects']];
    var keys=defs.filter(function(d){ return groups[d[0]].length; });
    box.innerHTML='<div class="tl-sublib-tabs"></div><input type="text" class="tl-sublib-search" placeholder="Filter clips\\u2026" hidden><div class="tl-sublib-grid"></div>';
    var tabsEl=box.querySelector('.tl-sublib-tabs'), gridEl=box.querySelector('.tl-sublib-grid');
    var searchEl=box.querySelector('.tl-sublib-search');
    var curKey=null;
    function draw(k, q){
      q=(q||'').toLowerCase().trim();
      gridEl.innerHTML='';
      (groups[k]||[]).forEach(function(it){
        if(q && (it.name||'').toLowerCase().indexOf(q)===-1 && (it.project||'').toLowerCase().indexOf(q)===-1) return;
        gridEl.appendChild(libItemEl(it,'media'));
      });
      // Load the first frame of THIS tab's clips immediately (no hover needed). The huge
      // "All projects" tab stays lazy so it doesn't spin up hundreds of decoders at once.
      if(window.LazyVideo){
        if(k==='global'){ window.LazyVideo.observe(gridEl); }
        else { Array.prototype.forEach.call(gridEl.querySelectorAll('video[data-lazy-src]'), function(v){
          v.preload='metadata'; window.LazyVideo.ensure(v); }); }
      }
    }
    function show(k){
      curKey=k;
      Array.prototype.forEach.call(tabsEl.children,function(t){ t.classList.toggle('active', t.getAttribute('data-k')===k); });
      // the "All projects" tab can hold hundreds of clips -> give it a filter box
      searchEl.hidden = (k!=='global'); searchEl.value='';
      draw(k, '');
    }
    searchEl.addEventListener('input', function(){ draw(curKey, this.value); });
    keys.forEach(function(d,i){ var k=d[0]; var t=document.createElement('button'); t.type='button'; t.className='tl-sublib-tab'+(i?'':' active'); t.setAttribute('data-k',k); t.textContent=d[1]+' ('+groups[k].length+')'; t.addEventListener('click',function(){show(k);}); tabsEl.appendChild(t); });
    if(keys.length) show(keys[0][0]);
  }
  // Map a sound onto category roles. Trainer-labeled sounds carry it.roles/it.reactions;
  // unlabeled ones are inferred from their category/name so the filter is still useful.
  var ROLE_KEYWORDS={
    transition:['whoosh','transition','swish','swoosh','swoop','swipe','whip','woosh','trans'],
    reaction:['reaction','ding','correct','wrong','error','cash','money','pop','boing','sparkle','laugh','gasp','aww','yay','applause','clap','fail','success','coin','bell'],
    impact:['impact','boom','hit','slam','bass','thud','punch','drop','smash','stinger'],
    ui:['ui','click','tap','tick','type','key','notif','bubble','beep','blip','caption'],
    riser:['riser','rise','build','uplift','sweep'],
    hook_riser:['hook','hookriser'],
    accent:['accent','shimmer','chime','glitter','magic','twinkle']
  };
  function inferredRoles(it){
    var r=(it.roles||[]).slice(); if(r.length) return r;
    var hay=((it.category||'')+' '+(it.name||'')).toLowerCase(), out=[];
    Object.keys(ROLE_KEYWORDS).forEach(function(role){ if(ROLE_KEYWORDS[role].some(function(k){return hay.indexOf(k)!==-1;})) out.push(role); });
    return out;
  }
  function soundInCat(it, roleKey, reactKey){
    if(!roleKey||roleKey==='all') return true;
    if(inferredRoles(it).indexOf(roleKey)===-1) return false;
    if(roleKey==='reaction' && reactKey && reactKey!=='all'){
      if((it.reactions||[]).indexOf(reactKey)!==-1) return true;
      var hay=((it.name||'')+' '+(it.category||'')).toLowerCase();
      return hay.indexOf(reactKey.replace(/_/g,' '))!==-1 || hay.indexOf(reactKey)!==-1;
    }
    return true;
  }
  function renderSfxLib(box, items){
    if(!box) return;
    libSounds=items.slice();            // feeds the FX inspector's sound dropdown
    if(sel&&sel.type==='fx') syncFx();  // repopulate dropdown if an FX is already selected
    if(sel&&sel.type==='overlay') syncOverlayInspector();
    var tax=window.SFX_TAX||{roles:[],reactions:[]};
    box.innerHTML='<input type="text" class="tl-sfx-search" placeholder="Search sounds\\u2026">'
      +'<div class="tl-sfx-cats" id="tl-sfx-cats"></div>'
      +'<div class="tl-sublib-grid" id="tl-sfx-grid"></div>';
    var grid=box.querySelector('#tl-sfx-grid'), search=box.querySelector('.tl-sfx-search');
    var catsEl=box.querySelector('#tl-sfx-cats');
    var curCat='all', curSub='all';
    // only offer category chips that actually match at least one loaded sound
    var cats=[['all','All']].concat((tax.roles||[]).filter(function(r){
      return r[0]!=='skip' && items.some(function(it){ return soundInCat(it,r[0],'all'); }); }));
    function subsFor(cat){
      // only the Reaction category actually has subcategories (its reaction slugs)
      if(cat!=='reaction') return [];
      return (tax.reactions||[]).filter(function(r){ return items.some(function(it){ return soundInCat(it,'reaction',r[0]); }); });
    }
    function closeSubPop(){ document.querySelectorAll('.tl-sfx-subpop').forEach(function(p){ p.remove(); }); }
    // subcategories live in a small popup anchored under the category button - and ONLY exist for
    // categories that actually have subcategories (currently Reaction).
    function openSubPop(anchor, cat){
      closeSubPop();
      var subs=subsFor(cat); if(!subs.length) return;
      var pop=document.createElement('div'); pop.className='tl-sfx-subpop';
      [['all','All']].concat(subs).forEach(function(s){ var b=document.createElement('button'); b.type='button';
        b.className='tl-sfx-cat tl-sfx-subchip'+(curSub===s[0]?' on':''); b.textContent=s[1];
        b.onclick=function(){ curSub=s[0]; closeSubPop(); drawCats(); draw(search.value); }; pop.appendChild(b); });
      document.body.appendChild(pop);
      var r=anchor.getBoundingClientRect();
      pop.style.left=Math.max(8, Math.min(r.left, window.innerWidth-pop.offsetWidth-10))+'px';
      pop.style.top=(r.bottom+5)+'px';
      setTimeout(function(){ document.addEventListener('pointerdown', function h(ev){
        if(!pop.contains(ev.target) && ev.target!==anchor){ closeSubPop(); document.removeEventListener('pointerdown',h); } }); }, 0);
    }
    function drawCats(){
      catsEl.innerHTML='';
      cats.forEach(function(c){ var hasSubs=subsFor(c[0]).length>0;
        var b=document.createElement('button'); b.type='button';
        b.className='tl-sfx-cat'+(curCat===c[0]?' on':'')+(hasSubs?' has-subs':'');
        b.innerHTML=esc(c[1])+(hasSubs?' <span class="tl-sfx-caret">\\u25BE</span>':'');
        b.onclick=function(){ curCat=c[0]; curSub='all'; drawCats(); draw(search.value);
          if(hasSubs) openSubPop(b, c[0]); else closeSubPop(); };
        catsEl.appendChild(b); });
    }
    function draw(q){ grid.innerHTML=''; q=(q||'').toLowerCase().trim(); var n=0;
      items.forEach(function(it){
        if(!soundInCat(it,curCat,curSub)) return;
        if(q && (it.name||'').toLowerCase().indexOf(q)===-1 && (it.category||'').toLowerCase().indexOf(q)===-1) return;
        grid.appendChild(libItemEl(it,'sfx')); n++; });
      if(!n) grid.innerHTML='<div class="tl-lib-loading">No matching sounds.</div>'; }
    search.addEventListener('input', function(){ draw(this.value); });
    drawCats(); draw('');
  }
  // ---- subcategorize a library sound into the existing categories (writes sfx_labels.json) ----
  function sfxCatText(it){
    var roles=(it.roles||[]), reacts=(it.reactions||[]);
    if(!roles.length) return it.labelable?'uncategorized':(it.category||'sound');
    return roles.map(function(r){ return r==='reaction'&&reacts.length?('reaction: '+reacts.join('/')):r; }).join(' + ');
  }
  function openSfxCatEditor(it, anchor){
    document.querySelectorAll('.tl-catpop').forEach(function(p){ p.remove(); });
    var tax=window.SFX_TAX||{roles:[],reactions:[]};
    var roles=(it.roles||[]).slice(), reacts=(it.reactions||[]).slice();
    var pop=document.createElement('div'); pop.className='tl-catpop';
    function render(){
      var rHtml=(tax.roles||[]).map(function(r){ return '<button type="button" class="tl-cat-role'+(roles.indexOf(r[0])!==-1?' on':'')+'" data-r="'+r[0]+'">'+esc(r[1])+'</button>'; }).join('');
      var showReact=roles.indexOf('reaction')!==-1;
      var xHtml=showReact?('<div class="tl-cat-sub"><div class="tl-cat-lbl">Reaction subcategory</div><div class="tl-cat-chips">'
        +(tax.reactions||[]).map(function(r){ return '<button type="button" class="tl-cat-rx'+(reacts.indexOf(r[0])!==-1?' on':'')+'" data-x="'+r[0]+'">'+esc(r[1])+'</button>'; }).join('')+'</div></div>'):'';
      pop.innerHTML='<div class="tl-cat-lbl">Category for &ldquo;'+esc(it.name)+'&rdquo;</div><div class="tl-cat-roles">'+rHtml+'</div>'+xHtml
        +'<div class="tl-cat-actions"><button type="button" class="button secondary tl-cat-cancel">Cancel</button><button type="button" class="button primary tl-cat-save">Save</button></div>';
      pop.querySelectorAll('.tl-cat-role').forEach(function(b){ b.onclick=function(){ var r=b.getAttribute('data-r');
        if(r==='skip'){ roles=(roles.indexOf('skip')!==-1)?[]:['skip']; }
        else { roles=roles.filter(function(x){return x!=='skip';}); var i=roles.indexOf(r); if(i!==-1) roles.splice(i,1); else roles.push(r); }
        render(); }; });
      pop.querySelectorAll('.tl-cat-rx').forEach(function(b){ b.onclick=function(){ var x=b.getAttribute('data-x'); var i=reacts.indexOf(x); if(i!==-1) reacts.splice(i,1); else reacts.push(x); if(roles.indexOf('reaction')===-1) roles.push('reaction'); render(); }; });
      pop.querySelector('.tl-cat-cancel').onclick=function(){ pop.remove(); };
      pop.querySelector('.tl-cat-save').onclick=function(){
        var body={file:it.file, roles:roles, reactions:reacts};
        fetch('/sfx-label',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})
          .then(function(r){return r.json();}).then(function(d){
            if(d&&d.ok){ it.roles=d.roles||roles; it.reactions=d.reactions||reacts; pop.remove();
              renderSfxLib(document.getElementById('tl-lib-sfx'), libSounds); }
            else alert((d&&d.error)||'Could not save the category.');
          }).catch(function(){ alert('Could not save the category.'); });
      };
    }
    render();
    document.body.appendChild(pop);
    var r=anchor.getBoundingClientRect();
    pop.style.top=Math.min(window.innerHeight-pop.offsetHeight-10, r.bottom+6)+'px';
    pop.style.left=Math.min(window.innerWidth-pop.offsetWidth-10, Math.max(10, r.left))+'px';
    setTimeout(function(){ document.addEventListener('pointerdown', function h(ev){ if(!pop.contains(ev.target)){ pop.remove(); document.removeEventListener('pointerdown', h); } }); }, 0);
  }
  function enableDrop(trackEl, accept, handler){
    if(!trackEl) return;
    trackEl.addEventListener('dragover', function(e){ e.preventDefault(); e.dataTransfer.dropEffect='copy'; trackEl.classList.add('drop-ok'); });
    trackEl.addEventListener('dragleave', function(){ trackEl.classList.remove('drop-ok'); });
    trackEl.addEventListener('drop', function(e){
      e.preventDefault(); trackEl.classList.remove('drop-ok');
      var data; try{ data=JSON.parse(e.dataTransfer.getData('text/plain')); }catch(err){ return; }
      if(!data || data.kind!==accept) return;
      var rect=trackEl.getBoundingClientRect();
      var x=e.clientX-rect.left+trackEl.scrollLeft;
      handler(data.item, Math.max(0, x/SCALE));
    });
  }
  enableDrop(clipsEl, 'media', function(it, atSec){
    var vis=visible(), acc=0, idx=vis.length;
    for(var i=0;i<vis.length;i++){ if(atSec < acc+vis[i].dur/2){ idx=i; break; } acc+=vis[i].dur; }
    var ns={ id:'add-'+Date.now(), label:it.name, dur:Math.max(1.5, it.type==='video'?3.0:2.5), poster:it.poster||it.url, clip:it.type==='video'?it.url:'', path:it.path, kind:it.type==='video'?'clip':'image', added:true, replaceable:false, speaker:false, source_trim:it.type==='video'?+(model.default_source_trim||0):0, source_speed:1, speed:1, media_identity:it.path||it.url, overlays:[] };
    var order=scenes.filter(function(x){return !x.removed;});
    order.splice(idx,0,ns);
    scenes=order.concat(scenes.filter(function(x){return x.removed;}));
    layout(); markDirty(); selectClip(ns.id);
  });
  enableDrop(sfxEl, 'sfx', function(it, atSec){
    var vis=visible(), best=vis.length?vis[0].id:null, acc=0;
    for(var i=0;i<vis.length;i++){ if(atSec < acc+vis[i].dur){ best=vis[i].id; break; } acc+=vis[i].dur; }
    if(best===null){ return; }
    var off=Math.max(0, atSec-(startOf(best)||0));
    var nf={ id:'sfx-'+Date.now(), scene_id:best, label:it.name, category:it.category||'custom', offset:+off.toFixed(2), duration:it.duration||1.0, volume:0.25, enabled:true, added:true, path:it.path, url:it.url||'' };
    sfx.push(nf);
    layout(); markDirty(); selectFx(nf.id);
  });
  setupLibrary();

  window.addEventListener('beforeunload', function(e){ if(dirty){ e.preventDefault(); e.returnValue=''; } });
  layout(); if(typeof fitZoom==='function') fitZoom(); seekTo(0); updatePlayhead();
  hist=[]; hIdx=-1; pushHistory(); syncHistButtons();
})();
</script>
<style>
  /* ---- toolbar declutter: Render + Agent rework open small popups; the original
     action buttons/toggles keep their ids INSIDE the popups (zero behavior change) ---- */
  .tl-toolbar { position:relative; z-index:1000; overflow:visible !important; }
  .tl-popwrap { position:relative; display:inline-flex; z-index:1001; }
  .tl-pop { position:fixed; z-index:2147483000; min-width:250px;
    background:var(--bg-raised); border:1px solid var(--line-strong); border-radius:12px;
    padding:12px; display:flex; flex-direction:column; gap:10px; box-shadow:var(--sh-2); }
  .tl-pop[hidden] { display:none; }
  .tl-pop .button { width:100%; margin:0; }
  .tl-pop .tl-cap-toggle, .tl-pop .tl-chk { padding:2px 0; border:0; background:none; }
  .tl-pop-field { margin:2px 0 -5px; color:var(--muted); font-size:11px; font-weight:700; text-transform:uppercase; letter-spacing:.05em; }
  .tl-pop select { width:100%; margin:0; }
  /* histzoom strip + total + audio mixer: keyboard/mouse cover these - hide the chrome
     (elements stay in the DOM so the editor JS keeps working) */
  .tl-meta, .tl-mixer-wrap { display:none !important; }
  /* SFX + Cut-SFX chips: SHARP, pointed time markers - the LEFT edge is the exact play time
     (crisp vertical edge + a downward tip). The name shows ONLY on hover (never on select),
     and stays available as the title tooltip. */
  #tl-sfx .tl-fx, #tl-trtrack .tl-fx {
    width:14px !important; min-width:14px; justify-content:center; overflow:visible;
    padding:0 !important; font-size:0 !important;
    border-radius:0 5px 5px 0;                 /* sharp on the left (the exact-time edge) */
    border-left:2px solid var(--accent);        /* crisp needle exactly at the start time */
  }
  #tl-sfx .tl-fx::before, #tl-trtrack .tl-fx::before {
    content:"\\266A"; font-size:9.5px; color:var(--accent); line-height:1;
  }
  /* the pointed tip: a small downward triangle whose point sits on the exact-time edge */
  #tl-sfx .tl-fx::after, #tl-trtrack .tl-fx::after {
    content:""; position:absolute; left:-2px; top:-5px; width:0; height:0;
    border-left:5px solid var(--accent); border-top:5px solid transparent; border-bottom:5px solid transparent;
    transform:rotate(45deg); transform-origin:left top;
  }
  /* name on HOVER only (deliberately NOT on .selected) */
  #tl-sfx .tl-fx:hover, #tl-trtrack .tl-fx:hover {
    width:auto !important; max-width:220px; padding:0 8px 0 6px !important;
    font-size:11px !important; z-index:8;
  }
  #tl-sfx .tl-fx:hover::before, #tl-trtrack .tl-fx:hover::before { margin-right:5px; }
  #tl-sfx .tl-fx.selected, #tl-trtrack .tl-fx.selected { box-shadow:0 0 0 2px var(--accent); }
  /* sound library rows: inline play button instead of the video-thumb center overlay */
  .tl-lib-sound { display:flex; align-items:center; gap:9px; padding:7px 10px; }
  .tl-lib-sound .tl-lib-play {
    position:static !important; transform:none !important; width:26px; height:26px;
    font-size:10px; border-width:1px; flex:0 0 auto;
  }
  .tl-lib-sound .tl-lib-name { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .tl-snd-info { flex:1; min-width:0; display:flex; flex-direction:column; gap:2px; }
  .tl-snd-cat { align-self:flex-start; max-width:100%; padding:1px 7px; font-size:10px; font-weight:700; border-radius:99px;
    border:1px solid var(--line); background:var(--accent-subtle); color:var(--accent); cursor:pointer; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; box-shadow:none; width:auto; min-width:0; }
  .tl-snd-cat.readonly { background:var(--bg-input); color:var(--faint); cursor:default; border-style:dashed; }
  button.tl-snd-cat:hover { border-color:var(--accent); color:var(--text); }
  .tl-catpop { position:fixed; z-index:2147483600; width:min(300px,92vw); background:var(--bg-raised); border:1px solid var(--line-strong); border-radius:12px; box-shadow:var(--sh-3); padding:12px; display:flex; flex-direction:column; gap:9px; }
  .tl-cat-lbl { font-size:11px; font-weight:700; text-transform:uppercase; letter-spacing:.04em; color:var(--muted); }
  .tl-cat-roles, .tl-cat-chips { display:flex; flex-wrap:wrap; gap:5px; }
  .tl-cat-role, .tl-cat-rx { width:auto; min-width:0; padding:5px 9px; font-size:11.5px; font-weight:600; border-radius:8px; border:1px solid var(--line); background:transparent; color:var(--muted); cursor:pointer; box-shadow:none; }
  .tl-cat-rx { font-size:10.5px; padding:4px 8px; }
  .tl-cat-role:hover, .tl-cat-rx:hover { border-color:var(--accent); color:var(--text); }
  .tl-cat-role.on, .tl-cat-rx.on { background:var(--accent-subtle); border-color:var(--accent); color:var(--text); }
  .tl-cat-sub { display:flex; flex-direction:column; gap:5px; border-top:1px solid var(--line); padding-top:8px; }
  .tl-cat-actions { display:flex; justify-content:flex-end; gap:8px; margin-top:2px; }
  .tl-cat-actions .button { width:auto; min-width:0; padding:6px 14px; font-size:12px; margin:0; }

  /* ============ ONE-WINDOW LAYOUT: no page scroll; library beside the player ============ */
  body:has(#timeline-root) main { height:100vh; max-height:100vh; overflow:hidden;
    display:flex; flex-direction:column; padding-top:8px; }
  /* back arrow floats onto the toolbar row (top-left) to save vertical space.
     MUST sit above the toolbar panel (z-index:1000) or it gets painted over and the
     user has no way back. The toolbar reserves a 54px left gap for exactly this button. */
  body.page-timeline .top { position:fixed; top:11px; left:11px; z-index:1200; margin:0; padding:0; pointer-events:none; }
  /* clean, flat back button (was a pixel-art shadowed control that looked out of place here) */
  body.page-timeline .top .back-arrow { pointer-events:auto; width:34px; height:34px; padding:0;
    border-radius:9px; border:1px solid var(--line-strong); background:var(--bg-raised);
    box-shadow:none; transform:none; display:flex; align-items:center; justify-content:center; color:var(--text); }
  body.page-timeline .top .back-arrow:hover { border-color:var(--accent); background:var(--bg-overlay);
    transform:none; box-shadow:none; }
  body.page-timeline .top .back-arrow:active { transform:translateY(1px); box-shadow:none; }
  body.page-timeline .top .back-arrow svg { width:17px; height:17px; }
  #timeline-root {
    flex:1 1 auto; min-height:0; display:grid; gap:12px; align-content:stretch;
    grid-template-columns: minmax(280px, 380px) 1fr;
    /* taller player + library rows, shorter tracks row (the captions lane was removed) */
    grid-template-rows: auto minmax(0, 1.9fr) minmax(96px, 0.62fr);
    /* toolbar box ends at the player's right edge; the library spans BOTH top rows so it
       reaches up to the top and is as large as possible */
    grid-template-areas: "toolbar library" "stagearea library" "tracks tracks";
  }
  /* compact toolbar: smaller box + all three buttons on ONE line (no wrap) */
  #timeline-root .tl-toolbar { grid-area:toolbar; margin:0; padding:6px 8px 6px 50px; align-content:center; min-height:0; }
  #timeline-root .tl-toolbar .tl-actions { flex-wrap:nowrap; gap:5px; }
  #timeline-root .tl-actions .tl-save-btn { font-size:11px; padding:6px 8px; }
  #timeline-root .tl-actions .tl-render-btn { font-size:11px; padding:6px 10px; }
  #timeline-root .tl-actions .tl-rework-btn { font-size:11px; padding:6px 8px; }
  /* player alone on the left; the LIBRARY takes the whole former inspector column.
     Flex-center so the 9:16 stage fills the full row height (definite height from the grid
     row) and stays centered - the box then derives its width from that height. */
  #timeline-root .tl-grid.tl-top { grid-area:stagearea; margin:0; min-height:0;
    display:flex; align-items:center; justify-content:center; }
  #timeline-root .tl-player { height:100%; width:100%; padding:4px; min-height:0; }
  /* inspector: floating popup (no fixed slot). Opens on sound/visual selection or via
     right-click on a clip -> "Open inspector". */
  #timeline-root .tl-inspector { display:none; }
  #timeline-root .tl-inspector.open {
    display:block; position:fixed; top:64px; right:16px; z-index:160;
    width:min(330px, 92vw); max-height:calc(100vh - 84px); overflow-y:auto;
    background:var(--bg-raised); border:1px solid var(--line-strong);
    box-shadow:var(--sh-3); border-radius:14px;
  }
  #timeline-root .tl-inspector .tl-insp-close {
    position:absolute; top:8px; right:8px; width:26px; height:26px; padding:0;
    border-radius:8px; border:1px solid var(--line); background:transparent;
    color:var(--muted) !important; font-size:13px; line-height:1; cursor:pointer;
  }
  #timeline-root .tl-inspector .tl-insp-close:hover { color:var(--danger) !important; border-color:var(--danger); }
  #timeline-root .tl-library { grid-area:library; margin:0; min-height:0;
    display:flex; flex-direction:column; }
  #timeline-root .tl-library .tl-lib-body { flex:1 1 auto; min-height:0; overflow-y:auto; }
  #timeline-root .tl-stage { grid-area:tracks; margin:0; min-height:0; overflow:auto; }
  /* the 9:16 stage fills the whole player row; width derives from the height via the
     base aspect-ratio:9/16 so the visible box matches the video with no letterboxing */
  body.page-timeline .tl-stage-view { height:100%; max-height:100%; }
  body.page-timeline .tl-player h2, body.page-timeline .tl-inspector h2,
  body.page-timeline .tl-library h2 { font-size:13px; margin-bottom:8px; }
  #timeline-root .tl-inspector { max-height:100%; overflow-y:auto; }
  /* change-script modal: centered fixed overlay (was opening at a weird offset) */
  body.page-timeline .tl-script-overlay:not([hidden]) { position:fixed; inset:0; z-index:950;
    display:flex; align-items:center; justify-content:center; }
  @media (max-width: 1000px) {
    #timeline-root { grid-template-columns:1fr; grid-template-rows:auto auto auto minmax(160px,1fr);
      grid-template-areas:"toolbar" "stagearea" "library" "tracks"; }
    body:has(#timeline-root) main { height:auto; max-height:none; overflow:visible; }
  }
</style>
<script>
(function(){
  /* popup toggling for Render / Agent rework + auto-save before render */
  function wirePop(openId, popId){
    var open=document.getElementById(openId), pop=document.getElementById(popId);
    if(!open||!pop) return;
    open.addEventListener('click', function(e){
      e.stopPropagation();
      var show=pop.hasAttribute('hidden');
      document.querySelectorAll('.tl-pop').forEach(function(p){ p.setAttribute('hidden',''); });
      if(show){
        pop.removeAttribute('hidden');
        var anchor=open.getBoundingClientRect(),box=pop.getBoundingClientRect();
        var left=Math.max(8,Math.min(anchor.left,window.innerWidth-box.width-8));
        var below=anchor.bottom+8,above=anchor.top-box.height-8;
        var top=(below+box.height<=window.innerHeight-8||above<8)?below:above;
        pop.style.left=left+'px';pop.style.top=Math.max(8,top)+'px';
      }
    });
    pop.addEventListener('click', function(e){ e.stopPropagation(); });
  }
  wirePop('tl-render-open','tl-render-pop');
  wirePop('tl-rework-open','tl-rework-pop');
  document.addEventListener('click', function(){
    document.querySelectorAll('.tl-pop').forEach(function(p){ p.setAttribute('hidden',''); });
  });
  document.addEventListener('keydown', function(e){
    if(e.key==='Escape') document.querySelectorAll('.tl-pop').forEach(function(p){ p.setAttribute('hidden',''); });
  });
  /* pressing Render saves the timeline automatically first (same save path as the button) */
  var renderBtn=document.getElementById('tl-render');
  if(renderBtn) renderBtn.addEventListener('click', function(){
    var s=document.getElementById('tl-save');
    if(s && !s.disabled && s.classList.contains('tl-unsaved')) s.click();
  }, true);
  /* opening the script modal from inside the popup: close the popup first */
  var sb=document.getElementById('tl-script-btn');
  if(sb) sb.addEventListener('click', function(){
    document.querySelectorAll('.tl-pop').forEach(function(p){ p.setAttribute('hidden',''); });
  }, true);
  /* floating inspector: close button + Escape + close when the mouse leaves it */
  var insp=document.getElementById('tl-inspector');
  if(insp){
    var x=document.createElement('button'); x.type='button'; x.className='tl-insp-close';
    x.textContent='✕'; x.setAttribute('aria-label','Close inspector');
    x.addEventListener('click', function(){ insp.classList.remove('open'); });
    insp.insertBefore(x, insp.firstChild);
    document.addEventListener('keydown', function(e){
      if(e.key==='Escape') insp.classList.remove('open');
    });
    // Close on mouse-leave with a short grace period (re-entering cancels), so it does not
    // snap shut the instant the cursor clips a corner while reaching for a control.
    var leaveTimer=null;
    insp.addEventListener('mouseleave', function(){
      leaveTimer=setTimeout(function(){ insp.classList.remove('open'); }, 450);
    });
    insp.addEventListener('mouseenter', function(){ if(leaveTimer){ clearTimeout(leaveTimer); leaveTimer=null; } });
  }
})();
</script>
"""


def _find_scene_image(project_dir, asset):
    if not asset:
        return None
    direct = Path(asset)
    if direct.is_absolute() and direct.exists() and is_image_path(direct):
        return direct
    for sub in ("web images", "gpt images", "local media", "speaker"):
        candidate = project_dir / sub / asset
        if candidate.exists() and is_image_path(candidate):
            return candidate
    return None


def _timeline_media(project_dir, scene, config, index, manifest_clips=None):
    """Return (poster_url, clip_url, source_image_path). Resolves the clip with the
    SAME logic the renderer uses (pipeline.scene_clip_path: derived name, then the
    seedance_manifest.json mapping) so the timeline mirrors exactly what the agent
    cut/rendered — Seedance clips and the speaker-hook clip included."""
    img = _find_scene_image(project_dir, scene.get("asset"))
    clip_url = ""
    poster_url = ""
    clip_path = None
    try:
        if pipeline.scene_uses_seedance(config, scene):
            clip_path = pipeline.scene_clip_path(
                config, scene, index,
                clip_dir=project_dir / "seedance 2.0", manifest_map=manifest_clips,
            )
    except Exception:
        clip_path = None
    # explicit clip name (e.g. speaker_hook.mp4) even if scene isn't seedance-flagged
    if clip_path is None and scene.get("clip"):
        cand = project_dir / "seedance 2.0" / Path(scene["clip"]).name
        if cand.exists() and is_video_path(cand):
            clip_path = cand
    if clip_path is not None and is_video_path(clip_path):
        clip_url = link_for(clip_path)
        poster = clip_path.with_suffix(".poster.jpg")
        if not poster.exists():
            pipeline.extract_poster_frame(clip_path, poster)
        if poster.exists():
            poster_url = link_for(poster)
    if not poster_url and img:
        poster_url = link_for(img)
    return poster_url, clip_url, img, clip_path


_CLIP_DUR_CACHE = {}
def _clip_source_seconds(clip_path):
    """Full playable length (seconds) of a clip file, cached in-process by (path, mtime, size).
    Used to cap timeline stretching so a clip can never be dragged past its own footage (which
    would freeze the last frame). Returns 0.0 when unknown (-> the editor applies no extra cap)."""
    if not clip_path:
        return 0.0
    try:
        p = Path(clip_path)
        st = p.stat()
        key = (str(p), int(st.st_mtime), int(st.st_size))
    except Exception:
        return 0.0
    if key in _CLIP_DUR_CACHE:
        return _CLIP_DUR_CACHE[key]
    dur = 0.0
    try:
        ffmpeg = pipeline.find_ffmpeg()
        ffprobe = pipeline.find_ffprobe(ffmpeg)
        dur = float(sfx_agent.media_duration(p, ffprobe) or 0.0)
    except Exception:
        dur = 0.0
    _CLIP_DUR_CACHE[key] = dur
    return dur


def project_has_render(slug):
    """True once the agent has produced at least one final render for this project.
    The timeline editor is only available after that first render."""
    project_dir = safe_project_dir(slug)
    if not project_dir:
        return False
    try:
        report = read_project_report(project_dir)
        if existing_report_path(report, "video"):
            return True
    except Exception:
        pass
    return latest_media(project_dir / "renders", {".mp4", ".webm"}) is not None


def resolve_replace_media_paths(slug, scene_ids):
    """Map marked timeline clip ids -> their replaceable web-image asset paths."""
    project_dir = safe_project_dir(slug)
    if not project_dir:
        return []
    try:
        config = agent_core.load_project_config(slug)
    except Exception:
        return []
    wanted = {str(x) for x in (scene_ids or [])}
    out = []
    for scene in config.get("scenes", []):
        if str(scene.get("id", "")) not in wanted:
            continue
        img = _find_scene_image(project_dir, scene.get("asset"))
        if img:
            out.append(str(Path(img).resolve()))
    return [p for p in out if valid_replace_path_values([p])]


def _sfx_labels_map():
    """{filename -> {roles:[], reactions:[]}} from the trainer ground truth (empty if none)."""
    try:
        data = json.loads((ROOT / "soundeffects" / "sfx_labels.json").read_text(encoding="utf-8"))
    except Exception:
        return {}
    out = {}
    for fn, rec in (data.get("labels") or {}).items():
        roles = rec.get("roles")
        if roles is None:
            roles = [rec["role"]] if rec.get("role") else []
        reactions = rec.get("reactions")
        if reactions is None:
            reactions = [rec["reaction"]] if rec.get("reaction") else []
        out[fn] = {"roles": [r for r in roles if r], "reactions": [r for r in reactions if r]}
    return out


def sfx_taxonomy():
    """The role + reaction taxonomy the timeline library lets the user pick from."""
    try:
        from tools import sfx_trainer
        roles = [[r[0], r[1]] for r in sfx_trainer.ROLES]
        data = json.loads((ROOT / "soundeffects" / "sfx_labels.json").read_text(encoding="utf-8"))
        reactions = data.get("reactions") or [list(r) for r in sfx_trainer.REACTIONS]
        reactions = [[r[0], r[1]] for r in reactions]
    except Exception:
        roles = [["transition", "Transition"], ["reaction", "Reaction"], ["impact", "Impact"],
                 ["accent", "Accent"], ["ui", "UI / click"], ["riser", "Riser"],
                 ["hook_riser", "Hook riser"], ["skip", "Skip"]]
        reactions = []
    return {"roles": roles, "reactions": reactions}


def global_sfx_library():
    """Every reusable sound effect: the builtin soundeffects/ folder plus every clip
    ever generated via text-to-audio (all projects + the global cache). Each sound carries its
    human trainer label (roles/reactions) so the timeline library can show + edit the category."""
    exts = {".wav", ".mp3", ".m4a", ".aac", ".ogg", ".flac"}
    roots = [ROOT / "soundeffects", ROOT / "wavespeed_media" / "sfx_generated"]
    projects = agent_core.PROJECTS_DIR
    if projects.exists():
        for proj in projects.iterdir():
            if proj.is_dir():
                roots.append(proj / "wavespeed_media" / "sfx_generated")
    labels = _sfx_labels_map()
    seen, out = set(), []
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*"), key=lambda p: p.name.lower()):
            if not path.is_file() or path.suffix.lower() not in exts:
                continue
            key = path.name.lower()
            if key in seen:
                continue
            seen.add(key)
            lbl = labels.get(path.name) or {}
            # library-relative? only files directly in soundeffects/ are trainer-labelable
            labelable = path.parent.resolve() == (ROOT / "soundeffects").resolve()
            out.append({
                "name": path.stem.replace("_", " "),
                "path": str(path.resolve()),
                "url": link_for(path),
                "file": path.name,
                "category": path.parent.name,
                "roles": lbl.get("roles", []),
                "reactions": lbl.get("reactions", []),
                "labelable": labelable,
                "duration": 1.0,
            })
    return out


def timeline_library_payload(slug):
    project_dir = safe_project_dir(slug)
    media = []
    seen = set()
    if project_dir:
        for kind, path in project_media_files(project_dir):
            if kind in {"render", "review"}:
                continue
            if is_image_path(path) or is_video_path(path):
                key = str(path.resolve())
                seen.add(key)
                media.append({
                    "kind": kind, "name": path.name, "path": key,
                    "url": link_for(path), "type": "video" if is_video_path(path) else "image",
                })
        # ALL downloaded scraped footage, including the DECLINED candidates (kept under
        # seedance 2.0/_candidates). These never appear in the accepted panels, but the timeline
        # library shows them so you can drag a passed-over clip back in. The hook finder's
        # female-influencer pool (_candidates/hook_influencer) gets its own "hook" bucket.
        cand_root = project_dir / "seedance 2.0" / "_candidates"
        if cand_root.exists():
            for path in sorted(cand_root.rglob("*"),
                               key=lambda p: p.stat().st_mtime if p.is_file() else 0, reverse=True):
                if not path.is_file() or not is_video_path(path):
                    continue
                if "_raw" in {p.lower() for p in path.parts}:
                    continue
                key = str(path.resolve())
                if key in seen:
                    continue
                seen.add(key)
                is_hook = "hook_influencer" in {p.lower() for p in path.parts}
                media.append({
                    "kind": "hook" if is_hook else "declined", "name": path.name, "path": key,
                    "url": link_for(path), "type": "video",
                })
    # hook/female-influencer clips are generic + reusable -> pool them from EVERY project
    for item in all_projects_hook_media(current_slug=slug):
        if item["path"] not in seen:
            seen.add(item["path"])
            media.append(item)
    return {"media": media, "sfx": global_sfx_library(),
            "sfx_taxonomy": sfx_taxonomy(),
            "global_media": all_projects_scraped_media(current_slug=slug)}


def save_sfx_label(payload):
    """Update one sound's trainer label from the timeline library. `payload` = {file, roles[],
    reactions[]}. Writes soundeffects/sfx_labels.json (invalidates the active flag so a re-run
    re-reads it). Only sounds directly in soundeffects/ are labelable."""
    fn = str((payload or {}).get("file") or "").strip()
    if not fn or "/" in fn or "\\" in fn or ".." in fn:
        return {"ok": False, "error": "bad filename"}
    target = (ROOT / "soundeffects" / fn)
    if not target.exists() or target.parent.resolve() != (ROOT / "soundeffects").resolve():
        return {"ok": False, "error": "not a labelable library sound"}
    path = ROOT / "soundeffects" / "sfx_labels.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except Exception:
        data = {}
    data.setdefault("labels", {})
    valid_roles = {r[0] for r in sfx_taxonomy()["roles"]}
    valid_reactions = {r[0] for r in sfx_taxonomy()["reactions"]}
    roles = [r for r in (payload.get("roles") or []) if r in valid_roles]
    reactions = [r for r in (payload.get("reactions") or []) if r in valid_reactions]
    if not roles:
        roles = ["skip"]
    rec = dict(data["labels"].get(fn) or {})
    rec.pop("role", None); rec.pop("reaction", None)
    rec["roles"] = roles
    rec["reactions"] = reactions if "reaction" in roles else []
    rec.setdefault("policy", "core")
    rec.setdefault("note", "")
    data["labels"][fn] = rec
    data["active"] = False           # labels changed -> next render re-reads them
    data["updated"] = int(time.time())
    try:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "file": fn, "roles": roles,
            "reactions": rec["reactions"], "active": False}


def all_projects_hook_media(current_slug=None, limit=2000):
    """Every hook-finder candidate clip (female influencer pool) across ALL projects, newest
    first: projects/*/seedance 2.0/_candidates/hook_influencer/*.mp4. The current project's own
    clips are skipped (timeline_library_payload already lists them with kind 'hook')."""
    root = agent_core.PROJECTS_DIR
    if not root.exists():
        return []
    rows = []
    for proj in root.iterdir():
        if not proj.is_dir() or proj.name.startswith("_") or proj.name == current_slug:
            continue
        hook_dir = proj / "seedance 2.0" / "_candidates" / "hook_influencer"
        if not hook_dir.exists():
            continue
        title = project_title_from_files(proj) or proj.name
        for path in hook_dir.glob("*.mp4"):
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            rows.append((mtime, {
                "kind": "hook", "name": path.name, "path": str(path.resolve()),
                "url": link_for(path), "type": "video",
                "project": title, "project_slug": proj.name,
            }))
    rows.sort(key=lambda r: r[0], reverse=True)
    return [item for _, item in rows[:limit]]


_CLIP_DEDUP_CACHE = {}
def _clip_dedup_key(path):
    """Identity of a scraped clip for cross-project de-duplication. The SAME source TikTok/X
    clip downloaded into several projects shares a (platform, clip_id) from its sidecar JSON, so
    it collapses to ONE library entry. Falls back to a byte fingerprint, then the filename."""
    try:
        st = path.stat()
        cache_key = (str(path), int(st.st_mtime), int(st.st_size))
    except OSError:
        return ("name", path.name)
    if cache_key in _CLIP_DEDUP_CACHE:
        return _CLIP_DEDUP_CACHE[cache_key]
    key = None
    sidecar = path.with_suffix(".json")
    try:
        if sidecar.exists():
            meta = json.loads(sidecar.read_text(encoding="utf-8"))
            cid = str(meta.get("clip_id") or "").strip()
            if cid:
                key = ("id", str(meta.get("platform") or "").lower(), cid)
    except Exception:
        key = None
    if key is None:
        try:
            import hashlib
            h = hashlib.sha1(); h.update(str(st.st_size).encode())
            with open(path, "rb") as fh:
                h.update(fh.read(131072))   # first 128 KB is enough to tell copies apart
            key = ("fp", h.hexdigest())
        except Exception:
            key = ("name", path.name)
    _CLIP_DEDUP_CACHE[cache_key] = key
    return key


def all_projects_scraped_media(current_slug=None, limit=5000):
    """EVERY accepted scraped clip across ALL projects (newest first) so the timeline library
    can offer footage from OTHER projects for reuse. Includes scraped_/manual_/replaced_/
    timeline_replaced_ clips; excludes derived files (speed_/capblur_/recovered_) and the
    per-project _candidates/_declined/_raw working folders (top-level *.mp4 only). The high
    limit is just a runaway guard - it shows all of them (lazy-loaded, so count doesn't matter)."""
    root = agent_core.PROJECTS_DIR
    if not root.exists():
        return []
    keep_prefixes = ("scraped_", "manual_", "replaced_", "timeline_replaced_")
    skip_prefixes = ("speed_", "capblur_", "recovered_")
    rows = []
    for proj in root.iterdir():
        if not proj.is_dir() or proj.name.startswith("_"):
            continue
        clip_dir = proj / "seedance 2.0"
        if not clip_dir.exists():
            continue
        title = project_title_from_files(proj) or proj.name
        is_current = (proj.name == current_slug)
        for path in clip_dir.glob("*.mp4"):
            name = path.name
            if name.startswith(skip_prefixes) or not name.startswith(keep_prefixes):
                continue
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            rows.append((mtime, {
                "kind": "global", "name": name, "path": str(path.resolve()),
                "url": link_for(path), "type": "video",
                "project": title, "project_slug": proj.name, "current": is_current,
            }))
    rows.sort(key=lambda r: r[0], reverse=True)
    # De-duplicate across projects: the same source clip shows ONCE (newest copy wins), so the
    # library isn't flooded with 10 identical previews of one TikTok from different projects.
    seen, out = set(), []
    for _, item in rows:
        key = _clip_dedup_key(Path(item["path"]))
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
        if len(out) >= limit:
            break
    return out


def timeline_model(slug):
    config = agent_core.load_project_config(slug)
    project_dir = agent_core.PROJECTS_DIR / slug
    manifest_clips = pipeline.seedance_manifest_map(project_dir / "seedance 2.0")
    scenes = []
    for index, scene in enumerate(config.get("scenes", [])):
        start = float(scene.get("start", 0) or 0)
        end = float(scene.get("end", start) or start)
        dur = max(0.3, end - start)
        poster, clip_url, img, clip_path = _timeline_media(project_dir, scene, config, index + 1, manifest_clips)
        is_speaker = bool(scene.get("speaker_hook"))
        label = scene.get("name") or scene.get("caption") or scene.get("script") or f"Scene {index + 1}"
        label = re.sub(r"\s+", " ", str(label)).strip()[:54] or f"Scene {index + 1}"
        if is_speaker:
            label = "\U0001f3a4 Speaker hook — " + label
        elif clip_url:
            label = "\U0001f3ac " + label
        replaceable = bool(img and "web images" in {p.lower() for p in img.parts} and not is_speaker)
        try:
            spd = max(0.5, min(2.0, float(scene.get("timeline_speed") or 1.0)))
        except (TypeError, ValueError):
            spd = 1.0
        scene_id = str(scene.get("id", index))
        try:
            source_trim = pipeline.seedance_clip_start_trim(config, scene) if clip_url else 0.0
        except Exception:
            source_trim = 0.0
        clip_name = str(scene.get("clip") or "")
        source_speed = spd if clip_name.startswith("speed_") and scene.get("timeline_speed_src") else 1.0
        media_identity = str(scene.get("scrape_clip_id") or scene.get("timeline_speed_src")
                             or scene.get("clip") or img or scene.get("asset") or scene_id)
        overlays = []
        for overlay_index, overlay in enumerate(scene.get("overlays") or []):
            if not isinstance(overlay, dict):
                continue
            item = dict(overlay)
            item.setdefault("id", f"ov-{scene_id}-{overlay_index}")
            overlays.append(item)
        scenes.append({
            "id": scene_id,
            "label": label,
            "dur": round(dur, 2),
            "poster": poster,
            "clip": clip_url,
            "kind": "speaker" if is_speaker else ("clip" if clip_url else "image"),
            "path": str(img) if img else "",
            "replaceable": replaceable,
            "speaker": is_speaker,
            "start": round(start, 3),
            "speed": spd,
            "blur_captions": bool(scene.get("blur_captions")),
            "source_speed": round(source_speed, 3),
            "source_trim": round(source_trim, 3),
            # #117 - full playable length of the underlying clip (0 = image / unknown). The editor
            # caps stretching at (source_full - source_trim)/source_speed so a clip can't be
            # dragged past its own footage into a frozen last frame.
            "source_full": round(_clip_source_seconds(clip_path), 3) if clip_url else 0.0,
            "media_identity": media_identity,
            "overlays": overlays,
        })
    # SFX events (split into boundary transitions vs. content), each individually editable.
    overrides = config.get("sfx_overrides") or {}
    # Scrape renders intentionally skip the generic keyword SFX plan and use ai_content_sfx.
    # Projects with the auto-SFX planner OFF (sfx_enabled False, e.g. an SFX-Master project that
    # mixes only its editable custom_sfx) must NOT show auto transition/content events either -
    # otherwise the editor showed sounds that the render doesn't add (or the reverse). Match the
    # renderer: no auto plan when scrape mode or sfx_enabled is off.
    plan = ([] if (config.get("clip_source") == "scrape" or not bool(config.get("sfx_enabled", True)))
            else pipeline.sfx_event_plan(config, has_speech=True))
    start_by_id = {s["id"]: s["start"] for s in scenes}
    def _ev_url(ev):
        p = ev.get("path")
        try:
            return link_for(Path(p)) if p and Path(p).exists() else ""
        except Exception:
            return ""

    transitions, content = [], []
    for ev in plan:
        ov = overrides.get(ev["id"]) or {}
        label = ev["label"]
        if ov.get("path"):  # editor picked a different sound - show its name, not the stale label
            try:
                label = Path(str(ov["path"])).stem.replace("_", " ")
            except Exception:
                pass
        if ev["transition"]:
            if ev["id"].startswith("tr-"):  # clip-boundary cut sound (CapCut connector)
                entry = {"id": ev["id"], "scene_id": ev["scene_id"], "label": label,
                         "volume": ev["volume"], "enabled": ev["enabled"],
                         "at": round(float(ev.get("at", 0) or 0), 3),
                         "url": _ev_url(ev), "path": str(ev.get("path") or "")}
                if ov.get("start_abs") is not None:  # editor moved it off the clip boundary
                    entry["start_abs"] = round(float(ov["start_abs"]), 3)
                transitions.append(entry)
        else:
            offset = round(ev["at"] - start_by_id.get(ev["scene_id"], ev["at"]), 3)
            content.append({"id": ev["id"], "scene_id": ev["scene_id"], "label": label, "category": ev["category"],
                            "offset": max(0.0, offset), "duration": ev["duration"], "volume": ev["volume"], "enabled": ev["enabled"],
                            "url": _ev_url(ev), "path": str(ev.get("path") or "")})
    # Found-footage renders get their real edit hits from config['ai_content_sfx'], not from
    # plan_sfx_events(). These sounds used to be audible in the render but absent from the
    # editor; the most obvious missing event was sfx-00 at exactly 0.0 seconds.
    content_ids = {str(item.get("id") or "") for item in content}
    scene_ranges = [(s["id"], float(s["start"]), float(s["start"]) + float(s["dur"]))
                    for s in scenes]

    def _scene_for_sfx_time(at):
        if not scene_ranges:
            return ""
        for sid, start, end in scene_ranges:
            if start <= at < end:
                return sid
        return scene_ranges[0][0] if at <= scene_ranges[0][1] else scene_ranges[-1][0]

    for raw in (config.get("ai_content_sfx") or []):
        if not isinstance(raw, dict):
            continue
        eid = str(raw.get("id") or "")
        if not eid or eid in content_ids:
            continue
        ov = overrides.get(eid) or {}
        try:
            at = max(0.0, float(ov.get("start_abs")
                                if ov.get("start_abs") is not None else raw.get("start", 0.0)))
            duration = max(0.05, float(raw.get("duration") or 0.5))
            volume = max(0.0, min(0.9, float(ov.get("volume")
                                            if ov.get("volume") is not None
                                            else raw.get("volume", 0.25))))
        except (TypeError, ValueError):
            continue
        path = str(ov.get("path") or raw.get("path") or "")
        category = str(raw.get("sfx_type") or raw.get("category") or "sound_effect")
        label = str(raw.get("label") or category.replace("_", " ")).strip().title() or "Sound Effect"
        sid = str(raw.get("scene_id") or _scene_for_sfx_time(at))
        content.append({
            "id": eid, "scene_id": sid, "label": label, "category": category,
            "start_abs": round(at, 3), "offset": 0.0, "duration": duration,
            "volume": volume, "enabled": ov.get("enabled", True) is not False,
            "url": _ev_url({"path": path}), "path": path,
            "source_duration": max(0.0, float(raw.get("source_duration") or 0.0)),
            "playback_rate": max(0.01, float(raw.get("playback_rate") or 1.0)),
        })
        content_ids.add(eid)
    # user-added sounds from earlier editing sessions (config['custom_sfx']) - marked added:true
    # so the editor round-trips them through the same add/delete path as fresh drops
    for cs in (config.get("custom_sfx") or []):
        p = str(cs.get("path") or "")
        content.append({"id": str(cs.get("id") or ""), "scene_id": str(cs.get("scene_id") or ""),
                        "label": cs.get("label") or (Path(p).stem.replace("_", " ") if p else "Sound"),
                        "category": "custom", "offset": max(0.0, float(cs.get("offset") or 0.0)),
                        "duration": float(cs.get("duration") or 1.0),
                        "source_duration": max(0.0, float(cs.get("source_duration") or 0.0)),
                        "playback_rate": max(0.01, float(cs.get("playback_rate") or 1.0)),
                        "volume": float(cs.get("volume") or 0.25),
                        "enabled": cs.get("enabled") is not False,
                        "url": _ev_url(cs), "path": p, "added": True})
    content.sort(key=lambda item: (float(item.get("start_abs")
                                         if item.get("start_abs") is not None
                                         else start_by_id.get(item.get("scene_id"), 0.0)
                                         + float(item.get("offset") or 0.0)),
                                   str(item.get("id") or "")))
    captions_on = bool(config.get("render_captions", True))
    caption_track = list(config.get("timeline_caption_track") or [])
    try:
        analysis = read_json_file(project_dir / "input" / "audio_analysis.json")
        rows = analysis.get("sentence_timestamps") if isinstance(analysis, dict) else None
        if rows:
            caption_track = rows
    except Exception:
        pass
    volumes = {
        "voice": float(config.get("audio_master_gain", 1.0) or 1.0),
        "music": float(config.get("background_music_volume", 0.0) or 0.0),
    }
    # The actual last render (captions + voice + SFX all baked in) so the preview is EXACTLY what
    # was rendered, not a silent scene-by-scene reconstruction.
    render_url = ""
    try:
        report = read_project_report(project_dir)
        rendered = existing_report_path(report, "video")
    except Exception:
        rendered = None
    if not rendered:
        rendered = latest_media(project_dir / "renders", {".mp4", ".webm"})
    if rendered and Path(rendered).exists():
        render_url = link_for(Path(rendered))
    # voice + music tracks so the EDITED sequence previews with real audio
    voice_url = ""
    voice_candidates = []
    configured_voice = str(config.get("audio_path") or "").strip()
    if configured_voice:
        voice_candidates.append(Path(configured_voice))
    voice_candidates.extend(project_dir / "input" / cand
                            for cand in ("voiceover_dehiss.wav", "voiceover.wav"))
    voice_duration = 0.0
    for vp in voice_candidates:
        if vp.exists():
            voice_url = link_for(vp)
            voice_duration = _clip_source_seconds(vp)   # #119 - show the voiceover length on the timeline
            break
    # fall back to the last caption timestamp if the audio can't be probed
    if voice_duration <= 0.01 and caption_track:
        try:
            voice_duration = max(float(r.get("end") or 0.0) for r in caption_track if isinstance(r, dict))
        except Exception:
            voice_duration = 0.0
    music_url = ""
    music_choice = str(config.get("background_music_choice") or config.get("background_music_file") or "").strip()
    if music_choice and music_choice.lower() != "none":
        mp = ROOT / "background music" / music_choice
        if mp.exists():
            music_url = link_for(mp)
    return {
        "slug": slug,
        "title": config.get("title", slug),
        "duration": round(float(config.get("duration", 0) or 0), 2),
        "scenes": scenes,
        "transitions": transitions,
        "sfx": content,
        "has_sfx": bool(transitions or content),
        # #108/#109 gating: captions are editable/re-renderable only when there is a real caption
        # track (an uploaded MP4 with baked-in captions has none). SFX are removable/redoable when
        # the project has editable markers or can plan SFX (baked-in upload audio is neither).
        "captions_editable": bool([r for r in caption_track if isinstance(r, dict) and str(r.get("text") or "").strip()]),
        "sfx_removable": bool(content) or bool(transitions) or bool(config.get("sfx_enabled", True)),
        # SFX/Captions-Master uploads split ONE finished video into seg_NN.mp4 pieces with the
        # captions BAKED into the pixels - replacing such a clip drops its caption and the render
        # can't re-add it to just that segment. The editor warns before a replace in this case.
        "captions_baked": bool(config.get("sfx_master_source") or config.get("captions_master_source")),
        "captions": captions_on,
        "sfx_on": bool(config.get("render_sfx_enabled", True)),
        "caption_track": caption_track,
        "caption_max_words": int(config.get("caption_max_words", 3) or 3),
        "caption_uppercase": bool(config.get("caption_uppercase", True)),
        "caption_center_y": float(config.get("caption_center_y", 0.72) or 0.72),
        "default_source_trim": float(config.get("seedance_clip_start_trim", 0.5) or 0.5),
        "volumes": volumes,
        "render_url": render_url,
        "voice_url": voice_url,
        "voice_duration": round(voice_duration, 3),
        "music_url": music_url,
        "clip_source": str(config.get("clip_source") or "generate"),
        "reasoning_model": str((config.get("wavespeed") or {}).get("reasoning_model")
                               or "openai/gpt-5.5"),
        "reasoning_mode": reasoning_modes.validate_reasoning_mode(
            str((config.get("wavespeed") or {}).get("reasoning_model") or "openai/gpt-5.5"),
            (config.get("wavespeed") or {}).get("reasoning_mode")),
        "scrape_sort": str(config.get("scrape_sort") or "MOST_LIKED"),
        "script_text": _project_script_text(project_dir, config),
        "hook_text": str(_project_run_form(project_dir).get("hook_text") or "").strip(),
        "speaker_name": str(_project_run_form(project_dir).get("speaker_name") or "").strip()
                        or pipeline.DEFAULT_TTS_SPEAKER,
        "tts_voice": str(_project_run_form(project_dir).get("tts_voice") or "").strip()
                     or pipeline.DEFAULT_TTS_VOICE,
        "tts_model": str(_project_run_form(project_dir).get("tts_model") or "").strip()
                     or pipeline.DEFAULT_TTS_MODEL,
        "tts_voices": list(pipeline.GEMINI_TTS_VOICES),
        "clip_density": str(config.get("clip_density")
                            or _project_run_form(project_dir).get("clip_density")
                            or "medium").strip().lower(),
    }


def _project_run_form(project_dir):
    """The saved run settings (speaker/voice/hook/...) of the project's last run."""
    try:
        run_form = json.loads((project_dir / "input" / "run_form.json")
                              .read_text(encoding="utf-8"))
        return run_form if isinstance(run_form, dict) else {}
    except Exception:
        return {}


def _project_script_text(project_dir, config):
    """Current spoken script for the timeline Script editor: the saved script.txt when
    present, else the scenes' voice lines joined one per line."""
    try:
        path = project_dir / "input" / "script.txt"
        if path.exists():
            text = path.read_text(encoding="utf-8", errors="replace").strip()
            if text:
                return text
    except Exception:
        pass
    return "\n".join(str(s.get("exact_voice_text") or s.get("script") or "").strip()
                     for s in (config.get("scenes") or [])
                     if str(s.get("exact_voice_text") or s.get("script") or "").strip())


def _regenerate_project_captions(slug, status_cb=None):
    """Redo captions: re-align the CURRENT voice to a fresh transcript and refresh the project's
    caption track so the next render burns updated captions. Used by the rework "redo captions"
    control (only offered when the project actually has an editable caption track)."""
    import voice_align
    project_dir = agent_core.PROJECTS_DIR / slug
    config = agent_core.load_project_config(slug)
    voice = None
    for cand in (config.get("audio_path"),
                 project_dir / "input" / "voiceover_dehiss.wav",
                 project_dir / "input" / "voiceover.wav"):
        try:
            if cand and Path(cand).exists():
                voice = Path(cand); break
        except Exception:
            pass
    if not voice:
        if status_cb: status_cb("Redo captions: no voice audio found - keeping existing captions.")
        return
    if status_cb: status_cb("Redo captions: re-aligning captions to the voice (fresh transcript)...")
    script_text = _project_script_text(project_dir, config)
    try:
        analysis, _tl = voice_align.analysis_from_audio(voice, script_text=script_text or None, status_cb=status_cb)
    except Exception as exc:
        if status_cb: status_cb(f"Redo captions failed ({exc}); keeping existing captions.")
        return
    if not (analysis and analysis.get("sentence_timestamps")):
        if status_cb: status_cb("Redo captions: alignment produced no lines; keeping existing captions.")
        return
    try:
        (project_dir / "input" / "audio_analysis.json").write_text(
            json.dumps(analysis, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
    config["timeline_caption_track"] = analysis["sentence_timestamps"]
    config["render_captions"] = True
    try:
        cfg_path = Path(config.get("_config_path", project_dir / "config" / "project.json"))
        cfg_path.write_text(json.dumps(agent_core.config_for_json(config), indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass
    if status_cb: status_cb(f"Redo captions: {len(analysis['sentence_timestamps'])} caption lines re-aligned.")


def start_timeline_job(slug, edits, regen_captions=False):
    job_id = str(int(time.time() * 1000))
    cancel_event = threading.Event()
    with JOB_LOCK:
        JOBS[job_id] = {
            "status": "running",
            "logs": ["Queued timeline render."],
            "log_times": [time.time()],
            "result": None,
            "error": None,
            "cancel_event": cancel_event,
            "project_dir": str(agent_core.PROJECTS_DIR / slug),
            "created_at": time.time(),
            "job_kind": "timeline",
        }

    def status_cb(message):
        with JOB_LOCK:
            job = JOBS.get(job_id)
            if not job or cancel_event.is_set():
                raise RunCancelled("Run cancelled by user.")
            job["logs"].append(message)
            job.setdefault("log_times", []).append(time.time())

    def worker():
        try:
            if regen_captions:
                _regenerate_project_captions(slug, status_cb=status_cb)
            result = agent_core.render_project_timeline(slug, edits, status_cb=status_cb, cancel_event=cancel_event)
            with JOB_LOCK:
                JOBS[job_id]["status"] = "done"
                JOBS[job_id]["result"] = result
        except (RunCancelled, pipeline.PipelineCancelled):
            with JOB_LOCK:
                JOBS[job_id]["status"] = "cancelled"
                JOBS[job_id]["logs"].append("Cancelled.")
        except Exception as exc:
            with JOB_LOCK:
                JOBS[job_id]["status"] = "error"
                JOBS[job_id]["error"] = f"{exc}\n\n{traceback.format_exc()}"
                JOBS[job_id]["logs"].append(f"Error: {exc}")

    threading.Thread(target=worker, daemon=True).start()
    return job_id


def start_timeline_social_replace_job(slug, scene_ids, reasoning_model=None, reasoning_mode=None):
    """Background job for targeted TikTok/X replacement of marked timeline scenes."""
    job_id = str(int(time.time() * 1000))
    cancel_event = threading.Event()
    with JOB_LOCK:
        JOBS[job_id] = {
            "status": "running",
            "logs": [f"Queued targeted TikTok/X replacement for {len(scene_ids or [])} scene(s)."],
            "log_times": [time.time()], "result": None, "error": None,
            "cancel_event": cancel_event,
            "project_dir": str(agent_core.PROJECTS_DIR / slug),
            "created_at": time.time(), "job_kind": "timeline",
        }

    def status_cb(message):
        with JOB_LOCK:
            job = JOBS.get(job_id)
            if not job or cancel_event.is_set():
                raise RunCancelled("Run cancelled by user.")
            job["logs"].append(message)
            job.setdefault("log_times", []).append(time.time())

    def worker():
        try:
            reasoning_modes.set_current_reasoning_mode(reasoning_model, reasoning_mode)
            result = agent_core.replace_timeline_scrape_scenes(
                slug, scene_ids, status_cb=status_cb, cancel_event=cancel_event,
                reasoning_model_override=reasoning_model)
            with JOB_LOCK:
                JOBS[job_id]["status"] = "done"
                JOBS[job_id]["result"] = result
        except (RunCancelled, pipeline.PipelineCancelled):
            with JOB_LOCK:
                JOBS[job_id]["status"] = "cancelled"
                JOBS[job_id]["logs"].append("Cancelled.")
        except Exception as exc:
            with JOB_LOCK:
                JOBS[job_id]["status"] = "error"
                JOBS[job_id]["error"] = f"{exc}\n\n{traceback.format_exc()}"
                JOBS[job_id]["logs"].append(f"Error: {exc}")

    threading.Thread(target=worker, daemon=True).start()
    return job_id


def start_timeline_rescript_job(slug, new_script, hook_text=None, voice_settings=None,
                                clip_density="medium", media_source="scrape"):
    """Timeline editor 'Change script': new voiceover + recut, reusing existing media for
    unchanged lines and finding media (project pool first, then TikTok/X) for the rest."""
    job_id = str(int(time.time() * 1000))
    cancel_event = threading.Event()
    with JOB_LOCK:
        JOBS[job_id] = {
            "status": "running",
            "logs": ["Queued script change: fresh voiceover + recut."],
            "log_times": [time.time()], "result": None, "error": None,
            "cancel_event": cancel_event,
            "project_dir": str(agent_core.PROJECTS_DIR / slug),
            "created_at": time.time(), "job_kind": "timeline",
        }

    def status_cb(message):
        with JOB_LOCK:
            job = JOBS.get(job_id)
            if not job or cancel_event.is_set():
                raise RunCancelled("Run cancelled by user.")
            job["logs"].append(message)
            job.setdefault("log_times", []).append(time.time())

    def worker():
        try:
            result = agent_core.rescript_and_recut(
                slug, new_script, hook_text=hook_text, voice_settings=voice_settings,
                clip_density=clip_density, media_source=media_source,
                status_cb=status_cb, cancel_event=cancel_event)
            with JOB_LOCK:
                JOBS[job_id]["status"] = "done"
                JOBS[job_id]["result"] = result
        except (RunCancelled, pipeline.PipelineCancelled):
            with JOB_LOCK:
                JOBS[job_id]["status"] = "cancelled"
                JOBS[job_id]["logs"].append("Cancelled.")
        except Exception as exc:
            tb = traceback.format_exc()
            _persist_job_error(agent_core.PROJECTS_DIR / slug, "timeline-rescript", exc, tb)
            with JOB_LOCK:
                JOBS[job_id]["status"] = "error"
                JOBS[job_id]["error"] = f"{exc}\n\n{tb}"
                JOBS[job_id]["logs"].append(f"Error: {exc}")

    threading.Thread(target=worker, daemon=True).start()
    return job_id


def _persist_job_error(project_dir, kind, exc, tb):
    """Write a failed job's traceback next to the project AND into logs/crash.log, so the cause
    survives even if the app dies before the browser can show it (the launcher discards stderr)."""
    try:
        stamp = datetime.datetime.now().isoformat(timespec="seconds")
        blob = f"===== {kind} FAILED @ {stamp} =====\n{exc}\n\n{tb}\n"
        try:
            Path(project_dir).mkdir(parents=True, exist_ok=True)
            (Path(project_dir) / "last_job_error.txt").write_text(blob, encoding="utf-8")
        except Exception:
            pass
        if _CRASH_FH:
            _CRASH_FH.write("\n" + blob)
            _CRASH_FH.flush()
    except Exception:
        pass


def start_timeline_revoice_job(slug, replace_scene_ids=None, reasoning_model=None, reasoning_mode=None):
    """Regenerate narration, retime the saved timeline, and optionally replace marked social clips."""
    replace_scene_ids = [str(value) for value in (replace_scene_ids or []) if str(value)]
    job_id = str(int(time.time() * 1000))
    cancel_event = threading.Event()
    with JOB_LOCK:
        JOBS[job_id] = {
            "status": "running",
            "logs": ["Queued fresh speech generation and timeline retime."],
            "log_times": [time.time()], "result": None, "error": None,
            "cancel_event": cancel_event,
            "project_dir": str(agent_core.PROJECTS_DIR / slug),
            "created_at": time.time(), "job_kind": "timeline",
        }

    def status_cb(message):
        with JOB_LOCK:
            job = JOBS.get(job_id)
            if not job or cancel_event.is_set():
                raise RunCancelled("Run cancelled by user.")
            job["logs"].append(message)
            job.setdefault("log_times", []).append(time.time())

    def worker():
        try:
            reasoning_modes.set_current_reasoning_mode(reasoning_model, reasoning_mode)
            result = agent_core.regenerate_timeline_speech(
                slug, status_cb=status_cb, cancel_event=cancel_event,
                render=not bool(replace_scene_ids))
            if replace_scene_ids:
                status_cb("Speech timing saved. Searching replacement media against the retimed lines...")
                result = agent_core.replace_timeline_scrape_scenes(
                    slug, replace_scene_ids, status_cb=status_cb, cancel_event=cancel_event,
                    reasoning_model_override=reasoning_model)
            with JOB_LOCK:
                JOBS[job_id]["status"] = "done"
                JOBS[job_id]["result"] = result
        except (RunCancelled, pipeline.PipelineCancelled):
            with JOB_LOCK:
                JOBS[job_id]["status"] = "cancelled"
                JOBS[job_id]["logs"].append("Cancelled.")
        except Exception as exc:
            with JOB_LOCK:
                JOBS[job_id]["status"] = "error"
                JOBS[job_id]["error"] = f"{exc}\n\n{traceback.format_exc()}"
                JOBS[job_id]["logs"].append(f"Error: {exc}")

    threading.Thread(target=worker, daemon=True).start()
    return job_id


# ============================ Reddit Story Mode ============================
REDDIT_STORY_DIR = ROOT / "outputs" / "reddit_story"
REDDIT_SESSION_FILE = REDDIT_STORY_DIR / "session_stories.json"

REDDIT_PAGE_HTML = """
<style>
  .rs-wrap { max-width: 900px; margin: 0 auto; }
  .rs-hero { text-align:center; padding: 10px 6px 4px; }
  .rs-hero h1 { margin: 0 0 6px; }
  .rs-hero p { color: var(--muted); font-weight:600; margin: 0 0 16px; }
  .rs-find { width:auto; min-width:0; padding:12px 26px; font-size:15px; }
  .rs-status { text-align:center; color:var(--muted); font-weight:600; margin:12px 0; min-height:20px; }
  .rs-cards { display:grid; grid-template-columns:repeat(auto-fill,minmax(260px,1fr)); gap:16px; margin-top:14px; }
  .rs-card { display:flex; flex-direction:column; gap:8px; padding:16px; background:var(--bg-raised); border:1px solid var(--line-strong); border-radius:var(--r-md); box-shadow:var(--sh-1); }
  .rs-card h3 { margin:0; font-size:16px; color:var(--text); }
  .rs-hook { font-weight:700; color:var(--accent); font-size:13px; }
  .rs-summary { color:var(--muted); font-size:12.5px; flex:1; }
  .rs-meta { display:flex; align-items:center; justify-content:space-between; font-size:11.5px; color:var(--faint); font-weight:700; }
  .rs-flag { color:var(--accent-2); }
  .rs-use { width:100%; margin-top:6px; }
</style>
<div class="rs-wrap">
  <div class="rs-hero">
    <h1>&#128172; Reddit Story Mode</h1>
    <p>Original Reddit-style story narrated over Minecraft parkour. No captions, no overlays &mdash; just voice + gameplay.</p>
    <button type="button" class="button primary rs-find" id="rs-find">&#128269; Find 5 Stories</button>
  </div>
  <div class="rs-status" id="rs-status"></div>
  <div class="rs-cards" id="rs-cards"></div>
</div>
<script>
(function(){
  var findBtn=document.getElementById('rs-find');
  var statusEl=document.getElementById('rs-status');
  var cardsEl=document.getElementById('rs-cards');
  function esc(t){ return String(t==null?'':t).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }
  function fmtDur(s){ s=parseInt(s||0,10); var m=Math.floor(s/60), r=s%60; return m+':'+(r<10?'0':'')+r; }
  function setStatus(t){ statusEl.textContent=t||''; }
  function renderCards(stories){
    cardsEl.innerHTML='';
    (stories||[]).forEach(function(s){
      var c=document.createElement('div'); c.className='rs-card';
      var flags=(s.risk_flags&&s.risk_flags.length)?('<span class="rs-flag">&#9888; '+esc(s.risk_flags.join(', '))+'</span>'):'';
      c.innerHTML='<h3>'+esc(s.title)+'</h3>'
        +'<div class="rs-hook">'+esc(s.hook)+'</div>'
        +'<div class="rs-summary">'+esc(s.summary)+'</div>'
        +'<div class="rs-meta"><span>&#9201; ~'+fmtDur(s.estimated_duration_sec)+'</span>'+flags+'</div>'
        +'<button type="button" class="button rs-use">Use This Story</button>';
      c.querySelector('.rs-use').addEventListener('click', function(){ useStory(s.id, this); });
      cardsEl.appendChild(c);
    });
  }
  function discover(){
    findBtn.disabled=true; cardsEl.innerHTML=''; setStatus('Writing 5 original stories\\u2026 (this can take ~20s)');
    fetch('/reddit-discover',{method:'POST'}).then(function(r){return r.json();}).then(function(d){
      findBtn.disabled=false;
      if(d&&d.stories&&d.stories.length){ setStatus('Pick a story to generate.'); renderCards(d.stories); }
      else { setStatus((d&&d.error)||'Could not generate stories.'); }
    }).catch(function(){ findBtn.disabled=false; setStatus('Could not generate stories (network error).'); });
  }
  function useStory(id, btn){
    if(btn){ btn.disabled=true; btn.textContent='Starting\\u2026'; }
    setStatus('Starting generation\\u2026');
    fetch('/reddit-generate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({story_id:id})})
      .then(function(r){return r.json();}).then(function(d){
        if(d&&d.job){ window.location.href=d.job; }
        else { if(btn){ btn.disabled=false; btn.textContent='Use This Story'; } setStatus((d&&d.error)||'Could not start generation.'); }
      }).catch(function(){ if(btn){ btn.disabled=false; btn.textContent='Use This Story'; } setStatus('Could not start generation.'); });
  }
  findBtn.addEventListener('click', discover);
})();
</script>
"""


def reddit_discover_stories():
    """Generate 5 safe story candidates, persist them for this session, return the list."""
    if reddit_stories is None:
        raise RuntimeError("Reddit Story Mode is unavailable (module failed to import).")
    stories = reddit_stories.generate_stories(status_cb=lambda m: print("[reddit]", m))
    REDDIT_STORY_DIR.mkdir(parents=True, exist_ok=True)
    REDDIT_SESSION_FILE.write_text(
        json.dumps({"stories": stories, "at": time.time()}, indent=2, ensure_ascii=False),
        encoding="utf-8")
    return stories


def reddit_story_by_id(story_id):
    try:
        data = json.loads(REDDIT_SESSION_FILE.read_text("utf-8"))
        for s in data.get("stories", []):
            if str(s.get("id")) == str(story_id):
                return s
    except Exception:
        pass
    return None


def start_reddit_job(story):
    job_id = str(int(time.time() * 1000))
    cancel_event = threading.Event()
    with JOB_LOCK:
        JOBS[job_id] = {
            "status": "running",
            "logs": [f"Queued Reddit Story: {story.get('title', 'story')}."],
            "log_times": [time.time()],
            "result": None,
            "error": None,
            "cancel_event": cancel_event,
            "project_dir": None,
            "created_at": time.time(),
            "job_kind": "reddit",
        }

    def status_cb(message):
        with JOB_LOCK:
            job = JOBS.get(job_id)
            if not job or cancel_event.is_set():
                raise RunCancelled("Run cancelled by user.")
            job["logs"].append(message)
            job.setdefault("log_times", []).append(time.time())

    def worker():
        try:
            result = reddit_orchestrator.run_story_job(
                story, job_id, status_cb=status_cb, cancel_event=cancel_event)
            with JOB_LOCK:
                JOBS[job_id]["status"] = "done"
                JOBS[job_id]["result"] = result
        except (RunCancelled, pipeline.PipelineCancelled):
            with JOB_LOCK:
                JOBS[job_id]["status"] = "cancelled"
                JOBS[job_id]["logs"].append("Cancelled.")
        except Exception as exc:  # noqa: BLE001
            with JOB_LOCK:
                JOBS[job_id]["status"] = "error"
                JOBS[job_id]["error"] = f"{exc}"
                JOBS[job_id]["logs"].append(f"Error: {exc}")

    threading.Thread(target=worker, daemon=True).start()
    return job_id


def reddit_page():
    header = brand_header(back=True)
    avail = reddit_stories is not None
    warn = "" if avail else ('<section class="panel"><div class="conn-warn">&#9888; Reddit Story Mode '
                             'module failed to import; check the server console.</div></section>')
    body = header + warn + REDDIT_PAGE_HTML
    return page("Reddit Story Mode", body)


def timeline_page(slug):
    project_dir = safe_project_dir(slug)
    if not project_dir:
        return page("Shortslab", brand_header(back=True) + '<section class="panel"><div class="hint">Unknown project.</div></section>')
    # The timeline editor is only available once the agent has produced a first render.
    if not project_has_render(slug):
        msg = ('<section class="panel"><h2>Timeline not ready yet</h2>'
               '<div class="hint">The timeline editor opens once this project has its first complete render. '
               'Run the agent (Create Short) first &mdash; then come back here to fine-tune and re-render.</div></section>')
        return page("Shortslab", brand_header(back=True) + msg)
    try:
        model = timeline_model(slug)
    except Exception as exc:
        return page("Shortslab", brand_header(back=True) + f'<section class="panel"><div class="hint">Could not load timeline: {esc(str(exc))}</div></section>')
    header = brand_header(back=True)
    model_tag = '<script id="timeline-model" type="application/json">' + json.dumps(model) + '</script>'
    # Chat-shell theme for the editor's VISUAL SHELL only (colors/radii/fonts via CSS variable
    # remap; zero behavior changes). Follows the chat theme preference and rewires the header
    # toggle to flip the same "sl-chat-theme" key.
    theme_tag = (
        '<link rel="stylesheet" href="/static/timeline-theme.css">'
        '<script>(function(){try{'
        'var t=localStorage.getItem("sl-chat-theme")||"dark";'
        'document.documentElement.classList.toggle("chat-light",t==="light");'
        'var mine=function(){'
        'var light=!document.documentElement.classList.contains("chat-light");'
        'document.documentElement.classList.toggle("chat-light",light);'
        'try{localStorage.setItem("sl-chat-theme",light?"light":"dark");}catch(e){}};'
        'window.toggleTheme=mine;'
        'window.addEventListener("load",function(){window.toggleTheme=mine;});'
        '}catch(e){}})();</script>'
    )
    body = theme_tag + header + model_tag + TIMELINE_SKELETON.replace("__SLUG__", esc(slug)) + TIMELINE_ASSETS
    return page("Timeline Editor", body, body_class="page-timeline")


def render_done_view(job, job_id):
    """The finished-render screen: only the final video + an Open-timeline-editor button.
    Returns None when there is no final video to show (so the caller keeps the detail view)."""
    result = job.get("result") or {}
    video = result.get("video") or result.get("video_no_sfx")
    if not (video and Path(video).exists()):
        return None
    vurl = link_for(Path(video))
    proj = job.get("project_dir")
    tslug = Path(proj).name if proj and Path(proj).exists() else None
    tl_btn = ""
    if tslug:
        # sfx jobs included: the SFX Master now writes a real project, so every added
        # sound can be moved/replaced/deleted in the timeline editor
        tl_btn = (f'<a class="button" href="/timeline?slug={urllib.parse.quote(tslug)}">'
                  f'&#127902; Open timeline editor</a>')
    return f"""
    <div id="job-root" data-status="done" data-job-id="{esc(job_id)}">
      <div class="done-wrap">
        <h1 class="done-title">&#10003; Your Short is ready</h1>
        <div class="done-stage" id="done-stage">
          <video class="done-video" src="{vurl}" controls autoplay muted playsinline preload="metadata"></video>
        </div>
        <div class="done-actions">
          {tl_btn}
          <button type="button" class="button secondary" onclick="saveRender('{urllib.parse.quote(str(Path(video).resolve()))}', this)">&#11015; Download</button>
          <a class="button secondary" href="/">New project</a>
          <a class="button secondary" href="/assets">Assets</a>
        </div>
      </div>
    </div>
    """


def job_page(job_id):
    with JOB_LOCK:
        job = dict(JOBS.get(job_id, {"status": "missing", "logs": [], "result": None, "error": "Unknown job"}))
    status = job["status"]
    # Finished render -> show ONLY the final video + Open timeline editor (no intermediate
    # media dump, progress bars, or console). Detail view stays for running/error states.
    if status == "done":
        done_view = render_done_view(job, job_id)
        if done_view:
            return page("Job", done_view)
    klass = "done" if status == "done" else "error" if status == "error" else "cancelled" if status == "cancelled" else "cancelling" if status == "cancelling" else ""
    logs = job.get("logs", [])
    progress_html = render_progress(status, logs, job.get("created_at"), log_times=job.get("log_times"), job_kind=job.get("job_kind"))
    result_html = render_outputs(job.get("result"), job_id)
    error_html = f'<section class="panel"><h2>Error</h2><pre>{esc(job.get("error"))}</pre></section>' if job.get("error") else ""
    media_html = render_media_replacer(job_id, job)
    cancel_html = ""
    if status in {"running", "cancelling", "awaiting_approval"}:
        cancel_html = (
            f'<form class="inline-form" method="post" action="/cancel?id={urllib.parse.quote(job_id)}">'
            f'<button class="danger" type="submit">Cancel run</button></form>'
        )
    timeline_html = ""
    proj = job.get("project_dir")
    if proj and Path(proj).exists():
        tslug = Path(proj).name
        timeline_html = f'<a class="button" href="/timeline?slug={urllib.parse.quote(tslug)}">&#127902; Timeline editor</a>'
    speech_html = ""
    if status == "awaiting_approval" and job.get("speech_audio"):
        qid = urllib.parse.quote(job_id)
        audio_url = link_for(Path(job["speech_audio"]))
        voice_opts = "".join(f'<option value="{esc(v)}">{esc(v)}</option>' for v in pipeline.GEMINI_TTS_VOICES)
        speech_html = f"""
        <section class="panel accent" id="speech-approval">
          <h2>&#127908; Approve the voiceover</h2>
          <div class="hint">Listen to the generated narration, then continue &mdash; or pick a different voice and regenerate.</div>
          <audio controls preload="auto" style="width:100%; margin:14px 0;" src="{audio_url}"></audio>
          <form class="inline-form" method="post" action="/approve-speech?id={qid}">
            <button type="submit">&#10003; Approve &amp; continue</button>
          </form>
          <details style="margin-top:16px;">
            <summary style="cursor:pointer; font-family:var(--pixel); font-size:11px; text-transform:uppercase; color:var(--accent-2);">Replace the voice (re-pick speaker)</summary>
            <form method="post" action="/replace-speech?id={qid}" style="margin-top:14px;">
              <div style="display:flex; gap:10px; flex-wrap:wrap;">
                <input type="text" name="speaker_name" value="Narrator" placeholder="Speaker name" style="flex:1; min-width:150px;">
                <select name="tts_voice" style="flex:1; min-width:180px;">{voice_opts}</select>
                <select name="tts_model" style="flex:1; min-width:160px;"><option value="flash">Flash TTS (cheaper)</option><option value="pro">Pro TTS (higher quality)</option></select>
              </div>
              <button class="danger" type="submit" style="margin-top:14px;">&#8635; Replace voice &amp; regenerate</button>
            </form>
          </details>
        </section>"""
    # Timeline-editor render: a clean animated progress bar only - no console log, no media sidebar,
    # and NO redundant "Rendering" heading / "running" status pill (the animated bar already says it).
    # Keep the Shortslab identity so the app doesn't look like it got replaced by a bare status page.
    if job.get("job_kind") == "timeline" and status not in {"done", "error", "cancelled"}:
        minimal = f"""
    <div id="job-root" data-status="{esc(status)}" data-job-id="{esc(job_id)}" data-minimal="1">
      <div class="tl-render-top">
        <span class="tl-render-brand"><img src="/static/app_icon.png" alt="" width="30" height="30">Shortslab</span>
        <div class="job-actions">{cancel_html}</div>
      </div>
      <div class="tl-render-center">
        {progress_html}
        <div id="job-outputs">{result_html}</div>
        <div id="job-error">{error_html}</div>
      </div>
    </div>
    """
        return page("Shortslab", minimal)
    body = f"""
    <div id="job-root" data-status="{esc(status)}" data-job-id="{esc(job_id)}">
      <div class="job-head">
        <div>
          <h1>Job</h1>
          <p>Status: <span id="job-status-label" class="status {klass}">{esc(status)}</span></p>
        </div>
        <div class="job-actions">
          {timeline_html}
          <a class="button secondary" href="/">New project</a>
          <a class="button secondary" href="/assets">Assets</a>
          {cancel_html}
        </div>
      </div>
      {speech_html}
      {progress_html}
      <div id="job-outputs">{result_html}</div>
      <div id="job-error">{error_html}</div>
      <div class="job-layout">
        <section class="panel">
          <div class="panel-head">
            <h2>Console</h2>
            <button class="button secondary" type="button" data-toggle-log="job-log">Hide logs</button>
          </div>
          <pre id="job-log" class="log-box">{esc(visible_log_text(logs))}</pre>
        </section>
        <aside id="media-sidebar-container">
          {media_html}
        </aside>
      </div>
    </div>
    """
    return page("Job", body)


def progress_page(job_id):
    """Standalone FOCUSED progress view (timeline render / SFX Master / VFX Master): no chat
    shell, no media previews - one big animated bar with real %, elapsed + ETA, and a small
    box showing the current action. Chimes + shows the result buttons when done."""
    jid = esc(str(job_id or ""))
    body = f"""
<style>
  html, body {{ margin:0; padding:0; }}
  body.page-progress {{ min-height:100vh; display:flex; align-items:center; justify-content:center;
    font-family:Inter, ui-sans-serif, system-ui, "Segoe UI", sans-serif;
    background: radial-gradient(circle at 65% 25%, rgba(57,255,20,.07), transparent 45%), #090b0a;
    color:#f1f3f1; }}
  body.page-progress main {{ max-width:none; width:100%; display:flex; align-items:center; justify-content:center; padding:0; }}
  .pg-wrap {{ width:min(640px, 92vw); display:flex; flex-direction:column; gap:18px; }}
  .pg-title {{ font-size:20px; font-weight:800; letter-spacing:-.01em; display:flex; align-items:center; gap:10px; }}
  .pg-title .pg-ico {{ font-size:24px; }}
  .pg-sub {{ color:#a0a6a0; font-size:13px; margin-top:-10px; }}
  .pg-bar {{ position:relative; height:26px; border-radius:99px; background:#151815;
    border:1px solid rgba(255,255,255,.10); overflow:hidden; }}
  .pg-fill {{ position:absolute; inset:0; width:0%; border-radius:99px;
    background:linear-gradient(90deg,#1fb212,#39ff14 60%,#7dff5c);
    box-shadow:0 0 18px rgba(57,255,20,.45); transition:width .6s ease; }}
  .pg-fill::before {{ content:""; position:absolute; inset:0;
    background:repeating-linear-gradient(45deg, rgba(255,255,255,.16) 0 12px, transparent 12px 24px);
    animation:pgstripes 1s linear infinite; }}
  .pg-fill::after {{ content:""; position:absolute; top:0; bottom:0; width:60px;
    background:linear-gradient(90deg, transparent, rgba(255,255,255,.5), transparent);
    animation:pgsheen 1.8s ease-in-out infinite; }}
  .pg-bar.indet .pg-fill {{ width:38% !important; animation:pgslide 1.6s ease-in-out infinite; }}
  @keyframes pgstripes {{ to {{ background-position:34px 0; }} }}
  @keyframes pgsheen {{ 0% {{ left:-70px; }} 100% {{ left:110%; }} }}
  @keyframes pgslide {{ 0% {{ transform:translateX(-40%); }} 50% {{ transform:translateX(220%); }} 100% {{ transform:translateX(-40%); }} }}
  .pg-stats {{ display:flex; justify-content:space-between; font-variant-numeric:tabular-nums;
    color:#a0a6a0; font-size:13px; }}
  .pg-stats b {{ color:#f1f3f1; font-weight:700; }}
  .pg-now {{ border:1px solid rgba(255,255,255,.09); background:rgba(20,23,20,.9); border-radius:12px;
    padding:11px 14px; font-family:ui-monospace, Consolas, monospace; font-size:12px; color:#9fdd9a;
    min-height:20px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }}
  .pg-now::before {{ content:"> "; color:#39ff14; }}
  .pg-done {{ display:none; flex-direction:column; gap:12px; }}
  .pg-actions {{ display:flex; gap:10px; flex-wrap:wrap; }}
  .pg-btn {{ display:inline-flex; align-items:center; gap:8px; padding:11px 18px; border-radius:11px;
    border:1px solid rgba(255,255,255,.14); background:#181a18; color:#f1f3f1; font-weight:700;
    font-size:13.5px; text-decoration:none; cursor:pointer; }}
  .pg-btn:hover {{ border-color:#39ff14; }}
  .pg-btn.primary {{ background:#39ff14; border-color:#39ff14; color:#062b00; }}
  .pg-err {{ display:none; border:1px solid rgba(239,102,92,.5); background:rgba(239,102,92,.08);
    color:#ffb4ad; border-radius:12px; padding:12px 14px; font-size:13px; white-space:pre-wrap; }}
  .pg-video {{ display:none; width:100%; max-height:46vh; border-radius:14px; background:#000;
    border:1px solid rgba(255,255,255,.1); }}
</style>
<div class="pg-wrap">
  <div class="pg-title"><span class="pg-ico" id="pg-ico">&#9881;&#65039;</span><span id="pg-title">Working...</span></div>
  <div class="pg-sub" id="pg-sub"></div>
  <div class="pg-bar indet" id="pg-bar"><div class="pg-fill" id="pg-fill"></div></div>
  <div class="pg-stats"><span>Elapsed <b id="pg-elapsed">0:00</b></span><span id="pg-pct"></span><span>Remaining <b id="pg-eta">&mdash;</b></span></div>
  <div class="pg-now" id="pg-now">Starting...</div>
  <video class="pg-video" id="pg-video" controls playsinline></video>
  <div class="pg-done" id="pg-done">
    <div class="pg-actions">
      <a class="pg-btn primary" id="pg-download" href="#" download>&#11015; Download video</a>
      <a class="pg-btn" id="pg-timeline" href="#">&#127902; Open timeline editor</a>
      <a class="pg-btn" href="/">&#8592; Back to Shortslab</a>
    </div>
  </div>
  <div class="pg-err" id="pg-err"></div>
</div>
<script>
(function() {{
  var JID = {json.dumps(str(job_id or ""))};
  var TITLES = {{ timeline: ["\\uD83C\\uDFAC", "Rendering your Short"],
                 sfx: ["\\uD83D\\uDD0A", "SFX Master"],
                 visual: ["\\uD83C\\uDFAF", "VFX Master"],
                 longform: ["\\uD83C\\uDFA8", "Longform video"],
                 run: ["\\u2699\\uFE0F", "Working"] }};
  var start = Date.now() / 1000, createdAt = 0, finished = false;
  function fmt(s) {{ s = Math.max(0, Math.round(s)); var m = Math.floor(s / 60); return m + ":" + String(s % 60).padStart(2, "0"); }}
  function pctFrom(text) {{
    var m = null, re = /Rendering frames:\\s*(\\d+(?:\\.\\d+)?)%/g, x;
    while ((x = re.exec(text))) m = parseFloat(x[1]);
    if (m == null) {{ var re2 = /image\\s+(\\d+)\\s*\\/\\s*(\\d+)/g; var y, last = null;
      while ((y = re2.exec(text))) last = y;
      if (last && +last[2] > 0) m = 100 * (+last[1]) / (+last[2]); }}
    return m;
  }}
  function chime() {{
    try {{ var ctx = new (window.AudioContext || window.webkitAudioContext)();
      [523.25, 659.25, 783.99].forEach(function(f, i) {{
        var o = ctx.createOscillator(), g = ctx.createGain();
        o.frequency.value = f; o.type = "sine"; o.connect(g); g.connect(ctx.destination);
        var t = ctx.currentTime + i * 0.12;
        g.gain.setValueAtTime(0, t); g.gain.linearRampToValueAtTime(0.18, t + 0.02);
        g.gain.exponentialRampToValueAtTime(0.001, t + 0.5);
        o.start(t); o.stop(t + 0.55); }});
    }} catch (e) {{}}
  }}
  setInterval(function() {{
    if (finished) return;
    var base = createdAt || start;
    document.getElementById("pg-elapsed").textContent = fmt(Date.now() / 1000 - base);
  }}, 1000);
  function poll() {{
    fetch("/job-status?id=" + encodeURIComponent(JID)).then(function(r) {{ return r.json(); }}).then(function(j) {{
      if (!j || !j.exists) {{ document.getElementById("pg-now").textContent = "Job not found."; return; }}
      if (j.created_at) createdAt = j.created_at;
      var t = TITLES[j.job_kind] || TITLES.run;
      document.getElementById("pg-ico").textContent = t[0];
      document.getElementById("pg-title").textContent = t[1];
      var lines = (j.log_text || "").split("\\n").filter(function(l) {{ return l.trim(); }});
      if (lines.length) document.getElementById("pg-now").textContent = lines[lines.length - 1].slice(0, 160);
      var pct = pctFrom(j.log_text || "");
      var bar = document.getElementById("pg-bar"), fill = document.getElementById("pg-fill");
      if (pct != null && j.status === "running") {{
        bar.classList.remove("indet"); fill.style.width = Math.min(99.5, pct) + "%";
        document.getElementById("pg-pct").innerHTML = "<b>" + pct.toFixed(0) + "%</b>";
        var elapsed = Date.now() / 1000 - (createdAt || start);
        if (pct > 3) document.getElementById("pg-eta").textContent = fmt(elapsed / pct * (100 - pct));
      }}
      if (j.status === "done" || j.status === "error" || j.status === "cancelled") {{
        finished = true;
        bar.classList.remove("indet"); fill.style.width = "100%";
        document.getElementById("pg-eta").textContent = "0:00";
        if (j.status === "done") {{
          document.getElementById("pg-pct").innerHTML = "<b>100%</b>";
          document.getElementById("pg-now").textContent = "Done.";
          document.getElementById("pg-done").style.display = "flex";
          if (j.result_video_url) {{
            var v = document.getElementById("pg-video");
            v.src = j.result_video_url; v.style.display = "block";
            var dl = document.getElementById("pg-download");
            var dlName = j.result_video_name || "video.mp4";
            dl.href = j.result_video_url; dl.setAttribute("download", dlName);
            // #121 - let the user CHOOSE where to save (native "Save As"), not force Downloads.
            dl.onclick = function(ev){{
              if (window.showSaveFilePicker) {{
                ev.preventDefault();
                (async function(){{
                  try {{
                    var h = await window.showSaveFilePicker({{ suggestedName: dlName,
                      types: [{{ description: "MP4 video", accept: {{ "video/mp4": [".mp4"] }} }}] }});
                    var resp = await fetch(j.result_video_url);
                    var w = await h.createWritable(); await resp.body.pipeTo(w);
                    dl.textContent = "\\u2713 Saved";
                  }} catch(e) {{
                    if (e && e.name === "AbortError") return;      // user cancelled the dialog
                    var a=document.createElement("a"); a.href=j.result_video_url; a.download=dlName;
                    document.body.appendChild(a); a.click(); a.remove();   // fallback
                  }}
                }})();
              }}
            }};
          }} else {{ document.getElementById("pg-download").style.display = "none"; }}
          var tl = document.getElementById("pg-timeline");
          if (j.project_slug) tl.href = "/timeline?slug=" + encodeURIComponent(j.project_slug);
          else tl.style.display = "none";
          chime();
        }} else {{
          fill.style.background = "#ef665c"; fill.style.boxShadow = "none";
          var err = document.getElementById("pg-err");
          err.style.display = "block";
          err.textContent = (j.status === "cancelled") ? "Cancelled." :
            ((j.log_text || "").split("\\n").slice(-6).join("\\n") || "The run failed.");
          document.getElementById("pg-done").style.display = "flex";
          document.getElementById("pg-download").style.display = "none";
        }}
        return;
      }}
      setTimeout(poll, 1000);
    }}).catch(function() {{ setTimeout(poll, 2000); }});
  }}
  poll();
}})();
</script>"""
    return page("Working...", body, body_class="page-progress")


def job_status_payload(job_id):
    with JOB_LOCK:
        job = dict(JOBS.get(job_id, {"status": "missing", "logs": [], "result": None, "error": "Unknown job"}))
    status = job.get("status", "missing")
    klass = "done" if status == "done" else "error" if status == "error" else "cancelled" if status == "cancelled" else "cancelling" if status == "cancelling" else ""
    logs = job.get("logs", [])
    payload = {
        "exists": status != "missing",
        "status": status,
        "klass": klass,
        "progress_html": render_progress(status, logs, job.get("created_at"), log_times=job.get("log_times"), job_kind=job.get("job_kind")),
        "log_text": visible_log_text(logs),
        "outputs_html": render_outputs(job.get("result"), job_id),
        "error_html": f'<section class="panel"><h2>Error</h2><pre>{esc(job.get("error"))}</pre></section>' if job.get("error") else "",
        "media_html": render_media_replacer(job_id, job),
        # chat-shell extras (additive; the legacy job page ignores them)
        "job_kind": job.get("job_kind", "run"),
        "project_slug": (Path(job["project_dir"]).name
                         if job.get("project_dir") and Path(job["project_dir"]).exists() else ""),
        "speech_audio_url": (link_for(Path(job["speech_audio"]))
                             if status == "awaiting_approval" and job.get("speech_audio") else ""),
        "speech_speed": (_speech_current_speed(job) if status == "awaiting_approval" else 0),
        "assigned_media": _assigned_media_payload(job),
        "created_at": job.get("created_at") or 0,
    }
    # direct result-video link for the standalone /progress view (additive)
    _res = job.get("result") if isinstance(job.get("result"), dict) else {}
    _vid = str(_res.get("video") or "")
    if _vid and Path(_vid).exists():
        payload["result_video_url"] = link_for(Path(_vid))
        payload["result_video_name"] = Path(_vid).name
    else:
        payload["result_video_url"] = ""
        payload["result_video_name"] = ""
    return json.dumps(payload).encode("utf-8")


def _speech_current_speed(job):
    """The narration speed currently baked into the halted voiceover (so the approval UI can show
    it and preview other speeds relative to it). Falls back to the clip-source default."""
    try:
        pd = project_dir_for_job(job)
        if pd:
            cfg = agent_core.load_project_config(pd.name)
            v = float(cfg.get("voice_speed") or 0)
            if v > 0:
                return round(v, 2)
            src = str(cfg.get("clip_source") or "").lower()
            return 1.30 if src == "scrape" else 1.15
    except Exception:
        pass
    return 1.15


def _assigned_media_payload(job, cap=60):
    """ONLY the clips currently assigned to scenes (the run's actual choices) - the chat shell
    renders these in its own clean grid instead of the full grouped media wall.

    Shown ONLY while a live TikTok/X SCRAPE run is processing. Reworks (redo captions/SFX/visual,
    timeline render, longform, ...) and finished runs return nothing, so the preview never shows
    stale assigned media from an earlier scrape."""
    if str(job.get("job_kind") or "").lower() in ("sfx", "visual", "caption", "timeline",
                                                    "longform", "viraltrans"):
        return []
    if str(job.get("status") or "").lower() not in ("running", "cancelling"):
        return []
    project_dir = project_dir_for_job(job)
    if not project_dir:
        return []
    # scrape-only: the grid shows scraped/downloaded clips, which is meaningless for a generate run
    try:
        run_form = read_json_file(project_dir / "input" / "run_form.json") or {}
        if str(run_form.get("clip_source") or "").lower() != "scrape":
            return []
    except Exception:
        return []
    items = []
    try:
        for kind, path in project_media_files(project_dir):
            if kind != "assigned":
                continue
            items.append({
                "name": path.name,
                "url": link_for(path),
                "path": str(path.resolve()),
                "type": "video" if is_video_path(path) else "image",
            })
            if len(items) >= cap:
                break
    except Exception:
        return []
    return items


def music_list_payload():
    """List the background-music tracks (from the 'background music' folder) for the picker,
    each with a /file URL so the browser can preview it."""
    tracks = []
    try:
        for path in pipeline.background_music_files({}):
            tracks.append({"name": path.stem.replace("_", " "), "file": path.name,
                           "url": link_for(path)})
    except Exception:
        pass
    return json.dumps({"tracks": tracks}).encode("utf-8")


def tiktok_status_payload():
    """JSON status for the Connect-TikTok control: available / ready / busy."""
    try:
        import tiktok_login
        avail = tiktok_login.available()
        ready = tiktok_login.is_ready()
    except Exception:
        avail = ready = False
    with TIKTOK_LOCK:
        busy = bool(TIKTOK_LOGIN.get("busy"))
        err = TIKTOK_LOGIN.get("error") or ""
    return json.dumps({"available": avail, "ready": ready, "busy": busy, "error": err}).encode("utf-8")


def start_tiktok_login():
    """Open the headed TikTok login window in a background thread (one at a time)."""
    try:
        import tiktok_login
    except Exception:
        return
    if not tiktok_login.available():
        return
    with TIKTOK_LOCK:
        if TIKTOK_LOGIN.get("busy"):
            return
        TIKTOK_LOGIN["busy"] = True
        TIKTOK_LOGIN["error"] = ""

    def _run():
        err = ""
        try:
            ok = tiktok_login.login(status_cb=lambda m: print("[tiktok-login]", m), timeout_s=300)
            if not ok:
                err = "Login window closed or timed out before sign-in completed."
        except Exception as exc:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            err = f"{exc.__class__.__name__}: {exc}"
            print("[tiktok-login] error:", err)
        finally:
            with TIKTOK_LOCK:
                TIKTOK_LOGIN["busy"] = False
                TIKTOK_LOGIN["thread"] = None
                TIKTOK_LOGIN["error"] = err

    print("[tiktok-login] launching login browser window...")

    th = threading.Thread(target=_run, daemon=True)
    with TIKTOK_LOCK:
        TIKTOK_LOGIN["thread"] = th
    th.start()


def twitter_status_payload():
    """JSON status for the Connect-X control: available / ready / busy."""
    try:
        import twitter_login
        avail = twitter_login.available()
        ready = twitter_login.is_ready()
    except Exception:
        avail = ready = False
    with TIKTOK_LOCK:
        busy = bool(TWITTER_LOGIN.get("busy"))
        err = TWITTER_LOGIN.get("error") or ""
    return json.dumps({"available": avail, "ready": ready, "busy": busy, "error": err}).encode("utf-8")


def start_twitter_login():
    """Open the headed X/Twitter login window in a background thread (one at a time)."""
    try:
        import twitter_login
    except Exception:
        return
    if not twitter_login.available():
        return
    with TIKTOK_LOCK:
        if TWITTER_LOGIN.get("busy"):
            return
        TWITTER_LOGIN["busy"] = True
        TWITTER_LOGIN["error"] = ""

    def _run():
        err = ""
        try:
            ok = twitter_login.login(status_cb=lambda m: print("[twitter-login]", m), timeout_s=300)
            if not ok:
                err = "Login window closed or timed out before sign-in completed."
        except Exception as exc:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            err = f"{exc.__class__.__name__}: {exc}"
            print("[twitter-login] error:", err)
        finally:
            with TIKTOK_LOCK:
                TWITTER_LOGIN["busy"] = False
                TWITTER_LOGIN["thread"] = None
                TWITTER_LOGIN["error"] = err

    print("[twitter-login] launching login browser window...")

    th = threading.Thread(target=_run, daemon=True)
    with TIKTOK_LOCK:
        TWITTER_LOGIN["thread"] = th
    th.start()


def higgsfield_status_payload():
    """JSON status for the Connect-Higgsfield control: available / ready / busy."""
    try:
        import higgsfield_login
        avail = higgsfield_login.available()
        ready = higgsfield_login.is_ready()
    except Exception:
        avail = ready = False
    with HIGGSFIELD_LOCK:
        busy = bool(HIGGSFIELD_LOGIN.get("busy"))
        err = HIGGSFIELD_LOGIN.get("error") or ""
    return json.dumps({"available": avail, "ready": ready, "busy": busy, "error": err}).encode("utf-8")


def start_higgsfield_login():
    """Open the headed Higgsfield login window in a background thread (one at a time)."""
    try:
        import higgsfield_login
    except Exception:
        return
    if not higgsfield_login.available():
        return
    with HIGGSFIELD_LOCK:
        if HIGGSFIELD_LOGIN.get("busy"):
            return
        HIGGSFIELD_LOGIN["busy"] = True
        HIGGSFIELD_LOGIN["error"] = ""

    def _run():
        err = ""
        try:
            ok = higgsfield_login.login(status_cb=lambda m: print("[higgsfield-login]", m), timeout_s=300)
            if not ok:
                err = "Login window closed or timed out before sign-in completed."
        except Exception as exc:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            err = f"{exc.__class__.__name__}: {exc}"
            print("[higgsfield-login] error:", err)
        finally:
            with HIGGSFIELD_LOCK:
                HIGGSFIELD_LOGIN["busy"] = False
                HIGGSFIELD_LOGIN["thread"] = None
                HIGGSFIELD_LOGIN["error"] = err

    print("[higgsfield-login] launching login browser window...")

    th = threading.Thread(target=_run, daemon=True)
    with HIGGSFIELD_LOCK:
        HIGGSFIELD_LOGIN["thread"] = th
    th.start()


def content_type_for(path):
    suffix = Path(path).suffix.lower()
    if suffix == ".ico":
        return "image/x-icon"
    if suffix == ".mp4":
        return "video/mp4"
    if suffix == ".webm":
        return "video/webm"
    if suffix in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if suffix == ".png":
        return "image/png"
    if suffix == ".webp":
        return "image/webp"
    if suffix == ".mp3":
        return "audio/mpeg"
    if suffix == ".wav":
        return "audio/wav"
    if suffix in {".m4a", ".aac"}:
        return "audio/mp4"
    if suffix == ".ogg":
        return "audio/ogg"
    if suffix == ".flac":
        return "audio/flac"
    if suffix in {".txt", ".json", ".log", ".md"}:
        return "text/plain; charset=utf-8"
    return "application/octet-stream"


def render_file_view(path, job_id=""):
    back = "/assets" if job_id == "assets" else f"/job?id={urllib.parse.quote(job_id)}" if job_id else "/"
    actions = f"""
    <div class="job-actions">
      <a class="button secondary" href="{back}">Back</a>
      <a class="button" href="{link_for(path)}">Raw file</a>
    </div>
    """
    if path.is_dir():
        items = []
        for child in sorted(path.iterdir()):
            items.append(f'<li><a href="{view_for(child, job_id)}">{esc(child.name)}</a></li>')
        content = f'<ul class="file-list">{"".join(items)}</ul>'
    elif is_image_path(path):
        content = f'<img src="{link_for(path)}" alt="{esc(path.name)}">'
    elif is_video_path(path):
        content = f'<video controls autoplay src="{link_for(path)}"></video>'
    elif is_audio_path(path):
        content = f'<audio controls autoplay src="{link_for(path)}"></audio>'
    elif path.suffix.lower() in {".txt", ".json", ".log", ".md"}:
        content = f'<pre>{esc(path.read_text(encoding="utf-8", errors="replace"))}</pre>'
    else:
        content = f'<p class="hint">Preview is not available for this file type.</p>'
    body = f"""
    <section class="media-page">
      <div class="job-head">
        <div>
          <h1>{esc(path.name)}</h1>
          <div class="hint">{esc(path)}</div>
        </div>
        {actions}
      </div>
      <div class="panel">{content}</div>
    </section>
    """
    return page(path.name, body)


class Handler(BaseHTTPRequestHandler):
    def send_bytes(self, data, content_type="text/html; charset=utf-8"):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def serve_file_ranged(self, path, content_type=None):
        """Serve a file with HTTP Range support so the browser <video>/<audio> element can
        actually stream and SEEK it. Without 206 partial-content responses, Chrome refuses to
        play many MP4s inline (the renders/media looked 'not playable')."""
        path = Path(path)
        try:
            file_size = path.stat().st_size
        except OSError:
            self.send_error(404)
            return
        ctype = content_type or content_type_for(path)
        range_header = self.headers.get("Range")
        start, end = 0, file_size - 1
        is_partial = False
        if range_header and range_header.strip().lower().startswith("bytes="):
            try:
                spec = range_header.split("=", 1)[1].split(",")[0].strip()
                s, _, e = spec.partition("-")
                if s.strip() == "":                      # suffix range: bytes=-N (last N bytes)
                    n = int(e)
                    start = max(0, file_size - n)
                    end = file_size - 1
                else:
                    start = int(s)
                    end = int(e) if e.strip() else file_size - 1
                end = min(end, file_size - 1)
                if start > end or start >= file_size:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{file_size}")
                    self.end_headers()
                    return
                is_partial = True
            except (ValueError, IndexError):
                start, end = 0, file_size - 1
                is_partial = False
        length = end - start + 1
        self.send_response(206 if is_partial else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if is_partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        if self.command == "HEAD":
            return
        try:
            with open(path, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(262144, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass        # the player closed the connection mid-stream (normal when seeking)

    def serve_voice_preview(self, voice):
        """Generate (once, cached) and serve a ~10s sample of a Gemini TTS voice so the user can
        preview it before committing. Cached under generated_assets/voice_previews/<voice>.wav."""
        voice = (voice or "").strip()
        if voice not in pipeline.GEMINI_TTS_VOICES:
            self.send_error(400, "Unknown voice")
            return
        prev_dir = ROOT / "generated_assets" / "voice_previews"
        try:
            prev_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        path = prev_dir / f"{voice}.wav"
        if not path.exists() or path.stat().st_size < 4096:
            if not os.environ.get("WAVESPEED_API_KEY", "").strip():
                self.send_error(503, "WAVESPEED_API_KEY not set")
                return
            sample = ("Hey — this is a quick preview of this voice. I can narrate your story with "
                      "energy, warmth, and a clear, punchy delivery for your short videos.")
            try:
                out = pipeline.generate_speech_gemini(sample, path, voice=voice, model="pro", status_cb=None)
                path = Path(out)
            except Exception as exc:  # noqa: BLE001
                print("[voice-preview] failed:", exc)
                self.send_error(500, "Voice preview generation failed")
                return
        self.serve_file_ranged(path, "audio/wav")

    def send_static_asset(self, name):
        allowed = {
            "app_icon.ico": "image/x-icon",
            "app_icon.png": "image/png",
            "favicon.ico": "image/x-icon",
            "start_icon.ico": "image/x-icon",
            "lab_bg.png": "image/png",
            # chat shell assets
            "chat-shell.css": "text/css; charset=utf-8",
            "chat-shell.js": "application/javascript; charset=utf-8",
            "timeline-theme.css": "text/css; charset=utf-8",
        }
        if name not in allowed:
            self.send_error(404)
            return
        path = STATIC_DIR / name
        if not path.exists():
            self.send_error(404)
            return
        self.send_bytes(path.read_bytes(), allowed[name])

    def do_HEAD(self):
        # Players probe video/audio with HEAD before streaming; answer /file with real headers.
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/file":
            resolved = safe_requested_path(urllib.parse.parse_qs(parsed.query).get("path", [""])[0])
            if not resolved or resolved.is_dir():
                self.send_error(404)
                return
            self.serve_file_ranged(resolved)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        q_all = urllib.parse.parse_qs(parsed.query)
        legacy = q_all.get("legacy_ui", ["0"])[0] in ("1", "true", "yes")
        if parsed.path == "/timeline-versions":
            slug = str(q_all.get("slug", [""])[0])
            self.send_bytes(json.dumps({"ok": True, "versions": timeline_versions(slug)}).encode("utf-8"),
                            "application/json; charset=utf-8")
        elif parsed.path == "/":
            q = q_all
            if legacy:
                self.send_bytes(form_page(clear="new" in q, open_load="load" in q, load_slug=q.get("project", [""])[0]))
                return
            initial = {}
            if "new" in q:
                initial["new"] = True
            if q.get("project", [""])[0]:
                initial["project"] = q.get("project", [""])[0]
            self.send_bytes(chat_ui.chat_shell_page(initial))
        elif parsed.path == "/manifest.webmanifest":
            manifest = {
                "name": "Autonomous Shorts Agent",
                "short_name": "Shorts Agent",
                "description": "AI assisted vertical short video generator.",
                "start_url": "/",
                "scope": "/",
                "display": "standalone",
                "background_color": "#090d14",
                "theme_color": "#090d14",
                "icons": [
                    {
                        "src": "/static/app_icon.png",
                        "sizes": "512x512",
                        "type": "image/png",
                        "purpose": "any maskable",
                    },
                    {
                        "src": "/favicon.ico",
                        "sizes": "16x16 32x32 48x48 256x256",
                        "type": "image/x-icon",
                    },
                ],
            }
            self.send_bytes(json.dumps(manifest).encode("utf-8"), "application/manifest+json; charset=utf-8")
        elif parsed.path == "/favicon.ico":
            self.send_static_asset("favicon.ico")
        elif parsed.path.startswith("/static/"):
            self.send_static_asset(Path(parsed.path).name)
        elif parsed.path == "/dev-tools":
            self.send_bytes(dev_tools_page())
        elif parsed.path == "/assets":
            show_hidden = q_all.get("show_hidden", ["0"])[0] in ("1", "true", "yes")
            if legacy:
                self.send_bytes(assets_page(show_hidden=show_hidden))
            else:
                self.send_bytes(chat_ui.chat_shell_page({"view": "assets"}))
        elif parsed.path == "/sfx":
            self.send_bytes(sfx_page() if legacy else chat_ui.chat_shell_page({"flow": "sfx"}))
        elif parsed.path == "/visual":
            self.send_bytes(visual_page() if legacy else chat_ui.chat_shell_page({"flow": "visual"}))
        elif parsed.path == "/viraltrans":
            self.send_bytes(viraltrans_page() if legacy else chat_ui.chat_shell_page({"flow": "viraltrans"}))
        elif parsed.path == "/captions":
            self.send_bytes(caption_page() if legacy else chat_ui.chat_shell_page({"flow": "captions"}))
        elif parsed.path == "/longform":
            self.send_bytes(longform_page() if legacy else chat_ui.chat_shell_page({"flow": "longform"}))
        elif parsed.path == "/projects-list":
            show_hidden = q_all.get("hidden", ["0"])[0] in ("1", "true", "yes")
            self.send_bytes(json.dumps(chat_ui.projects_list_payload(show_hidden=show_hidden)).encode("utf-8"),
                            "application/json; charset=utf-8")
        elif parsed.path == "/jobs-list":
            self.send_bytes(json.dumps(chat_ui.jobs_list_payload()).encode("utf-8"),
                            "application/json; charset=utf-8")
        elif parsed.path == "/chat-state":
            self.send_bytes(json.dumps(chat_ui.load_chat_state()).encode("utf-8"),
                            "application/json; charset=utf-8")
        elif parsed.path == "/timeline":
            slug = urllib.parse.parse_qs(parsed.query).get("slug", [""])[0]
            self.send_bytes(timeline_page(slug))
        elif parsed.path == "/progress":
            jid = urllib.parse.parse_qs(parsed.query).get("id", [""])[0]
            self.send_bytes(progress_page(jid))
        elif parsed.path == "/ui-state":
            self.send_bytes(json.dumps(load_ui_state()).encode("utf-8"), "application/json; charset=utf-8")
        elif parsed.path == "/presets":
            self.send_bytes(json.dumps({"user": load_presets(), "builtin": BUILTIN_PRESETS}).encode("utf-8"), "application/json; charset=utf-8")
        elif parsed.path == "/project-preset":
            slug = urllib.parse.parse_qs(parsed.query).get("slug", [""])[0]
            project_dir = safe_project_dir(slug)
            if not project_dir:
                self.send_error(404)
                return
            state = project_form_state(project_dir)
            payload = {
                "slug": project_dir.name,
                "title": state.get("title") or project_title_from_files(project_dir),
                "state": state,
            }
            self.send_bytes(json.dumps(payload).encode("utf-8"), "application/json; charset=utf-8")
        elif parsed.path == "/project-media":
            slug = urllib.parse.parse_qs(parsed.query).get("slug", [""])[0]
            project_dir = safe_project_dir(slug)
            if not project_dir:
                self.send_error(404)
                return
            self.send_bytes(json.dumps(project_media_payload(project_dir)).encode("utf-8"), "application/json; charset=utf-8")
        elif parsed.path == "/timeline-library":
            slug = urllib.parse.parse_qs(parsed.query).get("slug", [""])[0]
            if not safe_project_dir(slug):
                self.send_error(404)
                return
            self.send_bytes(json.dumps(timeline_library_payload(slug)).encode("utf-8"), "application/json; charset=utf-8")
        elif parsed.path == "/music-list":
            self.send_bytes(music_list_payload(), "application/json; charset=utf-8")
        elif parsed.path == "/voice-preview":
            voice = urllib.parse.parse_qs(parsed.query).get("voice", [""])[0]
            self.serve_voice_preview(voice)
        elif parsed.path == "/reddit":
            self.send_bytes(reddit_page() if legacy else chat_ui.chat_shell_page({"flow": "reddit"}))
        elif parsed.path == "/twitter-status":
            self.send_bytes(twitter_status_payload(), "application/json; charset=utf-8")
        elif parsed.path == "/tiktok-status":
            self.send_bytes(tiktok_status_payload(), "application/json; charset=utf-8")
        elif parsed.path == "/higgsfield-status":
            self.send_bytes(higgsfield_status_payload(), "application/json; charset=utf-8")
        elif parsed.path == "/scrape-browser-status":
            self.send_bytes(json.dumps(scrape_browser_preview.status()).encode("utf-8"),
                            "application/json; charset=utf-8")
        elif parsed.path == "/scrape-browser-preview":
            shot = scrape_browser_preview.snapshot().get("jpeg") or b""
            if not shot:
                self.send_error(404)
            else:
                self.send_bytes(shot, "image/jpeg")
        elif parsed.path == "/job":
            job_id = q_all.get("id", [""])[0]
            if legacy:
                self.send_bytes(job_page(job_id))
            else:
                self.send_bytes(chat_ui.chat_shell_page({"job": job_id}))
        elif parsed.path == "/job-status":
            job_id = urllib.parse.parse_qs(parsed.query).get("id", [""])[0]
            self.send_bytes(job_status_payload(job_id), "application/json; charset=utf-8")
        elif parsed.path == "/file":
            resolved = safe_requested_path(urllib.parse.parse_qs(parsed.query).get("path", [""])[0])
            if not resolved:
                self.send_error(404)
                return
            if resolved.is_dir():
                self.send_error(404)
                return
            # Range-capable streaming so videos/audio play and seek in the browser.
            self.serve_file_ranged(resolved)
        elif parsed.path == "/save-render":
            # Reliable "Download" for the native window: the WebView2 control does not act on
            # <a download> links, so copy the file server-side into the user's Downloads folder
            # (this machine == the app host) and report where it landed.
            resolved = safe_requested_path(urllib.parse.parse_qs(parsed.query).get("path", [""])[0])
            if not resolved or resolved.is_dir():
                self.send_bytes(json.dumps({"ok": False, "error": "file not found"}).encode("utf-8"),
                                "application/json; charset=utf-8")
                return
            try:
                import shutil
                downloads = Path.home() / "Downloads"
                if not downloads.is_dir():
                    downloads = Path.home()
                dest = downloads / resolved.name
                if dest.exists():
                    stem, suffix, i = dest.stem, dest.suffix, 1
                    while (downloads / f"{stem}_{i}{suffix}").exists():
                        i += 1
                    dest = downloads / f"{stem}_{i}{suffix}"
                shutil.copy2(resolved, dest)
                self.send_bytes(json.dumps({"ok": True, "name": dest.name, "dest": str(dest)}).encode("utf-8"),
                                "application/json; charset=utf-8")
            except Exception as exc:
                self.send_bytes(json.dumps({"ok": False, "error": str(exc)}).encode("utf-8"),
                                "application/json; charset=utf-8")
        elif parsed.path == "/view":
            query = urllib.parse.parse_qs(parsed.query)
            resolved = safe_requested_path(query.get("path", [""])[0])
            if not resolved:
                self.send_error(404)
                return
            self.send_bytes(render_file_view(resolved, query.get("job", [""])[0]))
        else:
            self.send_error(404)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/dev-trainer-open":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length) if length else b""
                fields = urllib.parse.parse_qs(raw.decode("utf-8", errors="replace"))
                tool = fields.get("tool", [""])[0]
                run_id = fields.get("run_id", [""])[0]
                url = start_dev_trainer(tool, run_id=run_id)
                self.send_response(303)
                self.send_header("Location", url)
                self.end_headers()
            except Exception as exc:  # noqa: BLE001
                self.send_bytes(page("Developer tool error",
                                     f'<section class="panel"><h2>Could not open trainer</h2>'
                                     f'<p>{esc(exc)}</p><a class="button secondary" href="/dev-tools">Back</a></section>'))
            return
        if parsed.path == "/reddit-discover":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length:
                    self.rfile.read(length)
            except Exception:
                pass
            try:
                stories = reddit_discover_stories()
                self.send_bytes(json.dumps({"stories": stories}).encode("utf-8"),
                                "application/json; charset=utf-8")
            except Exception as exc:  # noqa: BLE001
                self.send_bytes(json.dumps({"error": str(exc)}).encode("utf-8"),
                                "application/json; charset=utf-8")
            return
        if parsed.path == "/reddit-generate":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length) if length else b"{}"
                data = json.loads(raw.decode("utf-8", "replace") or "{}")
            except Exception:
                data = {}
            story = data.get("story") if isinstance(data.get("story"), dict) else None
            if not story and data.get("story_id"):
                story = reddit_story_by_id(data.get("story_id"))
            if not story:
                self.send_bytes(json.dumps({"error": "Story not found; click Find 5 Stories again."}).encode("utf-8"),
                                "application/json; charset=utf-8")
                return
            if reddit_orchestrator is None:
                self.send_bytes(json.dumps({"error": "Reddit Story Mode is unavailable."}).encode("utf-8"),
                                "application/json; charset=utf-8")
                return
            job_id = start_reddit_job(story)
            self.send_bytes(json.dumps({"job": f"/job?id={job_id}", "job_id": job_id}).encode("utf-8"),
                            "application/json; charset=utf-8")
            return
        if parsed.path == "/tiktok-login":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length:
                    self.rfile.read(length)
            except Exception:
                pass
            start_tiktok_login()
            self.send_bytes(tiktok_status_payload(), "application/json; charset=utf-8")
            return
        if parsed.path == "/twitter-login":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length:
                    self.rfile.read(length)
            except Exception:
                pass
            start_twitter_login()
            self.send_bytes(twitter_status_payload(), "application/json; charset=utf-8")
            return
        if parsed.path == "/higgsfield-login":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length:
                    self.rfile.read(length)
            except Exception:
                pass
            start_higgsfield_login()
            self.send_bytes(higgsfield_status_payload(), "application/json; charset=utf-8")
            return
        if parsed.path == "/rename-project":
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length else b""
            try:
                data = json.loads(raw.decode("utf-8", errors="replace")) if raw else {}
            except Exception:
                data = {}
            result = rename_project_title(data.get("slug"), data.get("title"))
            self.send_bytes(json.dumps(result).encode("utf-8"), "application/json; charset=utf-8")
            return
        if parsed.path == "/hide-project":
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length else b""
            try:
                data = json.loads(raw.decode("utf-8", errors="replace")) if raw else {}
            except Exception:
                data = {}
            result = set_project_hidden(data.get("slug"), bool(data.get("hidden")))
            self.send_bytes(json.dumps(result).encode("utf-8"), "application/json; charset=utf-8")
            return
        if parsed.path == "/resume-project":
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length else b""
            try:
                data = json.loads(raw.decode("utf-8", errors="replace")) if raw else {}
            except Exception:
                data = {}
            result = resume_project(data.get("slug"))
            self.send_bytes(json.dumps(result).encode("utf-8"), "application/json; charset=utf-8")
            return
        if parsed.path == "/ui-state":
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw.decode("utf-8", errors="replace") or "{}")
            except Exception:
                data = {}
            save_ui_state(data)
            self.send_response(204)
            self.end_headers()
            return
        if parsed.path == "/chat-state":
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw.decode("utf-8", errors="replace") or "{}")
            except Exception:
                data = {}
            ok = chat_ui.save_chat_state(data)
            self.send_bytes(json.dumps({"ok": bool(ok)}).encode("utf-8"), "application/json; charset=utf-8")
            return
        if parsed.path == "/save-preset":
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length)
            try:
                payload = json.loads(raw.decode("utf-8", errors="replace") or "{}")
            except Exception:
                payload = {}
            ok = save_named_preset(payload.get("name"), payload.get("data") if isinstance(payload.get("data"), dict) else {})
            self.send_response(200 if ok else 400)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": ok}).encode("utf-8"))
            return
        if parsed.path == "/delete-preset":
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length)
            try:
                payload = json.loads(raw.decode("utf-8", errors="replace") or "{}")
            except Exception:
                payload = {}
            ok = delete_named_preset(payload.get("name"))
            self.send_response(200 if ok else 400)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": ok}).encode("utf-8"))
            return
        if parsed.path == "/approve-speech":
            _q = urllib.parse.parse_qs(parsed.query)
            job_id = _q.get("id", [""])[0]
            _spd = _q.get("speed", [""])[0]
            with JOB_LOCK:
                job = JOBS.get(job_id)
                if job and job.get("status") == "awaiting_approval":
                    job["speech_decision"] = "approve"
                    try:
                        job["speech_speed_choice"] = float(_spd) if _spd else None
                    except (TypeError, ValueError):
                        job["speech_speed_choice"] = None
                    ev = job.get("approval_event")
                if job:
                    ev = job.get("approval_event")
            if job and job.get("approval_event"):
                job["approval_event"].set()
            self.send_response(303)
            self.send_header("Location", f"/job?id={urllib.parse.quote(job_id)}")
            self.end_headers()
            return
        if parsed.path == "/replace-speech":
            job_id = urllib.parse.parse_qs(parsed.query).get("id", [""])[0]
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            posted = urllib.parse.parse_qs(body.decode("utf-8", errors="replace"))
            with JOB_LOCK:
                job = JOBS.get(job_id)
                restart_fields = dict(job.get("restart_fields") or {}) if job else None
                proj = job.get("project_dir") if job else None
            if not job or restart_fields is None:
                self.send_error(404)
                return
            # tell the paused run to stop (the gate raises and the worker ends)
            with JOB_LOCK:
                job["speech_decision"] = "replace"
            if job.get("approval_event"):
                job["approval_event"].set()
            # delete the old voiceover so the restarted run regenerates it with the new voice
            if proj:
                try:
                    for sub in ("voice", "input"):
                        d = Path(proj) / sub
                        if d.exists():
                            for f in d.glob("*"):
                                if f.suffix.lower() in {".wav", ".mp3", ".m4a"} and ("voice" in f.name.lower() or "hook" in f.name.lower() or "narration" in f.name.lower()):
                                    try: f.unlink()
                                    except Exception: pass
                except Exception:
                    pass
            # apply the user's new speaker choice and restart focusing on speech first
            for key in ("speaker_name", "tts_voice", "tts_model", "speaker_image_path"):
                vals = posted.get(key)
                if vals:
                    restart_fields[key] = vals[0]
            restart_fields["halt_after_speech"] = "on"
            restart_fields["loaded_project_source"] = restart_fields.get("loaded_project_source") or (Path(proj).name if proj else "")
            new_job = start_job(restart_fields, {})
            self.send_response(303)
            self.send_header("Location", f"/job?id={urllib.parse.quote(new_job)}")
            self.end_headers()
            return
        if parsed.path == "/exclude-run-media":
            length = int(self.headers.get("Content-Length", "0"))
            body = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8", errors="replace"))
            job_id = body.get("id", [""])[0]
            raw = body.get("path", [""])[0]
            resolved = safe_requested_path(raw)
            with JOB_LOCK:
                job = JOBS.get(job_id)
                project_dir = Path(job.get("project_dir")) if job and job.get("project_dir") else None
                exclusions = job.get("media_exclusions") if job else None
                exclusion_lock = job.get("media_exclusion_lock") if job else None
            ok = False
            error = "Invalid job or media path."
            if resolved and resolved.is_file() and project_dir and project_dir.resolve() in resolved.parents:
                parts = {part.lower() for part in resolved.parts}
                is_candidate = "_candidates" in parts and "_raw" not in parts
                is_assigned = (resolved.parent.name.lower() == "seedance 2.0"
                               and resolved.name.lower().startswith("scraped_")
                               and is_video_path(resolved))
                if is_candidate or is_assigned:
                    try:
                        meta_path = resolved.with_suffix(".json")
                        meta = read_json_file(meta_path)
                        excluded_values = {str(resolved.resolve()).lower()}
                        source_path = str(meta.get("source_path") or "")
                        if source_path:
                            try:
                                source = Path(source_path).resolve()
                                if project_dir.resolve() in source.parents:
                                    excluded_values.add(str(source).lower())
                            except Exception:
                                pass
                        if exclusion_lock and exclusions is not None:
                            with exclusion_lock:
                                exclusions.update(excluded_values)
                        quarantine = project_dir / "seedance 2.0" / "_manual_removed"
                        quarantine.mkdir(parents=True, exist_ok=True)
                        stamp = int(time.time() * 1000)
                        dest = quarantine / f"{stamp}_{resolved.name}"
                        import shutil as _sh
                        _sh.move(str(resolved), str(dest))
                        if meta_path.exists():
                            _sh.move(str(meta_path), str(dest.with_suffix(".json")))
                        with JOB_LOCK:
                            current = JOBS.get(job_id)
                            if current:
                                current["logs"].append(f"Manual media exclusion: {resolved.name}")
                                current.setdefault("log_times", []).append(time.time())
                        ok = True
                        error = ""
                    except Exception as exc:
                        error = f"Could not quarantine clip: {exc}"
                else:
                    error = "Only accepted candidates or assigned clips can be removed here."
            self.send_response(200 if ok else 400)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": ok, "error": error}).encode("utf-8"))
            return
        if parsed.path == "/accept-media":
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            raw = urllib.parse.parse_qs(body.decode("utf-8", errors="replace")).get("path", [""])[0]
            resolved = safe_requested_path(raw)
            projects_root = agent_core.PROJECTS_DIR.resolve()
            ok = False
            if (resolved and resolved.is_file() and projects_root in resolved.parents
                    and "_declined" in {part.lower() for part in resolved.parts}):
                try:
                    import shutil as _sh
                    dest = resolved.parent.parent / f"manual_{resolved.stem.replace('declined_', '')}.mp4"
                    _sh.move(str(resolved), str(dest))
                    try:
                        resolved.with_suffix(".json").unlink(missing_ok=True)
                    except Exception:
                        pass
                    ok = True
                except Exception:
                    ok = False
            self.send_response(200 if ok else 400)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": ok}).encode("utf-8"))
            return
        if parsed.path == "/delete-media":
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            raw = urllib.parse.parse_qs(body.decode("utf-8", errors="replace")).get("path", [""])[0]
            resolved = safe_requested_path(raw)
            projects_root = agent_core.PROJECTS_DIR.resolve()
            ok = False
            if (resolved and resolved.is_file()
                    and projects_root in resolved.parents):
                try:
                    resolved.unlink()
                    ok = True
                except Exception:
                    ok = False
            self.send_response(200 if ok else 400)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": ok}).encode("utf-8"))
            return
        if parsed.path == "/cancel":
            job_id = urllib.parse.parse_qs(parsed.query).get("id", [""])[0]
            ok, _ = cancel_job(job_id)
            if not ok:
                self.send_error(404)
                return
            self.send_response(303)
            self.send_header("Location", f"/job?id={urllib.parse.quote(job_id)}")
            self.end_headers()
            return
        if parsed.path == "/replace-media":
            job_id = urllib.parse.parse_qs(parsed.query).get("id", [""])[0]
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            values = urllib.parse.parse_qs(body.decode("utf-8", errors="replace")).get("media_path", [])
            ok, message = queue_media_replacements(job_id, values)
            wants_json = "application/json" in self.headers.get("Accept", "")
            if wants_json:
                payload = {"ok": ok, "message": message}
                self.send_bytes(json.dumps(payload).encode("utf-8"), "application/json; charset=utf-8")
                return
            if not ok:
                with JOB_LOCK:
                    job = JOBS.get(job_id)
                    if job:
                        job["logs"].append(f"Replacement selection ignored: {message}")
                self.send_response(303)
                self.send_header("Location", f"/job?id={urllib.parse.quote(job_id)}")
                self.end_headers()
                return
            self.send_response(303)
            self.send_header("Location", f"/job?id={urllib.parse.quote(job_id)}")
            self.end_headers()
            return
        if parsed.path == "/sfx-label":
            length = int(self.headers.get("Content-Length", "0"))
            try:
                data = json.loads(self.rfile.read(length).decode("utf-8", errors="replace") or "{}")
            except Exception:
                data = {}
            res = save_sfx_label(data)
            self.send_bytes(json.dumps(res).encode("utf-8"), "application/json; charset=utf-8")
            return
        if parsed.path == "/timeline-redo-sfx":
            length = int(self.headers.get("Content-Length", "0"))
            try:
                data = json.loads(self.rfile.read(length).decode("utf-8", errors="replace") or "{}")
            except Exception:
                data = {}
            slug = str(data.get("slug", ""))
            if not slug or not safe_project_dir(slug):
                self.send_bytes(json.dumps({"ok": False, "error": "Unknown project."}).encode("utf-8"), "application/json; charset=utf-8")
                return
            snapshot_timeline_version(slug, "Before redo SFX")
            jid = start_redo_sfx_job(slug, reasoning_model=data.get("reasoning_model"),
                                     sfx_amount=data.get("sfx_amount", "medium"))
            self.send_bytes(json.dumps({"ok": True, "job_id": jid}).encode("utf-8"), "application/json; charset=utf-8")
            return
        if parsed.path == "/timeline-version-restore":
            length = int(self.headers.get("Content-Length", "0"))
            try:
                data = json.loads(self.rfile.read(length).decode("utf-8", errors="replace") or "{}")
                restore_timeline_version(str(data.get("slug", "")), str(data.get("version_id", "")))
                self.send_bytes(json.dumps({"ok": True}).encode("utf-8"), "application/json; charset=utf-8")
            except Exception as exc:
                self.send_bytes(json.dumps({"ok": False, "error": str(exc)}).encode("utf-8"), "application/json; charset=utf-8")
            return
        if parsed.path == "/timeline-render":
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw.decode("utf-8", errors="replace") or "{}")
            except Exception:
                data = {}
            slug = str(data.get("slug", ""))
            edits = data.get("edits") or {}
            if not slug or not safe_project_dir(slug):
                self.send_bytes(json.dumps({"ok": False, "error": "Unknown project."}).encode("utf-8"), "application/json; charset=utf-8")
                return
            job_id = start_timeline_job(slug, edits)
            self.send_bytes(json.dumps({"ok": True, "job": f"/job?id={urllib.parse.quote(job_id)}"}).encode("utf-8"), "application/json; charset=utf-8")
            return
        if parsed.path == "/timeline-replace":
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw.decode("utf-8", errors="replace") or "{}")
            except Exception:
                data = {}
            slug = str(data.get("slug", ""))
            media_path = str(data.get("media_path", ""))
            project_dir = safe_project_dir(slug)
            if not project_dir or not media_path or not valid_replace_path_values([media_path]):
                self.send_bytes(json.dumps({"ok": False, "error": "Only web images of this project can be replaced."}).encode("utf-8"), "application/json; charset=utf-8")
                return
            snapshot_timeline_version(slug, "Before replacing timeline media")
            fields = project_form_state(project_dir)
            for key, value in list(fields.items()):
                if isinstance(value, bool):
                    fields[key] = "on" if value else ""
            fields["loaded_project_source"] = slug
            fields["slug"] = slug
            fields["loaded_project_mode"] = "recut_existing_only"
            fields["initial_replace_media_path"] = [media_path]
            job_id = start_job(fields, {})
            self.send_bytes(json.dumps({"ok": True, "job": f"/job?id={urllib.parse.quote(job_id)}"}).encode("utf-8"), "application/json; charset=utf-8")
            return
        if parsed.path == "/timeline-save":
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw.decode("utf-8", errors="replace") or "{}")
            except Exception:
                data = {}
            slug = str(data.get("slug", ""))
            if not slug or not safe_project_dir(slug):
                self.send_bytes(json.dumps({"ok": False, "error": "Unknown project."}).encode("utf-8"), "application/json; charset=utf-8")
                return
            try:
                result = agent_core.save_timeline_edits(slug, data.get("edits") or {})
                self.send_bytes(json.dumps({"ok": True, "scenes": result.get("scenes")}).encode("utf-8"), "application/json; charset=utf-8")
            except Exception as exc:
                self.send_bytes(json.dumps({"ok": False, "error": str(exc)}).encode("utf-8"), "application/json; charset=utf-8")
            return
        if parsed.path == "/timeline-rescript":
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw.decode("utf-8", errors="replace") or "{}")
            except Exception:
                data = {}
            slug = str(data.get("slug", ""))
            new_script = str(data.get("script", "") or "").strip()
            if not slug or not safe_project_dir(slug):
                self.send_bytes(json.dumps({"ok": False, "error": "Unknown project."}).encode("utf-8"), "application/json; charset=utf-8")
                return
            if not new_script:
                self.send_bytes(json.dumps({"ok": False, "error": "The script is empty."}).encode("utf-8"), "application/json; charset=utf-8")
                return
            hook_text = data.get("hook_text")
            if hook_text is not None:
                hook_text = str(hook_text)
            voice_settings = {key: str(data.get(key)).strip()
                              for key in ("speaker_name", "tts_voice", "tts_model")
                              if data.get(key) is not None and str(data.get(key)).strip()}
            clip_density = str(data.get("clip_density") or "medium").strip().lower()
            if clip_density not in ("few", "medium", "many"):
                clip_density = "medium"
            media_source = str(data.get("media_source") or "scrape").strip().lower()
            if media_source not in ("scrape", "keep_visible", "library"):
                media_source = "scrape"
            job_id = start_timeline_rescript_job(slug, new_script, hook_text=hook_text,
                                                 voice_settings=voice_settings,
                                                 clip_density=clip_density, media_source=media_source)
            self.send_bytes(json.dumps({"ok": True, "job": f"/job?id={job_id}"}).encode("utf-8"), "application/json; charset=utf-8")
            return
        if parsed.path == "/timeline-rework":
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw.decode("utf-8", errors="replace") or "{}")
            except Exception:
                data = {}
            slug = str(data.get("slug", ""))
            project_dir = safe_project_dir(slug)
            if not project_dir:
                self.send_bytes(json.dumps({"ok": False, "error": "Unknown project."}).encode("utf-8"), "application/json; charset=utf-8")
                return
            do_replace = bool(data.get("replace_media"))
            do_recut = bool(data.get("reorder_recut"))
            do_revoice = bool(data.get("regenerate_speech"))
            do_redo_sfx = bool(data.get("redo_sfx"))
            do_add_more_sfx = bool(data.get("add_more_sfx"))
            do_redo_captions = bool(data.get("redo_captions"))
            sfx_amount = str(data.get("sfx_amount") or "medium").strip().lower()
            if sfx_amount not in ("low", "medium", "high"):
                sfx_amount = "medium"
            reasoning_model = str(data.get("reasoning_model") or "openai/gpt-5.5").strip()
            if reasoning_model not in {"anthropic/claude-fable-5", "anthropic/claude-sonnet-5",
                                       "anthropic/claude-opus-4.8", "openai/gpt-5.5",
                                       "openai/gpt-5.6-sol", "openai/gpt-5.6-terra", "openai/gpt-5.6-luna",
                                       "google/gemini-3.5-flash", "google/gemini-3.1-flash-lite",
                                       "google/gemini-3.1-pro-preview"}:
                reasoning_model = "openai/gpt-5.5"
            reasoning_mode = reasoning_modes.validate_reasoning_mode(reasoning_model, data.get("reasoning_mode"))
            reasoning_modes.set_current_reasoning_mode(reasoning_model, reasoning_mode)
            # 1) persist the current timeline (order/durations/etc.) so the rework builds on it
            try:
                agent_core.save_timeline_edits(slug, data.get("edits") or {})
            except Exception as exc:
                self.send_bytes(json.dumps({"ok": False, "error": f"Could not save edits: {exc}"}).encode("utf-8"), "application/json; charset=utf-8")
                return
            snapshot_timeline_version(slug, "Before " + ("adding more SFX" if do_add_more_sfx else
                                                         "redo captions" if do_redo_captions else "redo/replacement rework"))
            # COMBINED reworks: an SFX redo/add can run together with "redo captions" in ONE job
            # (captions are re-aligned first, then the SFX pass renders the final with both).
            if do_redo_sfx or do_add_more_sfx:
                if do_redo_sfx and do_add_more_sfx:
                    self.send_bytes(json.dumps({"ok": False, "error": "Choose either redo SFX or add more SFX."}).encode("utf-8"), "application/json; charset=utf-8")
                    return
                job_id = start_redo_sfx_job(slug, reasoning_model=reasoning_model, reasoning_mode=reasoning_mode, sfx_amount=sfx_amount,
                                            mode="add" if do_add_more_sfx else "redo",
                                            regen_captions=do_redo_captions)
                self.send_bytes(json.dumps({"ok": True, "job": f"/job?id={urllib.parse.quote(job_id)}"}).encode("utf-8"), "application/json; charset=utf-8")
                return
            # Redo captions on its own (no SFX / media re-plan): re-align captions, then re-render.
            if do_redo_captions and not (do_revoice or do_replace or do_recut):
                job_id = start_timeline_job(slug, data.get("edits") or {}, regen_captions=True)
                self.send_bytes(json.dumps({"ok": True, "job": f"/job?id={urllib.parse.quote(job_id)}"}).encode("utf-8"), "application/json; charset=utf-8")
                return
            # A new take changes the authoritative clock. Generate and align it before any
            # optional social replacement so new searches use the newly timed spoken lines.
            if do_revoice:
                replace_ids = [str(value) for value in (data.get("replace_ids") or []) if str(value)]
                if do_replace:
                    try:
                        saved_config = agent_core.load_project_config(slug)
                    except Exception:
                        saved_config = {}
                    if str(saved_config.get("clip_source") or "").lower() != "scrape":
                        self.send_bytes(json.dumps({
                            "ok": False,
                            "error": "Regenerate speech first, then run web-image replacement as a separate rework. Combined speech + replacement is available for TikTok/X projects."
                        }).encode("utf-8"), "application/json; charset=utf-8")
                        return
                    if not replace_ids:
                        self.send_bytes(json.dumps({"ok": False, "error": "Mark at least one scrape clip for replacement."}).encode("utf-8"), "application/json; charset=utf-8")
                        return
                job_id = start_timeline_revoice_job(slug, replace_ids if do_replace else None,
                                                    reasoning_model=reasoning_model,
                                                    reasoning_mode=reasoning_mode)
                self.send_bytes(json.dumps({"ok": True, "job": f"/job?id={urllib.parse.quote(job_id)}"}).encode("utf-8"), "application/json; charset=utf-8")
                return
            # Scrape projects replace marked scenes by running a NEW targeted TikTok/X search.
            # Do not route them through the old web-image-only replacement path.
            if do_replace:
                try:
                    saved_config = agent_core.load_project_config(slug)
                except Exception:
                    saved_config = {}
                if str(saved_config.get("clip_source") or "").lower() == "scrape":
                    replace_ids = [str(value) for value in (data.get("replace_ids") or []) if str(value)]
                    if not replace_ids:
                        self.send_bytes(json.dumps({"ok": False, "error": "Mark at least one scrape clip for replacement."}).encode("utf-8"), "application/json; charset=utf-8")
                        return
                    job_id = start_timeline_social_replace_job(slug, replace_ids,
                                                               reasoning_model=reasoning_model,
                                                               reasoning_mode=reasoning_mode)
                    self.send_bytes(json.dumps({"ok": True, "job": f"/job?id={urllib.parse.quote(job_id)}"}).encode("utf-8"), "application/json; charset=utf-8")
                    return
            # 2) resolve the marked clips' replaceable media paths
            replace_paths = []
            if do_replace:
                replace_paths = resolve_replace_media_paths(slug, data.get("replace_ids") or [])
                if not replace_paths:
                    self.send_bytes(json.dumps({"ok": False, "error": "None of the marked clips use a replaceable web image."}).encode("utf-8"), "application/json; charset=utf-8")
                    return
            fields = project_form_state(project_dir)
            for key, value in list(fields.items()):
                if isinstance(value, bool):
                    fields[key] = "on" if value else ""
            fields["loaded_project_source"] = slug
            fields["slug"] = slug
            fields["loaded_project_mode"] = "recut_existing_only"
            fields["reasoning_model"] = reasoning_model
            fields["reasoning_mode"] = reasoning_mode or ""
            if replace_paths:
                fields["initial_replace_media_path"] = replace_paths
            job_id = start_job(fields, {})
            self.send_bytes(json.dumps({"ok": True, "job": f"/job?id={urllib.parse.quote(job_id)}"}).encode("utf-8"), "application/json; charset=utf-8")
            return
        if parsed.path == "/sfx-run":
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            content_type = self.headers.get("Content-Type", "")
            if "multipart/form-data" in content_type:
                fields, files = parse_multipart(content_type, body)
            else:
                fields, files = {}, {}
            job_id = start_sfx_job(fields, files)
            self.send_response(303)
            self.send_header("Location", f"/job?id={job_id}")
            self.end_headers()
            return
        if parsed.path == "/visual-run":
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            content_type = self.headers.get("Content-Type", "")
            if "multipart/form-data" in content_type:
                fields, files = parse_multipart(content_type, body)
            else:
                fields, files = {}, {}
            job_id = start_visual_job(fields, files)
            self.send_response(303)
            self.send_header("Location", f"/job?id={job_id}")
            self.end_headers()
            return
        if parsed.path == "/viraltrans-generate":
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            fields = urllib.parse.parse_qs(body.decode("utf-8", errors="replace"))
            job_id = start_viraltrans_job(fields)
            self.send_response(303)
            self.send_header("Location", f"/job?id={job_id}")
            self.end_headers()
            return
        if parsed.path == "/captions-run":
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            content_type = self.headers.get("Content-Type", "")
            if "multipart/form-data" in content_type:
                fields, files = parse_multipart(content_type, body)
            else:
                fields, files = {}, {}
            job_id = start_caption_job(fields, files)
            self.send_response(303)
            self.send_header("Location", f"/job?id={job_id}")
            self.end_headers()
            return
        if parsed.path == "/longform-run":
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            content_type = self.headers.get("Content-Type", "")
            if "multipart/form-data" in content_type:
                fields, files = parse_multipart(content_type, body)
            else:
                fields, files = {}, {}
            # pasted script -> full longform VIDEO pipeline; uploaded prompt .txt -> the
            # legacy image-set generator (kept as a compatibility path)
            if str(fields.get("script") or "").strip():
                job_id = start_longform_video_job(fields)
            else:
                job_id = start_longform_job(fields, files)
            self.send_response(303)
            self.send_header("Location", f"/job?id={job_id}")
            self.end_headers()
            return
        if parsed.path != "/run":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" in content_type:
            fields, files = parse_multipart(content_type, body)
        else:
            parsed_fields = urllib.parse.parse_qs(body.decode("utf-8", errors="replace"))
            fields = {k: (v if k in ("initial_replace_media_path", "initial_remove_media_path") else v[0]) for k, v in parsed_fields.items()}
            files = {}
        save_ui_state(ui_state_from_form_fields(fields))
        job_id = start_job(fields, files)
        self.send_response(303)
        self.send_header("Location", f"/job?id={job_id}")
        self.end_headers()

    def log_message(self, fmt, *args):
        return


def launch_browser(host, port):
    import time
    import subprocess
    import webbrowser
    time.sleep(1)
    url = f"http://{host}:{port}/?nocache={int(time.time())}"
    try:
        chrome_path = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
        if Path(chrome_path).exists():
            subprocess.Popen([chrome_path, f"--app={url}", "--window-size=1920,1080", "--window-position=0,0"])
            return
        edge_path = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
        if Path(edge_path).exists():
            subprocess.Popen([edge_path, f"--app={url}", "--window-size=1920,1080", "--window-position=0,0"])
            return
    except Exception:
        pass
    webbrowser.open(url)


class QuietServer(ThreadingHTTPServer):
    """Suppress the full traceback wall for ROUTINE client disconnects. Every hover-play /
    seek on a media-heavy page aborts a video Range request mid-transfer, which the stock
    server prints as a scary multi-frame traceback - the console then looks like the app
    is crashing nonstop while nothing is actually wrong."""

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionAbortedError, ConnectionResetError,
                            BrokenPipeError, TimeoutError)):
            return                      # browser cancelled a media request - routine
        super().handle_error(request, client_address)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7865)
    args = parser.parse_args()
    server = QuietServer((args.host, args.port), Handler)
    print(f"Autonomous Shorts Agent running at http://{args.host}:{args.port}")
    import threading
    # threading.Thread(target=launch_browser, args=(args.host, args.port), daemon=True).start()
    server.serve_forever()


if __name__ == "__main__":
    main()
