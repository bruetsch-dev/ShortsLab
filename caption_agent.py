"""Caption-only post-production for an existing video (the "Caption master").

Upload any MP4 and this module burns in the SAME viral word-by-word green-box captions
the main render pipeline uses - with NO external APIs:

  1. extract the audio track with ffmpeg,
  2. transcribe it with frame-accurate word timing locally (faster-whisper),
  3. build the same timed caption chunks (pipeline.build_caption_chunks),
  4. draw them per frame with the same renderer (pipeline.draw_animated_caption),
  5. encode straight to H.264 via an ffmpeg pipe and mux the ORIGINAL audio back.

Everything runs on the local machine; the only requirement is faster-whisper (already
used for voice alignment) and ffmpeg. No WaveSpeed / Gemini / OpenAI calls.
"""

import subprocess
import tempfile
import time
from pathlib import Path

import numpy as np

import pipeline
import voice_align

try:
    import cv2
except Exception:  # pragma: no cover
    cv2 = None

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent
CAPTION_OUTPUT_DIR = ROOT / "projects" / "_captioned"
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"}


def log(status_cb, message):
    if status_cb:
        status_cb(message)


def _cancelled(cancel_event):
    return bool(cancel_event is not None and cancel_event.is_set())


def _extract_audio(video_path, out_wav, ffmpeg):
    """Pull a mono 16 kHz wav (what faster-whisper wants) out of the video. None if no audio."""
    cmd = [ffmpeg, "-y", "-i", str(video_path), "-vn", "-ac", "1", "-ar", "16000",
           "-f", "wav", str(out_wav)]
    try:
        subprocess.run(cmd, capture_output=True, timeout=600)
    except Exception:
        return None
    return out_wav if out_wav.exists() and out_wav.stat().st_size > 2048 else None


def caption_video(video_path, max_words=1, language=None, caption_center_y=0.60,
                  status_cb=None, cancel_event=None, caption_config=None, labels=None):
    """Burn the standard animated captions onto `video_path`. Returns a result dict:
    {output, words, chunks, duration, fps, size}. Raises RuntimeError on a hard failure."""
    video_path = Path(video_path)
    if cv2 is None:
        raise RuntimeError("OpenCV (cv2) is required for captioning but is not installed.")
    if not video_path.exists():
        raise RuntimeError(f"Input video not found: {video_path}")
    if not voice_align.available():
        raise RuntimeError("Local transcriber (faster-whisper) is not installed. "
                           "Install it with: pip install faster-whisper")
    ffmpeg = pipeline.find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found; cannot read/encode the video.")

    CAPTION_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    tmpdir = Path(tempfile.mkdtemp(prefix="cap_"))
    audio_wav = tmpdir / "audio.wav"

    # 1) audio -> 2) local transcription with word timing
    log(status_cb, "Extracting audio track...")
    have_audio = _extract_audio(video_path, audio_wav, ffmpeg)
    if not have_audio:
        raise RuntimeError("No audio track found in the video, so there is nothing to caption.")
    log(status_cb, "Transcribing speech locally (faster-whisper, no API)...")
    words = voice_align.transcribe_words(audio_wav, language=language, status_cb=status_cb) or []
    words = [w for w in words if (w.get("word") or "").strip()]
    if not words:
        raise RuntimeError("No speech was detected in the audio, so no captions were created.")
    log(status_cb, f"Transcribed {len(words)} word(s).")

    # video properties
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise RuntimeError(f"Could not open video: {video_path}")
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 30.0)
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    if width <= 0 or height <= 0:
        cap.release()
        raise RuntimeError("Could not read video dimensions.")
    duration = (frame_count / fps) if (frame_count and fps) else float(words[-1].get("end", 0)) + 0.5

    # 3) the SAME caption chunks the render pipeline builds (word_times = absolute seconds)
    chunks = pipeline.build_caption_chunks("", max(0.1, duration), max_words=max(1, int(max_words)),
                                           uppercase=True, word_times=words)
    config = {
        "caption_active_box": True,        # signature green highlight box
        "caption_center_y": float(caption_center_y),
        "caption_max_words": int(max_words),
        "caption_uppercase": True,
    }
    config.update(dict(caption_config or {}))
    labels = [dict(row) for row in (labels or []) if isinstance(row, dict)]

    # 5) encode straight to H.264 via an ffmpeg pipe (single encode = best quality) and mux audio
    out_name = f"{video_path.stem}_captioned_{time.strftime('%Y%m%d_%H%M%S')}.mp4"
    output = CAPTION_OUTPUT_DIR / out_name
    enc = subprocess.Popen(
        [ffmpeg, "-y",
         "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{width}x{height}", "-r", f"{fps:.6f}", "-i", "pipe:0",
         "-i", str(video_path),
         "-map", "0:v:0", "-map", "1:a:0?",
         "-c:v", "libx264", "-crf", "18", "-preset", "veryfast", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "192k", "-shortest", str(output)],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    log(status_cb, f"Rendering captions onto {frame_count or '?'} frame(s) ({width}x{height} @ {fps:.0f}fps)...")
    drawn = 0
    last_pct = -1
    try:
        i = 0
        while True:
            if _cancelled(cancel_event):
                raise RuntimeError("Cancelled by user.")
            ok, frame = cap.read()
            if not ok:
                break
            t = i / fps
            base = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
            base = pipeline.draw_animated_caption(base, chunks, t, width, height, config)
            label = next((row for row in labels
                          if float(row.get("start", 0)) <= t < float(row.get("end", 0))), None)
            if label and str(label.get("text") or "").strip():
                draw = ImageDraw.Draw(base)
                font = pipeline.get_font(max(30, int(width * 0.034)), True)
                draw.text((width // 2, int(height * 0.075)), str(label["text"]),
                          font=font, anchor="mm", fill=(255, 255, 255),
                          stroke_width=max(2, int(width * 0.003)), stroke_fill=(0, 0, 0))
            enc.stdin.write(np.asarray(base, dtype=np.uint8).tobytes())
            drawn += 1
            i += 1
            if frame_count:
                pct = int(i * 100 / frame_count)
                if pct >= last_pct + 10:
                    log(status_cb, f"Captioning: {pct}%")
                    last_pct = pct
    finally:
        cap.release()
        try:
            enc.stdin.close()
        except Exception:
            pass
        enc.wait()

    if not output.exists() or output.stat().st_size < 4096:
        raise RuntimeError("ffmpeg failed to write the captioned video.")
    log(status_cb, f"Done. Captioned video saved: {output.name}")
    return {
        "video": str(output),                 # render_done_view / render_outputs key
        "original_video": str(video_path),
        "output": str(output),
        "output_name": output.name,
        "words": len(words),
        "chunks": len(chunks),
        "frames": drawn,
        "duration": round(duration, 2),
        "fps": round(fps, 2),
        "size": f"{width}x{height}",
    }
