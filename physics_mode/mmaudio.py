"""Generate the whole soundtrack for a rendered clip with MMAudio V2.

    https://wavespeed.ai/models/wavespeed-ai/mmaudio-v2

MMAudio watches the video and produces audio that follows what it sees, so it replaces the
entire hand-built layer: no impact placement, no material synthesis, no gain staging per
event. The silent cut goes up, one track comes back.

The local synthesis in `sound.py` stays as the fallback. This call needs balance and a
round trip; a mode that produces nothing when the network is down would be worse than one
that produces a simpler noise.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pipeline

MODEL = "wavespeed-ai/mmaudio-v2"
API_BASE = pipeline.API_BASE


def _log(cb, msg):
    (cb or print)(msg)


def generate(video, out_path, prompt: str = "", *, negative_prompt: str = "",
             seed: int = -1, status_cb=None, key: str | None = None,
             cancel_event=None, ffmpeg: str = "ffmpeg",
             duration: float | None = None) -> str | None:
    """Send `video` to MMAudio and mux the returned track onto it.

    Returns the output path, or None if anything went wrong - the caller falls back to the
    locally synthesised sound rather than shipping a silent video.
    """
    video, out_path = Path(video), Path(out_path)
    if not video.exists():
        return None
    key = key or pipeline.api_key()
    if not key:
        _log(status_cb, "MMAudio: no WAVESPEED_API_KEY; using local sounds.")
        return None
    try:
        _log(status_cb, "MMAudio: uploading the silent cut...")
        video_url, _ = pipeline.upload_media(video, key)
        payload = {"video": video_url, "prompt": prompt, "seed": int(seed)}
        if negative_prompt:
            payload["negative_prompt"] = negative_prompt
        if duration:
            # MMAudio takes the length in seconds; without it the track can come back
            # shorter than the cut and the video ends in silence.
            payload["duration"] = round(float(duration), 2)
        response = pipeline.request_json("POST", f"{API_BASE}/{MODEL}", key, payload,
                                         timeout=240)
        task_id = pipeline.unwrap_id(response)
        _log(status_cb, f"MMAudio: generating ({task_id})...")
        # poll_wavespeed returns (outputs, response) - unpacking it as a bare list makes
        # every successful job look like it produced nothing.
        outputs, _resp = pipeline.poll_wavespeed(task_id, key, timeout_s=900,
                                                 cancel_event=cancel_event,
                                                 status_cb=status_cb, label="MMAudio")
    except Exception as exc:  # noqa: BLE001
        _log(status_cb, f"MMAudio failed ({exc}); using local sounds.")
        return None

    urls = [u for u in (outputs or []) if isinstance(u, str)]
    if not urls:
        _log(status_cb, "MMAudio returned nothing; using local sounds.")
        return None
    # download_wavespeed_outputs returns RECORDS and writes the first output to
    # primary_path; treating its return value as a path loses the file it just fetched.
    got = out_path.with_name(out_path.stem + "_raw.mp4")
    pipeline.download_wavespeed_outputs(urls, got, out_path.parent,
                                        out_path.stem + "_mm", ".mp4",
                                        cancel_event=cancel_event)
    if not got.exists():
        _log(status_cb, "MMAudio output could not be downloaded; using local sounds.")
        return None

    # MMAudio returns a video with the new track already on it. Re-encode nothing: take
    # OUR picture and ITS audio, so a re-encode on their side cannot degrade the render.
    # MMAudio hands back a quiet mix - measured -21.0 LUFS on the wrecking-ball sweep,
    # 7dB under where a short should sit. Normalise on the way in, with the limiter last
    # so AAC overshoot cannot push the peak over.
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
           "-i", str(video), "-i", str(got),
           "-filter_complex",
           # Gentle compression only. It buys about 1.7 LU (-21.0 -> -19.3 measured);
           # squeezing harder would flatten exactly the calm the format is going for, and
           # this is content that is mostly silence with three hits in it.
           "[1:a]acompressor=threshold=0.06:ratio=2.5:attack=15:release=350,"
           "loudnorm=I=-16:TP=-1.5:LRA=11,alimiter=limit=0.85:level=disabled[a]",
           "-map", "0:v", "-map", "[a]", "-c:v", "copy",
           "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
           "-movflags", "+faststart", str(out_path)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    if out_path.exists() and r.returncode == 0:
        _log(status_cb, "MMAudio: soundtrack mixed onto the render.")
        return str(out_path)
    _log(status_cb, "MMAudio audio could not be muxed; using local sounds.")
    return None


def prompt_for(title: str, extra: str = "") -> str:
    """A short description of what should be heard, from what the scene is.

    MMAudio follows the picture on its own; the prompt only steers the character. Naming
    the materials and asking for restraint keeps it from adding music or a whoosh score
    over what should be a physical sound.
    """
    base = (f"{title}. Realistic physical sound only: the impact, the material breaking "
            "and the pieces settling. Deep and soft, no music, no voices, no whoosh "
            "effects, no reverb tail, calm and clean.")
    return f"{base} {extra}".strip()
