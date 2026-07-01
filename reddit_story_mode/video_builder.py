"""Build the final Reddit Story video: local parkour background + TTS voiceover only.

Output: vertical 1080x1920 H.264/AAC, voiceover audio only (parkour audio dropped), no
captions/overlays/watermark. The parkour loops if shorter than the narration and is
center-cropped after scaling if it is wider than 9:16. Duration matches the voiceover.
"""

import subprocess
from pathlib import Path

import pipeline

TARGET_W, TARGET_H = 1080, 1920


def _probe_duration(path, ffprobe=None):
    ffprobe = ffprobe or _ffprobe()
    if not ffprobe:
        return 0.0
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=30).stdout.strip()
        return max(0.0, float(out))
    except Exception:
        return 0.0


def _ffprobe():
    ff = pipeline.find_ffmpeg()
    if not ff:
        return None
    cand = Path(ff).with_name("ffprobe" + Path(ff).suffix)
    return str(cand) if cand.exists() else "ffprobe"


def build_video(parkour_path, voice_path, out_path, status_cb=None, cancel_event=None):
    """Compose parkour (looped, cropped to 1080x1920) with the voiceover. Returns out_path.

    - `-stream_loop -1` on the parkour so it never runs out under a long narration.
    - Maps ONLY the parkour video and the voice audio (original gameplay audio is discarded).
    - scale+crop to a filled 9:16 frame (works for landscape or already-vertical sources).
    - `-t` = voiceover duration and `-shortest` so the export ends with the narration.
    """
    ffmpeg = pipeline.find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found - cannot build the Reddit Story video.")
    parkour_path, voice_path, out_path = Path(parkour_path), Path(voice_path), Path(out_path)
    if not parkour_path.exists():
        raise RuntimeError(f"Parkour clip missing: {parkour_path}")
    if not voice_path.exists():
        raise RuntimeError(f"Voiceover missing: {voice_path}")
    voice_dur = _probe_duration(voice_path)
    if voice_dur <= 0.1:
        raise RuntimeError("Voiceover has no measurable duration.")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if status_cb:
        status_cb(f"Building {TARGET_W}x{TARGET_H} video ({voice_dur:.1f}s) from parkour + voiceover...")
    vf = (f"scale={TARGET_W}:{TARGET_H}:force_original_aspect_ratio=increase,"
          f"crop={TARGET_W}:{TARGET_H},setsar=1,fps=30,format=yuv420p")
    cmd = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "warning",
        "-stream_loop", "-1", "-i", str(parkour_path),   # input 0: looping parkour (video)
        "-i", str(voice_path),                            # input 1: voiceover (audio)
        "-map", "0:v:0", "-map", "1:a:0",                 # parkour video + voice audio ONLY
        "-vf", vf,
        "-t", f"{voice_dur:.3f}",
        "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
        "-shortest", "-movflags", "+faststart",
        str(out_path),
    ]
    try:
        pipeline.check_cancel(cancel_event=cancel_event)
    except Exception:
        pass
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 or not out_path.exists():
        tail = (proc.stderr or "")[-600:]
        raise RuntimeError(f"ffmpeg failed to build the video: {tail}")
    if status_cb:
        status_cb(f"Video built: {out_path.name}")
    return out_path
