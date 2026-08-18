"""Dreamcore prompt writing and music-aware assembly.

The reference folder contains several different edit grammars, not one universal cadence:
the common four-shot reveal at roughly 3.7-second intervals, almost unbroken passages with
one late reveal (or no cut), and deliberately unstable memory-glitch bursts. Higgsfield can
only generate ten seconds at a time, so every generated prompt is a self-contained 10s
chapter that explicitly names one of those grammars. The final editor may still snap an
internal cut to the selected music, but prompt generation no longer forces every idea into
the same three-shot template or the same sunny cloud suburb.
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


_REFERENCE_DNA = """You write production-ready prompts for 10-second vertical dreamcore videos.

REFERENCE DNA (derived from the full local reference folder, never copied shot-for-shot):
* Begin with something immediately readable and ordinary: civic infrastructure, a transit
  interior, a domestic object, a shop, a school corridor, a road, a playground, a machine.
* Break reality with ONE dominant spatial rule: impossible repetition, impossible scale,
  an interior turned inside-out, architecture suspended in weather, a route with no valid
  destination, or familiar geometry folding into a physically impossible system.
* The viewer understands the ordinary anchor first and discovers the impossible rule next.
  Weird decoration alone is not dreamcore. The contradiction must be visible in the frame.
* Use clean, deep-focus images with strong perspective and a readable silhouette. Lighting
  may be hard blue midday, pale overcast, candy-pastel afterglow, sodium night, fluorescent
  interior or wet neon. The references do NOT all use blue sky, clouds or suburban houses.
* Emptiness is common, but not an absolute law. If a distant human presence improves scale,
  keep it anonymous and secondary. Never make a talking character the subject.
* Motion has intent: slow forward travel, lateral glide, crane reveal, orbit, or a perfectly
  still observation used as contrast. Do not add generic handheld wobble.

NOVELTY:
Do not recreate the references' recognizable combinations (colourful houses on a cloud
cliff, an endless cloud road, a neighbourhood cylinder, a ferris wheel above Earth, a train
filled with clouds, or a giant slide from orbit). Abstract their visual grammar and invent a
new ordinary anchor, impossible rule, palette, materials and reveal. Across multiple prompts,
build a coherent collection rather than repeating the same location.

CREATIVE RANGE:
Possible families include impossible public utilities, recursive service corridors, soft
technology ruins, indoor weather systems, nostalgia enlarged to civic scale, transit limbo,
domestic architecture obeying the wrong gravity, and quiet places governed by an absurd
physical rule. These are starting axes, not subjects to copy verbatim.
"""

_PROMPT_RULES = """PROMPT CONSTRUCTION:
* Every output text is one copy-ready prompt for EXACTLY 10.0 seconds and vertical 9:16.
* State the requested edit pattern and every cut time explicitly. A hard cut is not a pan,
  dissolve, morph or zoom. A continuous pattern contains no hidden cut.
* For each shot, name the lens/view height, camera path, foreground anchor, perspective,
  impossible spatial fact, materials, palette, light direction and what new information the
  shot reveals. Use concrete nouns and geometry, not mood adjectives as substitutes.
* Preserve anchor identity, materials, palette and lighting across cuts within a chapter.
  In Memory Glitch only, adjacent micro-shots may jump between related locations while one
  recurring object and one colour signature keep the sequence legible.
* End on the strongest spatial reveal or unresolved image. Do not waste the final second on
  a fade, logo, title, black frame or generic beauty shot.
* Natural environmental motion is allowed when it proves scale, but architecture and anchor
  objects must not melt, mutate or randomly change identity.
* Include: no dialogue, no captions, no logo, no watermark. Avoid long negative lists.

Return strict JSON:
{"world":"<one-sentence collection-level visual logic>",
 "prompts":[{"label":"<specific 3-6 word concept>",
             "edit_style":"<the assigned style id>",
             "cut_times":[<seconds>],
             "shots":["<short concrete shot summary>"],
             "text":"<complete one-line 10-second generation prompt>"}]}
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

HOW MUCH DETAIL:
  Write enough concrete visual information to control every assigned shot, normally 60-100
  words per deliberate shot. Micro-shots in Memory Glitch are concise by necessity. Spend
  detail on camera, geometry, materials, palette, lighting and the visual reveal; do not pad
  the prompt with synonyms for a mood. Follow the JSON contract above exactly."""


PROMPT_SYSTEM = _REFERENCE_DNA + "\n\n" + _PROMPT_RULES + "\n\n" + _SAFETY_RULES

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


EDIT_STYLES = {
    "classic_reveal": {
        "name": "Classic reveal",
        "cuts": [3.7, 7.4],
        "direction": ("Three deliberate shots of one place. Begin inside the readable "
                      "ordinary anchor, cut wider at 3.7s, then reveal the full impossible "
                      "geometry at 7.4s. Every cut must increase spatial understanding."),
    },
    "continuous_passage": {
        "name": "Continuous passage",
        "cuts": [],
        "direction": ("One uninterrupted 10-second camera move with no cuts, dissolves or "
                      "teleports. The impossible rule becomes legible through parallax, "
                      "occlusion and a controlled change in camera height or direction."),
    },
    "late_reveal": {
        "name": "Late reveal",
        "cuts": [7.4],
        "direction": ("Hold one patient exploratory move until 7.4s, then hard-cut once to "
                      "a radically clearer scale reveal for the final 2.6 seconds."),
    },
    "memory_glitch": {
        "name": "Memory glitch",
        # Measured from the two rapid-cut references: four ~0.3s fragments followed by a
        # longer hold, repeated in waves. It is intentionally opt-in because generators
        # obey the other three patterns more reliably.
        "cuts": [0.3, 0.6, 0.9, 1.2, 2.4, 2.7, 3.0, 3.3, 4.9, 5.2, 5.5, 5.8, 7.4, 7.7, 8.0, 8.3],
        "direction": ("A controlled memory-overflow montage: four 0.3-second hard-cut "
                      "fragments, a longer held image, then two more related bursts. Keep "
                      "one recurring object and one colour signature so it reads as an "
                      "intentional memory fracture rather than random stock images."),
    },
}


def edit_patterns_for(style: str, clip_count: int) -> list[dict]:
    """Reference-derived edit assignments for fixed 10-second Higgsfield generations."""
    key = str(style or "auto").strip().lower()
    if key in EDIT_STYLES:
        keys = [key] * max(1, int(clip_count))
    else:
        # Auto deliberately mixes the reliable reference languages. Memory Glitch remains
        # explicit: asking a video model for sixteen exact cuts is useful when wanted, but
        # a bad default for somebody expecting two dependable generations.
        cycle = ("classic_reveal", "continuous_passage", "late_reveal")
        keys = [cycle[i % len(cycle)] for i in range(max(1, int(clip_count)))]
    return [{"id": k, **EDIT_STYLES[k]} for k in keys]


def prompts_for(brief: str, clip_count: int = 2, cuts_per_clip: int = 2,
                phrase: float = 3.85, clip_seconds: float = 10.0,
                status_cb=None, model: str = PROMPT_MODEL,
                edit_style: str = "auto") -> dict:
    """Create independent, copy-ready 10s prompts from the measured reference grammar.

    ``cuts_per_clip`` stays in the signature for old saved projects and callers. New runs
    use ``edit_style``; leaving the direction blank is a supported creative mode.
    """
    clip_seconds = 10.0  # Higgsfield's actual generation unit; do not imply longer clips.
    patterns = edit_patterns_for(edit_style, clip_count)
    _log(status_cb, "Writing 10-second Dreamcore prompts: "
         + ", ".join(p["name"] for p in patterns) + ".")
    agent_core.assert_wavespeed_balance(status_cb=status_cb)
    direction = str(brief or "").strip()
    if direction:
        creative_input = ("Optional user direction follows. Preserve its useful subject or "
                          "constraints, but transform it through the reference DNA and do not "
                          f"illustrate it literally:\n{direction}")
    else:
        creative_input = ("The user supplied no direction. Be the creative director: invent a "
                          "fresh, specific collection concept without asking a question. Avoid "
                          "the recognizable reference combinations listed in NOVELTY and avoid "
                          "the generic fallback of an empty corridor or cloud suburb.")
    assignments = []
    for i, pattern in enumerate(patterns, 1):
        cut_text = ", ".join(f"{x:.1f}s" for x in pattern["cuts"]) or "none"
        assignments.append(
            f"Clip {i}: edit_style={pattern['id']}; hard cut times={cut_text}; "
            f"{pattern['direction']}")
    ask = (creative_input + "\n\nCreate exactly " + str(int(clip_count))
           + " distinct 10.0-second prompts. Follow these per-clip assignments exactly:\n"
           + "\n".join(assignments)
           + "\nThe concepts should feel authored as one collection, but each chapter must "
             "have its own ordinary anchor and impossible spatial rule. Return only JSON.")
    budget = min(24000, 2200 + int(clip_count) * 1500)
    out = agent_core._post_llm_json(
        model, [{"role": "system", "content": PROMPT_SYSTEM},
                {"role": "user", "content": ask}],
        max_tokens=budget, temperature=0.92 if not direction else 0.82, timeout=900) or {}
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
            assigned = patterns[i]
            prompts.append({"label": label or f"Clip {i + 1}", "text": text,
                            "shots": shots, "edit_style": assigned["id"],
                            "cut_times": assigned["cuts"]})
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
    _log(status_cb, f"{len(prompts)} independent 10-second prompts ready.")
    return {"world": " ".join(str(out.get("world") or "").split()), "prompts": prompts,
            "phrase": phrase, "edit_style": str(edit_style or "auto")}


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


def shots_in(clip, threshold: float = SCENE_THRESHOLD,
             min_shot: float = MIN_SHOT) -> list[dict]:
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
        if b - a >= float(min_shot):
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
        # Continuous, late-reveal and memory-glitch chapters carry an authored timing
        # inside the generated 10s file. Re-quantising them to 3.85s phrases would erase
        # exactly the reference edit language the user selected.
        if shot.get("preserve_duration"):
            slot = have
        elif have >= phrase * (1.0 - SPEED_SLACK):
            slot = phrase
        elif have >= half * (1.0 - SPEED_SLACK):
            slot = half
        else:
            dropped.append({**shot, "reason": f"only {have:.2f}s, shorter than half a phrase"})
            continue
        # Pull the END of this shot onto a real note. Only accept the snap while the shot
        # still has the material to fill it - a longer slot than the clip can cover would
        # be a slow-down beyond SPEED_SLACK, which is visible.
        snapped = _snap(t + slot) if not shot.get("preserve_duration") else t + slot
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
                  target_seconds: float | None = None,
                  prompt_meta: list | None = None) -> dict:
    """Uploaded clips + a music bed -> one short whose every cut sits on the melody."""
    ffmpeg = str(pipeline.find_ffmpeg() or "ffmpeg")
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    work = Path(work_dir or out_path.parent / "_dreamcore")
    work.mkdir(parents=True, exist_ok=True)

    grid = music_grid(bed_path, status_cb=status_cb)
    shots = []
    for i, clip in enumerate(clips, 1):
        prompt = ((prompt_meta or [])[i - 1]
                  if i - 1 < len(prompt_meta or []) else {})
        style = str((prompt or {}).get("edit_style") or "classic_reveal")
        if style == "continuous_passage":
            # Force one source shot even if the generator introduces a harmless exposure
            # jump that scene detection mistakes for an edit.
            found = shots_in(clip, threshold=1.1, min_shot=0.1)
        elif style == "memory_glitch":
            found = shots_in(clip, threshold=0.18, min_shot=0.12)
        else:
            found = shots_in(clip)
        if style in {"continuous_passage", "late_reveal", "memory_glitch"}:
            for shot in found:
                shot["preserve_duration"] = True
                shot["edit_style"] = style
        lengths = ", ".join("%.1fs" % s["seconds"] for s in found) or "unreadable"
        _log(status_cb, f"Clip {i}: {style}, {len(found)} shot(s) ({lengths}).")
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
