"""Shortslab shell v3 - the dense run page + the wait screen. Server side.

Reached with ?ui=v3 on the routed pages. Purely additive: the classic chat shell and the
legacy forms are untouched, and every submission from static/shell-v3.js goes to the SAME
routes with the SAME field names (RUN_MANIFEST / MASTER_MANIFESTS in chat_ui.py).

What lives here and nowhere else:
- the v3 document (one stylesheet, one script, same boot JSON as the chat shell plus the
  saved draft so the page can offer Resume / Start fresh without a second request);
- the per-step typical durations. The job log already timestamps every line, and
  app.compute_step_view turns that into real per-step durations; this module remembers them
  for finished runs so the next run's wait screen can say "Director: 3:20, typically 2:40";
- the run-health counters parsed from the log (candidates inspected, rejections, queries,
  beats covered) so a run that is going wrong is visible long before it ends.
"""

from __future__ import annotations

import json
import re
import statistics
import threading
import time
from pathlib import Path

import chat_ui
import reasoning_modes

ROOT = Path(__file__).resolve().parent
STEP_HISTORY_PATH = ROOT / "step_history.json"
SHELL_V3_VERSION = "shell-3.0"
HISTORY_KEEP = 12                      # durations remembered per step and run kind

_HISTORY_LOCK = threading.Lock()
_RECORDED_JOBS = set()


def asset_ver():
    """mtime cache-buster for the v3 assets (same idea as chat_ui._asset_ver, own files)."""
    try:
        return str(int(max((ROOT / "static" / name).stat().st_mtime
                           for name in ("shell-v3.css", "shell-v3.js", "showroom3d/scene.js", "showroom3d/showroom.css"))))
    except Exception:
        return str(int(time.time()))


# ------------------------------------------------------------------ step history

def _load_history():
    try:
        data = json.loads(STEP_HISTORY_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def history_key(job, logs=None):
    """Which population a run's step durations belong to. A scrape Clip Short and a generated
    AI Short share step NAMES but not durations, so they are kept apart."""
    kind = str(job.get("job_kind") or "run").lower() or "run"
    if kind != "run":
        return kind
    try:
        import app
        if app._is_discovery_run(logs or job.get("logs") or []):
            return "run:discovery"
    except Exception:
        pass
    source = str(job.get("clip_source") or "").lower()
    if not source:
        try:
            import app
            source = "scrape" if app._is_scrape_run(logs or job.get("logs") or []) else "generate"
        except Exception:
            source = "generate"
    return f"run:{source or 'generate'}"


def typical_durations(key):
    """{step name: median seconds} for one run kind, from the runs remembered so far."""
    rows = _load_history().get(key) or {}
    out = {}
    for name, values in rows.items():
        nums = [float(v) for v in values if isinstance(v, (int, float)) and v >= 0]
        if nums:
            out[name] = round(statistics.median(nums), 1)
    return out


def record_step_durations(job_id, key, steps):
    """Remember the finished steps of one run (once per job id). Best-effort, never raises."""
    with _HISTORY_LOCK:
        if job_id in _RECORDED_JOBS:
            return False
        _RECORDED_JOBS.add(job_id)
        data = _load_history()
        bucket = data.setdefault(key, {})
        changed = False
        for step in steps:
            if step.get("state") != "done" or step.get("elapsed") is None:
                continue
            values = bucket.setdefault(step["name"], [])
            values.append(round(float(step["elapsed"]), 1))
            del values[:-HISTORY_KEEP]
            changed = True
        if not changed:
            return False
        try:
            tmp = STEP_HISTORY_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
            tmp.replace(STEP_HISTORY_PATH)
        except Exception:
            return False
        return True


# ------------------------------------------------------------------ run health

_HEALTH_PATTERNS = {
    "checked": (re.compile(r"V4 source gate: inspecting "),
                re.compile(r"Discovery: trying @")),
    "rejected": (re.compile(r"V4 source gate: rejected "),
                 re.compile(r"Discovery: skipped @"),
                 re.compile(r"Discovery: rejected by vision review")),
    "accepted": (re.compile(r"Downloaded accepted candidate"),
                 re.compile(r"Discovery: candidate \d+/\d+ accepted")),
}
_DELIVERED_RE = re.compile(r"V4 delivered (\d+)/(\d+) beats")
_GEN_RE = re.compile(r"(Seedance clip|GPT image)\s+(\d+)\s*/\s*(\d+)")

# "Queries run" is counted from the lines the search providers actually emit, one per executed
# search, and by the query TEXT so a retried or re-sorted query is not counted twice:
#   scrapedo_tiktok.search        "Scrape.do TikTok: 12 unique post URL(s) for 'q' · 5 credits."
#   scrapedo_tiktok.search_many   "Scrape.do TikTok: search 'q' failed (RuntimeError)."
#   brightdata_tiktok.search      "Bright Data TikTok fallback: requesting up to 6 record(s) for 'q'."
#   clip_scraper.backend_search   "Sort MOST_LIKED: +3 new clip(s) (7 total) for 'q'."
#   tiktok fallback tag           "TikTok: 'q' had no relevant results; trying native tag ..."
#   scrape_v4                     "V4 TikTok session: search failed for 'q' (TimeoutError)."
# Bright's keyword collector is the one provider that does NOT log per query - it submits ONE
# job for many keywords - so its count is read out of the submit line instead.
_QUOTED = r"['\"](?P<q>[^'\"]{1,200})['\"]"
_QUERY_TEXT_RES = (
    re.compile(r"Scrape\.do TikTok: \d+ unique post URL\(s\) for " + _QUOTED),
    re.compile(r"Scrape\.do TikTok: search " + _QUOTED + r" failed"),
    re.compile(r"Bright Data TikTok fallback: requesting up to \d+ record\(s\) for " + _QUOTED),
    re.compile(r"V4 TikTok session: search failed for " + _QUOTED),
    re.compile(r"Sort [A-Z_]+: \+\d+ new clip\(s\) \(\d+ total\) for " + _QUOTED),
    re.compile(r"TikTok: " + _QUOTED + r" had no relevant results"),
)
_QUERY_BATCH_RE = re.compile(r"Bright Data TikTok: one job for (\d+) keyword\(s\)")


def run_health(logs, live_timeline=None):
    """Counters that say whether a run is healthy, parsed from the job log.

    Only counters with evidence are returned - a generated AI Short never inspects TikTok
    posts, so it must not show '0 checked' as if something were wrong."""
    counts = {key: 0 for key in _HEALTH_PATTERNS}
    beats_covered = beats_total = None
    clips = images = None
    seen_queries = set()
    batched_queries = 0
    for raw in logs or []:
        text = str(raw)
        if text.startswith("PREVIEW_IMAGE|") or text.startswith("PROJECT_DIR|"):
            continue
        for key, patterns in _HEALTH_PATTERNS.items():
            if any(p.search(text) for p in patterns):
                counts[key] += 1
        for pattern in _QUERY_TEXT_RES:
            m = pattern.search(text)
            if m:
                seen_queries.add(m.group("q").strip().casefold())
                break
        m = _QUERY_BATCH_RE.search(text)
        if m:
            batched_queries += int(m.group(1))
        m = _DELIVERED_RE.search(text)
        if m:
            beats_covered, beats_total = int(m.group(1)), int(m.group(2))
        m = _GEN_RE.search(text)
        if m:
            done, total = int(m.group(2)), int(m.group(3))
            if m.group(1).startswith("Seedance"):
                clips = (done, total)
            else:
                images = (done, total)
    if isinstance(live_timeline, dict) and live_timeline.get("beats"):
        beats = live_timeline["beats"]
        beats_total = len(beats)
        beats_covered = sum(1 for b in beats if b.get("assigned"))
    out = {}
    for key, value in counts.items():
        if value:
            out[key] = value
    if seen_queries or batched_queries:
        out["queries"] = len(seen_queries) + batched_queries
    if beats_total:
        out["beats_covered"] = int(beats_covered or 0)
        out["beats_total"] = int(beats_total)
    if clips:
        out["clips_done"], out["clips_total"] = clips
    if images:
        out["images_done"], out["images_total"] = images
    return out


# ------------------------------------------------------------------ job-status extras

def _activity(logs):
    for raw in reversed(logs or []):
        text = str(raw)
        if text and not text.startswith("PREVIEW_IMAGE|") and not text.startswith("PROJECT_DIR|"):
            return re.sub(r"\s+", " ", text).strip()[:180]
    return ""


def progress_fraction(steps, fallback_percent):
    """0..1 from typical durations when the history knows them, else the legacy marker %."""
    known = [s for s in steps if s.get("typical")]
    if len(known) < max(2, len(steps) // 2):
        return max(0.0, min(0.99, float(fallback_percent or 0) / 100.0))
    total = sum(float(s.get("typical") or 0) for s in steps) or 1.0
    done = 0.0
    for s in steps:
        typical = float(s.get("typical") or 0)
        if s.get("state") == "done":
            done += typical
        elif s.get("state") == "active":
            done += min(typical, float(s.get("elapsed") or 0))
    return max(0.0, min(0.99, done / total))


_BEATS_CACHE = {}
_SUB_RE = (
    # "Caption removal: filling frames 37-63 of 63"  ->  the clip being rebuilt right now
    re.compile(r"Caption removal: filling frames (?P<a>\d+)-(?P<b>\d+) of (?P<n>\d+)"),
    # "Seedance clip 3/10", "GPT image 4/21", "scene 2/13"
    re.compile(r"(?P<what>Seedance clip|GPT image|image|clip|scene)\s+(?P<i>\d+)\s*/\s*(?P<n>\d+)", re.I),
)


def run_beats(job):
    """The beats of the edit this job is rendering: [{start, end, text}], or [].

    The progress bar is a bar of TIME, and the video is made of beats of time, so the bar can be
    subdivided by them: the viewer sees which shot the render is on instead of a bare percentage.
    Read from the project's own config and cached on its mtime - this is polled every 2.5s.
    """
    path = str(job.get("project_dir") or "")
    if not path:
        return []
    config_dir = Path(path) / "config"
    try:
        newest = max(config_dir.glob("project*.json"), key=lambda p: p.stat().st_mtime)
        stamp = newest.stat().st_mtime
    except (ValueError, OSError):
        return []
    hit = _BEATS_CACHE.get(str(newest))
    if hit and hit[0] == stamp:
        return hit[1]
    beats = []
    try:
        scenes = json.loads(newest.read_text(encoding="utf-8")).get("scenes") or []
        for scene in sorted(scenes, key=lambda s: float(s.get("start") or 0)):
            start, end = float(scene.get("start") or 0), float(scene.get("end") or 0)
            if end > start:
                beats.append({"start": round(start, 3), "end": round(end, 3),
                              "text": str(scene.get("exact_voice_text") or scene.get("script")
                                           or scene.get("name") or "")[:70],
                              # The files this beat is made of - how a pre-pass line is placed on
                              # the bar. Both names are needed: the scene points at the DERIVED
                              # file (`capblur_07_ab12cd34.mp4`) while the clean-up logs the
                              # SOURCE it is working from (`scraped_06.mp4`), and on a project
                              # whose clips carry no post id in their name the two have nothing
                              # in common to match on. Measured: every beat read -1.
                              "clip": str(scene.get("clip") or ""),
                              "src": str(scene.get("caption_blur_src")
                                         or scene.get("timeline_speed_src") or "")})
    except Exception:                                                   # noqa: BLE001
        beats = []
    _BEATS_CACHE[str(newest)] = (stamp, beats)
    return beats


_CLIP_IN_LINE = re.compile(r"\b([\w.\- ]+?\.(?:mp4|mov|webm|mkv))\b")
_PREP_LINE = re.compile(r"^(?:Timeline render|Caption removal)\b")


def _source_key(name):
    """The post id inside a clip's file name - the one part every derived copy keeps."""
    ids = re.findall(r"\d{10,}", str(name or ""))
    return max(ids, key=len) if ids else str(name or "").rsplit(".", 1)[0].casefold()


def _same_source(a, b):
    """Do these two file names come from the same download?

    Not string equality: the derived copies TRUNCATE. `prepare_timeline_clips` builds its cut as
    `win<v>_{stem[:26]}_{ms}.mp4`, so a 19-digit TikTok id arrives as an 11-digit prefix of it.
    One id being the start of the other is the same post.
    """
    ka, kb = _source_key(a), _source_key(b)
    if not ka or not kb:
        return False
    if ka == kb:
        return True
    if ka.isdigit() and kb.isdigit():
        short, long = sorted((ka, kb), key=len)
        return len(short) >= 10 and long.startswith(short)
    return False


def phase_position(logs, beats, scene_clips):
    """Which BEAT the run is working on right now, and what it is doing to it.

    The bar is ruled by beats, so everything that moves along it has to be expressed in beats -
    including the passes that run BEFORE a single frame is rendered. `prepare_timeline_clips`
    walks the scenes in order, cleaning captions and rebuilding letterboxed frames one clip at a
    time, and every one of its lines names the clip. Mapping that name back to its scene turns a
    silent ten-minute pre-pass into visible movement across the same bar the render will use.

    Returns (index, label) with index -1 when nothing can be placed yet.
    """
    if not beats or not scene_clips:
        return -1, ""
    for line in reversed(logs or []):
        text = str(line)
        if not _PREP_LINE.match(text):
            continue
        low = text.lower()
        doing = ("cleaning captions" if ("caption" in low or "carries text" in low
                                         or "cleaning" in low) else "repairing the frame")
        for name in _CLIP_IN_LINE.findall(text):
            for i, names in enumerate(scene_clips):
                if i >= len(beats):
                    continue
                for clip in (names if isinstance(names, (list, tuple)) else [names]):
                    if clip and (_same_source(clip, name) or clip == name):
                        return i, doing
        # a caption-removal line with no file name belongs to the clip the previous line named
        if text.startswith("Caption removal"):
            continue
    return -1, ""


def sub_progress(logs):
    """Where the CURRENT phase is inside its own work: {done, total, label} or None.

    A render is not one job but a queue of them - clean this clip, generate that image, render
    the frames - and each one reports its own count. The phase bar reads the newest of those
    counts so a long silent step (ProPainter on one clip can run for minutes) still moves.
    """
    for line in reversed(logs or []):
        text = str(line)
        # The beat label above already says WHAT is happening to WHICH shot; this line is only the
        # count inside that shot, so it names the unit and nothing else.
        m = _SUB_RE[0].search(text)
        if m:
            return {"done": int(m.group("b")), "total": int(m.group("n")), "label": "frames"}
        m = _SUB_RE[1].search(text)
        if m and int(m.group("n")) > 0:
            what = m.group("what").lower()
            label = "clips" if "clip" in what else "images" if "image" in what else "scenes"
            return {"done": int(m.group("i")), "total": int(m.group("n")), "label": label}
    return None


def job_extras(job_id, job, live_timeline=None):
    """The additive `v3` block of /job-status. The chat shell ignores it."""
    import app
    status = str(job.get("status") or "missing")
    logs = job.get("logs") or []
    kind = str(job.get("job_kind") or "run")
    clip_source = job.get("clip_source")
    percent, _activity_text = app.progress_state(status, logs)
    steps = []
    if kind not in ("timeline", "longform"):
        steps = app.compute_step_view(status, logs, log_times=job.get("log_times"),
                                      job_kind=kind, clip_source=clip_source)
    key = history_key(job, logs)
    typical = typical_durations(key) if steps else {}
    for step in steps:
        step["typical"] = typical.get(step["name"])
    if status == "done" and steps:
        record_step_durations(job_id, key, steps)
    bar = app._render_bar_percent(logs) if kind in ("timeline", "longform") else None
    beats = run_beats(job)
    beat_index, beat_doing = phase_position(
        logs, beats, [[b.get("clip"), b.get("src")] for b in beats])
    active = next((s["name"] for s in steps if s["state"] == "active"), "")
    created = float(job.get("created_at") or 0) or time.time()
    return {
        "version": SHELL_V3_VERSION,
        "kind": kind,
        "history_key": key,
        "steps": steps,
        "typical_total": (round(sum(typical.values()), 1)
                          if typical and len(typical) >= len(steps) else None),
        "progress": (bar / 100.0 if bar is not None else progress_fraction(steps, percent)),
        "phase": active,
        "activity": _activity(logs),
        "elapsed": max(0.0, time.time() - created),
        "health": run_health(logs, live_timeline),
        "beats": beats,
        "beat_index": beat_index,
        "beat_doing": beat_doing,
        "sub": sub_progress(logs),
    }


# ------------------------------------------------------------------ the document

def page(initial=None):
    """The v3 document. `initial` carries the same deep-link keys as chat_ui.chat_shell_page."""
    import app
    initial = dict(initial or {})
    opts = chat_ui.extract_legacy_options()
    try:
        connections = {
            "tiktok": json.loads(app.tiktok_status_payload().decode("utf-8")),
            "instagram": json.loads(app.instagram_status_payload().decode("utf-8")),
            "higgsfield": json.loads(app.higgsfield_status_payload().decode("utf-8")),
        }
    except Exception:
        connections = {}
    boot = {
        "version": SHELL_V3_VERSION,
        "strings": chat_ui.UI_STRINGS,
        "options": opts,
        "reasoningConfig": reasoning_modes.public_config(),
        "manifest": {"run": chat_ui.RUN_MANIFEST, "masters": chat_ui.MASTER_MANIFESTS},
        "uiState": app.load_ui_state(),
        "connections": connections,
        "initial": initial,
        "voices_preview": "/voice-preview?voice=",
        "chatState": chat_ui.load_chat_state(),
        "now": time.time(),
    }
    boot_json = json.dumps(boot, ensure_ascii=False).replace("</", "<\\/")
    ver = asset_ver()
    # The 3D showroom brings its own palette, and it defines it on :root - so linking it
    # unconditionally repainted every screen in the app. It loads only when it is used.
    _use_showroom = str((initial or {}).get("ui") or "").strip().lower() == "showroom"
    showroom_css = (f'<link rel="stylesheet" href="/static/showroom3d/showroom.css?v={ver}">'
                    if _use_showroom else "")
    html = f"""<!DOCTYPE html>
<html lang="en" class="v3">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark">
<title>Shortslab</title>
<link rel="icon" id="v3-favicon" href="/favicon.ico" sizes="any">
<link rel="stylesheet" href="/static/shell-v3.css?v={ver}">
{showroom_css}
</head>
<body class="v3-body">
<div id="v3" class="v3-app" data-view="home">
  <header class="v3-top" id="v3-top"></header>
  <main class="v3-main" id="v3-main" tabindex="-1"></main>
  <footer class="v3-dock" id="v3-dock" hidden></footer>
</div>
<div id="v3-layer"></div>
<script id="v3-boot" type="application/json">{boot_json}</script>
<script src="/static/shell-v3.js?v={ver}"></script>
{app.heartbeat_script()}
</body>
</html>"""
    return html.encode("utf-8")
