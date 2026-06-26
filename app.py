import argparse
import html
import json
import re
import threading
import time
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import agent_core
import pipeline
import sfx_agent


ROOT = Path(__file__).resolve().parent
SPEAKER_GALLERY_DIR = ROOT / "speaker images"
SPEAKER_UPLOADED_DIR = ROOT / "speaker" / "uploaded"
SPEAKER_GALLERY_DIR.mkdir(exist_ok=True)
SPEAKER_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
JOBS = {}
JOB_LOCK = threading.Lock()
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
    "loaded_project_mode": "normal",
    "run_type": "normal",
    "speaker_name": "Narrator",
    "tts_voice": "Achernar",
    "tts_model": "flash",
    "image_model": "openai/gpt-image-2/text-to-image",
    "hook_text": "",
    "hook_pause_s": "0.45",
    "speaker_image_path": "",
    "clip_source": "generate",
    "scrape_platforms": "tiktok",
    "scrape_terms": "",
    "script_relevancy": "70",
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
        f'<a class="button secondary nav-sfx" href="/sfx">{ICON_SFX}<span>SFX master</span></a>'
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
        f'<div><h1>Shortslab</h1>{STEPS_HTML}</div>'
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
    "\U0001F1EF\U0001F1F5 Japanese Dark Facts": {
        "speaker_name": "Narrator", "tts_voice": "Charon", "tts_model": "pro",
        "video_model": "seedance-2.0", "image_model": "openai/gpt-image-2/text-to-image",
        "reasoning_model": "openai/gpt-5.5",
        "speaker_image_path": "", "visual_script": "",
        "use_visual_direction": False, "enable_speaker_hook": False,
        # no generated images — the video layer is filled by scraped real clips
        "out_web_images": False, "out_wikimedia": False, "out_gpt_images": False,
        "out_video_clips": True, "out_sfx": True, "out_transition_sfx": True,
        "out_background_music": True, "out_captions": True, "halt_after_speech": False,
        "clip_source": "scrape", "scrape_platforms": "tiktok",
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


def test_scrape_connection(cookies):
    """Probe TikTok/Instagram with the given connection (browser name or cookies.txt
    path) and report whether real clips are reachable. Returns {ok, message}."""
    if not cookies:
        return {"ok": False, "message": "Pick a browser you're signed in to, or give a cookies.txt path."}
    try:
        import clip_scraper
        import yt_dlp
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": f"Scraper unavailable ({exc.__class__.__name__})."}
    clip_scraper.set_cookies(cookies)
    try:
        opts = clip_scraper._ydl_opts({"playlistend": 3, "socket_timeout": 12})
        targets = [
            ("TikTok", "https://www.tiktok.com/tag/tokyofashion"),
            ("TikTok", "https://www.tiktok.com/tag/japan"),
        ]
        found, errors = [], []
        for name, url in targets:
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=False)
                ents = clip_scraper._entries(info)
                if ents:
                    found.append(f"{name} ({len(ents)} clips)")
                else:
                    errors.append(f"{name}: no clips")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{name}: {exc.__class__.__name__}")
    finally:
        clip_scraper.set_cookies(None)
    if found:
        return {"ok": True, "message": "Connected — reachable: " + ", ".join(found) + "."}
    detail = "; ".join(errors) if errors else "nothing returned"
    return {"ok": False, "message": "Couldn't reach TikTok clips (" + detail + "). Sign in to TikTok "
                                    "in that browser first; for Chrome/Edge, fully close the browser so its "
                                    "cookie file can be read."}


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
    return project_dir.name.replace("_", " ").strip().title()


def project_options_html():
    items = []
    for project_dir in list_previous_projects():
        created = time.strftime("%Y-%m-%d %H:%M", time.localtime(project_dir.stat().st_mtime))
        title = project_title_from_files(project_dir)
        slug = project_dir.name
        items.append(f'<div class="project-list-item" style="padding: 13px 15px; border: 1px solid var(--line); border-radius: var(--r-md); cursor: pointer; background: var(--bg-input); color: var(--text); transition: background .16s, border-color .16s;" onmouseover="this.style.background=\'var(--bg-overlay)\';this.style.borderColor=\'var(--accent)\'" onmouseout="this.style.background=\'var(--bg-input)\';this.style.borderColor=\'var(--line)\'" onclick="window.loadProject(\'{esc(slug)}\')"><div style="font-weight: 600; margin-bottom: 4px;">{esc(title)}</div><div style="font-size: 12px; color: var(--faint);">{esc(slug)} &bull; {esc(created)}</div></div>')
    return "".join(items)


def project_form_state(project_dir):
    state = load_ui_state()
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
    state["loaded_project_mode"] = "recut_existing_only"
    normalized = normalize_ui_state(state)
    normalized["slug"] = project_dir.name
    normalized["loaded_project_mode"] = "recut_existing_only"
    return normalized


def app_style():
    return """
    <style>
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
      main { max-width: 1320px; margin: 0 auto; padding: 26px clamp(18px, 3.5vw, 44px) 60px; position: relative; z-index: 1; }
      h1 {
        font-family: var(--display);
        font-size: 19px; margin: 0; letter-spacing: 0; font-weight: 400; line-height: 1.15;
        color: var(--ink); text-shadow: 2px 2px 0 rgba(232,71,43,.28);
      }
      h2 { font-family: var(--pixel); font-size: 15px; margin: 0 0 13px; color: var(--ink); font-weight: 400; letter-spacing: .3px; text-transform: uppercase; }
      ::selection { background: var(--accent-2); color: #fff; }
      :focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
      .top { display: flex; justify-content: space-between; gap: 24px; align-items: center; padding-bottom: 16px; margin-bottom: 24px; border-bottom: 3px solid var(--ink); }
      .brand { display: flex; gap: 13px; align-items: center; min-width: 0; }
      .brand-mark { display: none; }
      .sub { color: var(--muted); margin-top: 7px; max-width: 760px; line-height: 1.5; font-size: 14px; }
      .ico { flex: 0 0 auto; vertical-align: middle; }
      /* wordmark logo lockup: a violet glyph block before the title */
      /* pixel asterisk logo glyph before the wordmark */
      .brand > div { position: relative; padding-left: 42px; }
      .brand > div::before {
        content: "\\002731"; position: absolute; left: 0; top: -2px; width: 30px; height: 30px;
        display: grid; place-items: center; font-size: 22px; color: var(--accent);
        text-shadow: 2px 2px 0 var(--ink);
      }
      .nav-actions { display: flex; gap: 9px; flex-wrap: wrap; justify-content: flex-end; align-items: center; }
      .nav-actions .button {
        width: auto; min-width: 0; display: inline-flex; align-items: center; gap: 8px;
        padding: 9px 14px; font-size: 11px; font-weight: 400; white-space: nowrap;
        font-family: var(--pixel); text-transform: uppercase; letter-spacing: .3px;
      }
      .nav-actions .button .ico { opacity: 1; width: 15px; height: 15px; }
      .nav-sep { display: none; }
      .nav-sfx { background: var(--accent-2); color: #fff; border-color: var(--ink); }
      .nav-sfx:hover { background: #d63d22; }
      .nav-sfx .ico { color: #fff; }
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
        position: sticky; top: 10px; z-index: 30;
        display: flex; flex-direction: column; gap: 15px;
        padding: 17px 19px; border-radius: var(--r-lg);
        margin-bottom: 22px; border: 3px solid var(--ink);
        background: var(--bg-raised);
        box-shadow: var(--sh-2);
      }
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
      @media (max-width: 720px) { .create-bar .create-short-btn { width: 100%; align-self: stretch; } }
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
        opacity: 0; pointer-events: none; transition: opacity var(--dur) var(--ease), transform var(--dur) var(--ease);
      }
      .help:hover::after, .help:focus::after { opacity: 1; transform: translateX(-50%) translateY(0); }
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
        padding: 11px 13px; min-height: 360px;
        white-space: pre-wrap; overflow-wrap: break-word; word-break: break-word; color: var(--text);
      }
      .script-highlight .hook-mark { color: #fff; -webkit-text-fill-color: #fff; font-weight: 700; background: var(--accent-2); border-radius: 2px; box-shadow: 0 0 0 1px var(--ink); }
      .hook-controls { display: flex; gap: 8px; align-items: center; margin-top: 12px; }
      .hook-controls .hook-btn { width: auto; min-width: 0; flex: 0 0 auto; padding: 8px 13px; font-size: 10px; }
      @media (max-width: 820px) { .script-highlight { min-height: 300px; } }
      .action-btns { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 9px; }
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
      .speaker-gallery { display: grid; grid-template-columns: repeat(auto-fill, minmax(72px, 1fr)); gap: 8px; margin-bottom: 10px; max-height: 232px; overflow-y: auto; padding: 2px; }
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
      .otoggles { display: grid; grid-template-columns: repeat(2, minmax(0,1fr)); gap: 11px 22px; }
      .otoggle { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin: 0; font-family: var(--mono); text-transform: none; letter-spacing: 0; font-size: 13.5px; font-weight: 700; color: var(--ink); }
      .otoggle input[type="checkbox"] { margin: 0; }
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
      .scrape-settings { margin-top: 14px; padding-top: 12px; border-top: 1px dashed var(--line-strong); }
      .scrape-lbl { margin-top: 12px; }
      .scrape-lbl:first-child { margin-top: 0; }
      .scrape-terms { min-height: 64px; resize: vertical; }
      .relv-val { color: var(--accent); font-weight: 700; }
      .connect-row { display: flex; gap: 8px; align-items: stretch; }
      .connect-row select { flex: 1; min-width: 0; margin: 0; }
      .connect-row .button { width: auto; min-width: 0; margin: 0; padding: 8px 14px; white-space: nowrap; }
      #connect-status.ok { color: var(--success); }
      #connect-status.bad { color: var(--accent-2); }
      .req-tag { display: inline-block; font-family: var(--mono); font-weight: 700; font-size: 9px; text-transform: uppercase; letter-spacing: .08em; color: #fff; background: var(--accent-2); border-radius: var(--r-sm); padding: 1px 5px; vertical-align: middle; }
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
      textarea { min-height: 360px; resize: vertical; line-height: 1.55; }
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
      @keyframes page-fade { from { opacity: 0; transform: translateY(4px); } to { opacity: 1; transform: none; } }
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
      .log-box { height: calc(min(78vh, 900px) - 70px); max-height: none; overflow: auto; font-size: 13px; }
      .preview-grid { display: grid; grid-template-columns: 1fr; gap: 12px; }
      .preview img { display: block; width: 100%; max-height: 420px; object-fit: contain; background: var(--bg-base); border: 1px solid var(--line); border-radius: var(--r-md); transition: transform .18s ease, filter .18s ease; }
      .preview:hover img { transform: scale(1.012); filter: contrast(1.04) saturate(1.02); }
      .preview strong { display: block; margin-bottom: 8px; }
      .preview-button { width: 100%; padding: 0; background: transparent; border: 0; color: inherit; text-align: left; }
      .preview-button, .preview-button:hover { background: transparent; box-shadow: none; transform: none; }
      .preview-button::after { display: none; }
      .media-sidebar { height: min(78vh, 900px); overflow: auto; }
      .media-sidebar form { display: block; background: transparent; border: 0; padding: 0; box-shadow: none; }
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
      .asset-card { display: grid; gap: 12px; align-content: start; padding: 14px; }
      .asset-card .asset-figure { display: block; padding: 0; margin: 0; border: 0; background: none; box-shadow: none; width: 100%; border-radius: var(--r-md); overflow: hidden; cursor: zoom-in; }
      .asset-card .asset-figure::after { display: none; }
      .asset-thumb { display: block; width: 100%; aspect-ratio: 16 / 10; object-fit: cover; background: var(--bg-base); border: 1px solid var(--line); border-radius: var(--r-md); }
      .asset-body { display: grid; gap: 6px; }
      .asset-body h2 { margin: 0; font-size: 17px; line-height: 1.2; }
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
      .preset-popup-card { max-width: 440px; padding: 22px 22px 20px; }
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
    """


def app_script():
    return """
    <script>
      (function () {
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
          window.setProjectMode("normal");
        };
        window.hideLoadedActions = function () {
          var panel = document.getElementById("loaded-actions-panel");
          if (panel) panel.style.display = "none";
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
              ? '<video src="' + url + '" muted preload="metadata"></video>'
              : item.type === "image"
                ? '<img src="' + url + '" alt="' + name + '">'
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
          function loadProjectMedia(slug) {
            if (!mediaBox) return;
            if (!slug) {
              clearProjectMedia();
              return;
            }
            mediaBox.dataset.slug = slug;
            var mediaHint = document.getElementById("project-media-hint");
            if (mediaHint) mediaHint.style.display = 'none';
            mediaBox.innerHTML = '<div class="hint">Loading media previews...</div>';
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
            // scraping is TikTok-only now; force the (hidden) platforms value clean
            var plat = document.querySelector('[name="scrape_platforms"]');
            if (plat) plat.value = "tiktok";
            var src = hid.value === "scrape" ? "scrape" : "generate";
            Array.prototype.forEach.call(document.querySelectorAll(".csrc-btn"), function (b) {
              b.classList.toggle("csrc-active", b.getAttribute("data-src") === src);
            });
            var box = document.getElementById("scrape-settings");
            if (box) box.style.display = (src === "scrape") ? "block" : "none";
            var rv = document.getElementById("relv-val"); var rng = document.getElementById("script-relevancy");
            if (rv && rng) rv.textContent = rng.value + "%";
          }
          window.syncClipSource = syncClipSource;
          window.setClipSource = function (src) {
            var hid = document.getElementById("clip-source"); if (!hid) return;
            hid.value = (src === "scrape") ? "scrape" : "generate";
            syncClipSource();
            var state = collectFormState();
            if (state) { try { localStorage.setItem(autosaveKey, JSON.stringify(state)); } catch (e) {} sendFormState(state); }
          };
          window.testConnection = function () {
            var sel = document.getElementById("scrape-cookies");
            var file = document.getElementById("scrape-cookies-file");
            var st = document.getElementById("connect-status");
            var btn = document.getElementById("connect-test-btn");
            var cookies = (file && file.value.trim()) || (sel && sel.value) || "";
            if (!cookies) { if (st) { st.className = "hint bad"; st.textContent = "Pick a browser you're signed in to, or give a cookies.txt path."; } return; }
            if (st) { st.className = "hint"; st.textContent = "Testing connection (this can take ~15s)…"; }
            if (btn) btn.disabled = true;
            fetch("/test-connection", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ cookies: cookies }) })
              .then(function (r) { return r.json(); })
              .then(function (d) {
                if (st) {
                  st.className = "hint " + (d && d.ok ? "ok" : "bad");
                  st.textContent = (d && d.message) || (d && d.ok ? "Connected." : "Connection failed.");
                }
              })
              .catch(function () { if (st) { st.className = "hint bad"; st.textContent = "Connection test failed (network)."; } })
              .finally(function () { if (btn) btn.disabled = false; });
          };
          window.applyTier = function (tier) {
            var TIERS = {
              cheap:  { video_model: "ltx-2.3",           image_model: "google/nano-banana-2/text-to-image", reasoning_model: "google/gemini-3.1-pro-preview", tts_model: "flash" },
              medium: { video_model: "seedance-2.0-fast", image_model: "google/nano-banana-2/text-to-image", reasoning_model: "openai/gpt-5.5",            tts_model: "pro" },
              best:   { video_model: "seedance-2.0",      image_model: "openai/gpt-image-2/text-to-image",   reasoning_model: "anthropic/claude-opus-4.8", tts_model: "pro" }
            };
            var preset = TIERS[tier];
            var form = document.getElementById("short-form");
            if (!preset || !form) return;
            Object.keys(preset).forEach(function (name) {
              var field = form.querySelector('[name="' + name + '"]');
              if (field) field.value = preset[name];
            });
            Array.prototype.forEach.call(document.querySelectorAll(".tier-btn"), function (b) { b.classList.remove("tier-active"); });
            var active = document.querySelector('.tier-btn[onclick*="' + tier + '"]');
            if (active) active.classList.add("tier-active");
            var state = collectFormState();
            if (state) {
              try { localStorage.setItem(autosaveKey, JSON.stringify(state)); } catch (e) {}
              sendFormState(state);
            }
            if (typeof setStatus === "function") setStatus(tier.charAt(0).toUpperCase() + tier.slice(1) + " preset loaded.");
          };
          var CUSTOM_PRESET_FIELDS = ["speaker_name","tts_voice","tts_model","video_model","image_model","reasoning_model","speaker_image_path","visual_script","use_visual_direction","enable_speaker_hook","out_web_images","out_wikimedia","out_gpt_images","out_video_clips","out_sfx","out_transition_sfx","out_background_music","out_captions","halt_after_speech","clip_source","scrape_platforms","scrape_terms","script_relevancy","scrape_cookies","scrape_cookies_file"];
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
          var statusLabel = document.getElementById("job-status-label");
          if (statusLabel) {
            statusLabel.textContent = data.status || "";
            statusLabel.className = "status " + (data.klass || "");
          }
          var progress = document.getElementById("job-progress-wrap");
          if (progress && data.progress_html) progress.outerHTML = data.progress_html;
          var log = document.getElementById("job-log");
          if (log && typeof data.log_text === "string") {
            log.textContent = data.log_text;
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
          var trigger = event.target.closest("[data-preview-src]");
          if (!trigger) return;
          var box = ensureLightbox();
          box.querySelector("img").src = trigger.getAttribute("data-preview-src");
          box.querySelector("img").alt = trigger.getAttribute("data-preview-title") || "Preview";
          box.querySelector(".lightbox-title").textContent = trigger.getAttribute("data-preview-title") || "Preview";
          box.classList.add("open");
        });

        window.addEventListener("load", function () {
          try { window.resizeTo(1920, 1080); window.moveTo(0, 0); } catch (e) {}
          stickLogToBottom();
          setupFormAutosave();
          setupProjectLoader();
          setupMediaTabs(document);
          setupReplacementForms(document);
          setupUiSounds();
          playDoneSoundOnce();
          setupJobPolling();
          if (typeof setupToggleSections === "function") setupToggleSections();
          if (typeof syncClipSource === "function") syncClipSource();
          var sform = document.getElementById("short-form");
          if (sform) sform.addEventListener("submit", function (ev) {
            var cs = document.getElementById("clip-source");
            if (!cs || cs.value !== "scrape") return;
            var sel = document.getElementById("scrape-cookies");
            var file = document.getElementById("scrape-cookies-file");
            var connected = (file && file.value.trim()) || (sel && sel.value);
            if (!connected) {
              ev.preventDefault();
              var st = document.getElementById("connect-status");
              if (st) { st.className = "hint bad"; st.textContent = "Connect TikTok before a scrape run — pick the browser you're signed in to, then click Test."; }
              var panel = document.getElementById("scrape-settings");
              if (panel) panel.scrollIntoView({ behavior: "smooth", block: "center" });
              if (typeof setStatus === "function") setStatus("Scrape runs require a TikTok connection.");
            }
          });
        });
        setInterval(stickLogToBottom, 5000);
      })();
    </script>
    """


def page(title, body, refresh=None):
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
    return f"""<!doctype html>
    <html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">{meta}{icons}<title>{esc(title)}</title>{app_style()}</head>
    <body><main>{body}</main>{app_script()}</body></html>""".encode("utf-8")


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

      <div class="create-bar panel">
        <div class="cbar-row">
          <div class="cbar-cell tiers">
            <span class="cbar-cap">Preset {help_tip("Cheap/Medium/Best load model picks. Custom recalls your saved preset. Save stores the current speaker, models, speaker image, visual direction and all toggles as the Custom preset.")}
              <button type="button" class="cbar-save-btn" onclick="saveCustomPreset()" title="Save current settings as the Custom preset" aria-label="Save Custom preset">&#128190;</button></span>
            <div class="tier-btns">
              <button type="button" class="tier-btn" onclick="applyTier('cheap')"><b>Cheap</b><small>~$0.50</small></button>
              <button type="button" class="tier-btn" onclick="applyTier('medium')"><b>Medium</b><small>~$1.50</small></button>
              <button type="button" class="tier-btn" onclick="applyTier('best')"><b>Best</b><small>~$3.50</small></button>
              <button type="button" class="tier-btn" onclick="applyCustomPreset()"><b>Custom</b><small>recall</small></button>
            </div>
          </div>
          <div class="cbar-divider" aria-hidden="true"></div>
          <div class="cbar-cell runtype">
            <span class="cbar-cap">Run type {help_tip("Normal: full agent pass. Smart: checks what media already exists in the project and only generates what is missing.")}</span>
            <input type="hidden" name="run_type" id="run-type" value="{esc(state.get('run_type') or 'normal')}">
            <div class="tier-btns runtype-btns">
              <button type="button" class="tier-btn" data-run="normal" onclick="setRunType('normal')"><b>Normal</b><small>full agent run</small></button>
              <button type="button" class="tier-btn" data-run="audit" onclick="setRunType('audit')"><b>Smart</b><small>fill missing media</small></button>
            </div>
          </div>
        </div>
        <div class="cbar-row cbar-models">
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
          <div class="cbar-cell">
            <span class="cbar-cap">Reasoning model</span>
            <select name="reasoning_model">
              <option value="openai/gpt-5.5"{' selected' if state.get("reasoning_model") == "openai/gpt-5.5" else ""}>GPT-5.5 (fast, standard)</option>
              <option value="google/gemini-3.1-pro-preview"{' selected' if state.get("reasoning_model") == "google/gemini-3.1-pro-preview" else ""}>Gemini 3.1 Pro Preview (cheap)</option>
              <option value="anthropic/claude-opus-4.8"{' selected' if state.get("reasoning_model") == "anthropic/claude-opus-4.8" else ""}>Claude Opus 4.8 (best quality)</option>
            </select>
          </div>
          <button type="submit" class="create-short-btn">&#9889; Create Short</button>
        </div>
        <div class="cbar-row" style="align-items:center; gap:14px;">
          <label class="cbar-halt"><input type="checkbox" name="halt_after_speech"{checked("halt_after_speech")}><span>Halt after generating speech {help_tip("Pause the run right after the voiceover is generated so you can listen and approve or replace it on the run page, then continue.")}</span></label>
        </div>
      </div>

      <section class="stack">
        {advanced_hidden_inputs(state)}
        <div class="panel accent">
          <label>Text script {help_tip("The voiceover is generated from this with Gemini TTS. Mark the opening line(s) as the hook: it is spoken first, then a short pause, then the rest. With a speaker image it drives the InfiniteTalk talking-head opening.")}</label>
          <div class="script-wrap">
            <div class="script-highlight" id="script-highlight" aria-hidden="true"></div>
            <textarea id="script-field" name="script" spellcheck="false" placeholder="Paste your script here. Write it as a punchy spoken narration — the AI generates the voiceover, finds visuals and cuts the Short from this text. Then select your opening line(s) and click &#8220;Mark hook&#8221;.">{esc(state.get("script"))}</textarea>
          </div>
          <input type="hidden" name="hook_text" id="hook-text" value="{esc(state.get('hook_text'))}">
          <div class="hook-controls">
            <button type="button" class="button secondary hook-btn" onclick="markHook()">&#9733; Mark hook</button>
            <button type="button" class="button secondary hook-btn" onclick="clearHook()">Clear</button>
            <span id="hook-indicator" class="hook-dot" hidden></span>
          </div>
        </div>
        <div class="panel toggle-panel" id="visual-panel">
          <div class="panel-head">
            <label>Optional Visual Direction {help_tip("Optional. The Voice Script is authoritative; this is secondary style guidance (e.g. darker documentary look, faster cuts, more maps). If empty, the visual plan is inferred from the script.")}</label>
            <label class="switch" title="Use this visual direction"><input type="checkbox" name="use_visual_direction"{checked("use_visual_direction")} onchange="toggleSection('visual-panel', this.checked)"></label>
          </div>
          <div class="panel-body">
            <textarea class="visual-textarea" name="visual_script" placeholder="Optional. Leave empty to let the agent plan visuals from the script. Use this only for style, e.g. darker documentary style, faster cuts, more maps.">{esc(state.get("visual_script"))}</textarea>
          </div>
        </div>

        <div class="panel">
          <label>Outputs {help_tip("Turn individual parts of the pipeline on or off. Off = that part is skipped entirely, and a Smart run will not generate it either.")}</label>
          <div class="otoggles">
            <label class="otoggle"><span>Web images</span><input type="checkbox" name="out_web_images"{checked("out_web_images")}></label>
            <label class="otoggle"><span>Wikimedia images</span><input type="checkbox" name="out_wikimedia"{checked("out_wikimedia")}></label>
            <label class="otoggle"><span>Generated images</span><input type="checkbox" name="out_gpt_images"{checked("out_gpt_images")}></label>
            <label class="otoggle"><span>Video clips</span><input type="checkbox" name="out_video_clips"{checked("out_video_clips")}></label>
            <label class="otoggle"><span>Sound effects</span><input type="checkbox" name="out_sfx"{checked("out_sfx")}></label>
            <label class="otoggle"><span>Transition SFX</span><input type="checkbox" name="out_transition_sfx"{checked("out_transition_sfx")}></label>
            <label class="otoggle"><span>Background music</span><input type="checkbox" name="out_background_music"{checked("out_background_music")}></label>
            <label class="otoggle"><span>Captions</span><input type="checkbox" name="out_captions"{checked("out_captions")}></label>
          </div>
        </div>

        <div class="panel" id="clipsource-panel">
          <label>Clip source {help_tip("Where the moving footage comes from. Generate = AI video/images (Seedance, GPT-Image). Scrape = download real TikTok/Instagram clips that match a visual style and cut them together (used by the Japanese-facts preset). Scraping ignores the AI generation outputs above.")}</label>
          <input type="hidden" name="clip_source" id="clip-source" value="{esc(state.get('clip_source') or 'generate')}">
          <div class="csrc-btns">
            <button type="button" class="csrc-btn" data-src="generate" onclick="setClipSource('generate')"><b>Generate</b><small>AI video &amp; images</small></button>
            <button type="button" class="csrc-btn" data-src="scrape" onclick="setClipSource('scrape')"><b>Scrape clips</b><small>real TikTok</small></button>
          </div>
          <div id="scrape-settings" class="scrape-settings" style="display:none;">
            <input type="hidden" name="scrape_platforms" value="tiktok">
            <label class="scrape-lbl">Visual style / search terms {help_tip("What kind of clips to look for, independent of the spoken script. Describe the look as TikTok-style hashtags/words (e.g. japan street style, tokyo at night, salaryman commute, kimono). The scraper turns these into TikTok hashtags and downloads matching clips.")}</label>
            <textarea name="scrape_terms" class="scrape-terms" placeholder="e.g. japan street style, tokyo night, salaryman commute, kimono, neon alley">{esc(state.get("scrape_terms"))}</textarea>
            <label class="scrape-lbl">Script relevancy <span id="relv-val" class="relv-val">{esc(state.get('script_relevancy') or '70')}%</span> {help_tip("How tightly downloaded clips must match the spoken script versus pure visual style. High = clips closely follow what's being said. Low = prioritize the look (more b-roll of the vibe), looser tie to the words.")}</label>
            <input type="range" name="script_relevancy" id="script-relevancy" min="0" max="100" step="5" value="{esc(state.get('script_relevancy') or '70')}" oninput="document.getElementById('relv-val').textContent=this.value+'%';">
            <label class="scrape-lbl">Connect TikTok <span class="req-tag">required</span> {help_tip("Scrape runs pull real TikTok clips, which need your logged-in session. Pick the browser where you're signed in to TikTok — the app reads that browser's cookies so yt-dlp downloads as you. No password is handled. For Chrome/Edge, fully close the browser so its cookie file can be read. Note: repurposing creators' clips is against TikTok's ToS/copyright; use responsibly.")}</label>
            <div class="connect-row">
              <select name="scrape_cookies" id="scrape-cookies">
                <option value=""{' selected' if not (state.get('scrape_cookies')) else ''}>Not connected</option>
                <option value="chrome"{' selected' if state.get('scrape_cookies')=='chrome' else ''}>Chrome (signed in to TikTok)</option>
                <option value="edge"{' selected' if state.get('scrape_cookies')=='edge' else ''}>Edge (signed in to TikTok)</option>
                <option value="firefox"{' selected' if state.get('scrape_cookies')=='firefox' else ''}>Firefox (signed in to TikTok)</option>
                <option value="brave"{' selected' if state.get('scrape_cookies')=='brave' else ''}>Brave (signed in to TikTok)</option>
              </select>
              <button type="button" class="button" id="connect-test-btn" onclick="testConnection()">Test</button>
            </div>
            <input type="text" name="scrape_cookies_file" id="scrape-cookies-file" placeholder="…or path to an exported TikTok cookies.txt (optional)" value="{esc(state.get('scrape_cookies_file'))}">
            <div id="connect-status" class="hint" style="margin-top:6px;"></div>
          </div>
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

        <div class="panel">
          <label>Voice &amp; narration {help_tip("Narration is generated from your script with Gemini TTS (English). The speaker name is sent ahead of the script; the voice is force-aligned for frame-accurate word-by-word captions and beat-synced cuts.")}</label>
          <div style="display:flex; gap:10px; flex-wrap:wrap;">
            <input type="text" name="speaker_name" value="{esc(state.get('speaker_name') or 'Narrator')}" placeholder="Speaker name (e.g. Rose)" style="flex:1; min-width:150px;">
            <select name="tts_voice" style="flex:1; min-width:180px;">{voice_options}</select>
            <select name="tts_model" style="flex:1; min-width:180px;">
              <option value="flash"{' selected' if sel_tts_model == 'flash' else ''}>Gemini 2.5 Flash TTS (cheaper)</option>
              <option value="pro"{' selected' if sel_tts_model == 'pro' else ''}>Gemini 2.5 Pro TTS (higher quality)</option>
            </select>
          </div>
          <div class="checks">
            <label><input type="checkbox" name="mix_voice_in_final"{checked("mix_voice_in_final")}> Use generated voice as narration {help_tip("The generated voice becomes the final narration; music and SFX are ducked under it.")}</label>
          </div>
        </div>
        <div class="panel toggle-panel" id="speaker-panel">
          <div class="panel-head">
            <label>Speaker hook clip {help_tip("Optional. Pick a saved speaker or upload a new face. The marked hook is spoken by this person as a lip-synced talking-head opening (InfiniteTalk). If no hook is marked, a Seedance speaker clip is the fallback.")}</label>
            <label class="switch" title="Talking-head hook on/off"><input type="checkbox" name="enable_speaker_hook"{checked("enable_speaker_hook")} onchange="toggleSection('speaker-panel', this.checked)"></label>
          </div>
          <div class="panel-body">
            <input type="hidden" name="speaker_image_path" id="speaker-image-path" value="{esc(state.get('speaker_image_path'))}">
            <input type="file" name="speaker_image_file" id="speaker-file" accept="image/*" onchange="onSpeakerUpload(this)" style="display:none">
            {speaker_gallery_html(state.get('speaker_image_path'))}
          </div>
        </div>
      </section>

      <section class="stack preview-section">
        <div class="loaded-media-panel" style="border: 1px solid var(--line); border-radius: 10px; padding: 22px;">
          <h2 style="margin-bottom: 5px;">&#127916; Project media preview</h2>
          <div class="hint" id="project-media-hint" style="margin-bottom: 18px;">No project loaded. Click &ldquo;Load project&rdquo; in the top bar to browse a past project &mdash; its media appears here.</div>
          <div id="project-media-preview"></div>
        </div>
      </section>
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
      window.setRunType=function(mode){
        var f=document.getElementById('run-type'); if(f) f.value=mode;
        Array.prototype.forEach.call(document.querySelectorAll('.runtype-btns .tier-btn'), function(b){
          b.classList.toggle('run-active', b.getAttribute('data-run')===mode);
        });
      };
      document.addEventListener('DOMContentLoaded', function(){
        var ta=document.getElementById('script-field');
        if(ta){
          ta.addEventListener('input', renderHighlight);
          ta.addEventListener('scroll', function(){ var hl=document.getElementById('script-highlight'); if(hl){ hl.scrollTop=ta.scrollTop; hl.scrollLeft=ta.scrollLeft; } });
        }
        var rt=document.getElementById('run-type'); if(rt){ window.setRunType(rt.value||'normal'); }
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
            <option value="anthropic/claude-opus-4.8" selected>Claude Opus 4.8 (recommended)</option>
            <option value="openai/gpt-5.5">GPT-5.5 (faster)</option>
          </select>
          <div class="hint">The agent detects scene changes, reads the timed transcript, and chooses sound effects from your local <code>soundeffects/</code> library.</div>
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
        if decision == "replace":
            raise RunCancelled("Voice replaced — restarting with a new speaker.")
        with JOB_LOCK:
            job = JOBS.get(job_id)
            if job:
                job["status"] = "running"
                job["logs"].append("Voiceover approved — continuing the run.")
                job.setdefault("log_times", []).append(time.time())
    return gate


def start_job(fields, files):
    job_id = str(int(time.time() * 1000))
    fields = dict(fields)
    audio_path = save_upload(files.get("audio_file"), job_id)
    speaker_image_path = save_upload(files.get("speaker_image_file"), job_id)
    cancel_event = threading.Event()
    replace_lock = threading.Lock()
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
            status_cb("Started.")
            result = sfx_agent.enhance_video_with_sfx(
                video_path, reasoning_model=reasoning_model, status_cb=status_cb
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
    ("Video clips", ("Seedance clip", "Generating missing Seedance", "Waiting for Seedance", "InfiniteTalk", "Speaker hook")),
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
    steps = SFX_STEPS if job_kind == "sfx" else RUN_STEPS
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


def render_progress(status, logs, created_at=None, log_times=None, job_kind=None):
    _percent, activity = progress_state(status, logs)
    elapsed = format_duration(time.time() - float(created_at or time.time()))
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


def media_kind_for_path(project_dir, path, manifest=None):
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
            items.append((media_kind_for_path(project_dir, path, manifest), path))
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


def media_preview_markup(kind, path, replaceable=False, queued=False, input_name="media_path"):
    resolved = str(path.resolve())
    label = "Replace" if replaceable else kind.title()
    if is_video_path(path):
        preview = f'<video src="{link_for(path)}" muted preload="metadata"></video>'
    elif is_image_path(path):
        preview = f'<img src="{link_for(path)}" alt="{esc(path.name)}">'
    elif is_audio_path(path):
        preview = '<div class="media-audio-tile">Audio</div>'
    else:
        preview = '<div class="media-audio-tile">File</div>'
    replace_html = (
        f'<label><input type="checkbox" name="{esc(input_name)}" value="{esc(resolved)}"{" checked" if queued else ""}> {esc(label)}</label>'
        if replaceable
        else f'<label>{esc(label)}</label>'
    )
    return (
        f'<article class="media-tile{" queued" if queued else ""}">'
        f'<span class="media-kind">{esc(kind)}</span>'
        f'<button class="preview-button" type="button" data-preview-src="{link_for(path)}" data-preview-title="{esc(path.name)}">'
        f'{preview}</button>'
        f'{replace_html}'
        f'<small>{esc(path.name)}</small>'
        f'</article>'
    )


def media_tabs_html(items, replaceable_paths=None, queued_paths=None, input_name="media_path"):
    replaceable_paths = replaceable_paths or set()
    queued_paths = queued_paths or set()
    groups = {}
    for kind, path in items:
        key = str(kind or "media")
        groups.setdefault(key, []).append(path)
    if not groups:
        return '<div class="hint">No media files found.</div>'
    order = ["web", "wikimedia", "seedance", "gpt source", "speaker", "render", "review", "local", "web rejected", "web replaced", "media"]
    keys = [key for key in order if key in groups] + sorted([key for key in groups if key not in order])
    tabs = []
    panels = []
    for index, key in enumerate(keys):
        tab_id = re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_") or f"tab_{index}"
        active = " active" if index == 0 else ""
        tabs.append(f'<button class="media-tab{active}" type="button" data-media-tab="{esc(tab_id)}">{esc(key)} <span>{len(groups[key])}</span></button>')
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
                )
            )
        panels.append(
            f'<div class="media-tab-panel" data-media-panel="{esc(tab_id)}"{" hidden" if index else ""}>'
            f'<div class="media-tiny-grid">{"".join(cards)}</div></div>'
        )
    return f'<div class="media-tabs">{"".join(tabs)}</div>' + "".join(panels)


def render_media_replacer(job_id, job):
    # Read-only live gallery. The agent auto-checks each asset for topic relevance
    # and fit, so there are no manual replace/remove controls.
    project_dir = project_dir_for_job(job)
    if not project_dir:
        return '<section class="panel media-sidebar"><h2>Project media</h2><div class="hint">Media appears here once the project folder is created.</div></section>'
    media_all = project_media_files(project_dir)
    if not media_all:
        return '<section class="panel media-sidebar"><h2>Project media</h2><div class="hint">Media appears here as soon as files exist.</div></section>'
    tabs = media_tabs_html(media_all)
    return f"""
    <section class="panel media-sidebar">
      <div class="panel-head"><h2>Project media</h2></div>
      <div class="hint">Live preview of everything the agent gathered and generated. Each asset is auto-reviewed for topic relevance and fit &mdash; no manual swapping needed.</div>
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


def visible_log_text(logs):
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
        ("No audio render", "video_no_audio", False),
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


def project_summary(project_dir):
    report = read_project_report(project_dir)
    video = existing_report_path(report, "video") or latest_media(project_dir / "renders", {".mp4", ".webm"})
    scene_review = existing_report_path(report, "scene_review")
    shot_review = existing_report_path(report, "shot_review")
    web_sheet = existing_report_path(report, "web_contact_sheet")
    gpt_sheet = existing_report_path(report, "gpt_contact_sheet")
    thumb = scene_review or shot_review or web_sheet or gpt_sheet or latest_media(project_dir / "review", {".jpg", ".jpeg", ".png", ".webp"})
    if not thumb:
        thumb = latest_media(project_dir / "gpt images", {".jpg", ".jpeg", ".png", ".webp"}) or latest_media(project_dir / "web images", {".jpg", ".jpeg", ".png", ".webp"})
    created_at = report.get("created_at")
    if not created_at:
        created_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(project_dir.stat().st_mtime))
    return {
        "title": report.get("title") or project_dir.name.replace("_", " ").strip().title(),
        "slug": project_dir.name,
        "created_at": created_at,
        "project_dir": project_dir,
        "video": video,
        "scene_review": scene_review,
        "shot_review": shot_review,
        "web_sheet": web_sheet,
        "gpt_sheet": gpt_sheet,
        "thumb": thumb,
        "renders": project_dir / "renders",
        "review": project_dir / "review",
        "gpt_images": count_media(project_dir / "gpt images", {".jpg", ".jpeg", ".png", ".webp"}),
        "web_images": count_media(project_dir / "web images", {".jpg", ".jpeg", ".png", ".webp"}),
        "seedance": count_media(project_dir / "seedance 2.0", {".mp4", ".webm", ".mov", ".m4v"}),
    }


def asset_card(summary):
    thumb = summary.get("thumb")
    has_video = bool(summary.get("video") and Path(summary["video"]).exists())
    if thumb and Path(thumb).exists() and is_image_path(thumb):
        thumb_html = (
            f'<button class="asset-figure preview-button" type="button" data-preview-src="{link_for(thumb)}" data-preview-title="{esc(summary["title"])}">'
            f'<img class="asset-thumb" src="{link_for(thumb)}" alt="{esc(summary["title"])}"></button>'
        )
    elif has_video:
        thumb_html = f'<div class="asset-figure"><video class="asset-thumb" preload="metadata" muted src="{link_for(summary["video"])}"></video></div>'
    else:
        thumb_html = '<div class="asset-figure"><div class="asset-thumb"></div></div>'

    slug = summary["slug"]
    # "Check results" opens the final video if it exists, otherwise the project folder.
    results_href = view_for(summary["video"], "assets") if has_video else view_for(summary["project_dir"], "assets")
    return f"""
    <article class="panel asset-card">
      {thumb_html}
      <div class="asset-body">
        <h2>{esc(summary["title"])}</h2>
        <div class="asset-sub">{esc(summary["created_at"])}</div>
        <div class="asset-meta">
          <span class="asset-pill">{summary["web_images"]} web</span>
          <span class="asset-pill">{summary["gpt_images"]} GPT</span>
          <span class="asset-pill">{summary["seedance"]} clips</span>
        </div>
      </div>
      <div class="asset-primary">
        <a class="button asset-go" href="{results_href}">&#9654; Check results</a>
        <a class="button secondary" href="/?project={esc(slug)}">&#128194; Load project</a>
      </div>
      {f'<a class="button asset-timeline" href="/timeline?slug={esc(slug)}">&#127902; Open timeline editor</a>' if has_video else '<div class="asset-timeline-locked">&#128274; Timeline opens after first render</div>'}
    </article>
    """


def assets_page():
    projects_dir = agent_core.PROJECTS_DIR
    projects = [p for p in projects_dir.iterdir() if p.is_dir()] if projects_dir.exists() else []
    projects.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    count = len(projects)
    cards = "".join(asset_card(project_summary(project)) for project in projects)
    if not cards:
        cards = '<section class="panel"><h2>No assets yet</h2><div class="hint">Finished runs and generated project folders will appear here.</div></section>'
    body = f"""
    {brand_header()}
    <section class="asset-grid">{cards}</section>
    """
    return page("Asset Library", body)


TIMELINE_SKELETON = """
<div id="timeline-root" data-slug="__SLUG__">
  <div class="tl-toolbar panel">
    <div class="tl-actions">
      <button type="button" class="button secondary tl-save-btn" id="tl-save" title="Save timeline changes">&#128190; Save</button>
      <button type="button" class="button primary tl-render-btn" id="tl-render">&#11015; Render</button>
      <div class="tl-rework-group">
        <button type="button" class="button tl-rework-btn" id="tl-rework">&#129302; Agent rework</button>
        <label class="tl-chk"><input type="checkbox" id="tl-rw-replace"> replace selected media</label>
        <label class="tl-chk"><input type="checkbox" id="tl-rw-recut" checked> reorder &amp; recut</label>
      </div>
    </div>
    <div class="tl-meta">
      <span class="tl-total" id="tl-total"></span>
      <label class="tl-cap-toggle"><span>Captions linked to speech</span><input type="checkbox" id="tl-captions"></label>
    </div>
  </div>
  <div class="tl-grid tl-top">
    <div class="panel tl-player">
      <h2>Preview</h2>
      <div class="tl-stage-view" id="tl-stage-view">
        <img id="tl-pimg" alt="">
        <video id="tl-pvid" muted playsinline></video>
        <div class="tl-stage-empty" id="tl-stage-empty">Press play to preview</div>
      </div>
      <div class="tl-player-bar">
        <button type="button" class="tl-ctrl" id="tl-back" title="Back 5s" aria-label="Back 5 seconds"><svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor"><path d="M6 6h2.2v12H6zM20 6v12L9.5 12z"/></svg></button>
        <button type="button" class="tl-ctrl tl-ctrl-main" id="tl-play" title="Play / Pause" aria-label="Play"><span id="tl-play-ico"><svg viewBox="0 0 24 24" width="22" height="22" fill="currentColor"><path d="M8 5v14l11-7z"/></svg></span></button>
        <button type="button" class="tl-ctrl" id="tl-fwd" title="Forward 5s" aria-label="Forward 5 seconds"><svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor"><path d="M15.8 6H18v12h-2.2zM4 6v12l10.5-6z"/></svg></button>
        <span class="tl-playtime" id="tl-playtime">0:00 / 0:00</span>
      </div>
    </div>
    <div class="panel tl-inspector" id="tl-inspector">
      <h2>Inspector</h2>
      <div class="hint" id="tl-insp-empty">Click a clip, transition or sound effect to edit it.</div>
      <div class="tl-insp-pane" id="tl-insp-clip" hidden>
        <div class="tl-insp-name" id="tl-insp-name"></div>
        <label>Duration (seconds)</label>
        <input type="number" id="tl-insp-dur" min="0.5" max="20" step="0.1">
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
        <div class="hint" id="tl-fx-note"></div>
      </div>
    </div>
  </div>
  <div class="panel tl-stage">
    <div class="tl-rows">
      <div class="tl-row-labels">
        <div class="tl-rlabel tl-rl-ruler"></div>
        <div class="tl-rlabel tl-rl-clips">Clips</div>
        <div class="tl-rlabel tl-rl-cap">Captions</div>
        <div class="tl-rlabel tl-rl-voice">Voice</div>
        <div class="tl-rlabel tl-rl-tr">Transitions</div>
        <div class="tl-rlabel tl-rl-sfx">Sound&nbsp;FX</div>
      </div>
      <div class="tl-scroll" id="tl-scroll">
        <div class="tl-playhead" id="tl-playhead"></div>
        <div class="tl-ruler" id="tl-ruler"></div>
        <div class="tl-track tl-clips-track" id="tl-clips"></div>
        <div class="tl-track tl-cap" id="tl-captrack"></div>
        <div class="tl-track tl-aud" id="tl-voice"></div>
        <div class="tl-track tl-trtrack" id="tl-trtrack"></div>
        <div class="tl-track tl-sfx" id="tl-sfx"></div>
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
    <div class="tl-lib-body" id="tl-lib-media"><div class="tl-lib-loading">Loading media…</div></div>
    <div class="tl-lib-body" id="tl-lib-sfx" hidden><div class="tl-lib-loading">Loading sounds…</div></div>
  </div>
</div>
"""

TIMELINE_ASSETS = """
<style>
  .tl-toolbar { display:flex; align-items:center; justify-content:space-between; gap:16px; flex-wrap:wrap; margin-bottom:16px; }
  .tl-actions { display:flex; align-items:center; gap:10px; flex-wrap:wrap; }
  /* scope width:auto under .tl-actions so it beats the global button{width:100%} */
  .tl-actions .button, .tl-actions .tl-render-btn, .tl-actions .tl-save-btn, .tl-actions .tl-rework-btn { width:auto; min-width:0; }
  .tl-render-btn { font-size:13.5px; padding:9px 16px; }
  .tl-save-btn { font-size:13.5px; padding:9px 14px; }
  .tl-save-btn.tl-unsaved { border-color:var(--accent); box-shadow:inset 0 0 0 1px var(--accent); color:var(--text); }
  .tl-rework-group { display:flex; align-items:center; gap:8px 12px; flex-wrap:wrap; padding-left:12px; margin-left:4px; border-left:1px solid var(--line-strong); }
  .tl-rework-btn { font-size:13px; padding:9px 14px; }
  .tl-chk { display:inline-flex; align-items:center; gap:6px; font-size:12px; font-weight:600; color:#c9d2cf; margin:0; cursor:pointer; }
  .tl-chk input { margin:0; }
  .tl-meta { display:flex; align-items:center; gap:16px; }
  .tl-total { font-weight:700; color:var(--muted); font-variant-numeric:tabular-nums; }
  .tl-cap-toggle { display:flex; align-items:center; gap:8px; font-weight:700; margin:0; cursor:pointer; }
  .tl-cap-toggle input { margin:0; }
  .tl-grid { display:grid; grid-template-columns:auto 1fr; gap:16px; align-items:start; }
  .tl-top { margin-bottom:16px; }
  .tl-player { display:flex; flex-direction:column; align-items:center; }
  .tl-player h2 { align-self:flex-start; }
  .tl-stage-view { position:relative; aspect-ratio:9/16; height:min(58vh,540px); width:auto; max-width:100%; background:#000; border:1px solid var(--line); border-radius:10px; overflow:hidden; display:flex; align-items:center; justify-content:center; }
  .tl-stage-view img, .tl-stage-view video { width:100%; height:100%; object-fit:cover; display:none; background:#000; }
  .tl-stage-empty { position:absolute; color:var(--faint); font-weight:600; }
  .tl-player-bar { display:flex; align-items:center; justify-content:center; gap:14px; margin-top:14px; }
  .tl-player-bar .tl-ctrl { width:42px; height:42px; min-width:0; padding:0; border-radius:50%; display:inline-flex; align-items:center; justify-content:center; color:var(--text); background:var(--bg-overlay); border:1px solid var(--line-strong); box-shadow:none; }
  .tl-ctrl::after { display:none; }
  .tl-player-bar .tl-ctrl:hover { background:var(--bg-raised); border-color:var(--accent); transform:translateY(-1px); box-shadow:none; }
  .tl-player-bar .tl-ctrl-main { width:54px; height:54px; color:#fff; background:linear-gradient(180deg,var(--accent-hover),var(--accent)); border-color:var(--accent-active); }
  .tl-ctrl-main:hover { background:linear-gradient(180deg,#a18dff,var(--accent-hover)); border-color:var(--accent-hover); }
  .tl-playtime { font-weight:700; color:var(--muted); font-variant-numeric:tabular-nums; margin-left:6px; }
  .tl-stage { overflow:hidden; }
  .tl-rows { display:flex; gap:10px; }
  .tl-row-labels { display:flex; flex-direction:column; gap:6px; flex:0 0 auto; }
  .tl-rlabel { display:flex; align-items:center; font-weight:600; color:var(--faint); font-size:11px; text-transform:uppercase; letter-spacing:.5px; }
  .tl-rl-ruler { height:26px; }
  .tl-rl-clips { height:96px; }
  .tl-rl-cap, .tl-rl-voice, .tl-rl-tr, .tl-rl-sfx { height:40px; }
  .tl-scroll { position:relative; overflow-x:auto; flex:1; min-width:0; padding-bottom:12px; user-select:none; -webkit-user-select:none; }
  .tl-ruler { position:relative; height:26px; cursor:pointer; }
  .tl-tick { position:absolute; top:0; height:26px; border-left:1px solid rgba(255,255,255,.2); padding-left:5px; font-size:10px; color:rgba(255,255,255,.6); }
  .tl-playhead { position:absolute; top:0; bottom:12px; width:2px; background:#ff5d5d; z-index:8; pointer-events:none; box-shadow:0 0 6px rgba(255,93,93,.8); }
  .tl-playhead::before { content:''; position:absolute; top:0; left:-5px; border-left:6px solid transparent; border-right:6px solid transparent; border-top:8px solid #ff5d5d; }
  .tl-track { position:relative; margin-top:6px; background:var(--bg-base); border:1px solid var(--line); border-radius:var(--r-md); }
  .tl-clips-track { height:96px; }
  .tl-cap, .tl-aud, .tl-trtrack, .tl-sfx { height:40px; }
  .tl-clip { position:absolute; top:4px; bottom:4px; border:2px solid var(--line-strong); border-radius:var(--r-md); overflow:hidden; cursor:grab; background:var(--bg-overlay); background-size:cover; background-position:center; touch-action:none; box-shadow:var(--sh-1); }
  .tl-clip.selected { border-color:var(--accent); box-shadow:0 0 0 2px var(--accent-subtle); z-index:4; }
  .tl-clip.dragging { opacity:.8; cursor:grabbing; z-index:9; }
  .tl-clip .tl-badge { position:absolute; top:5px; left:6px; font-size:11px; font-weight:900; color:#fff; text-shadow:0 1px 3px #000; background:rgba(0,0,0,.45); padding:1px 6px; border-radius:5px; }
  .tl-clip .tl-clip-label { position:absolute; left:0; right:0; bottom:0; padding:4px 8px; font-size:11px; font-weight:800; color:#fff; background:linear-gradient(transparent, rgba(0,0,0,.85)); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .tl-clip .tl-handle { position:absolute; top:0; right:0; bottom:0; width:13px; cursor:ew-resize; background:linear-gradient(90deg, transparent, rgba(124,92,255,.6)); }
  .tl-seam { position:absolute; top:0; bottom:0; width:0; border-left:2px dashed rgba(124,92,255,.4); z-index:3; pointer-events:none; }
  .tl-trans { position:absolute; transform:translate(-50%,-50%); z-index:6; width:24px; height:24px; cursor:pointer; display:flex; align-items:center; justify-content:center; touch-action:none; }
  .tl-trans .tl-diamond { width:16px; height:16px; transform:rotate(45deg); background:var(--accent-2); border:1px solid #fff; border-radius:4px; box-shadow:0 0 0 3px var(--bg-base); transition:transform .12s ease; }
  .tl-trans.selected .tl-diamond { background:var(--accent); border-color:#fff; }
  .tl-trans.disabled .tl-diamond { background:var(--faint); border-color:var(--line-strong); }
  .tl-trans:hover .tl-diamond { transform:rotate(45deg) scale(1.18); }
  .tl-capbar { position:absolute; top:7px; bottom:7px; border-radius:7px; background:var(--accent-subtle); border:1px solid rgba(124,92,255,.4); display:flex; align-items:center; padding-left:9px; color:var(--text); font-size:11px; font-weight:600; white-space:nowrap; overflow:hidden; }
  .tl-capbar.off { opacity:.3; }
  .tl-audbar { position:absolute; top:7px; bottom:7px; left:0; right:0; border-radius:7px; background:rgba(111,211,255,.12); border:1px solid rgba(111,211,255,.3); }
  .tl-fx { position:absolute; top:6px; bottom:6px; border-radius:7px; background:var(--bg-overlay); border:1px solid var(--line-strong); cursor:pointer; display:flex; align-items:center; padding:0 8px; font-size:11px; font-weight:600; color:var(--text); white-space:nowrap; overflow:hidden; box-sizing:border-box; }
  .tl-fx.selected { border-color:var(--accent); box-shadow:0 0 0 2px var(--accent-subtle); z-index:4; }
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
  .tl-lib-body { display:block; max-height:380px; overflow-y:auto; padding:2px; }
  .tl-sublib-tabs { display:flex; gap:6px; flex-wrap:wrap; margin-bottom:10px; position:sticky; top:0; background:var(--bg-raised); padding-bottom:6px; z-index:2; }
  .tl-sublib-tab { width:auto; min-width:0; padding:5px 10px; font-family:var(--pixel); font-size:9px; text-transform:uppercase; background:var(--bg-input); border:2px solid var(--ink); color:var(--ink); border-radius:var(--r-sm); box-shadow:none; }
  .tl-sublib-tab::after { display:none; }
  .tl-sublib-tab.active { background:var(--accent); color:#fff; }
  .tl-sublib-grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(118px,1fr)); gap:10px; }
  .tl-sfx-search { width:100%; margin-bottom:10px; position:sticky; top:0; }
  .tl-lib-body[hidden] { display:none; }
  .tl-lib-loading { color:var(--faint); font-size:13px; }
  .tl-lib-item { position:relative; border:1px solid var(--line); border-radius:var(--r-md); overflow:hidden; background:var(--bg-input); cursor:grab; }
  .tl-lib-item:hover { border-color:var(--accent); }
  .tl-lib-item.dragging { opacity:.5; }
  .tl-lib-item img, .tl-lib-item video { display:block; width:100%; aspect-ratio:9/14; object-fit:cover; background:var(--bg-base); pointer-events:none; }
  .tl-lib-item .tl-lib-name { padding:4px 7px; font-size:11px; color:var(--muted); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .tl-lib-sound { display:flex; align-items:center; gap:8px; padding:9px 11px; }
  .tl-lib-sound .tl-lib-ico { flex:0 0 auto; color:var(--accent); }
  .tl-track.drop-ok { outline:2px dashed var(--accent); outline-offset:-2px; background:var(--accent-subtle); }
  @media (max-width:760px){ .tl-grid{ grid-template-columns:1fr; } .tl-stage-view{ height:auto; width:100%; max-width:300px; } }
</style>
<script>
(function(){
  var rootEl = document.getElementById('timeline-root');
  if (!rootEl) return;
  var model;
  try { model = JSON.parse(document.getElementById('timeline-model').textContent); } catch(e){ return; }
  var slug = rootEl.getAttribute('data-slug');
  var scenes = (model.scenes||[]).map(function(s){ return Object.assign({}, s); });
  var transitions = (model.transitions||[]).map(function(t){ return Object.assign({}, t); });
  var sfx = (model.sfx||[]).map(function(s){ return Object.assign({}, s); });
  var volumes = Object.assign({voice:1, music:0}, model.volumes||{});
  var captionsOn = !!model.captions;
  var SCALE = 70;
  var sel = null;
  var markedReplace = {};   // scene id -> true when its media is marked for agent replacement
  var dirty = false;        // unsaved edits
  function markDirty(){ dirty = true; var b=document.getElementById('tl-save'); if(b){ b.classList.add('tl-unsaved'); } }

  var clipsEl=document.getElementById('tl-clips'), capEl=document.getElementById('tl-captrack'),
      voiceEl=document.getElementById('tl-voice'), trEl=document.getElementById('tl-trtrack'),
      sfxEl=document.getElementById('tl-sfx'), ruler=document.getElementById('tl-ruler'),
      playhead=document.getElementById('tl-playhead');

  function esc(t){ return String(t==null?'':t).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }
  function visible(){ return scenes.filter(function(s){ return !s.removed; }); }
  function totalDur(){ return visible().reduce(function(a,s){ return a+s.dur; },0); }
  function fmt(t){ t=Math.max(0,t); var m=Math.floor(t/60), s=Math.floor(t%60); return m+':'+(s<10?'0':'')+s; }
  function startOf(id){ var vis=visible(), acc=0; for(var i=0;i<vis.length;i++){ if(vis[i].id===id) return acc; acc+=vis[i].dur; } return null; }

  function layout(){
    var vis=visible(), total=totalDur(), width=Math.max(720, total*SCALE);
    [clipsEl,capEl,voiceEl,trEl,sfxEl,ruler].forEach(function(el){ if(el) el.style.width=width+'px'; });
    ruler.innerHTML='';
    var step = total>40?10:5;
    for(var t=0;t<=total+0.01;t+=step){ var d=document.createElement('div'); d.className='tl-tick'; d.style.left=(t*SCALE)+'px'; d.textContent=fmt(t); ruler.appendChild(d); }
    clipsEl.innerHTML='';
    var x=0;
    vis.forEach(function(s, i){
      var w=s.dur*SCALE;
      var b=document.createElement('div');
      b.className='tl-clip'+(sel&&sel.type==='clip'&&sel.id===s.id?' selected':'')+(markedReplace[s.id]?' marked-replace':'');
      b.style.left=x+'px'; b.style.width=w+'px'; b.setAttribute('data-id', s.id);
      if(s.poster) b.style.backgroundImage='url('+s.poster+')';
      var badge=(s.clip?'\\u25B6 ':'')+(s.speaker?'\\uD83C\\uDFA4 ':'');
      var repTag=markedReplace[s.id]?'<span class="tl-replace-tag">REPLACE</span>':'';
      var markCls='tl-clip-mark'+(markedReplace[s.id]?' on':'');
      b.innerHTML='<span class="'+markCls+'" title="Mark this media for the agent to replace"></span><span class="tl-badge">'+badge+s.dur.toFixed(1)+'s</span>'+repTag+'<span class="tl-clip-label">'+esc(s.label)+'</span><span class="tl-handle"></span>';
      (function(scn){ b.querySelector('.tl-clip-mark').addEventListener('pointerdown', function(ev){ ev.stopPropagation(); ev.preventDefault(); if(markedReplace[scn.id]) delete markedReplace[scn.id]; else markedReplace[scn.id]=true; layout(); markDirty(); }); })(s);
      b.querySelector('.tl-handle').addEventListener('pointerdown', function(ev){ ev.stopPropagation(); startResize(ev, s); });
      b.addEventListener('pointerdown', function(ev){ if(ev.target.classList.contains('tl-handle')||ev.target.classList.contains('tl-clip-mark')) return; startClipDrag(ev, s, b); });
      clipsEl.appendChild(b);
      if(i>0){ var seam=document.createElement('div'); seam.className='tl-seam'; seam.style.left=x+'px'; clipsEl.appendChild(seam); }
      x+=w;
    });
    trEl.innerHTML='';
    vis.forEach(function(s, i){
      if(i===0) return;
      var tr=transitions.filter(function(t){ return t.scene_id===s.id; })[0];
      if(!tr) return;
      var bx=startOf(s.id)*SCALE;
      var node=document.createElement('div');
      node.className='tl-trans'+(sel&&sel.type==='trans'&&sel.id===tr.id?' selected':'')+(tr.enabled===false?' disabled':'');
      node.style.left=bx+'px'; node.style.top='50%'; node.title='Transition between clips';
      node.innerHTML='<span class="tl-diamond"></span>';
      node.addEventListener('pointerdown', function(ev){ ev.stopPropagation(); selectTrans(tr.id); });
      trEl.appendChild(node);
    });
    sfxEl.innerHTML='';
    sfx.forEach(function(fx){
      var st=startOf(fx.scene_id);
      if(st===null) return;
      var fxx=(st+(fx.offset||0))*SCALE;
      var node=document.createElement('div');
      node.className='tl-fx'+(sel&&sel.type==='fx'&&sel.id===fx.id?' selected':'')+(fx.enabled===false?' disabled':'');
      node.style.left=fxx+'px'; node.style.width=Math.max(60,(fx.duration||0.5)*SCALE)+'px';
      node.innerHTML='\\u266A '+esc(fx.label);
      node.addEventListener('pointerdown', function(ev){ ev.stopPropagation(); selectFx(fx.id); });
      sfxEl.appendChild(node);
    });
    capEl.innerHTML = total>0 ? ('<div class="tl-capbar'+(captionsOn?'':' off')+'" style="left:0;width:'+(total*SCALE)+'px">Captions '+(captionsOn?'on':'off')+'</div>') : '';
    voiceEl.innerHTML='<div class="tl-audbar"></div>';
    document.getElementById('tl-total').textContent='Total '+fmt(total)+'  -  '+vis.length+' clips';
    updatePlayhead();
  }

  function startResize(ev, s){
    ev.preventDefault();
    var startX=ev.clientX, startDur=s.dur;
    function move(e){ var dd=(e.clientX-startX)/SCALE; s.dur=Math.max(0.5, Math.min(20, +(startDur+dd).toFixed(1))); layout(); if(sel&&sel.type==='clip'&&sel.id===s.id) syncInspector(); }
    function up(){ document.removeEventListener('pointermove',move); document.removeEventListener('pointerup',up); }
    document.addEventListener('pointermove',move); document.addEventListener('pointerup',up);
  }

  function startClipDrag(ev, s, block){
    ev.preventDefault();
    var startX=ev.clientX, dragging=false;
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
    function up(){
      document.removeEventListener('pointermove',move); document.removeEventListener('pointerup',up);
      var b=clipsEl.querySelector('.tl-clip[data-id="'+s.id+'"]'); if(b) b.classList.remove('dragging');
      if(!dragging) selectClip(s.id);
    }
    document.addEventListener('pointermove',move); document.addEventListener('pointerup',up);
  }

  function showPane(which){
    document.getElementById('tl-insp-empty').hidden = !!which;
    document.getElementById('tl-insp-clip').hidden = which!=='clip';
    document.getElementById('tl-insp-fx').hidden = which!=='fx';
  }
  function selectClip(id){ sel={type:'clip',id:id}; layout(); syncInspector(); }
  function selectTrans(id){ sel={type:'trans',id:id}; layout(); syncFx(); }
  function selectFx(id){ sel={type:'fx',id:id}; layout(); syncFx(); }

  function syncInspector(){
    var s=scenes.filter(function(x){return x.id===sel.id;})[0];
    if(!s||s.removed){ sel=null; showPane(null); return; }
    showPane('clip');
    document.getElementById('tl-insp-name').textContent=s.label;
    document.getElementById('tl-insp-dur').value=s.dur;
  }
  function currentFx(){
    if(!sel) return null;
    if(sel.type==='trans') return transitions.filter(function(t){return t.id===sel.id;})[0];
    if(sel.type==='fx') return sfx.filter(function(f){return f.id===sel.id;})[0];
    return null;
  }
  function syncFx(){
    var fx=currentFx();
    if(!fx){ sel=null; showPane(null); return; }
    showPane('fx');
    document.getElementById('tl-fx-name').textContent=(sel.type==='trans'?'Transition: ':'Sound: ')+(fx.label||'');
    document.getElementById('tl-fx-enabled').checked = fx.enabled!==false;
    var v=fx.volume||0; document.getElementById('tl-fx-vol').value=v;
    document.getElementById('tl-fx-vol-val').textContent=Math.round(v*100)+'%';
    document.getElementById('tl-fx-note').textContent='';
  }

  document.getElementById('tl-insp-dur').addEventListener('input', function(){ var s=scenes.filter(function(x){return x.id===(sel&&sel.id);})[0]; if(s){ s.dur=Math.max(0.5,Math.min(20, parseFloat(this.value)||s.dur)); layout(); markDirty(); } });
  document.getElementById('tl-insp-remove').addEventListener('click', function(){ var s=scenes.filter(function(x){return x.id===(sel&&sel.id);})[0]; if(s){ s.removed=true; sel=null; layout(); showPane(null); markDirty(); } });
  document.getElementById('tl-fx-enabled').addEventListener('change', function(){ var fx=currentFx(); if(fx){ fx.enabled=this.checked; layout(); markDirty(); } });
  document.getElementById('tl-fx-vol').addEventListener('input', function(){ var fx=currentFx(); if(fx){ fx.volume=parseFloat(this.value); document.getElementById('tl-fx-vol-val').textContent=Math.round(fx.volume*100)+'%'; layout(); markDirty(); } });

  var pimg=document.getElementById('tl-pimg'), pvid=document.getElementById('tl-pvid'), pempty=document.getElementById('tl-stage-empty');
  var playing=false, clock=0, lastTs=0, curIdx=-1;
  function sceneAt(t){ var vis=visible(), acc=0; for(var i=0;i<vis.length;i++){ if(t < acc+vis[i].dur){ return {scene:vis[i], idx:i}; } acc+=vis[i].dur; } return vis.length? {scene:vis[vis.length-1], idx:vis.length-1} : null; }
  function showScene(info){
    if(!info){ pimg.style.display='none'; pvid.style.display='none'; pempty.style.display='block'; return; }
    pempty.style.display='none';
    if(curIdx===info.idx) return;
    curIdx=info.idx; var s=info.scene;
    if(s.clip){ pimg.style.display='none'; pvid.style.display='block'; try{ pvid.src=s.clip; pvid.currentTime=0; if(playing) pvid.play().catch(function(){}); }catch(e){} }
    else { pvid.pause(); pvid.style.display='none'; pimg.style.display='block'; pimg.src=s.poster||''; }
  }
  function updatePlayhead(){ playhead.style.left=(clock*SCALE)+'px'; document.getElementById('tl-playtime').textContent=fmt(clock)+' / '+fmt(totalDur()); }
  function tick(ts){
    if(!playing) return;
    var dt=(ts-lastTs)/1000; lastTs=ts; clock+=dt;
    var total=totalDur();
    if(clock>=total){ clock=total; updatePlayhead(); stop(); return; }
    showScene(sceneAt(clock)); updatePlayhead(); requestAnimationFrame(tick);
  }
  var PLAY_ICO='<svg viewBox="0 0 24 24" width="22" height="22" fill="currentColor"><path d="M8 5v14l11-7z"/></svg>';
  var PAUSE_ICO='<svg viewBox="0 0 24 24" width="22" height="22" fill="currentColor"><path d="M6 5h4v14H6zM14 5h4v14h-4z"/></svg>';
  function setPlayIcon(p){ var el=document.getElementById('tl-play-ico'); if(el) el.innerHTML=p?PAUSE_ICO:PLAY_ICO; }
  function play(){ if(playing) return; if(clock>=totalDur()-0.05){ clock=0; curIdx=-1; } playing=true; lastTs=performance.now(); setPlayIcon(true); showScene(sceneAt(clock)); requestAnimationFrame(tick); }
  function stop(){ playing=false; pvid.pause(); setPlayIcon(false); }
  function seekBy(d){ clock=Math.max(0,Math.min(totalDur(), clock+d)); curIdx=-1; showScene(sceneAt(clock)); updatePlayhead(); }
  document.getElementById('tl-play').addEventListener('click', function(){ if(playing) stop(); else play(); });
  document.getElementById('tl-back').addEventListener('click', function(){ seekBy(-5); });
  document.getElementById('tl-fwd').addEventListener('click', function(){ seekBy(5); });
  ruler.addEventListener('pointerdown', function(e){ var rect=ruler.getBoundingClientRect(); clock=Math.max(0,Math.min(totalDur(),(e.clientX-rect.left+ruler.scrollLeft)/SCALE)); curIdx=-1; showScene(sceneAt(clock)); updatePlayhead(); });

  function bindVol(id, key, out){
    var el=document.getElementById(id), o=document.getElementById(out);
    el.value=volumes[key]; o.textContent=Math.round(volumes[key]*100)+'%';
    el.addEventListener('input', function(){ volumes[key]=parseFloat(this.value); o.textContent=Math.round(volumes[key]*100)+'%'; markDirty(); });
  }
  bindVol('tl-voice-vol','voice','tl-v-voice');
  bindVol('tl-music-vol','music','tl-v-music');
  var capToggle=document.getElementById('tl-captions');
  capToggle.checked=captionsOn;
  capToggle.addEventListener('change', function(){ captionsOn=this.checked; layout(); markDirty(); });

  function collectEdits(){
    var vis=visible();
    return {
      scenes: vis.map(function(s){return {id:s.id, duration:s.dur};}),
      order: vis.map(function(s){return s.id;}),
      removed: scenes.filter(function(s){return s.removed;}).map(function(s){return s.id;}),
      added: scenes.filter(function(s){return s.added;}).map(function(s){return {id:s.id, kind:s.kind, path:s.path, clip:s.clip, poster:s.poster, dur:s.dur, after:s.id};}),
      replace: Object.keys(markedReplace),
      volumes: volumes,
      captions: captionsOn,
      transitions: transitions.map(function(t){return {id:t.id, volume:t.volume, enabled:t.enabled};}),
      sfx: sfx.map(function(f){return {id:f.id, volume:f.volume, enabled:f.enabled, added:!!f.added, path:f.path, scene_id:f.scene_id, offset:f.offset};})
    };
  }

  document.getElementById('tl-save').addEventListener('click', function(){
    var btn=this; btn.disabled=true;
    fetch('/timeline-save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({slug:slug, edits:collectEdits()})})
      .then(function(r){return r.json();})
      .then(function(d){ btn.disabled=false; if(d&&d.ok){ dirty=false; btn.classList.remove('tl-unsaved'); btn.innerHTML='\\u2713 Saved'; setTimeout(function(){ btn.innerHTML='\\uD83D\\uDCBE Save'; },1400); } else { alert((d&&d.error)||'Could not save.'); } })
      .catch(function(){ btn.disabled=false; alert('Could not save.'); });
  });

  document.getElementById('tl-render').addEventListener('click', function(){
    var btn=this; btn.disabled=true; var old=btn.innerHTML; btn.textContent='Starting…';
    fetch('/timeline-render',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({slug:slug, edits:collectEdits()})})
      .then(function(r){return r.json();})
      .then(function(d){ if(d&&d.ok&&d.job){ window.location.href=d.job; } else { btn.disabled=false; btn.innerHTML=old; alert((d&&d.error)||'Could not start render.'); } })
      .catch(function(){ btn.disabled=false; btn.innerHTML=old; alert('Could not start render.'); });
  });

  document.getElementById('tl-rework').addEventListener('click', function(){
    var doReplace=document.getElementById('tl-rw-replace').checked;
    var doRecut=document.getElementById('tl-rw-recut').checked;
    if(!doReplace && !doRecut){ alert('Pick at least one rework option.'); return; }
    if(doReplace && !Object.keys(markedReplace).length){ alert('Mark at least one clip\\'s media to replace (select a clip, then tick \"Mark this clip\\'s media to be replaced\").'); return; }
    var btn=this; btn.disabled=true; btn.textContent='Starting…';
    fetch('/timeline-rework',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({slug:slug, replace_media:doReplace, reorder_recut:doRecut, replace_ids:Object.keys(markedReplace), edits:collectEdits()})})
      .then(function(r){return r.json();})
      .then(function(d){ if(d&&d.ok&&d.job){ window.location.href=d.job; } else { btn.disabled=false; btn.innerHTML='\\uD83E\\uDD16 Agent rework'; alert((d&&d.error)||'Could not start rework.'); } })
      .catch(function(){ btn.disabled=false; btn.innerHTML='\\uD83E\\uDD16 Agent rework'; alert('Could not start rework.'); });
  });

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
      renderMediaLib(document.getElementById('tl-lib-media'), (d&&d.media)||[]);
      renderSfxLib(document.getElementById('tl-lib-sfx'), (d&&d.sfx)||[]);
    }).catch(function(){
      document.getElementById('tl-lib-media').innerHTML='<div class="tl-lib-loading">Could not load media.</div>';
      document.getElementById('tl-lib-sfx').innerHTML='<div class="tl-lib-loading">Could not load sounds.</div>';
    });
  }
  function libItemEl(it, kind){
    var el=document.createElement('div'); el.className='tl-lib-item'; el.draggable=true;
    if(kind==='media'){
      var media = it.type==='video' ? '<video src="'+esc(it.url)+'" muted preload="metadata"></video>' : '<img src="'+esc(it.url)+'" alt="">';
      el.innerHTML=media+'<div class="tl-lib-name">'+esc(it.name)+'</div>';
    } else {
      el.classList.add('tl-lib-sound');
      el.innerHTML='<span class="tl-lib-ico"><svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2"><path d="M11 5 6 9H3v6h3l5 4z"/><path d="M16 9a4 4 0 0 1 0 6"/></svg></span><div class="tl-lib-name">'+esc(it.name)+'</div>';
    }
    el.addEventListener('dragstart', function(e){ el.classList.add('dragging'); e.dataTransfer.setData('text/plain', JSON.stringify({kind:kind, item:it})); e.dataTransfer.effectAllowed='copy'; });
    el.addEventListener('dragend', function(){ el.classList.remove('dragging'); });
    return el;
  }
  function renderMediaLib(box, items){
    if(!box) return;
    if(!items.length){ box.innerHTML='<div class="tl-lib-loading">No project media.</div>'; return; }
    var groups={}, order=['web','wikimedia','gpt source','seedance','speaker','local','media'];
    var labelMap={'web':'Web images','wikimedia':'Wikimedia','gpt source':'GPT','seedance':'Clips','speaker':'Speaker','local':'Local','media':'Other'};
    items.forEach(function(it){ var k=it.kind||'media'; (groups[k]=groups[k]||[]).push(it); });
    var keys=order.filter(function(k){return groups[k];}).concat(Object.keys(groups).filter(function(k){return order.indexOf(k)===-1;}));
    box.innerHTML='<div class="tl-sublib-tabs"></div><div class="tl-sublib-grid"></div>';
    var tabsEl=box.querySelector('.tl-sublib-tabs'), gridEl=box.querySelector('.tl-sublib-grid');
    function show(k){
      Array.prototype.forEach.call(tabsEl.children,function(t){ t.classList.toggle('active', t.getAttribute('data-k')===k); });
      gridEl.innerHTML=''; (groups[k]||[]).forEach(function(it){ gridEl.appendChild(libItemEl(it,'media')); });
    }
    keys.forEach(function(k,i){ var t=document.createElement('button'); t.type='button'; t.className='tl-sublib-tab'+(i?'':' active'); t.setAttribute('data-k',k); t.textContent=(labelMap[k]||k)+' ('+groups[k].length+')'; t.addEventListener('click',function(){show(k);}); tabsEl.appendChild(t); });
    show(keys[0]);
  }
  function renderSfxLib(box, items){
    if(!box) return;
    box.innerHTML='<input type="text" class="tl-sfx-search" placeholder="Search sounds\\u2026"><div class="tl-sublib-grid" id="tl-sfx-grid"></div>';
    var grid=box.querySelector('#tl-sfx-grid'), search=box.querySelector('.tl-sfx-search');
    function draw(q){ grid.innerHTML=''; q=(q||'').toLowerCase().trim(); var n=0; items.forEach(function(it){ if(q && (it.name||'').toLowerCase().indexOf(q)===-1 && (it.category||'').toLowerCase().indexOf(q)===-1) return; grid.appendChild(libItemEl(it,'sfx')); n++; }); if(!n) grid.innerHTML='<div class="tl-lib-loading">No matching sounds.</div>'; }
    search.addEventListener('input', function(){ draw(this.value); });
    draw('');
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
    var ns={ id:'add-'+Date.now(), label:it.name, dur:Math.max(1.5, it.type==='video'?3.0:2.5), poster:it.poster||it.url, clip:it.type==='video'?it.url:'', path:it.path, kind:it.type==='video'?'clip':'image', added:true, replaceable:false, speaker:false };
    var order=scenes.filter(function(x){return !x.removed;});
    order.splice(idx,0,ns);
    scenes=order.concat(scenes.filter(function(x){return x.removed;}));
    layout(); markDirty(); selectClip(ns.id);
  });
  enableDrop(sfxEl, 'sfx', function(it, atSec){
    var vis=visible(), best=vis.length?vis[0].id:null, acc=0;
    for(var i=0;i<vis.length;i++){ if(atSec < acc+vis[i].dur){ best=vis[i].id; break; } acc+=vis[i].dur; }
    if(best===null){ return; }
    sfx.push({ id:'sfx-'+Date.now(), scene_id:best, label:it.name, category:it.category||'custom', offset:0, duration:it.duration||1.0, volume:0.25, enabled:true, added:true, path:it.path });
    layout(); markDirty();
  });
  setupLibrary();

  window.addEventListener('beforeunload', function(e){ if(dirty){ e.preventDefault(); e.returnValue=''; } });
  layout(); updatePlayhead();
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
    return poster_url, clip_url, img


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


def global_sfx_library():
    """Every reusable sound effect: the builtin soundeffects/ folder plus every clip
    ever generated via text-to-audio (all projects + the global cache)."""
    exts = {".wav", ".mp3", ".m4a", ".aac", ".ogg", ".flac"}
    roots = [ROOT / "soundeffects", ROOT / "wavespeed_media" / "sfx_generated"]
    projects = agent_core.PROJECTS_DIR
    if projects.exists():
        for proj in projects.iterdir():
            if proj.is_dir():
                roots.append(proj / "wavespeed_media" / "sfx_generated")
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
            cat = path.parent.name
            out.append({
                "name": path.stem.replace("_", " "),
                "path": str(path.resolve()),
                "url": link_for(path),
                "category": cat,
                "duration": 1.0,
            })
    return out


def timeline_library_payload(slug):
    project_dir = safe_project_dir(slug)
    media = []
    if project_dir:
        for kind, path in project_media_files(project_dir):
            if kind in {"render", "review"}:
                continue
            if is_image_path(path) or is_video_path(path):
                media.append({
                    "kind": kind, "name": path.name, "path": str(path.resolve()),
                    "url": link_for(path), "type": "video" if is_video_path(path) else "image",
                })
    return {"media": media, "sfx": global_sfx_library()}


def timeline_model(slug):
    config = agent_core.load_project_config(slug)
    project_dir = agent_core.PROJECTS_DIR / slug
    manifest_clips = pipeline.seedance_manifest_map(project_dir / "seedance 2.0")
    scenes = []
    for index, scene in enumerate(config.get("scenes", [])):
        start = float(scene.get("start", 0) or 0)
        end = float(scene.get("end", start) or start)
        dur = max(0.3, end - start)
        poster, clip_url, img = _timeline_media(project_dir, scene, config, index + 1, manifest_clips)
        is_speaker = bool(scene.get("speaker_hook"))
        label = scene.get("name") or scene.get("caption") or scene.get("script") or f"Scene {index + 1}"
        label = re.sub(r"\s+", " ", str(label)).strip()[:54] or f"Scene {index + 1}"
        if is_speaker:
            label = "\U0001f3a4 Speaker hook — " + label
        elif clip_url:
            label = "\U0001f3ac " + label
        replaceable = bool(img and "web images" in {p.lower() for p in img.parts} and not is_speaker)
        scenes.append({
            "id": str(scene.get("id", index)),
            "label": label,
            "dur": round(dur, 2),
            "poster": poster,
            "clip": clip_url,
            "kind": "speaker" if is_speaker else ("clip" if clip_url else "image"),
            "path": str(img) if img else "",
            "replaceable": replaceable,
            "speaker": is_speaker,
            "start": round(start, 3),
        })
    # SFX events (split into boundary transitions vs. content), each individually editable.
    overrides = config.get("sfx_overrides") or {}
    plan = pipeline.sfx_event_plan(config, has_speech=True)
    start_by_id = {s["id"]: s["start"] for s in scenes}
    transitions, content = [], []
    for ev in plan:
        if ev["transition"]:
            if ev["id"].startswith("tr-"):  # clip-boundary transition (CapCut connector)
                transitions.append({"id": ev["id"], "scene_id": ev["scene_id"], "label": ev["label"],
                                    "volume": ev["volume"], "enabled": ev["enabled"]})
        else:
            offset = round(ev["at"] - start_by_id.get(ev["scene_id"], ev["at"]), 3)
            content.append({"id": ev["id"], "scene_id": ev["scene_id"], "label": ev["label"], "category": ev["category"],
                            "offset": max(0.0, offset), "duration": ev["duration"], "volume": ev["volume"], "enabled": ev["enabled"]})
    captions_on = bool(config.get("render_captions", True))
    volumes = {
        "voice": float(config.get("audio_master_gain", 1.0) or 1.0),
        "music": float(config.get("background_music_volume", 0.0) or 0.0),
    }
    return {
        "slug": slug,
        "title": config.get("title", slug),
        "duration": round(float(config.get("duration", 0) or 0), 2),
        "scenes": scenes,
        "transitions": transitions,
        "sfx": content,
        "captions": captions_on,
        "volumes": volumes,
    }


def start_timeline_job(slug, edits):
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
    body = header + model_tag + TIMELINE_SKELETON.replace("__SLUG__", esc(slug)) + TIMELINE_ASSETS
    return page("Timeline Editor", body)


def job_page(job_id):
    with JOB_LOCK:
        job = dict(JOBS.get(job_id, {"status": "missing", "logs": [], "result": None, "error": "Unknown job"}))
    status = job["status"]
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
    if proj and Path(proj).exists() and job.get("job_kind") != "sfx":
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
    }
    return json.dumps(payload).encode("utf-8")


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

    def send_static_asset(self, name):
        allowed = {
            "app_icon.ico": "image/x-icon",
            "app_icon.png": "image/png",
            "favicon.ico": "image/x-icon",
            "start_icon.ico": "image/x-icon",
        }
        if name not in allowed:
            self.send_error(404)
            return
        path = STATIC_DIR / name
        if not path.exists():
            self.send_error(404)
            return
        self.send_bytes(path.read_bytes(), allowed[name])

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/":
            q = urllib.parse.parse_qs(parsed.query)
            self.send_bytes(form_page(clear="new" in q, open_load="load" in q, load_slug=q.get("project", [""])[0]))
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
        elif parsed.path == "/assets":
            self.send_bytes(assets_page())
        elif parsed.path == "/sfx":
            self.send_bytes(sfx_page())
        elif parsed.path == "/timeline":
            slug = urllib.parse.parse_qs(parsed.query).get("slug", [""])[0]
            self.send_bytes(timeline_page(slug))
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
        elif parsed.path == "/job":
            job_id = urllib.parse.parse_qs(parsed.query).get("id", [""])[0]
            self.send_bytes(job_page(job_id))
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
            self.send_bytes(resolved.read_bytes(), content_type_for(resolved))
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
        if parsed.path == "/test-connection":
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length)
            try:
                payload = json.loads(raw.decode("utf-8", errors="replace") or "{}")
            except Exception:
                payload = {}
            result = test_scrape_connection(str(payload.get("cookies", "") or "").strip())
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps(result).encode("utf-8"))
            return
        if parsed.path == "/approve-speech":
            job_id = urllib.parse.parse_qs(parsed.query).get("id", [""])[0]
            with JOB_LOCK:
                job = JOBS.get(job_id)
                if job and job.get("status") == "awaiting_approval":
                    job["speech_decision"] = "approve"
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
            # 1) persist the current timeline (order/durations/etc.) so the rework builds on it
            try:
                agent_core.save_timeline_edits(slug, data.get("edits") or {})
            except Exception as exc:
                self.send_bytes(json.dumps({"ok": False, "error": f"Could not save edits: {exc}"}).encode("utf-8"), "application/json; charset=utf-8")
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7865)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Autonomous Shorts Agent running at http://{args.host}:{args.port}")
    import threading
    # threading.Thread(target=launch_browser, args=(args.host, args.port), daemon=True).start()
    server.serve_forever()


if __name__ == "__main__":
    main()
