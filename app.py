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


ROOT = Path(__file__).resolve().parent
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
}
UI_CHECKBOX_DEFAULTS = {
    "autonomous_director": True,
    "use_audio_timing": True,
    "use_llm_search": True,
    "use_llm_video_review": True,
    "enable_speaker_hook": False,
    "auto_web_images": True,
    "background_music_enabled": False,
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
                state[key] = str(raw[key])
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
      main { max-width: 95vw; margin: 0 auto; padding: 30px; }
      h1 { font-size: 31px; margin: 0; letter-spacing: 0; text-shadow: 0 1px 0 #000; }
      h2 { font-size: 18px; margin: 0 0 12px; color: #fff8eb; }
      .top { display: flex; justify-content: space-between; gap: 16px; align-items: flex-start; border-bottom: 1px solid var(--line); padding-bottom: 18px; margin-bottom: 22px; }
      .sub { color: var(--muted); margin-top: 7px; max-width: 780px; line-height: 1.45; }
      .nav-actions { display: flex; gap: 10px; flex-wrap: wrap; justify-content: flex-end; min-width: 170px; }
      .nav-actions .button { width: auto; min-width: 132px; }
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
      .job-layout { display: grid; grid-template-columns: minmax(0, 1fr) minmax(280px, 360px); gap: 16px; align-items: stretch; margin-top: 16px; }
      .job-layout > .panel, .job-layout > aside { min-height: min(76vh, 860px); }
      .job-layout { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 16px; align-items: stretch; }
      .job-layout > .panel, .job-layout > aside { min-width: 0; }
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
      .loaded-media-panel { max-height: 720px; overflow: auto; }
      .loaded-media-panel .media-tiny-grid { grid-template-columns: repeat(auto-fit, minmax(112px, 1fr)); }
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
    </style>
    """


def app_script():
    return """
    <script>
      (function () {
        window.setRunMode = function(mode) {
          var form = document.getElementById("short-form");
          if (!form) return;
          if (mode === "quality_run") {
            form.querySelector("[name='autonomous_director']").checked = true;
            form.querySelector("[name='use_llm_search']").checked = true;
            form.querySelector("[name='use_llm_video_review']").checked = true;
            form.querySelector("[name='allow_seedance']").checked = true;
            form.querySelector("[name='allow_gpt']").checked = true;
            form.querySelector("[name='allow_gpt']").checked = true;
          } else if (mode === "repair_recut") {
            document.getElementById("load-modal").classList.add("active");
            var select = document.getElementById("project-select");
            if (select) select.focus();
          }
          document.querySelectorAll(".run-mode-btn").forEach(function(b) {
            b.classList.remove("primary");
            b.classList.add("secondary");
          });
          var activeBtn = document.getElementById("btn-mode-" + mode);
          if (activeBtn) {
            activeBtn.classList.remove("secondary");
            activeBtn.classList.add("primary");
          }
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
            var replace = item.replaceable
              ? '<label><input type="checkbox" name="initial_replace_media_path" value="' + path + '"> Replace</label> ' +
                '<label><input type="checkbox" name="initial_remove_media_path" value="' + path + '"> Remove</label>'
              : '<label>' + kind + '</label> ' +
                '<label><input type="checkbox" name="initial_remove_media_path" value="' + path + '"> Remove</label>';
            return [
              '<article class="media-tile">',
              '<span class="media-kind">' + kind + '</span>',
              '<button class="preview-button" type="button" data-preview-src="' + url + '" data-preview-title="' + name + '">',
              preview + '</button>',
              replace,
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
          clearProjectMedia();
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
            master.gain.exponentialRampToValueAtTime(0.18, now + 0.018);
            master.gain.exponentialRampToValueAtTime(0.0001, now + 0.32);
            master.connect(ctx.destination);
            [660, 990].forEach(function (freq, index) {
              var osc = ctx.createOscillator();
              var gain = ctx.createGain();
              osc.type = "sine";
              osc.frequency.setValueAtTime(freq, now + index * 0.055);
              gain.gain.setValueAtTime(0.0001, now + index * 0.055);
              gain.gain.exponentialRampToValueAtTime(1.0, now + index * 0.055 + 0.018);
              gain.gain.exponentialRampToValueAtTime(0.0001, now + index * 0.055 + 0.22);
              osc.connect(gain);
              gain.connect(master);
              osc.start(now + index * 0.055);
              osc.stop(now + index * 0.055 + 0.26);
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
        '<meta name="application-name" content="Autonomous Shorts Agent">'
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

    def checked(name):
        return " checked" if state.get(name) else ""

    body = f"""
    <div class="top">
      <div>
        
        
      </div>
      <div class="nav-actions">
        <button type="button" class="button secondary" onclick="document.getElementById('load-modal').classList.add('active')">Load Project</button>
        <a class="button secondary" href="/assets">Assets</a>
      </div>
    </div>
    <form id="short-form" method="post" action="/run" enctype="multipart/form-data">
      <input id="loaded-project-source" type="hidden" name="loaded_project_source" value="">
      
      <section class="stack">
        <div class="panel mode-selector" style="text-align: center; margin-bottom: 20px;">
          <label style="font-size: 1.2em; margin-bottom: 10px; display: block;">Run Mode</label>
          <div style="display: flex; gap: 10px; justify-content: center;">
            <button type="button" id="btn-mode-quality_run" class="button primary run-mode-btn" onclick="window.setRunMode('quality_run')">Quality Run</button>
            <button type="button" id="btn-mode-repair_recut" class="button secondary run-mode-btn" onclick="window.setRunMode('repair_recut')">Repair / Recut</button>
          </div>
          <div id="mode-description" class="hint" style="margin-top: 10px;">Quality Run: Auto Director, GPT review, full search, and Seedance generation.</div>
        </div>

        <div class="panel accent">
          <label>Text script</label>
          <textarea name="script" placeholder="Paste timed/plain text script here, or leave empty when uploading speech audio.">{esc(state.get("script"))}</textarea>
        </div>
        <div class="panel">
          <label>Optional Visual Direction</label>
          <textarea class="visual-textarea" name="visual_script" placeholder="Optional. Leave empty if you want the agent to create the visual plan automatically from the Voice Script. Use this only for style or direction, e.g. darker documentary style, faster cuts, more maps, more archive images, more dramatic reconstructions.">{esc(state.get("visual_script"))}</textarea>
          <div class="hint">The Voice Script is authoritative. The Optional Visual Direction is secondary guidance. If empty, the full visual plan is inferred from the Voice Script.</div>
        </div>

        <div class="panel">
          <label>Loaded project action / regenerate</label>
          <select name="loaded_project_mode">
            <option value="normal"{' selected' if state.get("loaded_project_mode") == "normal" else ""}>Normal agent run in this folder</option>
            <option value="recut_existing_only"{' selected' if state.get("loaded_project_mode") == "recut_existing_only" else ""}>Replace selected media + recut existing</option>
            <option value="recut_new_web_images"{' selected' if state.get("loaded_project_mode") == "recut_new_web_images" else ""}>Regenerate web/wiki images + recut</option>
            <option value="recut_regenerate_seedance"{' selected' if state.get("loaded_project_mode") == "recut_regenerate_seedance" else ""}>Regenerate Seedance clips + recut</option>
            <option value="recut_recreate_speaker_clip"{' selected' if state.get("loaded_project_mode") == "recut_recreate_speaker_clip" else ""}>Recreate speaker hook clip + recut</option>
          </select>
          <div class="hint">For old projects, choose what should be reused or regenerated. Selected web/wiki images are replaced before the recut.</div>
        </div>

        <div class="panel">
          <label>Speech audio</label>
          <input type="file" name="audio_file" accept="audio/*,video/mp4,video/webm">
          <div class="hint">Optional. Gemini 3.5 Flash transcribes and creates timed visual beats. The uploaded speech audio is timing reference only and is not mixed into the final video.</div>
        </div>
        <div class="panel">
          <label>Speaker hook image</label>
          <input type="file" name="speaker_image_file" accept="image/*">
          <div class="checks">
            <label><input type="checkbox" name="enable_speaker_hook"{checked("enable_speaker_hook")}> Create Seedance speaker hook clip from uploaded image</label>
          </div>
          <div class="hint">Optional. Reasoning Agent analyzes the uploaded speaker image, then Seedance creates a close-up, energetic self-recorded hook clip with the uploaded person's appearance.</div>
        </div>

        <div class="panel">
          <label>Video Model</label>
          <select name="video_model">
            <option value="seedance-2.0"{' selected' if state.get("video_model", state.get("seedance_model", "seedance-2.0")) == "seedance-2.0" else ""}>Seedance 2.0 (Spicy + Web Search)</option>
            <option value="seedance-v1.5-pro"{' selected' if state.get("video_model", state.get("seedance_model")) == "seedance-v1.5-pro" else ""}>Seedance 1.5 Pro</option>
            <option value="ltx-2.3"{' selected' if state.get("video_model") == "ltx-2.3" else ""}>LTX-2.3</option>
            <option value="happyhorse-1.1"{' selected' if state.get("video_model") == "happyhorse-1.1" else ""}>Happy Horse 1.1 (720p)</option>
          </select>
          <div class="hint">Select the primary model used for generating video clips. (Speaker hook is always Seedance 2.0).</div>
        </div>

        <div class="panel">
          <label>Reasoning Model</label>
          <select name="reasoning_model">
            <option value="openai/gpt-5.5"{' selected' if state.get("reasoning_model") == "openai/gpt-5.5" else ""}>GPT-5.5 (Fast, standard)</option>
            <option value="anthropic/claude-opus-4.8"{' selected' if state.get("reasoning_model") == "anthropic/claude-opus-4.8" else ""}>Claude Opus 4.8 (Slower, higher quality)</option>
          </select>
          <div class="hint">Select the LLM that will act as the Auto Director, Video Reviewer, Search Planner, and Script Editor.</div>
        </div>

        <details class="panel">
          <summary style="cursor: pointer; font-weight: bold; padding: 5px;">Advanced options</summary>
          <div style="margin-top: 20px;">
            <label>Agent options</label>
            <div class="checks">
              <label><input type="checkbox" name="autonomous_director"{checked("autonomous_director")}> Auto Director decides APIs, media counts, and scene strategy</label>
              <label><input type="checkbox" name="use_audio_timing"{checked("use_audio_timing")}> Use Gemini audio timing when audio is uploaded</label>
              <label><input type="checkbox" name="use_llm_search"{checked("use_llm_search")}> Use Reasoning Agent to plan web image searches</label>
              <label><input type="checkbox" name="use_llm_video_review"{checked("use_llm_video_review")}> Use Reasoning Agent two-pass review and auto-correction</label>
              <label><input type="checkbox" name="auto_web_images"{checked("auto_web_images")}> Search and download web images automatically</label>
              <label><input type="checkbox" name="background_music_enabled"{checked("background_music_enabled")}> Add background music</label>
              <label><input type="checkbox" name="allow_gpt"{checked("allow_gpt")}> Generate GPT source images for Seedance when needed</label>
              <label><input type="checkbox" name="allow_seedance"{checked("allow_seedance")}> Generate missing Seedance clips</label>
            </div>
          </div>
        </details>
        <button type="submit">Create Short</button>
      </section>

      <section class="stack">
        <div class="loaded-media-panel" style="flex: 1; border: 1px solid var(--line); border-radius: 8px; padding: 20px;">
          <h2 style="margin-bottom: 5px;">Project Media Preview</h2>
          <div class="hint" id="project-media-hint" style="margin-bottom: 20px;">No project loaded. Click "Load Project" in the top bar to select a past project.</div>
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
    return page("Autonomous Shorts Agent", body)


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
        fields["speaker_image_path"] = speaker_image_path
    with JOB_LOCK:
        initial_logs = ["Queued."]
        if replace_requests:
            initial_logs.append(f"Queued {len(replace_requests)} initial image replacement request(s) from loaded project media.")
        if speaker_image_path:
            initial_logs.append("Speaker hook image uploaded.")
        JOBS[job_id] = {
            "status": "running",
            "logs": initial_logs,
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
        ("Rendering frames", 84),
        ("Mixing audio", 86),
        ("Encoding final MP4", 87),
        ("Creating review sheets", 88),
        ("Scene review sheet", 90),
        ("Shot review sheet", 92),
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


def render_progress(status, logs, created_at=None):
    percent, activity = progress_state(status, logs)
    elapsed = format_duration(time.time() - float(created_at or time.time()))
    return f"""
    <div id="job-progress-wrap" class="progress-wrap">
      <div class="elapsed-line"><span>Total time elapsed</span><strong>{esc(elapsed)}</strong></div>
      <div class="progress-label"><span>Current: {esc(activity)}</span><strong>{percent}%</strong></div>
      <div class="progress-track"><div class="progress-fill" style="width:{percent}%"></div></div>
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
    project_dir = project_dir_for_job(job)
    media_all = project_media_files(project_dir)
    replaceable = replaceable_media_files(project_dir)
    queued = queued_replacement_paths(job)
    if not project_dir:
        return '<section class="panel media-sidebar"><h2>Media Files</h2><div class="hint">Media appears here after the project folder is created.</div></section>'
    if not media_all:
        return '<section class="panel media-sidebar"><h2>Media Files</h2><div class="hint">Media appears here as soon as files exist.</div></section>'
    replaceable_set = {str(path.resolve()).lower() for _, path in replaceable}
    visible_items = [(kind, path) for kind, path in media_all if str(path.resolve()).lower() not in queued]
    queued_count = len(queued)
    tabs = media_tabs_html(visible_items, replaceable_paths=replaceable_set, queued_paths=queued, input_name="media_path")
    return f"""
    <section class="panel media-sidebar">
      <div class="panel-head">
        <h2>Media Files</h2>
      </div>
      <form method="post" action="/replace-media?id={urllib.parse.quote(job_id)}" data-replace-media-form="1">
        <div class="media-toolbar">
          <button class="secondary" type="submit">Queue selected replacements</button>
        </div>
        <div class="hint">Select web/wiki images while Seedance runs. Queued images disappear here and are replaced before rendering.{f" {queued_count} queued." if queued_count else ""}</div>
        {tabs}
      </form>
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
    cards = "".join(asset_card(project_summary(project)) for project in projects)
    if not cards:
        cards = '<section class="panel"><h2>No assets yet</h2><div class="hint">Finished runs and generated project folders will appear here.</div></section>'
    body = f"""
    <div class="top">
      <div>
        <h1>Assets</h1>
        <div class="sub">Previous runs, renders, review sheets, generated images, Seedance clips, and downloaded web media.</div>
      </div>
      <div class="nav-actions">
        <a class="button secondary" href="/">New project</a>
      </div>
    </div>
    <section class="asset-grid">{cards}</section>
    """
    return page("Assets", body)


def job_page(job_id):
    with JOB_LOCK:
        job = dict(JOBS.get(job_id, {"status": "missing", "logs": [], "result": None, "error": "Unknown job"}))
    status = job["status"]
    klass = "done" if status == "done" else "error" if status == "error" else "cancelled" if status == "cancelled" else "cancelling" if status == "cancelling" else ""
    logs = job.get("logs", [])
    progress_html = render_progress(status, logs, job.get("created_at"))
    result_html = render_outputs(job.get("result"), job_id)
    error_html = f'<section class="panel"><h2>Error</h2><pre>{esc(job.get("error"))}</pre></section>' if job.get("error") else ""
    media_html = render_media_replacer(job_id, job)
    cancel_html = ""
    if status in {"running", "cancelling"}:
        cancel_html = (
            f'<form class="inline-form" method="post" action="/cancel?id={urllib.parse.quote(job_id)}">'
            f'<button class="danger" type="submit">Cancel run</button></form>'
        )
    body = f"""
    <div id="job-root" data-status="{esc(status)}" data-job-id="{esc(job_id)}">
      <div class="job-head">
        <div>
          <h1>Job</h1>
          <p>Status: <span id="job-status-label" class="status {klass}">{esc(status)}</span></p>
        </div>
        <div class="job-actions">
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
        "progress_html": render_progress(status, logs, job.get("created_at")),
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
