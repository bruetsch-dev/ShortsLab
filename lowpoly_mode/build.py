"""Build one narrated low-poly short end to end.

    idea -> story + shot list -> voiceover -> one authored Blender shot per beat
         -> cut to the voiceover -> captions burned on

Every stage writes into the work folder, so a run that dies halfway can be inspected and
the expensive parts (the voiceover, the authored scripts) reused rather than re-bought.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import caption_agent
import pipeline

from . import shots as shotlib
from . import story as storylib

# Small and cheap on purpose. The style is crude, so resolution and sample count buy
# nothing but render time - and a 12-shot story is 12 renders, not one.
RES = (540, 960)
FPS = 24
SAMPLES = 8


def _log(cb, msg):
    (cb or print)(msg)


def _probe(path) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    try:
        return float((r.stdout or "").strip())
    except ValueError:
        return 0.0


def build(out_dir, prompt: str, *, seconds: float = 30.0, status_cb=None,
          captions: bool = True, voice: str | None = None,
          blender: str | None = None, ffmpeg: str = "ffmpeg") -> dict:
    """Make the short. Returns {"video": ..., "title": ..., "shots": [...]}."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    plan = storylib.write_story(prompt, seconds=seconds, status_cb=status_cb)
    (out / "story.json").write_text(json.dumps(plan, indent=1, ensure_ascii=False),
                                    encoding="utf-8")

    _log(status_cb, "Recording the voiceover...")
    kwargs = {"status_cb": status_cb}
    if voice:
        kwargs["voice"] = voice
    # Take the path the TTS RETURNS, and if that is missing look for a sibling with the
    # same stem: it decides the container itself and wrote voiceover.wav for a request
    # named voiceover.mp3, which made a perfectly good 1.8MB take look like an empty one.
    written = pipeline.generate_speech_gemini(plan["narration"], out / "voiceover.mp3",
                                              **kwargs)
    voice_path = Path(written) if written else out / "voiceover.mp3"
    if not voice_path.exists():
        siblings = sorted(out.glob("voiceover.*"))
        if siblings:
            voice_path = siblings[0]
    vo_len = _probe(voice_path)
    if vo_len <= 0:
        raise RuntimeError(f"The voiceover came out empty ({voice_path.name}).")
    _log(status_cb, f"Voiceover: {vo_len:.1f}s")

    # The shot lengths in the plan are the writer's intent; the voice is the truth.
    timed = storylib.fit_shots_to_audio(plan["shots"], vo_len)

    clips = []
    for i, shot in enumerate(timed, 1):
        _log(status_cb, f"Shot {i}/{len(timed)} ({shot['seconds']:.1f}s)")
        try:
            script = shotlib.author_shot(shot["scene"], i, shot["seconds"], out,
                                         status_cb=status_cb, blender=blender)
            clip = shotlib.render_shot(script, out / "shots" / f"{i:02d}",
                                       shot["seconds"], res=RES, samples=SAMPLES,
                                       fps=FPS, status_cb=status_cb, blender=blender)
        except Exception as exc:  # noqa: BLE001
            _log(status_cb, f"  shot {i} dropped: {exc}")
            clip = None
        if clip:
            clips.append(clip)
            shot["clip"] = str(clip)
    if not clips:
        raise RuntimeError("Not one shot rendered.")
    if len(clips) < len(timed):
        _log(status_cb, f"{len(timed) - len(clips)} of {len(timed)} shots failed; "
                        "building from the rest.")

    silent = out / "cut_silent.mp4"
    listing = silent.with_suffix(".txt")
    listing.write_text("".join(f"file '{Path(c).as_posix()}'\n" for c in clips),
                       encoding="utf-8")
    subprocess.run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                    "-f", "concat", "-safe", "0", "-i", str(listing),
                    "-c:v", "libx264", "-crf", "20", "-preset", "medium",
                    "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(silent)],
                   capture_output=True, timeout=1800)
    listing.unlink(missing_ok=True)
    if not silent.exists():
        raise RuntimeError("The shots could not be joined.")

    # -shortest, so a dropped shot trims the tail rather than freezing on black while the
    # narration keeps going over nothing.
    voiced = out / "cut_voiced.mp4"
    subprocess.run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                    "-i", str(silent), "-i", str(voice_path),
                    "-map", "0:v", "-map", "1:a", "-c:v", "copy",
                    "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-shortest",
                    "-movflags", "+faststart", str(voiced)],
                   capture_output=True, timeout=1800)
    if not voiced.exists():
        raise RuntimeError("The voiceover could not be muxed onto the cut.")

    final = voiced
    if captions:
        _log(status_cb, "Burning word-by-word captions...")
        try:
            res = caption_agent.caption_video(voiced, max_words=1, status_cb=status_cb)
            final = Path(res["output"])
        except Exception as exc:  # noqa: BLE001
            _log(status_cb, f"Captions skipped ({exc}); keeping the uncaptioned cut.")

    report = {"title": plan["title"], "video": str(final), "narration": plan["narration"],
              "seconds": round(_probe(final), 2), "shots": timed,
              "voiceover": str(voice_path)}
    (out / "report.json").write_text(json.dumps(report, indent=1, ensure_ascii=False),
                                     encoding="utf-8")
    _log(status_cb, f"Done: {report['seconds']:.1f}s, {len(clips)} shots")
    return report
