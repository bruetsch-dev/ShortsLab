"""Longform doodle-video pipeline: paste a SCRIPT, get a finished 16:9 video.

Stages (fully autonomous after Start):
  1. VOICEOVER  - the script is split into sentence-safe parts and sent to Gemini TTS
                  (2.5 Flash or 2.5 Pro via WaveSpeed); parts are concatenated losslessly.
  2. TIMESTAMPS - faster-whisper transcribes the finished voiceover with word timestamps;
                  the KNOWN script is aligned onto the ASR timing (voice_align), then grouped
                  into clause lines -> transcript.txt like "[0:03.4] But somehow, ..." with
                  sub-second precision.
  3. PROMPTS    - the transcript is sent to the reasoning model (Opus 4.8 by default) with the
                  STAGE-3 doodle prompt; the app auto-replies "next" until every timestamp has
                  an image prompt, then writes image_prompts_<slug>.txt itself.
  4. IMAGES     - each prompt (timestamp stripped) goes to the user's logged-in Higgsfield
                  session: FLUX.2 Pro (unlimited), 16:9, IMAGE_CONCURRENCY generations in
                  flight (1 today - see the constant for why). Failed generations are retried;
                  a run whose generations ALL fail stops early instead of grinding out black
                  frames. A character-reference frame is generated FIRST. Files are named with
                  the timestamp AND the on-screen duration.
  5. REVIEW     - the run stops on a movable image/voiceover timeline with archived generations
                   and three optional thumbnail/title choices. Assembly begins only after approval;
                   the encoded video is checked for real black-screen spans before success.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
import concurrent.futures
import urllib.error
from pathlib import Path

from PIL import Image

import agent_core
import pipeline

ROOT = Path(__file__).resolve().parent
OUT_ROOT = agent_core.PROJECTS_DIR / "_longform"

# OFF by default, and it must stay that way: this truncates the SCRIPT, so a 20-minute video
# came out as a 2-minute one with a single voiceover part. "2-3 minutes" was about how long each
# TTS PART is (see tts_chunk_limit), never about how much of the script gets narrated.
#
# Kept only as an opt-in for cheap iteration: LONGFORM_VOICEOVER_LIMIT_S=150 narrates the first
# two and a half minutes. Anything falsy narrates the whole script.
LONGFORM_VOICEOVER_LIMIT_S = float(os.environ.get("LONGFORM_VOICEOVER_LIMIT_S", "0") or 0)

# Measured on the live Seed endpoint: 2200 chars -> 133s and 4500 -> 313s, i.e. 16.5 and 14.4
# characters per second. The slower figure is the safe one when deciding how much text fits in a
# given number of seconds - it errs towards a shorter clip, never a longer one.
NARRATION_CHARS_PER_SECOND = 14.4


def trim_script_to_seconds(script, seconds):
    """Cut a script down to roughly `seconds` of narration, on a sentence boundary.

    Returns the script unchanged when `seconds` is falsy, so the full-length path is the same
    code with the limit switched off.
    """
    text = str(script or "").strip()
    if not seconds or seconds <= 0 or not text:
        return text
    budget = int(float(seconds) * NARRATION_CHARS_PER_SECOND)
    if len(text) <= budget:
        return text
    kept, total = [], 0
    for sentence in re.findall(r"[^.!?]*[.!?]+(?:\s|$)|[^.!?]+$", text):
        piece = sentence.strip()
        if not piece:
            continue
        if kept and total + len(piece) > budget:
            break
        kept.append(piece)
        total += len(piece) + 1
    # Never return nothing: one sentence longer than the budget is still that sentence.
    return " ".join(kept) if kept else text.split(". ")[0].strip() + "."


# Sentence-safe chunking limit per TTS call. Measured against the live Seed endpoint rather than
# guessed: 2200 chars -> 2.2 minutes of audio, 4500 -> 5.2 minutes complete in one 34-second
# call, and 5200, 5600, 6000 and 9000 all came back as a failed prediction. The model declares no
# maxLength at all, so the ceiling is only discoverable by trying it.
#
# 4400 was tried and deliberately reverted: a part is also the unit that gets REGENERATED when a
# chunk comes back silent or truncated, and the unit a reviewer approves or declines in
# "halt after speech". Five-minute parts make both of those far more expensive than the saved
# API calls are worth. 2600 keeps a part at roughly two and a half minutes.
TTS_PART_CHAR_LIMIT = 2600

# ---- images: Ideogram (P-Image) over the WaveSpeed HTTP API ------------------------------
# Replaces the Higgsfield browser session. That route needed a visible window, a manual
# "Unlimited" switch and a DataDome bot-check that fails every automated click, and its
# page-recycling was the source of the late-delivery/mis-attribution bug this file spent
# hundreds of lines defending against. An ordinary POST+poll has none of that: every request
# owns its own result, so attribution is exact by construction.
P_IMAGE_MODEL = "pruna-ai/p-image/ideogram"
# The project format. A sketch explainer is normally a 16:9 long video, but the same pipeline
# also makes ~1 minute 9:16 shorts: same drawing style, same cut speed, just a vertical canvas
# and a much shorter script. run_longform_video() sets this once per run and everything - the
# prompt text, the generator request, the resume checks and the assembly - reads it from here,
# which is how IMAGE_ASPECT already worked for the generation thread pool.
IMAGE_ASPECT = "16:9"
LANDSCAPE_SIZE = (1920, 1080)
PORTRAIT_SIZE = (1080, 1920)

# Slow push-in on the stills, as a FRACTION of the frame. 0 = off (the default, so existing
# projects render byte-identically). 0.05 means the picture ends 5% larger than it started,
# spread evenly over that image's whole hold - deliberately below the point where it reads as
# movement rather than as the frame simply breathing.
IMAGE_ZOOM = 0.0
IMAGE_ZOOM_SUBTLE = 0.05
# Each zoomed still becomes its own decode chain in one filtergraph. Past this many scenes that
# graph stops being reasonable, so a long explainer renders flat rather than failing.
IMAGE_ZOOM_MAX_SCENES = 150

# A still held for four seconds reads as frozen, and a slow push-in on EVERY still reads as a
# slideshow with one trick. These are the other ways to make a stickman drawing feel drawn rather
# than scanned - all built from the same zoompan pass, so none of them costs an extra encode.
#
#   push       the original: every image creeps inward
#   alternate  in, out, in, out - the direction changes at each cut, so consecutive images stop
#              feeling like the same move repeated
#   paper      a push-in plus a slow off-rhythm drift, as if the sheet were held in a hand
#   jump       stop-motion: the image holds still and steps, instead of gliding
IMAGE_MOTION = "push"
IMAGE_MOTION_STYLES = ("push", "alternate", "paper", "jump")
# ~6 steps a second at 24fps: enough to read as stepped, not so coarse it reads as dropped frames.
JUMP_STEP_FRAMES = 4
# Measured in the OVERSAMPLED frame, so roughly half this on the finished canvas.
PAPER_DRIFT_PX = 18.0
# Deliberately not a round number of seconds: a drift that lines up with the cut rhythm stops
# looking like a hand and starts looking like a bug.
PAPER_DRIFT_PERIOD_F = 41.0


def set_image_motion(value):
    """Choose how the stills move. Unknown values fall back to the original push-in."""
    global IMAGE_MOTION
    name = str(value or "").strip().lower()
    IMAGE_MOTION = name if name in IMAGE_MOTION_STYLES else "push"
    return IMAGE_MOTION


def _motion_expressions(index, zoom, frames, style=None):
    """The zoompan z/x/y expressions for one scene. Kept apart from the filtergraph so the
    movement can be reasoned about - and tested - without running ffmpeg."""
    style = str(style or IMAGE_MOTION or "push").lower()
    frames = max(1, int(frames))
    zoom = max(0.0, float(zoom))
    centre_x, centre_y = "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    if style == "alternate" and index % 2 == 1:
        # Pull OUT: start at full zoom and open up. Same amount of movement, opposite direction.
        z = f"max(1+{zoom:.4f}-{zoom:.4f}*on/{frames},1)"
        return z, centre_x, centre_y
    if style == "paper":
        # Keep a floor of crop headroom for the whole shot so the drift can never run off the
        # edge of the oversampled frame and clip against it.
        floor = zoom * 0.35
        z = (f"min(1+{floor:.4f}+{zoom - floor:.4f}*on/{frames},"
             f"1+{zoom:.4f})")
        x = f"{centre_x}+{PAPER_DRIFT_PX:.1f}*sin(on/{PAPER_DRIFT_PERIOD_F:.1f})"
        # A different period on each axis, or the drift is a diagonal line instead of a wander.
        y = f"{centre_y}+{PAPER_DRIFT_PX * 0.7:.1f}*sin(on/{PAPER_DRIFT_PERIOD_F * 1.6:.1f}+1.1)"
        return z, x, y
    if style == "jump":
        steps = max(1, frames // JUMP_STEP_FRAMES)
        z = (f"min(1+{zoom:.4f}*floor(on/{JUMP_STEP_FRAMES})/{steps},"
             f"1+{zoom:.4f})")
        return z, centre_x, centre_y
    z = f"min(1+{zoom:.4f}*on/{frames},1+{zoom:.4f})"
    return z, centre_x, centre_y


def set_image_zoom(amount):
    """How far the stills push in over their hold. Returns the value actually used."""
    global IMAGE_ZOOM
    try:
        value = float(amount or 0.0)
    except (TypeError, ValueError):
        value = 0.0
    IMAGE_ZOOM = max(0.0, min(0.30, value))
    return IMAGE_ZOOM


def set_project_aspect(aspect):
    """Point the module at 16:9 or 9:16 for this run. Returns the canvas size."""
    global IMAGE_ASPECT
    IMAGE_ASPECT = "9:16" if str(aspect or "").strip() == "9:16" else "16:9"
    return video_size()


def video_size():
    return PORTRAIT_SIZE if IMAGE_ASPECT == "9:16" else LANDSCAPE_SIZE


def adopt_project_aspect(out_dir):
    """Point the module at an EXISTING project's format before touching its frames.

    Without this every editor, retime and rebuild call would test a vertical project's images
    against 16:9, decide they were all wrong, and offer to regenerate art that is perfectly
    fine. Projects written before the shorts format existed have no key and stay landscape.
    """
    try:
        state = json.loads((Path(out_dir) / STATE_FILE).read_text(encoding="utf-8"))
    except Exception:                                                   # noqa: BLE001
        state = {}
    set_image_zoom((state or {}).get("image_zoom") or 0.0)
    # A project carries its own movement, like its own zoom: reopening an older short must not
    # silently re-render it with whatever style the last run happened to use.
    set_image_motion((state or {}).get("image_motion") or "push")
    return set_project_aspect((state or {}).get("aspect") or "16:9")

SHORT_FORM_ADDENDUM = """

## THIS IS A ONE-MINUTE VERTICAL SHORT, NOT A LONG EXPLAINER

Everything above still holds, with these overrides. They exist because the rules above are written
for twenty minutes of film; applied unchanged to sixty seconds they produce slow, half-empty
pictures.

S1. A picture is on screen for about TWO seconds, not four. It must read instantly: ONE subject
doing ONE thing, large in frame. No wide establishing shots, no scene with three things happening -
at this speed the viewer sees the biggest shape and nothing else. This overrides rule 9c's
multi-moment frames and rule 9d's wide shots; it does NOT override rule 9a. Fewer ACTIONS, not no
PLACE: the subject is still in a room, on a bed, under a window - three or four named props behind
and around it, simply arranged so the main shape still wins. A short built from symbols floating
on colour is the same slide-deck look, just faster.

S2. COMPOSE FOR A TALL FRAME. The subject fills the middle of a 9:16 canvas, action in the upper
two thirds; the bottom fifth is where the platform draws its own buttons. A composition spread
left-to-right wastes most of the picture.

S3. THE BACKGROUND COLOUR HOLDS FOR THREE OR FOUR PICTURES, THEN CHANGES. Measured on a finished
short: the colour changed at 16 of 16 cuts, seventeen different backgrounds in forty-five
seconds, and the result flickers instead of moving. A colour marks a SECTION of the narration -
where the story is, what changes, how it ends - so it changes when the section does, not when the
picture does. Two identical backgrounds in a row are fine and normal; four in a row is a frozen
video, and a new colour on every single frame is noise.

S4. Rule 10's quota is per-VIDEO here: at most ONE labelled diagram in the whole short, and only if
the narration genuinely explains a mechanism. A short has no time to read.
   AND IT HAS TO BE READABLE ON A PHONE. The one diagram in a measured short carried the whole
   point of the video - three boxes reading FIRST SLEEP, MIDNIGHT WAKE, SECOND SLEEP - at a size
   nobody could decipher on a 9:16 screen, so the single frame that explained the fact explained
   nothing. A label in a short is at most TWO boxes, ONE capital word each, and that word is drawn
   large enough to span roughly a third of the frame's width. If the idea needs three boxes or two
   words a box, it is not a diagram in this format: draw it as consecutive pictures instead and
   let the narration do the labelling.

S5. Every picture carries the ONE idea its own line says. If the line is a punchline - "You can't."
- draw the reaction to it, not the setup again."""


def fit_aspect(text, aspect=None):
    """Prompt text is written for 16:9. A shorts run needs exactly the same drawing rules on a
    vertical canvas, so the ratio is swapped where it is spoken rather than the whole STAGE-3
    prompt being duplicated and left to drift out of sync with the landscape one."""
    target_aspect = "9:16" if str(aspect or IMAGE_ASPECT).strip() == "9:16" else "16:9"
    if target_aspect == "16:9":
        return text
    # The STAGE-3 rules are written for a twenty-minute film. A short keeps the same drawing
    # style but needs its own pacing and composition, appended rather than forked so the two
    # formats cannot drift apart.
    if "STAGE 3" in str(text):
        text = str(text) + SHORT_FORM_ADDENDUM
    return (str(text).replace("16:9 aspect ratio", "9:16 vertical aspect ratio")
                     .replace("16:9 composition", "9:16 vertical composition")
                     .replace("16:9 image prompt", "9:16 vertical image prompt"))

# 1k + "very low" (user 2026-08-16): the cheapest tier on this endpoint, $0.003 per image
# against $0.030 for 2k/high - a 240-frame video costs ~$0.72 instead of ~$7.20. Flat doodle
# art is the one style that survives it: no fine texture to lose, and the composition comes
# from the prompt rather than from the model thinking about it.
IMAGE_RESOLUTION = "1k"
IMAGE_THINKING = "very low"
IMAGE_OUTPUT_FORMAT = "png"
# Median generation is ~8s, so the run is now network-bound rather than session-bound.
IMAGE_CONCURRENCY = 6               # parallel generations in flight
IMAGE_TIMEOUT_S = 300               # per image: submit + poll + download
IMAGE_RETRIES = 2                   # re-generate a failed image up to N extra times
MAX_DEAD_ATTEMPTS_BEFORE_GIVING_UP = 6  # failed generations with ZERO successes = provider gone
MAX_CONSECUTIVE_FAILURES_MIDRUN = 10    # unbroken failure streak while fresh work remains = died mid-run
# Kept for anything that still imports them; the assembly asks video_size() so a shorts run
# renders 1080x1920 instead of letterboxing a vertical drawing into a landscape frame.
VIDEO_W, VIDEO_H, VIDEO_FPS = 1920, 1080, 30


STATE_FILE = "state.json"           # resume state: script + timings + prompts of the last run
MIN_IMAGE_BYTES = 1024              # smaller than this = a truncated/failed write, regenerate
MIN_AUDIO_BYTES = 8192              # 24kHz/16bit mono: <0.2s of audio, so a truncated part


class LongformError(RuntimeError):
    pass


_IMAGE_DONE_CACHE = {}


def _image_done(path, expected_aspect=None):
    """True when an image is healthy and, when requested, has the right canvas ratio.

    Higgsfield's generator defaults to 3:4.  A UI-selector regression once produced a valid but
    portrait frame for a 16:9 longform project; checking only byte size made every resume trust
    that wrong frame forever.

    The aspect check opens the file, and the project list calls this for every frame of every
    longform project - measured at 4218 opens and 1.5 of the 4.9 seconds a single list took. A
    written frame never changes, so the answer is cached against the file's size and mtime: a
    rewritten or replaced frame gets a new key and is checked again.
    """
    try:
        p = Path(path)
        if not p.is_file() or p.stat().st_size < MIN_IMAGE_BYTES:
            return False
        if expected_aspect:
            _st = p.stat()
            _key = (str(p), _st.st_size, _st.st_mtime_ns, str(expected_aspect))
            if _key in _IMAGE_DONE_CACHE:
                return _IMAGE_DONE_CACHE[_key]
            _result = _image_aspect_ok(p, expected_aspect) and not _image_is_blank(p)
            if len(_IMAGE_DONE_CACHE) > 20000:
                _IMAGE_DONE_CACHE.clear()
            _IMAGE_DONE_CACHE[_key] = _result
            return _result
        return True
    except (OSError, ValueError, ZeroDivisionError):
        return False


# A provider occasionally returns a valid PNG that is simply black. Byte size and canvas ratio
# both pass it, so every resume trusted it and the finished render was refused at the very end
# for containing a black screen - with nothing to regenerate the frame. Judging it here makes the
# generator treat it as missing and draw it again.
BLANK_IMAGE_LUMA = 16.0


def _image_is_blank(p):
    """True when the frame is the black PNG a provider sometimes returns instead of a picture.

    The threshold is measured, not guessed: across a whole short the darkest genuine frame sat at
    73 and the dead one at 11, so anything under 16 is dead. This deliberately judges DARKNESS
    only. A second clause once also rejected any frame whose pixels were all within a few levels
    of each other, on the theory that a flat field carries no picture - it rejected five healthy
    images the first time it met real data, and flat-colour heuristics have misjudged drawings
    here before. A frame this pipeline would actually produce is never flat, so the clause bought
    nothing and cost correctness.
    """
    try:
        from PIL import Image
        with Image.open(p) as image:
            small = image.convert("L").resize((48, 48))
        pixels = list(small.getdata())
        if not pixels:
            return True
        return sum(pixels) / float(len(pixels)) < BLANK_IMAGE_LUMA
    except Exception:                                                   # noqa: BLE001
        return False


def _image_aspect_ok(p, expected_aspect):
    """Does this image's canvas match the requested ratio? Reads the header only."""
    try:
        from PIL import Image
        with Image.open(p) as image:
            width, height = image.size
        left, right = str(expected_aspect).split(":", 1)
        target = float(left) / float(right)
        actual = float(width) / float(height)
        # Allow normal rounding/cropping differences while rejecting 3:4 as 16:9.
        return abs(actual - target) <= 0.06
    except (OSError, ValueError, ZeroDivisionError):
        return False


def _audio_done(path):
    """True when a TTS part is already on disk and is not a truncated stub."""
    try:
        p = Path(path)
        return p.is_file() and p.stat().st_size >= MIN_AUDIO_BYTES
    except OSError:
        return False


def load_state(out_dir, script):
    """Resume state for THIS script, or None.

    Keyed on the exact script: if a single word changed, the line split, the timings and the
    prompts all change, so nothing from the old run may be reused (the images would land on the
    wrong lines). Returning None simply means "run every stage again".
    """
    try:
        data = json.loads((Path(out_dir) / STATE_FILE).read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict) or str(data.get("script") or "") != str(script or ""):
        return None
    return data


def save_state(out_dir, **fields):
    """Persist the resume state atomically (never leave a half-written state behind)."""
    path = Path(out_dir) / STATE_FILE
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(existing, dict):
            existing = {}
    except Exception:
        existing = {}
    existing.update(fields)
    try:
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(existing, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path)
    except Exception:
        pass
    return existing


def _log(cb, msg):
    if cb:
        cb(msg)


def slug_for(script):
    words = re.findall(r"[A-Za-z0-9]+", str(script or ""))[:6]
    slug = "_".join(w.lower() for w in words) or f"longform_{int(time.time())}"
    return slug[:60]


# ------------------------------------------------------------------ 1) VOICEOVER

def clean_narration_script(script):
    """Remove source-system artifacts that must never be spoken by TTS.

    Web research can leave private-use citation wrappers such as
    ``\ue200cite\ue202turn123search4\ue201`` in otherwise finished prose. They are useful to a
    chat renderer, but a speech model reads the payload aloud as "turn one two three...".
    Keep the authored wording and paragraph structure while deleting only those wrappers and
    any exposed turn/search identifiers left behind by a malformed wrapper.
    """
    text = re.sub(r"\r\n?", "\n", str(script or ""))
    text = re.sub(r"\ue200[^\ue201]*\ue201", "", text)
    text = re.sub(r"\bturn\d+(?:(?:search|fetch|view|open|news)\d+)+\b", "", text,
                  flags=re.IGNORECASE)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" +([,.;:!?])", r"\1", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return _strip_production_directives(text).strip()


# A stage direction the narrator reads aloud is the worst possible artifact: it is fluent, so
# nothing downstream flags it, and the finished Short simply says "Now film the payoff" in a
# confident voice. Measured on a Luna scrape-mode script for "why nobody talks on Tokyo trains":
# the script prompt uses the word "film" fourteen times to explain what footage EXISTS, and the
# model echoed that vocabulary straight into the spoken line.
#
# Openers only. A sentence may legitimately contain "watch" ("commuters watch the doors close");
# what is never narration is a clause addressing the production that then hands off to the real
# sentence with a colon.
_DIRECTIVE_OPENER = re.compile(
    r"^(?:and\s+|so\s+|then\s+)?(?:now|next|here|first|finally)?\s*"
    r"(?:we\s+)?(?:film|shoot|cut\s+to|open\s+on|close\s+on|smash\s+cut|b-?roll)\b"
    r"[^:.!?]*:\s*",
    re.IGNORECASE)
_DIRECTIVE_WHOLE = re.compile(
    r"^(?:cut\s+to|open\s+on|close\s+on|b-?roll|insert\s+shot|montage|title\s+card)\b"
    r"[^.!?]*[.!?]?\s*$",
    re.IGNORECASE)


def _strip_production_directives(text):
    """Delete camera/editing instructions that leaked into narration meant to be SPOKEN."""
    out = []
    for para in re.split(r"(\n\s*\n)", str(text or "")):
        if not para.strip():
            out.append(para)
            continue
        kept = []
        for piece in re.split(r"(?<=[.!?])\s+", para):
            piece = piece.strip()
            if not piece:
                continue
            # Strip the opener FIRST: "Cut to: the doors slide open." is a direction wrapped
            # around a real sentence, and both patterns match it. Deleting the whole thing
            # would silently shorten the Short, so whatever follows the colon wins.
            cleaned = _DIRECTIVE_OPENER.sub("", piece, count=1).strip()
            if cleaned and cleaned != piece:
                kept.append(cleaned[0].upper() + cleaned[1:])
                continue
            if _DIRECTIVE_WHOLE.match(piece):
                continue                      # nothing but a direction - no sentence to save
            kept.append(piece)
        out.append(" ".join(kept))
    return "".join(out)


def tts_chunk_limit(tts_model=None):
    """Characters per TTS part for THIS model.

    One global number cannot serve three providers: Inworld rejects anything over 2000
    characters outright, while Seed was measured at 4500 working and 5200 failing. Asking the
    provider keeps a limit that is right for one from silently breaking another - the 2600 that
    suits Seed is a hard overflow on Inworld.
    """
    declared = pipeline.tts_text_limit(tts_model) if tts_model else 0
    if not declared:
        return TTS_PART_CHAR_LIMIT
    # Leave room for the square-bracket stage direction Inworld prepends to every part.
    return min(TTS_PART_CHAR_LIMIT, declared - 220)


def split_script_for_tts(script, limit=TTS_PART_CHAR_LIMIT):
    """Split the script into TTS-sized parts WITHOUT breaking sentences. Paragraphs first,
    then sentences when a paragraph alone exceeds the limit."""
    text = re.sub(r"\r\n?", "\n", str(script or "")).strip()
    if not text:
        return []
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    units = []
    for p in paragraphs:
        if len(p) <= limit:
            units.append(p)
        else:
            units.extend(s.strip() for s in re.findall(r"[^.!?]+[.!?]+(?:\s|$)|[^.!?]+$", p) if s.strip())
    parts, cur = [], ""
    for u in units:
        if cur and len(cur) + len(u) + 1 > limit:
            parts.append(cur.strip())
            cur = u
        else:
            cur = (cur + " " + u).strip() if cur else u
    if cur.strip():
        parts.append(cur.strip())
    return parts


def split_sketch_short_for_tts(script):
    """Keep a Sketch Short's opening as its own TTS take.

    A whole 45-second Short in one Seed/Gemini request can only receive one delivery style, so a
    hook instruction simply gets averaged away.  The authored first line is normally the hook;
    if the script arrives as one paragraph, use its first one or two short sentences instead.
    The remaining narration still uses the usual sentence-safe splitter.
    """
    text = re.sub(r"\r\n?", "\n", str(script or "")).strip()
    if not text:
        return []
    nonempty_lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(nonempty_lines) >= 2 and len(nonempty_lines[0]) <= 320:
        hook = nonempty_lines[0]
        body = "\n\n".join(nonempty_lines[1:]).strip()
    else:
        sentences = [sentence.strip() for sentence in re.findall(
            r"[^.!?…]+(?:[.!?…]+(?:[\"')\]]*)|$)", text) if sentence.strip()]
        if not sentences:
            return split_script_for_tts(text)
        hook_parts = [sentences[0]]
        if len(sentences) > 1 and len(sentences[0]) + len(sentences[1]) + 1 <= 260:
            hook_parts.append(sentences[1])
        hook = " ".join(hook_parts).strip()
        body = text[len(hook):].strip()
    body_parts = split_script_for_tts(body) if body else []
    return [hook] + body_parts if hook else body_parts


def longest_silence_run(path, ffmpeg=None, floor_db=-45.0):
    """Length (s) of the single longest unbroken silent stretch in ``path`` (0.0 if none)."""
    ffmpeg = ffmpeg or pipeline.find_ffmpeg()
    try:
        r = subprocess.run([ffmpeg, "-hide_banner", "-i", str(path), "-af",
                            f"silencedetect=noise={floor_db}dB:d=2", "-f", "null", "-"],
                           capture_output=True, text=True, timeout=180)
        runs = [float(x) for x in re.findall(r"silence_duration:\s*(\d+(?:\.\d+)?)", r.stderr or "")]
        return max(runs) if runs else 0.0
    except Exception:
        return 0.0


def audio_is_silent(path, ffmpeg=None, floor_db=-45.0, max_silence_run=8.0):
    """True when a TTS part FAILED - either near-silent throughout, OR it speaks for a bit and
    then holds a long unbroken silence.

    Gemini TTS has two failure modes that a byte-size check passes straight into the voiceover as
    a dead hole:
      1. a whole chunk comes back near-digital-silence, and
      2. the read is TRUNCATED - the first third is spoken and the rest is padded with minutes of
         silence (the tickle part-5 bug: 56s of speech + 607s of silence). A whole-part MEAN check
         is fooled by the spoken head (that part averaged -36 dB and sailed past a -45 dB floor),
         so mean volume alone cannot see it.
    We therefore fail a part if its mean is below the floor OR it contains a single silent run
    longer than any legitimate narration pause (sentence gaps are well under 2s)."""
    ffmpeg = ffmpeg or pipeline.find_ffmpeg()
    try:
        r = subprocess.run([ffmpeg, "-hide_banner", "-i", str(path), "-af", "volumedetect",
                            "-f", "null", "-"], capture_output=True, text=True, timeout=120)
        m = re.search(r"mean_volume:\s*(-?\d+(?:\.\d+)?)\s*dB", r.stderr or "")
        mean = float(m.group(1)) if m else -99.0
    except Exception:
        return False                         # can't measure -> don't wrongly reject
    if mean <= floor_db:
        return True
    return longest_silence_run(path, ffmpeg, floor_db) >= max_silence_run


VOICEOVER_TARGET_LUFS = -20.0       # measured centre of what Seed and Gemini already return


def match_part_loudness(part_paths, ffmpeg, status_cb=None, target=VOICEOVER_TARGET_LUFS):
    """Bring every voiceover part to the same integrated loudness, in place.

    Each part is its own TTS call and comes back at its own level - measured on two finished
    projects, parts of the SAME video differed by 2.2 dB and 1.1 dB. That is an audible step at
    the seam, and it is also why the review screen sounds inconsistent: you are judging takes
    recorded at different volumes.

    Deliberately gain-only, via `volume`, not a compressor and not a full two-pass loudnorm.
    Lifting a TTS part with dynamics processing raises its noise floor with it - that hiss was
    tracked down once already and cost a day - so this measures each part and applies one flat
    gain. A part already within half a decibel is left untouched rather than re-encoded.
    """
    parts = [Path(p) for p in part_paths if Path(p).is_file()]
    if len(parts) < 2:
        return list(part_paths)
    levels = {}
    for path in parts:
        loudness = measure_integrated_loudness(path, ffmpeg)
        if loudness is not None:
            levels[path] = loudness
    if len(levels) < 2:
        return list(part_paths)
    spread = max(levels.values()) - min(levels.values())
    if spread < 0.5:
        _log(status_cb, f"Voiceover parts are already within {spread:.1f} dB - no change.")
        return list(part_paths)
    _log(status_cb, f"Levelling {len(levels)} voiceover part(s): {spread:.1f} dB apart, "
                    f"matching all to {target:.1f} LUFS.")
    out = []
    for path in part_paths:
        path = Path(path)
        gain = target - levels[path] if path in levels else 0.0
        if abs(gain) < 0.5:
            out.append(str(path))
            continue
        levelled = path.with_name(path.stem + "_lvl" + path.suffix)
        done = subprocess.run(
            [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(path),
             "-filter:a", f"volume={gain:+.2f}dB", "-c:a",
             "libmp3lame" if path.suffix.lower() == ".mp3" else "pcm_s16le",
             *(["-b:a", "192k"] if path.suffix.lower() == ".mp3" else []),
             str(levelled)], capture_output=True, text=True, timeout=600)
        if levelled.is_file() and levelled.stat().st_size > 4096:
            path.unlink(missing_ok=True)
            levelled.replace(path)
            out.append(str(path))
        else:
            # A failed level is not worth losing the take over.
            _log(status_cb, f"Could not level {path.name}: {(done.stderr or '')[-120:]}")
            levelled.unlink(missing_ok=True)
            out.append(str(path))
    return out


def measure_integrated_loudness(path, ffmpeg):
    """Integrated LUFS for one file, or None when it cannot be measured."""
    try:
        done = subprocess.run(
            [ffmpeg, "-hide_banner", "-nostats", "-i", str(path),
             "-filter:a", "ebur128=framelog=quiet", "-f", "null", "-"],
            capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.findall(r"I:\s*(-?\d+(?:\.\d+)?)\s*LUFS", done.stderr or "")
    return float(match[-1]) if match else None


def concat_audio_parts(part_paths, out_path, ffmpeg):
    """Concatenate TTS parts RAW (concat demuxer, no resampling - resampling injects a noise
    floor; see pipeline.concat_audio_with_pause). Single part -> plain copy."""
    part_paths = [Path(p) for p in part_paths]
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if len(part_paths) == 1:
        # keep container/codec: transcode once to wav for a stable downstream format
        r = subprocess.run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                            "-i", str(part_paths[0]), "-ac", "2", "-ar", "44100",
                            str(out_path)], capture_output=True, text=True, timeout=300)
        if not out_path.exists():
            raise LongformError(f"Voiceover convert failed: {(r.stderr or '')[-300:]}")
        return out_path
    # normalize every part to the same PCM format first, then concat losslessly
    tmp_parts = []
    for i, p in enumerate(part_paths):
        t = out_path.with_name(f"_part{i:02d}.wav")
        r = subprocess.run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(p),
                            "-ac", "2", "-ar", "44100", "-c:a", "pcm_s16le", str(t)],
                           capture_output=True, text=True, timeout=300)
        if not t.exists():
            raise LongformError(f"Voiceover part convert failed: {(r.stderr or '')[-300:]}")
        tmp_parts.append(t)
    lst = out_path.with_name("_parts.txt")
    lst.write_text("".join(f"file '{p.name}'\n" for p in tmp_parts), encoding="utf-8")
    # Absolute paths, because cwd is set below. Handed a RELATIVE out_path - which every caller
    # outside this module does - the list file's own relative path was then resolved a second
    # time against the new working directory and ffmpeg reported the list as missing while it
    # sat right there. It cost a full set of paid TTS parts to find.
    # The parts above are PCM. Copying that stream works into a .wav and CANNOT work into an
    # .mp3 - ffmpeg wrote a zero-byte file and exited, and the "did it work" check below passed
    # it because the file existed. Pick the codec from the container the caller asked for.
    codec = (["-c", "copy"] if out_path.suffix.lower() == ".wav"
             else ["-c:a", "libmp3lame", "-q:a", "2"])
    r = subprocess.run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-f", "concat",
                        "-safe", "0", "-i", str(lst.resolve()), *codec,
                        str(out_path.resolve())],
                       capture_output=True, text=True, timeout=900,
                       cwd=str(out_path.resolve().parent))
    for t in tmp_parts:
        t.unlink(missing_ok=True)
    lst.unlink(missing_ok=True)
    # Existence is not success: a failed copy leaves an empty container behind.
    if not out_path.exists() or out_path.stat().st_size < 1024:
        raise LongformError(f"Voiceover concat failed: {(r.stderr or '')[-300:]}")
    return out_path


def apply_voice_speed(path, speed, ffmpeg=None, status_cb=None):
    """Return a pitch-preserving re-tempoed copy of ``path``.

    Never replace ``path`` itself.  The approval player can still be streaming that file when
    the user clicks Continue and Windows then rejects ``os.replace`` with ``WinError 5``.  A
    versioned output also keeps the untouched TTS take as the source of truth, so selecting a
    different speed later cannot accidentally apply atempo twice.
    """
    try:
        speed = float(speed or 0)
    except (TypeError, ValueError):
        return Path(path)
    path = Path(path)
    if not speed:
        return path
    speed = max(0.5, min(2.0, speed))     # the picker offers 0.90-1.30; never trust it blindly
    if abs(speed - 1.0) < 0.01:
        return path
    ffmpeg = ffmpeg or pipeline.find_ffmpeg()
    if not ffmpeg:
        return path
    try:
        stat = path.stat()
        source_version = f"{stat.st_size:x}_{stat.st_mtime_ns:x}"
    except OSError:
        source_version = str(time.time_ns())
    speed_tag = f"{speed:.2f}".replace(".", "p")
    # The timestamp makes every conversion target unique.  ffmpeg therefore never has to open
    # an earlier preview/output for replacement either (that file may also still be streamed).
    out_path = path.with_name(
        f"{path.stem}_speed_{speed_tag}x_{source_version}_{time.time_ns():x}{path.suffix}")
    # atempo only accepts 0.5..2.0, so anything outside has to be chained - pipeline already
    # knows how to build that chain, and the clip pipeline uses the same one
    out = subprocess.run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(path),
                          "-af", pipeline.atempo_filter_chain(speed), str(out_path)],
                         capture_output=True, text=True, timeout=600)
    if not out_path.exists() or out_path.stat().st_size < MIN_AUDIO_BYTES:
        out_path.unlink(missing_ok=True)
        raise LongformError((out.stderr or "voice speed conversion failed")[-180:])
    _log(status_cb, f"Narration speed set to {speed:.2f}x.")
    return out_path


def voiceover_path_from_state(out_dir, state=None):
    """Resolve the immutable voiceover selected by the resume state, safely inside out_dir."""
    out_dir = Path(out_dir)
    name = Path(str((state or {}).get("voiceover_file") or "voiceover.wav")).name
    selected = out_dir / name
    if _audio_done(selected):
        return selected
    return out_dir / "voiceover.wav"


def generate_voiceover(script, out_dir, tts_model="pro", status_cb=None, cancel_event=None,
                       speech_gate=None, voice=None, speaker=None, resume=True, mix_gate=None,
                       tts_options=None, short_form=False, hook_in_intro=False):
    """Script -> voiceover.wav (parts stitched). Returns (path, parts_count).

    `voice` / `speaker` pick the Gemini TTS narrator (None = pipeline defaults), so longform uses
    the same narrator selection as the other modes instead of always the built-in default voice.

    `speech_gate(parts_info, regen_part)` (optional, "Halt after speech"): called AFTER all
    TTS parts exist and BEFORE they are stitched. `parts_info` is a list of
    {"index", "text", "path"}; `regen_part(i)` re-generates part i with a fresh TTS take and
    returns the new path. The gate blocks until the user has approved every part (declines
    trigger regen through the callback); it raises to cancel the run.

    RESUME: the parts are only stitched (and deleted) once the gate has approved them all, so a
    run that is cancelled or lost in the approval gate leaves every `vo_part*.wav` behind while
    `voiceover.wav` never appears - the outer resume check misses it and used to pay for the whole
    TTS again. The part paths are therefore recorded in state.json as they are produced (takes
    included) and reused here, which drops you straight back into the approval screen.
    """
    # A Sketch Short normally gets its opening line as its own take, because a hook read cannot
    # be applied to a whole 45-second request. But when the Google intro is speaking the hook,
    # that line is no longer IN this script - so the splitter would hand the hook performance,
    # and its own separate take, to whatever sentence now happens to come first. Reported: the
    # voiceover's "hook part" was the script's SECOND line and the first was nowhere in it.
    sketch_hook_take = short_form and not hook_in_intro
    parts = (split_sketch_short_for_tts(script) if sketch_hook_take
             else split_script_for_tts(script, limit=tts_chunk_limit(tts_model)))
    if not parts:
        raise LongformError("The script is empty.")
    ffmpeg = pipeline.find_ffmpeg()
    if not ffmpeg:
        raise LongformError("ffmpeg not found.")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    # only pass a narrator when one was chosen, so an empty pick keeps pipeline's own defaults
    # A 9:16 Sketch Explainer Short must not inherit the relaxed longform delivery.  Its first
    # TTS part is rendered with an even stronger hook directive below; later parts remain lively
    # but comprehensible.  Keeping the profile in the saved settings means an old calm take is
    # never silently reused after this behaviour changed.
    delivery_profile = "sketch_short_hook_v2" if short_form else "longform_documentary_v1"
    tts_kw = {"style": (pipeline.TTS_STYLE_SKETCH_SHORT if short_form
                        else pipeline.TTS_STYLE_LONGFORM)}
    tts_settings = dict(tts_options or {}) if tts_model in pipeline.SEED_SPEECH_TTS_ALIASES else {}
    tts_settings["delivery_profile"] = delivery_profile
    if voice:
        tts_kw["voice"] = voice
    if speaker:
        tts_kw["speaker"] = speaker
    if tts_model in pipeline.SEED_SPEECH_TTS_ALIASES:
        opts = dict(tts_options or {})
        tts_kw.update({
            "voice_instruction": str(opts.get("voice_instruction") or "").strip() or None,
            "auto_upbeat": False,
            "language": str(opts.get("language") or "").strip(),
            "tts_speed": opts.get("speed", 1.0),
            "volume": opts.get("volume", 1.0),
            "pitch": opts.get("pitch", 0),
            "sample_rate": opts.get("sample_rate", 24000),
            "output_format": opts.get("output_format", "mp3"),
        })

    def _part_tts_kwargs(index):
        """Give only the opening Sketch Short take the scroll-stopping hook performance."""
        call_kw = dict(tts_kw)
        if not sketch_hook_take:
            # Seed takes its direction through voice_instruction, never through the Gemini
            # `style` field - so a longform run that set nothing here fell back to
            # SEED_DEFAULT_VOICE_INSTRUCTION, which is written for a 30-second Short. Twenty
            # minutes of "strong emphasis on the hook and key words" is the wrong register and
            # is what made the narration sound off.
            provider = pipeline.tts_provider(tts_model)
            supplied = str((tts_options or {}).get("voice_instruction") or "").strip()
            if supplied or provider == "gemini":
                return call_kw                      # typed by hand, or Gemini's own style field
            if provider == "seed":
                call_kw["voice_instruction"] = (
                    f"{pipeline.SEED_LONGFORM_OPENING_INSTRUCTION} "
                    f"{pipeline.SEED_LONGFORM_VOICE_INSTRUCTION}" if index == 0
                    else pipeline.SEED_LONGFORM_VOICE_INSTRUCTION)
            elif provider == "inworld":
                # Inworld reads its note as a bracket line; the wording differs from Seed's
                # because it is a stage direction, not a description of a narrator.
                call_kw["voice_instruction"] = pipeline.INWORLD_LONGFORM_DIRECTION
            return call_kw
        if pipeline.tts_provider(tts_model) == "inworld":
            supplied = str((tts_options or {}).get("voice_instruction") or "").strip()
            call_kw["voice_instruction"] = supplied or (
                pipeline.INWORLD_SHORT_HOOK_DIRECTION if index == 0
                else pipeline.INWORLD_SHORT_DIRECTION)
        elif tts_model in pipeline.SEED_SPEECH_TTS_ALIASES:
            supplied = str((tts_options or {}).get("voice_instruction") or "").strip()
            if index == 0:
                hook_direction = (
                    "This is the hook only. Start immediately with strong confident energy, "
                    "attack the first words, sharply emphasize the surprising phrase, then land "
                    "the final word with a deliberate punch. It must feel like a viral educational "
                    "Short opening. No slow warm-up, no flat documentary tone, never shout.")
                call_kw["voice_instruction"] = (
                    f"{supplied} {hook_direction}".strip() if supplied else hook_direction)
            else:
                call_kw["voice_instruction"] = supplied or (
                    "Upbeat, friendly and clear short-form narration with punchy emphasis; "
                    "keep the energy natural and never shouty.")
        elif index == 0:
            call_kw["style"] = pipeline.TTS_STYLE_SKETCH_SHORT_HOOK
        return call_kw

    # Reusable parts must belong to THIS script, narrator and delivery directive: the state is keyed
    # on the script (load_state) and the split it was recorded under must still produce the same part
    # count, else part 3 of the old split would be spoken over part 3 of the new one. The saved list
    # is an index-aligned PREFIX - a run that died on part 3 of 8 saved 2 paths, and those 2 are
    # still worth reusing - so it is the recorded total that is compared, not the list length.
    # The style matters as much as the voice: parts recorded under the old viral-narrator directive
    # are the wrong PERFORMANCE, and reusing them would silently undo an edit to TTS_STYLE_LONGFORM.
    saved = load_state(out_dir, script) if resume else None

    # A stitched voiceover for this exact script/narrator/directive IS the finished product of this
    # function. The parts are deleted at stitch time, so without this shortcut an interruption
    # anywhere after it - the mix gate below blocks on a human, the transcription takes minutes -
    # would find no parts and re-buy the entire TTS.
    raw_voice_path = out_dir / "voiceover.wav"
    saved_voice_path = voiceover_path_from_state(out_dir, saved)
    if (resume and saved and saved.get("voiceover_ready") and _audio_done(raw_voice_path)
            and str(saved.get("voice") or "") == str(voice or "")
            and str(saved.get("tts_model") or tts_model) == str(tts_model)
            and dict(saved.get("tts_settings") or {}) == tts_settings
            and str(saved.get("tts_style") or "") == str(tts_kw["style"] or "")
            # Reported: toggling the Google search intro changed nothing, because the run
            # resumed a stitched voiceover from the other setting and the hook take was
            # never generated. Only a state that predates this field may skip the check.
            and (saved.get("sketch_hook_take") is None
                 or bool(saved.get("sketch_hook_take")) == bool(sketch_hook_take))
            # Every project on disk predates that field, so for those the recorded PART
            # COUNT stands in for it: the two splits of the same script do not produce the
            # same number of parts, and a mismatch means the other setting recorded this.
            and (saved.get("tts_part_total") is None
                 or int(saved.get("tts_part_total") or 0) == len(parts))):
        _log(status_cb, f"Resume: the voiceover is already stitched ({raw_voice_path.name}) - keeping it.")
        # The mix gate (hear the whole take, set the speed) runs AFTER stitching, so a run killed
        # at that screen resumes right here - with the choice never made. Re-offer it; once a
        # speed is recorded ("keep 1.0x" included) the question is settled and stays settled.
        if mix_gate is not None and saved.get("voice_speed") is None:
            speed = mix_gate(str(raw_voice_path))
            saved_voice_path = apply_voice_speed(raw_voice_path, speed, ffmpeg,
                                                 status_cb=status_cb)
            save_state(out_dir, voice_speed=float(speed or 1.0),
                       voiceover_file=saved_voice_path.name)
        elif (saved.get("voice_speed") not in (None, 1, 1.0)
              and (Path(str(saved.get("voiceover_file") or "voiceover.wav")).name == "voiceover.wav"
                   or not _audio_done(out_dir / Path(str(saved.get("voiceover_file"))).name))):
            # The selected derivative was removed, but the paid raw TTS is intact. Rebuild it
            # locally without another approval/TTS round.
            saved_voice_path = apply_voice_speed(raw_voice_path, saved.get("voice_speed"), ffmpeg,
                                                 status_cb=status_cb)
            save_state(out_dir, voiceover_file=saved_voice_path.name)
        return saved_voice_path, int(saved.get("tts_parts") or len(parts))

    saved_files = (saved or {}).get("tts_part_files") or []
    if (int((saved or {}).get("tts_part_total") or 0) != len(parts)
            or str((saved or {}).get("voice") or "") != str(voice or "")
            or str((saved or {}).get("tts_model") or tts_model) != str(tts_model)
            or dict((saved or {}).get("tts_settings") or {}) != tts_settings
            or str((saved or {}).get("tts_style") or "") != str(tts_kw["style"] or "")):
        saved_files = []

    part_files = []

    def _remember():
        """Record the parts after every TTS call, so a run killed halfway keeps what it paid for.

        The timings, prompts and duration in the state describe the PREVIOUS voiceover, so they
        are dropped in the same write: this is the point where the new script enters the state,
        and a resume that found the new script next to the old lines would happily pair the new
        audio with the old script's timings.
        """
        save_state(out_dir, script=script, voice=voice or "", tts_model=tts_model,
                   tts_style=tts_kw["style"] or "", tts_settings=tts_settings,
                   sketch_hook_take=bool(sketch_hook_take),
                   tts_part_files=[str(p) for p in part_files], tts_part_total=len(parts),
                   # parts are being (re)made, so any stitched voiceover on disk is the old one.
                   # voice_speed None means "the speed question was never answered" - a NUMBER
                   # (1.0 included) means the user chose, and only then may resume skip the gate.
                   voiceover_ready=False, voice_speed=None, voiceover_file="voiceover.wav",
                   lines=None, prompts=None, audio_duration=0.0)

    for i, part in enumerate(parts):
        if cancel_event is not None and cancel_event.is_set():
            raise pipeline.PipelineCancelled("Cancelled.")
        existing = saved_files[i] if i < len(saved_files) else ""
        if existing and _audio_done(existing) and not audio_is_silent(existing, ffmpeg):
            _log(status_cb, f"Resume: reusing voiceover part {i + 1}/{len(parts)} "
                            f"({Path(existing).name}) - no new TTS.")
            part_files.append(Path(existing))
            continue
        _tts_label = ("ByteDance Seed Speech TTS 2.0" if tts_model in pipeline.SEED_SPEECH_TTS_ALIASES
                      else f"Gemini 2.5 {'Pro' if tts_model == 'pro' else 'Flash'} TTS")
        _log(status_cb, f"Voiceover part {i + 1}/{len(parts)} ({len(part)} chars) "
                        f"with {_tts_label}"
                        f"{(' - narrator ' + str(voice)) if voice else ''}...")
        p = None
        for attempt in range(3):             # a silent part = failed TTS; retry before accepting
            p = pipeline.generate_speech_gemini(part, out_dir / f"vo_part{i:02d}.wav",
                                                model=tts_model, cancel_event=cancel_event,
                                                status_cb=status_cb, **_part_tts_kwargs(i))
            if not (p and _audio_done(p)):
                _log(status_cb, f"Voiceover part {i + 1} produced no audio - retry {attempt + 1}/3.")
                continue
            if audio_is_silent(p, ffmpeg):
                _log(status_cb, f"Voiceover part {i + 1} came back SILENT - retry {attempt + 1}/3.")
                continue
            break
        if not (p and _audio_done(p)):
            raise LongformError(f"Voiceover part {i + 1} failed to generate audible speech.")
        if audio_is_silent(p, ffmpeg):
            raise LongformError(
                f"Voiceover part {i + 1} keeps coming back silent from the TTS - stopping instead "
                "of stitching a silent gap into the voiceover. Try again or switch narrator/model.")
        part_files.append(p)
        _remember()
    # the per-part writes only ever hold the prefix generated SO FAR; this one records the reused
    # tail as well, so a kill in the gate below does not re-buy parts that are sitting on disk
    _remember()

    # Level the takes BEFORE they are reviewed: judging a performance against a part that is two
    # decibels louder is judging the volume, not the read.
    match_part_loudness(part_files, ffmpeg, status_cb=status_cb)

    if speech_gate is not None:
        parts_info = [{"index": i, "text": parts[i], "path": str(part_files[i])}
                      for i in range(len(parts))]
        take_counter = {}

        def regen_part(i):
            if cancel_event is not None and cancel_event.is_set():
                raise pipeline.PipelineCancelled("Cancelled.")
            take = take_counter.get(i, 0) + 1
            take_counter[i] = take
            _log(status_cb, f"Re-generating voiceover part {i + 1}/{len(parts)} (take {take + 1})...")
            # a NEW filename per take: browsers cache the old audio URL otherwise
            new_path = pipeline.generate_speech_gemini(
                parts[i], out_dir / f"vo_part{i:02d}_take{take}.wav",
                model=tts_model, cancel_event=cancel_event, status_cb=status_cb,
                **_part_tts_kwargs(i))
            old = part_files[i]
            part_files[i] = Path(new_path)
            try:
                if str(old) != str(new_path):
                    Path(old).unlink(missing_ok=True)
            except Exception:
                pass
            _remember()     # the take replaces the part: resume must not point at the deleted one
            return str(new_path)

        _log(status_cb, f"Halt after speech: waiting for your approval of {len(parts)} "
                        "voiceover part(s)...")
        speech_gate(parts_info, regen_part)
        _log(status_cb, "All voiceover parts approved - stitching and continuing.")

    out = concat_audio_parts(part_files, out_dir / "voiceover.wav", ffmpeg)
    # Last line of defence: even with the per-part guards, a silent hole in the STITCHED voiceover
    # is the one defect the user must never ship unknowingly. Scan the whole take and refuse to
    # continue on a minutes-long dead stretch - regenerating a specific part here is impossible
    # (they are about to be deleted), so this hard-fails with a clear message instead.
    hole = longest_silence_run(out, ffmpeg)
    if hole >= 15.0:
        raise LongformError(
            f"The stitched voiceover contains a {hole:.0f}s silent gap - a TTS part came back "
            "truncated/silent. Not continuing with a dead hole in the narration; please start the "
            "voiceover again (a fresh run re-generates the failed part).")
    # Keep the approved source parts.  They are the only lossless way to review or regenerate one
    # paragraph of an existing Sketch Explainer without buying/rebuilding the other paragraphs.
    # Older builds deleted them here, which made per-part editing impossible after the first run.
    save_state(out_dir, tts_part_files=[str(Path(p)) for p in part_files], tts_model=tts_model,
               tts_settings=tts_settings, tts_part_texts=list(parts), voiceover_ready=True,
               # Which SPLIT produced these parts. Turning the Google intro on moves the hook
               # out of the narration and removes its dedicated take; a voiceover recorded
               # under the other setting is the wrong audio, not a reusable one.
               sketch_hook_take=bool(sketch_hook_take),
               voice_speed=None, voiceover_file=out.name)
    _log(status_cb, f"Voiceover ready: {out.name} ({len(parts)} part(s) stitched).")

    # `mix_gate(path) -> speed`: the stitched voiceover, played whole, before anything is timed
    # against it. Returns the narration speed to bake in (None/1.0 = leave it). It blocks on the
    # user, which is exactly why the resume shortcut above exists.
    if mix_gate is not None:
        speed = mix_gate(str(out))
        out = apply_voice_speed(out, speed, ffmpeg, status_cb=status_cb)
        save_state(out_dir, voice_speed=float(speed or 1.0), voiceover_file=out.name)
    return out, len(parts)


def audio_duration_seconds(path, ffprobe=None):
    """Return an audio duration without trusting a browser/file-size approximation."""
    path = Path(path)
    try:
        with __import__("wave").open(str(path), "rb") as wav:
            return wav.getnframes() / float(wav.getframerate() or 1)
    except Exception:
        pass
    ffprobe = ffprobe or pipeline.find_ffprobe(pipeline.find_ffmpeg())
    try:
        done = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nokey=1:noprint_wrappers=1", str(path)],
            capture_output=True, text=True, timeout=30, check=False)
        return float((done.stdout or "0").strip() or 0.0)
    except Exception:
        return 0.0


def _speech_review_manifest_path(out_dir):
    return Path(out_dir) / "speech_parts" / "manifest.json"


def save_speech_review_manifest(out_dir, script, parts, **extra):
    """Persist the exact paragraph-to-audio mapping used by the approval waveform."""
    out_dir = Path(out_dir)
    manifest_path = _speech_review_manifest_path(out_dir)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": 1, "script": str(script or ""), "parts": parts, **extra}
    tmp = manifest_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, manifest_path)
    return payload


def load_speech_review_parts(out_dir, script=None):
    """Load durable speech parts, including parts retained by newer TTS runs.

    The manifest is preferred because its text/audio association is explicit.  For a project
    created before the manifest existed, retained ``tts_part_files`` are upgraded in place.
    """
    out_dir = Path(out_dir)
    state = load_state(out_dir, script) or {}
    script = str(script if script is not None else state.get("script") or "")
    manifest_path = _speech_review_manifest_path(out_dir)
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        rows = list(manifest.get("parts") or [])
        if manifest.get("script") == script and rows and all(
                _audio_done(out_dir / str(row.get("file") or "")) for row in rows):
            return manifest
    except Exception:
        pass

    texts = list(state.get("tts_part_texts") or split_script_for_tts(script))
    files = [Path(p) for p in (state.get("tts_part_files") or [])]
    if len(files) != len(texts) or not all(_audio_done(p) for p in files):
        return None
    speed = float(state.get("voice_speed") or 1.0)
    # Retained TTS source parts are natural-speed audio, while the selected combined voiceover
    # may be 1.15x. Review and regeneration must use ONE clock or a replaced paragraph would be
    # fast between slower neighbours (and every downstream image switch would drift again).
    if abs(speed - 1.0) >= 0.01:
        files = [apply_voice_speed(path, speed) for path in files]
    rows = []
    for idx, (text, path) in enumerate(zip(texts, files)):
        rows.append({"index": idx, "text": text, "file": os.path.relpath(path, out_dir),
                     "duration": round(audio_duration_seconds(path), 3), "take": 0})
    return save_speech_review_manifest(
        out_dir, script, rows, voice=str(state.get("voice") or ""),
        tts_model=str(state.get("tts_model") or "pro"),
        voice_speed=speed)


def reconstruct_speech_review_parts(out_dir, script=None, status_cb=None):
    """Upgrade an old project by cutting its selected voiceover back into TTS-sized parts.

    Word-aligned line timings are used when present, so cuts land between the exact script
    sections.  The proportional fallback is only for very old projects without word timing.
    """
    out_dir = Path(out_dir)
    state = load_state(out_dir, script) or {}
    script = str(script if script is not None else state.get("script") or "")
    texts = split_script_for_tts(script)
    source = voiceover_path_from_state(out_dir, state)
    if not texts or not _audio_done(source):
        return None
    duration = audio_duration_seconds(source)
    if duration <= 0:
        return None
    word_starts = []
    for line in state.get("lines") or []:
        for word in line.get("words") or []:
            try:
                word_starts.append(float(word.get("s")))
            except (TypeError, ValueError):
                pass
    counts = [max(1, len(re.findall(r"\b[\w'-]+\b", text))) for text in texts]
    total_words = sum(counts)
    boundaries = [0.0]
    consumed = 0
    for count in counts[:-1]:
        consumed += count
        if len(word_starts) >= total_words and consumed < len(word_starts):
            before = word_starts[max(0, consumed - 1)]
            after = word_starts[consumed]
            boundary = (before + after) / 2.0
        else:
            boundary = duration * consumed / float(total_words or 1)
        boundaries.append(max(boundaries[-1] + 0.01, min(duration, boundary)))
    boundaries.append(duration)

    parts_dir = out_dir / "speech_parts"
    parts_dir.mkdir(parents=True, exist_ok=True)
    ffmpeg = pipeline.find_ffmpeg()
    rows = []
    for idx, text in enumerate(texts):
        target = parts_dir / f"part_{idx:03d}.wav"
        start, end = boundaries[idx], boundaries[idx + 1]
        done = subprocess.run(
            [ffmpeg, "-y", "-ss", f"{start:.6f}", "-to", f"{end:.6f}", "-i", str(source),
             "-vn", "-acodec", "pcm_s16le", str(target)],
            capture_output=True, text=True, timeout=max(120, int(end - start) * 2), check=False)
        if done.returncode != 0 or not _audio_done(target):
            raise LongformError(f"Could not prepare speech part {idx + 1}: "
                                f"{(done.stderr or '')[-240:]}")
        rows.append({"index": idx, "text": text,
                     "file": os.path.relpath(target, out_dir),
                     "duration": round(audio_duration_seconds(target), 3), "take": 0})
    _log(status_cb, f"Prepared {len(rows)} reviewable speech parts from the existing voiceover.")
    return save_speech_review_manifest(
        out_dir, script, rows, voice=str(state.get("voice") or ""),
        tts_model=str(state.get("tts_model") or "pro"),
        voice_speed=float(state.get("voice_speed") or 1.0), source=source.name)


def commit_speech_review_parts(out_dir, manifest, status_cb=None):
    """Stitch reviewed parts and invalidate every old image-switch timestamp.

    The images themselves remain on disk.  On the next project continuation the new voiceover is
    transcribed, :func:`retime_longform_assets` renames the matching images onto the new clock,
    and ``timeline.json`` is rewritten from those new positions.
    """
    out_dir = Path(out_dir)
    state = load_state(out_dir, manifest.get("script")) or {}
    rows = sorted(manifest.get("parts") or [], key=lambda row: int(row.get("index", 0)))
    paths = [out_dir / str(row.get("file") or "") for row in rows]
    if not rows or not all(_audio_done(path) for path in paths):
        raise LongformError("One or more reviewed speech parts are missing.")
    ffmpeg = pipeline.find_ffmpeg()
    target = out_dir / f"voiceover_review_{int(time.time() * 1000)}.wav"
    concat_audio_parts(paths, target, ffmpeg)
    # A second review can happen before the pending retime has run. Preserve the ORIGINAL clock
    # in that case; replacing it with empty `lines` would orphan every existing image.
    old_lines = list(state.get("lines") or state.get("retime_source_lines") or [])
    old_prompts = list(state.get("prompts") or state.get("retime_source_prompts") or [])
    old_duration = float(state.get("audio_duration") or
                         state.get("retime_source_audio_duration") or 0.0)
    save_state(
        out_dir, voiceover_ready=True, voiceover_file=target.name,
        tts_parts=len(rows), tts_part_total=len(rows),
        tts_part_files=[str(path) for path in paths],
        tts_part_texts=[str(row.get("text") or "") for row in rows],
        # Explicit retime source: clearing `lines` prevents resume from pairing a new narration
        # with the previous image clock, while these fields let it reuse/rename the images.
        retime_source_lines=old_lines, retime_source_prompts=old_prompts,
        retime_source_audio_duration=old_duration,
        lines=None, prompts=None, audio_duration=0.0)
    _log(status_cb, "Reviewed speech saved. Image-switch timings will be regenerated from the "
                    "new narration when the project continues.")
    return target


def finalize_speech_review_timing(out_dir, status_cb=None):
    """Immediately rebuild image filenames + timeline after speech-part regeneration."""
    out_dir = Path(out_dir)
    try:
        state = json.loads((out_dir / STATE_FILE).read_text(encoding="utf-8"))
    except Exception:
        state = {}
    script = str(state.get("script") or "")
    old_lines = list(state.get("retime_source_lines") or [])
    old_prompts = list(state.get("retime_source_prompts") or [])
    old_duration = float(state.get("retime_source_audio_duration") or 0.0)
    voice = voiceover_path_from_state(out_dir, state)
    if not script or not old_lines or not _audio_done(voice):
        raise LongformError("The previous image clock or the reviewed voiceover is missing.")
    _log(status_cb, "Re-transcribing reviewed narration to rebuild every image switch...")
    new_duration = audio_duration_seconds(voice)
    new_lines = transcribe_lines(script, voice, status_cb=status_cb)
    if len(new_lines) != len(old_lines):
        raise LongformError(
            f"Speech retiming produced {len(new_lines)} lines but the project has "
            f"{len(old_lines)} images. The old timeline was kept so no image is misplaced.")
    prompts = retime_longform_assets(
        out_dir, old_lines, new_lines, old_duration, new_duration,
        prompts=old_prompts, status_cb=status_cb)
    save_state(out_dir, lines=new_lines, prompts=prompts,
               audio_duration=round(new_duration, 3),
               retime_source_lines=None, retime_source_prompts=None,
               retime_source_audio_duration=0.0)
    write_transcript(new_lines, out_dir / "transcript.txt")
    if prompts:
        write_prompts_file(prompts, out_dir / f"image_prompts_{out_dir.name}.txt")
    # Retime can expose empty slots even though suitable pictures are parked in audit/archive
    # folders. Reuse those project-local assets immediately before declaring anything missing.
    recover_archived_images(out_dir, status_cb=status_cb)
    natural_durations = line_durations(new_lines, new_duration)
    results = {}
    for idx, line in enumerate(new_lines):
        candidate = out_dir / "images" / f"{image_key(idx, line, natural_durations[idx])}.png"
        if _image_done(candidate, IMAGE_ASPECT):
            results[idx] = str(candidate)
    cut_durations = caption_cut_durations(new_lines, prompts, new_duration)
    write_timeline_manifest(
        new_lines, cut_durations, results, new_duration, out_dir / "timeline.json",
        voice_speed=float(state.get("voice_speed") or 1.0))
    _log(status_cb, f"Timeline retimed: {len(new_lines)} image switches now follow the "
                    "regenerated narration.")
    return {"lines": len(new_lines), "duration": round(new_duration, 3),
            "images": len(results), "timeline": str(out_dir / "timeline.json")}


# ------------------------------------------------------------------ 2) TIMESTAMPS

# A 20-minute explainer holds a drawing about four seconds; a one-minute short cannot. At the
# long-form pace a 37s short got ten images, and the packing to a 10-12 word target swallowed the
# punchlines - "The instant itself, where being awake ended. You can't." shared one picture, so
# the line that lands got no image of its own.
SHORT_MIN_WORDS, SHORT_MAX_WORDS = 3, 9


def strip_script_headings(script):
    """Drop markdown headings and horizontal rules from a pasted script.

    A pasted document usually opens with "# Some Title". That is the document's title, not a
    spoken line - but every stage downstream took it as narration: it was read aloud (hash
    included) as TTS part 0, became its own drawing beat, and was picked as the hook. So the
    short opened with its title spoken twice, once in the Google intro and once in the video.
    """
    kept = []
    for line in str(script or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if stripped and set(stripped) <= set("-=_*"):        # ---- / **** rules
            continue
        kept.append(line)
    return chr(10).join(kept).strip()


def split_script_lines(script, min_words=6, max_words=15, short_form=False):
    """Split narration into First-Dog-style visual beats.

    That reference edit holds a drawing for about four seconds and changes on a completed
    thought. The previous 3–8 word clock averaged roughly two seconds and could emit isolated
    one-word frames. Prefer punctuation near the natural 10–12 word centre, allow a slightly
    shorter complete sentence, and rebalance the tail so tiny orphan cuts cannot survive.
    """
    words = re.sub(r"\s+", " ", str(script or "")).strip().split()
    if not words:
        return []
    if short_form:
        min_words, max_words = SHORT_MIN_WORDS, SHORT_MAX_WORDS
    min_words = max(2, int(min_words))
    max_words = max(min_words, int(max_words))
    target = max(min_words, round((min_words + max_words) / 2))
    # A finished sentence and a comma are NOT worth the same. Ranking them together meant a
    # comma one word from the centre beat a full stop two words away, and the drawing changed
    # in the middle of a thought.
    sentence_end = re.compile(r"[.!?][\"')\]]*$")
    clause_end = re.compile(r"[,;:—–-][\"')\]]*$")
    # How far past the ceiling it may reach for a real boundary rather than chopping between
    # two words of one phrase. On the sleep script, 23% of all cuts landed mid-sentence -
    # "...the exact moment you fell" / "asleep last night." was cut between "fell" and "asleep".
    overshoot = 4
    lines = []
    pos = 0
    while pos < len(words):
        remaining = len(words) - pos
        if remaining <= max_words:
            take = remaining
        else:
            lo = pos + min_words
            hi = min(pos + max_words, len(words))
            nearest = lambda spots: min(spots, key=lambda i: (abs((i - pos) - target), i)) - pos
            strong = [i for i in range(lo, hi + 1) if sentence_end.search(words[i - 1])]
            if short_form and strong:
                # Break at the FIRST finished sentence, not the one nearest a word target. In a
                # short every complete thought earns its own picture; packing two of them into
                # one beat is what made the cuts feel off against the voice.
                take = min(strong) - pos
                lines.append(words[pos:pos + take])
                pos += take
                continue
            weak = [i for i in range(lo, hi + 1) if clause_end.search(words[i - 1])]
            if strong:
                take = nearest(strong)
            elif weak:
                take = nearest(weak)
            else:
                # Nothing to cut on inside the window: look a little past the ceiling before
                # giving up, so a long clause ends where it ends instead of mid-phrase.
                stretch = min(pos + max_words + overshoot, len(words))
                late = [i for i in range(hi + 1, stretch + 1)
                        if sentence_end.search(words[i - 1]) or clause_end.search(words[i - 1])]
                take = (min(late) - pos) if late else max_words
        lines.append(words[pos:pos + take])
        pos += take

    # A final remainder can be shorter than the floor when the preceding beat already reached
    # the ceiling. Redistribute the pair instead of producing an abrupt one-word image flash.
    if len(lines) > 1 and len(lines[-1]) < min_words:
        combined = lines[-2] + lines[-1]
        if len(combined) <= max_words:
            lines[-2:] = [combined]
        else:
            left_size = max(min_words, min(max_words, len(combined) // 2))
            lines[-2:] = [combined[:left_size], combined[left_size:]]
    return [" ".join(line) for line in lines]


def fmt_ts(seconds, precise=True):
    """[m:ss.d] with sub-second precision (more exact than the foziscribe example)."""
    seconds = max(0.0, float(seconds))
    m = int(seconds // 60)
    s = seconds - m * 60
    return f"[{m}:{s:04.1f}]" if precise else f"[{m}:{int(s):02d}]"


# ------------------------------------------------------------------ 2b) SUBTITLES
#
# The subtitle rules live in subtitles.py so that the sketch shorts, the longform explainers
# and the clip shorts all cut captions the same way. They are re-exported under their old
# names because for the sketch/longform modes the cues ARE the edit clock: the picture
# changes exactly where the caption changes.
from subtitles import (                              # noqa: E402  (grouped with its users)
    SRT_GAP, SRT_MAX_CHARS, SRT_MAX_LINE_CHARS, SRT_MAX_SECONDS, SRT_MAX_WORDS,
    SRT_MIN_SECONDS, SRT_PAUSE, parse_srt, srt_clock, subtitle_cues, tidy_cues,
    wrap_subtitle, write_srt)

SUBTITLE_FILE = "subtitles.srt"

# How far a picture cut may be moved to land on a caption boundary. Measured on the sleep
# explainer: at 0.6s a third of the cuts snap and none leaves its own sentence; forcing
# every cut onto a boundary instead pulled them seconds away from the words.
SNAP_WINDOW_S = 0.6


def ensure_subtitles(project_dir, lines, status_cb=None):
    """Write the project's .srt from the beats that are actually on screen.

    Called on every render, not only on a fresh transcription, so the subtitle timestamps and
    the picture cuts can never drift apart: whichever clock produced the beats, the file next to
    the video describes THAT clock. A project made before subtitles existed gets one here.
    """
    if not lines:
        return None
    cues = tidy_cues([{"start": float(l.get("start") or 0.0),
                       "end": float(l.get("end") or 0.0),
                       "text": str(l.get("text") or "").lstrip("# ").strip()}
                      for l in lines])
    path = write_srt([c for c in cues if c["text"]], subtitle_path(project_dir))
    _log(status_cb, f"Subtitles: {path.name} ({len(cues)} cue(s)), same timestamps as the cuts.")
    return path


def subtitle_path(project_dir):
    return Path(project_dir) / SUBTITLE_FILE


# A picture may change more often than a subtitle does. The reference explainer holds a drawing
# ~2.8s (21.8/min); cue-length beats gave 13-18/min. Splitting a long cue in two is what closes
# that gap without touching the subtitle timings the user tuned by hand.
BEAT_TARGET_SECONDS = 2.9      # measured off the reference edit
BEAT_MIN_SECONDS = 1.15        # below this a drawing reads as a flicker, not a beat
BEAT_MIN_WORDS = 3


def split_cue_into_beats(cue, target=BEAT_TARGET_SECONDS):
    """One cue -> one or more picture beats, cut on its own word timings.

    Returns a list of {start, end, text, words}. A cue that is already short enough, or has too
    few words to divide, comes back unchanged - never an empty list.
    """
    words = list(cue.get("words") or [])
    start = float(cue.get("start") or 0.0)
    end = float(cue.get("end") or start)
    span = end - start
    pieces = max(1, int(round(span / float(target or BEAT_TARGET_SECONDS))))
    if (pieces < 2 or span < BEAT_MIN_SECONDS * 2
            or len(words) < BEAT_MIN_WORDS * 2):
        return [{"start": start, "end": end, "text": str(cue.get("text") or ""),
                 "words": words}]
    # Never more pieces than the words can honestly carry.
    pieces = min(pieces, len(words) // BEAT_MIN_WORDS)
    if pieces < 2:
        return [{"start": start, "end": end, "text": str(cue.get("text") or ""),
                 "words": words}]

    def word_start(word, fallback):
        try:
            return float(word.get("s", fallback))
        except (AttributeError, TypeError, ValueError):
            return float(fallback)

    def word_text(word):
        if isinstance(word, dict):
            return str(word.get("w") or "")
        return str(word or "")

    # Prefer a comma/clause boundary near each ideal split, so a drawing changes on a breath
    # rather than between two words of one phrase.
    breaks = [0]
    for piece in range(1, pieces):
        ideal = int(round(len(words) * piece / pieces))
        best, best_cost = ideal, 99
        for offset in range(-2, 3):
            index = ideal + offset
            if index <= breaks[-1] or index >= len(words):
                continue
            if index - breaks[-1] < BEAT_MIN_WORDS or len(words) - index < BEAT_MIN_WORDS:
                continue
            previous = word_text(words[index - 1]).rstrip()
            cost = abs(offset) + (0 if previous.endswith((",", ";", ":", ".", "!", "?")) else 3)
            if cost < best_cost:
                best, best_cost = index, cost
        if best > breaks[-1]:
            breaks.append(best)
    breaks.append(len(words))

    out = []
    for position in range(len(breaks) - 1):
        chunk = words[breaks[position]:breaks[position + 1]]
        if not chunk:
            continue
        piece_start = word_start(chunk[0], start) if position else start
        if position + 1 < len(breaks) - 1:
            following = words[breaks[position + 1]] if breaks[position + 1] < len(words) else None
            piece_end = word_start(following, end) if following is not None else end
        else:
            piece_end = end
        if piece_end - piece_start < 0.05:
            piece_end = piece_start + 0.05
        # The minimum was only checked against the WHOLE cue, so a split that landed on an
        # awkward word boundary still produced a sub-second flash (measured: 0.94s). Fold a
        # too-short piece back into the one before it.
        if out and piece_end - piece_start < BEAT_MIN_SECONDS:
            out[-1]["end"] = piece_end
            out[-1]["text"] = (out[-1]["text"] + " "
                               + " ".join(word_text(w) for w in chunk)).strip()
            out[-1]["words"] = list(out[-1]["words"]) + list(chunk)
            continue
        out.append({"start": piece_start, "end": piece_end,
                    "text": " ".join(word_text(w) for w in chunk).strip(),
                    "words": chunk})
    # The backward merge above cannot rescue the FIRST piece of a cue - there is nothing behind
    # it yet - so a fast opening chunk could still flash by (measured: 8 beats under a second
    # across four projects). One cleanup pass, folding any survivor into its neighbour.
    cleaned = []
    for piece in out:
        if (cleaned and piece["end"] - piece["start"] < BEAT_MIN_SECONDS):
            cleaned[-1]["end"] = piece["end"]
            cleaned[-1]["text"] = (cleaned[-1]["text"] + " " + piece["text"]).strip()
            cleaned[-1]["words"] = list(cleaned[-1]["words"]) + list(piece["words"])
        elif (not cleaned and len(out) > 1
              and piece["end"] - piece["start"] < BEAT_MIN_SECONDS):
            follower = out[1]
            follower["start"] = piece["start"]
            follower["text"] = (piece["text"] + " " + follower["text"]).strip()
            follower["words"] = list(piece["words"]) + list(follower["words"])
        else:
            cleaned.append(piece)
    out = cleaned
    return out or [{"start": start, "end": end, "text": str(cue.get("text") or ""),
                    "words": words}]


def transcribe_lines(script, audio_path, status_cb=None, srt_path=None):
    """Voiceover -> subtitle cues -> [{start, end, text, words}] beats with EXACT timings.

    The known script is aligned onto faster-whisper word timings (voice_align). Those words are
    then grouped into SUBTITLE cues, and each cue becomes one beat - one picture. The subtitle
    file is the intermediate product and is written to disk, so the cuts in the finished video
    and the timestamps in the .srt are the same numbers by construction rather than by luck.
    """
    import voice_align
    if not voice_align.available():
        raise LongformError("faster-whisper is not installed (pip install faster-whisper).")
    _log(status_cb, "Transcribing the voiceover (faster-whisper, word timestamps)...")
    asr_words = voice_align.transcribe_words(audio_path, status_cb=status_cb)
    if not asr_words:
        raise LongformError("Transcription produced no words.")
    aligned = voice_align.align_script_to_words(str(script), asr_words)
    cues = subtitle_cues(aligned)
    if not cues:
        raise LongformError("The voiceover produced no subtitle cues.")
    if srt_path:
        written = write_srt(cues, srt_path)
        _log(status_cb, f"Subtitles written: {written.name} ({len(cues)} cue(s)). "
                        f"Every picture change lands on one of these timestamps.")
    # The .srt above is written from the UNTOUCHED cues. The pictures are then allowed to change
    # more often inside them: one cue can carry two drawings where it runs long.
    out = []
    for cue in cues:
        for beat in split_cue_into_beats(cue):
            out.append({"start": round(float(beat["start"]), 2),
                        "end": round(float(beat["end"]), 2),
                        "text": beat["text"], "words": beat["words"]})
    span = max(0.001, float(out[-1]["end"]) - float(out[0]["start"])) if out else 1.0
    _log(status_cb, f"Transcript: {len(cues)} subtitle cue(s) -> {len(out)} picture beat(s) "
                    f"({len(out) / (span / 60.0):.1f} per minute).")
    return out


def write_transcript(lines, out_path):
    out_path = Path(out_path)
    out_path.write_text("\n".join(f"{fmt_ts(l['start'])} {l['text']}" for l in lines) + "\n",
                        encoding="utf-8")
    return out_path


# ------------------------------------------------------------------ 3) IMAGE PROMPTS

# Bump when the STAGE-3 doodle-prompt FORMAT changes. A resumed project whose saved prompts
# predate this version regenerates ALL prompts - and the images made from them - in the new
# style instead of reusing the old-format cache.
# 6: First-Dog-style visual beats replace the overly dense 3–8 word image clock. Existing
# prompt/image sets intentionally regenerate because their subjects no longer match the lines.
PROMPT_FORMAT_VERSION = 6
SCENE_CLOCK_VERSION = 2

# The mascot is described in WORDS: the image endpoint takes text only, and the look has to
# survive hundreds of frames anyway.
# A short, rigid description keeps it recognisable; the last line is what makes it feel part of
# the drawing instead of a sticker (user 2026-07-25: "seine aktionen sollen zum bild passen").
MASCOT_ART = Path(__file__).resolve().parent / "assets" / "mascot" / "blob.png"
MASCOT_NAME = "Blob"
MASCOT_LOOK = (
    "a small mascot called Blob: one rounded blob-shaped body in warm mustard yellow with a "
    "thick dark charcoal outline, a wide flat bottom, no arms or legs, and two oversized "
    "white circular eyes of different sizes sitting high and off-centre with small dark "
    "pupils. Always this exact character, same colours, same thick outline, drawn in the same "
    "flat MS Paint style as the rest of the frame, thick uniform black outline, no shading")
MASCOT_RULE = (
    "MASCOT: hide {name} somewhere in this frame - {look}. Keep it SMALL (roughly a tenth of "
    "the frame height) and place it off to one side, in a corner, behind or peeking around "
    "something. It must never be the subject and never overlap the main action. Give it ONE "
    "small reaction that fits what this frame shows - watching, "
    "hiding, leaning in, looking away, mimicking the subject - so it belongs to the scene.")


def mascot_clause():
    """The sentence appended to every image prompt when the mascot option is on."""
    return MASCOT_RULE.format(name=MASCOT_NAME, look=MASCOT_LOOK)


def add_mascot(prompts):
    """Append the mascot instruction to every prompt row (idempotent).

    Rows are {"timestamp": ..., "prompt": ...}; only the prompt text is touched, so the
    positional line mapping and the timestamp check downstream stay intact.
    """
    clause = mascot_clause()
    out = []
    for row in prompts:
        if not isinstance(row, dict):
            txt = str(row or "").strip()
            out.append(txt if "MASCOT:" in txt else (txt.rstrip(". ") + ". " + clause))
            continue
        txt = str(row.get("prompt") or "").strip()
        if txt and "MASCOT:" not in txt:
            row = dict(row, prompt=txt.rstrip(". ") + ". " + clause)
        out.append(row)
    return out


STAGE3_PROMPT = """## STAGE 3 - GENERATE IMAGE PROMPTS FOR EVERY TIMESTAMP

Once the user pastes their timestamped script, generate one detailed text-to-image prompt for every single timestamp line.

**IMAGE PROMPT RULES:**

1. Every prompt must begin with its timestamp COPIED EXACTLY as it appears in the script (e.g. `[0:03.4]`) - do not reformat or round timestamps
2. Every prompt must open with the style anchor: "Flat solid-color hand-drawn webcomic illustration, crude MS Paint look, no gradients, no realistic lighting, stick figures with large round white heads and thin straight black limbs, thick uniform black outlines of one constant weight on everything including props and background shapes, oversized simple black dot eyes with exaggerated expressions, thin curved eyebrows, simple line mouths,"
   The anchor names the drawing language ONCE for the whole frame - figure, props and scenery
   alike. Without "including props and background shapes" the model draws a flat comic character
   and then renders the room behind it with soft shading and a gradient sky, and the frame stops
   looking hand-made. Every added prop is drawn with the SAME outline weight as the character.
3. Every prompt must end with one of two style locks. There are two because the wordless lock, appended to every prompt, is what made the labelled diagrams of rule 10 impossible - it forbade the labels that are the whole point of those frames.
   ORDINARY SCENE (the default, use for almost every prompt): "flat single-color background, flat solid fill colors only, no gradients, no shading, no texture, no lighting, no text, no words, no letters, no numbers, no captions, no labels, no signage, no photorealism, no 3D, no realistic faces, no anime style, amateur webcomic aesthetic, deliberately unpolished, minimal detail, 16:9 aspect ratio."
   DIAGRAM FRAME (only for the frame types in rule 10): "flat single-color background, flat solid fill colors only, no gradients, no shading, no texture, no lighting, short bold capital labels only, no sentences, no paragraphs, no photorealism, no 3D, no realistic faces, no anime style, amateur webcomic aesthetic, deliberately unpolished, minimal detail, 16:9 aspect ratio."
4. Be specific inside each prompt - describe what characters are present and what they are doing, their exact expression, what objects are in the scene, what background color is used

5. NO TEXT IN ORDINARY SCENES - but LABELLED DIAGRAMS ARE REQUIRED. In a normal illustrative frame the picture carries no written language: no captions, no titles, no speech bubbles with words, no signs, no book covers, no numbers on clocks, no letters on shirts. Replace an apparent need for a word with a DRAWN symbol - a question mark, an exclamation mark, an arrow, a heart, a skull, a tick, a lightbulb, a magnifying glass, a clock face without numbers.
   The exception is a DIAGRAM FRAME, and it is not optional: see rule 10. There, short labels are the point of the picture. Keep every label to ONE or TWO words in capitals, no sentences - an image model draws a short word cleanly and turns a sentence into scribble.
6. VISUALISE THE SITUATION, NOT THE WORD. This is the single biggest quality difference between
   a channel that looks hand-made and one that looks like a slide deck. A concept rendered as a
   giant symbol on a colour field reads as a stock icon; the same concept staged as a moment with
   a character in it reads as a film.
   BANNED as the SUBJECT of a frame: a giant question mark, a giant red X, a giant pair of
   scissors, a giant hourglass, a giant clock, a gear over a head, a lightbulb over a head, a
   floating brain. These may appear SMALL inside a scene - never as the thing the frame is about.
   - "your body doesn't know the difference" -> NOT a figure under a huge question mark. Instead:
     the figure at a kitchen table looking between two identical mugs, one hand half-raised,
     eyebrows up, a chair pushed back behind him and a window with morning light behind that.
   - "millions of years" -> NOT a huge hourglass. Instead: the same patch of ground drawn three
     times across the frame, divided by two thin black lines - a fern and a lizard, then a small
     furry animal, then a stick figure with a phone - the hills behind changing shape in each.
   - "you can't remember the moment" -> NOT a giant X over a bed. Instead: the figure sitting up
     in bed reaching back over its own shoulder while three small thought-pictures behind its
     head fade out one after another, lamp on, window dark.

GOLD-STANDARD EXAMPLE - ORDINARY SCENE. Match this exact shape: style anchor, then a NAMED
CAMERA FRAMING, then the three layers of rule 9a, then the background colour, then the style lock.
The two figures are not floating on a colour - they are in a hall, with things in front of them
and behind them:
`Flat solid-color hand-drawn webcomic illustration, crude MS Paint look, no gradients, no realistic lighting, stick figures with large round white heads and thin straight black limbs, thick uniform black outlines of one constant weight on everything including props and background shapes, oversized simple black dot eyes with exaggerated expressions, thin curved eyebrows, simple line mouths, medium shot, a helmeted stick figure and a crowned stick figure clinking two beer mugs together at a long wooden table, both with wide open line mouths and eyebrows raised high, a red heart shape floating above them, a roast bird and a spilled cup on the table in front of them, a bench and a third figure asleep face-down further along the table, behind them a stone archway and two hanging banners with a torch bracket on the wall, plain pale blue background, flat single-color background, flat solid fill colors only, no gradients, no shading, no texture, no lighting, no text, no words, no letters, no numbers, no captions, no labels, no signage, no photorealism, no 3D, no realistic faces, no anime style, amateur webcomic aesthetic, deliberately unpolished, minimal detail, 16:9 aspect ratio.`

COUNTER-EXAMPLE - what NOT to write. Same information, and it is the look being removed: a subject
with no place, no framing, no layers, and a symbol doing the explaining.
`Simple MS Paint style illustration, ... a stick figure lying in a bed with a large red X above it, plain dark blue background, ...`

GOLD-STANDARD EXAMPLE - DIAGRAM FRAME (rule 10; note the short capital labels and the diagram lock):
`Flat solid-color hand-drawn webcomic illustration, crude MS Paint look, no gradients, no realistic lighting, thick uniform black outlines of one constant weight on everything, four rounded boxes arranged in a ring joined by four thick black curved arrows pointing clockwise, each box holding one crude drawing - a stick figure lying awake with wide dot eyes, a stick figure staring at a glowing rectangle, a stick figure slumped with drooping eyebrows, a stick figure lying awake again - and one bold black capital word beneath each box reading WAKE, SCREEN, TIRED, WAKE, plain warm yellow background, flat single-color background, flat solid fill colors only, no gradients, no shading, no texture, no lighting, short bold capital labels only, no sentences, no paragraphs, no photorealism, no 3D, no realistic faces, no anime style, amateur webcomic aesthetic, deliberately unpolished, minimal detail, 16:9 aspect ratio.`

7. THE BACKGROUND COLOUR MUST CHANGE THROUGH THE VIDEO - AND IT COMES FROM THE PLACE, NOT THE
   MOOD. Twenty seconds of the same dark blue is the fastest way to look cheap; measured on a
   finished short, the first four frames were dark blue, dark blue, dark blue, dark blue. Derive
   the colour from WHERE the scene is: a bedroom at night is deep blue, but the lamp corner of
   that same bedroom is warm amber, moonlight through the window is pale grey-blue, a diagram is
   flat beige or off-white, the moment of falling asleep is violet, morning is pale yellow. That
   gives six different frames inside one location, which is exactly what the reference channels
   do. Never run one colour for more than about ninety seconds, and never for more than three
   consecutive frames. A viewer who sees the same colour at 0:50 as at 0:02 has stopped
   registering the picture; consistency belongs on the thumbnail, not inside the film. Tone can
   still steer the choice:
   - Ancient / prehistoric -> tan or dark blue background
   - Danger / threat -> stark white or a red-tinted sky
   - Happy / triumph / discovery -> bright white or yellow background
   - Underwater / science -> solid blue background
   - Outdoor / nature / evolution -> flat green ground + blue sky
   - Fire / night / ancient ritual -> solid orange background
8. Hold the SUBJECT across consecutive timestamps, but change the PICTURE EVERY TIME. If three lines describe one moment, stay on that moment - and cut to a different framing of it: from the figure to a close-up of the object in its hand, to a wide shot showing the whole room, to a diagram of what is happening. Every prompt must deliberately name a DIFFERENT camera framing, gesture, reaction, object detail, panel or cause/effect diagram than the frame immediately before it. What must not repeat is the identical figure at the identical angle on the identical background; six frames of one character in one bed is the failure this rule exists to prevent, not the outcome it wants.

9a. PUT THE SCENE SOMEWHERE - AND BUILD IT IN THREE LAYERS. A figure floating on a colour field
   is a placeholder, not an illustration. At least three quarters of the frames in a video must be
   a real place, drawn in the same flat style with the same thick outlines. Name all three layers
   in the prompt, every time:
   - BACKGROUND: the flat colour plus what is far away - a window with a moon and stars, hills,
     a wall with a picture on it, trees, a horizon line
   - MIDGROUND: the character and the furniture or terrain it is actually on - bed, chair, table,
     rock, campfire
   - FOREGROUND: one or two near objects that frame the shot - a corner of a blanket, slippers, a
     glass of water on a nightstand, a branch across the top corner
   A bedroom is not "a bedroom": it is a window with a moon, a wall clock, a bedside lamp, a
   nightstand, a glass of water, slippers, a blanket folded back, a door ajar. Ask for those
   things by name. The BACKGROUND stays one flat colour - every prop sits on top of it as a flat
   shape with the same outline weight. This costs nothing in style and is most of the difference
   between an empty frame and a scene.
   THE LAYERS MUST BE AT DIFFERENT DISTANCES, AND THEY FOLLOW THE FRAMING. Three layers is how a
   WIDE or MEDIUM shot is built. On an extreme close-up there is no far horizon and no branch
   across the corner: name only what is genuinely at that distance - the object filling the
   frame, and the one flat colour behind it. Asking for all three layers on a close-up is what
   produces a pile of unrelated flat shapes overlapping each other with nothing behind anything.
   Say where each named thing SITS relative to the subject - "behind the figure", "on the table
   in front of it" - so the drawing stacks instead of colliding.
9b. ABSENCE IS STILL DRAWN. A line about something NOT happening - a memory that was never made, a gap, nothing being recorded - must still show a scene with a subject in it. Never ask for "a blank page", "an empty void", "a faint dot on white" or anything whose subject IS the emptiness: the generator obeys, and the frame arrives as bare paper. Draw the absence as an ACTION instead - the figure holding an empty notebook up to the light, a filing cabinet with one drawer pulled out and nothing inside, a hand reaching for a shelf where the box should be. Measured on a finished short, two frames written this way came back with under 3% of the page carrying any ink at all, against 10% for the rest.
9c. ONE FRAME, MORE THAN ONE MOMENT. The strongest frames in the reference channels carry
   several beats of story at once - a character waking, a danger behind it, and the result of
   both, all in one picture. A frame that says only "awake | asleep" is a caption with a border.
   Wherever a line covers a span of time or a change of state, draw the span: the same character
   two or three times across one frame, divided by thin black lines or simply staged left to
   right, each instance a little further along - reading in bed with the lamp on, then eyes
   half-closed with the book tipping, then asleep with the book on the floor and the lamp off.
   Small clock faces, a dropped object or a changing sky carry the passage of time without a
   single written word.

9d. ROTATE THE CAMERA. Six frames of one character, centred, front-on, full body is the look this
   brief exists to prevent. Name a framing in every prompt and do not use the same one twice in a
   row. Rotate through: wide establishing shot, medium shot, extreme close-up of the eyes,
   overhead shot looking straight down at the bed, side profile, over-the-shoulder POV,
   silhouette against a lit window, reaction close-up, labelled diagram, split panel. A useful
   run is wide -> close-up -> overhead -> diagram -> close-up -> wide.
   MOVE ONE STEP AT A TIME, AND KEEP THE CHARACTER THE SAME CHARACTER. Rotating the camera does
   not mean jumping between the extremes: going wide -> extreme close-up -> wide makes the
   figure's head change size violently from frame to frame and the sequence stops reading as one
   place. Step through neighbouring framings - wide to medium, medium to close-up - and when you
   do cut to an extreme close-up, cut back out through a medium shot rather than straight to a
   wide. Whatever the distance, the character keeps the same proportions, the same head-to-body
   ratio and the same face; only how much of it you can see changes.

10. DIAGRAMS AND PANELS ARE PART OF THE RHYTHM, NOT A GARNISH. At least one labelled diagram every ninety seconds, and a split panel at least as often. These are the frames that EXPLAIN rather than merely illustrate, and they buy variety without inventing a new location:
   - **Labelled cycle:** three or four boxes in a ring joined by thick arrows, each box holding a tiny drawing and a ONE-WORD capital label, showing how a loop feeds itself
   - **Labelled before/after:** the same figure twice, divided by one bold black line, each half with a one-word label under it
   - **Four-panel grid:** one frame split into four by thick black lines, each panel a different moment or condition with a one-word label
   - **Labelled anatomy:** one object with two or three thick arrows pointing at the parts that matter, one word at the end of each arrow
   Everything else stays wordless. Use these proven WORDLESS frame types for the rest:
   - **Object IN USE:** the object that matters held, carried, dropped, broken or looked at by a
     character inside a place - a cracked hourglass lying in the sand with a figure walking past
     it, not an hourglass centred on a colour field. The old "one large object centred on a plain
     background" frame is the icon look this brief exists to remove; it is no longer a frame type.
   - **Evolution sequence:** left-to-right creature or human progression with a big right-pointing arrow
   - **Pointed diagram:** an animal or object with a thick yellow arrow pointing at the one part that matters
   - **Stick figure reaction:** a thought bubble above the head containing a drawn SYMBOL - a question mark, an exclamation mark, a lightbulb, a skull - never a written word
   - **Villain personified:** an abstract concept given an angry cartoon face (a sun with a knife, a brain with boxing gloves)
   - **Globe + creatures:** an Earth globe centered, surrounded by floating cartoon animals or objects
   - **Comparison split:** two halves of the frame showing the two things being compared, divided by one bold black line

**OUTPUT FORMAT - DELIVER IN BATCHES OF 20**

Deliver the image prompts in batches of 20 prompts at a time inside copyable code blocks. Do NOT create any text file. Do NOT deliver all prompts at once.

Rules for batching:

- Output the first 20 prompts (or fewer, if the script has fewer remaining) inside ONE fenced code block. Separate each prompt from the next with exactly ONE blank line. Do NOT add any text, headers, or commentary between prompts inside the code block.
- After each batch's code block, if more timestamps remain, end with exactly this line, then stop and wait:

**Reply "next" for the next 20 prompts.**

- When the user replies "next", output the next batch of up to 20 prompts in a new code block, following the same format.
- Continue until every timestamp has a prompt.

Do not skip any timestamp. One timestamp = one prompt. Every prompt stays on its own line, with one blank line between prompts. Always output prompts in chronological timestamp order, and keep them in order across batches.

Only after the FINAL batch has been delivered - when every timestamp now has a prompt - end with exactly this line:

**All image prompts are now delivered - one for every timestamp in your script.**

Always include in every prompt: no text, no words, no letters, no numbers, no captions, no labels, no photorealism, no 3D render, no gradients, no drop shadows, no textures, no realistic faces, no anime style."""

_PROMPT_LINE_RE = re.compile(r"^\s*(\[\d+:\d{2}(?:\.\d)?\])\s*(.+)$")


def parse_prompt_batch(text):
    """Extract '[ts] prompt' lines from an assistant reply (inside or outside code fences)."""
    body = str(text or "")
    blocks = re.findall(r"```[a-zA-Z]*\n(.*?)```", body, re.S)
    source = "\n".join(blocks) if blocks else body
    out = []
    for raw in source.splitlines():
        m = _PROMPT_LINE_RE.match(raw.strip())
        if m:
            out.append({"timestamp": m.group(1), "prompt": m.group(2).strip()})
    return out


def generate_image_prompts(lines, reasoning_model=None, status_cb=None, cancel_event=None,
                           checkpoint_path=None, mascot=False, aspect=None):
    """Transcript lines -> one doodle prompt per line, via the STAGE-3 conversation.
    The app itself replies "next" until every timestamp is covered. Mapping is POSITIONAL
    (prompt N belongs to line N) with a timestamp sanity check. Raises on shortfall."""
    if not os.environ.get("WAVESPEED_API_KEY"):
        raise LongformError("WAVESPEED_API_KEY missing - image prompts need the reasoning model.")
    model = str(reasoning_model or "anthropic/claude-opus-4.8")
    target_aspect = "9:16" if str(aspect or IMAGE_ASPECT).strip() == "9:16" else "16:9"
    aspect_text = lambda text: fit_aspect(text, target_aspect)
    transcript = "\n".join(f"{fmt_ts(l['start'])} {l['text']}" for l in lines)
    messages = [{"role": "system", "content": aspect_text(STAGE3_PROMPT)},
                {"role": "user", "content": transcript}]
    checkpoint = Path(checkpoint_path) if checkpoint_path else None
    prompts, resumed_checkpoint = [], False

    def save_checkpoint():
        if not checkpoint:
            return
        checkpoint.parent.mkdir(parents=True, exist_ok=True)
        payload = {"line_count": len(lines), "first_ts": fmt_ts(lines[0]["start"]),
                   "last_ts": fmt_ts(lines[-1]["start"]), "prompts": prompts[:len(lines)]}
        tmp = checkpoint.with_suffix(checkpoint.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(checkpoint)

    if checkpoint and checkpoint.is_file():
        try:
            saved = json.loads(checkpoint.read_text(encoding="utf-8"))
            if (int(saved.get("line_count") or 0) == len(lines)
                    and saved.get("first_ts") == fmt_ts(lines[0]["start"])
                    and saved.get("last_ts") == fmt_ts(lines[-1]["start"])
                    and isinstance(saved.get("prompts"), list)):
                prompts = [p for p in saved["prompts"] if isinstance(p, dict)
                           and p.get("timestamp") and p.get("prompt")][:len(lines)]
                resumed_checkpoint = bool(prompts)
                if resumed_checkpoint:
                    _log(status_cb, f"Image prompts: resumed {len(prompts)}/{len(lines)} from "
                                    "the saved batch checkpoint.")
                    # The saved conversation is gone, so replaying "next" would make the model
                    # start again at line 1 and duplicate what the checkpoint already holds.
                    # Point it at the lines that are still MISSING instead - same STAGE-3 rules,
                    # just a shorter transcript.
                    if len(prompts) < len(lines):
                        messages = [
                            {"role": "system", "content": aspect_text(STAGE3_PROMPT)},
                            {"role": "user", "content": "\n".join(
                                f"{fmt_ts(l['start'])} {l['text']}"
                                for l in lines[len(prompts):])},
                        ]
        except Exception as exc:  # noqa: BLE001
            _log(status_cb, f"Image prompt checkpoint was unreadable; starting fresh ({exc}).")
    max_rounds = (len(lines) // 20) + 4
    # Skip the model only when the checkpoint is COMPLETE. The condition used to be "resumed at
    # all", so resuming a PARTIAL checkpoint got zero rounds and fell straight through to
    # missing-slot recovery (60 lines at most) and then the deterministic emergency template for
    # everything left - a project interrupted at 40 of 304 prompts could never be finished, and
    # two thirds of its art came from that template. A complete checkpoint must still cost
    # nothing, which is why this tests the count rather than the flag.
    for round_no in range(0 if len(prompts) >= len(lines) else max_rounds):
        if cancel_event is not None and cancel_event.is_set():
            raise pipeline.PipelineCancelled("Cancelled.")
        _log(status_cb, f"Image prompts: batch {round_no + 1} from {model} "
                        f"({len(prompts)}/{len(lines)} so far)...")
        data = agent_core.post_json_url(agent_core.WAVESPEED_LLM_API, {
            "model": model, "messages": messages, "temperature": 0.6, "max_tokens": 8000,
        }, timeout=600)
        reply = data["choices"][0]["message"]["content"]
        batch = parse_prompt_batch(reply)
        if not batch:
            _log(status_cb, f"Prompt batch {round_no + 1} contained no parseable prompts; "
                            "switching to missing-slot recovery.")
            break
        prompts.extend(batch)
        save_checkpoint()
        messages.append({"role": "assistant", "content": reply})
        if len(prompts) >= len(lines) or "all image prompts are now delivered" in reply.lower():
            break
        messages.append({"role": "user", "content": "next"})
    # A model sometimes announces "all delivered" two or three items early. Recover ONLY the
    # missing tail in small exact batches instead of throwing away a 20-30 minute conversation.
    recovery_round = 0
    while len(prompts) < len(lines) and recovery_round < 3:
        if cancel_event is not None and cancel_event.is_set():
            raise pipeline.PipelineCancelled("Cancelled.")
        missing = lines[len(prompts):min(len(lines), len(prompts) + 20)]
        exact = "\n".join(f"{fmt_ts(line['start'])} {line['text']}" for line in missing)
        _log(status_cb, f"Image prompts: recovering {len(missing)} missing slot(s) "
                        f"({len(prompts)}/{len(lines)} saved)...")
        try:
            data = agent_core.post_json_url(agent_core.WAVESPEED_LLM_API, {
                "model": model,
                "messages": [
                    {"role": "system", "content":
                        aspect_text("Generate exactly one 16:9 image prompt for every "
                                   "supplied timestamp. ") +
                        "Output only lines in the form '[m:ss.s] prompt', chronological, no fence, "
                        "no commentary. Every prompt must request a concrete hand-drawn 2D doodle "
                        "scene with flat colors, bold black outlines and simple stick figures, "
                        "containing NO written language of any kind, and explicitly: no text, no "
                        "words, no letters, no numbers, no captions, no labels, no photorealism, "
                        "no 3D, no gradients, no shadows, no textures, no anime."},
                    {"role": "user", "content": exact}],
                "temperature": 0.35, "max_tokens": 5000,
            }, timeout=300)
            recovered = parse_prompt_batch(data["choices"][0]["message"]["content"])
        except Exception as exc:  # noqa: BLE001
            recovered = []
            _log(status_cb, f"Missing-slot recovery attempt failed ({exc}).")
        if recovered:
            prompts.extend(recovered[:len(missing)])
            save_checkpoint()
        recovery_round += 1
    # Last-resort deterministic prompts keep the project renderable even if the LLM repeatedly
    # omits a line. They remain tied to the exact narration rather than duplicating random art.
    while len(prompts) < len(lines):
        idx = len(prompts)
        line = lines[idx]
        prompt = ("Hand-drawn 2D doodle cartoon animation, flat colors, bold black outlines, "
                  "slightly imperfect marker lines, one concrete visual metaphor for the narration "
                  f"\"{str(line.get('text') or '')[:300]}\", simple expressive stick figures and "
                  + aspect_text("one clear focal object, plain background, clean 16:9 "
                               "composition, " + NO_TEXT_LOCK))
        prompts.append({"timestamp": fmt_ts(line["start"]), "prompt": prompt})
        _log(status_cb, f"Image prompts: built a safe local fallback for slot {idx + 1}.")
        save_checkpoint()
    prompts = prompts[:len(lines)]
    strip_text_from_prompts(prompts, status_cb=status_cb, aspect=target_aspect)
    mismatch = sum(1 for l, p in zip(lines, prompts)
                   if p["timestamp"] != fmt_ts(l["start"]))
    if mismatch or len(prompts) != len(lines):
        _log(status_cb, f"Image prompt timestamps differ from the transcript "
                        f"({mismatch} mismatched, {len(prompts)}/{len(lines)} prompts); "
                        "reusing existing images without generating replacements.")
    if mascot:
        prompts = add_mascot(prompts)
        _log(status_cb, f"Mascot: {MASCOT_NAME} hidden in all {len(prompts)} image prompts.")
    _log(status_cb, f"All {len(prompts)} image prompts delivered.")
    return prompts


def write_prompts_file(prompts, out_path):
    out_path = Path(out_path)
    out_path.write_text("\n\n".join(f"{p['timestamp']} {p['prompt']}" for p in prompts) + "\n",
                        encoding="utf-8")
    return out_path


# ------------------------------------------------------------------ 4) IMAGES (Ideogram)

CHARACTER_REFERENCE_PROMPT = (
    "Flat solid-color hand-drawn webcomic illustration, crude MS Paint look, no gradients, no realistic lighting, stick figures with large round white heads and thin straight black limbs, thick uniform black outlines of one constant weight on everything including props and background shapes, oversized simple black dot eyes with exaggerated expressions, thin curved eyebrows, simple line mouths, a single stick figure standing in a neutral relaxed pose, arms straight down at "
    "its sides, large round white head with two big black dot eyes and a small straight line "
    "mouth, centered on a plain white background, full body visible, no other objects, "
    "flat single-color background, flat solid fill colors only, no gradients, no shading, no texture, no lighting, no text, no words, no letters, no numbers, no captions, no labels, no signage, no photorealism, no 3D, no realistic faces, no anime style, amateur webcomic aesthetic, deliberately unpolished, minimal detail, 16:9 aspect ratio.")


def image_key(index, line, duration):
    """Filename stem: index + timestamp + on-screen DURATION (user rule: length visible)."""
    ts = fmt_ts(line["start"]).replace(":", "-").replace(".", "-").strip("[]")
    return f"img{index:03d}_[{ts}]_dur{duration:.2f}s"


def resolve_image_for(image_dir, index, line, duration, aspect=None):
    """The image belonging to this line, tolerating a changed CUT LENGTH.

    image_key bakes the on-screen duration into the filename so the length is visible in the
    folder. That makes the name change whenever a cut is retimed, and the assembly then reports
    a scene it can plainly see on disk as missing: one project had 228 images, 227 names matching
    and the first at dur4.60s against an expected dur4.80s - the picture was fine, the beat had
    simply been retimed by two tenths after it was drawn.

    Index and TIMESTAMP identify the line; the duration does not. A file whose timestamp differs
    belongs to a different line and is deliberately NOT reused - that is the mismatch guard the
    exact key was there to provide, and it stays.
    """
    image_dir = Path(image_dir)
    target_aspect = "9:16" if str(aspect or IMAGE_ASPECT).strip() == "9:16" else "16:9"
    exact = image_dir / f"{image_key(index, line, duration)}.png"
    if _image_done(exact, target_aspect):
        return exact
    stamp = fmt_ts(line["start"]).replace(":", "-").replace(".", "-").strip("[]")
    prefix = f"img{index:03d}_[{stamp}]_dur"
    for candidate in sorted(image_dir.glob(f"img{index:03d}_*.png")):
        if candidate.name.startswith(prefix) and _image_done(candidate, target_aspect):
            return candidate
    return None



# The tail every prompt must carry. Ideogram is a TEXT-STRONG model - it will happily render
# any word it finds in the prompt - so the ban has to be stated as explicitly as the style.
NO_TEXT_LOCK = ("no text, no words, no letters, no numbers, no captions, no labels, no signage, "
                "no gradients, no shadows, no textures, no photorealism, no 3D, no realistic "
                "faces, no anime style, 16:9 aspect ratio, educational YouTube explainer "
                "doodle style.")

# Phrasings a model reaches for when it wants words on the frame. Each is cut out of the prompt
# rather than trusted to the style lock, because a positive instruction ("text reading X") beats
# a negative one ("no text") in every image model.
_TEXT_INSTRUCTION_RES = (
    # ...text at the top reading "WORD",   ...label reading "WORD",   ...sign saying "WORD"
    re.compile(r",?\s*[^,]*\b(?:text|caption|title|label|lettering|sign|signage|word|words|"
               r"headline|banner)\b[^,]*?\b(?:reading|saying|says|that reads|spelling)\b\s*"
               r"[\"“‘'][^\"”’']{0,80}[\"”’']", re.I),
    # bare "bold ALL CAPS text at the top" with no quoted string
    re.compile(r",?\s*[^,]*\b(?:all[- ]caps|bold black)\b[^,]*\b(?:text|caption|lettering)\b"
               r"[^,]*", re.I),
    # a leftover clause that only announces text
    re.compile(r",?\s*[^,]*\b(?:on-screen|onscreen)\s+(?:text|caption|words)\b[^,]*", re.I),
)


def strip_text_from_prompts(prompts, status_cb=None, aspect=None):
    """Guarantee the NO-TEXT contract mechanically (user 2026-08-16: "keine texte oder captions
    mehr in den images").

    The STAGE-3 spec forbids written language, but a model that spent a hundred prompts writing
    `text at the top reading "X"` drifts back into it - and one such clause outweighs the whole
    negative style lock. So every caption phrasing is CUT from the prompt here, and the lock is
    appended if it is missing. Mutates `prompts` in place; returns how many were repaired."""
    stripped = locked = 0
    for p in prompts:
        if not isinstance(p, dict):
            continue
        text = str(p.get("prompt") or "").strip()
        if not text:
            continue
        cleaned = text
        for pattern in _TEXT_INSTRUCTION_RES:
            cleaned = pattern.sub("", cleaned)
        cleaned = re.sub(r"\s{2,}", " ", cleaned).replace(" ,", ",").strip(" ,.")
        if cleaned != text.strip(" ,."):
            stripped += 1
        if "no text" not in cleaned.lower():
            cleaned = cleaned.rstrip(" ,.") + ", " + fit_aspect(NO_TEXT_LOCK, aspect)
            locked += 1
        else:
            cleaned = cleaned.rstrip(" ,") + ("" if cleaned.endswith(".") else ".")
        p["prompt"] = cleaned
    if stripped or locked:
        _log(status_cb, f"No-text contract enforced: {stripped} prompt(s) still asked for "
                        f"on-screen words (removed), {locked} were missing the no-text lock.")
    return stripped + locked


def caption_cut_starts(lines, prompts, audio_duration, lead=0.0):
    """Cut times: frame i appears when its narration line starts.

    This used to hunt for the moment the frame's CAPTION PHRASE was spoken, because a caption
    naming a word from the middle of a sentence ("...almost nothing. WHY?") would otherwise
    appear seconds early. The frames carry no words any more, so there is no phrase to sync to
    and the sentence start is the honest cut point. Cuts are forced monotonic and the first is
    pinned to 0 so the video never opens on black."""
    cuts = []
    for line in lines:
        cut = float(line["start"]) - float(lead or 0.0)
        if cuts:
            cut = max(cut, cuts[-1] + 0.05)
        cuts.append(max(0.0, min(cut, float(audio_duration or 0.0) or cut)))
    if cuts:
        cuts[0] = 0.0
    return cuts


def caption_cut_durations(lines, prompts, audio_duration):
    """Per-frame on-screen durations derived from the caption-synced cuts."""
    cuts = caption_cut_starts(lines, prompts, audio_duration)
    durs = []
    for i, c in enumerate(cuts):
        nxt = cuts[i + 1] if i + 1 < len(cuts) else max(audio_duration, c + 0.4)
        durs.append(max(0.35, round(nxt - c, 3)))
    return durs


def ensure_line_words(project_dir, status_cb=None):
    """Retrofit per-word timings onto a state whose lines predate the words field (needed by the
    caption-synced cuts). Re-transcribes the recorded voiceover and maps the words onto the
    EXISTING lines by word count - the stored start/end stay untouched."""
    import voice_align
    project_dir = Path(project_dir)
    try:
        state = json.loads((project_dir / STATE_FILE).read_text(encoding="utf-8"))
    except Exception:
        return False
    lines = state.get("lines") or []
    if not lines or all(l.get("words") for l in lines):
        return bool(lines)
    if not voice_align.available():
        _log(status_cb, "faster-whisper not available - keeping sentence-start cuts.")
        return False
    voice = project_dir / Path(str(state.get("voiceover_file") or "voiceover.wav")).name
    if not voice.is_file():
        return False
    _log(status_cb, "Computing per-word timings for caption-synced cuts (one-time)...")
    asr = voice_align.transcribe_words(voice, status_cb=status_cb)
    aligned = voice_align.align_script_to_words(str(state.get("script") or ""), asr)
    wi = 0
    for l in lines:
        n = len(str(l.get("text") or "").split())
        chunk = aligned[wi:wi + n]
        wi += n
        l["words"] = [{"w": str(w.get("word") or ""), "s": round(float(w["start"]), 2)}
                      for w in chunk]
    state["lines"] = lines
    tmp = (project_dir / STATE_FILE).with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(project_dir / STATE_FILE)
    _log(status_cb, "Per-word timings saved.")
    return True


def _archive_old_images(images_dir, status_cb=None):
    """Move existing frames aside (not delete) so a format change regenerates them from scratch.
    Nothing is lost - the old PNGs live on under images/_old_format_<ts>/ if ever needed."""
    images_dir = Path(images_dir)
    if not images_dir.is_dir():
        return 0
    stale = [p for p in images_dir.glob("*.png")]
    if not stale:
        return 0
    dest = images_dir / f"_old_format_{time.strftime('%Y%m%d_%H%M%S')}"
    dest.mkdir(parents=True, exist_ok=True)
    moved = 0
    for p in stale:
        try:
            p.replace(dest / p.name)
            moved += 1
        except Exception:
            pass
    if moved:
        _log(status_cb, f"Moved {moved} old-format frame(s) to {dest.name}/ - they will be "
                        "regenerated in the new caption style.")
    return moved


def _archive_images_from_index(images_dir, start_index, status_cb=None):
    """Archive only frames at/after a broken prompt slot so valid earlier frames can be reused."""
    images_dir = Path(images_dir)
    stale = list(images_dir.glob("*.png"))
    selected = []
    for path in stale:
        match = re.match(r"img(\d{3})_", path.name)
        if match and int(match.group(1)) >= int(start_index):
            selected.append(path)
    if not selected:
        return 0
    dest = images_dir / f"_prompt_repair_{time.strftime('%Y%m%d_%H%M%S')}"
    dest.mkdir(parents=True, exist_ok=True)
    moved = 0
    for path in selected:
        try:
            path.replace(dest / path.name)
            moved += 1
        except OSError:
            pass
    if moved:
        _log(status_cb, f"Moved {moved} frame(s) from broken prompt slot #{start_index + 1} onward "
                        f"to {dest.name}/ for regeneration.")
    return moved


def line_durations(lines, audio_duration):
    """Per-line on-screen duration: to the next line's start; last line holds to audio end."""
    durs = []
    for i, l in enumerate(lines):
        if i + 1 < len(lines):
            durs.append(max(0.4, round(lines[i + 1]["start"] - l["start"], 2)))
        else:
            durs.append(max(0.4, round(max(audio_duration, l["end"]) - l["start"], 2)))
    return durs


def speech_cut_durations(lines, audio_duration):
    """Return a gapless edit clock anchored to the spoken start of every line.

    Image filenames contain historical timestamps and prompt captions may name a word from the
    middle of a sentence.  Neither is a reliable edit point.  The local forced-alignment result
    in ``line['start']`` is: frame 0 opens at video time zero, every following frame switches
    when its corresponding narration line actually starts, and the final frame holds exactly to
    the end of the voiceover.  This produces one monotonic timeline without overlaps or holes.
    """
    if not lines:
        return []
    audio_end = max(0.0, float(audio_duration or 0.0))
    cuts = [0.0]
    for line in lines[1:]:
        cuts.append(max(cuts[-1] + 0.04, min(audio_end, float(line.get("start") or 0.0))))
    durations = []
    for idx, cut in enumerate(cuts):
        nxt = cuts[idx + 1] if idx + 1 < len(cuts) else max(audio_end, cut + 0.04)
        durations.append(max(0.04, round(nxt - cut, 3)))
    return durations


def saved_project_cut_durations(state, lines, audio_duration):
    """Return the edit clock selected by an already-saved longform project.

    Legacy projects retain caption-triggered cuts.  A project explicitly repaired to the speech
    clock must keep that choice when reopening or rendering; otherwise a harmless rebuild would
    silently put the old, early caption cuts back.
    """
    repair = (state or {}).get("retime_range") or {}
    if isinstance(repair, dict) and repair.get("method") == "speech-clock":
        return speech_cut_durations(lines, audio_duration)
    return caption_cut_durations(lines, (state or {}).get("prompts") or [], audio_duration)


def rebuild_timeline_from_speech_clock(out_dir, status_cb=None):
    """Repair a saved longform timeline from the real narration clock, without any API work.

    Image names used to encode caption-triggered holds.  That makes both the filename duration
    and the edit point drift from the narration whenever a caption word occurs in the middle of
    a sentence.  The stored forced-alignment line starts are the authoritative clock: keep image
    *index* semantic, rename each existing frame to that clock, then rebuild a gapless manifest.
    """

    adopt_project_aspect(out_dir)
    out_dir = Path(out_dir)
    try:
        state = json.loads((out_dir / STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        state = {}
    lines = list(state.get("lines") or [])
    voice = voiceover_path_from_state(out_dir, state)
    if not lines:
        raise LongformError("Cannot retime: this project has no saved narration lines.")
    if not _audio_done(voice):
        raise LongformError("Cannot retime: the saved voiceover is missing.")

    audio_duration = audio_duration_seconds(voice)
    # A semantic range-retime may have redistributed ``line.start`` while keeping the
    # forced-alignment word clock intact.  For image cuts the first spoken word is the
    # authoritative boundary; restore it before calculating durations so a picture does
    # not appear during the preceding pause or sentence.
    restored = 0
    for line in lines:
        word_times = []
        for word in line.get("words") or []:
            try:
                word_times.append(float(word.get("s")))
            except (TypeError, ValueError, AttributeError):
                continue
        if word_times:
            spoken_start = round(min(word_times), 3)
            if abs(float(line.get("start") or 0.0) - spoken_start) > 0.001:
                restored += 1
            line["start"] = spoken_start
    for idx, line in enumerate(lines):
        next_start = (float(lines[idx + 1]["start"])
                      if idx + 1 < len(lines) else audio_duration)
        line["end"] = round(max(float(line["start"]) + 0.04, next_start), 3)
    durations = speech_cut_durations(lines, audio_duration)
    images_dir = out_dir / "images"
    staged = []
    renamed = 0
    if images_dir.is_dir():
        # Locate by index rather than timestamp: the timestamp in an old filename is precisely
        # what this repair replaces.  A two-phase move prevents collisions among adjacent files.
        for idx, (line, duration) in enumerate(zip(lines, durations)):
            destination = images_dir / f"{image_key(idx, line, duration)}.png"
            candidates = sorted(
                p for p in images_dir.glob(f"img{idx:03d}_*.png")
                if _image_done(p, IMAGE_ASPECT) and p != destination
            )
            if destination.is_file() or not candidates:
                continue
            source = candidates[0]
            temporary = images_dir / f".speech_clock_{idx:03d}_{time.time_ns():x}.png"
            os.replace(source, temporary)
            staged.append((temporary, destination))
        for temporary, destination in staged:
            os.replace(temporary, destination)
            renamed += 1

    results = {}
    for idx, (line, duration) in enumerate(zip(lines, durations)):
        candidate = images_dir / f"{image_key(idx, line, duration)}.png"
        if _image_done(candidate, IMAGE_ASPECT):
            results[idx] = str(candidate)
    prompts = list(state.get("prompts") or [])
    if prompts:
        for idx, row in enumerate(prompts):
            if idx < len(lines) and isinstance(row, dict):
                row["timestamp"] = fmt_ts(lines[idx]["start"])
        write_prompts_file(prompts, out_dir / f"image_prompts_{out_dir.name}.txt")
    save_state(out_dir, lines=lines, audio_duration=round(audio_duration, 3), prompts=prompts,
               retime_range={"start": 0, "end": len(lines) - 1,
                             "matched": len(results), "method": "speech-clock"})
    write_transcript(lines, out_dir / "transcript.txt")
    write_timeline_manifest(lines, durations, results, audio_duration,
                            out_dir / "timeline.json",
                            voice_speed=float(state.get("voice_speed") or 1.0))
    _log(status_cb, f"Speech-clock retime complete: {len(results)} frame(s), {renamed} renamed, "
                    f"{restored} cut(s) restored from aligned word starts.")
    return {"lines": len(lines), "images": len(results), "renamed": renamed,
            "duration": round(audio_duration, 3), "timeline": str(out_dir / "timeline.json")}


def retime_existing_range(out_dir, start_idx, end_idx, replacement_lines=None,
                          status_cb=None):
    """Locally match existing images and retime one inclusive line range.

    The range keeps its original wall-clock span, while line holds are redistributed by
    spoken-word count.  Images are assigned by token overlap between their saved prompt/
    narration and the target line (with the original slot as a stable tie-breaker).  No
    image generation, network call, or Higgsfield session is involved; lines and files
    outside the range are left byte-for-byte untouched.
    """

    adopt_project_aspect(out_dir)
    out_dir = Path(out_dir)
    state_path = out_dir / STATE_FILE
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise LongformError(f"Could not read longform state: {exc}") from exc
    lines = [dict(row) for row in (state.get("lines") or []) if isinstance(row, dict)]
    if not lines:
        raise LongformError("This project has no timed voiceover lines.")
    try:
        first, last = int(start_idx), int(end_idx)
    except (TypeError, ValueError) as exc:
        raise LongformError("Scene range must use numeric indexes.") from exc
    if first > last:
        first, last = last, first
    if first < 0 or last >= len(lines):
        raise LongformError(f"Scene range must be between 1 and {len(lines)}.")
    if last - first < 1:
        raise LongformError("Select at least two scenes to retime.")
    if not os.environ.get("WAVESPEED_API_KEY", "").strip():
        raise LongformError("Gemini retime requires WAVESPEED_API_KEY; no local fallback was run.")

    # Optional replacement lines are deliberately constrained to the selected range.  This
    # lets a local transcription/analysis caller provide refreshed voiceover text without
    # accidentally changing the rest of the project.
    incoming = replacement_lines if isinstance(replacement_lines, list) else []
    if incoming and len(incoming) != last - first + 1:
        raise LongformError("Replacement line count must match the selected scene range.")
    original = [dict(row) for row in lines]
    audio_duration = float(state.get("audio_duration") or 0.0)
    old_durations = line_durations(original, audio_duration)
    span_start = float(original[first].get("start") or 0.0)
    span_end = span_start + sum(old_durations[first:last + 1])
    if last + 1 < len(original):
        # The next line is outside the selection and is the authoritative right boundary.
        span_end = float(original[last + 1].get("start") or span_end)
    span = max(0.04 * (last - first + 1), span_end - span_start)

    def words(text):
        return set(re.findall(r"[a-z0-9']+", str(text or "").lower()))

    prompts = state.get("prompts") or []
    descriptors = []
    for idx in range(first, last + 1):
        prompt = prompts[idx] if idx < len(prompts) else {}
        prompt_text = prompt.get("prompt", "") if isinstance(prompt, dict) else prompt
        descriptors.append(str(prompt_text or "") + " " + str(original[idx].get("text") or ""))

    targets = []
    for offset, idx in enumerate(range(first, last + 1)):
        row = dict(incoming[offset]) if incoming else dict(original[idx])
        text = str(row.get("text") or row.get("script") or original[idx].get("text") or "")
        row["text"] = text
        targets.append(row)
    word_clocks = [
        [{"word": str(word.get("w") or ""), "time": float(word.get("s") or 0.0)}
         for word in (row.get("words") or [])]
        for row in targets
    ]
    weights = [max(1, len(words(row.get("text")))) for row in targets]
    minimum = 0.4
    if sum(weights) * minimum > span:
        durations = [span / len(weights)] * len(weights)
    else:
        extra = span - sum(weights) * minimum
        total = float(sum(weights))
        durations = [minimum + extra * weight / total for weight in weights]
    # Resolve current image paths by stable index, then assign them to target lines using a
    # deterministic local text match.  Missing images remain missing; they are never generated.
    image_dir = out_dir / "images"
    sources = []
    for idx in range(first, last + 1):
        path = image_dir / f"{image_key(idx, original[idx], old_durations[idx])}.png"
        if not _image_done(path, IMAGE_ASPECT):
            candidates = [p for p in image_dir.glob(f"img{idx:03d}_*.png") if _image_done(p, IMAGE_ASPECT)]
            path = candidates[0] if candidates else None
        sources.append(path)
        if any(path is None for path in sources):
            raise LongformError("Gemini vision retime requires an existing image for every selected scene.")
    target_tokens = [words(row.get("text")) for row in targets]
    descriptor_tokens = [words(value) for value in descriptors]
    remaining = set(range(len(sources)))
    assignment = {}
    gemini_order = [None] * len(sources)
    gemini_switches = [None] * len(sources)
    batch_size = 10
    voiceover_path = voiceover_path_from_state(out_dir, state)
    voiceover_url = None
    if _audio_done(voiceover_path):
        _log(status_cb, "Uploading voiceover for Gemini retime...")
        upload_error = None
        for attempt in range(3):
            try:
                voiceover_url, _ = pipeline.upload_media(
                    voiceover_path, os.environ["WAVESPEED_API_KEY"])
                upload_error = None
                break
            except (OSError, urllib.error.URLError) as exc:
                upload_error = exc
                if attempt < 2:
                    time.sleep(2 ** attempt)
        if upload_error is not None:
            # Word clocks were aligned against this exact immutable voiceover. The media upload is
            # supplementary audio context, never a reason to prevent the vision retime from running.
            _log(status_cb, "Voiceover upload failed after 3 attempts; Gemini will use the exact "
                            "voiceover transcript and aligned word times instead.")
    for batch_start in range(0, len(sources), batch_size):
        batch_end = min(len(sources), batch_start + batch_size)
        try:
            batch_sheet = out_dir / "review" / f".retime_batch_{batch_start:04d}.jpg"
            batch_sheet = agent_core.create_media_contact_sheet(
                sources[batch_start:batch_end], batch_sheet,
                title=f"Existing visuals {batch_start + 1}-{batch_end}")
            if batch_sheet is None:
                raise ValueError("Could not create a vision contact sheet.")
            payload = {
                "model": "google/gemini-3.1-pro-preview",
                "messages": [
                    {"role": "system", "content":
                     "You are a vision editor. Ignore every filename, index, prompt, and other "
                     "metadata. Judge only the supplied image pixels and the voiceover. Return only "
                     "JSON with exactly one match for every target index and every source index. "
                     "Match each source image to one target and choose the exact absolute spoken "
                     "time of its visible caption using the supplied voiceover word times; do not "
                     "invent times. Schema: "
                     "{\"matches\":[{\"target\":0,\"source\":0,\"spoken_at\":12.34}]}."},
                    {"role": "user", "content": [
                        {"type": "text", "text": json.dumps({
                            "targets": [row.get("text", "") for row in targets[batch_start:batch_end]],
                            "word_times": word_clocks[batch_start:batch_end],
                            "instruction": "The contact sheet follows. Images are ordered left-to-right, "
                            "top-to-bottom. Ignore all labels and metadata; use only the pixels.",
                        }, ensure_ascii=False)}
                    ]},
                ],
                "temperature": 0.0,
                "max_tokens": 1200,
                "response_format": {"type": "json_object"},
            }
            content = payload["messages"][1]["content"]
            content.append({
                "type": "image_url",
                "image_url": {"url": agent_core.image_data_url(batch_sheet)},
            })
            if voiceover_url:
                content.append({
                    "type": "text",
                    "text": "Use this voiceover audio as the timing reference:",
                })
                content.append({
                    "type": "audio_url",
                    "audio_url": {"url": voiceover_url},
                })
            _log(status_cb, f"Calling Gemini 3.1 Pro for retime batch "
                            f"{batch_start + 1}-{batch_end}/{len(sources)}...")
            request_error = None
            for attempt in range(2):
                try:
                    response = agent_core.post_json_url(
                        agent_core.WAVESPEED_LLM_API, payload, timeout=120)
                    request_error = None
                    break
                except (OSError, urllib.error.URLError) as exc:
                    request_error = exc
                    if attempt < 2:
                        time.sleep(2 ** attempt)
            if request_error is not None:
                raise request_error
            parsed = agent_core.extract_json_object(
                response["choices"][0]["message"]["content"]) or {}
            matches = parsed.get("matches")
            if not isinstance(matches, list) or len(matches) != batch_end - batch_start:
                correction = dict(payload)
                correction["messages"] = list(payload["messages"]) + [{
                    "role": "user",
                    "content": (
                        f"Your last answer contained {len(matches) if isinstance(matches, list) else 0} "
                        f"matches. Return the complete JSON now: exactly {batch_end - batch_start} "
                        "matches, with target and source each containing every index exactly once."
                    ),
                }]
                response = agent_core.post_json_url(
                    agent_core.WAVESPEED_LLM_API, correction, timeout=120)
                parsed = agent_core.extract_json_object(
                    response["choices"][0]["message"]["content"]) or {}
                matches = parsed.get("matches")
                if not isinstance(matches, list) or len(matches) != batch_end - batch_start:
                    raise ValueError(
                        "Gemini returned an incomplete retime batch after correction.")
            local_targets = [int(item["target"]) for item in matches]
            local_sources = [int(item["source"]) for item in matches]
            if (sorted(local_targets) != list(range(batch_end - batch_start))
                    or sorted(local_sources) != list(range(batch_end - batch_start))):
                raise ValueError("Gemini returned a non-bijective retime batch.")
            for item in matches:
                target = batch_start + int(item["target"])
                gemini_order[target] = batch_start + int(item["source"])
                gemini_switches[target] = float(item["spoken_at"])
            _log(status_cb, f"Gemini 3.1 Pro retime batch "
                            f"{batch_start + 1}-{batch_end}/{len(sources)} complete.")
        except (KeyError, TypeError, ValueError, OSError) as exc:
            _log(status_cb, f"Gemini 3.1 Pro batch {batch_start + 1}-{batch_end} unavailable; "
                            f"retime aborted ({exc}).")
            raise LongformError(
                f"Gemini 3.1 Pro retime batch {batch_start + 1}-{batch_end} failed: {exc}"
            ) from exc
    if not all(value is not None for value in gemini_order + gemini_switches):
        gemini_order = None
        gemini_switches = None
    if gemini_switches:
        # Keep the selected range's outer boundary fixed while using Gemini's spoken caption
        # switches for every subsequent visual. The first selected image owns the incoming hold.
        switches = [span_start] + [
            max(span_start, min(span_end, value)) for value in gemini_switches[1:]
        ]
        switches = [max(switches[i], switches[i - 1] + 0.04)
                    for i in range(len(switches))]
        durations = [
            max(0.04, (switches[i + 1] if i + 1 < len(switches) else span_end) - switches[i])
            for i in range(len(switches))
        ]
    cursor = span_start
    for row, duration in zip(targets, durations):
        row["start"] = round(cursor, 3)
        row["end"] = round(cursor + duration, 3)
        cursor += duration
    targets[-1]["end"] = round(span_end, 3)
    for idx, row in zip(range(first, last + 1), targets):
        lines[idx] = row
    for target_pos, target_set in enumerate(target_tokens):
        best = None
        best_score = -1.0
        for source_pos in remaining:
            source_set = descriptor_tokens[source_pos]
            overlap = (len(target_set & source_set) / max(1, len(target_set | source_set))
                       if target_set and source_set else 0.0)
            score = overlap + (0.001 if source_pos == target_pos else 0.0)
            if gemini_order is not None:
                score += 1.0 if source_pos == gemini_order[target_pos] else 0.0
            if score > best_score:
                best, best_score = source_pos, score
        if best is not None:
            assignment[target_pos] = best
            remaining.remove(best)

    staged = []
    try:
        for target_pos, source_pos in assignment.items():
            source = sources[source_pos]
            if source is None:
                continue
            target_idx = first + target_pos
            destination = image_dir / f"{image_key(target_idx, lines[target_idx], line_durations(lines, audio_duration)[target_idx])}.png"
            temp = image_dir / f".retime_range_{time.time_ns()}_{target_pos}.png"
            os.replace(source, temp)
            staged.append((temp, destination))
        for temp, destination in staged:
            os.replace(temp, destination)
    except OSError as exc:
        for temp, _destination in staged:
            if temp.exists():
                try:
                    temp.unlink()
                except OSError:
                    pass
        raise LongformError(f"Could not retime existing images: {exc}") from exc

    state["lines"] = lines
    state["retime_range"] = {"start": first, "end": last, "matched": len(staged),
                             "method": "local-text-overlap"}
    save_state(out_dir, **state)
    write_transcript(lines, out_dir / "transcript.txt")
    current_durations = line_durations(lines, audio_duration)
    current_results = {}
    for idx, line in enumerate(lines):
        candidate = out_dir / "images" / f"{image_key(idx, line, current_durations[idx])}.png"
        if candidate.is_file():
            current_results[idx] = str(candidate)
    write_timeline_manifest(lines, current_durations, current_results, audio_duration,
                            out_dir / "timeline.json",
                            voice_speed=state.get("voice_speed") or 1.0)
    _log(status_cb, f"Retimed scenes {first + 1}-{last + 1}: matched {len(staged)} existing image(s).")
    return {"start": first, "end": last, "matched": len(staged),
            "lines": lines, "method": "local-text-overlap"}


def retime_longform_assets(out_dir, old_lines, new_lines, old_audio_duration,
                           new_audio_duration, prompts=None, status_cb=None):
    """Move already-generated images onto a changed narration clock.

    Images are semantic and positional: image N still illustrates script line N when only the
    voice speed changes.  Their timestamp/duration filenames, however, must follow the NEW audio
    clock or resume would regenerate them and assembly could place stale durations on the cut.
    Rename in two phases so two new names can never collide with an old one mid-migration.
    """
    old_lines = list(old_lines or [])
    new_lines = list(new_lines or [])
    if not old_lines or len(old_lines) != len(new_lines):
        return list(prompts or []) if prompts is not None else None
    old_durations = line_durations(old_lines, old_audio_duration)
    new_durations = line_durations(new_lines, new_audio_duration)
    images_dir = Path(out_dir) / "images"
    staged = []
    if images_dir.exists():
        for idx in range(len(new_lines)):
            old_path = images_dir / f"{image_key(idx, old_lines[idx], old_durations[idx])}.png"
            new_path = images_dir / f"{image_key(idx, new_lines[idx], new_durations[idx])}.png"
            if old_path == new_path or _image_done(new_path):
                continue
            source = old_path if _image_done(old_path) else None
            if source is None:
                candidates = [p for p in images_dir.glob(f"img{idx:03d}_*.png")
                              if _image_done(p) and p != new_path]
                if len(candidates) == 1:
                    source = candidates[0]
            if source is None:
                continue
            temp = images_dir / f".retime_{idx:03d}_{time.time_ns():x}.png"
            try:
                os.replace(source, temp)
                staged.append((temp, new_path))
            except OSError as exc:
                _log(status_cb, f"Could not stage image #{idx + 1} for retiming ({exc}).")
        moved = 0
        for temp, destination in staged:
            try:
                os.replace(temp, destination)
                moved += 1
            except OSError as exc:
                _log(status_cb, f"Could not rename {temp.name} to its new timing ({exc}).")
        if moved:
            _log(status_cb, f"Voice speed changed: renamed and retimed {moved} existing image(s).")

    if prompts is None:
        return None
    updated = []
    for idx, prompt in enumerate(prompts):
        row = dict(prompt) if isinstance(prompt, dict) else {"prompt": str(prompt or "")}
        if idx < len(new_lines):
            row["timestamp"] = fmt_ts(new_lines[idx]["start"])
        updated.append(row)
    return updated


def write_timeline_manifest(lines, durations, results, audio_duration, out_path,
                            voice_speed=1.0):
    """Persist the exact image placement used by assembly and later timeline/resume tooling."""
    out_path = Path(out_path)
    rows = []
    cursor = 0.0
    for idx, (line, duration) in enumerate(zip(lines, durations)):
        image = results.get(idx) if isinstance(results, dict) else None
        rows.append({
            "index": idx,
            "start": round(cursor, 3),
            "end": round(cursor + float(duration), 3),
            "duration": round(float(duration), 3),
            "image": Path(image).name if image else None,
            "text": str(line.get("text") or ""),
        })
        cursor += float(duration)
    payload = {"audio_duration": round(float(audio_duration or 0.0), 3),
               "voice_speed": round(float(voice_speed or 1.0), 3),
               "scenes": rows}
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, out_path)
    return out_path


def _normalize_image_aspect(path, expected_aspect, status_cb=None):
    """Repair an unexpected provider ratio locally, so the editor never reports real art missing."""
    path = Path(path)
    target = "9:16" if str(expected_aspect).strip() == "9:16" else "16:9"
    if _image_aspect_ok(path, target):
        return True
    try:
        target_w, target_h = (1080, 1920) if target == "9:16" else (1920, 1080)
        target_ratio = target_w / target_h
        with Image.open(path) as source:
            image = source.convert("RGBA")
            width, height = image.size
            if width < 2 or height < 2:
                return False
            current_ratio = width / height
            if current_ratio > target_ratio:
                crop_w = max(1, round(height * target_ratio))
                left = max(0, (width - crop_w) // 2)
                crop = image.crop((left, 0, left + crop_w, height))
            else:
                crop_h = max(1, round(width / target_ratio))
                top = max(0, (height - crop_h) // 2)
                crop = image.crop((0, top, width, top + crop_h))
            repaired = crop.resize((target_w, target_h), Image.Resampling.LANCZOS)
            temp = path.with_name(f".{path.stem}.aspectfix_{time.time_ns():x}{path.suffix}")
            repaired.save(temp, format="PNG")
        os.replace(temp, path)
        ok = _image_aspect_ok(path, target)
        if ok:
            _log(status_cb, f"image {path.name}: provider returned the wrong ratio; repaired to {target} locally.")
        return ok
    except (OSError, ValueError) as exc:
        _log(status_cb, f"Could not repair the aspect ratio of {path.name}: {exc}")
        return False


def _p_image_one(index, prompt, dest, key, cancel_event=None, status_cb=None, aspect=None):
    """One Ideogram generation: submit, poll, download. Returns the path or None.

    Each call owns its own prediction id, so a slow generation can never deliver onto a
    later prompt's slot - the failure mode that made the browser route need an OCR audit.
    """
    target_aspect = "9:16" if str(aspect or IMAGE_ASPECT).strip() == "9:16" else "16:9"
    payload = {
        "prompt": str(prompt or "").strip(),
        "aspect_ratio": target_aspect,
        "resolution": IMAGE_RESOLUTION,
        "thinking": IMAGE_THINKING,
        "output_format": IMAGE_OUTPUT_FORMAT,
    }
    response = pipeline.request_json("POST", f"{pipeline.API_BASE}/{P_IMAGE_MODEL}",
                                     key, payload, timeout=120)
    prediction_id = pipeline.unwrap_id(response)
    outputs, _ = pipeline.poll_wavespeed(prediction_id, key, timeout_s=IMAGE_TIMEOUT_S,
                                         interval_s=2, cancel_event=cancel_event,
                                         label=f"image #{index + 1}")
    if not outputs:
        return None
    pipeline.download_file(outputs[0], Path(dest))
    return str(dest) if _normalize_image_aspect(dest, target_aspect, status_cb=status_cb) else None


def generate_images(prompts, lines, durations, out_dir, status_cb=None, cancel_event=None,
                    mascot=False, aspect=None):
    """Ideogram (P-Image) 16:9 frames over the WaveSpeed API.

    IMAGE_CONCURRENCY generations run in parallel; a failed one is retried up to
    IMAGE_RETRIES times. Images already on disk are reused, so a resumed run only pays for
    what is missing.

    Returns {index: path|None}."""
    import concurrent.futures
    key = os.environ.get("WAVESPEED_API_KEY", "")
    if not key:
        raise LongformError("WAVESPEED_API_KEY missing - the image stage needs it.")
    out_dir = Path(out_dir)
    target_aspect = "9:16" if str(aspect or IMAGE_ASPECT).strip() == "9:16" else "16:9"
    out_dir.mkdir(parents=True, exist_ok=True)
    # The mascot travels as WORDS only (see MASCOT_RULE, applied in generate_image_prompts).
    # The old route could pin the actual artwork as a reference image; this endpoint takes
    # text alone, so Blob is only as consistent as its description.
    if mascot:
        _log(status_cb, "Mascot: described in the prompt text (this model takes no reference "
                        "image, so expect more drift than the pinned-artwork route).")

    cancel_check = (lambda: cancel_event is not None and cancel_event.is_set())
    total = len(prompts)
    results = {}
    attempts = {}
    # RESUME: the filename encodes index + timestamp + duration, so a reused file always
    # belongs to the line it is mapped onto; if the script or its timing changed, the key
    # changes and the frame is regenerated instead of silently mismatched.
    #
    # That covers timing, but NOT the prompt text. A rerun that regenerates the prompts against
    # the same script and the same timings produces new wording under identical filenames, and
    # every old frame was silently kept: a delivered short had 16 of 17 pictures drawn from
    # prompts that no longer existed - one asked for a close-up on deep purple and the frame on
    # screen was a figure in a bed on white. So the prompt each frame was drawn from is recorded
    # and compared. A project from before this carries no record and keeps its frames.
    saved_prints = {}
    try:
        saved_prints = dict((json.loads((out_dir / STATE_FILE).read_text(encoding="utf-8"))
                             or {}).get("image_prompt_prints") or {})
    except (OSError, ValueError):
        saved_prints = {}
    prints = {}
    stale = 0
    for idx in range(total):
        text = prompts[idx].get("prompt") if isinstance(prompts[idx], dict) else prompts[idx]
        prints[str(idx)] = hashlib.sha1(str(text or "").encode("utf-8")).hexdigest()[:12]
    for idx in range(total):
        existing = resolve_image_for(out_dir, idx, lines[idx], durations[idx], target_aspect)
        if existing is None:
            continue
        was = saved_prints.get(str(idx))
        if was and was != prints[str(idx)]:
            stale += 1
            continue
        results[idx] = str(existing)
    if stale:
        _log(status_cb, f"{stale} frame(s) were drawn from a prompt that has since changed; "
                        "they will be drawn again.")
    if results:
        _log(status_cb, f"Resume: {len(results)}/{total} image(s) already generated - "
                        f"only the missing {total - len(results)} will be generated.")
    queue = [i for i in range(total) if i not in results]
    dead = 0                            # failed attempts so far
    fresh_ok = 0                        # successes THIS session - resume pre-fills results,
    #                                     and judging the provider by yesterday's images
    #                                     would disable the dead-provider stop exactly when
    #                                     a key has expired
    done_count = {"n": sum(1 for v in results.values() if v)}

    while queue:
        if cancel_check():
            raise pipeline.PipelineCancelled("Cancelled.")
        _log(status_cb, f"Generating {len(queue)} image(s), up to {IMAGE_CONCURRENCY} at a time...")
        round_results = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=IMAGE_CONCURRENCY) as pool:
            futures = {}
            for idx in queue:
                dest = out_dir / f"{image_key(idx, lines[idx], durations[idx])}.png"
                futures[pool.submit(_p_image_one, idx, prompts[idx]["prompt"], dest, key,
                                    cancel_event=cancel_event, status_cb=status_cb,
                                    aspect=target_aspect)] = idx
            for fut in concurrent.futures.as_completed(futures):
                idx = futures[fut]
                try:
                    path = fut.result()
                except pipeline.PipelineCancelled:
                    raise
                except Exception as exc:  # noqa: BLE001
                    path = None
                    _log(status_cb, f"image #{idx + 1} failed: {str(exc)[:160]}")
                if path and _image_done(path, target_aspect):
                    round_results[idx] = path
                    fresh_ok += 1
                    done_count["n"] += 1
                    _log(status_cb, f"image {done_count['n']}/{total} - #{idx + 1} done")
                else:
                    dead += 1
        if cancel_check():
            raise pipeline.PipelineCancelled("Cancelled.")
        # cold start: nothing has ever worked this session = the provider or the key is gone
        if not fresh_ok and dead >= MAX_DEAD_ATTEMPTS_BEFORE_GIVING_UP:
            raise LongformError(
                f"The first {dead} image generations all failed - the image API looks "
                "unreachable. Stopping instead of filling the video with black frames; "
                "check WAVESPEED_API_KEY and resume (nothing is lost).")
        next_queue = []
        for idx in queue:
            path = round_results.get(idx)
            if path:
                results[idx] = path
            else:
                attempts[idx] = attempts.get(idx, 0) + 1
                if attempts[idx] <= IMAGE_RETRIES:
                    next_queue.append(idx)
                else:
                    results[idx] = None
                    _log(status_cb, f"image #{idx + 1} failed after {IMAGE_RETRIES} retries "
                                    "- the render will stay blocked until it is restored.")
        # mid-run death: a whole round produced nothing while retriable images remain
        if not round_results and next_queue and fresh_ok == 0:
            raise LongformError(
                "A full generation round produced no images - the image API looks down. "
                "Stopping; the finished images are kept.")
        if next_queue:
            _log(status_cb, f"Retrying {len(next_queue)} image(s) that failed this round...")
        queue = next_queue
    ok = sum(1 for v in results.values() if v)
    # Record which prompt each finished frame was drawn from, so a later run that rewrites the
    # prompts can tell its own art from art that belongs to wording nobody uses any more.
    try:
        save_state(out_dir, image_prompt_prints={k: v for k, v in prints.items()
                                                 if results.get(int(k))})
    except Exception:                                                   # noqa: BLE001
        pass
    _log(status_cb, f"Images finished: {ok}/{total} generated.")
    return results


# ------------------------------------------------------------------ 4b) THUMBNAIL

# The thumbnail is the same drawing hand as the video, or the click and the video look like
# two different channels.
_THUMB_STYLE_HEAD = ("Simple MS Paint style illustration, crude hand-drawn look, stick figures "
                     "with large round white heads and thin straight black limbs, thick uniform "
                     "black outlines on everything, oversized simple black dot eyes with "
                     "exaggerated expressions, thin curved eyebrows, simple line mouths, ")
_THUMB_STYLE_BODY = (", flat single-color background, flat solid fill colors only, high contrast, "
                     "no gradients, no shading, no texture, no lighting, no photorealism, no 3D, "
                     "no realistic faces, no anime style, amateur webcomic aesthetic, "
                     "deliberately unpolished, minimal detail")


def _thumb_style_tail(aspect=None):
    """The style tail, ending in the aspect ratio THIS project actually renders.

    It used to end in a hardcoded "16:9 aspect ratio." - so a 9:16 Short got landscape
    thumbnails. Measured on a delivered sketch short: 1672x941 and 1360x768 for a 1080x1920
    video, which is unusable as a Shorts thumbnail.
    """
    ratio = str(aspect or IMAGE_ASPECT or "16:9").strip() or "16:9"
    return "%s, %s aspect ratio." % (_THUMB_STYLE_BODY, ratio)


# Kept so older callers and saved prompts still resolve; new code passes the project aspect.
_THUMB_STYLE_TAIL = _THUMB_STYLE_BODY + ", 16:9 aspect ratio."


def _fallback_thumb_hook(script, lines):
    """A short, punchy ALL-CAPS thumbnail hook derived from the script when no model is used."""
    first = ""
    for l in (lines or []):
        t = str(l.get("text") or "").strip()
        if t:
            first = t
            break
    first = first or " ".join(str(script or "").split()[:8])
    words = [w for w in re.sub(r"[^A-Za-z0-9?' ]", " ", first).split() if w]
    hook = " ".join(words[:4]).upper().strip(" '")
    return (hook + ("?" if first.rstrip().endswith("?") else "")) or "WATCH THIS"


def build_thumbnail_prompt(script, lines, reasoning_model=None, status_cb=None):
    """Compose a click-optimised doodle-thumbnail prompt: an expressive stick figure + one bold
    visual + a HUGE ALL-CAPS hook. Uses the reasoning model for hook+subject when a key is
    present, else a deterministic fallback."""
    hook, subject = "", ("a stick figure with a hugely exaggerated shocked, wide-eyed curious "
                         "face, both hands raised")
    if os.environ.get("WAVESPEED_API_KEY"):
        try:
            data = agent_core.post_json_url(agent_core.WAVESPEED_LLM_API, {
                "model": str(reasoning_model or "anthropic/claude-opus-4.8"),
                "messages": [
                    {"role": "system", "content":
                        "You design a viral YouTube thumbnail for a doodle explainer video. "
                        "Reply STRICT JSON: {\"hook\": \"1-4 word ALL-CAPS curiosity hook\", "
                        "\"subject\": \"one short vivid visual of an expressive stick-figure "
                        "doodle scene, no text\"}. The hook creates a curiosity gap; never spoil "
                        "the answer."},
                    {"role": "user", "content": "SCRIPT:\n" + str(script or "")[:4000]}],
                "temperature": 0.7, "max_tokens": 200,
                "response_format": {"type": "json_object"},
            }, timeout=90)
            j = agent_core.extract_json_object(data["choices"][0]["message"]["content"]) or {}
            hook = str(j.get("hook") or "").strip().upper()
            subject = str(j.get("subject") or "").strip() or subject
        except Exception as exc:            # noqa: BLE001
            _log(status_cb, f"Thumbnail concept fell back to the script ({exc}).")
    hook = hook or _fallback_thumb_hook(script, lines)
    return (f"{_THUMB_STYLE_HEAD}{subject}, a big bold red circle or arrow highlighting the key "
            f"element, HUGE bold black ALL CAPS marker text filling the top of the frame reading "
            f"\"{hook}\"{_thumb_style_tail()}"), hook


def build_thumbnail_concepts(script, lines, reasoning_model=None, status_cb=None):
    """Return three deliberately different thumbnail concepts, each with its matching title."""
    fallback_hook = _fallback_thumb_hook(script, lines)
    clean = re.sub(r"\s+", " ", str(script or "")).strip()
    fallback_title = (clean.split(".", 1)[0][:88].strip(" -:;,.") or "The Story You Never Knew")
    concepts = [
        {"hook": fallback_hook, "title": fallback_title,
         "subject": "one shocked stick figure discovering the central contradiction, extreme reaction"},
        {"hook": "HOW IS THIS REAL?", "title": f"The Strange Truth Behind {fallback_title}"[:96],
         "subject": "two stick figures on opposite sides of the central conflict, one clear visual contrast"},
        {"hook": "NOBODY EXPECTED THIS", "title": f"What Really Happened: {fallback_title}"[:96],
         "subject": "one dramatic oversized object from the story with a tiny worried stick figure beside it"},
    ]
    if os.environ.get("WAVESPEED_API_KEY"):
        try:
            data = agent_core.post_json_url(agent_core.WAVESPEED_LLM_API, {
                "model": str(reasoning_model or "anthropic/claude-opus-4.8"),
                "messages": [
                    {"role": "system", "content":
                        "Create exactly 3 DISTINCT viral YouTube thumbnail concepts for a doodle "
                        "explainer. Each concept needs a matching honest video title. The IMAGE is "
                        "minimal: one focal scene, at most two characters, one unanswered visual "
                        "question, no labels or written words. Vary the angle: (1) shock/reaction, "
                        "(2) conflict/contrast, (3) mystery/object. Reply STRICT JSON: "
                        "{\"variants\":[{\"hook\":\"short internal concept tag\",\"title\":\"specific "
                        "compelling video title\",\"subject\":\"one concise curiosity-driven visual "
                        "scene, no written text\"}, ...]}. Never spoil the answer."},
                    {"role": "user", "content": "SCRIPT:\n" + clean[:5000]}],
                "temperature": 0.9, "max_tokens": 650,
                "response_format": {"type": "json_object"},
            }, timeout=120)
            parsed = agent_core.extract_json_object(data["choices"][0]["message"]["content"]) or {}
            got = parsed.get("variants") or []
            if isinstance(got, list) and len(got) >= 3:
                normalized = []
                for item in got[:3]:
                    if not isinstance(item, dict):
                        break
                    hook = str(item.get("hook") or "").strip().upper()[:48]
                    title = str(item.get("title") or "").strip()[:110]
                    subject = str(item.get("subject") or "").strip()[:420]
                    if not hook or not title or not subject:
                        break
                    normalized.append({"hook": hook, "title": title, "subject": subject})
                if len(normalized) == 3:
                    concepts = normalized
        except Exception as exc:  # noqa: BLE001
            _log(status_cb, f"Thumbnail concepts fell back to the script ({exc}).")
    return concepts


def thumbnail_variants(out_dir):
    """Read healthy generated thumbnail variants and their paired titles."""

    adopt_project_aspect(out_dir)
    out_dir = Path(out_dir)
    try:
        meta = json.loads((out_dir / "thumbnail_variants.json").read_text(encoding="utf-8"))
    except Exception:
        meta = {}
    try:
        selected = int(meta.get("selected") or 0) if isinstance(meta, dict) else 0
    except (TypeError, ValueError):
        selected = 0
    result = []
    for i, item in enumerate((meta.get("variants") or []) if isinstance(meta, dict) else []):
        path = out_dir / str(item.get("file") or "")
        if _image_done(path, IMAGE_ASPECT):
            result.append({"index": i, "file": str(path.relative_to(out_dir)).replace("\\", "/"),
                           "title": str(item.get("title") or ""),
                           "hook": str(item.get("hook") or ""), "selected": i == selected})
    return result


def _generate_wavespeed_thumbnail(prompt, out_path, key, cancel_event=None,
                                  status_cb=None, label="Thumbnail"):
    """Generate one 16:9 doodle thumbnail with GPT Image 2.0 through WaveSpeed."""
    if cancel_event is not None and cancel_event.is_set():
        raise pipeline.PipelineCancelled("Cancelled.")
    model = "openai/gpt-image-2/text-to-image"
    payload = {
        "aspect_ratio": IMAGE_ASPECT,
        "enable_base64_output": False,
        "enable_sync_mode": False,
        "output_format": "png",
        "prompt": str(prompt),
        "quality": "medium",
        "resolution": "1k",
    }
    response = pipeline.request_json(
        "POST", f"{pipeline.API_BASE}/{model}", key, payload, timeout=240)
    prediction_id = pipeline.unwrap_id(response)
    outputs, _ = pipeline.poll_wavespeed(
        prediction_id, key, timeout_s=600, interval_s=3,
        cancel_event=cancel_event, status_cb=status_cb, label=label)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    pipeline.download_file(outputs[0], out_path)
    if not _image_done(out_path, IMAGE_ASPECT):
        raise LongformError(f"{label} returned an invalid image.")
    return str(out_path)


def generate_thumbnail(out_dir, script, lines, reasoning_model=None, status_cb=None,
                       cancel_event=None, force=False):
    """Generate exactly three GPT Image 2.0 thumbnail/title pairs through WaveSpeed."""
    out_dir = Path(out_dir)
    thumb = out_dir / "thumbnail.png"
    existing = thumbnail_variants(out_dir)
    if not force and len(existing) == 3 and _image_done(thumb, IMAGE_ASPECT):
        _log(status_cb, "Three thumbnail variants already exist - reusing them.")
        return str(thumb)
    if cancel_event is not None and cancel_event.is_set():
        return None
    key = pipeline.api_key()
    concepts = build_thumbnail_concepts(script, lines, reasoning_model=reasoning_model,
                                        status_cb=status_cb)
    variants_dir = out_dir / "thumbnails"
    variants_dir.mkdir(parents=True, exist_ok=True)
    if force and any(variants_dir.glob("thumbnail_*.png")):
        archive = variants_dir / ("archive_" + time.strftime("%Y%m%d_%H%M%S"))
        archive.mkdir(parents=True, exist_ok=True)
        for old in variants_dir.glob("thumbnail_*.png"):
            try:
                old.replace(archive / old.name)
            except OSError:
                pass
    jobs = []
    metadata = []
    for i, concept in enumerate(concepts[:3]):
        path = variants_dir / f"thumbnail_{i + 1}.png"
        prompt = (f"{_THUMB_STYLE_HEAD}{concept['subject']}. One dominant focal point, at most two "
                  "characters, one expressive face, one intriguing object or physical contrast, "
                  "strong readable silhouette and generous clean negative space. Create curiosity "
                  "without explaining the answer. NO text, NO letters, NO words, NO numbers, NO "
                  "labels, NO split panels, NO infographic layout, NO repeated annotations, NO "
                  f"circles or arrows.{_thumb_style_tail()} Family-friendly, clean professional "
                  "YouTube thumbnail composition.")
        jobs.append((i, prompt, str(path)))
        metadata.append({"file": str(path.relative_to(out_dir)).replace("\\", "/"),
                         "title": concept["title"], "hook": concept["hook"]})
    # Persist the three titles BEFORE any paid image request. A restart/cancel after 1-2 images
    # must not erase the concepts or leave completed thumbnails with unknowable matching titles.
    metadata_path = out_dir / "thumbnail_variants.json"
    metadata_path.write_text(
        json.dumps({"selected": 0, "complete": False, "variants": metadata},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    _log(status_cb, "Generating 3 thumbnail variants with matching titles...")
    try:
        pending, res = list(jobs), {}
        for attempt in range(3):
            if not pending or (cancel_event and cancel_event.is_set()):
                break
            if attempt:
                _log(status_cb, f"Retrying {len(pending)} missing thumbnail variant(s) "
                                f"(attempt {attempt + 1}/3)...")
            batch = {}
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(3, len(pending))) as pool:
                futures = {
                    pool.submit(_generate_wavespeed_thumbnail, prompt, path, key,
                                cancel_event, status_cb, f"Thumbnail {i + 1}"): (i, path)
                    for i, prompt, path in pending
                }
                for future, (i, path) in futures.items():
                    try:
                        batch[i] = future.result()
                    except pipeline.PipelineCancelled:
                        raise
                    except Exception as exc:  # noqa: BLE001
                        _log(status_cb, f"Thumbnail {i + 1} failed ({exc}).")
                        batch[i] = None
            res.update(batch)
            pending = [(i, prompt, path) for i, prompt, path in pending
                       if not (_image_done(path, IMAGE_ASPECT) and res.get(i))]
    except Exception as exc:                # noqa: BLE001
        _log(status_cb, f"Thumbnail generation failed ({exc}).")
        return None
    healthy = [i for i, _prompt, path in jobs if _image_done(path, IMAGE_ASPECT) and res.get(i)]
    if len(healthy) != 3:
        _log(status_cb, f"Thumbnail set incomplete ({len(healthy)}/3); generate again from the editor.")
        return None
    metadata_path.write_text(
        json.dumps({"selected": 0, "complete": True, "variants": metadata},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    shutil.copy2(variants_dir / "thumbnail_1.png", thumb)
    save_state(out_dir, thumbnail_title=metadata[0]["title"], thumbnail_selected=0)
    _log(status_cb, "Three thumbnail variants saved. Variant 1 is selected for now.")
    return str(thumb)


# ------------------------------------------------------------------ 5) VERIFY + ASSEMBLE

def verify_images(lines, results, reasoning_model=None, status_cb=None):
    """Deterministic completeness check + (when a key is present) the reasoning model confirms
    the timestamp->image mapping before assembly."""
    missing = [i for i in range(len(lines))
               if not results.get(i) or not _image_done(results.get(i), IMAGE_ASPECT)]
    _log(status_cb, f"Coverage check: {len(lines) - len(missing)}/{len(lines)} timestamps have "
                    f"an image" + (f"; missing: {[i + 1 for i in missing]}" if missing else "."))
    if os.environ.get("WAVESPEED_API_KEY"):
        try:
            listing = "\n".join(
                f"{fmt_ts(l['start'])} -> {Path(results[i]).name if results.get(i) else 'MISSING'}"
                for i, l in enumerate(lines))
            data = agent_core.post_json_url(agent_core.WAVESPEED_LLM_API, {
                "model": str(reasoning_model or "anthropic/claude-opus-4.8"),
                "messages": [
                    {"role": "system", "content":
                        "You verify a timestamp->image mapping for a video assembly. Reply with "
                        "STRICT JSON: {\"complete\": bool, \"missing_timestamps\": [..]}"},
                    {"role": "user", "content": listing}],
                "temperature": 0.0, "max_tokens": 400,
                "response_format": {"type": "json_object"},
            }, timeout=120)
            verdict = agent_core.extract_json_object(data["choices"][0]["message"]["content"]) or {}
            _log(status_cb, f"Reasoning check: complete={verdict.get('complete')} "
                            f"missing={verdict.get('missing_timestamps') or []}")
        except Exception as exc:  # noqa: BLE001
            _log(status_cb, f"Reasoning check skipped ({exc}).")
    return missing


def detect_black_segments(video_path, min_duration=0.15):
    """Inspect the encoded video itself; timeline/file coverage alone cannot prove it has pixels."""
    ffmpeg = pipeline.find_ffmpeg()
    if not ffmpeg or not Path(video_path).is_file():
        return []
    run = subprocess.run(
        [ffmpeg, "-hide_banner", "-v", "info", "-i", str(video_path),
         "-vf", f"blackdetect=d={float(min_duration):.3f}:pix_th=0.10:pic_th=0.98",
         "-an", "-f", "null", os.devnull],
        capture_output=True, text=True, timeout=3600, check=False)
    segments = []
    for match in re.finditer(
            r"black_start:([0-9.]+)\s+black_end:([0-9.]+)\s+black_duration:([0-9.]+)",
            (run.stderr or "")):
        segments.append({"start": round(float(match.group(1)), 3),
                         "end": round(float(match.group(2)), 3),
                         "duration": round(float(match.group(3)), 3)})
    return segments


# The reference "someone googles it" clip, kept with the project rather than pointed at a
# ComfyUI output folder that gets cleared. Any clip with the same search page works: the tracker
# finds the logo, it does not care where the file came from.
HOOK_INTRO_CLIP = ROOT / "assets" / "hook_intros" / "google_search.mp4"


# A search box holds what a PERSON typed, and nobody googles their own symptom in the second
# person. A script says "why does YOUR stomach growl"; the search that produced the video says
# "why does MY stomach growl". Ordered longest-first so "you are" is rewritten before "you".
BOUNDARY = chr(92) + "b"        # the regex word-boundary token
_SEARCH_PERSON = (
    ("you're", "i'm"), ("you are", "i'm"), ("your own", "my own"),
    ("yourself", "myself"), ("yours", "mine"), ("your", "my"), ("you", "i"),
)


def as_typed_search(text):
    """Rewrite a narration line into what someone would actually type into Google.

    Second person to first person, and the casing a search box really carries - people type in
    lower case and skip the apostrophe. Only whole words are touched, so "young" and "yourself"
    are not mangled into something else.
    """
    out = " ".join(str(text or "").split())
    if not out:
        return ""
    for src, dst in _SEARCH_PERSON:
        out = re.sub(BOUNDARY + re.escape(src) + BOUNDARY, dst, out,
                     flags=re.IGNORECASE)
    out = out.lower().replace("i'm", "im").rstrip("?.!").strip()
    # "i" on its own reads as a typo in a search box only when capitalised; keep it lower like
    # the rest of the query, which is how a real search history looks.
    return out


def drop_hook_from_narration(script, hook):
    """Remove the sentence the Google intro already speaks, so the short says it ONCE.

    The intro types the hook AND reads it aloud. The script's opening sentence is usually that
    same hook, so the finished short asked its question twice in a row - once over the search
    box and again as the first line of the video. Only an exact opening match is removed: a hook
    the user marked from the middle of the script is still narrated in its place.
    """
    body = str(script or "").strip()
    wanted = " ".join(str(hook or "").split()).strip().rstrip("?.!").casefold()
    if not body or not wanted:
        return body
    first = re.match(r"\s*([^.!?]*[.!?])", body)
    if not first:
        return body
    # The heading marker is decoration, not words: "# Why does..." is the same opening sentence
    # as "Why does...", and comparing them with the hash attached never matched.
    opening = " ".join(first.group(1).lstrip("#").split()).strip().rstrip("?.!").casefold()
    if opening != wanted:
        return body
    return body[first.end():].strip()


def first_sentence(text):
    """The first sentence of `text`, ending at the EARLIEST terminator.

    Scanning ". ", "? ", "! " in that order asks the wrong question: it stops at the first
    PATTERN found anywhere in the string, not at the first terminator. "Why does only one of my
    nostrils work? Try breathing through your nose right now." carries no ". " until the end of
    the SECOND sentence, so the full stop beat the question mark and the search box was typed
    with both sentences in it.
    """
    text = " ".join(str(text or "").split())
    end = re.search(r"[.!?](?=\s|$)", text)
    return text[:end.end()].strip() if end else text.strip()


def opening_line_of(script):
    """The script's own opening line, for when nothing was marked.

    A pasted document's first line is often "# Some Title", and a title is not narration - left
    in it was read aloud with the hash attached and drew a beat of its own, which is why headings
    are dropped everywhere else. But these scripts are WRITTEN as "# Why does only one of my
    nostrils work?" followed by the body: there the title IS the hook, and dropping it typed the
    script's SECOND sentence into the search box ("try breathing through my nose right now").

    So the narration's own opening question wins whenever there is one - that is the sentence
    drop_hook_from_narration can then remove, which is what keeps the short from asking its
    question twice. A heading's question stands in only when the narration opens with a
    statement and there is otherwise nothing to google. Any heading that is not a question is a
    title and is skipped exactly as before.
    """
    heading_question = ""
    for line in str(script or "").splitlines():
        stripped = line.strip()
        if not stripped or set(stripped) <= set("-=_*"):
            continue
        if stripped.startswith("#"):
            heading = " ".join(stripped.lstrip("#").split()).strip("*_ ")
            if heading.endswith("?") and not heading_question:
                heading_question = heading
            continue
        body = " ".join(stripped.split())
        if heading_question and not first_sentence(body).endswith("?"):
            return heading_question
        return body
    return heading_question


def hook_line_of(script, marked=None):
    """The line typed into the intro: what the user marked, else the script's opening line.

    A hook that is a whole paragraph reads as noise in a search box, so it is trimmed to its
    first sentence.
    """
    # A marked hook is taken as given, except for markdown decoration: the user marks a line as
    # it appears in their pasted document, and "# Why Does Only One..." typed into a search box
    # shows the hash.
    text = " ".join(str(marked or "").lstrip("#").split())
    return first_sentence(text or opening_line_of(script))


def _probe_video_size(path):
    """(width, height) of a video file, or None when it cannot be read."""
    try:
        ffprobe = pipeline.find_ffprobe(pipeline.find_ffmpeg())
        if not ffprobe:
            return None
        done = subprocess.run(
            [str(ffprobe), "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", str(path)],
            capture_output=True, text=True, timeout=60)
        width, _, height = (done.stdout or "").strip().partition("x")
        if width.isdigit() and height.isdigit() and int(width) > 0 and int(height) > 0:
            return int(width), int(height)
    except Exception:                                                   # noqa: BLE001
        pass
    return None


def prepend_hook_intro(short_path, intro_path, out_path, status_cb=None, ffmpeg=None):
    """Join the typed-search intro in front of a finished short.

    Both are conformed to the short's own canvas and rate first: the intro is a 720x1280/24fps
    render and concatenating mismatched streams drops frames silently rather than failing.
    """
    log = status_cb or (lambda _m: None)
    ffmpeg = str(ffmpeg or pipeline.find_ffmpeg() or "")
    short_path, intro_path, out_path = Path(short_path), Path(intro_path), Path(out_path)
    if not intro_path.is_file():
        raise LongformError(f"The hook intro is missing: {intro_path}")
    # The canvas comes from the SHORT, never from the module global. `assemble_video` already
    # takes its aspect explicitly for exactly this reason - any other job in the process may
    # have set the global meanwhile - but the join still read it, so a correctly rendered 9:16
    # short was scaled up and CROPPED into 1920x1080 at the last step. Measured on a delivered
    # sketch short (2026-08-29): every source image 720x1280, the intro 720x1280, the
    # `_with_hook.mp4` 1920x1080 with the artwork cut off at the sides.
    #
    # Joining an intro must not change the format of the thing it is joined to.
    width, height = _probe_video_size(short_path) or video_size()
    graph = (
        f"[0:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},fps={VIDEO_FPS},format=yuv420p,setsar=1[i];"
        f"[1:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},fps={VIDEO_FPS},format=yuv420p,setsar=1[m];"
        "[0:a]aformat=sample_rates=48000:channel_layouts=stereo[ia];"
        "[1:a]aformat=sample_rates=48000:channel_layouts=stereo[ma];"
        "[i][ia][m][ma]concat=n=2:v=1:a=1[v][a]"
    )
    log("Joining the hook intro in front of the short...")
    done = subprocess.run(
        [ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
         "-i", str(intro_path), "-i", str(short_path),
         "-filter_complex", graph, "-map", "[v]", "-map", "[a]",
         "-c:v", "libx264", "-preset", "medium", "-crf", "19",
         "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(out_path)],
        capture_output=True, text=True, timeout=1800)
    if not out_path.is_file() or out_path.stat().st_size < 10000:
        raise LongformError(f"Could not join the hook intro: {(done.stderr or '')[-300:]}")
    return out_path


def assemble_video(lines, durations, results, audio_path, out_path, status_cb=None,
                   aspect=None):
    """Cut the frames to the voiceover.

    `aspect` is explicit on purpose. This used to read the module-global canvas, which every
    other job, editor and rebuild in the process is free to change - so a 9:16 short whose
    images are 720x1280 was assembled onto a 1920x1080 canvas because something else had set
    16:9 while its images were still generating. Measured on a delivered short: portrait frames,
    landscape video.
    """
    canvas_aspect = str(aspect or IMAGE_ASPECT)
    canvas_w, canvas_h = ((1080, 1920) if canvas_aspect == "9:16" else (1920, 1080))
    ffmpeg = pipeline.find_ffmpeg()
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    missing = [i for i in range(len(lines))
               if not results.get(i) or not _image_done(results.get(i), IMAGE_ASPECT)]
    if missing:
        preview = ", ".join(f"#{i + 1} {fmt_ts(lines[i]['start'])}" for i in missing[:12])
        more = f" (+{len(missing) - 12} more)" if len(missing) > 12 else ""
        raise LongformError(
            f"Refusing to render a video with {len(missing)} black/missing scene(s): "
            f"{preview}{more}. Restore or regenerate these images first.")
    work = out_path.parent / "_assembly"
    work.mkdir(exist_ok=True)
    black = work / "black.png"
    if not black.exists():
        r = subprocess.run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                            "-f", "lavfi", "-i",
                            f"color=black:s={canvas_w}x{canvas_h}",
                            "-frames:v", "1", str(black)], capture_output=True, timeout=60)
        if not black.exists():
            raise LongformError("Could not create the black fallback frame.")
    lst = work / "concat.txt"
    # The concat demuxer silently DROPS frames when image dimensions change mid-stream
    # (decoder reinit) - a replaced/uploaded image with a different size made its scene
    # (and neighbours) vanish while the previous image kept showing. Normalize any
    # odd-sized image to the majority size once, into the assembly workspace.
    from PIL import Image as _PILImage
    sizes = {}
    for i in range(len(lines)):
        p = results.get(i)
        if not p:
            continue
        try:
            with _PILImage.open(p) as im:
                sizes[i] = im.size
        except Exception:
            sizes[i] = None
    counts = {}
    for s in sizes.values():
        if s:
            counts[s] = counts.get(s, 0) + 1
    base_size = max(counts, key=counts.get) if counts else (1280, 720)
    for i, s in sizes.items():
        if not s or s == base_size:
            continue
        src = Path(results[i])
        norm = work / f"norm_{i:03d}_{src.stem[:40]}.png"
        try:
            with _PILImage.open(src) as im:
                im = im.convert("RGB")
                ratio = min(base_size[0] / im.width, base_size[1] / im.height)
                nw, nh = max(1, round(im.width * ratio)), max(1, round(im.height * ratio))
                canvas = _PILImage.new("RGB", base_size, (0, 0, 0))
                canvas.paste(im.resize((nw, nh), _PILImage.LANCZOS),
                             ((base_size[0] - nw) // 2, (base_size[1] - nh) // 2))
                canvas.save(norm)
            results[i] = str(norm)
            _log(status_cb, f"Scene {i + 1}: normalized {s[0]}x{s[1]} image to "
                            f"{base_size[0]}x{base_size[1]} for a glitch-free concat.")
        except Exception as exc:  # noqa: BLE001 - keep the original rather than fail the render
            _log(status_cb, f"Scene {i + 1}: could not normalize image size ({exc}).")
    entries = []
    for i in range(len(lines)):
        img = results.get(i) or str(black)
        entries.append(f"file '{Path(img).resolve().as_posix()}'\nduration {durations[i]:.3f}\n")
    # concat demuxer needs the last file repeated (its duration otherwise ignored)
    last_img = results.get(len(lines) - 1) or str(black)
    entries.append(f"file '{Path(last_img).resolve().as_posix()}'\n")
    lst.write_text("".join(entries), encoding="utf-8")
    _log(status_cb, f"Assembling {len(lines)} scenes -> {out_path.name} ...")
    width, height = canvas_w, canvas_h
    zoom = float(IMAGE_ZOOM or 0.0)
    if zoom > 0 and len(lines) > IMAGE_ZOOM_MAX_SCENES:
        _log(status_cb, f"Image zoom is off for this render: {len(lines)} scenes is past the "
                        f"{IMAGE_ZOOM_MAX_SCENES} a single filtergraph should carry.")
        zoom = 0.0
    if zoom > 0:
        # One decode chain per still, each with its own push-in, joined by the concat FILTER -
        # still a single encode. The concat DEMUXER cannot do this: by the time its output
        # reaches the filter chain the scene boundaries are gone, so a zoom driven by time would
        # not know where one image ends and the next begins.
        inputs, chains, labels = [], [], []
        for i in range(len(lines)):
            img = results.get(i) or str(black)
            frames = max(1, int(round(float(durations[i]) * VIDEO_FPS)))
            inputs += ["-loop", "1", "-t", f"{float(durations[i]):.3f}", "-i", str(img)]
            z_expr, x_expr, y_expr = _motion_expressions(i, zoom, frames)
            # Fit to the canvas exactly as the flat path does, THEN oversample: zoompan crops
            # from what it is given, and cropping a 1080-wide frame directly makes the push-in
            # visibly step. d=1 emits one frame per input frame, so `on` ramps once per frame.
            chains.append(
                f"[{i}:v]fps={VIDEO_FPS},"
                f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,"
                f"scale={width * 2}:{height * 2}:flags=lanczos,"
                f"zoompan=z='{z_expr}':d=1"
                f":x='{x_expr}':y='{y_expr}':s={width}x{height}:fps={VIDEO_FPS},"
                f"setsar=1[v{i}]")
            labels.append(f"[v{i}]")
        graph = (";".join(chains) + ";" + "".join(labels)
                 + f"concat=n={len(labels)}:v=1:a=0,format=yuv420p[v]")
        _log(status_cb, f"Image motion: {IMAGE_MOTION} at {zoom:.0%} "
                        f"on {len(lines)} still(s).")
        command = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                   *inputs, "-i", str(audio_path),
                   "-filter_complex", graph,
                   "-map", "[v]", "-map", f"{len(lines)}:a"]
    else:
        command = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                   "-f", "concat", "-safe", "0", "-i", str(lst),
                   "-i", str(audio_path),
                   "-vf", (f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                           f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,"
                           f"fps={VIDEO_FPS},format=yuv420p")]
    r = subprocess.run([
        *command,
        "-c:v", "libx264", "-preset", "medium", "-crf", "19",
        "-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart",
        str(out_path)], capture_output=True, text=True, timeout=3600)
    if not out_path.exists() or out_path.stat().st_size < 10000:
        raise LongformError(f"Assembly failed: {(r.stderr or '')[-400:]}")
    black = detect_black_segments(out_path)
    if black:
        spans = ", ".join(f"{item['start']:.2f}-{item['end']:.2f}s" for item in black[:8])
        raise LongformError(
            f"Rendered video contains {len(black)} detected black-screen segment(s): {spans}. "
            "The file was kept for diagnosis but is not marked as a successful render.")
    _log(status_cb, f"Final longform video ready: {out_path}")
    return out_path


# ---------------------------------------------------------------- word helper
# Kept for caption_cut_starts, which now always falls back to the line start: with no words on
# the frames there is no caption phrase left to sync a cut to.
def _norm_words(text):
    return [w for w in re.sub(r"[^A-Z0-9 ]", " ", str(text or "").upper()).split() if len(w) >= 2]


def frames_from_disk(project_dir):
    """The frame list the post-run editor works on: for every timed line the expected image
    filename, whether it exists (missing = black frame in the video), timestamp, duration and
    the narration text. Returns (state, frames) or (None, [])."""

    adopt_project_aspect(project_dir)
    project_dir = Path(project_dir)
    try:
        state = json.loads((project_dir / STATE_FILE).read_text(encoding="utf-8"))
    except Exception:
        return None, []
    lines = state.get("lines") or []
    if not lines:
        return state, []
    audio_duration = float(state.get("audio_duration") or 0.0)
    durations = saved_project_cut_durations(state, lines, audio_duration)
    frames = []
    cursor = 0.0
    for i, line in enumerate(lines):
        resolved = resolve_image_for(project_dir / "images", i, line, durations[i], IMAGE_ASPECT)
        path = resolved or (project_dir / "images" / f"{image_key(i, line, durations[i])}.png")
        name = path.name
        frames.append({
            "idx": i, "start": round(cursor, 3),
            "end": round(cursor + float(durations[i]), 3),
            "ts": fmt_ts(line["start"]), "dur": durations[i],
            "text": str(line.get("text") or ""), "file": name,
            "exists": _image_done(path, IMAGE_ASPECT),
        })
        cursor += float(durations[i])
    return state, frames


def reconcile_image_names(project_dir, status_cb=None):
    """Rename each index's image onto the filename the CURRENT line clock expects.

    Image filenames encode the on-screen timestamp+duration (image_key), so any change to the
    timings - a re-transcription after the voiceover was edited, a speed change - shifts every
    expected name and the untouched image files suddenly look "missing" (black frames), even
    though the right picture for index i is sitting right there under its old name. The index i
    is the stable key (same script -> same line order -> same img{i:03d}_ prefix), so we glob by
    index and rename the real file onto the expected name. Idempotent; safe to call before every
    rebuild."""
    project_dir = Path(project_dir)
    try:
        state = json.loads((project_dir / STATE_FILE).read_text(encoding="utf-8"))
    except Exception:
        return 0
    lines = state.get("lines") or []
    if not lines:
        return 0
    imgdir = project_dir / "images"
    if not imgdir.is_dir():
        return 0
    durations = saved_project_cut_durations(
        state, lines, float(state.get("audio_duration") or 0.0))
    renamed = 0
    for i, line in enumerate(lines):
        expected = imgdir / (image_key(i, line, durations[i]) + ".png")
        if expected.is_file() and expected.stat().st_size > 1024:
            continue                                     # already on the right name
        real = [c for c in sorted(imgdir.glob(f"img{i:03d}_*.png"))
                if c.stat().st_size > 1024]
        if not real:
            continue                                     # genuinely never generated
        expected.unlink(missing_ok=True)                 # a stale/tiny placeholder under this name
        real[0].rename(expected)
        renamed += 1
    if renamed:
        _log(status_cb, f"Reconciled {renamed} image filename(s) to the current timings.")
    return renamed


def recover_archived_images(project_dir, status_cb=None):
    """Restore valid project-local art before falling back to a held neighbouring frame.

    Prompt repair deliberately moves stale frames out of ``images/`` before it asks the image
    provider for replacements.  That is safe only while every replacement arrives.  If a run is
    interrupted (or a few image calls fail), the timed timeline still points at the now-empty
    root folder while the original, correctly indexed art sits under
    ``images/_prompt_repair_*``.  The former recovery code ignored those files and copied the
    nearest neighbour instead.  Besides making the editor show empty scenes, that turned a
    perfectly usable original cut into a visible repeated still.

    An archived frame with the same stable index and timestamp belongs to the same narration
    slot, even if only its baked duration changed.  Restore it to the active root first.  Keep
    manually replaced/deleted-title archives out of this automatic path: those are intentional
    editor history, not failed prompt-repair work.  Only when no compatible archived frame exists
    do we retain the old hold fallback, which guarantees no black scene when the project has at
    least one valid frame.
    """
    project_dir = Path(project_dir)
    try:
        state = json.loads((project_dir / STATE_FILE).read_text(encoding="utf-8"))
    except Exception:
        return 0
    lines, prompts = state.get("lines") or [], state.get("prompts") or []
    if not lines or len(prompts) != len(lines):
        return 0
    durations = saved_project_cut_durations(
        state, lines, float(state.get("audio_duration") or 0.0))
    image_dir = project_dir / "images"
    target_aspect = str(state.get("aspect") or IMAGE_ASPECT)
    results = {}
    for idx, line in enumerate(lines):
        path = resolve_image_for(image_dir, idx, line, durations[idx], target_aspect)
        if path and _image_done(path, target_aspect):
            results[idx] = str(path)
    before = len(results)
    if before == len(lines):
        return 0
    recovered = 0

    def archived_for(index, line, duration):
        """Find an automatic-repair archive that still belongs to this exact narration slot."""
        stamp = fmt_ts(line["start"]).replace(":", "-").replace(".", "-").strip("[]")
        prefix = f"img{index:03d}_[{stamp}]_dur"
        candidates = []
        for candidate in image_dir.rglob(f"img{index:03d}_*.png"):
            # These folders are user/editor history. Restoring them would undo a deliberate
            # replacement or reintroduce a heading image that was intentionally removed.
            parts = {part.lower() for part in candidate.relative_to(image_dir).parts[:-1]}
            if any(part.startswith("_manual_unused_") or part == "_removed_title" for part in parts):
                continue
            if _image_done(candidate, target_aspect):
                candidates.append(candidate)
        if not candidates:
            return None
        exact = [p for p in candidates if p.name.startswith(prefix)]
        # An index is stable through a speech-clock retime.  If there is exactly one archived
        # candidate for it, it is safer and more relevant than a repeated neighbour even when
        # the old timestamp was rounded differently.
        pool = exact or (candidates if len(candidates) == 1 else [])
        return max(pool, key=lambda p: p.stat().st_mtime_ns) if pool else None

    for idx, line in enumerate(lines):
        if results.get(idx):
            continue
        source = archived_for(idx, line, durations[idx])
        if source is None:
            continue
        target = image_dir / f"{image_key(idx, line, durations[idx])}.png"
        try:
            target.unlink(missing_ok=True)
            source.replace(target)
        except OSError as exc:
            _log(status_cb, f"Could not restore archived frame #{idx + 1} ({exc}).")
            continue
        if _image_done(target, target_aspect):
            results[idx] = str(target)
            recovered += 1
            _log(status_cb, f"Restored archived image for frame #{idx + 1}; its original cut is preserved.")

    # No suitable project-local art remains.  Hold a nearby valid image rather than render a
    # blank scene; this is a last-resort continuity hold, never the first choice over unused art.
    stable_sources = dict(results)
    held = 0
    if stable_sources:
        for idx in range(len(lines)):
            if results.get(idx):
                continue
            source_idx = min(stable_sources, key=lambda candidate: abs(candidate - idx))
            source = Path(stable_sources[source_idx])
            target = image_dir / f"{image_key(idx, lines[idx], durations[idx])}.png"
            try:
                shutil.copy2(source, target)
            except OSError:
                continue
            if _image_done(target, target_aspect):
                results[idx] = str(target)
                held += 1
                _log(status_cb, f"Filled frame #{idx + 1} with a hold of nearby frame "
                                f"#{source_idx + 1} (no generation, no black screen).")
    return recovered + held


def repair_heading_voiceover(project_dir, status_cb=None):
    """Drop a spoken TITLE from an existing project's voiceover, and re-time what remains.

    Stripping headings at the start of a run only helps a NEW run. A project generated before
    that fix keeps a voiceover whose first TTS part IS the title, and every rebuild reuses that
    audio - so the finished short says its own hook three times: once typed and spoken in the
    Google intro, once as the narrated title, and once as the script's opening line. Confirmed by
    transcribing the delivered file.

    The parts are separate files, so the repair is exact: drop the heading parts, concatenate the
    rest, and re-align. Returns True when it changed something.
    """
    project_dir = Path(project_dir)
    try:
        state = json.loads((project_dir / STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    texts = list(state.get("tts_part_texts") or [])
    files = [Path(p) for p in (state.get("tts_part_files") or [])]
    if not texts or len(texts) != len(files):
        return False
    keep = [i for i, t in enumerate(texts) if not str(t or "").strip().startswith("#")]
    if len(keep) == len(texts):
        return False                                    # nothing spoken that should not be
    if not keep:
        _log(status_cb, "The voiceover contains ONLY a title - refusing to leave it empty.")
        return False
    missing = [i for i in keep if not files[i].is_file()]
    if missing:
        _log(status_cb, "The voiceover parts are no longer on disk; the spoken title cannot be "
                        "removed without generating the narration again.")
        return False
    dropped = [texts[i] for i in range(len(texts)) if i not in keep]
    _log(status_cb, f"Removing {len(dropped)} spoken title line(s) from the voiceover: "
                    f"{dropped[0][:60]!r}")
    ffmpeg = pipeline.find_ffmpeg()
    voice_path = project_dir / Path(str(state.get("voiceover_file") or "voiceover.wav")).name
    concat_audio_parts([files[i] for i in keep], voice_path, ffmpeg)
    # Keep the EXISTING beat split and just remove the title beat. Re-splitting the shortened
    # script from scratch produced 18 beats where the project has 13 images, and the render
    # then refused five black scenes - the pictures are fine, it was the mapping that moved.
    old_lines = list(state.get("lines") or [])
    heading = [i for i, ln in enumerate(old_lines)
               if str((ln or {}).get("text") or "").strip().startswith("#")]
    if not heading:
        _log(status_cb, "The voiceover held a title but no beat does; leaving the timing alone.")
        return False
    offset = float(old_lines[heading[-1]].get("end") or 0.0)
    lines = []
    for index, line in enumerate(old_lines):
        if index in heading:
            continue
        moved = dict(line)
        moved["start"] = max(0.0, round(float(line.get("start") or 0.0) - offset, 3))
        moved["end"] = max(0.0, round(float(line.get("end") or 0.0) - offset, 3))
        moved["words"] = [{**w, "s": max(0.0, round(float(w.get("s") or 0.0) - offset, 3))}
                          for w in (line.get("words") or [])]
        lines.append(moved)
    duration = audio_duration_seconds(voice_path, ffmpeg)
    # The title had a picture of its own; drop it so image N still belongs to beat N.
    images = sorted((project_dir / "images").glob("*.png"))
    archive = project_dir / "images" / "_removed_title"
    for index in heading:
        if index < len(images):
            archive.mkdir(parents=True, exist_ok=True)
            images[index].replace(archive / images[index].name)
    reconcile_image_names(project_dir, status_cb=None)
    save_state(project_dir, script=strip_script_headings(str(state.get("script") or "")),
               lines=lines, audio_duration=round(duration, 3),
               tts_part_texts=[texts[i] for i in keep],
               tts_part_files=[str(files[i]) for i in keep],
               tts_part_total=len(keep))
    _log(status_cb, f"Voiceover repaired: title beat removed, {len(lines)} beat(s) left, "
                    f"{duration:.1f}s (was {offset:.1f}s longer).")
    return True


def _word_key(word):
    """Words compared for alignment: letters and digits only.

    Punctuation and casing differ between the script and a hand-corrected subtitle file, and
    comparing them raw finds no match at all where the two are in fact the same sentence.
    """
    return "".join(ch for ch in str(word or "").lower() if ch.isalnum())


def map_cues_to_frames(lines, cues):
    """For every subtitle cue, the index of the beat whose picture belongs to it.

    Both sides describe the same narration, so they are aligned on their WORD streams rather
    than on their clocks. Anchoring by time looked simpler and was wrong: the saved beats start
    mid-phrase, so a cue landed on the neighbouring beat about half the time.

    The result is deliberately allowed to repeat. Forcing a distinct picture per cue - so that
    every caption boundary gets a visible cut - was measured on the sleep explainer and pulled
    the pictures a median of 10.9 seconds away from the words they illustrate, because there are
    fewer pictures than cues. A held picture is better than the wrong one.
    """
    beat_words, owner = [], []
    for index, line in enumerate(lines):
        words = line.get("words") or [{"w": w} for w in str(line.get("text") or "").split()]
        for word in words:
            key = _word_key(word.get("w") if isinstance(word, dict) else word)
            if key:
                beat_words.append(key)
                owner.append(index)
    cue_words, cue_owner = [], []
    for index, cue in enumerate(cues):
        for word in str(cue.get("text") or "").split():
            key = _word_key(word)
            if key:
                cue_words.append(key)
                cue_owner.append(index)
    if not beat_words or not cue_words:
        return []
    import difflib
    mapped = [None] * len(cue_words)
    matcher = difflib.SequenceMatcher(None, cue_words, beat_words, autojunk=False)
    for a, b, size in matcher.get_matching_blocks():
        for k in range(size):
            mapped[a + k] = b + k
    first = {}
    for position, cue_index in enumerate(cue_owner):
        if mapped[position] is not None and cue_index not in first:
            first[cue_index] = owner[mapped[position]]
    frames, last = [], 0
    for cue_index in range(len(cues)):
        # A cue whose words matched nothing keeps the previous picture rather than jumping.
        last = max(last, first.get(cue_index, last))
        frames.append(last)
    return frames


def snap_frames_to_cue_starts(lines, cues):
    """One cut time per PICTURE, taken from the nearest subtitle boundary.

    The first attempt let the cues drive the timeline - one segment per cue - and it was wrong
    in both directions: there are more cues than pictures, so some pictures were held across two
    cues while the video gained cuts the pictures could not fill.

    Here the pictures drive it, exactly as they always did, and the subtitles only supply the
    TIMES. Each picture keeps its place and is used once; its cut is moved to the caption
    boundary nearest the moment its own line is actually spoken. So the number of cuts is the
    number of pictures, no picture repeats, and no cut lands in the middle of a phrase.
    """
    if not lines or not cues:
        return []
    boundaries = [float(c.get("start") or 0.0) for c in cues]
    targets = []
    for line in lines:
        words = line.get("words") or []
        first = float(words[0].get("s")) if words and words[0].get("s") is not None else None
        targets.append(first if first is not None else float(line.get("start") or 0.0))
    # Snap a cut to a caption boundary only when one is genuinely close, and otherwise leave it
    # on the spoken word where it already is.
    #
    # Two stricter rules were tried and measured first, and both were worse. Assigning every
    # picture its own boundary - greedily, then as an optimal monotonic matching - pulled the
    # cuts a median of 6.3s and 4.7s away from the words. The reason is structural and not a bug
    # in the search: the beats and the captions divide the same narration at different rates in
    # places, so a one-to-one match has to stretch to absorb the difference. Nothing that forces
    # one boundary per picture can avoid that.
    #
    # So the cuts that CAN sit on a phrase edge do, the rest stay exactly where the line is
    # spoken, and none of them moves far enough to leave its own sentence.
    import bisect
    cuts = [0.0]
    for index in range(1, len(lines)):
        want = targets[index]
        position = bisect.bisect_left(boundaries, want)
        nearby = [boundaries[j] for j in (position - 1, position)
                  if 0 <= j < len(boundaries) and abs(boundaries[j] - want) <= SNAP_WINDOW_S]
        choice = min(nearby, key=lambda b: abs(b - want)) if nearby else want
        cuts.append(max(cuts[-1] + 0.04, choice))
    return cuts


def fit_pictures_to_cues(lines, cues, audio_duration=0.0):
    """The CUES drive the edit: every cut lands on a subtitle timestamp, or there is no cut.

    `snap_frames_to_cue_starts` keeps one segment per picture and only pulls a cut onto a
    caption boundary when one is within SNAP_WINDOW_S. Measured on the sleep explainer, that
    left 236 of 541 cuts (44%) sitting mid-phrase, up to 1.93s from any boundary - so the film
    cut once inside a sentence AND again at its end, which is what reads as "too many cuts".

    Here a cue is a segment. Consecutive cues that want the same picture become ONE segment, so
    a held picture never produces an invisible cut, and a picture no cue asked for is dropped.
    Returns (starts, durations, picture_indexes) - all the same length.
    """
    frames = map_cues_to_frames(lines, cues)
    if not frames:
        return [], [], []
    runs = []                                  # [start_cue, end_cue, picture]
    for cue_index, picture in enumerate(frames):
        if runs and runs[-1][2] == picture:
            runs[-1][1] = cue_index
        else:
            runs.append([cue_index, cue_index, picture])
    starts, durations, pictures = [], [], []
    for position, (first_cue, last_cue, picture) in enumerate(runs):
        start = float(cues[first_cue].get("start") or 0.0)
        if position + 1 < len(runs):
            end = float(cues[runs[position + 1][0]].get("start") or start)
        else:
            end = max(float(cues[last_cue].get("end") or start), float(audio_duration or 0.0))
        starts.append(round(start, 3))
        durations.append(max(0.04, round(end - start, 3)))
        pictures.append(picture)
    # The first cue rarely begins at zero - this narration opens at 0.298s - but the assembly
    # simply plays the durations back to back from t=0. Leaving that gap out shifted EVERY later
    # cut earlier by exactly that much: measured 0/480 cuts on a boundary, all 0.298s early.
    # The first picture therefore covers the leading silence too.
    if starts:
        durations[0] = round(durations[0] + starts[0], 3)
        starts[0] = 0.0
    return starts, durations, pictures


def _recut_strict(project_dir, state, lines, cues, available, audio_duration, voice_path,
                  status_cb=None):
    """One segment per subtitle cue: every cut lands on a timestamp, or there is no cut.

    The default path keeps one segment per PICTURE and only pulls a cut onto a caption boundary
    when one is within SNAP_WINDOW_S. Measured on the sleep explainer: 541 cuts, of which only
    305 (56%) sat on a boundary - the other 236 fell mid-phrase, up to 1.93s from one, so the
    film cut inside a sentence AND again at its end. That is what reads as "too many cuts".

    Here the cues drive the edit. Pictures no cue asked for are dropped, which is the requested
    "throw out the surplus images": 547 cues and 541 pictures collapse to 481 segments, all of
    them on a cue start.
    """
    project_dir = Path(project_dir)
    starts, durations, pictures = fit_pictures_to_cues(lines, cues, audio_duration)
    if not starts:
        raise LongformError("The subtitles and the saved beats share no words - cannot re-cut.")
    missing = [index for index in pictures if index not in available]
    if missing:
        raise LongformError(f"{len(missing)} picture(s) chosen by the subtitles are missing "
                            f"from disk (first: frame #{missing[0] + 1}).")

    dropped = len(lines) - len(set(pictures))
    _log(status_cb, f"{len(cues)} cue(s) -> {len(starts)} picture change(s), every one on a "
                    f"subtitle timestamp. {dropped} surplus picture(s) dropped; "
                    f"{len(set(pictures))} of {len(lines)} kept.")

    new_lines, results = [], {}
    for position, picture in enumerate(pictures):
        end = starts[position] + durations[position]
        new_lines.append({"start": round(starts[position], 3),
                          "end": round(end, 3),
                          "text": str((lines[picture] or {}).get("text") or ""),
                          "words": (lines[picture] or {}).get("words") or []})
        results[position] = available[picture]

    slug = project_dir.name
    out = project_dir / f"{slug}_cuecut.mp4"
    n = 2
    while out.exists():
        out = project_dir / f"{slug}_cuecut_v{n}.mp4"
        n += 1
    write_srt(cues, subtitle_path(project_dir))
    write_timeline_manifest(new_lines, durations, results, audio_duration,
                            project_dir / "timeline.json",
                            voice_speed=state.get("voice_speed") or 1.0)
    rendered = assemble_video(new_lines, durations, results, voice_path, out,
                              status_cb=status_cb,
                              aspect=str(state.get("aspect") or IMAGE_ASPECT))
    save_state(project_dir, render_pending=False, last_video=Path(rendered).name)
    _publish_with_subtitles(project_dir, rendered, status_cb)
    return rendered


def recut_to_subtitles(project_dir, srt_source=None, status_cb=None, strict=False):
    """Re-cut a finished project so every picture change lands on a subtitle timestamp.

    The pictures and the voiceover are untouched - only the edit clock is replaced. The cuts
    were previously derived from a word-count rule applied to the script, which put them
    wherever that rule happened to fall: in the middle of a phrase, or holding across two.

    ``srt_source`` may be a .srt file to cut to; without one the project's own subtitles are
    used. A new versioned MP4 is written, so the previous render survives.
    """
    adopt_project_aspect(project_dir)
    project_dir = Path(project_dir)
    reconcile_image_names(project_dir, status_cb=status_cb)
    recover_archived_images(project_dir, status_cb=status_cb)
    state, frames = frames_from_disk(project_dir)
    if not state or not frames:
        raise LongformError("No resumable state in this project - nothing to re-cut.")
    lines = state["lines"]
    audio_duration = float(state.get("audio_duration") or 0.0)
    voice_path = project_dir / Path(str(state.get("voiceover_file") or "voiceover.wav")).name
    if not _audio_done(voice_path):
        raise LongformError("The voiceover file is missing - cannot re-cut.")

    source = Path(str(srt_source)) if srt_source else subtitle_path(project_dir)
    if not source.is_file():
        raise LongformError(f"No subtitle file to cut to: {source}")
    cues = tidy_cues(parse_srt(source))
    if not cues:
        raise LongformError(f"{source.name} contains no usable cues.")
    _log(status_cb, f"Re-cutting to {source.name}: {len(cues)} cue(s) against "
                    f"{len(lines)} existing beat(s).")

    available = {f["idx"]: str(project_dir / "images" / f["file"]) for f in frames if f["exists"]}
    missing = sorted(i for i in range(len(lines)) if i not in available)
    if missing:
        raise LongformError(f"{len(missing)} picture(s) are missing from disk "
                            f"(first: frame #{missing[0] + 1}).")

    # Every picture keeps its place and is used ONCE; only the cut TIMES come from the
    # subtitles. Letting the cues drive the timeline instead was tried and rejected: there are
    # more cues than pictures, so pictures were held across two captions while the film gained
    # cuts that had no picture of their own to show.
    if strict:
        return _recut_strict(project_dir, state, lines, cues, available, audio_duration,
                             voice_path, status_cb=status_cb)
    cuts = snap_frames_to_cue_starts(lines, cues)
    boundaries = {round(float(c["start"]), 3) for c in cues}
    snapped = sum(1 for t in cuts[1:] if round(t, 3) in boundaries)
    moved = [abs(cuts[i] - float((lines[i].get("words") or [{}])[0].get("s",
                                                                       lines[i]["start"])))
             for i in range(1, len(lines))]
    _log(status_cb, f"{len(cuts)} picture change(s); {snapped} moved onto a caption boundary, "
                    f"the rest stay on the spoken word. Largest move "
                    f"{max(moved) if moved else 0:.2f}s.")

    new_lines, results, durations = [], {}, []
    for index, line in enumerate(lines):
        new_lines.append({"start": round(cuts[index], 3),
                          "end": round(float(line.get("end") or cuts[index]), 3),
                          "text": str(line.get("text") or ""),
                          "words": line.get("words") or []})
        results[index] = available[index]
    for index in range(len(new_lines)):
        nxt = (cuts[index + 1] if index + 1 < len(cuts)
               else max(audio_duration, cuts[index] + 0.4))
        durations.append(max(0.04, round(nxt - cuts[index], 3)))

    slug = project_dir.name
    out = project_dir / f"{slug}_srtcut.mp4"
    n = 2
    while out.exists():
        out = project_dir / f"{slug}_srtcut_v{n}.mp4"
        n += 1
    write_srt(cues, subtitle_path(project_dir))
    write_timeline_manifest(new_lines, durations, results, audio_duration,
                            project_dir / "timeline.json",
                            voice_speed=state.get("voice_speed") or 1.0)
    rendered = assemble_video(new_lines, durations, results, voice_path, out,
                              status_cb=status_cb,
                              aspect=str(state.get("aspect") or IMAGE_ASPECT))
    save_state(project_dir, render_pending=False, last_video=Path(rendered).name)
    _publish_with_subtitles(project_dir, rendered, status_cb)
    announce_language_tracks(project_dir, status_cb)
    return rendered


def rebuild_from_disk(project_dir, status_cb=None, image_motion=None):
    """Re-assemble the longform MP4 from whatever images are on disk right now (the post-run
    frame editor's Rebuild). The voiceover and line clock come from state.json untouched; images
    the user replaced/moved are picked up by filename; a new VERSIONED mp4 is written so the
    previous render is never overwritten."""

    adopt_project_aspect(project_dir)
    project_dir = Path(project_dir)
    # adopt_project_aspect restores the project's SAVED movement, which is what an ordinary
    # rebuild wants - an old short must not silently re-render in a style it never had. But it
    # also overwrote a style the caller had just chosen, so picking one and then rebuilding
    # produced the old style with no warning. An explicit choice wins and is remembered.
    if image_motion:
        set_image_motion(image_motion)
        save_state(Path(project_dir), image_motion=IMAGE_MOTION)
    # a re-timed voiceover shifts every duration-encoded image name; re-anchor by index first so
    # the untouched pictures are not mistaken for missing (black) frames.
    # Before anything reads the timings: a project made before headings were stripped has its
    # own title sitting in the voiceover, and every rebuild would say the hook three times.
    repair_heading_voiceover(project_dir, status_cb=status_cb)
    reconcile_image_names(project_dir, status_cb=status_cb)
    recover_archived_images(project_dir, status_cb=status_cb)
    state, frames = frames_from_disk(project_dir)
    if not state or not frames:
        raise LongformError("No resumable state in this project - nothing to rebuild.")
    lines = state["lines"]
    audio_duration = float(state.get("audio_duration") or 0.0)
    # Rewrite the subtitles from the beats about to be rendered, every time. Writing them only
    # at transcription would let an edited timeline drift away from the .srt sitting next to it.
    ensure_subtitles(project_dir, lines, status_cb)
    durations = line_durations(lines, audio_duration)
    voice_path = project_dir / Path(str(state.get("voiceover_file") or "voiceover.wav")).name
    if not _audio_done(voice_path):
        raise LongformError("The voiceover file is missing - cannot rebuild.")
    results = {}
    for f in frames:
        if f["exists"]:
            results[f["idx"]] = str(project_dir / "images" / f["file"])
    _log(status_cb, f"Rebuilding from disk: {len(results)}/{len(lines)} frames present.")
    # Preserve an explicit speech-clock repair rather than putting caption-triggered cuts back
    # during a rebuild. Older projects retain their existing caption-sync behaviour.
    durations = saved_project_cut_durations(state, lines, audio_duration)
    slug = project_dir.name
    out = project_dir / f"{slug}.mp4"
    n = 2
    while out.exists():
        out = project_dir / f"{slug}_v{n}.mp4"
        n += 1
    write_timeline_manifest(lines, durations, results, audio_duration,
                            project_dir / "timeline.json",
                            voice_speed=state.get("voice_speed") or 1.0)
    # The project's OWN saved format, not the module global: the global belongs to whichever
    # job touched it last, and a concurrent 16:9 run is what put 720x1280 frames onto a
    # 1920x1080 canvas in a delivered short.
    rendered = assemble_video(lines, durations, results, voice_path, out, status_cb=status_cb,
                              aspect=str(state.get("aspect") or IMAGE_ASPECT))
    # Optional by design: without it the short is exactly what it was before. The settings come
    # from the project's own state - this function is reached from the editor's Rebuild button
    # and is handed nothing but a directory, so reading them from anywhere else was a NameError
    # that took the whole Rebuild down with it.
    # Rebuild is pressed repeatedly while tuning a short, and every press used to prepend
    # another intro to the file the last press produced - two, three Google searches stacked in
    # front of the same video. The intro belongs on the bare render, once.
    if bool(state.get("hook_intro")) and not str(rendered).endswith("_with_hook.mp4"):
        try:
            import sketch_hook_intro
            spoken = hook_line_of(str(state.get("script") or ""),
                                  str(state.get("hook_text") or ""))
            # The BOX shows what a person typed, the NARRATOR reads the script's own sentence.
            # Nobody googles their own symptom in the second person: a script that says "why
            # does YOUR stomach growl" came from a search for "why does MY stomach growl".
            typed = as_typed_search(spoken)
            intro = project_dir / "hook_intro.mp4"
            # A generated opener dropped in at the review screen REPLACES the stock clip. It was
            # made from this short's own prompt, so the query is already on its screen - only the
            # spoken hook has to be laid over it. Without this branch the upload would be
            # decoration: the render would go on using the same stock four seconds.
            supplied = project_dir / "opener_clip.mp4"
            voice_kw = {"voice": str(state.get("voice") or "") or None,
                        "tts_model": str(state.get("tts_model") or "") or None,
                        "voice_instruction": str(state.get("tts_voice_instruction") or "") or None,
                        "tts_speed": float(state.get("tts_native_speed") or 1.0)}
            if supplied.is_file() and supplied.stat().st_size > 10000:
                # A generated opener dropped in at the review screen REPLACES the stock clip. It
                # was made from this short's own prompt, so the query is already on its screen
                # and only the spoken hook has to be laid over it. Without this branch the upload
                # would be decoration and the render would use the same stock four seconds.
                _log(status_cb, f"Opener: using the clip you generated ({supplied.name}); "
                                "the built-in Google clip is skipped for this short.")
                sketch_hook_intro.voice_over_opener(
                    supplied, intro, spoken_text=spoken, status_cb=status_cb, **voice_kw)
            else:
                # The hook is SPOKEN, so it must be spoken by the short's own narrator. Leaving
                # the voice out let Gemini pick its default and the intro arrived in a
                # stranger's voice.
                sketch_hook_intro.build_hook_intro(
                    HOOK_INTRO_CLIP, typed, intro, status_cb=status_cb, spoken_text=spoken,
                    # Every short opened with the SAME four seconds of the same source clip -
                    # measured at 3.993056s across ten delivered projects. The project name seeds
                    # a small per-short treatment so two uploads never share identical opening
                    # frames, and re-rendering one short reproduces its own intro exactly.
                    variation_seed=project_dir.name, **voice_kw)
            joined = out.with_name(f"{out.stem}_with_hook.mp4")
            prepend_hook_intro(rendered, intro, joined, status_cb=status_cb)
            rendered = joined
            _log(status_cb, f'Hook intro: typed "{typed}" into the search box, '
                            f'read by {state.get("voice") or "the project voice"}.')
        except Exception as exc:      # noqa: BLE001 - the short itself must still be delivered
            _log(status_cb, f"Hook intro skipped ({type(exc).__name__}: {exc}); "
                            "the short was rendered without it.")
    save_state(project_dir, render_pending=False, last_video=Path(rendered).name)
    # Publish AFTER the hook intro, not inside assemble_video: the deliverable is the joined
    # file, and copying the bare render would put the version without the intro in .renders.
    # The subtitles ride along - renders.publish copies a same-stem .srt with the video.
    _publish_with_subtitles(project_dir, rendered, status_cb)
    announce_language_tracks(project_dir, status_cb)
    return rendered


def announce_language_tracks(project_dir, status_cb=None):
    """Say that the extra languages are waiting, without starting them.

    They deliberately do NOT run as part of the render. They are only worth making once you
    have watched the finished video and kept it, and they are the most expensive thing here -
    starting them automatically would spend seven voiceovers on a cut you might discard.
    """
    try:
        state = json.loads((Path(project_dir) / STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not state.get("multilang"):
        return False
    if state.get("language_tracks"):
        _log(status_cb, "Multi-language: tracks already exist for this project.")
        return False
    import multilang
    _log(status_cb, "Multi-language is ON. Watch the video first - then press "
                    "'Generate language tracks' to add "
                    + ", ".join(l["name"] for l in multilang.languages_for()) + ".")
    return True


def build_language_tracks(project_dir, rendered=None, status_cb=None, cancel_event=None,
                          force=False, only=None):
    """Extra spoken tracks for the seven biggest non-English audiences on YouTube.

    Started by hand from the finished-video card, never by the render: this is seven
    translations and seven full voiceovers, which on a twenty-minute explainer is the most
    expensive thing the app can be told to do, and it is only worth spending on a cut you have
    already watched and kept.

    Never raises. The tracks are an addition to a finished deliverable, so a translation problem
    must not be able to lose the video.
    """
    project_dir = Path(project_dir)
    try:
        state = json.loads((project_dir / STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not (state.get("multilang") or force):
        return []
    rendered = Path(rendered) if rendered else project_dir / str(
        state.get("last_video") or f"{project_dir.name}.mp4")
    try:
        import multilang
        import renders
        script = str(state.get("script") or "")
        if not script.strip():
            _log(status_cb, "Multi-language: this project has no script to translate.")
            return []
        languages = multilang.languages_for()
        if only:
            # A partial run: retrying the languages that failed, or producing a subset when the
            # account cannot pay for all seven at once.
            wanted = {str(code).strip().lower() for code in only}
            languages = [l for l in languages if l["code"] in wanted]
        _log(status_cb, f"Multi-language: {len(languages)} extra track(s) - "
                        + ", ".join(l["name"] for l in languages))
        tracks = multilang.build_tracks(
            script, project_dir / "audio", stem=Path(rendered).stem,
            tts_model=str(state.get("tts_model") or "pro"),
            model=str(state.get("reasoning_model") or "") or None,
            languages=languages, status_cb=status_cb, cancel_event=cancel_event,
            tts_options={"voice_instruction": state.get("tts_voice_instruction") or "",
                         "speed": state.get("tts_native_speed") or 1.0,
                         "volume": state.get("tts_volume") or 1.0,
                         "pitch": state.get("tts_pitch") or 0,
                         "sample_rate": state.get("tts_sample_rate") or 24000})
        if tracks:
            multilang.write_manifest(tracks, project_dir / "audio" / "language_tracks.json")
            # The tracks land beside the video they belong to, so an upload is one folder.
            for track in tracks:
                renders.publish(Path(track["audio"]), project=project_dir.name,
                                status_cb=None, sidecars=False)
                renders.publish(Path(track["script"]), project=project_dir.name,
                                status_cb=None, sidecars=False)
                if track.get("srt"):
                    renders.publish(Path(track["srt"]), project=project_dir.name,
                                    status_cb=None, sidecars=False)
            if not only:
                save_state(project_dir, language_tracks=tracks)
            _log(status_cb, f"Multi-language: {len(tracks)} track(s) copied into .renders.")
        return tracks
    except Exception as exc:                                            # noqa: BLE001
        _log(status_cb, f"Multi-language skipped ({type(exc).__name__}: {exc}); "
                        "the video itself is unaffected.")
        return []


def _publish_with_subtitles(project_dir, rendered, status_cb=None):
    """Copy the finished video into .renders together with the project's subtitles.

    The .srt is written once per project under its own name; the render may be a versioned
    ``_v3`` or a ``_with_hook`` join, so the file is placed beside the video under the video's
    stem first. Both then travel together.
    """
    import renders
    rendered = Path(rendered)
    subtitles = subtitle_path(project_dir)
    try:
        if subtitles.is_file() and rendered.with_suffix(".srt") != subtitles:
            shutil.copy2(subtitles, rendered.with_suffix(".srt"))
    except OSError:
        pass
    renders.publish(rendered, project=Path(project_dir).name, status_cb=status_cb)


# ------------------------------------------------------------------ ORCHESTRATOR

def run_longform_video(script, tts_model="pro", reasoning_model=None, reasoning_mode=None,
                       status_cb=None, cancel_event=None, speech_gate=None, resume=True,
                       voice=None, speaker=None, mix_gate=None, mascot=False,
                       halt_after_speech=False, tts_options=None, aspect="16:9",
                       image_zoom=0.0, hook_intro=False, hook_text="",
                       image_motion="push", multilang=False):
    """The whole pipeline. Returns a result dict for the job UI.

    RESUME (default on): re-running the SAME script continues the existing project instead of
    starting over - the voiceover, timings and prompts are reloaded from state.json and only the
    images that are actually missing get generated. The state is keyed on the exact script, so
    editing the script starts a clean run (old images would otherwise land on shifted lines).
    """
    original_script = str(script or "").strip()
    # True once the Google intro has taken the hook line out of the narration: the voiceover
    # must then NOT treat whatever is now first as the hook.
    hook_moved_to_intro = False
    script = clean_narration_script(original_script)
    if script != original_script:
        _log(status_cb, "Removed hidden citation markers before voiceover generation.")
    # A pasted document's own title is not narration. Left in, it was spoken as TTS part 0 with
    # the hash still attached, took a drawing beat of its own, and was chosen as the hook - so
    # the short said its title twice, once in the Google intro and once in the video.
    # The Google intro types the hook AND reads it aloud, so the narration must not open with
    # the same sentence - the delivered short asked its question twice in a row.
    if hook_intro:
        hook_line = hook_line_of(script, hook_text)
        # Say which sentence won BEFORE anything else happens. Silently picking the wrong one is
        # how the search box ended up typing the script's second sentence: nothing in the run
        # ever named the line, so the first sign of it was the finished video.
        source = ("marked by you" if str(hook_text or "").strip()
                  else "from the script opening - mark a hook to choose it yourself")
        _log(status_cb, f"Google intro will type {as_typed_search(hook_line)!r} ({source}).")
        # The shipped opener is one stock clip reused in every short. This is the prompt for
        # generating a fresh one instead, with THIS short's search line already in it. It is
        # produced here, at the very start, so the clip can be generated while the rest of the
        # pipeline runs - not after it.
        try:
            import sketch_hook_intro as _shi
            opener_prompt = _shi.hook_clip_prompt(as_typed_search(hook_line), seed=slug_for(script))
            _log(status_cb, "OPENER PROMPT (generate a ~5s clip from this, then drop it in at "
                            "the timeline step):\n" + opener_prompt)
        except Exception as exc:                                        # noqa: BLE001
            _log(status_cb, f"Opener prompt unavailable ({type(exc).__name__}); "
                            "the built-in intro clip will be used.")
            opener_prompt = ""
        shorter = drop_hook_from_narration(script, hook_line)
        if shorter and shorter != script:
            hook_moved_to_intro = True
            _log(status_cb, f"The Google intro speaks the hook, so the narration starts after "
                            f"it: {hook_line[:60]!r}")
            script = shorter
    without_headings = strip_script_headings(script)
    if without_headings and without_headings != script:
        dropped = len(script.splitlines()) - len(without_headings.splitlines())
        _log(status_cb, f"Dropped {dropped} heading line(s) from the narration - a title is not "
                        f"a spoken line.")
        script = without_headings
    if len(script) < 40:
        raise LongformError("Please paste the full script (at least a few sentences).")

    # The image stage needs the API key, and the check lives up front rather than inside
    # generate_images: a fresh run with no key used to pay for the TTS, the transcription and
    # the prompt calls before failing on a precondition. Now it stops in ~0s having spent nothing.
    if not os.environ.get("WAVESPEED_API_KEY"):
        raise LongformError("WAVESPEED_API_KEY is missing - the voiceover and the images both "
                            "need it.")

    # Format first: everything downstream - the prompt wording, the generator request, the
    # resume checks and the final canvas - reads it, so it has to be set before any of them run.
    size = set_project_aspect(aspect)
    # IMAGE_ASPECT remains for older editor functions, but a production job must keep its own
    # canvas even when another longform job starts while this one is generating frames.
    project_aspect = IMAGE_ASPECT
    zoom = set_image_zoom(image_zoom)
    set_image_motion(image_motion)
    if IMAGE_ASPECT == "9:16":
        _log(status_cb, f"Shorts format: {size[0]}x{size[1]} vertical.")
    if zoom:
        _log(status_cb, f"Image zoom: a {zoom:.0%} slow push-in on every still.")

    slug = slug_for(script)
    out_dir = OUT_ROOT / slug
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "script.txt").write_text(script, encoding="utf-8")
    # Kept beside the project so it survives the job log: the user generates the opener while the
    # rest of the run works, and comes back to it at the editing step.
    if locals().get("opener_prompt"):
        (out_dir / "opener_prompt.txt").write_text(opener_prompt, encoding="utf-8")
    # Read identity BEFORE writing creator options. Otherwise save_state would replace the script
    # key first and make an old voiceover look as though it belonged to newly edited narration.
    state = load_state(out_dir, script) if resume else None
    fresh_identity = state is None
    # Keep the creator choices with the longform project so reopening it restores the
    # same production setup instead of falling back to fresh defaults.
    saved_options = dict(tts_options or {})
    save_state(
        out_dir,
        script=script,
        aspect=project_aspect,
        image_zoom=IMAGE_ZOOM,
        image_motion=IMAGE_MOTION,
        hook_intro=bool(hook_intro),
        hook_text=str(hook_text or ""),
        multilang=bool(multilang),
        tts_model=str(tts_model or "pro"),
        voice=str(voice or ""),
        reasoning_model=str(reasoning_model or ""),
        mascot_enabled=bool(mascot),
        halt_after_speech=bool(halt_after_speech),
        reasoning_mode=str(reasoning_mode or ""),
        tts_voice_instruction=str(saved_options.get("voice_instruction") or ""),
        tts_language=str(saved_options.get("language") or ""),
        tts_native_speed=saved_options.get("speed", 1.0),
        tts_volume=saved_options.get("volume", 1.0),
        tts_pitch=saved_options.get("pitch", 0),
        tts_sample_rate=saved_options.get("sample_rate", 24000),
        tts_output_format=str(saved_options.get("output_format") or "mp3"),
        **({"lines": None, "prompts": None, "audio_duration": 0.0,
            "voiceover_ready": False, "voiceover_file": "voiceover.wav",
            "tts_part_files": [], "tts_part_total": 0}
           if fresh_identity else {}),
    )

    raw_voice_path = out_dir / "voiceover.wav"
    voice_path = voiceover_path_from_state(out_dir, state)
    ffprobe = pipeline.find_ffprobe(pipeline.find_ffmpeg())

    def _probe_duration(path):
        try:
            out = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration",
                                  "-of", "default=nokey=1:noprint_wrappers=1", str(path)],
                                 capture_output=True, text=True, timeout=30)
            return float((out.stdout or "0").strip() or 0.0)
        except Exception:
            return 0.0

    # A different narrator means the saved voiceover is the wrong voice -> regenerate it (and with
    # it the timings), otherwise resume would silently keep the old narrator forever.
    if state and voice and str(state.get("voice") or "") != str(voice):
        _log(status_cb, "Narrator changed - regenerating the voiceover (timings will be redone).")
        state = None
    # The delivery directive is as much part of the performance as the narrator: a voiceover
    # stitched under a different directive is the wrong read, and keeping it would silently undo
    # an edit to TTS_STYLE_LONGFORM forever. (generate_voiceover guards its own reuse the same
    # way; this closes the outer path that skips generate_voiceover entirely.)
    if state and str(state.get("tts_style") or "") != str(pipeline.TTS_STYLE_LONGFORM or ""):
        _log(status_cb, "Delivery directive changed - regenerating the voiceover "
                        "(timings will be redone).")
        state = None
    if state is None:
        voice_path = raw_voice_path

    # If only the derived file vanished, recreate it from the untouched paid TTS. The existing
    # line clock remains valid because the same recorded speed is applied again.
    recorded_speed = float((state or {}).get("voice_speed") or 1.0)
    recorded_name = Path(str((state or {}).get("voiceover_file") or "voiceover.wav")).name
    recorded_path = out_dir / recorded_name
    if (state and abs(recorded_speed - 1.0) >= 0.01 and not _audio_done(recorded_path)
            and _audio_done(raw_voice_path)):
        voice_path = apply_voice_speed(raw_voice_path, recorded_speed, status_cb=status_cb)
        save_state(out_dir, voiceover_file=voice_path.name)

    # Older/interrupted states can already contain timed images while the speed decision is still
    # pending. Do not silently reuse that old clock: ask for speed, transcribe the resulting audio,
    # then rename the same semantic images onto the new clock below.
    explicit_retime = bool(state and state.get("retime_source_lines"))
    speed_retime = bool(state and state.get("lines") and mix_gate is not None
                        and state.get("voice_speed") is None and _audio_done(raw_voice_path))
    old_lines = (list((state or {}).get("retime_source_lines") or []) if explicit_retime
                 else list((state or {}).get("lines") or []) if speed_retime else [])
    old_prompts = (list((state or {}).get("retime_source_prompts") or []) if explicit_retime
                   else list((state or {}).get("prompts") or []) if speed_retime else None)
    old_audio_duration = (float((state or {}).get("retime_source_audio_duration") or 0.0)
                          if explicit_retime else float((state or {}).get("audio_duration") or 0.0))
    reusable = bool(state and state.get("lines") and _audio_done(voice_path) and not speed_retime)
    retimed_prompts = None
    if reusable and int(state.get("scene_clock_version") or 0) != SCENE_CLOCK_VERSION:
        # Re-time locally against the already paid voiceover. The grouping changed, not the read,
        # so buying TTS again would be wasteful; old pictures are archived because their meanings
        # no longer map one-to-one onto the longer visual beats.
        audio_duration = float(state.get("audio_duration") or 0.0) or _probe_duration(voice_path)
        _log(status_cb, "Updating image timing to the calmer First Dog pacing (voiceover reused).")
        lines = transcribe_lines(script, voice_path, status_cb=status_cb,
                                  srt_path=subtitle_path(out_dir))
        _archive_old_images(out_dir / "images", status_cb)
        save_state(out_dir, lines=lines, prompts=None, audio_duration=round(audio_duration, 3),
                   scene_clock_version=SCENE_CLOCK_VERSION)
        state = dict(state)
        state.update({"lines": lines, "prompts": None,
                      "audio_duration": round(audio_duration, 3),
                      "scene_clock_version": SCENE_CLOCK_VERSION})
    if reusable:
        # Reusing the voiceover is what makes resume work at all: fresh TTS would shift every
        # timestamp, which changes every image key and would orphan the images already generated.
        tts_parts = int(state.get("tts_parts") or 0)
        audio_duration = float(state.get("audio_duration") or 0.0) or _probe_duration(voice_path)
        lines = state["lines"]
        _log(status_cb, f"Resume: reusing the existing voiceover + {len(lines)} timed line(s) "
                        "(no TTS, no transcription re-run).")
    else:
        voice_path, tts_parts = generate_voiceover(script, out_dir, tts_model=tts_model,
                                                   status_cb=status_cb, cancel_event=cancel_event,
                                                   speech_gate=speech_gate, voice=voice,
                                                   speaker=speaker, resume=resume,
                                                   mix_gate=mix_gate, tts_options=tts_options,
                                                   short_form=(IMAGE_ASPECT == "9:16"),
                                                   hook_in_intro=hook_moved_to_intro)
        audio_duration = _probe_duration(voice_path)
        lines = transcribe_lines(script, voice_path, status_cb=status_cb,
                                  srt_path=subtitle_path(out_dir))
        if (speed_retime or explicit_retime) and len(old_lines) == len(lines):
            retimed_prompts = retime_longform_assets(
                out_dir, old_lines, lines, old_audio_duration, audio_duration,
                prompts=old_prompts, status_cb=status_cb)
        elif explicit_retime:
            _log(status_cb, "The regenerated speech produced a different line count; existing "
                            "images cannot be mapped safely and will be regenerated.")
        save_state(out_dir, script=script, lines=lines, tts_parts=tts_parts, voice=voice or "",
                   prompts=retimed_prompts,
                   audio_duration=round(audio_duration, 3),
                   scene_clock_version=SCENE_CLOCK_VERSION,
                   retime_source_lines=None, retime_source_prompts=None,
                   retime_source_audio_duration=0.0)
    transcript_path = write_transcript(lines, out_dir / "transcript.txt")
    _log(status_cb, f"Transcript written: {transcript_path.name}")

    prompts = (state or {}).get("prompts") if reusable else retimed_prompts
    # Retiming rewrites timestamps but not prompt semantics/format. Preserve the saved format
    # marker or the normal stale-format guard would archive every reusable image immediately
    # after a speech-only regeneration.
    cached_fmt = ((state or {}).get("prompt_format")
                  if (reusable or retimed_prompts is not None) else None)
    prompt_alignment_bad = bool(prompts) and (
        len(prompts) != len(lines) or any(
            not isinstance(prompt, dict)
            or prompt.get("timestamp") != fmt_ts(lines[i]["start"])
            for i, prompt in enumerate(prompts[:len(lines)])
        )
    )
    stale_format = bool(prompts) and cached_fmt != PROMPT_FORMAT_VERSION
    if prompts and len(prompts) == len(lines) and not stale_format and not prompt_alignment_bad:
        _log(status_cb, f"Resume: reusing the {len(prompts)} saved image prompt(s).")
    else:
        if stale_format:
            # The saved prompts predate the current doodle-prompt format (e.g. before the
            # mandatory ALL-CAPS top caption). Regenerate ALL prompts, and move the images made
            # from the old prompts aside so they are regenerated in the new style too.
            _log(status_cb, "Image prompts are an older format - regenerating all prompts in the "
                            "new caption style (and the images made from them).")
            _archive_old_images(out_dir / "images", status_cb)
        elif prompt_alignment_bad:
            _log(status_cb, "Saved image prompts are not aligned to the current transcript - "
                            "regenerating prompts and their images.")
            first_bad = next(
                (i for i, prompt in enumerate(prompts[:len(lines)])
                 if not isinstance(prompt, dict)
                 or prompt.get("timestamp") != fmt_ts(lines[i]["start"])),
                min(len(prompts), len(lines)),
            )
            _archive_images_from_index(out_dir / "images", first_bad, status_cb)
        prompt_checkpoint = out_dir / "image_prompts_checkpoint.json"
        prompts = generate_image_prompts(lines, reasoning_model=reasoning_model,
                                         status_cb=status_cb, cancel_event=cancel_event,
                                         checkpoint_path=prompt_checkpoint, mascot=mascot,
                                         aspect=project_aspect)
        save_state(out_dir, script=script, lines=lines, prompts=prompts, tts_parts=tts_parts,
                   voice=voice or "", audio_duration=round(audio_duration, 3),
                   prompt_format=PROMPT_FORMAT_VERSION,
                   scene_clock_version=SCENE_CLOCK_VERSION)
        prompt_checkpoint.unlink(missing_ok=True)
    prompts_path = write_prompts_file(prompts, out_dir / f"image_prompts_{slug}.txt")
    _log(status_cb, f"Prompt file written: {prompts_path.name}")

    durations = line_durations(lines, audio_duration)
    results = generate_images(prompts, lines, durations, out_dir / "images",
                              status_cb=status_cb, cancel_event=cancel_event, mascot=mascot,
                              aspect=project_aspect)

    # A prompt-repair run temporarily archives the prior frames before asking the provider for
    # replacements.  Do not write an editor timeline with holes when a handful of those calls
    # time out: recover the original, same-index artwork first and refresh the result map that
    # verify_images()/timeline.json consume.  This is deliberately before verification so a
    # project cannot report hundreds of healthy PNGs as "missing" merely because they were in
    # the prompt-repair archive at the moment the provider stopped responding.
    restored = recover_archived_images(out_dir, status_cb=status_cb)
    if restored:
        for idx, line in enumerate(lines):
            if results.get(idx):
                continue
            candidate = resolve_image_for(out_dir / "images", idx, line, durations[idx],
                                          project_aspect)
            if candidate and _image_done(candidate, project_aspect):
                results[idx] = str(candidate)
        _log(status_cb, f"Image recovery filled {restored} interrupted prompt-repair slot(s) before review.")

    # Thumbnail generation is user-triggered from the pre-render editor. It deliberately is not
    # hidden inside the already long image run: the editor always exposes Generate thumbnails,
    # and one click produces three title-paired options. Reuse a prior selection on resume.
    thumbnail_path = out_dir / "thumbnail.png"
    thumbnail = str(thumbnail_path) if _image_done(thumbnail_path, IMAGE_ASPECT) else ""

    missing = verify_images(lines, results, reasoning_model=reasoning_model, status_cb=status_cb)
    latest_state = load_state(out_dir, script) or {}
    # Assembly cuts follow the locally forced-aligned word containing each image's caption phrase,
    # keeping the visual and its baked-in caption synchronized with the narration.
    cut_durations = caption_cut_durations(lines, prompts, audio_duration)
    timeline_path = write_timeline_manifest(
        lines, cut_durations, results, audio_duration, out_dir / "timeline.json",
        voice_speed=latest_state.get("voice_speed") or 1.0)
    # The first assembly is intentionally deferred. The creator must see and approve the real
    # image/voiceover timeline first; rendering here used to lock mistakes into a slow MP4 before
    # the user had any chance to move frames or recover unused generations.
    save_state(out_dir, render_pending=True, reasoning_model=str(reasoning_model or ""))
    return {
        "project_dir": str(out_dir), "voiceover": str(voice_path),
        "transcript": str(transcript_path), "prompts_file": str(prompts_path),
        "timeline": str(timeline_path),
        "images_done": sum(1 for v in results.values() if v), "images_total": len(lines),
        "missing_images": [fmt_ts(lines[i]["start"]) for i in missing],
        "tts_parts": tts_parts, "audio_duration": round(audio_duration, 2),
        "thumbnail": thumbnail or "", "render_pending": True,
        "open_longform_editor": True,
    }
