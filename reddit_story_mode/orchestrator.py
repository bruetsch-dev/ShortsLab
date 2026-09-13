"""Run one Reddit Story job end-to-end: voiceover -> parkour pick -> video build.

Designed to be driven by the app's existing JOBS/daemon-thread system: it takes a status_cb
and a cancel_event and returns a result dict shaped like the main pipeline's
({"video": <path>, ...}) so the existing job page / render_done_view can show it unchanged.
"""

import os
from pathlib import Path

from . import wavespeed_tts, parkour_picker, video_builder

ROOT = Path(__file__).resolve().parent.parent
OUTPUT_ROOT = Path(os.environ.get("REDDIT_OUTPUT_DIR") or (ROOT / "outputs" / "reddit_story"))

# progress phase labels the UI can key on
PHASE_VOICE = "generating_voiceover"
PHASE_PARKOUR = "selecting_parkour"
PHASE_BUILD = "building_video"
PHASE_DONE = "done"


def run_story_job(story, job_id, status_cb=None, cancel_event=None):
    """Produce the final vertical MP4 for one story. Raises RuntimeError with a readable
    message on failure (caller marks the job failed and shows the message)."""
    if not isinstance(story, dict) or not str(story.get("script") or "").strip():
        raise RuntimeError("No story script provided for the Reddit Story job.")
    out_dir = OUTPUT_ROOT / str(job_id)
    out_dir.mkdir(parents=True, exist_ok=True)

    def phase(name, msg):
        if status_cb:
            status_cb(f"[{name}] {msg}")

    # 1) voiceover
    phase(PHASE_VOICE, "Generating WaveSpeed voiceover...")
    voice_path = wavespeed_tts.generate_voiceover(
        story["script"], out_dir / "voice.mp3",
        voice_description=wavespeed_tts.DEFAULT_VOICE_DESCRIPTION,
        speed=wavespeed_tts.DEFAULT_SPEED,
        cancel_event=cancel_event, status_cb=status_cb)

    # 2) parkour clip
    phase(PHASE_PARKOUR, "Selecting a Minecraft parkour clip from the local pool...")
    parkour_path = parkour_picker.pick_clip(status_cb=status_cb)

    # 3) build the video
    phase(PHASE_BUILD, "Building the final 1080x1920 video...")
    video_path = video_builder.build_video(
        parkour_path, voice_path, out_dir / "reddit_story.mp4",
        status_cb=status_cb, cancel_event=cancel_event)

    phase(PHASE_DONE, "Reddit Story video ready.")
    return {
        "title": story.get("title") or "Reddit Story",
        "video": str(video_path),
        "voice": str(voice_path),
        "parkour": str(parkour_path),
        "story_id": story.get("id"),
        "output_dir": str(out_dir),
    }
