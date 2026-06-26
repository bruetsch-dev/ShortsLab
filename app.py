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
    "speaker_name": "Narrator",
    "tts_voice": "Achernar",
    "tts_model": "flash",
    "image_model": "openai/gpt-image-2/text-to-image",
    "hook_text": "",
    "hook_pause_s": "0.45",
    "speaker_image_path": "",
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
}
UI_PERSIST_SKIP_FIELDS = {"slug", "loaded_project_mode"}


class RunCancelled(RuntimeError):
    pass


def esc(value):
    return html.escape(str(value or ""), quote=True)


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
        items.append(f'<div class="project-list-item" style="padding: 12px; border: 1px solid #334247; border-radius: 5px; cursor: pointer; background: #0f1518; transition: background 0.2s;" onmouseover="this.style.background=\'#1a2429\'" onmouseout="this.style.background=\'#0f1518\'" onclick="window.loadProject(\'{esc(slug)}\')"><div style="font-weight: bold; margin-bottom: 4px;">{esc(title)}</div><div style="font-size: 12px; color: #a8aaa3;">{esc(slug)} &bull; {esc(created)}</div></div>')
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
      :root {
        color-scheme: dark;
        font-family: Inter, Segoe UI, Arial, sans-serif;
        --bg: #090b0d;
        --bg-panel: #12161a;
        --bg-panel-2: #171d21;
        --line: #2a3438;
        --line-strong: #536066;
        --text: #f4f0e7;
        --muted: #a8aaa3;
        --accent: #7bd6c5;
        --accent-2: #f0b45f;
        --accent-3: #d96b59;
      }
      * { scrollbar-color: #3c4a4f #0d1012; }
      body {
        margin: 0;
        min-height: 100vh;
        background:
          linear-gradient(90deg, rgba(255,255,255,.035) 1px, transparent 1px),
          linear-gradient(0deg, rgba(255,255,255,.025) 1px, transparent 1px),
          linear-gradient(180deg, #101418 0%, var(--bg) 58%, #070809 100%);
        background-size: 36px 36px, 36px 36px, auto;
        color: var(--text);
      }
      main { max-width: 1480px; margin: 0 auto; padding: 30px clamp(20px, 4vw, 48px); }
      h1 {
        font-size: 30px; margin: 0; letter-spacing: -.4px; font-weight: 900; line-height: 1.05;
        background: linear-gradient(96deg, #fff8eb 0%, var(--accent) 58%, var(--accent-2) 100%);
        -webkit-background-clip: text; background-clip: text; -webkit-text-fill-color: transparent;
      }
      h2 { font-size: 18px; margin: 0 0 12px; color: #fff8eb; }
      .top { display: flex; justify-content: space-between; gap: 24px; align-items: center; border-bottom: 1px solid var(--line); padding-bottom: 20px; margin-bottom: 26px; }
      .brand { display: flex; gap: 16px; align-items: center; min-width: 0; }
      .brand-mark {
        width: 56px; height: 56px; flex: 0 0 auto; border-radius: 14px; object-fit: cover;
        border: 1px solid var(--line-strong);
        box-shadow: 0 10px 26px rgba(0,0,0,.45), inset 0 1px 0 rgba(255,255,255,.08);
      }
      .sub { color: var(--muted); margin-top: 7px; max-width: 760px; line-height: 1.5; font-size: 14px; }
      .nav-actions { display: flex; gap: 10px; flex-wrap: wrap; justify-content: flex-end; min-width: 170px; }
      .nav-actions .button { width: auto; min-width: 132px; }
      .tagline { font-weight: 800; letter-spacing: .3px; color: #ffe6ad; text-transform: lowercase; }
      .tagline .dot { color: var(--accent-2); padding: 0 5px; }
      .create-bar {
        grid-column: 1 / -1;
        position: sticky; top: 10px; z-index: 30;
        display: flex; gap: 16px; align-items: flex-end; flex-wrap: wrap;
        margin-bottom: 22px; border: 1px solid var(--line-strong);
        background: linear-gradient(180deg, rgba(34,25,11,.95), rgba(18,18,20,.95));
        box-shadow: 0 14px 34px rgba(0,0,0,.4);
      }
      .create-bar-models { display: flex; gap: 14px; flex: 1; flex-wrap: wrap; min-width: 0; }
      .create-bar .field { display: flex; flex-direction: column; gap: 6px; flex: 1; min-width: 190px; }
      .create-bar .field label { margin: 0; font-size: 12px; color: var(--muted); text-transform: uppercase; letter-spacing: .5px; }
      .create-short-btn { width: auto; min-width: 200px; font-size: 16px; font-weight: 800; padding: 14px 26px; align-self: stretch; }
      @media (max-width: 720px) { .create-short-btn { width: 100%; } }
      .tier-row { flex-basis: 100%; display: flex; gap: 12px; align-items: center; flex-wrap: wrap; }
      .tier-label { font-size: 12px; text-transform: uppercase; letter-spacing: .5px; color: var(--muted); font-weight: 700; }
      .tier-btns { display: flex; gap: 10px; flex: 1; min-width: 280px; }
      .create-bar .tier-btns .tier-btn { flex: 1 1 0; }
      .tier-btn {
        width: auto; min-width: 104px; display: flex; flex-direction: column; align-items: center; gap: 1px;
        padding: 8px 16px; line-height: 1.15; font-weight: 800; cursor: pointer;
        border: 1px solid var(--line-strong); border-radius: 10px; background: #161a1d; color: #f3efe6;
        transition: transform .12s ease, border-color .12s ease, background .12s ease;
      }
      .tier-btn small { font-weight: 600; font-size: 11px; color: var(--muted); }
      .tier-btn:hover { transform: translateY(-1px); border-color: var(--accent-2); background: #1d2226; }
      .tier-btn.tier-active { border-color: var(--accent); background: #2c1d0a; color: #fff7e9; box-shadow: inset 0 -2px 0 rgba(240,180,95,.8); }
      .tier-btn.tier-active small { color: #f6dcae; }
      .tier-hint { flex: 1; min-width: 180px; margin: 0; }
      .action-btns { display: flex; gap: 8px; flex-wrap: wrap; }
      .action-btns .action-btn {
        width: auto; flex: 1 1 150px; min-width: 140px;
        display: flex; flex-direction: column; gap: 2px; align-items: flex-start;
        padding: 10px 14px; border: 1px solid var(--line); border-radius: 9px;
        background: #12181b; color: #d7d2c6; font-weight: 800; cursor: pointer; line-height: 1.2;
        transition: transform .12s ease, border-color .12s ease, background .12s ease;
      }
      .action-btns .action-btn small { font-weight: 600; font-size: 11px; color: var(--muted); }
      .action-btns .action-btn:hover { transform: translateY(-1px); border-color: var(--accent-2); }
      .action-btns .action-btn.active { border-color: var(--accent); background: #2c1d0a; color: #fff7e9; }
      .action-btns .action-btn.active small { color: #f6dcae; }
      .speaker-gallery { display: grid; grid-template-columns: repeat(auto-fill, minmax(72px, 1fr)); gap: 8px; margin-bottom: 10px; max-height: 232px; overflow-y: auto; padding: 2px; }
      .speaker-tile { width: 100%; padding: 0; border: 2px solid var(--line); border-radius: 8px; overflow: hidden; background: #06080a; cursor: pointer; aspect-ratio: 3 / 4; transition: border-color .12s ease, transform .12s ease; }
      .speaker-tile img { width: 100%; height: 100%; object-fit: cover; display: block; }
      .speaker-tile:hover { border-color: var(--accent-2); transform: translateY(-1px); }
      .speaker-tile.selected { border-color: var(--accent); box-shadow: 0 0 0 2px rgba(240,180,95,.4); }
      .speaker-upload { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; margin: 4px 0 8px; }
      .speaker-upload .hint { margin: 0; }
      form { display: grid; grid-template-columns: 1fr 1fr; gap: 30px; align-items: start; }
      label { display: block; font-weight: 850; margin-bottom: 7px; color: #fff8eb; }
      input, textarea, select {
        width: 100%;
        box-sizing: border-box;
        border: 1px solid var(--line);
        border-bottom-color: var(--line-strong);
        border-radius: 4px;
        padding: 12px 12px;
        font-size: 14px;
        background: #0d1114;
        color: var(--text);
        outline: none;
        transition: border-color .18s ease, box-shadow .18s ease, background .18s ease;
      }
      input:focus, textarea:focus, select:focus {
        border-color: var(--accent);
        box-shadow: 0 0 0 3px rgba(123, 214, 197, .14), inset 0 0 0 1px rgba(123, 214, 197, .2);
        background: #10171a;
      }
      input[type="checkbox"] { accent-color: var(--accent); }
      input[type="file"]::file-selector-button {
        border: 1px solid #496360;
        border-radius: 4px;
        background: #172a2b;
        color: #e9fff9;
        padding: 8px 10px;
        margin-right: 10px;
        font-weight: 850;
      }
      textarea { min-height: 360px; resize: vertical; line-height: 1.45; }
      textarea.visual-textarea { min-height: 210px; }
      .wide { grid-column: 1 / -1; }
      .panel {
        background: linear-gradient(180deg, var(--bg-panel-2), var(--bg-panel));
        border: 1px solid var(--line);
        border-radius: 6px;
        padding: 16px;
        box-shadow: 0 18px 50px rgba(0, 0, 0, .34), inset 0 1px 0 rgba(255,255,255,.045);
        transition: border-color .18s ease, transform .18s ease, box-shadow .18s ease;
      }
      .panel:hover { border-color: #3e4b50; box-shadow: 0 22px 54px rgba(0,0,0,.38), inset 0 1px 0 rgba(255,255,255,.055); }
      .panel.accent { border-left: 4px solid var(--accent); border-top-color: #38595c; }
      .stack { display: grid; gap: 14px; }
      .controls { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; }
      .project-load-controls { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 10px; align-items: end; }
      .project-load-controls .button { width: auto; min-width: 112px; padding: 12px 14px; }
      .checks { display: grid; gap: 10px; }
      .checks label { font-weight: 400; display: flex; gap: 7px; align-items: center; }
      .checks input { width: auto; }
      button:not(.preview-button), .button {
        position: relative;
        overflow: hidden;
        border: 1px solid #b68245;
        border-radius: 4px;
        background: #1b1711;
        color: #fff2df;
        padding: 13px 18px;
        font-weight: 950;
        cursor: pointer;
        width: 100%;
        text-decoration: none;
        display: inline-block;
        text-align: center;
        box-sizing: border-box;
        box-shadow: inset 0 -3px 0 rgba(240, 180, 95, .78), 0 10px 24px rgba(0,0,0,.28);
        transition: transform .16s ease, border-color .16s ease, box-shadow .16s ease, background .16s ease;
      }
      button:not(.preview-button)::after, .button::after {
        content: "";
        position: absolute;
        inset: 0;
        transform: translateX(-120%) skewX(-18deg);
        background: linear-gradient(90deg, transparent, rgba(255,255,255,.18), transparent);
        transition: transform .4s ease;
      }
      button:not(.preview-button):hover, .button:hover {
        transform: translateY(-1px);
        border-color: var(--accent-2);
        background: #21190f;
        box-shadow: inset 0 -3px 0 rgba(240, 180, 95, .95), 0 14px 30px rgba(0,0,0,.36);
      }
      button:not(.preview-button):hover::after, .button:hover::after { transform: translateX(120%) skewX(-18deg); }
      button:not(.preview-button):active, .button:active, .tier-btn:active, .action-btn:active { transform: translateY(1px) scale(.992); box-shadow: inset 0 2px 7px rgba(0,0,0,.45); }
      @keyframes page-fade { from { opacity: 0; transform: translateY(4px); } to { opacity: 1; transform: none; } }
      main { animation: page-fade .34s ease both; }
      @media (prefers-reduced-motion: reduce) { main { animation: none; } }
      button[type="submit"] { background: #281908; border-color: var(--accent-2); color: #fff7e9; }
      .button.secondary {
        background: #101619;
        border: 1px solid #37454b;
        color: #dff8f2;
        box-shadow: inset 0 -3px 0 rgba(123, 214, 197, .34), 0 10px 24px rgba(0,0,0,.24);
      }
      .button.secondary:hover { background: #142023; border-color: var(--accent); }
      button.danger, .button.danger {
        background: #2a1111;
        border-color: #a84b45;
        color: #ffe1dc;
        box-shadow: inset 0 -3px 0 rgba(217, 107, 89, .55), 0 10px 24px rgba(0,0,0,.26);
      }
      button.danger:hover, .button.danger:hover { background: #351515; border-color: #d96b59; }
      pre {
        white-space: pre-wrap;
        background: #06080a;
        color: #e9e6db;
        padding: 14px;
        border: 1px solid #1c2529;
        border-radius: 5px;
        max-height: 480px;
        overflow: auto;
        line-height: 1.35;
      }
      a { color: var(--accent); }
      .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap: 14px; }
      .status { display: inline-block; border-radius: 3px; padding: 5px 10px; font-weight: 950; background: #172024; color: #dbe6dc; border: 1px solid #32434a; }
      .done { background: #10281f; border-color: #2f7258; color: #c8f1df; }
      .error { background: #341714; border-color: #80392f; color: #ffd9d3; }
      .cancelled { background: #2a2113; border-color: #8d6a31; color: #ffe0a9; }
      .cancelling { background: #2a2113; border-color: #8d6a31; color: #ffe0a9; }
      .hint { color: var(--muted); font-size: 13px; margin-top: 6px; line-height: 1.35; }
      .job-head { display: flex; justify-content: space-between; gap: 16px; align-items: center; margin-bottom: 16px; }
      .job-actions { display: flex; gap: 10px; flex-wrap: wrap; justify-content: flex-end; }
      .job-actions .button { width: auto; min-width: 132px; }
      .inline-form { margin: 0; display: inline; }
      .inline-form button { width: auto; min-width: 132px; }
      .panel-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 10px; }
      .panel-head h2 { margin: 0; }
      .panel-head .button { width: auto; min-width: 94px; padding: 8px 11px; }
      .progress-wrap { margin: 16px 0 18px; }
      .elapsed-line { display: flex; justify-content: space-between; gap: 12px; margin-bottom: 8px; color: #9aa4a1; font-size: 13px; font-weight: 800; }
      .progress-label { display: flex; justify-content: space-between; gap: 12px; margin-bottom: 8px; color: #cbc7ba; font-weight: 800; }
      .progress-label span { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
      .progress-track { height: 14px; border-radius: 3px; overflow: hidden; background: #0d1114; border: 1px solid var(--line); box-shadow: inset 0 1px 8px rgba(0,0,0,.65); }
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
        padding: 9px 12px; border-radius: 9px; border: 1px solid var(--line);
        background: #0e1316; color: #8b938f; font-weight: 800; font-size: 12.5px;
        transition: color .25s ease, border-color .25s ease, background .25s ease;
      }
      .step-chip .step-fill { position: absolute; left: 0; top: 0; bottom: 0; width: 0; z-index: 0;
        background: linear-gradient(90deg, rgba(240,180,95,.32), rgba(240,180,95,.08)); }
      .step-chip .step-name, .step-chip .step-time { position: relative; z-index: 1; }
      .step-chip .step-name { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
      .step-chip .step-time { font-size: 11px; font-weight: 700; color: #c9c3b6; font-variant-numeric: tabular-nums; }
      .step-chip.pending { opacity: .5; }
      .step-chip.active { color: #fff7e9; border-color: var(--accent); background: #1a1407; box-shadow: 0 6px 18px rgba(0,0,0,.35); }
      .step-chip.active .step-fill { animation: stepfill 45s linear forwards; animation-delay: calc(-1 * var(--el)); }
      .step-chip.done { color: #d8ffe2; border-color: #2f7d49; background: #102316; }
      .step-chip.done .step-fill { width: 100%; background: linear-gradient(90deg, #1f6b3a, #2f9b54); }
      .step-chip.done .step-time { color: #9fe6b6; }
      .step-chip.stopped { color: #ffd9d3; border-color: #7d3030; background: #221010; }
      .step-chip.stopped .step-fill { width: 100%; background: linear-gradient(90deg, #6b1f1f, #9b2f2f); }
      @keyframes stepfill { from { width: 6%; } to { width: 94%; } }
      .progress-current { color: #cbc7ba; font-weight: 700; font-size: 13px; min-height: 18px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
      .job-layout { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 16px; align-items: stretch; margin-top: 16px; }
      .job-layout > .panel, .job-layout > aside { min-height: min(76vh, 860px); min-width: 0; }
      .log-box { height: calc(min(78vh, 900px) - 70px); max-height: none; overflow: auto; font-size: 13px; }
      .preview-grid { display: grid; grid-template-columns: 1fr; gap: 12px; }
      .preview img { display: block; width: 100%; max-height: 420px; object-fit: contain; background: #06080a; border: 1px solid #253036; border-radius: 5px; transition: transform .18s ease, filter .18s ease; }
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
        border: 1px solid #263238;
        background: #0a0f12;
        border-radius: 5px;
        padding: 6px;
        min-width: 0;
      }
      .media-tile.queued { border-color: #d7b35d; box-shadow: inset 0 0 0 1px rgba(215,179,93,.25); }
      .media-tile img { display: block; width: 100%; aspect-ratio: 9 / 14; object-fit: cover; border-radius: 3px; background: #050709; }
      .media-tile video { display: block; width: 100%; aspect-ratio: 9 / 14; object-fit: cover; border-radius: 3px; background: #050709; }
      .media-audio-tile { display: grid; place-items: center; width: 100%; aspect-ratio: 9 / 14; border-radius: 3px; background: #050709; color: #a8aaa3; border: 1px solid #26343a; font-weight: 900; }
      .media-tile label { display: flex; gap: 6px; align-items: center; margin-top: 6px; color: #d8dbd2; font-size: 12px; font-weight: 850; }
      .media-tile input { width: auto; margin: 0; accent-color: var(--accent); }
      .media-tile small { display: block; color: #8d9895; font-size: 11px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; margin-top: 4px; }
      .media-kind { position: absolute; top: 10px; left: 10px; padding: 3px 5px; border-radius: 3px; background: rgba(4,7,8,.82); border: 1px solid rgba(210,217,206,.18); color: #e7e4db; font-size: 10px; font-weight: 950; text-transform: uppercase; }
      .preview-section { grid-column: 1 / -1; }
      .loaded-media-panel .media-tiny-grid { grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; }
      .loaded-media-panel .media-tab-panel { max-height: none; }
      .media-tabs { display: flex; flex-wrap: nowrap; overflow-x: auto; padding-bottom: 5px; gap: 6px; margin: 10px 0; }
      .media-tab {
        border: 1px solid #334247; background: #0f1518; color: #d8dbd2; padding: 7px 9px;
        border-radius: 3px; font-size: 12px; font-weight: 900; cursor: pointer;
      }
      .media-tab.active { border-color: rgba(123,214,197,.75); background: rgba(123,214,197,.14); color: #f6fff9; }
      .media-tab-panel[hidden] { display: none; }
      .output-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr)); gap: 12px; margin-bottom: 16px; }
      .output-card { display: grid; gap: 10px; align-content: start; border-top: 1px solid #45545a; }
      .output-card strong { font-size: 15px; }
      .output-card small { color: #9a9d95; word-break: break-all; line-height: 1.35; }
      .output-actions { display: flex; gap: 8px; }
      .output-actions .button { width: auto; flex: 1; padding: 10px 12px; }
      .media-inline { width: 100%; max-height: 260px; border-radius: 5px; background: #06080a; border: 1px solid #253036; object-fit: contain; }
      .asset-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 14px; }
      .asset-card { display: grid; gap: 12px; align-content: start; }
      .asset-card h2 { margin-bottom: 0; }
      .asset-thumb { width: 100%; aspect-ratio: 16 / 10; object-fit: cover; background: #06080a; border: 1px solid #253036; border-radius: 5px; }
      .asset-meta { display: flex; gap: 8px; flex-wrap: wrap; color: #c7cbc3; font-size: 12px; }
      .asset-pill { border: 1px solid #334247; background: #0f1518; padding: 5px 8px; border-radius: 3px; }
      .asset-actions { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; }
      .asset-actions .button { width: 100%; padding: 10px 12px; }
      .media-page img, .media-page video { width: 100%; max-height: 78vh; object-fit: contain; background: #06080a; border: 1px solid #253036; border-radius: 6px; }
      .media-page audio { width: 100%; }
      .file-list { display: grid; gap: 8px; padding-left: 0; list-style: none; }
      .file-list a { display: block; padding: 10px 12px; border-radius: 4px; background: #11171a; border: 1px solid var(--line); text-decoration: none; }
      .file-list a:hover { border-color: var(--accent); background: #152024; }
      .lightbox { position: fixed; inset: 0; display: none; place-items: center; padding: 24px; background: rgba(4, 6, 7, .9); backdrop-filter: blur(8px); z-index: 50; }
      .lightbox.open { display: grid; }
      .lightbox-inner { width: min(1100px, 96vw); }
      .lightbox-bar { display: flex; justify-content: space-between; align-items: center; gap: 12px; margin-bottom: 10px; color: #f2f0e8; }
      .lightbox-title { font-weight: 900; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
      .lightbox-close { width: auto; padding: 8px 12px; background: #101619; color: #f2f0e8; border: 1px solid #3c4440; }
      .lightbox img { display: block; width: 100%; max-height: 84vh; object-fit: contain; background: #06080a; border: 1px solid #2e3b41; border-radius: 6px; }
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
        position: fixed; top: 0; left: 0; width: 100%; height: 100%;
        background: rgba(0,0,0,0.8); z-index: 1000;
        display: flex; justify-content: center; align-items: center;
        opacity: 0; pointer-events: none; transition: opacity 0.2s;
      }
      .modal-overlay.active { opacity: 1; pointer-events: auto; }
      .modal-content {
        background: #111; padding: 30px; border-radius: 8px;
        border: 1px solid var(--line); width: 100%; max-width: 600px;
        box-shadow: 0 10px 30px rgba(0,0,0,0.5);
      }
      .modal-content h2 { margin-top: 0; }
      .modal-close { float: right; cursor: pointer; color: var(--muted); border: none; background: none; font-size: 20px; }
      .modal-close:hover { color: #fff; }
      /* --- Form-page polish --- */
      .mode-selector { background: linear-gradient(180deg, #15191d, #11151a); }
      .mode-selector > label { font-size: 16px; letter-spacing: .2px; }
      .run-mode-btn { min-width: 168px; }
      .button.primary { border-color: var(--accent-2); background: #2c1d0a; color: #fff7e9; box-shadow: inset 0 -3px 0 rgba(240,180,95,.95), 0 12px 28px rgba(0,0,0,.34); }
      .loaded-media-panel {
        background: linear-gradient(180deg, var(--bg-panel-2), var(--bg-panel));
        border: 1px solid var(--line) !important; border-radius: 10px !important;
        box-shadow: 0 18px 50px rgba(0,0,0,.34), inset 0 1px 0 rgba(255,255,255,.045);
      }
      #project-media-hint {
        text-align: center; min-height: 220px; padding: 64px 24px;
        border: 1px dashed #303c42; border-radius: 10px; color: var(--muted);
        background: repeating-linear-gradient(45deg, rgba(255,255,255,.014) 0 12px, transparent 12px 24px);
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
        var autosaveKey = "autonomous-shorts-agent-ui-state-v1";
        var autosaveSkip = {
          initial_replace_media_path: true,
          loaded_project_source: true,
          slug: true,
          loaded_project_mode: true
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
            if (cb && input.files && input.files.length) cb.checked = true;
          };
          window.applyTier = function (tier) {
            var TIERS = {
              cheap:  { video_model: "ltx-2.3",           image_model: "google/nano-banana-2/text-to-image", reasoning_model: "z-ai/glm-5.2",              tts_model: "flash" },
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
          document.addEventListener("pointerdown", function (ev) {
            var el = ev.target.closest("button, .button, .tier-btn, .action-btn, .speaker-tile");
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


def form_page():
    state = load_ui_state()
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
    <div class="top">
      <div class="brand">
        <img class="brand-mark" src="/static/app_icon.png" alt="" width="56" height="56">
        <div>
          <h1>Shortslab</h1>
          <p class="sub"><span class="tagline">script <span class="dot">&bull;</span> chill <span class="dot">&bull;</span> upload</span> &mdash; paste a script, pick a voice, and the Auto Director builds a viral 9:16 Short end to end: narration, footage, word-by-word captions, and cinematic motion.</p>
        </div>
      </div>
      <div class="nav-actions">
        <button type="button" class="button" onclick="newProject()">&#43; New Project</button>
        <button type="button" class="button secondary" onclick="document.getElementById('load-modal').classList.add('active')">&#128193; Load Project</button>
        <a class="button secondary" href="/sfx">&#128266; Add SFX to a Video</a>
        <a class="button secondary" href="/assets">&#127916; Asset Library</a>
      </div>
    </div>
    <form id="short-form" method="post" action="/run" enctype="multipart/form-data">
      <input id="loaded-project-source" type="hidden" name="loaded_project_source" value="">

      <div class="create-bar panel">
        <div class="tier-row">
          <span class="tier-label">Quality preset</span>
          <div class="tier-btns">
            <button type="button" class="tier-btn" onclick="applyTier('cheap')">Cheap<small>~$0.50 / run</small></button>
            <button type="button" class="tier-btn" onclick="applyTier('medium')">Medium<small>~$1.50 / run</small></button>
            <button type="button" class="tier-btn" onclick="applyTier('best')">Best<small>~$3.50 / run</small></button>
          </div>
          <span class="tier-hint hint">Presets just load the model picks below &mdash; tweak them anytime. Prices are rough estimates.</span>
        </div>
        <div class="create-bar-models">
          <div class="field">
            <label>Video model</label>
            <select name="video_model">
              <option value="seedance-2.0"{' selected' if state.get("video_model", state.get("seedance_model", "seedance-2.0")) == "seedance-2.0" else ""}>Seedance 2.0 (Web Search + Audio)</option>
              <option value="seedance-2.0-fast"{' selected' if state.get("video_model") == "seedance-2.0-fast" else ""}>Seedance 2.0 Fast (Web Search + Audio)</option>
              <option value="seedance-v1.5-pro"{' selected' if state.get("video_model", state.get("seedance_model")) == "seedance-v1.5-pro" else ""}>Seedance 1.5 Pro</option>
              <option value="ltx-2.3"{' selected' if state.get("video_model") == "ltx-2.3" else ""}>LTX-2.3 (cheap)</option>
              <option value="happyhorse-1.1"{' selected' if state.get("video_model") == "happyhorse-1.1" else ""}>Happy Horse 1.1 (720p)</option>
            </select>
          </div>
          <div class="field">
            <label>Image model</label>
            <select name="image_model">
              <option value="openai/gpt-image-2/text-to-image"{' selected' if state.get("image_model") == "openai/gpt-image-2/text-to-image" else ""}>GPT-Image-2 (best)</option>
              <option value="google/nano-banana-2/text-to-image"{' selected' if state.get("image_model") == "google/nano-banana-2/text-to-image" else ""}>Nano-Banana-2 (cheap)</option>
            </select>
          </div>
          <div class="field">
            <label>Reasoning model</label>
            <select name="reasoning_model">
              <option value="openai/gpt-5.5"{' selected' if state.get("reasoning_model") == "openai/gpt-5.5" else ""}>GPT-5.5 (fast, standard)</option>
              <option value="anthropic/claude-opus-4.8"{' selected' if state.get("reasoning_model") == "anthropic/claude-opus-4.8" else ""}>Claude Opus 4.8 (best quality)</option>
              <option value="z-ai/glm-5.2"{' selected' if state.get("reasoning_model") == "z-ai/glm-5.2" else ""}>GLM-5.2 (cheap)</option>
            </select>
          </div>
        </div>
        <button type="submit" class="button primary create-short-btn">&#9889; Create Short</button>
      </div>

      <section class="stack">
        <div class="panel accent">
          <label>Text script</label>
          <textarea id="script-field" name="script" placeholder="Paste your timed/plain text script here. The voiceover is generated from it automatically.">{esc(state.get("script"))}</textarea>
          <input type="hidden" name="hook_text" id="hook-text" value="{esc(state.get('hook_text'))}">
          <div class="hook-controls" style="display:flex; gap:8px; align-items:center; flex-wrap:wrap; margin-top:10px;">
            <button type="button" class="button secondary" onclick="markHook()">&#9733; Mark selection as hook</button>
            <button type="button" class="button secondary" onclick="clearHook()">Clear hook</button>
            <span id="hook-indicator" class="hint" style="flex:1; min-width:160px;"></span>
          </div>
          <div class="hint">Select the opening line(s) in the script and click &ldquo;Mark selection as hook&rdquo;. The hook is spoken first, then a short pause, then the rest &mdash; and (with a speaker image) it drives the InfiniteTalk talking-head opening.</div>
        </div>
        <div class="panel">
          <label>Optional Visual Direction</label>
          <textarea class="visual-textarea" name="visual_script" placeholder="Optional. Leave empty if you want the agent to create the visual plan automatically from the Voice Script. Use this only for style or direction, e.g. darker documentary style, faster cuts, more maps, more archive images, more dramatic reconstructions.">{esc(state.get("visual_script"))}</textarea>
          <div class="hint">The Voice Script is authoritative. The Optional Visual Direction is secondary guidance. If empty, the full visual plan is inferred from the Voice Script.</div>
        </div>

        <div class="panel" id="loaded-actions-panel" style="display:none;">
          <button type="button" class="button" onclick="openTimeline()" style="margin-bottom:12px;">&#127902; Open timeline editor (trim, mix &amp; render)</button>
          <label>Loaded project &mdash; what should the run do?</label>
          <input type="hidden" name="loaded_project_mode" id="loaded-project-mode" value="{esc(state.get('loaded_project_mode') or 'normal')}">
          <div class="action-btns">
            <button type="button" class="action-btn" data-mode="normal" onclick="setProjectMode('normal')">Normal run<small>fresh pass in this folder</small></button>
            <button type="button" class="action-btn" data-mode="recut_existing_only" onclick="setProjectMode('recut_existing_only')">Recut existing<small>reuse current media, re-edit</small></button>
            <button type="button" class="action-btn" data-mode="recut_new_web_images" onclick="setProjectMode('recut_new_web_images')">New web images<small>refetch images + recut</small></button>
            <button type="button" class="action-btn" data-mode="recut_regenerate_seedance" onclick="setProjectMode('recut_regenerate_seedance')">Regenerate clips<small>new Seedance clips + recut</small></button>
            <button type="button" class="action-btn" data-mode="recut_recreate_speaker_clip" onclick="setProjectMode('recut_recreate_speaker_clip')">Recreate hook<small>new speaker hook + recut</small></button>
          </div>
          <div class="hint">Only shown for a loaded project. Choose what to reuse versus regenerate before re-rendering.</div>
        </div>

        <div class="panel">
          <label>Voice &amp; narration</label>
          <div style="display:flex; gap:10px; flex-wrap:wrap;">
            <input type="text" name="speaker_name" value="{esc(state.get('speaker_name') or 'Narrator')}" placeholder="Speaker name (e.g. Rose)" style="flex:1; min-width:150px;">
            <select name="tts_voice" style="flex:1; min-width:180px;">{voice_options}</select>
            <select name="tts_model" style="flex:1; min-width:180px;">
              <option value="flash"{' selected' if sel_tts_model == 'flash' else ''}>Gemini 2.5 Flash TTS (cheaper)</option>
              <option value="pro"{' selected' if sel_tts_model == 'pro' else ''}>Gemini 2.5 Pro TTS (higher quality)</option>
            </select>
          </div>
          <div class="checks">
            <label><input type="checkbox" name="mix_voice_in_final"{checked("mix_voice_in_final")}> Use the generated voice as the final narration (music/SFX ducked under it)</label>
          </div>
          <div class="hint">The narration is generated from your script with Gemini TTS (English). The speaker name is sent ahead of the script, and the voice is force-aligned for frame-accurate word-by-word captions and beat-synced cuts.</div>
        </div>
        <div class="panel">
          <label>Speaker hook image</label>
          <input type="hidden" name="speaker_image_path" id="speaker-image-path" value="{esc(state.get('speaker_image_path'))}">
          {speaker_gallery_html(state.get('speaker_image_path'))}
          <div class="speaker-upload">
            <span class="hint">Or upload a new face:</span>
            <input type="file" name="speaker_image_file" accept="image/*" onchange="onSpeakerUpload(this)">
          </div>
          <div class="checks">
            <label><input type="checkbox" name="enable_speaker_hook"{checked("enable_speaker_hook")}> Create a talking-head hook clip (InfiniteTalk) from this image</label>
          </div>
          <div class="hint">Optional. Pick a saved speaker or upload a new face. The marked hook is then spoken by this person as a lip-synced talking-head opening (InfiniteTalk), driven by the generated hook audio. (If no hook is marked, a Seedance speaker clip is the fallback.)</div>
        </div>

        <details class="panel">
          <summary style="cursor: pointer; font-weight: bold; padding: 5px;">Advanced options</summary>
          <div style="margin-top: 20px;">
            <label>Agent options</label>
            <div class="checks">
              <label><input type="checkbox" name="autonomous_director"{checked("autonomous_director")}> Auto Director decides APIs, media counts, and scene strategy</label>
              <label><input type="checkbox" name="use_audio_timing"{checked("use_audio_timing")}> Force-align the generated voice for frame-accurate timing</label>
              <label><input type="checkbox" name="use_llm_search"{checked("use_llm_search")}> Use Reasoning Agent to plan web image searches</label>
              <label><input type="checkbox" name="use_llm_video_review"{checked("use_llm_video_review")}> Use Reasoning Agent two-pass review and auto-correction</label>
              <label><input type="checkbox" name="auto_web_images"{checked("auto_web_images")}> Search and download web images automatically</label>
              <label><input type="checkbox" name="background_music_enabled"{checked("background_music_enabled")}> Add background music</label>
              <label><input type="checkbox" name="generate_missing_sfx"{checked("generate_missing_sfx")}> Generate missing sound effects with Kling when the library has no fit</label>
              <label><input type="checkbox" name="allow_gpt"{checked("allow_gpt")}> Generate GPT source images for Seedance when needed</label>
              <label><input type="checkbox" name="allow_seedance"{checked("allow_seedance")}> Generate missing Seedance clips</label>
            </div>
          </div>
        </details>
      </section>

      <section class="stack preview-section">
        <div class="loaded-media-panel" style="border: 1px solid var(--line); border-radius: 10px; padding: 22px;">
          <h2 style="margin-bottom: 5px;">&#127916; Project media preview</h2>
          <div class="hint" id="project-media-hint" style="margin-bottom: 18px;">No project loaded. Click &ldquo;Load Project&rdquo; in the top bar to browse a past project &mdash; its media appears here, full width.</div>
          <div id="project-media-preview"></div>
        </div>
      </section>
    </form>

    <div id="load-modal" class="modal-overlay">
      <div class="modal-content">
        <button class="modal-close" onclick="document.getElementById('load-modal').classList.remove('active')">&times;</button>
        <h2 style="margin-bottom: 15px;">Load previous project (Repair / Recut)</h2>
        <div>
          <div class="project-load-controls" style="display: flex; flex-direction: column; gap: 8px; max-height: 50vh; overflow-y: auto;">
            {previous_project_options}
          </div>
          <div id="project-load-status" class="hint" style="margin-top: 15px;">Click a project above to load its script, visual direction, audio, and available settings. Web images will appear in the Media Preview.</div>
        </div>
      </div>
    </div>
    """
    body += """
    <script>
    (function(){
      function ind(){
        var h=document.getElementById('hook-text');
        var el=document.getElementById('hook-indicator');
        if(!h||!el) return;
        var v=(h.value||'').trim();
        el.textContent = v ? ('Hook: “'+(v.length>70?v.slice(0,70)+'…':v)+'”') : 'No hook marked (the whole script is read straight through).';
        el.style.color = v ? 'var(--accent, #ffd400)' : '';
      }
      window.markHook=function(){
        var ta=document.getElementById('script-field'); if(!ta) return;
        var sel=ta.value.substring(ta.selectionStart, ta.selectionEnd).trim();
        if(!sel){ alert('Select the hook text inside the script first, then click \\u201cMark selection as hook\\u201d.'); return; }
        var h=document.getElementById('hook-text'); if(h){ h.value=sel; } ind();
      };
      window.clearHook=function(){ var h=document.getElementById('hook-text'); if(h){ h.value=''; } ind(); };
      document.addEventListener('DOMContentLoaded', ind); ind();
    })();
    </script>
    """
    return page("Shortslab", body)


def sfx_page():
    body = """
    <div class="top">
      <div class="brand">
        <img class="brand-mark" src="/static/app_icon.png" alt="" width="56" height="56">
        <div>
          <h1>AI Sound-Effect Pass</h1>
          <p class="sub">Upload a finished Short and an Opus&nbsp;4.8 agent adds fitting sound effects &mdash; whooshes on image changes, impacts and stingers to punch key spoken moments &mdash; mixed quietly under your existing audio. The video stream stays untouched (lossless).</p>
        </div>
      </div>
      <div class="nav-actions">
        <a class="button secondary" href="/">New project</a>
        <a class="button secondary" href="/assets">Assets</a>
      </div>
    </div>
    <form method="post" action="/sfx-run" enctype="multipart/form-data">
      <section class="stack">
        <div class="panel accent">
          <label>Finished Short (video)</label>
          <input type="file" name="video_file" accept="video/mp4,video/quicktime,video/webm,video/x-matroska,.mp4,.mov,.webm,.mkv" required>
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
    fields["_cancel_event"] = cancel_event
    fields["_replace_lock"] = replace_lock
    fields["_replace_requests"] = replace_requests
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
    """Saved speaker images: the repo 'speaker images' folder + previously uploaded."""
    out, seen = [], set()
    for folder in (SPEAKER_GALLERY_DIR, SPEAKER_UPLOADED_DIR):
        if not folder.exists():
            continue
        files = [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in SPEAKER_IMAGE_EXTS]
        for p in sorted(files, key=lambda x: x.stat().st_mtime, reverse=True):
            rp = str(p.resolve())
            if rp not in seen:
                seen.add(rp)
                out.append(p)
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
    if not images:
        return ('<div class="hint">No saved speaker images yet. Drop face photos into the '
                '<code>speaker images</code> folder in the repo, or upload one below &mdash; '
                'uploaded speakers are remembered here.</div>')
    tiles = []
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
    thumb_html = ""
    if thumb and Path(thumb).exists() and is_image_path(thumb):
        thumb_html = (
            f'<button class="preview-button" type="button" data-preview-src="{link_for(thumb)}" data-preview-title="{esc(summary["title"])}">'
            f'<img class="asset-thumb" src="{link_for(thumb)}" alt="{esc(summary["title"])}"></button>'
        )
    elif summary.get("video") and Path(summary["video"]).exists():
        thumb_html = f'<video class="asset-thumb" preload="metadata" muted src="{link_for(summary["video"])}"></video>'
    else:
        thumb_html = '<div class="asset-thumb"></div>'
    actions = [
        f'<a class="button" href="{view_for(summary["project_dir"], "assets")}">Project</a>',
        f'<a class="button secondary" href="{view_for(summary["renders"], "assets")}">Renders</a>',
        f'<a class="button secondary" href="{view_for(summary["review"], "assets")}">Review</a>',
    ]
    if summary.get("video") and Path(summary["video"]).exists():
        actions.insert(0, f'<a class="button" href="{view_for(summary["video"], "assets")}">Final video</a>')
    return f"""
    <article class="panel asset-card">
      {thumb_html}
      <div>
        <h2>{esc(summary["title"])}</h2>
        <div class="hint">{esc(summary["slug"])} - {esc(summary["created_at"])}</div>
      </div>
      <div class="asset-meta">
        <span class="asset-pill">{summary["web_images"]} web</span>
        <span class="asset-pill">{summary["gpt_images"]} GPT</span>
        <span class="asset-pill">{summary["seedance"]} Seedance</span>
      </div>
      <div class="asset-actions">{"".join(actions)}</div>
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
    <div class="top">
      <div class="brand">
        <img class="brand-mark" src="/static/app_icon.png" alt="" width="56" height="56">
        <div>
          <h1>Asset Library</h1>
          <p class="sub">{count} project{"" if count == 1 else "s"} &mdash; final renders, review sheets, GPT &amp; web images, Seedance clips and speaker faces, all in one place.</p>
        </div>
      </div>
      <div class="nav-actions">
        <a class="button" href="/">&#43; New Project</a>
        <a class="button secondary" href="/sfx">&#128266; Add SFX to a Video</a>
      </div>
    </div>
    <section class="asset-grid">{cards}</section>
    """
    return page("Asset Library", body)


TIMELINE_SKELETON = """
<div id="timeline-root" data-slug="__SLUG__">
  <div class="tl-toolbar panel">
    <button type="button" class="button primary tl-render-btn" id="tl-render">&#127902; Render final video</button>
    <div class="tl-total" id="tl-total"></div>
    <div class="hint tl-help">Drag a clip to reorder, drag its right edge to trim. Click a clip to select it &mdash; then trim, remove, or ask the agent to replace it.</div>
  </div>
  <div class="tl-grid tl-top">
    <div class="panel tl-player">
      <h2>Preview</h2>
      <div class="tl-stage-view" id="tl-stage-view">
        <img id="tl-pimg" alt="">
        <video id="tl-pvid" muted playsinline></video>
        <div class="tl-stage-empty" id="tl-stage-empty">Press play to preview the sequence</div>
      </div>
      <div class="tl-player-bar">
        <button type="button" class="button secondary" id="tl-play">&#9654; Play</button>
        <span class="tl-playtime" id="tl-playtime">0:00 / 0:00</span>
        <span class="hint">Plays the real footage in order at the trimmed timing. Captions, voice &amp; SFX are added at render.</span>
      </div>
    </div>
    <div class="panel tl-inspector" id="tl-inspector">
      <h2>Inspector</h2>
      <div class="hint" id="tl-insp-empty">Click a clip in the timeline to edit it.</div>
      <div id="tl-insp-body" hidden>
        <div class="tl-insp-name" id="tl-insp-name"></div>
        <label>Duration (seconds)</label>
        <input type="number" id="tl-insp-dur" min="0.5" max="20" step="0.1">
        <div class="tl-insp-actions">
          <button type="button" class="button secondary" id="tl-insp-remove">Remove clip</button>
          <button type="button" class="button" id="tl-insp-replace" hidden>&#129302; Replace via agent</button>
        </div>
        <div class="hint" id="tl-insp-note">Removing a clip drops it from the render and re-flows the timeline.</div>
      </div>
    </div>
  </div>
  <div class="panel tl-stage">
    <div class="tl-rows">
      <div class="tl-row-labels">
        <div class="tl-rlabel" style="height:22px"></div>
        <div class="tl-rlabel">Clips</div>
        <div class="tl-rlabel tl-rlabel-sm">Captions</div>
        <div class="tl-rlabel tl-rlabel-sm">Voice</div>
        <div class="tl-rlabel tl-rlabel-sm">SFX / Music</div>
      </div>
      <div class="tl-scroll" id="tl-scroll">
        <div class="tl-playhead" id="tl-playhead"></div>
        <div class="tl-ruler" id="tl-ruler"></div>
        <div class="tl-track" id="tl-clips"></div>
        <div class="tl-track tl-cap" id="tl-captions"></div>
        <div class="tl-track tl-aud" id="tl-voice"></div>
        <div class="tl-track tl-aud" id="tl-sfx"></div>
      </div>
    </div>
  </div>
  <div class="panel tl-mixer-wrap">
    <h2>Audio mixer</h2>
    <div class="tl-mixer">
      <div class="tl-slider"><label>Voice <span id="tl-v-voice"></span></label><input type="range" id="tl-voice-vol" min="0" max="1.5" step="0.05"></div>
      <div class="tl-slider"><label>Clip audio <span id="tl-v-seedance"></span></label><input type="range" id="tl-seedance-vol" min="0" max="1" step="0.02"></div>
      <div class="tl-slider"><label>Sound effects <span id="tl-v-sfx"></span></label><input type="range" id="tl-sfx-vol" min="0" max="0.6" step="0.01"></div>
      <div class="tl-slider"><label>Music <span id="tl-v-music"></span></label><input type="range" id="tl-music-vol" min="0" max="0.6" step="0.01"></div>
    </div>
  </div>
</div>
"""

TIMELINE_ASSETS = """
<style>
  .tl-toolbar { display:flex; align-items:center; gap:16px; flex-wrap:wrap; margin-bottom:16px; }
  .tl-render-btn { width:auto; min-width:230px; font-size:16px; }
  .tl-total { font-weight:900; color:#ffe6ad; }
  .tl-help { flex:1; min-width:220px; margin:0; }
  .tl-grid { display:grid; grid-template-columns:1fr 1fr; gap:16px; }
  .tl-top { margin-bottom:16px; }
  .tl-stage-view { position:relative; width:100%; aspect-ratio:16/9; background:#06080a; border:1px solid var(--line); border-radius:8px; overflow:hidden; display:flex; align-items:center; justify-content:center; }
  .tl-stage-view img, .tl-stage-view video { max-width:100%; max-height:100%; width:100%; height:100%; object-fit:contain; display:none; background:#000; }
  .tl-stage-empty { position:absolute; color:#7e8884; font-weight:700; }
  .tl-player-bar { display:flex; align-items:center; gap:12px; margin-top:12px; flex-wrap:wrap; }
  .tl-player-bar .button { width:auto; min-width:96px; }
  .tl-playtime { font-weight:800; color:#ffe6ad; font-variant-numeric:tabular-nums; }
  .tl-stage { overflow:hidden; }
  .tl-rows { display:flex; gap:10px; }
  .tl-row-labels { display:flex; flex-direction:column; gap:8px; flex:0 0 auto; }
  .tl-rlabel { height:64px; display:flex; align-items:center; font-weight:800; color:#9aa4a1; font-size:12px; text-transform:uppercase; letter-spacing:.5px; }
  .tl-rlabel-sm { height:40px; }
  .tl-scroll { position:relative; overflow-x:auto; flex:1; min-width:0; padding-bottom:10px; }
  .tl-ruler { position:relative; height:22px; cursor:pointer; }
  .tl-tick { position:absolute; top:0; height:22px; border-left:1px solid #2a343a; padding-left:4px; font-size:10px; color:#7e8884; }
  .tl-playhead { position:absolute; top:0; bottom:10px; width:2px; background:var(--accent-3, #ff5d5d); z-index:5; pointer-events:none; box-shadow:0 0 6px rgba(255,93,93,.8); }
  .tl-playhead::before { content:''; position:absolute; top:0; left:-4px; border-left:5px solid transparent; border-right:5px solid transparent; border-top:7px solid var(--accent-3,#ff5d5d); }
  .tl-track { position:relative; height:64px; margin-top:8px; background:#0d1114; border:1px solid var(--line); border-radius:6px; }
  .tl-track.tl-cap, .tl-track.tl-aud { height:40px; }
  .tl-clip { position:absolute; top:3px; bottom:3px; border:1px solid var(--accent-2); border-radius:6px; overflow:hidden; cursor:grab; background:#1a1407; background-size:cover; background-position:center; touch-action:none; }
  .tl-clip.selected { border-color:var(--accent); box-shadow:0 0 0 2px rgba(240,180,95,.55); }
  .tl-clip.dragging { opacity:.75; cursor:grabbing; z-index:6; }
  .tl-clip .tl-clip-label { position:absolute; left:0; right:0; bottom:0; padding:3px 6px; font-size:11px; font-weight:800; color:#fff; background:linear-gradient(transparent, rgba(0,0,0,.82)); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .tl-clip .tl-clip-dur { position:absolute; top:2px; left:5px; font-size:10px; font-weight:900; color:#fff; text-shadow:0 1px 3px #000; }
  .tl-clip .tl-handle { position:absolute; top:0; right:0; bottom:0; width:11px; cursor:ew-resize; background:linear-gradient(90deg, transparent, rgba(240,180,95,.6)); }
  .tl-capblock { position:absolute; top:6px; bottom:6px; border-radius:4px; background:#13311f; border:1px solid #2f7d49; }
  .tl-audbar { position:absolute; top:6px; bottom:6px; left:0; right:0; border-radius:4px; background:repeating-linear-gradient(90deg,#1a2a33 0 6px,#16242c 6px 12px); border:1px solid #2a3a44; }
  .tl-mixer-wrap { margin-top:16px; }
  .tl-mixer { display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:16px; }
  .tl-slider label { display:flex; justify-content:space-between; margin-bottom:6px; }
  .tl-insp-name { font-weight:800; color:#fff8eb; margin-bottom:12px; }
  .tl-insp-actions { display:flex; gap:8px; flex-wrap:wrap; margin:12px 0 8px; }
  .tl-insp-actions .button { width:auto; flex:1; min-width:140px; }
  @media (max-width:760px){ .tl-grid{ grid-template-columns:1fr; } }
</style>
<script>
(function(){
  var rootEl = document.getElementById('timeline-root');
  if (!rootEl) return;
  var model;
  try { model = JSON.parse(document.getElementById('timeline-model').textContent); } catch(e){ return; }
  var slug = rootEl.getAttribute('data-slug');
  var scenes = (model.scenes||[]).map(function(s){ return Object.assign({}, s); });
  var volumes = Object.assign({voice:1, seedance:0.16, sfx:0.075, music:0}, model.volumes||{});
  var SCALE = 30;
  var selectedId = null;
  var clips=document.getElementById('tl-clips'), caps=document.getElementById('tl-captions'), ruler=document.getElementById('tl-ruler');
  var playhead=document.getElementById('tl-playhead');
  function esc(t){ return String(t==null?'':t).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }
  function visible(){ return scenes.filter(function(s){ return !s.removed; }); }
  function totalDur(){ return visible().reduce(function(a,s){ return a + s.dur; }, 0); }
  function fmt(t){ t=Math.max(0,t); var m=Math.floor(t/60), s=Math.floor(t%60); return m+':'+(s<10?'0':'')+s; }

  function layout(){
    var total=totalDur(), width=Math.max(640, total*SCALE);
    [clips,caps,ruler,document.getElementById('tl-voice'),document.getElementById('tl-sfx')].forEach(function(el){ if(el) el.style.width=width+'px'; });
    ruler.innerHTML='';
    for(var t=0;t<=total+0.01;t+=5){ var d=document.createElement('div'); d.className='tl-tick'; d.style.left=(t*SCALE)+'px'; d.textContent=fmt(t); ruler.appendChild(d); }
    clips.innerHTML=''; caps.innerHTML='';
    var x=0;
    visible().forEach(function(s){
      var w=s.dur*SCALE;
      var b=document.createElement('div');
      b.className='tl-clip'+(s.id===selectedId?' selected':'');
      b.style.left=x+'px'; b.style.width=w+'px';
      b.setAttribute('data-id', s.id);
      if(s.thumb) b.style.backgroundImage='url('+s.thumb+')';
      b.innerHTML='<span class="tl-clip-dur">'+s.dur.toFixed(1)+'s</span><span class="tl-clip-label">'+(s.speaker?'(speaker) ':'')+esc(s.label)+'</span><span class="tl-handle"></span>';
      b.querySelector('.tl-handle').addEventListener('pointerdown', function(ev){ ev.stopPropagation(); startResize(ev, s); });
      b.addEventListener('pointerdown', function(ev){ if(ev.target.classList.contains('tl-handle')) return; startClipDrag(ev, s, b); });
      clips.appendChild(b);
      if(s.caption){ var c=document.createElement('div'); c.className='tl-capblock'; c.style.left=(x+2)+'px'; c.style.width=Math.max(2,w-4)+'px'; caps.appendChild(c); }
      x+=w;
    });
    document.getElementById('tl-voice').innerHTML='<div class="tl-audbar"></div>';
    document.getElementById('tl-sfx').innerHTML='<div class="tl-audbar"></div>';
    document.getElementById('tl-total').textContent='Total '+fmt(total)+'  -  '+visible().length+' clips';
    updatePlayhead();
  }

  function startResize(ev, s){
    ev.preventDefault();
    var startX=ev.clientX, startDur=s.dur;
    function move(e){ var dd=(e.clientX-startX)/SCALE; s.dur=Math.max(0.5, Math.min(20, +(startDur+dd).toFixed(1))); layout(); if(selectedId===s.id) syncInspector(); }
    function up(){ document.removeEventListener('pointermove',move); document.removeEventListener('pointerup',up); }
    document.addEventListener('pointermove',move); document.addEventListener('pointerup',up);
  }

  function startClipDrag(ev, s, block){
    ev.preventDefault();
    var startX=ev.clientX, dragging=false;
    function move(e){
      if(!dragging && Math.abs(e.clientX-startX) < 6) return;
      dragging=true; block.classList.add('dragging');
      var rect=clips.getBoundingClientRect();
      var px=e.clientX-rect.left+clips.scrollLeft;
      var vis=visible(), acc=0, target=vis.length-1;
      for(var i=0;i<vis.length;i++){ var w=vis[i].dur*SCALE; if(px < acc+w/2){ target=i; break; } acc+=w; if(i===vis.length-1) target=vis.length-1; }
      var order=scenes.filter(function(x){return !x.removed;});
      var from=order.indexOf(s);
      if(from!==-1 && from!==target){
        order.splice(from,1); order.splice(target,0,s);
        var removed=scenes.filter(function(x){return x.removed;});
        scenes=order.concat(removed);
        layout(); block=clips.querySelector('.tl-clip[data-id="'+s.id+'"]'); if(block) block.classList.add('dragging');
      }
    }
    function up(){
      document.removeEventListener('pointermove',move); document.removeEventListener('pointerup',up);
      var b=clips.querySelector('.tl-clip[data-id="'+s.id+'"]'); if(b) b.classList.remove('dragging');
      if(!dragging) select(s.id);
    }
    document.addEventListener('pointermove',move); document.addEventListener('pointerup',up);
  }

  function select(id){ selectedId=id; layout(); syncInspector(); }
  function syncInspector(){
    var s=scenes.filter(function(x){return x.id===selectedId;})[0];
    var empty=document.getElementById('tl-insp-empty'), body=document.getElementById('tl-insp-body');
    if(!s||s.removed){ empty.hidden=false; body.hidden=true; return; }
    empty.hidden=true; body.hidden=false;
    document.getElementById('tl-insp-name').textContent=s.label;
    document.getElementById('tl-insp-dur').value=s.dur;
    var rep=document.getElementById('tl-insp-replace');
    rep.hidden=!s.replaceable;
    document.getElementById('tl-insp-note').textContent = s.replaceable
      ? 'Replace via agent re-searches a fresh web image for this clip, then re-renders.'
      : 'Removing a clip drops it from the render and re-flows the timeline.';
  }
  document.getElementById('tl-insp-dur').addEventListener('input', function(){ var s=scenes.filter(function(x){return x.id===selectedId;})[0]; if(s){ s.dur=Math.max(0.5,Math.min(20, parseFloat(this.value)||s.dur)); layout(); } });
  document.getElementById('tl-insp-remove').addEventListener('click', function(){ var s=scenes.filter(function(x){return x.id===selectedId;})[0]; if(s){ s.removed=true; selectedId=null; layout(); syncInspector(); } });
  document.getElementById('tl-insp-replace').addEventListener('click', function(){
    var s=scenes.filter(function(x){return x.id===selectedId;})[0];
    if(!s || !s.replaceable || !s.path) return;
    if(!confirm('Ask the agent to find a fresh web image for this clip and re-render the video?')) return;
    var btn=this; btn.disabled=true; btn.textContent='Starting...';
    fetch('/timeline-replace',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({slug:slug, media_path:s.path})})
      .then(function(r){return r.json();})
      .then(function(d){ if(d&&d.ok&&d.job){ window.location.href=d.job; } else { btn.disabled=false; btn.innerHTML='&#129302; Replace via agent'; alert((d&&d.error)||'Could not start replacement.'); } })
      .catch(function(){ btn.disabled=false; btn.innerHTML='&#129302; Replace via agent'; alert('Could not start replacement.'); });
  });

  // ---- preview player ----
  var pimg=document.getElementById('tl-pimg'), pvid=document.getElementById('tl-pvid'), pempty=document.getElementById('tl-stage-empty');
  var playing=false, clock=0, lastTs=0, curIdx=-1;
  function sceneAt(t){ var vis=visible(), acc=0; for(var i=0;i<vis.length;i++){ if(t < acc+vis[i].dur){ return {scene:vis[i], idx:i, local:t-acc}; } acc+=vis[i].dur; } return vis.length? {scene:vis[vis.length-1], idx:vis.length-1, local:0} : null; }
  function showScene(info){
    if(!info){ pimg.style.display='none'; pvid.style.display='none'; pempty.style.display='block'; return; }
    pempty.style.display='none';
    if(curIdx===info.idx) return;
    curIdx=info.idx;
    var s=info.scene;
    if(s.video && s.thumb){ pimg.style.display='none'; pvid.style.display='block'; try{ pvid.src=s.thumb; pvid.currentTime=0; if(playing) pvid.play().catch(function(){}); }catch(e){} }
    else { pvid.pause(); pvid.style.display='none'; pimg.style.display='block'; pimg.src=s.thumb||''; }
  }
  function updatePlayhead(){
    playhead.style.left=(clock*SCALE)+'px';
    document.getElementById('tl-playtime').textContent=fmt(clock)+' / '+fmt(totalDur());
  }
  function tick(ts){
    if(!playing) return;
    var dt=(ts-lastTs)/1000; lastTs=ts; clock+=dt;
    var total=totalDur();
    if(clock>=total){ clock=total; updatePlayhead(); stop(); return; }
    showScene(sceneAt(clock)); updatePlayhead();
    requestAnimationFrame(tick);
  }
  function play(){ if(playing) return; if(clock>=totalDur()-0.05){ clock=0; curIdx=-1; } playing=true; lastTs=performance.now(); document.getElementById('tl-play').innerHTML='&#10073;&#10073; Pause'; showScene(sceneAt(clock)); requestAnimationFrame(tick); }
  function stop(){ playing=false; pvid.pause(); document.getElementById('tl-play').innerHTML='&#9654; Play'; }
  document.getElementById('tl-play').addEventListener('click', function(){ if(playing) stop(); else play(); });
  ruler.addEventListener('click', function(e){ var rect=ruler.getBoundingClientRect(); clock=Math.max(0,Math.min(totalDur(),(e.clientX-rect.left+ruler.scrollLeft)/SCALE)); curIdx=-1; showScene(sceneAt(clock)); updatePlayhead(); });

  function bindVol(id, key, out){
    var el=document.getElementById(id), o=document.getElementById(out);
    el.value=volumes[key]; o.textContent=Math.round(volumes[key]*100)+'%';
    el.addEventListener('input', function(){ volumes[key]=parseFloat(this.value); o.textContent=Math.round(volumes[key]*100)+'%'; });
  }
  bindVol('tl-voice-vol','voice','tl-v-voice');
  bindVol('tl-seedance-vol','seedance','tl-v-seedance');
  bindVol('tl-sfx-vol','sfx','tl-v-sfx');
  bindVol('tl-music-vol','music','tl-v-music');

  document.getElementById('tl-render').addEventListener('click', function(){
    var btn=this; btn.disabled=true; btn.textContent='Starting render...';
    var vis=visible();
    var edits={ scenes: vis.map(function(s){return {id:s.id, duration:s.dur};}),
                order: vis.map(function(s){return s.id;}),
                removed: scenes.filter(function(s){return s.removed;}).map(function(s){return s.id;}),
                volumes: volumes };
    fetch('/timeline-render',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({slug:slug, edits:edits})})
      .then(function(r){return r.json();})
      .then(function(d){ if(d&&d.ok&&d.job){ window.location.href=d.job; } else { btn.disabled=false; btn.textContent='Render final video'; alert((d&&d.error)||'Could not start render.'); } })
      .catch(function(){ btn.disabled=false; btn.textContent='Render final video'; alert('Could not start render.'); });
  });
  layout(); updatePlayhead();
})();
</script>
"""


def _timeline_thumb(project_dir, scene):
    """Return (thumb_url, is_video, resolved_path_or_None) for a scene's media."""
    asset = scene.get("asset")
    clip = scene.get("clip")
    if asset:
        direct = Path(asset)
        if direct.is_absolute() and direct.exists():
            return link_for(direct), is_video_path(direct), direct
        for sub in ("web images", "gpt images", "local media", "speaker", "seedance 2.0"):
            candidate = project_dir / sub / asset
            if candidate.exists():
                return link_for(candidate), is_video_path(candidate), candidate
    if clip:
        candidate = project_dir / "seedance 2.0" / clip
        if candidate.exists():
            return link_for(candidate), True, candidate
    return "", False, None


def timeline_model(slug):
    config = agent_core.load_project_config(slug)
    project_dir = agent_core.PROJECTS_DIR / slug
    scenes = []
    for index, scene in enumerate(config.get("scenes", [])):
        start = float(scene.get("start", 0) or 0)
        end = float(scene.get("end", start) or start)
        dur = max(0.3, end - start)
        thumb, is_video, resolved = _timeline_thumb(project_dir, scene)
        label = scene.get("name") or scene.get("caption") or scene.get("script") or f"Scene {index + 1}"
        label = re.sub(r"\s+", " ", str(label)).strip()[:54] or f"Scene {index + 1}"
        replaceable = bool(resolved and "web images" in {p.lower() for p in resolved.parts}
                           and not scene.get("speaker_hook"))
        scenes.append({
            "id": str(scene.get("id", index)),
            "label": label,
            "dur": round(dur, 2),
            "thumb": thumb,
            "video": bool(is_video),
            "path": str(resolved) if resolved else "",
            "replaceable": replaceable,
            "caption": bool(scene.get("caption") or scene.get("render_caption") or scene.get("script")),
            "speaker": bool(scene.get("speaker_hook")),
        })
    volumes = {
        "voice": float(config.get("audio_master_gain", 1.0) or 1.0),
        "seedance": float(config.get("seedance_audio_volume", 0.16) or 0.0),
        "sfx": float(config.get("sfx_volume", 0.075) or 0.0),
        "music": float(config.get("background_music_volume", 0.0) or 0.0),
    }
    return {
        "slug": slug,
        "title": config.get("title", slug),
        "duration": round(float(config.get("duration", 0) or 0), 2),
        "scenes": scenes,
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
        return page("Timeline Editor", '<div class="top"><div class="brand"><h1>Timeline Editor</h1></div></div><section class="panel"><div class="hint">Unknown project.</div></section>')
    try:
        model = timeline_model(slug)
    except Exception as exc:
        return page("Timeline Editor", f'<div class="top"><div class="brand"><h1>Timeline Editor</h1></div></div><section class="panel"><div class="hint">Could not load timeline: {esc(str(exc))}</div></section>')
    header = f"""
    <div class="top">
      <div class="brand">
        <img class="brand-mark" src="/static/app_icon.png" width="56" height="56" alt="">
        <div><h1>Timeline Editor</h1><p class="sub">{esc(model['title'])} &mdash; trim clips, set volumes, preview the order, then render the final video.</p></div>
      </div>
      <div class="nav-actions">
        <a class="button secondary" href="/">&#43; New Project</a>
        <a class="button secondary" href="/assets">&#127916; Asset Library</a>
      </div>
    </div>
    """
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
    if status in {"running", "cancelling"}:
        cancel_html = (
            f'<form class="inline-form" method="post" action="/cancel?id={urllib.parse.quote(job_id)}">'
            f'<button class="danger" type="submit">Cancel run</button></form>'
        )
    timeline_html = ""
    proj = job.get("project_dir")
    if proj and Path(proj).exists() and job.get("job_kind") != "sfx":
        tslug = Path(proj).name
        timeline_html = f'<a class="button" href="/timeline?slug={urllib.parse.quote(tslug)}">&#127902; Timeline editor</a>'
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
            self.send_bytes(form_page())
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
