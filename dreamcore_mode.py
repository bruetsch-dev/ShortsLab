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
import re
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
  * Describe the CAMERA's motion in every shot. A prompt that reads like a photograph
    produces a photograph that wobbles. (The world itself stays still - see below.)
  * Keep it concrete. "A slow steady dolly forward down the centre line of the road, the
    same red clapboard house repeating to the vanishing point" beats "eerie empty street"."""

_AESTHETIC = """You write prompts for dreamcore short videos.

WHAT THIS ACTUALLY IS - measured from the reference reels, not from the word "dreamcore":

    ONE ORDINARY THING, REPEATED FOREVER, IN AN IMPOSSIBLE PLACE.

  A row of small colourful suburban houses along a road - and the road runs along the edge
  of a cliff that drops into a sea of clouds, and the houses repeat past the horizon. A
  two-lane road on a narrow strip of grass floating above the clouds, a yellow warning sign
  and one abandoned car on it. An entire neighbourhood wrapped around the INSIDE of a
  mile-wide tube with a disc of blue sky at the far end. A door standing open in a sand
  dune. A corridor lined with switched-on televisions.

  The OBJECTS are always mundane - houses, kerbs, lawns, road markings, road signs, an old
  car, lamps, TVs. The GEOMETRY is what is impossible: the scale, the repetition, where the
  ground stops. Never dress a mundane object up to be strange; put it somewhere it cannot be.

THE CAMERA - the most important rule, and the one most often written wrong:
  * The camera MOVES IN EVERY SHOT, and it is the ONLY thing that moves.
  * The move is a slow, steady, CONSTANT-SPEED dolly FORWARD along the perspective line -
    down the road, down the row, down the tube. No acceleration, no handheld, no shake, no
    whip pan, no zoom. A camera gliding on rails, or a drone holding a straight line.
  * The other two moves that belong here: a slow rise (crane up) that reveals more of the
    impossible shape, and a slow tilt down from level to looking-down. Both at the same
    unhurried constant speed.
  * NEVER write "locked off", "static camera" or "the camera holds still". A still frame of
    a still world is a photograph, and the reference is never that.
  * Write the move into the prompt for every single shot, in those words.

THE WORLD IS FROZEN:
  * Nothing in the world moves. No wind, no people, no traffic, no swaying grass, no
    rippling water, no flickering lights, no dust in a beam.
  * That stillness against a moving camera IS the uncanny feeling. "A curtain moves in an
    unfelt draught" is the single most common way to break it.
  * No people. No animals. Vehicles are parked and empty.

THE LOOK - all four references agree, and every one of these is the opposite of "eerie":
  * A pristine hyper-real 3D render. Sharp and clean: no grain, no dirt, no wear, no VHS,
    no chroma bleed, no vignette, no camcorder timestamp.
  * DEEP FOCUS. Everything from the near kerb to the vanishing point is in focus. No bokeh,
    no shallow depth of field.
  * BRIGHT HARD SUNLIGHT from one side, mid-day, with sharp consistent shadows falling the
    same way in every shot. A deep blue sky with huge white cumulus towers. Never overcast,
    never grey, never "no sun", never flat and shadowless.
  * Cheerful saturated colour: primary-coloured clapboard houses, emerald mown grass, white
    cloud, deep blue sky. Or the candy variant - pink, lilac and cyan pastels. THE CONTRAST
    between a happy palette and total emptiness is the entire effect. A sad, drained or
    grey palette kills it dead.

COMPOSITION:
  * One-point perspective, every time. A road, a row of houses, a kerb, a corridor, a tube
    - something runs from the bottom of the frame to a vanishing point and the eye follows
    it. Centre it and make it symmetrical, or run it as a hard diagonal.
  * The repetition must be visible IN the frame: the same house, the same lamp, the same
    window, receding until it is too small to count.
  * Shot in 9:16, so the tall axis carries the sky above and the ground below.

THE SHOTS ARE ONE WORLD FROM FURTHER AND FURTHER OUT:
  * Not different places. The SAME place, seen from a new vantage after every cut, each one
    revealing more of how impossible it is.
  * A good order: inside it at eye level -> close on one of the repeated objects -> wide
    down the whole row -> a high aerial, tilted down, that shows the true shape and scale
    (the strip is a slab in the clouds; the houses go on for ever; the street is inside a
    cylinder). The last shot is the reveal and it should be the biggest.
  * Every shot keeps the same sun direction, the same palette, the same materials.

"""


_CUT_RULES = """THE ONE THING THAT MAKES THIS WORK - the cuts are IN the prompt:

  * Each prompt must produce a clip that contains {cuts_per_clip} HARD CUTS inside it.
    Not a pan, not a dissolve, not a camera move: a hard cut to a new vantage on the same
    world. Write them explicitly, with times, in the prompt text.
  * The clip is about {clip_seconds} seconds long, and the hold times are FIXED:
    {hold_sequence}, in that order. Write exactly those numbers into the prompt. They are
    musical phrases of the track this gets cut to, plus a small margin - generators never
    honour a hold to the frame, and half a second too much is trimmed away while half a
    second too little has to be slowed down.
  * Do not add a shot beyond that list. Three full phrases do not fit in a ten-second clip,
    and an extra shot is simply cut off at the end of the generation.

HOLDING THE WORLD TOGETHER:
  * Write a WORLD sentence first: the ordinary object that repeats, the impossible geometry
    it sits in, the saturated palette, the sun direction, and the clean-render look. Be
    specific enough that two separate generations land in the same place under the same sun.
  * Begin every prompt with that exact same world sentence, word for word. Do not paraphrase
    it between prompts.

"""


_SAFETY_RULES = """KEEPING THE PROMPT CLEAR OF SAFETY FILTERS - this costs nothing and saves whole clips:
  * Write what IS there, never what is absent. "No people anywhere" puts a person into the
    prompt, and generators act on the noun, not the negation. Say "the street stands
    unoccupied", "the architecture and the light are the only subjects".
  * Stay out of rooms that read as surveillance of undressed people even when empty:
    changing cubicles, locker aisles, shower rooms, saunas, toilets, bedrooms.
  * Avoid wording that reads as a body or a crime scene: skin, flesh, bare, blood, stains
    that "spread", a trail "that stops", something "dragged".
  * Prefer "closed for the night", "long empty", "out of season" to "abandoned", "derelict"
    or "decaying" - same mood, and the first set does not co-occur with disaster imagery.
  * The camera is a camera, not a hidden one. Never "hidden camera", "security footage of",
    "spy cam", "found tape of someone".

WORDS THAT DESTROY IT - these come from travel and stock footage, and a generator obeys them
over anything else in the prompt. Never write them:
  golden hour, magic hour, sunset, sunrise, sun-drenched, dappled sunlight, cinematic, epic,
  majestic, breathtaking, stunning, gorgeous, serene, peaceful, tranquil, idyllic, lush,
  rolling hills, vista, panorama, drone shot, aerial flyover, lens flare, god rays, nature
  documentary, travel film.
  One "golden hour" turns the whole clip into a meditation-app background. Say instead:
  high mid-day sun from the left, hard sharp shadows, a deep blue sky with towering white
  cumulus, a slow steady dolly forward, a high vantage tilted down.
  And these are useless because a generator cannot render an adjective - it renders the
  nouns beside them: eerie, uncanny, liminal, surreal, dreamlike, unsettling, mysterious,
  ethereal, haunting. Name the object and the geometry instead.

HOW MUCH DETAIL - this is not optional, and short prompts are the usual failure:
  Write 60-110 words FOR EACH SHOT, not for the whole prompt. A generator fills everything
  you leave unsaid with the average of its training data, and the average of "empty street"
  is a stock photograph. Every shot names, concretely:
    - the camera move, in words, with its direction and its constant slow speed
    - the camera height and whether it is level, tilted down, or looking up
    - the vanishing point and what leads the eye into it
    - the repeated object, how many are visible, and how far back they are still countable
    - the impossible geometry and where exactly the ordinary ground stops
    - materials and their colours: clapboard, asphalt, kerbstone, mown turf, roof shingle,
      painted steel - each with its actual colour named
    - the sun: its direction, the hardness of the shadows, where they fall
    - the sky: its blue, the shape and size of the cumulus, how much of the frame it takes
    - that the air is completely still and nothing in the world moves
  Do not repeat the world sentence's contents inside each shot - build ON it.

Return JSON:
  {"world": "<the shared world sentence>",
   "prompts": [{"label": "<3-5 words>", "shots": ["<shot 1 vantage>", "<shot 2 vantage>"],
                "text": "<the full one-line prompt, cuts and hold times included>"}]}"""


PROMPT_SYSTEM = _AESTHETIC + _CUT_RULES + _WORLD_RULES + "\n\n" + _SAFETY_RULES

# Wording that gets a perfectly innocent liminal prompt refused. Two kinds: rooms whose name
# alone reads as surveillance of undressed people, and negations - a generator acts on the
# noun in "no people", which is why the house rule everywhere else is positive framing.
_SAFETY_PATTERNS = (
    (r"\b(changing|dressing|fitting)\s+(room|cubicle|area|stall)s?\b", "changing room"),
    (r"\block(er)?\s*(room|aisle|bank)s?\b", "locker room"),
    (r"\bshower(s|\s+room|\s+block|\s+head)?\b", "shower"),
    (r"\b(sauna|steam\s*room|changing\s*curtain|toilet|urinal|bathroom stall)\b", "washroom"),
    (r"\b(bedroom|motel bed|unmade bed)\b", "bedroom"),
    (r"\b(naked|nude|bare skin|flesh|body|bodies|corpse|blood)\b", "body word"),
    (r"\b(hidden|spy|secret)\s+cam(era)?\b", "hidden camera"),
    (r"\bsecurity (footage|cam)", "surveillance framing"),
    (r"\bno (people|one|person|humans?)\b", "negation"),
    (r"\b(nobody|no-one)\b", "negation"),
    (r"\bwithout (any )?(people|humans)\b", "negation"),
)


# Travel-and-stock vocabulary. A generator weighs "golden hour" far above "liminal", so one
# of these words is enough to turn a surreal-cliffs brief into meditation-app b-roll - which
# is exactly what happened: the clip came back as terraced hills in warm sunlight, and
# Gemini named it "stock nature b-roll, meditation video background".
_STOCK_PATTERNS = (
    (r"\bgolden[- ]hour\b|\bmagic hour\b", "golden hour"),
    (r"\bsun(set|rise)\b|\bsetting sun\b", "sunset"),
    (r"\bsun[- ]drenched\b|\bdappled sun\w*|\bwarm (afternoon |evening )?sun\w*", "warm sun"),
    (r"\bcinematic\b|\bepic\b|\bmajestic\b|\bbreathtaking\b|\bstunning\b", "ad language"),
    (r"\bserene\b|\bpeaceful\b|\btranquil\b|\bidyllic\b|\blush\b", "ad language"),
    (r"\brolling hills\b|\bvista\b|\bpanorama\b|\bsweeping landscape\b", "postcard"),
    (r"\bdrone (shot|footage)\b|\baerial (shot|view)\b|\bfly[- ]?over\b", "drone shot"),
    (r"\blens flare\b|\bgod rays\b|\bsun ?beams? (streaming|pouring)", "flare"),
    (r"\bnature documentary\b|\btravel (film|video)\b|\bnational geographic\b", "stock genre"),
)

# Adjectives that name the FEELING instead of the fact. A generator cannot render "uncanny";
# it renders the nouns around it, which is a normal room.
_VAGUE_PATTERNS = (
    (r"\bliminal\b", "liminal"), (r"\buncanny\b", "uncanny"), (r"\beerie\b", "eerie"),
    (r"\bsurreal\b", "surreal"), (r"\bdream-?like\b", "dreamlike"),
    (r"\bunsettling\b", "unsettling"), (r"\bmysterious\b", "mysterious"),
    (r"\bethereal\b", "ethereal"), (r"\bhaunting\b", "haunting"),
)


def stock_review(text: str) -> list:
    """Words in a finished prompt that pull it toward stock landscape footage.

    Reported, never rewritten - same rule as safety_review. Knowing WHY a clip came back
    looking like a screensaver is worth more than a silent edit.
    """
    low = str(text or "").lower()
    hits = []
    for pattern, label in _STOCK_PATTERNS + _VAGUE_PATTERNS:
        if re.search(pattern, low) and label not in hits:
            hits.append(label)
    return hits


def safety_review(text: str) -> list:
    """Wording in a finished prompt that generators commonly refuse. Empty list = clean."""
    low = str(text or "").lower()
    hits = []
    for pattern, label in _SAFETY_PATTERNS:
        if re.search(pattern, low) and label not in hits:
            hits.append(label)
    return hits


def shot_plan(clip_seconds: float, phrase: float, max_shots: int = 4) -> list:
    """Hold times for one generated clip, as many musical shots as its length allows.

    A generator makes clips of a fixed length, and asking for more shots than fit gets the
    last one truncated - three full phrases need 11.55s, so in a 10s clip the third shot
    simply is not there. Full phrases are used while they fit; a HALF phrase is added if
    the remainder can carry one, because half-grid points are still on the music and the
    editor already places shots on them. Each hold carries HOLD_MARGIN so the editor trims
    rather than stretches.
    """
    budget = max(phrase, float(clip_seconds or 0) or phrase)
    holds, used = [], 0.0
    while len(holds) < max_shots and used + phrase + HOLD_MARGIN <= budget + 0.35:
        holds.append(round(phrase + HOLD_MARGIN, 1))
        used += phrase
    half = phrase / 2.0
    if len(holds) < max_shots and used + half + HOLD_MARGIN <= budget + 0.35:
        holds.append(round(half + HOLD_MARGIN, 1))
    return holds or [round(phrase + HOLD_MARGIN, 1)]


def prompts_for(brief: str, clip_count: int = 2, cuts_per_clip: int = 2,
                phrase: float = 3.85, clip_seconds: float = 10.0,
                status_cb=None, model: str = PROMPT_MODEL) -> dict:
    """Copy-ready prompts, each producing one clip that already contains its own cuts."""
    _log(status_cb, f"Writing {clip_count} prompts, {cuts_per_clip} cuts inside each...")
    agent_core.assert_wavespeed_balance(status_cb=status_cb)
    # How many shots fit is arithmetic, not a preference: the generator makes clips of a
    # fixed length, and three full phrases need 11.55s. Asking for more than fits loses the
    # last shot in every clip.
    holds = shot_plan(clip_seconds, phrase, max_shots=int(cuts_per_clip) + 1)
    hold_text = " then ".join(f"{h:.1f}s" for h in holds)
    system = (PROMPT_SYSTEM
              .replace("{cuts_per_clip}", str(max(1, len(holds) - 1)))
              .replace("{hold_sequence}", hold_text)
              .replace("{clip_seconds}", f"{float(clip_seconds):.0f}")
              .replace("{hold}", f"{phrase + HOLD_MARGIN:.1f}")
              .replace("{phrase}", f"{phrase:.1f}"))
    # The brief is named as SCENERY, not as the subject. Called "the idea" it was treated
    # as the thing to depict, and a brief about cliffs and grass came back as a landscape
    # film with a dreamcore label on it.
    ask = (f"The setting these take place in - scenery only, dreamcore is still the "
           f"subject: {str(brief or '').strip() or 'empty liminal spaces'}\n"
           f"Write exactly {int(clip_count)} prompts. Each produces ONE clip of about "
           f"{float(clip_seconds):.0f} seconds holding {len(holds)} shots with hold times "
           f"{hold_text}, separated by {max(1, len(holds) - 1)} hard cuts.")
    # max_tokens has to carry clip_count x (world sentence + 3 shots x ~100 words). At 4000
    # the last prompt came back truncated mid-shot, and a truncated prompt is a silently
    # worse video rather than an error.
    budget = min(24000, 1800 + int(clip_count) * (int(cuts_per_clip) + 1) * 320)
    out = agent_core._post_llm_json(
        model, [{"role": "system", "content": system}, {"role": "user", "content": ask}],
        max_tokens=budget, temperature=0.75, timeout=900) or {}
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
    flagged = [(p["label"], hits) for p in prompts
               if (hits := safety_review(p["text"]))]
    if flagged:
        # Reported, not rewritten. Editing a generator prompt behind the user's back is how
        # a prompt stops matching the clip it produced; naming the words lets them decide.
        _log(status_cb, "Dreamcore: wording that generators often refuse - "
             + "; ".join(f"{label}: {', '.join(h)}" for label, h in flagged))
    stock = [(p["label"], hits) for p in prompts if (hits := stock_review(p["text"]))]
    if stock:
        _log(status_cb, "Dreamcore: stock-footage wording (this is what makes a clip come "
             "back as a screensaver) - "
             + "; ".join(f"{label}: {', '.join(h)}" for label, h in stock))
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
    # SNAP TO THE NOTE, NOT TO THE ARITHMETIC. The grid is perfectly regular; the music is
    # not. On the supplied bed 7 of 49 grid points sit on no note at all, and a cut placed
    # there is audibly beside the melody - measured on the first real edit, one of two cuts
    # was 135 ms off. The onsets are where the notes actually start, so each boundary is
    # pulled to the nearest one within tolerance and the shot is cut to match.
    ons = [float(o) for o in (grid.get("onsets") or [])]
    offset = float(grid.get("offset") or 0.0)

    def _snap(boundary):
        if not ons:
            return boundary
        near = min(ons, key=lambda o: abs((o - offset) - boundary))
        return near - offset if abs((near - offset) - boundary) <= ONSET_TOLERANCE else boundary

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
        # Pull the END of this shot onto a real note. Only accept the snap while the shot
        # still has the material to fill it - a longer slot than the clip can cover would
        # be a slow-down beyond SPEED_SLACK, which is visible.
        snapped = _snap(t + slot)
        if abs(snapped - (t + slot)) > 1e-6 and snapped - t >= MIN_SHOT:
            if have >= (snapped - t) * (1.0 - SPEED_SLACK):
                slot = round(snapped - t, 3)
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
