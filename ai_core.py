"""AI Core: a rough idea becomes a storyline choice, then three ready-to-paste prompts.

    "Spongebob one day pov"
        -> five one-line storylines to choose between
        -> three video prompts for THE SAME world, different moments and angles
        -> the user generates the clips wherever they like and drops them back in

The app does not generate the video here. It does the two things that are actually hard:
holding one world steady across three separate generations, and turning a five-word idea
into a sequence worth watching. Everything between is a copy button.

Consistency is the whole game. Three prompts written independently produce three different
worlds, so a WORLD block - the same sentence about look, palette, era and rendering - is
written once and repeated verbatim at the front of every prompt. Generators drift anyway;
identical wording is the cheapest thing that holds them together.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import agent_core
import pipeline

# Cheap models on purpose: this is writing, not reasoning, and it runs twice per short.
STORY_MODEL = "google/gemini-3.5-flash"
PROMPT_MODEL = "anthropic/claude-opus-4.8"

# Rules that hold for every video generator, from the project's prompting reference:
# no model names, no durations, no aspect ratios, no resolutions, no parameter names
# inside the prompt text - those are set outside it. One line, no line breaks.
_PROMPT_RULES = """Write prompts that work in any modern video generator.

  * NEVER name a model, a duration, an aspect ratio, a resolution or any generation
    setting inside the prompt. Those are chosen outside it.
  * One flowing line per prompt. No line breaks, no bullet points, no headings.
  * Lead with the shot: what the camera is, whose eyes we are behind, what fills the
    frame. Then the action, then the world around it, then light, then sound.
  * Describe MOTION, not a still. A prompt that reads like a photograph produces a
    photograph that wobbles.
  * Keep it concrete. "Warm afternoon light through dusty blinds" beats "beautiful
    lighting"."""

STORY_SYSTEM = """You turn a rough short-video idea into five different ways it could go.

Each option is ONE short-form video built from THREE clips: same world, same character,
three different moments - a different place or a different angle each time. Think of it as
a small journey, not three unrelated shots.

Give five genuinely DIFFERENT takes on the idea, not five wordings of one take. Vary what
part of the day, story or journey you cover, and vary the emotional shape: one might be
routine and calm, one might build to a surprise, one might be pure atmosphere.

For each option:
  * title: three or four words
  * summary: one sentence a person can choose between at a glance
  * beats: exactly three, each a handful of words naming the moment and where it happens

Return JSON: {"options": [{"title": "...", "summary": "...",
                           "beats": ["...", "...", "..."]}]}"""

PROMPT_SYSTEM = """You write the three video prompts for one short.

You are given the user's idea and the storyline they chose. Produce three prompts, one per
beat, that a generator can turn into three clips which CUT TOGETHER as one video.

""" + _PROMPT_RULES + """

HOLDING THE WORLD TOGETHER - this is the part that usually fails:
  * Write a WORLD sentence first: the look, palette, era, rendering style, and the
    character's fixed appearance and clothing. Be specific enough that two different
    generations land in the same place.
  * Begin every one of the three prompts with that exact same world sentence, word for
    word. Do not paraphrase it between prompts.
  * Then continue each prompt with what is different: the moment, the place, the angle,
    the action.

WHERE THE POV GOES - it goes FIRST, always:
  * The WORLD sentence must OPEN with the point of view and who we are: "First-person POV
    as SpongeBob SquarePants, seeing his own yellow sponge arms and red tie..." - not with
    the palette, not with the style, not with the location.
  * Generators weight the opening words hardest and quietly drop what comes late. A POV
    named halfway through a sentence gets rendered as a third-person shot roughly half the
    time; named in the first four words it holds.
  * Put the concrete POV details up front too - the hands, arms, sleeves, what the body
    holds - since those are what keep the camera on the character's face height and out of
    an over-the-shoulder shot.
  * Then repeat the POV once more, briefly, inside each prompt's own action part, so a
    long prompt cannot drift out of it by the end.
  * If the idea is not a POV, the same rule applies to whatever the subject is: name it in
    the opening words of the shared sentence.

Return JSON:
  {"world": "<the shared world sentence>",
   "prompts": [{"label": "<3-5 words naming this beat>", "text": "<the full prompt>"}]}"""


def storylines(brief: str, status_cb=None, model: str = STORY_MODEL) -> list[dict]:
    """Five ways the idea could go, for the user to pick between."""
    log = status_cb or print
    log("Sketching five ways this could go...")
    agent_core.assert_wavespeed_balance(status_cb=status_cb)
    out = agent_core._post_llm_json(
        model,
        [{"role": "system", "content": STORY_SYSTEM},
         {"role": "user", "content": str(brief or "").strip()}],
        max_tokens=2000, temperature=0.85, timeout=240) or {}
    options = []
    for item in (out.get("options") or [])[:5]:
        if not isinstance(item, dict):
            continue
        beats = [str(b).strip() for b in (item.get("beats") or []) if str(b).strip()][:3]
        while len(beats) < 3:
            beats.append("")
        options.append({"title": str(item.get("title") or "Option").strip(),
                        "summary": str(item.get("summary") or "").strip(),
                        "beats": beats})
    if not options:
        raise RuntimeError("The writer returned no storylines.")
    log(f"{len(options)} storylines ready.")
    return options


def prompts_for(brief: str, storyline: dict, status_cb=None,
                model: str = PROMPT_MODEL) -> dict:
    """Three copy-ready video prompts for the chosen storyline."""
    log = status_cb or print
    log("Writing the three prompts...")
    agent_core.assert_wavespeed_balance(status_cb=status_cb)
    beats = " | ".join(str(b) for b in (storyline.get("beats") or []) if b)
    ask = (f"The idea: {str(brief or '').strip()}\n"
           f"The chosen storyline: {storyline.get('title', '')} - "
           f"{storyline.get('summary', '')}\n"
           f"Its three beats, in order: {beats}")
    out = agent_core._post_llm_json(
        model,
        [{"role": "system", "content": PROMPT_SYSTEM},
         {"role": "user", "content": ask}],
        max_tokens=3000, temperature=0.6, timeout=420) or {}
    prompts = []
    for i, item in enumerate((out.get("prompts") or [])[:3]):
        if isinstance(item, dict):
            text = str(item.get("text") or "").strip()
            label = str(item.get("label") or "").strip()
        else:
            text, label = str(item).strip(), ""
        if not text:
            continue
        # one line: a stray newline breaks paste into most generator boxes
        text = " ".join(text.split())
        prompts.append({"label": label or f"Clip {i + 1}", "text": text})
    if not prompts:
        raise RuntimeError("No prompts came back.")
    log(f"{len(prompts)} prompts ready.")
    return {"world": " ".join(str(out.get("world") or "").split()), "prompts": prompts}


# ---------------------------------------------------------------- assembly

W, H, FPS = 1080, 1920, 30


def _probe_seconds(path) -> float:
    exe = shutil.which("ffprobe") or "ffprobe"
    try:
        out = subprocess.run(
            [exe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True, timeout=60).stdout.strip()
        return max(0.0, float(out))
    except Exception:  # noqa: BLE001
        return 0.0


def _normalize(src: Path, dst: Path, ffmpeg: str) -> None:
    """One clip -> the house format, with an audio track guaranteed to exist.

    Uploaded clips come from whichever generator the user chose, so they arrive at
    different sizes, frame rates and - the one that actually breaks things - some with
    audio and some without. Concatenating a silent clip onto a loud one drops the audio
    stream for everything after it, so a silent source gets silence explicitly.
    """
    has_audio = False
    try:
        probe = shutil.which("ffprobe") or "ffprobe"
        has_audio = bool(subprocess.run(
            [probe, "-v", "error", "-select_streams", "a", "-show_entries",
             "stream=codec_type", "-of", "csv=p=0", str(src)],
            capture_output=True, text=True, timeout=60).stdout.strip())
    except Exception:  # noqa: BLE001
        pass
    cmd = [ffmpeg, "-y", "-i", str(src)]
    if not has_audio:
        cmd += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
                "-shortest"]
    cmd += ["-vf", (f"scale={W}:{H}:force_original_aspect_ratio=increase,"
                    f"crop={W}:{H},fps={FPS}"),
            "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
            "-movflags", "+faststart", str(dst)]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    if res.returncode != 0 or not dst.exists():
        raise RuntimeError(f"Could not read {src.name}: {res.stderr.strip()[-300:]}")


def assemble(clips: list, out_path, status_cb=None, work_dir=None) -> dict:
    """Join the uploaded clips, in order, into one vertical short.

    Returns {"video": str, "clips": [{"file": str, "seconds": float}]} - the per-clip
    lengths are what the timeline editor needs to lay the cuts out afterwards.
    """
    log = status_cb or print
    paths = [Path(c) for c in clips if c and Path(c).exists()]
    if not paths:
        raise RuntimeError("No clips to assemble.")
    ffmpeg = str(pipeline.find_ffmpeg())
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    work = Path(work_dir or out_path.parent / "_assemble")
    work.mkdir(parents=True, exist_ok=True)

    parts, meta = [], []
    for i, src in enumerate(paths, 1):
        log(f"Preparing clip {i} of {len(paths)}...")
        dst = work / f"part{i}.mp4"
        _normalize(src, dst, ffmpeg)
        parts.append(dst)
        meta.append({"file": str(src), "seconds": round(_probe_seconds(dst), 3)})

    log("Joining the clips...")
    listing = work / "concat.txt"
    listing.write_text("".join(f"file '{p.as_posix()}'\n" for p in parts),
                       encoding="utf-8")
    res = subprocess.run(
        [ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", str(listing),
         "-c", "copy", "-movflags", "+faststart", str(out_path)],
        capture_output=True, text=True, timeout=1800)
    if res.returncode != 0 or not out_path.exists():
        raise RuntimeError(f"Joining failed: {res.stderr.strip()[-400:]}")
    total = sum(m["seconds"] for m in meta)
    log(f"Assembled {len(parts)} clips into {total:.1f}s.")
    return {"video": str(out_path), "clips": meta, "seconds": round(total, 3)}
