"""Put the Short's own hook into the "someone googles it" intro clip.

The source clip is ten seconds of hands typing, with a Google page on the monitor and the words
INSERT CUSTOM SEARCH QUERY HERE appearing letter by letter in the search box. To make it usable
for every Short, that placeholder is replaced by the hook the user marked - typed at the same
pace, in the same place.

The hard part is that the camera pushes in: over the clip the page drifts eight pixels left,
twenty-five up, and grows nine percent. A fixed overlay would slide off the box within a second.
So the Google logo - high contrast, unchanging, and directly above the box - is tracked by
template match every frame, and the search box is derived from it. Match confidence runs
0.96-1.00 across the clip, and a frame that ever fell below is filled in from its neighbours
rather than guessed at.

Nothing here is specific to one clip beyond the geometry constants, which were measured, not
assumed: at the tracked reference scale the box sits at x -52..+221 and y +56..+99 relative to
the logo's top-left corner.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import tempfile
from pathlib import Path

try:
    import cv2
    import numpy as np
except ImportError:                                                     # noqa: BLE001
    cv2 = None
    np = None

import pipeline

# Measured on the reference clip at t=0.40s, where the tracker scores an exact 1.000 match.
LOGO_BOX = (265, 300, 190, 55)          # x, y, w, h of the Google logo template
PILL_DX0, PILL_DX1 = -52, 221           # search box left/right, relative to the logo's x
PILL_DY0, PILL_DY1 = 56, 99             # search box top/bottom, relative to the logo's y
# Measured, not guessed: the coloured lens/mic icons occupy x 452..485 of a box ending at 486,
# so 38px is exactly what must stay untouched. The first attempt reserved 62 and left the tail of
# the placeholder ("...RE") on screen; covering from 10px in left its head ("IN") behind.
ICON_RESERVE = 38                       # the mic and lens icons live at the box's right end
COVER_INSET = 2                         # painting starts at the box's inner edge
TEXT_INSET = 12                         # but the new text is written with normal padding

TYPE_START_S, TYPE_END_S = 0.55, 9.40   # when the placeholder starts and finishes appearing
# A Short cannot spend eight seconds on its own intro. The typing is re-timed to land inside
# this window and the clip is cut just after, leaving a short beat on the finished query.
# The intro is glued in front of the short, so it has to sit at the SAME loudness as the
# narration that follows it. At -15 it arrived 5.4 dB louder than the video (measured: intro
# -14.9 LUFS, short -20.3), which is a jolt on the first word of every video - and the first
# word is the one that decides whether anyone keeps watching.
HOOK_TARGET_LUFS = -20.0

TARGET_SECONDS = 4.0
TAIL_HOLD_S = 0.45                      # how long the completed query stays on screen
TEXT_RGB = (70, 70, 74)
DEFAULT_SPEED = 1.2

# The intro ends by flying INTO the query it just typed. A slow creep for most of the clip, then
# the last fraction of a second accelerates into a blur - the cut to the first drawing lands
# inside that blur, so the intro hands over instead of stopping.
ZOOM_CREEP = 1.08          # how far the slow push gets before the rush starts
ZOOM_PEAK = 30.0           # the frame is a few pixels wide by the last frame
ZOOM_MIN_RUSH_S = 0.35     # the rush is never so short that it reads as a glitch cut


class HookIntroError(RuntimeError):
    """The intro could not be built. Carries a message for the user, not a stack trace."""


def available():
    return cv2 is not None and np is not None


def _decode(video, ffmpeg):
    """Every frame of the clip as BGR arrays, plus its size and rate."""
    probe = subprocess.run(
        [str(pipeline.find_ffprobe(ffmpeg)), "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,r_frame_rate,nb_frames",
         "-of", "default=nw=1", str(video)],
        capture_output=True, text=True, timeout=120).stdout
    info = dict(line.split("=", 1) for line in probe.strip().splitlines() if "=" in line)
    width, height = int(info.get("width", 0)), int(info.get("height", 0))
    num, _, den = str(info.get("r_frame_rate", "24/1")).partition("/")
    fps = float(num) / float(den or 1)
    if not width or not height:
        raise HookIntroError("Could not read the hook clip's size.")
    raw = subprocess.run(
        [str(ffmpeg), "-v", "error", "-i", str(video), "-f", "rawvideo",
         "-pix_fmt", "bgr24", "-"], capture_output=True, timeout=900).stdout
    stride = width * height * 3
    frames = [np.frombuffer(raw[i * stride:(i + 1) * stride], dtype=np.uint8)
              .reshape(height, width, 3).copy()
              for i in range(len(raw) // stride)]
    if not frames:
        raise HookIntroError("The hook clip decoded to no frames.")
    return frames, width, height, fps


def track_page(frames):
    """(dx, dy, scale) per frame, from the Google logo. Returns None where the match is weak."""
    x, y, w, h = LOGO_BOX
    template = cv2.cvtColor(frames[0][y:y + h, x:x + w], cv2.COLOR_BGR2GRAY)
    track = []
    for frame in frames:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        best = (0.0, None, None)
        for scale in np.arange(0.88, 1.30, 0.02):
            sized = cv2.resize(template, None, fx=float(scale), fy=float(scale))
            if sized.shape[0] >= gray.shape[0] or sized.shape[1] >= gray.shape[1]:
                continue
            score = cv2.matchTemplate(gray, sized, cv2.TM_CCOEFF_NORMED)
            _, top, _, where = cv2.minMaxLoc(score)
            if top > best[0]:
                best = (float(top), where, float(scale))
        track.append(best if best[0] >= 0.80 else None)
    if all(entry is None for entry in track):
        raise HookIntroError("The hook clip's search page could not be tracked.")
    # A weak frame borrows from its neighbours rather than jumping the overlay.
    last = next(entry for entry in track if entry)
    for i, entry in enumerate(track):
        if entry is None:
            track[i] = last
        else:
            last = entry
    return track


def _box_for(entry):
    _score, (lx, ly), scale = entry
    x0 = int(round(lx + PILL_DX0 * scale))
    x1 = int(round(lx + PILL_DX1 * scale))
    y0 = int(round(ly + PILL_DY0 * scale))
    y1 = int(round(ly + PILL_DY1 * scale))
    return x0, y0, x1, y1, scale


def _zoom_target(track, width, height):
    """Where the push-in aims: the middle of the tracked search box.

    Taken as a median across the clip, because the camera must not inherit the tracker's
    frame-to-frame jitter - at 30x a two-pixel wobble is a sixty-pixel lurch.
    """
    centres = [_box_for(entry) for entry in track if entry]
    if not centres:
        return width / 2.0, height / 2.0
    return (float(np.median([(b[0] + b[2]) / 2.0 for b in centres])),
            float(np.median([(b[1] + b[3]) / 2.0 for b in centres])))


def _zoom_factor(seconds, span, rush_from, creep, peak):
    """Slow creep up to `rush_from`, then an accelerating flight to `peak` by `span`.

    The rush is geometric rather than linear: a constant multiplier per second is what reads as
    constant zoom speed, so raising the progress to a power on top of that is what makes it
    visibly accelerate instead of merely moving fast.
    """
    creep, peak = max(1.0, float(creep)), max(1.0, float(peak))
    if peak <= 1.0001:
        return 1.0
    rush_from = min(max(0.0, float(rush_from)), max(0.0, span - 0.05))
    if seconds <= rush_from:
        return 1.0 + (creep - 1.0) * (seconds / rush_from if rush_from > 0 else 1.0)
    rush = max(1e-6, span - rush_from)
    progress = min(1.0, (seconds - rush_from) / rush)
    return creep * (peak / creep) ** (progress ** 2.2)


def _zoom_frame(frame, factor, target):
    """Crop towards `target` by `factor` and blow it back up to full size.

    The crop drifts from the centre of the frame to the target as the zoom grows, so the early
    creep stays centred and only the flight commits to the search box.
    """
    if factor <= 1.0001:
        return frame
    height, width = frame.shape[:2]
    crop_w = max(8, int(round(width / factor)))
    crop_h = max(8, int(round(height / factor)))
    pull = min(1.0, (factor - 1.0) / 3.0)
    cx = width / 2.0 + (float(target[0]) - width / 2.0) * pull
    cy = height / 2.0 + (float(target[1]) - height / 2.0) * pull
    x = int(round(min(max(0.0, cx - crop_w / 2.0), width - crop_w)))
    y = int(round(min(max(0.0, cy - crop_h / 2.0), height - crop_h)))
    return cv2.resize(frame[y:y + crop_h, x:x + crop_w], (width, height),
                      interpolation=cv2.INTER_CUBIC)


def _typed_share(seconds, start, end):
    if seconds <= start:
        return 0.0
    if seconds >= end:
        return 1.0
    return (seconds - start) / (end - start)


def _speak_hook(text, dest, voice, tts_model, ffmpeg, status_cb=None,
                voice_instruction=None, tts_speed=1.0):
    """The hook line, read aloud, for the intro's own audio bed.

    It is the SAME sentence the short opens with, so it is read by the short's own narrator with
    the short's own hook delivery. Left to the defaults it came out on Gemini in a stranger's
    voice in front of a Seed-voiced short - which is exactly what a viewer notices first.
    """
    try:
        produced = pipeline.generate_speech_gemini(
            text, dest, voice=voice or None,
            model=tts_model or pipeline.DEFAULT_TTS_MODEL,
            style=pipeline.TTS_STYLE_SKETCH_SHORT_HOOK,
            voice_instruction=voice_instruction or None,
            tts_speed=float(tts_speed or 1.0),
            status_cb=None, output_format="wav")
        return Path(produced) if produced else None
    except Exception as exc:                                            # noqa: BLE001
        if status_cb:
            status_cb(f"Hook intro: no voiceover this run ({type(exc).__name__}).")
        return None


# Every sketch short opened with the SAME four seconds. Measured 2026-08-30 across ten delivered
# projects: `hook_intro.mp4` was 3.993056s in every single one, cut from the one source clip at
# assets/hook_intros/google_search.mp4, with only the typed query differing. Ten uploads that
# begin with byte-identical footage are ten uploads a platform can match to each other.
#
# So the intro gets a small per-project treatment: a different crop window, a slightly different
# grade and a slightly different length. It is seeded by the PROJECT NAME, so it is stable - a
# re-render of the same short reproduces the same intro - while two different shorts never get
# the same one. Every range is deliberately small enough that the intro still looks like itself.
# ---------------------------------------------------------------- generated opener prompt
#
# The shipped opener is one stock clip reused in every short - measured at 3.993056s in all ten
# delivered projects, the same frames every time. Rather than disguise that, the run now hands
# the user a PROMPT so the opener can be generated fresh per video. The typed search line is
# baked in, so the screen shows this short's own question.
#
# Written for a director-style video model: labelled sections, one continuous shot, no model
# name, no duration and no aspect ratio in the text - those belong in the generation controls,
# never in the prompt.
# The aesthetic is not invented: it is measured off assets/hook_intros/google_search.mp4, the
# clip every short currently opens with. Pink plush desk mat, pastel keycaps with a mint
# spacebar and one brass knob, a warm LED strip under a light wooden riser, a magenta-to-peach
# gradient wash on the wall, a moon lamp, tulips in a white ceramic vase, a cream knit sleeve,
# and a soft hazy bloom over the whole frame.
#
# The rotations stay INSIDE that look. The first version wandered to oak desks and grey mats and
# came back reading generic - which is the whole complaint it exists to answer.
_SURFACE = ("a soft blush-pink plush desk mat with visible fuzzy texture",
            "a pale lilac plush desk mat with a soft fuzzy pile",
            "a cream-pink velvet desk mat with a gentle sheen",
            "a powder-pink fluffy desk mat, deep and soft under the keyboard")
_KEYBOARD = ("a compact pastel mechanical keyboard - lilac and cream keycaps, a mint-green "
             "spacebar row, a few peach accent keys and one small brass rotary knob",
             "a compact pastel mechanical keyboard - cream and lavender keycaps, a soft blue "
             "accent row, peach modifier keys and a single gold knob",
             "a compact pastel mechanical keyboard - white and mint keycaps, lilac modifiers, "
             "a coral escape key and a brushed brass knob",
             "a compact pastel mechanical keyboard - blush and cream keycaps, a sage-green "
             "spacebar, lavender accents and one small gold dial")
_PROPS = ("a round white moon lamp glowing softly on the left, a small green succulent in a "
          "pale ceramic pot, and pink tulips in a matte white vase on the right",
          "a round frosted moon lamp on the left, a trailing green plant in a blush ceramic "
          "pot, and soft pink peonies in a matte white vase on the right",
          "a glowing spherical lamp on the left, a small monstera cutting in a pale pot, and "
          "dried pampas stems in a cream vase on the right",
          "a soft globe lamp on the left, a tiny succulent in a speckled ceramic pot, and "
          "white-and-pink ranunculus in a matte vase on the right")
_GLOW = ("a smooth magenta-to-peach gradient washing across the wall behind the monitor, warm "
         "amber LED strip light spilling from under the riser onto the desk",
         "a soft lilac-to-warm-cream gradient glowing on the wall behind, a warm LED strip "
         "under the riser pooling light on the mat",
         "a gentle pink-to-lavender ambient wash on the back wall, warm strip lighting beneath "
         "the monitor riser catching the desk surface",
         "a hazy peach-to-violet glow across the wall behind the screen, a warm under-shelf LED "
         "strip grazing the desk from beneath the riser")
_CAMERA = ("a locked-off top-down-angled shot looking past the hands to the screen, with an "
           "almost imperceptible push in",
           "a slightly high straight-on shot with the keyboard across the lower third, a very "
           "slow drift toward the screen",
           "a gentle overhead-tilted angle taking in the mat, the keyboard and the monitor, "
           "creeping closer",
           "a soft three-quarter angle from just behind the right hand, holding steady with a "
           "barely visible push in")
_HANDS = ("a pair of adult hands in a soft cream knit sleeve, short unpainted nails",
          "a pair of adult hands in an oversized cream knit sleeve pushed to the wrist",
          "a pair of adult hands in a soft ivory sweater sleeve, neat short nails",
          "a pair of adult hands in a pale blush knit sleeve, short bare nails")


def hook_clip_prompt(typed_query, seed=None):
    """A text-to-video prompt for THIS short's opener, with its own search line baked in.

    Roughly five seconds of screen time: the hands type the question and stop. One continuous
    shot on purpose - a cut inside the opener would fight the cut into the short itself.

    No model name, no duration and no aspect ratio in the text: those belong in the generation
    controls, and a video model that reads them in the prompt tends to render them.
    """
    query = " ".join(str(typed_query or "").split())
    digest = hashlib.sha1(("%s|%s" % (seed or "", query)).encode("utf-8", "ignore")).hexdigest()

    def pick(options, index):
        return options[int(digest[index * 2:index * 2 + 2], 16) % len(options)]

    lines = [
        "Setting: a cosy pastel desk setup filmed from just behind the keyboard - "
        + pick(_SURFACE, 0) + ", " + pick(_KEYBOARD, 1)
        + ", and a slim monitor raised on a light wooden riser. Behind and around it, "
        + pick(_PROPS, 2)
        + ". The monitor fills the upper half of the frame and shows a clean browser on the "
          "Google home page: the coloured Google wordmark centred above an empty rounded search "
          "box on a plain white page, no other tabs or windows.",
        "Subject: " + pick(_HANDS, 3) + ", resting on the keyboard as the shot opens.",
        "Action: the hands are typing FAST from the first frame - quick, confident, practised "
        "touch-typing, fingers moving rapidly across the keys without pausing, hesitating or "
        "lifting away. The search box fills in quickly with the words \"" + query + "\", the "
        "text keeping up with the keystrokes and the cursor blinking at the end of the line. "
        "The typing finishes just before the shot ends and the hands settle, still on the keys. "
        "Nothing is clicked and no results page appears.",
        "Camera: " + pick(_CAMERA, 4) + ".",
        "Lighting: " + pick(_GLOW, 5)
        + ". Soft, diffused and dreamy, with a gentle bloom around the bright screen and the "
          "lamp, and a shallow, creamy background blur.",
        "Style: photographic, filmed on a phone, soft pastel colour palette throughout - blush "
        "pink, lilac, mint and warm cream - high-key and airy, gentle haze, smooth bokeh, "
        "realistic skin and fabric texture. Cosy aesthetic desk-setup look. No text overlays, "
        "no captions, no watermarks, and no logos other than the Google wordmark on the screen.",
        "Audio: ASMR-quality mechanical keyboard typing, close-miked and loud in the mix - a "
        "fast, dense run of crisp tactile clacks with a deep thock on every keypress, audible "
        "key travel and bottom-out, the faint rustle of a knit sleeve against the desk mat, "
        "over a near-silent room tone. Dry and detailed, no reverb. The rapid typing is the "
        "main sound of the shot. No music and no voice.",
    ]
    return chr(10).join(lines)


def intro_variation(seed):
    """Small, stable, per-project differences for the shared intro clip.

    Every parameter is drawn from its OWN slice of the digest. Deriving them all from one
    `hash % 12` looked varied but was not: with a dozen buckets two projects out of four already
    landed on byte-identical settings, which is the thing this exists to prevent.
    """
    digest = hashlib.sha1(str(seed or "").encode("utf-8", "ignore")).hexdigest()

    def slice_at(index, span):
        return int(digest[index * 4:index * 4 + 4], 16) % span

    return {
        # at most ~2% off the edges, panned a little differently each time
        "crop": 0.978 + slice_at(0, 9) * 0.0025,
        "pan_x": (slice_at(1, 9) - 4) * 0.0018,
        "pan_y": (slice_at(2, 9) - 4) * 0.0018,
        # a grade nobody would call a filter, but no two are identical
        "saturation": 0.955 + slice_at(3, 11) * 0.009,
        "gamma": 0.975 + slice_at(4, 11) * 0.005,
        "hue": (slice_at(5, 13) - 6) * 0.55,
        # a different number of frames, so the DURATIONS stop matching too - all ten delivered
        # shorts were 3.993056s to the microsecond
        "trim_frames": slice_at(6, 7),
    }


def variation_filter(variation):
    """The ffmpeg video filter for one variation, or "" when there is nothing to do."""
    if not variation:
        return ""
    crop = float(variation.get("crop") or 1.0)
    parts = []
    if crop < 0.999:
        # trunc(), not iw/2*2: ffmpeg expression arithmetic is floating point, so `iw/2*2` is
        # simply iw again and the crop happily produced 713x1268 - libx264 then refused the
        # whole encode with "width not divisible by 2" and the intro never built.
        width = f"trunc(iw*{crop:.4f}/2)*2"
        height = f"trunc(ih*{crop:.4f}/2)*2"
        x = f"trunc(((iw-out_w)/2+(iw*{float(variation.get('pan_x') or 0.0):.4f}))/2)*2"
        y = f"trunc(((ih-out_h)/2+(ih*{float(variation.get('pan_y') or 0.0):.4f}))/2)*2"
        parts.append(f"crop={width}:{height}:{x}:{y}")
    parts.append("eq=saturation=%.4f:gamma=%.4f" % (float(variation.get("saturation") or 1.0),
                                                    float(variation.get("gamma") or 1.0)))
    hue = float(variation.get("hue") or 0.0)
    if abs(hue) > 0.05:
        parts.append(f"hue=h={hue:.3f}")
    return ",".join(parts)


def voice_over_opener(source, out_path, spoken_text=None, status_cb=None, ffmpeg=None,
                      voice=None, tts_model=None, voice_instruction=None, tts_speed=1.0):
    """Put the short's own narrator over an opener the user generated elsewhere.

    The generated clip already SHOWS the search line - it was made from a prompt containing it -
    so nothing is drawn on top here. Only the hook is spoken over it, at the same loudness the
    built-in intro is normalised to, so the cut into the short does not jump.

    The clip's own audio (keystrokes, room tone) is kept underneath at a low level; a silent
    opener reads as a stall before the video starts.
    """
    log = status_cb or (lambda _m: None)
    ffmpeg = str(ffmpeg or pipeline.find_ffmpeg() or "")
    source, out_path = Path(source), Path(out_path)
    if not source.is_file():
        raise HookIntroError("The generated opener clip is missing: %s" % source)
    work = Path(tempfile.mkdtemp(prefix="opener_"))
    try:
        spoken = None
        say = " ".join(str(spoken_text or "").split())
        if say:
            spoken = _speak_hook(say, work / "hook_voice.wav", voice, tts_model, ffmpeg, log,
                                 voice_instruction=voice_instruction, tts_speed=tts_speed)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        if spoken and Path(spoken).is_file():
            # 0.28 is the level the STOCK intro uses, where the keyboard is incidental texture
            # under the narration. A generated opener is made FOR its typing sound, so the bed
            # comes up to sit with the voice instead of under it.
            graph = ("[0:a]aformat=sample_rates=48000:channel_layouts=stereo,volume=0.62[bed];"
                     "[1:a]aformat=sample_rates=48000:channel_layouts=stereo,"
                     "adelay=120|120,volume=1.0[vo];"
                     "[bed][vo]amix=inputs=2:normalize=0:dropout_transition=0,"
                     "loudnorm=I=%s:TP=-1.5:LRA=11[a]" % HOOK_TARGET_LUFS)
            command = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                       "-i", str(source), "-i", str(spoken),
                       "-filter_complex", graph, "-map", "0:v", "-map", "[a]", "-shortest"]
        else:
            # No narration available: keep the clip's own sound rather than delivering silence.
            log("Opener: no hook voiceover this run; keeping the clip's own audio.")
            command = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                       "-i", str(source), "-map", "0:v", "-map", "0:a?"]
        done = subprocess.run(
            command + ["-c:v", "libx264", "-preset", "medium", "-crf", "18",
                       "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", str(out_path)],
            capture_output=True, text=True, timeout=1800)
        if not out_path.is_file() or out_path.stat().st_size < 10000:
            raise HookIntroError("Could not prepare the generated opener: %s"
                                 % (done.stderr or "")[-300:])
        log("Opener ready: %s" % out_path.name)
        return out_path
    finally:
        shutil.rmtree(work, ignore_errors=True)


def build_hook_intro(source, hook_text, out_path, speed=DEFAULT_SPEED, ffmpeg=None,
                     status_cb=None, target_seconds=TARGET_SECONDS, voice=None,
                     tts_model=None, zoom_peak=ZOOM_PEAK, zoom_creep=ZOOM_CREEP,
                     voice_instruction=None, tts_speed=1.0, spoken_text=None,
                     variation_seed=None):
    """Write a copy of `source` with `hook_text` typed into its search box.

    `target_seconds` is the finished length. The source runs ten seconds and types its
    placeholder across nine of them, which is far too slow to open a Short - so the typing is
    re-timed to finish just before the cut and the clip is trimmed there.
    """
    log = status_cb or (lambda _m: None)
    if not available():
        raise HookIntroError("OpenCV is required to build the hook intro.")
    from PIL import Image, ImageDraw, ImageFont
    ffmpeg = str(ffmpeg or pipeline.find_ffmpeg() or "")
    if not ffmpeg:
        raise HookIntroError("ffmpeg was not found.")
    source, out_path = Path(source), Path(out_path)
    if not source.is_file():
        raise HookIntroError(f"The hook clip is missing: {source}")
    text = " ".join(str(hook_text or "").split()).upper()
    if not text:
        raise HookIntroError("There is no hook text to type into the intro.")

    log("Hook intro: decoding the clip...")
    frames, width, height, fps = _decode(source, ffmpeg)
    # Keep only the frames the finished intro needs, at the requested playback speed.
    keep = max(12, int(round(float(target_seconds) * float(speed or 1.0) * fps)))
    frames = frames[:keep]
    span = len(frames) / float(fps)
    type_from = min(0.35, span * 0.10)
    type_until = max(type_from + 0.4, span - TAIL_HOLD_S * float(speed or 1.0))
    log(f"Hook intro: {len(frames)} frames kept -> {span / float(speed or 1.0):.1f}s "
        f"at {float(speed or 1.0):g}x; typing finishes at "
        f"{type_until / float(speed or 1.0):.1f}s.")
    log(f"Hook intro: tracking the page across {len(frames)} frames...")
    track = track_page(frames)

    # The flight starts the instant the hook is fully typed: the viewer gets to read the finished
    # query, and then the intro leaves. Anything shorter than a third of a second reads as a
    # glitch rather than a move, so a very tight tail borrows time from the creep.
    rate_real = float(fps) * float(speed or 1.0)
    span_real = len(frames) / rate_real
    rush_from = min(type_until / float(speed or 1.0), span_real - ZOOM_MIN_RUSH_S)
    zoom_to = _zoom_target(track, width, height)
    if float(zoom_peak or 0) > 1.0:
        log(f"Hook intro: push-in creeps to {float(zoom_creep):g}x, then flies to "
            f"{float(zoom_peak):g}x over the last {span_real - rush_from:.2f}s.")

    def write_frame(index, frame):
        factor = _zoom_factor(index / rate_real, span_real, rush_from,
                              zoom_creep, zoom_peak)
        cv2.imwrite(str(out_frames / f"{index:05d}.png"),
                    _zoom_frame(frame, factor, zoom_to))

    font_path = pipeline.FONT_REGULAR or pipeline.FONT_BOLD
    work = Path(tempfile.mkdtemp(prefix="hookintro_"))
    try:
        out_frames = work / "f"
        out_frames.mkdir()
        for index, (frame, entry) in enumerate(zip(frames, track)):
            x0, y0, x1, y1, scale = _box_for(entry)
            right = x1 - int(round(ICON_RESERVE * scale))
            cover_left = x0 + int(round(COVER_INSET * scale))
            left = x0 + int(round(TEXT_INSET * scale))
            if right <= left or y1 <= y0:
                write_frame(index, frame)
                continue
            # Paint the placeholder out with the box's OWN colour, sampled from a clean strip
            # just inside its top edge, so the patch matches the screen's changing light.
            patch = frame[max(0, y0 + 2):y0 + 6, cover_left:right]
            fill = (patch.reshape(-1, 3).mean(axis=0) if patch.size
                    else np.array([246, 246, 246], dtype=np.float32))
            frame[y0 + 3:y1 - 3, cover_left:right] = fill.astype(np.uint8)

            share = _typed_share(index / float(fps), type_from, type_until)
            shown = text[:max(0, int(round(len(text) * share)))]
            if shown or share > 0:
                image = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
                draw = ImageDraw.Draw(image)
                size = max(8, int(round((y1 - y0) * 0.42)))
                font = (ImageFont.truetype(font_path, size) if font_path
                        else ImageFont.load_default())
                # Shrink to fit rather than letting a long hook run under the icons.
                while font_path and size > 8 and draw.textlength(shown, font=font) > (right - left):
                    size -= 1
                    font = ImageFont.truetype(font_path, size)
                baseline = y0 + (y1 - y0 - size) // 2 - int(round(2 * scale))
                draw.text((left, baseline), shown, font=font, fill=TEXT_RGB)
                if share < 1.0 and (index // max(1, int(fps / 3))) % 2 == 0:
                    caret = left + int(draw.textlength(shown, font=font)) + 2
                    draw.line([(caret, y0 + 8), (caret, y1 - 8)], fill=TEXT_RGB, width=2)
                frame = cv2.cvtColor(np.array(image), cv2.COLOR_RGB2BGR)
            write_frame(index, frame)

        # The hook is SPOKEN over the typing - a silent intro reads as a stall before the video
        # starts. The keyboard stays underneath at a low level so the typing still feels real.
        # What is TYPED and what is SPOKEN are no longer the same string: the box shows a real
        # search query ("why does my stomach growl"), the narrator reads the script's sentence.
        say = " ".join(str(spoken_text or "").split()) or (text.title() if text.isupper() else text)
        spoken = _speak_hook(say,
                             work / "hook_voice.wav", voice, tts_model, ffmpeg, log,
                             voice_instruction=voice_instruction, tts_speed=tts_speed)
        log(f"Hook intro: encoding at {speed:g}x...")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        rate = float(fps) * float(speed or 1.0)
        tempo = min(2.0, max(0.5, float(speed or 1.0)))
        inputs = ["-framerate", f"{rate:.4f}", "-i", str(out_frames / "%05d.png"),
                  "-i", str(source)]
        if spoken and Path(spoken).is_file():
            inputs += ["-i", str(spoken)]
            graph = (f"[1:a]atempo={tempo:.4f},volume=0.28[keys];"
                     "[2:a]aformat=sample_rates=48000:channel_layouts=stereo,"
                     "adelay=120|120,volume=1.0[vo];"
                     "[keys][vo]amix=inputs=2:normalize=0:dropout_transition=0,"
                     f"loudnorm=I={HOOK_TARGET_LUFS}:TP=-1.5:LRA=11[a]")
        else:
            graph = f"[1:a]atempo={tempo:.4f}[a]"
        variation = intro_variation(variation_seed) if variation_seed else None
        vfilter = variation_filter(variation)
        if variation and variation.get("trim_frames"):
            # Drop a few frames off the END: the typing is finished by then, so the intro loses
            # nothing, and the finished files stop sharing one exact duration.
            total = len(list(out_frames.glob("*.png")))
            keep = max(1, total - int(variation["trim_frames"]))
            trim_frames_arg = ["-frames:v", str(keep)] if keep < total else []
        else:
            trim_frames_arg = []
        if vfilter:
            graph = f"[0:v]{vfilter}[v];" + graph
            log("Hook intro: per-project variation applied so two uploads never share the "
                "same opening frames.")
        done = subprocess.run(
            [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", *inputs,
             "-filter_complex", graph,
             "-map", "[v]" if vfilter else "0:v", "-map", "[a]", "-shortest",
             *trim_frames_arg,
             "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-b:a", "192k", str(out_path)],
            capture_output=True, text=True, timeout=1800)
        if not out_path.is_file() or out_path.stat().st_size < 10000:
            raise HookIntroError(f"Encoding the hook intro failed: "
                                 f"{(done.stderr or '')[-300:]}")
        log(f"Hook intro ready: {out_path.name}")
        return out_path
    finally:
        shutil.rmtree(work, ignore_errors=True)
