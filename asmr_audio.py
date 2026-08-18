"""Local, deterministic ASMR audio finishing for an existing video.

No model or external service is used. The original production sound is cleaned
and shaped while the video stream is copied byte-for-byte.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from pathlib import Path

import agent_core
import pipeline


PROFILES = {
    "natural": {
        "label": "Natural detail",
        "filter": (
            "highpass=f=38,lowpass=f=15500,"
            "equalizer=f=180:t=q:w=0.9:g=1.0,equalizer=f=2600:t=q:w=1.0:g=1.4,"
            "equalizer=f=7200:t=q:w=0.9:g=1.2,"
            "acompressor=threshold=0.10:ratio=1.7:attack=22:release=220:makeup=1.25,"
            "extrastereo=m=1.10:c=0,loudnorm=I=-19:LRA=12:TP=-1.2"
        ),
    },
    "close": {
        "label": "Close-up ASMR",
        "filter": (
            "highpass=f=42,lowpass=f=15500,afftdn=nf=-34:tn=1:gs=3,"
            "equalizer=f=150:t=q:w=0.8:g=1.2,equalizer=f=2400:t=q:w=0.9:g=2.0,"
            "equalizer=f=6800:t=q:w=0.8:g=2.2,"
            "acompressor=threshold=0.085:ratio=2.0:attack=16:release=180:makeup=1.35,"
            "extrastereo=m=1.18:c=0,loudnorm=I=-18:LRA=10:TP=-1.0"
        ),
    },
    "soft": {
        "label": "Soft & calm",
        "filter": (
            "highpass=f=45,lowpass=f=12500,afftdn=nf=-32:tn=1:gs=4,"
            "equalizer=f=220:t=q:w=0.9:g=1.4,equalizer=f=3600:t=q:w=1.0:g=-1.5,"
            "equalizer=f=8200:t=q:w=0.8:g=-0.8,"
            "acompressor=threshold=0.11:ratio=1.6:attack=28:release=260:makeup=1.2,"
            "extrastereo=m=1.08:c=0,loudnorm=I=-20:LRA=13:TP=-1.5"
        ),
    },
}


def _run(command, timeout=900):
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        detail = (result.stderr or result.stdout or "ffmpeg failed").strip()
        raise RuntimeError(detail[-1800:])
    return result


def _duration(path: Path, ffprobe: str) -> float:
    result = _run([ffprobe, "-v", "error", "-show_entries", "format=duration",
                   "-of", "default=nw=1:nk=1", str(path)], timeout=60)
    try:
        return max(0.01, float(result.stdout.strip()))
    except ValueError as exc:
        raise RuntimeError("Could not read the video's duration.") from exc


def _timeline_project(source: Path, output: Path, duration: float, ffmpeg: str,
                      profile: str, status_cb=None):
    base = re.sub(r"[^a-z0-9_]+", "_", source.stem.lower()).strip("_")[:38] or "video"
    slug = f"asmr_{base}_{time.strftime('%H%M%S')}"
    project = agent_core.PROJECTS_DIR / slug
    clips = project / "seedance 2.0"
    for folder in (clips, project / "input", project / "config", project / "renders"):
        folder.mkdir(parents=True, exist_ok=True)

    clip_name = "source_video.mp4"
    _run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(source),
          "-map", "0:v:0", "-an", "-c:v", "copy", str(clips / clip_name)])
    _run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(output),
          "-vn", "-ar", "48000", "-ac", "2", str(project / "input" / "voiceover.wav")])
    shutil.copy2(output, project / "renders" / output.name)
    config = {
        "project_slug": slug, "title": f"ASMR Sound - {source.stem}"[:80],
        "duration": round(duration, 3),
        "scenes": [{"id": "01", "name": "Source video", "script": "",
                    "exact_voice_text": "", "start": 0.0, "end": round(duration, 3),
                    "clip": clip_name, "asset": clip_name, "seedance": True,
                    "seedance_start_trim": 0.0, "render_caption": False}],
        "custom_sfx": [], "sfx_enabled": False, "render_captions": False,
        "animated_captions": False, "use_seedance_clips": True,
        "seedance_clip_start_trim": 0.0, "background_music_choice": "none",
        "audio_master_gain": 1.0, "asmr_master_source": str(source),
        "asmr_profile": profile,
    }
    (project / "config" / "project.json").write_text(
        json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")
    if status_cb:
        status_cb("Editable timeline project created.")
    return slug, project


def enhance_video(video_path, profile="close", out_dir=None, status_cb=None):
    source = Path(video_path)
    if not source.exists():
        raise RuntimeError("Uploaded video was not found.")
    profile = str(profile or "close").strip().lower()
    if profile not in PROFILES:
        profile = "close"
    ffmpeg = pipeline.find_ffmpeg()
    ffprobe = pipeline.find_ffprobe(ffmpeg)
    if not ffmpeg or not ffprobe:
        raise RuntimeError("ffmpeg and ffprobe are required for ASMR Sound.")
    duration = _duration(source, ffprobe)
    destination = Path(out_dir) if out_dir else agent_core.ROOT / "outputs" / "asmr"
    destination.mkdir(parents=True, exist_ok=True)
    output = destination / f"{source.stem}_ASMR_{time.strftime('%Y%m%d_%H%M%S')}.mp4"
    if status_cb:
        status_cb(f"Analysed {duration:.1f}s of original sound.")
        status_cb(f"Applying {PROFILES[profile]['label']} profile...")
    _run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(source),
          "-map", "0:v:0", "-map", "0:a:0", "-c:v", "copy",
          "-af", PROFILES[profile]["filter"], "-ar", "48000", "-c:a", "aac",
          "-b:a", "256k", "-movflags", "+faststart", str(output)])
    if not output.exists() or output.stat().st_size < 4096:
        raise RuntimeError("ASMR Sound did not produce an output video.")
    if status_cb:
        status_cb("ASMR sound master complete. The picture was preserved without re-encoding.")
    slug, project = _timeline_project(source, output, duration, ffmpeg, profile, status_cb)
    return {"video": str(output), "output": str(output), "original_video": str(source),
            "profile": profile, "profile_label": PROFILES[profile]["label"],
            "duration": round(duration, 2), "timeline_slug": slug,
            "project_dir": str(project)}
