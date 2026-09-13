"""Action Edit - three raw AI action clips in, one finished vertical short out.

The source material is AI-generated action-sports footage: three ~10 s vertical clips per
Short, each carrying an internal hard cut (the generating prompt said "Cut to:"), so a Short is
really about six visual beats rather than three. This module turns that into a platform-ready
cut with a consistent series look, original action sound and deliberate pacing.

Two properties drive most of the design. It runs over a whole series - 21 Shorts, 63 clips - so
the look must be IDENTICAL everywhere: there is deliberately no auto-exposure and no per-Short
levelling, because either would make Short 07 grade differently from Short 08. And it must be
deterministic: every choice that could have been random (which whoosh lands on which cut) is
seeded from the Short's own name, so a re-run reproduces the same file.

Each stage is a small function over dataclasses so it can be tested on its own, and no stage
ever writes into the user's source folder - all intermediates live in a temp directory.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

import pipeline

# ------------------------------------------------------------------ configuration

DEFAULT_CONFIG = {
    "render": {"width": 1080, "height": 1920, "fps": 30, "crf": 18,
               "preset": "slow", "audio_bitrate": "320k"},
    "edit": {"hook_enabled": True, "hook_max_s": 1.8, "head_trim_frames": 3,
             "tail_trim_frames": 3, "jcut_frames": 4, "max_ramps": 1,
             "loop_shaping": True, "scene_threshold": 0.4},
    "grade": {"lut": "assets/luts/series.cube", "grain": 6, "vignette_pov": True},
    "audio": {"target_lufs": -14.0, "true_peak": -1.0, "music_duck_db": -5.0,
              "prelude_silence_frames": 3, "whoosh_lead_frames": 3,
              # Action Edit is source-sound-first. It must never quietly borrow arbitrary
              # clips from the user's global SFX library.
              "external_transition_sfx": False},
}

# The ramp shape from the spec: 100% -> 40% over 3 frames -> hold 40% for 8 frames -> back over
# 3 frames. Split into constant-speed pieces because a piecewise setpts expression is far harder
# to predict (and to test) than three concatenated segments. The two transition pieces run at the
# midpoint speed, which is what "over 3 frames" averages out to.
# The old 40% hold looked like a freeze and stretched fourteen source frames into almost two
# seconds. The successful manual edit only used a restrained 88% hero moment.
RAMP_SHAPE = ((3, 0.94), (8, 0.88), (3, 0.94))

# 9:16 with the 1% tolerance from the input contract.
TARGET_AR = 9.0 / 16.0
AR_TOLERANCE = 0.01

# Motion is measured on a tiny grayscale decode - 64 wide keeps a 3-clip Short's analysis under
# a second while still separating a trick peak from a hold.
MOTION_W = 64
MOTION_H = 114


class ActionEditError(RuntimeError):
    """A Short that cannot be built. Carries a message meant for the user, not a stack trace."""


@dataclass
class ClipProbe:
    path: Path
    duration: float
    fps: float
    width: int
    height: int
    has_audio: bool
    pix_fmt: str = ""
    sample_rate: int = 0

    @property
    def aspect(self) -> float:
        return (self.width / self.height) if self.height else 0.0


@dataclass
class Shot:
    """One visual beat: a range inside one source clip."""
    clip_index: int          # 0-based index into the Short's clips
    start: float             # seconds into that clip
    end: float
    motion: float = 0.0      # mean absolute frame difference over the shot
    pov: bool = False        # tagged POV -> gets the vignette

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


@dataclass
class Segment:
    """One piece of the output timeline. A shot is one segment unless a ramp splits it."""
    clip_index: int
    start: float
    end: float
    speed: float = 1.0       # 1.0 = realtime, 0.4 = slow motion
    pov: bool = False
    shot_index: int = -1     # which shot it came from (-1 = the hook)
    is_hook: bool = False
    out_start: float = 0.0   # filled in when the timeline is laid out

    @property
    def src_duration(self) -> float:
        return max(0.0, self.end - self.start)

    @property
    def out_duration(self) -> float:
        return self.src_duration / self.speed if self.speed else 0.0


@dataclass
class EditPlan:
    segments: list
    shots: list
    cuts: list = field(default_factory=list)       # output timestamps where a cut happens
    ramps: list = field(default_factory=list)      # output timestamps of the ramp holds
    hook: dict = field(default_factory=dict)
    impact: float = -1.0                           # output timestamp of the biggest hit
    warnings: list = field(default_factory=list)

    @property
    def duration(self) -> float:
        return sum(s.out_duration for s in self.segments)


# ------------------------------------------------------------------ small helpers

def _toml(path: Path) -> dict:
    import tomllib
    try:
        return tomllib.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as exc:                                            # noqa: BLE001
        raise ActionEditError(f"{Path(path).name} is not valid TOML: {exc}") from exc


def _merge(base: dict, over: dict) -> dict:
    """Deep-merge one config table over another; the override wins per leaf, not per table."""
    out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in base.items()}
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(root=None, short_dir=None) -> dict:
    """Defaults <- config.toml <- short.toml.

    The per-Short keys (title/music/hook/text/...) sit at the TOP level of short.toml while the
    tuning tables sit in [render]/[edit]/..., so the two are split here: tables merge into the
    config, loose keys land in cfg["short"].
    """
    cfg = _merge(DEFAULT_CONFIG, {})
    if root:
        cfg = _merge(cfg, _toml(Path(root) / "config.toml"))
    short = {}
    if short_dir:
        short = _toml(Path(short_dir) / "short.toml")
        cfg = _merge(cfg, {k: v for k, v in short.items() if isinstance(v, dict)})
    cfg["short"] = {k: v for k, v in short.items() if not isinstance(v, dict)}
    return cfg


def _run(cmd, timeout=1800):
    # NOTHING IS EVER TYPED AT THESE. ffmpeg watches stdin for its interactive keys ("q" to
    # stop), so a child that inherits a console handle nobody writes to has something it can
    # block on. `-nostdin` tells it not to look; DEVNULL means there is nothing to look at even
    # for the tools that ignore the flag.
    #
    # WHAT IS MEASURED AND WHAT IS INFERRED, because a later reader will need the difference.
    # Measured 2026-09-09: a render here burned 322 CPU-seconds, then took 0.00 CPU-seconds
    # across two separate 15-second samples while still alive after two hours, and held the
    # whole suite. That stall is real. That STDIN caused it is not established: the same test
    # with both guards in place later ran 306 CPU-seconds and exited normally, which shows the
    # render is genuinely slow (preset slow, crf 18, a ten-segment concat) but proves nothing
    # about the stall. Treat this as a precaution against a known ffmpeg behaviour - the same
    # family as the ProPainter stdin deadlock - not as a fix with a demonstrated mechanism. If
    # the stall returns, the cause is still open.
    argv = [str(c) for c in cmd]
    if argv and "ffmpeg" in Path(argv[0]).name.lower() and "-nostdin" not in argv:
        argv.insert(1, "-nostdin")
    return subprocess.run(argv, capture_output=True, text=True, stdin=subprocess.DEVNULL,
                          timeout=timeout, encoding="utf-8", errors="replace")


def _tools():
    ffmpeg = pipeline.find_ffmpeg()
    if not ffmpeg:
        raise ActionEditError("ffmpeg was not found - Action Edit needs it for every stage.")
    ffprobe = pipeline.find_ffprobe(ffmpeg)
    if not ffprobe:
        raise ActionEditError("ffprobe was not found - Action Edit needs it to read the clips.")
    return str(ffmpeg), str(ffprobe)


def _escape_filter_path(path) -> str:
    """A filter argument wants forward slashes and an escaped drive colon; a raw Windows path
    turns into a filter-syntax error rather than a missing-file error, which is worse."""
    return str(path).replace("\\", "/").replace(":", "\\:")


def _seed_for(name) -> int:
    """A stable seed per Short. The built-in hash() is salted per process, so it would pick a
    different whoosh on every run and quietly break determinism; sha1 does not."""
    return int(hashlib.sha1(str(name).encode("utf-8")).hexdigest()[:8], 16)


# ------------------------------------------------------------------ stage 1: probe & normalise

def probe_clip(path, ffprobe=None) -> ClipProbe:
    path = Path(path)
    if not path.exists():
        raise ActionEditError(f"{path.name} is missing.")
    if ffprobe is None:
        _, ffprobe = _tools()
    res = _run([ffprobe, "-v", "error", "-print_format", "json",
                "-show_format", "-show_streams", str(path)], timeout=120)
    try:
        data = json.loads(res.stdout or "{}")
    except json.JSONDecodeError:
        raise ActionEditError(f"{path.name} could not be read by ffprobe.") from None
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if not video:
        raise ActionEditError(f"{path.name} has no video stream.")
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    num, _, den = str(video.get("avg_frame_rate") or "0/1").partition("/")
    try:
        fps = float(num) / float(den or 1)
    except (ValueError, ZeroDivisionError):
        fps = 0.0
    try:
        duration = float((data.get("format") or {}).get("duration") or 0.0)
    except (TypeError, ValueError):
        duration = 0.0
    return ClipProbe(
        path=path, duration=duration, fps=fps or 30.0,
        width=int(video.get("width") or 0), height=int(video.get("height") or 0),
        has_audio=bool(audio), pix_fmt=str(video.get("pix_fmt") or ""),
        sample_rate=int((audio or {}).get("sample_rate") or 0),
    )


def probe_short(short_dir, ffprobe=None) -> list:
    """The three clips of one Short, validated.

    This fails loudly on purpose: a malformed Short must stop its own build rather than quietly
    produce a wrong-shaped video that is only noticed after 21 of them have been uploaded.
    """
    short_dir = Path(short_dir)
    clips = [short_dir / f"clip{i}.mp4" for i in (1, 2, 3)]
    missing = [c.name for c in clips if not c.exists()]
    if missing:
        raise ActionEditError(f"{short_dir.name}: missing {', '.join(missing)}. "
                              "A Short is exactly clip1.mp4, clip2.mp4 and clip3.mp4.")
    if ffprobe is None:
        _, ffprobe = _tools()
    probes = []
    for clip in clips:
        p = probe_clip(clip, ffprobe)
        if not p.width or not p.height:
            raise ActionEditError(f"{clip.name} has no usable video size.")
        if abs(p.aspect - TARGET_AR) > TARGET_AR * AR_TOLERANCE:
            raise ActionEditError(
                f"{clip.name} is {p.width}x{p.height} (aspect {p.aspect:.3f}), not 9:16 "
                f"({TARGET_AR:.3f}). Action Edit only takes vertical clips.")
        probes.append(p)
    return probes


def normalise(src, dst, cfg, ffmpeg, has_audio=True) -> None:
    """One clip -> the working format: 1080x1920, 30 fps, yuv420p, 48 kHz stereo.

    A clip without audio gets explicit silence rather than no stream at all, because every
    downstream audio filter assumes each segment has something to trim.
    """
    r = cfg["render"]
    cmd = [ffmpeg, "-y", "-i", str(src)]
    if not has_audio:
        cmd += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
                "-shortest"]
    cmd += [
        "-vf", (f"scale={r['width']}:{r['height']}:force_original_aspect_ratio=increase:"
                f"flags=lanczos,crop={r['width']}:{r['height']},fps={r['fps']},format=yuv420p"),
        # This is the only encode before the final one, so it stays well clear of visible loss -
        # the grade and the grain still have to be laid on top of it.
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "12", "-pix_fmt", "yuv420p",
        "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2",
        str(dst),
    ]
    res = _run(cmd)
    if res.returncode != 0 or not Path(dst).exists():
        raise ActionEditError(f"Could not normalise {Path(src).name}: "
                              f"{(res.stderr or '').strip()[-300:]}")


# ------------------------------------------------------------------ stage 2: beat detection

def detect_boundaries(path, ffmpeg, threshold=0.4, skip_edges=0.5) -> list:
    """Timestamps of the hard cuts inside one clip.

    Each source clip carries exactly one internal cut, so one boundary is the expected answer.
    Boundaries in the first/last half second are dropped: an AI clip usually fades up from black,
    and that fade reads as a scene change without being one.
    """
    res = _run([ffmpeg, "-hide_banner", "-i", str(path), "-filter_complex",
                f"select='gt(scene,{float(threshold):.3f})',metadata=print:file=-",
                "-an", "-f", "null", "-"], timeout=600)
    text = (res.stdout or "") + "\n" + (res.stderr or "")
    times = []
    for line in text.splitlines():
        if "pts_time:" not in line:
            continue
        try:
            times.append(float(line.split("pts_time:", 1)[1].split()[0]))
        except (ValueError, IndexError):
            continue
    duration = probe_clip(path).duration
    times = sorted({round(t, 3) for t in times
                    if skip_edges < t < max(skip_edges, duration - skip_edges)})
    return times


def shots_for(clip_index, path, duration, boundaries, warnings) -> list:
    """Split one clip into its shots. Never crashes on a surprising boundary count - a Short
    that detects badly still has to produce a watchable video."""
    kept = list(boundaries)
    if len(kept) == 0:
        warnings.append(f"clip{clip_index + 1}: no internal cut found, treated as one shot.")
    elif len(kept) > 2:
        warnings.append(f"clip{clip_index + 1}: {len(kept)} cuts found (expected 1); "
                        "treated as one shot to avoid chopping it up.")
        kept = []
    edges = [0.0] + kept + [duration]
    shots = []
    for a, b in zip(edges, edges[1:]):
        if b - a > 0.2:                     # a sliver is a detection artefact, not a shot
            shots.append(Shot(clip_index=clip_index, start=a, end=b))
    return shots or [Shot(clip_index=clip_index, start=0.0, end=duration)]


# ------------------------------------------------------------------ motion analysis

def motion_signal(path, ffmpeg, fps) -> np.ndarray:
    """Per-frame mean absolute difference, i.e. how much is moving at each moment.

    Decoded tiny and grayscale on purpose: the absolute numbers do not matter, only where the
    peaks are, and a full-size decode of 63 clips would dominate the run time.
    """
    cmd = [ffmpeg, "-nostdin", "-v", "error", "-i", str(path),
           "-vf", f"scale={MOTION_W}:{MOTION_H},format=gray,fps={fps}",
           "-f", "rawvideo", "-pix_fmt", "gray", "-"]
    res = subprocess.run([str(c) for c in cmd], capture_output=True, timeout=900,
                         stdin=subprocess.DEVNULL)
    frame = MOTION_W * MOTION_H
    count = len(res.stdout) // frame
    if count < 2:
        return np.zeros(1, dtype=np.float32)
    frames = np.frombuffer(res.stdout[:count * frame], dtype=np.uint8)
    frames = frames.reshape(count, MOTION_H, MOTION_W).astype(np.float32)
    diff = np.abs(np.diff(frames, axis=0)).mean(axis=(1, 2))
    # One value per frame: frame 0 inherits frame 1's, so index == frame number everywhere else.
    return np.concatenate([diff[:1], diff]).astype(np.float32)


def _slice(signal, fps, start, end) -> np.ndarray:
    a = max(0, int(round(start * fps)))
    b = min(len(signal), max(a + 1, int(round(end * fps))))
    return signal[a:b]


def score_shots(shots, signals, fps) -> None:
    """Fill in each shot's motion score, in place."""
    for shot in shots:
        seg = _slice(signals[shot.clip_index], fps, shot.start, shot.end)
        shot.motion = float(seg.mean()) if len(seg) else 0.0


def snap_to_motion(signal, fps, seconds, window_frames=4, lo=None, hi=None) -> float:
    """Move a cut point to the nearest local motion maximum within +/- window frames.

    Cutting on movement rather than on an arbitrary frame is most of what makes an edit feel
    intentional; the window is small enough that it never changes which shot you are in.
    """
    if len(signal) < 2:
        return seconds
    centre = int(round(seconds * fps))
    a = max(0, centre - window_frames)
    b = min(len(signal), centre + window_frames + 1)
    if b <= a:
        return seconds
    best = a + int(np.argmax(signal[a:b]))
    out = best / float(fps)
    if lo is not None:
        out = max(lo, out)
    if hi is not None:
        out = min(hi, out)
    return round(out, 4)


# ------------------------------------------------------------------ stage 3: assembly & pacing

def trim_shots(shots, signals, cfg, fps) -> list:
    """Trim the AI fade-in/out mush off both ends of every shot, then snap to motion."""
    head = int(cfg["edit"]["head_trim_frames"]) / fps
    tail = int(cfg["edit"]["tail_trim_frames"]) / fps
    out = []
    for shot in shots:
        start, end = shot.start + head, shot.end - tail
        if end - start < 0.25:                       # too short to trim - leave it alone
            start, end = shot.start, shot.end
        sig = signals[shot.clip_index]
        start = snap_to_motion(sig, fps, start, 4, lo=shot.start, hi=end - 0.2)
        end = snap_to_motion(sig, fps, end, 4, lo=start + 0.2, hi=shot.end)
        out.append(Shot(clip_index=shot.clip_index, start=start, end=end,
                        motion=shot.motion, pov=shot.pov))
    return out


def pick_hook(shots, cfg, fps):
    """The cold open: the last 0.6-0.9 s of the highest-motion shot, shown before the story
    starts. Capped at hook_max_s so it never turns into a fourth beat."""
    if not cfg["edit"].get("hook_enabled", True) or not shots:
        return None
    idx = max(range(len(shots)), key=lambda i: shots[i].motion)
    shot = shots[idx]
    length = min(0.9, float(cfg["edit"].get("hook_max_s", 1.0)), max(0.0, shot.duration))
    if length < 0.3:
        return None
    length = max(0.6, length) if shot.duration >= 0.6 else length
    return {"shot_index": idx, "clip_index": shot.clip_index,
            "start": round(shot.end - length, 4), "end": round(shot.end, 4),
            "seconds": round(length, 4)}


def pick_ramps(shots, segments, signals, cfg, fps) -> list:
    """Auto-pick the trick peaks worth ramping: the strongest motion maxima, at least 4 s apart,
    never in the opening 1.5 s or the closing 1 s."""
    max_ramps = int(cfg["edit"].get("max_ramps", 2))
    if max_ramps <= 0:
        return []
    total = sum(s.out_duration for s in segments)
    peaks = []
    for seg in segments:
        if seg.is_hook:
            continue
        sig = _slice(signals[seg.clip_index], fps, seg.start, seg.end)
        if len(sig) < 3:
            continue
        i = int(np.argmax(sig))
        out_t = seg.out_start + (i / float(fps)) / seg.speed
        peaks.append((float(sig[i]), out_t, seg))
    peaks.sort(key=lambda p: -p[0])
    chosen, seen_shots = [], set()
    for strength, out_t, seg in peaks:
        if out_t < 1.5 or out_t > total - 1.0:
            continue
        if any(abs(out_t - c[1]) < 4.0 for c in chosen):
            continue
        chosen.append((strength, out_t, seg))
        if len(chosen) >= max_ramps:
            break
    return sorted(chosen, key=lambda c: c[1])


def lay_out(segments) -> float:
    """Assign every segment its output start time. Returns the total duration."""
    t = 0.0
    for seg in segments:
        seg.out_start = round(t, 6)
        t += seg.out_duration
    return t


def ramp_frames_added(fps) -> float:
    """How many extra frames one ramp adds. The tests pin this, and the audio layout needs it,
    so it is derived from RAMP_SHAPE rather than written down twice."""
    src = sum(n for n, _ in RAMP_SHAPE)
    out = sum(n / speed for n, speed in RAMP_SHAPE)
    return out - src


def apply_ramp(segments, target, fps) -> list:
    """Split the segment containing `target` (an output timestamp) into pre / ramp / post.

    Splitting into constant-speed pieces rather than writing one piecewise setpts expression is
    the spec's own preference, and it means the output duration is arithmetic instead of a guess.
    """
    out = []
    src_frames = sum(n for n, _ in RAMP_SHAPE)
    for seg in segments:
        span = seg.out_duration
        if seg.is_hook or not (seg.out_start <= target < seg.out_start + span):
            out.append(seg)
            continue
        # where the peak sits inside the segment, in SOURCE seconds
        span_src = src_frames / float(fps)
        if seg.src_duration <= span_src:
            out.append(seg)                  # the shot itself is shorter than a ramp
            continue
        into = (target - seg.out_start) * seg.speed
        # A peak sitting near the end of a shot still deserves its ramp - anchor the ramp so it
        # fits inside the shot instead of dropping it. Refusing here meant a trick that happened
        # just before a cut, which is most of them, never got ramped at all.
        ramp_start = min(max(seg.start, seg.start + into), seg.end - span_src)
        ramp_end = ramp_start + span_src
        if ramp_start > seg.start:
            out.append(Segment(seg.clip_index, seg.start, ramp_start, seg.speed, seg.pov,
                               seg.shot_index))
        t = ramp_start
        for n, speed in RAMP_SHAPE:
            piece = n / float(fps)
            out.append(Segment(seg.clip_index, t, t + piece, speed, seg.pov, seg.shot_index))
            t += piece
        if seg.end > ramp_end:
            out.append(Segment(seg.clip_index, ramp_end, seg.end, seg.speed, seg.pov,
                               seg.shot_index))
    return out


def plan_edit(probes, signals, cfg, boundaries_per_clip, fps) -> EditPlan:
    """Everything that decides WHAT the cut is, with no ffmpeg involved - so it can be tested
    without rendering anything."""
    warnings = []
    shots = []
    for i, probe in enumerate(probes):
        shots.extend(shots_for(i, probe.path, probe.duration,
                               boundaries_per_clip[i], warnings))
    score_shots(shots, signals, fps)
    # pov_shots is ONE-BASED, matching the clip1/clip2/clip3 naming the user already works in.
    # Accepting both bases meant pov_shots=[2] vignetted shot 1 as well as shot 2.
    pov_tags = {int(t) for t in (cfg["short"].get("pov_shots") or [])}
    for i, shot in enumerate(shots):
        shot.pov = (i + 1) in pov_tags
    shots = trim_shots(shots, signals, cfg, fps)
    score_shots(shots, signals, fps)

    segments = [Segment(s.clip_index, s.start, s.end, 1.0, s.pov, i)
                for i, s in enumerate(shots)]

    hook = pick_hook(shots, cfg, fps)
    if hook:
        segments.insert(0, Segment(hook["clip_index"], hook["start"], hook["end"],
                                   1.0, False, hook["shot_index"], is_hook=True))
    lay_out(segments)

    forced = cfg["short"].get("ramp_at")
    if forced:
        targets = [float(t) for t in forced][:int(cfg["edit"].get("max_ramps", 2))]
    else:
        targets = [t for _, t, _ in pick_ramps(shots, segments, signals, cfg, fps)]
    ramps = []
    for target in sorted(targets, reverse=True):      # back to front: earlier starts stay valid
        before = len(segments)
        segments = apply_ramp(segments, target, fps)
        if len(segments) > before:
            ramps.append(round(target, 3))
            # The pieces a split just created carry no timeline position yet, and the next
            # (earlier) target is matched against out_start - without this they all read as
            # starting at 0 and swallow it.
            lay_out(segments)
        else:
            warnings.append(f"ramp at {target:.2f}s did not fit inside a shot and was skipped.")
    lay_out(segments)
    ramps.sort()

    # A cut is a boundary between two different shots. The pieces a ramp splits a shot into are
    # NOT cuts - putting a whoosh on them would be audible nonsense - so they are skipped by
    # comparing shot identity rather than segment identity.
    cuts = []
    for prev, seg in zip(segments, segments[1:]):
        if seg.shot_index != prev.shot_index or seg.is_hook != prev.is_hook:
            cuts.append(seg.out_start)

    impact = -1.0
    best = -1.0
    for seg in segments:
        sig = _slice(signals[seg.clip_index], fps, seg.start, seg.end)
        if not len(sig):
            continue
        i = int(np.argmax(sig))
        if float(sig[i]) > best:
            best = float(sig[i])
            impact = seg.out_start + (i / float(fps)) / seg.speed
    return EditPlan(segments=segments, shots=shots, cuts=[round(c, 4) for c in cuts],
                    ramps=ramps, hook=hook or {}, impact=round(impact, 4), warnings=warnings)


# ------------------------------------------------------------------ optional AI editing pass
def _editor_contact_sheet(clips, shots, work, ffmpeg):
    """Make a labelled start/middle/end visual brief for every detected source shot."""
    try:
        from PIL import Image, ImageDraw
    except Exception:
        return ""
    rows = []
    for index, shot in enumerate(shots):
        inset = min(0.18, shot.duration * 0.12)
        times = (shot.start + inset, (shot.start + shot.end) / 2.0, shot.end - inset)
        frames = []
        for position, at in zip(("START", "MID", "END"), times):
            frame_path = Path(work) / f"editor_shot_{index + 1:02d}_{position.lower()}.jpg"
            result = _run([ffmpeg, "-y", "-ss", f"{at:.3f}", "-i", str(clips[shot.clip_index]),
                           "-frames:v", "1", "-vf", "scale=180:-2", "-q:v", "4", str(frame_path)],
                          timeout=90)
            if result.returncode == 0 and frame_path.exists():
                frames.append((position, at, Image.open(frame_path).convert("RGB")))
        if len(frames) != 3:
            continue
        image_h = max(frame.height for _, _, frame in frames)
        canvas = Image.new("RGB", (560, image_h + 48), "#0a0d0b")
        draw = ImageDraw.Draw(canvas)
        for column, (position, at, frame) in enumerate(frames):
            canvas.paste(frame, (column * 185, 0))
            draw.text((column * 185 + 5, image_h + 5), f"{position} {at:.1f}s", fill="#9dae9f")
        draw.text((5, image_h + 25),
                  f"SHOT {index + 1} | clip {shot.clip_index + 1} | limits {shot.start:.2f}-{shot.end:.2f}s",
                  fill="white")
        rows.append(canvas)
    if not rows:
        return ""
    row_h = max(row.height for row in rows)
    sheet = Image.new("RGB", (560, row_h * len(rows)), "#050605")
    for index, row in enumerate(rows):
        sheet.paste(row, (0, index * row_h))
    output = Path(work) / "ai_editor_contact_sheet.jpg"
    sheet.save(output, quality=92)
    return str(output)


def _cuts_for_segments(segments):
    return [round(seg.out_start, 4) for prev, seg in zip(segments, segments[1:])
            if seg.shot_index != prev.shot_index or seg.is_hook != prev.is_hook]


def apply_ai_editor(plan, clips, signals, cfg, work, ffmpeg, editor_model, status_cb=None):
    """Let a vision-capable editor choose hook/order/ramp moments from real source frames."""
    editor_model = str(editor_model or "").strip()
    if not editor_model or editor_model == "local":
        return plan, {"mode": "local", "applied": False, "reason": "Local action cut engine selected."}
    sheet = _editor_contact_sheet(clips, plan.shots, work, ffmpeg)
    if not sheet:
        return plan, {"mode": editor_model, "applied": False, "reason": "Could not make editor contact sheet."}
    prompt = """You are editing a vertical action-sports Short from a contact sheet showing START/MID/END
frames of every detected source shot. Make it feel like a deliberate 15-22 second social edit.
Choose a gripping 0.8-1.8 second cold-open from the clearest trick/payoff. Then build a readable
setup -> action -> strongest final payoff. Remove dead standing, empty approaches, repeated angles,
static aftermath and weak footage. Do not repeat the cold-open frames later. Never cross the printed
shot limits: those limits are hard source cuts. Keep body segments 1.8-4.8 seconds where possible.
Select at most one shot for a subtle 0.88x hero slowdown; never create a freeze-like ramp.
Return STRICT JSON only:
{"hook":{"shot":<shot number>,"start":<absolute source second>,"end":<absolute source second>},
 "segments":[{"shot":<shot number>,"start":<absolute source second>,"end":<absolute source second>}],
 "slow_shot":<shot number or 0>,"why":"short editorial rationale"}
"""
    try:
        # Local import avoids making this local-only mode depend on the scrape module at startup.
        import scrape_v2
        answer = scrape_v2._vision_json(prompt, sheet, max_tokens=700, temperature=0.2,
                                         reasoning_model=editor_model) or {}
    except Exception as exc:  # noqa: BLE001 - a model outage must not discard the local edit
        return plan, {"mode": editor_model, "applied": False,
                      "reason": f"AI editor unavailable ({type(exc).__name__}); used local cut."}
    available = list(range(1, len(plan.shots) + 1))
    # seen_shots is what stops the model reusing the same shot twice; without it every AI edit
    # died on a NameError the moment the model answered.
    chosen, seen_shots = [], set()
    for item in (answer.get("segments") or []):
        try:
            number = int(item.get("shot"))
            source = plan.shots[number - 1]
            start = max(source.start, float(item.get("start")))
            end = min(source.end, float(item.get("end")))
        except (TypeError, ValueError, IndexError, AttributeError):
            continue
        if number in available and number not in seen_shots and end - start >= 0.65:
            chosen.append((number, round(start, 4), round(end, 4)))
            seen_shots.add(number)
    if not chosen:
        return plan, {"mode": editor_model, "applied": False,
                      "reason": "AI editor returned no usable body segments; used local cut."}
    hook_answer = answer.get("hook") or {}
    try:
        hook_number = int(hook_answer.get("shot"))
        hook_source = plan.shots[hook_number - 1]
        hook_start = max(hook_source.start, float(hook_answer.get("start")))
        hook_end = min(hook_source.end, float(hook_answer.get("end")))
    except (TypeError, ValueError, IndexError, AttributeError):
        hook_number = 0
        hook_start = hook_end = 0.0
    if hook_number not in available or not 0.6 <= hook_end - hook_start <= 2.1:
        return plan, {"mode": editor_model, "applied": False,
                      "reason": "AI editor returned no usable hook; used local cut."}
    segments = []
    if cfg["edit"].get("hook_enabled", True):
        segments.append(Segment(hook_source.clip_index, hook_start, hook_end,
                                0.88, False, hook_number - 1, is_hook=True))
    for number, start, end in chosen:
        source = plan.shots[number - 1]
        # Remove overlap with the cold-open rather than replaying the same frames.
        if number == hook_number and not (end <= hook_start or start >= hook_end):
            if hook_start - start >= 0.8:
                end = hook_start
            elif end - hook_end >= 0.8:
                start = hook_end
            else:
                continue
        segments.append(Segment(source.clip_index, start, end, 1.0, source.pov, number - 1))
    if len(segments) <= int(bool(cfg["edit"].get("hook_enabled", True))):
        return plan, {"mode": editor_model, "applied": False,
                      "reason": "AI editor removed every body segment; used local cut."}
    # A partial/truncated model response must not turn a full Action Edit into a tiny teaser.
    # The local planner is safer than accepting an unreadably short or sprawling plan.
    planned_seconds = sum(segment.out_duration for segment in segments)
    if planned_seconds < 10.0 or planned_seconds > 26.0:
        return plan, {"mode": editor_model, "applied": False,
                      "reason": (f"AI editor proposed {planned_seconds:.1f}s outside the usable "
                                 "Action Edit range; used local cut.")}
    slow_number = 0
    try:
        slow_number = int(answer.get("slow_shot") or 0)
    except (TypeError, ValueError):
        pass
    if int(cfg["edit"].get("max_ramps", 1)) > 0 and slow_number in available:
        target = next((seg for seg in segments if not seg.is_hook and seg.shot_index == slow_number - 1), None)
        if target and target.out_duration <= 2.4:
            target.speed = 0.88
    lay_out(segments)
    best_motion, impact = -1.0, -1.0
    for segment in segments:
        if segment.is_hook:
            continue
        signal = _slice(signals[segment.clip_index], cfg["render"]["fps"], segment.start, segment.end)
        if not len(signal):
            continue
        peak_index = int(np.argmax(signal))
        if float(signal[peak_index]) > best_motion:
            best_motion = float(signal[peak_index])
            impact = segment.out_start + (peak_index / float(cfg["render"]["fps"])) / segment.speed
    result = EditPlan(segments=segments, shots=plan.shots, cuts=_cuts_for_segments(segments),
                      ramps=[round(seg.out_start, 3) for seg in segments
                             if not seg.is_hook and seg.speed < 1.0],
                      hook={"shot_index": hook_number - 1, "clip_index": hook_source.clip_index,
                            "start": round(hook_start, 4), "end": round(hook_end, 4),
                            "seconds": round((hook_end - hook_start) / 0.88, 4)},
                      impact=round(impact, 4),
                      warnings=plan.warnings)
    return result, {"mode": editor_model, "applied": True,
                    "segments": [{"shot": n, "start": s, "end": e} for n, s, e in chosen],
                    "hook_shot": hook_number, "slow_shot": slow_number,
                    "why": str(answer.get("why") or "")[:300]}


# ------------------------------------------------------------------ stage 6: sound design

def whoosh_pool(root) -> list:
    """An explicitly supplied Action-Edit transition pack, in a stable order.

    This deliberately does not fall back to ``soundeffects``. That folder belongs to the normal
    Short pipeline and contains reactions, UI noises and old sounds that must never leak into an
    Action Edit. The default Action Edit does not call this function at all.
    """
    for spec_dir in (Path(root) / "assets" / "sfx" / "whoosh",
                     Path(__file__).resolve().parent / "assets" / "sfx" / "whoosh"):
        if spec_dir.is_dir():
            found = sorted((p for p in spec_dir.iterdir()
                            if p.is_file()
                            and p.suffix.lower() in (".wav", ".mp3", ".m4a", ".ogg")),
                           key=lambda p: p.name.lower())
            if found:
                return found
    return []


def _mean_volume(path, start, end, ffmpeg) -> float:
    """Mean dBFS of one range, used to level the shots against each other."""
    res = _run([ffmpeg, "-v", "info", "-ss", f"{start:.3f}", "-to", f"{end:.3f}",
                "-i", str(path), "-vn", "-af", "volumedetect", "-f", "null", "-"], timeout=300)
    for line in (res.stderr or "").splitlines():
        if "mean_volume:" in line:
            try:
                return float(line.split("mean_volume:", 1)[1].strip().split()[0])
            except (ValueError, IndexError):
                break
    return -91.0        # treat unreadable as silence rather than boosting noise


def _db_to_linear(db) -> float:
    return float(10.0 ** (float(db) / 20.0))


def build_audio(plan, norm_clips, cfg, work, ffmpeg, short_name, root, status_cb=None) -> dict:
    """The three sound layers, mixed and loudness-normalised, written to one wav.

    Built as its own pass rather than inside the final render so the mix can be measured (the
    loudness target needs a real measurement, not a guess) and inspected on its own.
    """
    log = status_cb or (lambda _m: None)
    a = cfg["audio"]
    fps = float(cfg["render"]["fps"])
    total = plan.duration
    jcut = int(cfg["edit"].get("jcut_frames", 4)) / fps

    inputs, parts, report = [], [], {}
    for clip in norm_clips:
        inputs += ["-i", str(clip)]

    # --- layer 1: the clips' own sound, levelled per shot and high-passed
    log("Levelling the clips' own sound...")
    target_db = -20.0
    gains = {}
    for idx, shot in enumerate(plan.shots):
        measured = _mean_volume(norm_clips[shot.clip_index], shot.start, shot.end, ffmpeg)
        # Clamped: a shot that is genuinely near-silent must not be dragged up to match a loud
        # one, or its noise floor becomes the loudest thing in the Short.
        gains[idx] = max(-6.0, min(6.0, target_db - measured)) if measured > -60.0 else 0.0
    report["shot_gain_db"] = {str(k): round(v, 2) for k, v in gains.items()}

    diegetic = []
    first_of_shot = set()
    seen = set()
    for seg in plan.segments:
        key = (seg.shot_index, seg.is_hook)
        if key not in seen:
            seen.add(key)
            first_of_shot.add(id(seg))
    for i, seg in enumerate(plan.segments):
        # J-cut: the incoming shot's sound starts a few frames early, under the outgoing picture.
        # Only the first piece of a shot gets it - the pieces a ramp made are not cuts.
        delay = seg.out_start - (jcut if id(seg) in first_of_shot and seg.out_start > 0 else 0.0)
        delay_ms = max(0, int(round(delay * 1000)))
        gain = gains.get(seg.shot_index, 0.0)
        if seg.speed != 1.0:
            # During a ramp the diegetic bed drops back and the music carries it. Pitching the
            # audio down with the picture would sound like a tape stopping, which is not the
            # effect a trick peak wants.
            gain += -8.0
        parts.append(
            f"[{seg.clip_index}:a]atrim=start={seg.start:.4f}:end={seg.end:.4f},"
            f"asetpts=PTS-STARTPTS,highpass=f=60,volume={_db_to_linear(gain):.4f},"
            f"adelay={delay_ms}|{delay_ms}[d{i}]")
        diegetic.append(f"[d{i}]")

    # --- layer 2: optional dedicated Action-Edit transitions, never the app's SFX library.
    # The shipped mode defaults to source audio only.  This branch exists only for a future,
    # explicitly configured Action-Edit pack under assets/sfx/whoosh.
    pool = whoosh_pool(root) if bool(a.get("external_transition_sfx", False)) else []
    lead = int(a.get("whoosh_lead_frames", 3)) / fps
    used = []
    sfx_labels = []
    if pool and plan.cuts:
        seed = _seed_for(short_name)
        for j, cut in enumerate(plan.cuts):
            pick = pool[(seed + j) % len(pool)]
            at = max(0.0, cut - lead)
            ms = int(round(at * 1000))
            idx = len(norm_clips) + len(used)
            inputs += ["-i", str(pick)]
            parts.append(f"[{idx}:a]aformat=sample_rates=48000:channel_layouts=stereo,"
                         f"volume=0.8,adelay={ms}|{ms}[s{j}]")
            sfx_labels.append(f"[s{j}]")
            used.append({"cut": round(cut, 3), "at": round(at, 3), "file": pick.name})
    report["whooshes"] = used

    # --- the layers that duck together for the pre-impact silence
    body = diegetic + sfx_labels
    parts.append("".join(body) + f"amix=inputs={len(body)}:normalize=0:"
                                 f"dropout_transition=0[body_raw]")
    duck_note = None
    if plan.impact > 0.4:
        # A beat of near-silence right before the hit is what makes the hit land. Music keeps
        # playing through it, so the drop reads as focus rather than as a dropout.
        pre = int(a.get("prelude_silence_frames", 3)) / fps
        start = max(0.0, plan.impact - 4.0 / fps)
        parts.append(f"[body_raw]volume=volume={_db_to_linear(-20.0):.4f}:"
                     f"enable='between(t,{start:.3f},{start + pre:.3f})'[body]")
        duck_note = {"start": round(start, 3), "end": round(start + pre, 3), "db": -20.0}
    else:
        parts.append("[body_raw]anull[body]")
    report["pre_impact_duck"] = duck_note

    # --- layer 3: music, shifted so its drop lands on the biggest motion peak
    music = cfg["short"].get("music")
    music_path = Path(music) if music else None
    if music_path and not music_path.is_absolute():
        music_path = Path(root) / music_path
    if music_path and music_path.exists():
        drop = cfg["short"].get("music_drop_s")
        peak = plan.impact if plan.impact > 0 else 0.0
        start_at, delay_ms = 0.0, 0
        if drop is not None:
            # Shift the MUSIC, never the video: the cut is already locked to the action.
            offset = float(drop) - peak
            if offset >= 0:
                start_at = offset
            else:
                delay_ms = int(round(-offset * 1000))
        chain = (f"[{len(norm_clips) + len(used)}:a]"
                 f"atrim=start={start_at:.3f},asetpts=PTS-STARTPTS,"
                 f"aformat=sample_rates=48000:channel_layouts=stereo,")
        if delay_ms:
            chain += f"adelay={delay_ms}|{delay_ms},"
        chain += (f"afade=t=in:st=0:d=0.3,"
                  f"afade=t=out:st={max(0.0, total - 0.5):.3f}:d=0.5[music]")
        inputs += ["-i", str(music_path)]
        parts.append(chain)
        # The bed sits under the music by the configured amount; the music itself stays at unity
        # so the loudness pass has something stable to work from.
        parts.append(f"[body]volume={_db_to_linear(a.get('music_duck_db', -5.0)):.4f}[bodyduck]")
        parts.append(f"[bodyduck][music]amix=inputs=2:normalize=0:dropout_transition=0[mixed]")
        report["music"] = {"file": music_path.name, "drop_s": drop,
                           "start_at": round(start_at, 3), "delay_ms": delay_ms}
    else:
        parts.append("[body]anull[mixed]")
        report["music"] = None
        if music:
            plan.warnings.append(f"music file not found: {music}")

    parts.append(f"[mixed]atrim=0:{total:.4f},asetpts=PTS-STARTPTS[out]")
    raw = work / "mix_raw.wav"
    res = _run([ffmpeg, "-y", *inputs, "-filter_complex", ";".join(parts),
                "-map", "[out]", "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2", str(raw)])
    if res.returncode != 0 or not raw.exists():
        raise ActionEditError(f"Sound design failed: {(res.stderr or '').strip()[-400:]}")

    # --- loudness: measure, then apply. One pass would only approximate the target.
    log("Measuring loudness...")
    target = float(a.get("target_lufs", -14.0))
    tp = float(a.get("true_peak", -1.0))
    measure = _run([ffmpeg, "-i", str(raw), "-af",
                    f"loudnorm=I={target}:TP={tp}:LRA=11:print_format=json",
                    "-f", "null", "-"])
    stats = {}
    text = (measure.stderr or "")
    if "{" in text:
        try:
            stats = json.loads(text[text.rindex("{"):text.rindex("}") + 1])
        except (ValueError, json.JSONDecodeError):
            stats = {}
    mixed = work / "mix.wav"
    if stats.get("input_i"):
        second = (f"loudnorm=I={target}:TP={tp}:LRA=11:"
                  f"measured_I={stats['input_i']}:measured_TP={stats['input_tp']}:"
                  f"measured_LRA={stats['input_lra']}:measured_thresh={stats['input_thresh']}:"
                  f"offset={stats.get('target_offset', '0.0')}:linear=true:print_format=summary")
    else:
        second = f"loudnorm=I={target}:TP={tp}:LRA=11"
        plan.warnings.append("loudness measurement failed; used a single-pass normalisation.")
    res = _run([ffmpeg, "-y", "-i", str(raw), "-af", second,
                "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2", str(mixed)])
    if res.returncode != 0 or not mixed.exists():
        raise ActionEditError(f"Loudness pass failed: {(res.stderr or '').strip()[-300:]}")
    report["loudness_before"] = {k: stats.get(k) for k in ("input_i", "input_tp", "input_lra")}
    report["loudness_target"] = {"I": target, "TP": tp, "LRA": 11}
    report["mix"] = str(mixed)
    return report


# ------------------------------------------------------------------ stage 5: colour grade

def grade_chain(cfg, root, use_lut=True, seed=0) -> tuple:
    """The series look, as a filter chain plus a note of what was actually applied.

    Identical for every Short by construction: no measurement of the picture feeds into it. An
    auto-levels step here would be the one change that made 21 Shorts stop looking like a series.
    """
    g = cfg["grade"]
    lut = g.get("lut")
    lut_path = Path(lut) if lut else None
    if lut_path and not lut_path.is_absolute():
        lut_path = Path(root) / lut_path
    if use_lut and lut_path and lut_path.exists():
        chain = [f"lut3d=file='{_escape_filter_path(lut_path)}'"]
        applied = f"lut3d:{lut_path.name}"
    else:
        chain = [
            "eq=contrast=1.06:saturation=1.12",
            # Shadows toward teal, highlights toward warm - the standard action-sports split
            # that keeps the orange top reading as the brightest thing in frame.
            "colorbalance=rs=-0.04:gs=0.01:bs=0.06:rh=0.05:gh=0.01:bh=-0.04",
        ]
        applied = "eq+colorbalance"
    grain = int(g.get("grain", 6) or 0)
    if grain > 0:
        # Grain goes on last, over everything. It measurably hides the smearing and edge
        # artefacts that give AI footage away.
        #
        # all_seed is not optional: without it ffmpeg seeds the noise from the system RNG, so the
        # same Short rendered twice came out with different bytes. That was the single thing
        # breaking the reproducibility the batch relies on, and it only showed up under load -
        # three runs in a row would match and the fourth would not.
        chain.append(f"noise=alls={grain}:allf=t+u:all_seed={int(seed) % 2147483647}")
        applied += f"+grain{grain}"
    return ",".join(chain), applied


# ------------------------------------------------------------------ stages 7-9: render & report

def text_overlay(cfg, plan) -> tuple:
    """The 3-5 word title card, or (None, reason) when there is nothing to draw."""
    text = str(cfg["short"].get("text") or "").strip()
    if not text:
        return None, None
    font = pipeline.FONT_BOLD or pipeline.FONT_REGULAR
    if not font:
        plan.warnings.append("no bundled font found; the text overlay was skipped.")
        return None, None
    h = int(cfg["render"]["height"])
    # 0.14 of frame height keeps it clear of the platform's own chrome at the top, and nowhere
    # near the bottom 22% where the caption, handle and buttons sit.
    y = int(round(h * 0.14))
    size = max(12, int(round(h / 22)))
    safe = "".join(ch for ch in text if ch.isalnum() or ch in " '-!?.,")
    draw = (f"drawtext=fontfile='{_escape_filter_path(font)}':text='{safe}':"
            f"fontcolor=white:fontsize={size}:borderw=3:bordercolor=black@0.6:"
            f"x=(w-text_w)/2:y={y}:enable='between(t,0.4,2.4)'")
    return draw, {"text": safe, "y": y, "y_fraction": round(y / float(h), 4),
                  "in": 0.4, "out": 2.4, "font_size": size}


def loop_decision(plan, signals, norm_clips, cfg, fps, ffmpeg):
    """Does the ending resemble the opening closely enough to loop?

    Compares brightness and motion of the first and last 12 frames. A short whose end looks
    nothing like its start gets a short dissolve back into the opening frame, which is what
    lifts the rewatch rate - but only when it was asked for, and never silently.
    """
    if not plan.segments:
        return {"applied": False, "reason": "no segments"}
    first, last = plan.segments[0], plan.segments[-1]
    n = 12 / fps

    # This reuses the tiny decode already done for motion rather than paying for another one.
    def bright_motion(seg, at_start):
        sig = signals[seg.clip_index]
        a = seg.start if at_start else max(seg.start, seg.end - n)
        part = _slice(sig, fps, a, min(seg.end, a + n))
        return float(part.mean()) if len(part) else 0.0

    head_motion = bright_motion(first, True)
    tail_motion = bright_motion(last, False)
    spread = abs(head_motion - tail_motion) / max(1e-6, max(head_motion, tail_motion))
    enabled = bool(cfg["edit"].get("loop_shaping", True))
    decision = {"head_motion": round(head_motion, 3), "tail_motion": round(tail_motion, 3),
                "divergence": round(spread, 3), "enabled": enabled}
    decision["applied"] = bool(enabled and spread > 0.35)
    decision["reason"] = ("ending differs from the opening" if decision["applied"]
                          else "ending already resembles the opening" if enabled
                          else "loop_shaping is off")
    return decision


def render(plan, norm_clips, mix_path, cfg, work, out_path, ffmpeg, root,
           use_lut=True, loop=None, status_cb=None) -> dict:
    """The single final encode: every segment, the grade, the loop tail and the text."""
    log = status_cb or (lambda _m: None)
    r = cfg["render"]
    fps = float(r["fps"])
    inputs, parts, labels = [], [], []
    for clip in norm_clips:
        inputs += ["-i", str(clip)]
    for i, seg in enumerate(plan.segments):
        # Frame indices, not seconds: trim=start:end compares timestamps and takes the frame
        # sitting on each boundary with it, so 13 segments quietly grew the short by ~6 frames.
        # The normalised clips are exact CFR, so frame == round(t * fps) with no ambiguity.
        a = int(round(seg.start * fps))
        b = max(a + 1, int(round(seg.end * fps)))
        chain = (f"[{seg.clip_index}:v]trim=start_frame={a}:end_frame={b},"
                 f"setpts=(PTS-STARTPTS)/{seg.speed:.4f}")
        if seg.pov and cfg["grade"].get("vignette_pov", True):
            chain += ",vignette=PI/5"          # POV shots only, per the series look
        parts.append(chain + f"[v{i}]")
        labels.append(f"[v{i}]")
    parts.append("".join(labels) + f"concat=n={len(labels)}:v=1:a=0[joined]")

    # Seeded from the Short's own name: stable for this Short across runs, different between
    # Shorts, so the grain never lands identically on two videos in the series.
    grain_seed = _seed_for(cfg["short"].get("title") or Path(out_path).stem)
    chain, applied = grade_chain(cfg, root, use_lut=use_lut, seed=grain_seed)
    parts.append(f"[joined]{chain},fps={fps:g}[graded]")
    tail = "[graded]"

    still_input = None
    if loop and loop.get("applied"):
        # xfade's output is first + second - transition, so a 0.2 s still crossed over 0.2 s
        # leaves the total length exactly as it was.
        still = work / "open_frame.png"
        first = plan.segments[0]
        res = _run([ffmpeg, "-y", "-ss", f"{first.start:.3f}", "-i", str(norm_clips[first.clip_index]),
                    "-frames:v", "1", str(still)], timeout=300)
        if res.returncode == 0 and still.exists():
            still_input = len(norm_clips)
            inputs += ["-loop", "1", "-t", "0.2", "-i", str(still)]
            parts.append(f"[{still_input}:v]scale={r['width']}:{r['height']},fps={fps:g},"
                         f"format=yuv420p,setpts=PTS-STARTPTS[still]")
            offset = max(0.0, plan.duration - 0.2)
            parts.append(f"[graded][still]xfade=transition=fade:duration=0.2:"
                         f"offset={offset:.3f}[looped]")
            tail = "[looped]"
        else:
            loop["applied"] = False
            loop["reason"] = "could not extract the opening frame"

    draw, text_info = text_overlay(cfg, plan)
    if draw:
        parts.append(f"{tail}{draw}[vout]")
    else:
        parts.append(f"{tail}null[vout]")

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [ffmpeg, "-y", *inputs, "-i", str(mix_path),
           "-filter_complex", ";".join(parts),
           "-map", "[vout]", "-map", f"{len(norm_clips) + (1 if still_input is not None else 0)}:a",
           "-c:v", "libx264", "-profile:v", "high", "-preset", str(r.get("preset", "slow")),
           "-crf", str(r.get("crf", 18)), "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", str(r.get("audio_bitrate", "320k")), "-ar", "48000", "-ac", "2",
           "-movflags", "+faststart",
           # Bit-exact and metadata-free so two runs of the same Short produce the same bytes;
           # otherwise the encoder stamps its version and the build stops being reproducible.
           "-fflags", "+bitexact", "-flags:v", "+bitexact", "-flags:a", "+bitexact",
           "-map_metadata", "-1", "-video_track_timescale", "30000",
           str(out_path)]
    log("Rendering...")
    res = _run(cmd, timeout=3600)
    if res.returncode != 0 or not out_path.exists():
        raise ActionEditError(f"Render failed: {(res.stderr or '').strip()[-500:]}")
    return {"grade": applied, "text": text_info, "loop": loop}


# ------------------------------------------------------------------ orchestration

def build_short(short_dir, out_path, root=None, cfg=None, status_cb=None,
                use_lut=True, work_dir=None, keep_work=False) -> dict:
    """One Short, end to end. Returns the edit report that is written next to the video."""
    log = status_cb or (lambda _m: None)
    short_dir = Path(short_dir)
    root = Path(root) if root else short_dir.parent.parent
    out_path = Path(out_path)
    cfg = cfg or load_config(root, short_dir)
    ffmpeg, ffprobe = _tools()
    fps = float(cfg["render"]["fps"])

    log(f"Reading {short_dir.name}...")
    probes = probe_short(short_dir, ffprobe)

    owns_work = work_dir is None
    work = Path(work_dir) if work_dir else Path(tempfile.mkdtemp(prefix="actionedit_"))
    work.mkdir(parents=True, exist_ok=True)
    try:
        norm = []
        for i, probe in enumerate(probes):
            log(f"Normalising clip {i + 1} of {len(probes)}...")
            dst = work / f"norm{i + 1}.mp4"
            normalise(probe.path, dst, cfg, ffmpeg, has_audio=probe.has_audio)
            norm.append(dst)

        log("Finding the cuts inside each clip...")
        boundaries, signals, norm_durations = [], [], []
        threshold = float(cfg["edit"].get("scene_threshold", 0.4))
        for clip in norm:
            boundaries.append(detect_boundaries(clip, ffmpeg, threshold))
            signals.append(motion_signal(clip, ffmpeg, fps))
            norm_durations.append(probe_clip(clip, ffprobe).duration)

        # The plan works against the NORMALISED clips, so its timestamps must come from their
        # durations - a source at 24 fps comes out a hair longer once it is conformed to 30.
        conformed = [ClipProbe(path=n, duration=d, fps=fps, width=cfg["render"]["width"],
                               height=cfg["render"]["height"], has_audio=True)
                     for n, d in zip(norm, norm_durations)]

        log("Planning the cut...")
        plan = plan_edit(conformed, signals, cfg, boundaries, fps)
        editor_model = str(cfg["short"].get("editor_model") or "local")
        if editor_model != "local":
            log(f"AI editor ({editor_model}) is reviewing the action beats...")
        plan, ai_editor = apply_ai_editor(plan, norm, signals, cfg, work, ffmpeg,
                                          editor_model, status_cb=log)
        if ai_editor.get("applied"):
            log("AI editor selected the hook, cut order and ramps from the source frames.")
        elif editor_model != "local":
            log(str(ai_editor.get("reason") or "AI editor did not return a usable plan; used local cut."))
        for w in plan.warnings:
            log(f"Note: {w}")

        audio = build_audio(plan, norm, cfg, work, ffmpeg,
                            cfg["short"].get("title") or short_dir.name, root, status_cb=log)
        loop = loop_decision(plan, signals, norm, cfg, fps, ffmpeg)
        log(f"Loop shaping: {loop['reason']}.")
        rendered = render(plan, norm, audio["mix"], cfg, work, out_path, ffmpeg, root,
                          use_lut=use_lut, loop=loop, status_cb=log)

        final = probe_clip(out_path, ffprobe)
        measured = _mean_volume(out_path, 0.0, min(final.duration, 60.0), ffmpeg)
        report = {
            "short": short_dir.name,
            "title": cfg["short"].get("title") or short_dir.name,
            "output": str(out_path),
            "duration_s": round(final.duration, 3),
            "planned_duration_s": round(plan.duration, 3),
            "fps": round(final.fps, 3),
            "resolution": f"{final.width}x{final.height}",
            "shots": [{"clip": s.clip_index + 1, "start": round(s.start, 3),
                       "end": round(s.end, 3), "motion": round(s.motion, 3), "pov": s.pov}
                      for s in plan.shots],
            "segments": [{"clip": s.clip_index + 1, "start": round(s.start, 3),
                          "end": round(s.end, 3), "speed": s.speed,
                          "out_start": round(s.out_start, 3), "hook": s.is_hook}
                         for s in plan.segments],
            "cuts": plan.cuts,
            "hook": plan.hook,
            "ramps": plan.ramps,
            "ramp_frames_each": round(ramp_frames_added(fps), 3),
            "impact_s": plan.impact,
            "grade": rendered["grade"],
            "text": rendered["text"],
            "loop_shaping": rendered["loop"],
            "ai_editor": ai_editor,
            "audio": {k: v for k, v in audio.items() if k != "mix"},
            "measured_mean_dbfs": round(measured, 2),
            "warnings": plan.warnings,
        }
        report_path = out_path.with_suffix(".json")
        report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        report["report"] = str(report_path)
        log(f"Done: {out_path.name} ({report['duration_s']:.2f}s)")
        return report
    finally:
        if owns_work and not keep_work:
            shutil.rmtree(work, ignore_errors=True)


def _is_fresh(short_dir: Path, out_path: Path) -> bool:
    """True when the output already exists and is newer than every input of its Short."""
    if not out_path.exists():
        return False
    newest = 0.0
    for name in ("clip1.mp4", "clip2.mp4", "clip3.mp4", "short.toml"):
        p = short_dir / name
        if p.exists():
            newest = max(newest, p.stat().st_mtime)
    return out_path.stat().st_mtime >= newest


def find_shorts(root) -> list:
    """Every folder under root that looks like a Short, in name order."""
    root = Path(root)
    if not root.is_dir():
        return []
    return sorted((d for d in root.iterdir()
                   if d.is_dir() and (d / "clip1.mp4").exists()), key=lambda d: d.name.lower())


def build_batch(root, out_dir, jobs=1, force=False, use_lut=True, status_cb=None) -> dict:
    """Every Short under root. One bad Short fails itself and nothing else."""
    log = status_cb or (lambda _m: None)
    root, out_dir = Path(root), Path(out_dir)
    shorts_root = root / "shorts" if (root / "shorts").is_dir() else root
    shorts = find_shorts(shorts_root)
    if not shorts:
        raise ActionEditError(f"No Shorts found under {shorts_root}. Each one needs a folder "
                              "with clip1.mp4, clip2.mp4 and clip3.mp4.")
    out_dir.mkdir(parents=True, exist_ok=True)
    results, failed, skipped = [], [], []

    def one(short_dir):
        out_path = out_dir / f"{short_dir.name}.mp4"
        if not force and _is_fresh(short_dir, out_path):
            return ("skip", short_dir.name, str(out_path))
        try:
            # Each job gets its own temp dir so parallel ffmpeg runs cannot collide over
            # intermediate names.
            work = Path(tempfile.mkdtemp(prefix=f"actionedit_{short_dir.name}_"))
            try:
                rep = build_short(short_dir, out_path, root=root, status_cb=log,
                                  use_lut=use_lut, work_dir=work)
            finally:
                shutil.rmtree(work, ignore_errors=True)
            return ("ok", short_dir.name, rep)
        except Exception as exc:                                        # noqa: BLE001
            return ("fail", short_dir.name, str(exc))

    jobs = max(1, int(jobs or 1))
    if jobs == 1:
        outcomes = [one(s) for s in shorts]
    else:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            outcomes = list(pool.map(one, shorts))

    for kind, name, payload in outcomes:
        if kind == "ok":
            results.append(payload)
            log(f"[ok]   {name}")
        elif kind == "skip":
            skipped.append(name)
            log(f"[skip] {name} (already up to date)")
        else:
            failed.append({"short": name, "error": payload})
            log(f"[FAIL] {name}: {payload}")
    return {"built": results, "skipped": skipped, "failed": failed,
            "total": len(shorts), "out_dir": str(out_dir)}
