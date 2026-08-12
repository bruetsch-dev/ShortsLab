"""Dreamcore shorts: prompts that already contain cuts, then an edit locked to the melody.

The reference this is built from is measured, not described. `between silence and sky`
(reference/dreamcore/) is 14.61s long, has exactly three cuts - 3.68, 7.32, 11.00 - and all
three land within 52 ms of a note onset in its music. The music underneath repeats a phrase
every ~3.85s, and 86% of that grid falls on an onset. So the whole look is one rule:

    a cut happens only on a phrase boundary, and every shot fills exactly one phrase.

That has a consequence for the prompts. A generator asked for "an abandoned mall at night"
returns one continuous 10-second drift, which is one shot, which is one phrase - three
generations for a 12-second video. To get the reference's cut density the CUT has to be
inside the generated clip, so the prompts here explicitly ask for two or three hard cuts at
named times, and those times come from the same grid the editor will later snap to.

The user generates the clips themselves and drops them back in. This module does the two
things that are hard: writing prompts that survive contact with a generator, and cutting
the result to the music without the cuts drifting.
"""

from __future__ import annotations

import json
import math
import shutil
import subprocess
from pathlib import Path

import numpy as np

import agent_core
import pipeline

PROMPT_MODEL = "anthropic/claude-opus-4.8"
VISION_MODEL = "google/gemini-3.1-pro-preview"

W, H, FPS = 1080, 1920, 30

# Analysis constants, all fixed by the reference measurement above.
SR = 22050
HOP = 512
WIN = 2048
ONSET_TOLERANCE = 0.12        # a cut this close to an onset reads as "on the music"
MIN_PHRASE = 1.6
MAX_PHRASE = 5.0
SCENE_THRESHOLD = 0.25        # ffmpeg scene score that counts as a hard cut
MIN_SHOT = 0.8                # shorter than this is a flash, not a shot
SPEED_SLACK = 0.12            # a shot may be slowed by up to this much to fill its phrase
HOLD_MARGIN = 0.4             # prompts ask for this much more than a phrase, so we trim


def _log(cb, msg):
    (cb or print)(msg)


# ----------------------------------------------------------------- the music grid


def _decode_mono(path) -> np.ndarray:
    ffmpeg = str(pipeline.find_ffmpeg() or "ffmpeg")
    raw = subprocess.run(
        [ffmpeg, "-v", "error", "-i", str(path), "-ac", "1", "-ar", str(SR),
         "-f", "f32le", "-"], capture_output=True, timeout=900).stdout
    return np.frombuffer(raw, dtype=np.float32)


def onsets(path, threshold: float = 1.6) -> tuple[list[float], float]:
    """Times where a new note starts, by spectral flux.

    Deliberately NOT a beat tracker: this music has no percussion, so a tempo grid would
    invent a pulse that is not in the recording. Note onsets are what the reference cuts
    actually sit on.
    """
    x = _decode_mono(path)
    if x.size < WIN * 2:
        return [], 0.0
    window = np.hanning(WIN).astype(np.float32)
    frames = 1 + (x.size - WIN) // HOP
    mags = np.empty((frames, WIN // 2 + 1), dtype=np.float32)
    for i in range(frames):
        mags[i] = np.abs(np.fft.rfft(x[i * HOP:i * HOP + WIN] * window))
    flux = np.maximum(0.0, np.diff(mags, axis=0)).sum(axis=1)
    if flux.max() > 0:
        flux /= flux.max()
    k = max(3, int(0.5 * SR / HOP))
    local = np.convolve(np.pad(flux, (k, k), mode="edge"),
                        np.ones(2 * k + 1) / (2 * k + 1), mode="valid")
    peaks: list[float] = []
    for i in range(1, len(flux) - 1):
        if flux[i] > local[i] * threshold and flux[i] >= flux[i - 1] and flux[i] > flux[i + 1]:
            t = (i + 1) * HOP / SR
            if not peaks or t - peaks[-1] > 0.12:
                peaks.append(round(t, 3))
    return peaks, x.size / SR


def music_grid(bed_path, status_cb=None) -> dict:
    """The phrase grid of a music bed: {"phrase": seconds, "offset": s, "cuts": [...]}.

    Searches the period/offset pair that puts the most grid points on a note onset. On the
    supplied dreamcore bed this finds 3.85s from 0.00s with 42 of 49 grid points on an
    onset; the same routine on the reference reel's own audio agrees with where its editor
    actually cut.
    """
    ons, duration = onsets(bed_path)
    if len(ons) < 6:
        _log(status_cb, "Music: too few note onsets to read a phrase; falling back to 3.85s.")
        phrase, offset = 3.85, 0.0
    else:
        best = None
        for period in np.arange(MIN_PHRASE, MAX_PHRASE, 0.01):
            for off in np.arange(0.0, period, 0.02):
                grid = np.arange(off, duration, period)
                if len(grid) < 4:
                    continue
                hits = sum(1 for g in grid
                           if any(abs(g - o) <= ONSET_TOLERANCE for o in ons))
                score = hits / len(grid)
                if best is None or (score, len(grid)) > (best[0], best[1]):
                    best = (score, len(grid), float(period), float(off), hits)
        phrase, offset = round(best[2], 3), round(best[3], 3)
        _log(status_cb, f"Music: a phrase every {phrase:.2f}s from {offset:.2f}s "
                        f"({best[4]}/{best[1]} grid points sit on a note).")
    cuts = [round(offset + i * phrase, 3)
            for i in range(int(math.ceil((duration - offset) / phrase)))]
    return {"phrase": phrase, "offset": offset, "duration": round(duration, 3),
            "cuts": cuts, "onsets": ons}


# ----------------------------------------------------------------- prompt writing


_WORLD_RULES = """Write prompts that work in any modern video generator.

  * NEVER name a model, a duration, an aspect ratio, a resolution or any generation
    setting inside the prompt. Those are chosen outside it.
  * One flowing line per prompt. No line breaks, no bullet points, no headings.
  * Describe MOTION, not a still. A prompt that reads like a photograph produces a
    photograph that wobbles.
  * Keep it concrete. "Flickering fluorescent tube over wet tiles" beats "eerie lighting"."""

PROMPT_SYSTEM = """You write prompts for dreamcore / liminal-space short videos.

A dreamcore short is a series of empty, uncanny, half-remembered places. No people, no
story, no dialogue. The feeling is "somewhere you have been but cannot place, and nobody
is there". Carpeted corridors, drained pools, stairwells, parking decks at 3am, hotel
lobbies with no guests, playgrounds under sodium light, endless indoor pools.

THE ONE THING THAT MAKES THIS WORK - the cuts are IN the prompt:

  * Each prompt must produce a clip that contains {cuts_per_clip} HARD CUTS inside it.
    Not a pan, not a dissolve, not a camera move: a hard cut to a different place in the
    same world. Write them explicitly, with times, in the prompt text - for example
    "hold for {phrase} seconds, then hard cut to ..., hold for {phrase} seconds, then hard
    cut to ...".
  * Use these exact hold lengths: {hold} seconds per shot. That is one musical phrase of
    the track this gets cut to, plus a small margin - generators never honour a hold time
    to the frame, and it is far better to have half a second too much (which is trimmed)
    than half a second too little (which has to be slowed).
  * Every shot inside a clip is a DIFFERENT place in the same world - a different room, a
    different corridor, a different level - never the same room from another angle.

HOLDING THE WORLD TOGETHER:
  * Write a WORLD sentence first: the place-type, palette, light source, era, film look,
    and the level of decay. Be specific enough that two separate generations land in the
    same building.
  * Begin every prompt with that exact same world sentence, word for word. Do not
    paraphrase it between prompts.

""" + _WORLD_RULES + """

DREAMCORE SPECIFICS that carry the aesthetic:
  * Camera: locked off or a very slow push. No handheld, no whip pans.
  * Emptiness is the subject. If a person appears the shot is wrong.
  * Light comes from inside the frame: fluorescent tubes, exit signs, pool lights, a TV.
  * Slight wrongness beats obvious horror: a door where a wall should be, a corridor that
    repeats, water indoors, a ceiling too low.
  * Look: consumer-camcorder or early digital, soft grain, slight chroma bleed, no
    cinematic colour grade.

Return JSON:
  {"world": "<the shared world sentence>",
   "prompts": [{"label": "<3-5 words>", "shots": ["<shot 1 place>", "<shot 2 place>"],
                "text": "<the full one-line prompt, cuts and hold times included>"}]}"""


def prompts_for(brief: str, clip_count: int = 4, cuts_per_clip: int = 2,
                phrase: float = 3.85, status_cb=None, model: str = PROMPT_MODEL) -> dict:
    """Copy-ready prompts, each producing one clip that already contains its own cuts."""
    _log(status_cb, f"Writing {clip_count} prompts, {cuts_per_clip} cuts inside each...")
    agent_core.assert_wavespeed_balance(status_cb=status_cb)
    system = (PROMPT_SYSTEM
              .replace("{cuts_per_clip}", str(int(cuts_per_clip)))
              .replace("{hold}", f"{phrase + HOLD_MARGIN:.1f}")
              .replace("{phrase}", f"{phrase:.1f}"))
    ask = (f"The idea: {str(brief or '').strip() or 'liminal spaces, no people'}\n"
           f"Write exactly {int(clip_count)} prompts. Each produces one clip holding "
           f"{int(cuts_per_clip) + 1} shots separated by {int(cuts_per_clip)} hard cuts.")
    out = agent_core._post_llm_json(
        model, [{"role": "system", "content": system}, {"role": "user", "content": ask}],
        max_tokens=4000, temperature=0.75, timeout=420) or {}
    if isinstance(out, list):
        out = {"prompts": out}
    prompts = []
    for i, item in enumerate((out.get("prompts") or [])[:int(clip_count)]):
        if isinstance(item, dict):
            text = " ".join(str(item.get("text") or "").split())
            label = str(item.get("label") or "").strip()
            shots = [str(s).strip() for s in (item.get("shots") or []) if str(s).strip()]
        else:
            text, label, shots = " ".join(str(item).split()), "", []
        if text:
            prompts.append({"label": label or f"Clip {i + 1}", "text": text, "shots": shots})
    if not prompts:
        raise RuntimeError("No prompts came back.")
    _log(status_cb, f"{len(prompts)} prompts ready "
                    f"({len(prompts) * (cuts_per_clip + 1)} shots at {phrase:.2f}s each = "
                    f"{len(prompts) * (cuts_per_clip + 1) * phrase:.1f}s of video).")
    return {"world": " ".join(str(out.get("world") or "").split()), "prompts": prompts,
            "phrase": phrase, "cuts_per_clip": int(cuts_per_clip)}


# ----------------------------------------------------------------- reading the uploads


def _probe_seconds(path) -> float:
    exe = shutil.which("ffprobe") or "ffprobe"
    try:
        return max(0.0, float(subprocess.run(
            [exe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True, timeout=120).stdout.strip()))
    except Exception:  # noqa: BLE001
        return 0.0


def shots_in(clip, threshold: float = SCENE_THRESHOLD) -> list[dict]:
    """Split one uploaded clip at its own hard cuts.

    The prompt asked the generator for cuts; whether it obeyed is a fact about the file, so
    it is measured here rather than assumed. A clip that came back as one continuous take
    simply yields one shot, and the editor treats it as one phrase.
    """
    ffmpeg = str(pipeline.find_ffmpeg() or "ffmpeg")
    duration = _probe_seconds(clip)
    if duration <= 0:
        return []
    out = subprocess.run(
        [ffmpeg, "-v", "error", "-i", str(clip), "-filter_complex",
         f"select='gt(scene,{threshold})',metadata=print:file=-", "-an", "-f", "null", "-"],
        capture_output=True, text=True, timeout=900).stdout
    marks = []
    for line in out.splitlines():
        if "pts_time:" in line:
            try:
                t = float(line.split("pts_time:")[1].split()[0])
            except (IndexError, ValueError):
                continue
            if MIN_SHOT < t < duration - MIN_SHOT:
                marks.append(round(t, 3))
    bounds = [0.0] + sorted(marks) + [round(duration, 3)]
    shots = []
    for a, b in zip(bounds, bounds[1:]):
        if b - a >= MIN_SHOT:
            shots.append({"clip": str(clip), "start": round(a, 3), "end": round(b, 3),
                          "seconds": round(b - a, 3)})
    return shots


# ----------------------------------------------------------------- the edit


def _cut_shot(src, start, take, slot, dst, ffmpeg) -> None:
    """One shot -> one house-format part occupying exactly `slot` seconds, video only.

    `take` is how much source is read, `slot` is how long it plays. When they differ the
    clip is time-stretched by setpts (>1 = slower), which is what lets a 3.7s generation
    fill a 3.85s musical phrase. Audio is dropped on purpose: the generated clips carry
    whatever noise the generator invented, and the finished short runs on the music alone.
    """
    factor = max(0.001, float(slot) / max(0.001, float(take)))
    res = subprocess.run(
        [ffmpeg, "-y", "-ss", f"{start:.3f}", "-i", str(src), "-t", f"{take:.3f}",
         "-an", "-vf", (f"scale={W}:{H}:force_original_aspect_ratio=increase,"
                        f"crop={W}:{H},setpts=(PTS-STARTPTS)*{factor:.6f},fps={FPS}"),
         "-t", f"{slot:.3f}",
         "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
         str(dst)], capture_output=True, text=True, timeout=1800)
    if res.returncode != 0 or not Path(dst).exists():
        raise RuntimeError(f"Could not cut {Path(src).name}: {res.stderr.strip()[-300:]}")


def plan_edit(shots: list, grid: dict, target_seconds: float | None = None) -> dict:
    """Lay the available shots onto the musical grid.

    Rules, in order:
      * a shot fills exactly one phrase, so every cut lands on a grid point
      * a shot longer than a phrase is trimmed from its start (the generated cut reveals
        the new place at the start; the tail is usually the drift)
      * a shot a little SHORTER than a phrase is slowed to fill it, by up to SPEED_SLACK.
        This is the common case and it needs saying why: generators do not honour a hold
        time to the frame, so asking for 3.85s returns 3.6-3.7s. Falling back to a half
        phrase there was measurably wrong - it turned a 3.7s shot into a 1.93s one and put
        the cuts on half-grid points, of which only some sit on a note. A 4% slow-down on
        a slowly drifting dreamcore shot is invisible, and it is not a freeze: every output
        frame still comes from a different source frame.
      * a shot that is short by more than that takes a HALF phrase instead - still a
        musical position - and only a shot too short for even that is dropped
      * shots that do not fit inside the music are dropped, and reported
    """
    phrase = float(grid.get("phrase") or 3.85)
    half = phrase / 2.0
    limit = float(target_seconds or grid.get("duration") or 0.0)
    placed, dropped = [], []
    t = 0.0
    for shot in shots:
        have = float(shot.get("seconds") or 0.0)
        if have >= phrase * (1.0 - SPEED_SLACK):
            slot = phrase
        elif have >= half * (1.0 - SPEED_SLACK):
            slot = half
        else:
            dropped.append({**shot, "reason": f"only {have:.2f}s, shorter than half a phrase"})
            continue
        if limit and t + slot > limit + 0.01:
            dropped.append({**shot, "reason": "past the end of the music"})
            continue
        # take is what we read from the source; slot is what it occupies on the timeline
        take = min(have, slot)
        placed.append({**shot, "at": round(t, 3), "take": round(take, 3),
                       "slot": round(slot, 3),
                       "speed": round(take / slot, 4) if slot > 0 else 1.0,
                       "trimmed": round(max(0.0, have - slot), 3)})
        t = round(t + slot, 3)
    return {"parts": placed, "dropped": dropped, "seconds": round(t, 3), "phrase": phrase}


def edit_to_music(clips: list, bed_path, out_path, status_cb=None, work_dir=None,
                  target_seconds: float | None = None) -> dict:
    """Uploaded clips + a music bed -> one short whose every cut sits on the melody."""
    ffmpeg = str(pipeline.find_ffmpeg() or "ffmpeg")
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    work = Path(work_dir or out_path.parent / "_dreamcore")
    work.mkdir(parents=True, exist_ok=True)

    grid = music_grid(bed_path, status_cb=status_cb)
    shots = []
    for i, clip in enumerate(clips, 1):
        found = shots_in(clip)
        lengths = ", ".join("%.1fs" % s["seconds"] for s in found) or "unreadable"
        _log(status_cb, f"Clip {i}: {len(found)} shot(s) ({lengths}).")
        shots.extend(found)
    if not shots:
        raise RuntimeError("None of the uploaded clips could be read.")

    plan = plan_edit(shots, grid, target_seconds)
    if not plan["parts"]:
        raise RuntimeError("No shot was long enough to fill even half a musical phrase.")
    _log(status_cb, f"Edit: {len(plan['parts'])} shot(s) on the grid = {plan['seconds']:.2f}s"
                    + (f"; {len(plan['dropped'])} dropped" if plan["dropped"] else ""))

    parts = []
    for i, part in enumerate(plan["parts"], 1):
        dst = work / f"shot{i:02d}.mp4"
        _cut_shot(part["clip"], part["start"], part["take"], part["slot"], dst, ffmpeg)
        parts.append(dst)

    listing = work / "concat.txt"
    listing.write_text("".join(f"file '{p.as_posix()}'\n" for p in parts), encoding="utf-8")
    silent = work / "cut_silent.mp4"
    res = subprocess.run(
        [ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", str(listing),
         "-c", "copy", str(silent)], capture_output=True, text=True, timeout=1800)
    if res.returncode != 0 or not silent.exists():
        raise RuntimeError(f"Joining failed: {res.stderr.strip()[-400:]}")

    # The music must start where the grid starts, or every cut is early by the offset.
    total = plan["seconds"]
    fade = min(1.2, total / 4.0)
    res = subprocess.run(
        [ffmpeg, "-y", "-i", str(silent), "-ss", f"{grid['offset']:.3f}", "-i", str(bed_path),
         "-t", f"{total:.3f}",
         "-af", f"afade=t=out:st={max(0.0, total - fade):.3f}:d={fade:.3f},aresample=48000",
         "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ac", "2",
         "-map", "0:v:0", "-map", "1:a:0", "-shortest",
         "-movflags", "+faststart", str(out_path)],
        capture_output=True, text=True, timeout=1800)
    if res.returncode != 0 or not out_path.exists():
        raise RuntimeError(f"Muxing the music failed: {res.stderr.strip()[-400:]}")

    report = {"video": str(out_path), "seconds": plan["seconds"], "grid": grid,
              "parts": plan["parts"], "dropped": plan["dropped"],
              "work_dir": str(work)}
    (work / "edit_report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    _log(status_cb, f"Done: {plan['seconds']:.2f}s, {len(parts)} shots, "
                    f"cuts every {grid['phrase']:.2f}s.")
    return report


def verify_edit(video_path, bed_path=None, status_cb=None) -> dict:
    """Measure the finished file: are the cuts really on the music?

    The whole feature is a timing claim, and a timing claim that is not measured on the
    rendered file is only an intention. Reports the same numbers the reference was judged
    by, so the two can be compared directly.
    """
    cuts_found = [s["start"] for s in shots_in(video_path)][1:]
    ons, _dur = onsets(bed_path or video_path)
    if not cuts_found:
        return {"cuts": [], "on_music": 0, "total": 0}
    deltas = [min(abs(c - o) for o in ons) for c in cuts_found] if ons else []
    near = sum(1 for d in deltas if d <= ONSET_TOLERANCE)
    _log(status_cb, f"Verify: {near}/{len(cuts_found)} cuts within "
                    f"{int(ONSET_TOLERANCE * 1000)} ms of a note "
                    f"(median {np.median(deltas) if deltas else 0:.3f}s).")
    return {"cuts": cuts_found, "deltas": [round(d, 3) for d in deltas],
            "on_music": near, "total": len(cuts_found)}
