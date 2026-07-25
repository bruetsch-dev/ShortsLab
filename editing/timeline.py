"""Timeline & Cutting Manager - the edit is driven by TTS word timestamps.

The primary input is the word-level timing JSON produced after TTS/whisper alignment.
Everything else (which clips exist, how long they are) is secondary: the spine is the
speech, and clips are trimmed, split and scaled to sit on it.

Three rules shape every cut:

DEAD FRAMES - the first and last `EDGE_TRIM` seconds of a source are never used. Stock
and generated clips open on a static frame and end on a drift; starting mid-motion is
what makes a cut feel deliberate.

JUMP CUTS - a shot held longer than `MAX_STATIC` seconds is split, and the middle piece
is punched in (scale > 1.0, centred, or on a supplied face box). Same footage, new
framing, the monotony breaks without needing more material.

IMPACT WORDS - a cut is FORCED onto the millisecond a high-impact word is spoken. Cut
points snap to those onsets first; the even spacing is only the fallback.

The output is a `TimelinePlan`: a list of `Cut`s with absolute timeline positions and
exact source in/out points. `plan_to_scenes()` converts it to the scene dicts the
existing renderer consumes, so this module can be dropped in without touching the
renderer.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

# ---------------------------------------------------------------- tuning

EDGE_TRIM = 0.5          # seconds shaved off both ends of every source clip
MAX_STATIC = 3.0         # a shot longer than this gets jump-cut
PUNCH_SCALES = (1.20, 1.30)
MIN_CUT = 0.60           # never emit a cut shorter than this - it reads as a glitch
SNAP_WINDOW = 0.35       # an impact word this close to a planned cut wins the position

# Words that earn a hard cut. Kept deliberately small and readable: a real
# lexicon belongs in a data file, not buried in the cutter.
IMPACT_WORDS = {
    "never", "nightmare", "sales", "secret", "shocking", "banned", "illegal",
    "trapped", "exposed", "worst", "brutal", "insane", "hidden", "forced",
    "collapse", "ruined", "dead", "gone", "empty", "alone", "broke", "debt",
    "quit", "fired", "caught", "lied", "stole", "vanished", "explodes",
    "suddenly", "instantly", "everything", "nothing", "billions", "millions",
}
_WORD_RE = re.compile(r"[^\w']+", re.UNICODE)


def _norm(word: str) -> str:
    return _WORD_RE.sub("", str(word or "")).lower()


# ---------------------------------------------------------------- data

@dataclass(frozen=True)
class ClipSource:
    """One piece of footage available to the cutter."""

    path: str
    duration: float
    face_center: tuple[float, float] | None = None   # 0..1 coords, for punch-in anchor

    @property
    def usable(self) -> tuple[float, float]:
        """(in, out) after dead-frame removal; empty range when the clip is too short."""
        lo = EDGE_TRIM
        hi = self.duration - EDGE_TRIM
        return (lo, hi) if hi - lo >= MIN_CUT else (0.0, max(0.0, self.duration))

    @property
    def usable_length(self) -> float:
        lo, hi = self.usable
        return max(0.0, hi - lo)


@dataclass
class Cut:
    """One clip on the timeline."""

    index: int
    start: float              # absolute position on the timeline
    end: float
    source: str               # clip path
    source_in: float          # in-point inside the source
    scale: float = 1.0        # 1.0 = untouched, >1 = punch-in
    anchor: tuple[float, float] = (0.5, 0.5)
    reason: str = "beat"      # beat | jump | impact:<word>
    impact_word: str | None = None

    @property
    def duration(self) -> float:
        return round(self.end - self.start, 3)

    def as_dict(self) -> dict:
        return {"index": self.index, "start": round(self.start, 3),
                "end": round(self.end, 3), "duration": self.duration,
                "source": self.source, "source_in": round(self.source_in, 3),
                "scale": round(self.scale, 3), "anchor": list(self.anchor),
                "reason": self.reason, "impact_word": self.impact_word}


@dataclass
class TimelinePlan:
    cuts: list[Cut] = field(default_factory=list)
    audio_duration: float = 0.0
    impact_hits: list[dict] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return self.cuts[-1].end if self.cuts else 0.0

    def as_dict(self) -> dict:
        return {"audio_duration": round(self.audio_duration, 3),
                "duration": round(self.duration, 3),
                "cut_count": len(self.cuts),
                "impact_hits": self.impact_hits,
                "cuts": [c.as_dict() for c in self.cuts]}

    def write(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.as_dict(), indent=1), encoding="utf-8")
        return path


# ---------------------------------------------------------------- inputs

def load_word_timings(source) -> list[dict]:
    """Read word timings from a path, a JSON string, a list or a {"words": [...]} dict.

    Accepts both key spellings seen in the wild ("word"/"text", "start"/"end").
    Returns [{"word": str, "start": float, "end": float}, ...] sorted by start.
    """
    data = source
    if isinstance(source, (str, Path)) and Path(str(source)).exists():
        data = json.loads(Path(str(source)).read_text(encoding="utf-8"))
    elif isinstance(source, str):
        data = json.loads(source)
    if isinstance(data, dict):
        data = data.get("words") or data.get("segments") or []
    out = []
    for row in data or []:
        if not isinstance(row, dict):
            continue
        try:
            start = float(row.get("start"))
            end = float(row.get("end", start))
        except (TypeError, ValueError):
            continue
        word = str(row.get("word", row.get("text", ""))).strip()
        if word and end >= start:
            out.append({"word": word, "start": start, "end": end})
    out.sort(key=lambda w: w["start"])
    return out


def probe_duration(path: str | Path, ffprobe: str = "ffprobe") -> float:
    try:
        r = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration",
                            "-of", "csv=p=0", str(path)],
                           capture_output=True, text=True, timeout=60)
        return float((r.stdout or "").strip())
    except Exception:  # noqa: BLE001 - a missing probe must not kill the plan
        return 0.0


def clips_from_paths(paths: Iterable[str | Path], ffprobe: str = "ffprobe") -> list[ClipSource]:
    out = []
    for p in paths:
        d = probe_duration(p, ffprobe)
        if d > 0.2:
            out.append(ClipSource(path=str(p), duration=d))
    return out


# ---------------------------------------------------------------- cut points

def impact_moments(words: Sequence[dict], extra: Iterable[str] = ()) -> list[dict]:
    """Onsets of high-impact words - the timeline's non-negotiable cut positions."""
    lex = {_norm(w) for w in IMPACT_WORDS} | {_norm(w) for w in extra}
    lex.discard("")
    return [{"word": w["word"], "t": float(w["start"])}
            for w in words if _norm(w["word"]) in lex]


def _cut_points(audio_dur: float, impacts: Sequence[dict], target: float) -> list[float]:
    """Cut positions across the whole audio.

    Impact onsets are placed FIRST and are never dropped - walking a cursor forward and
    only taking impacts it happened to pass lost one of two in a real 38s script. The
    even spacing is then filled in around them, and any filler landing within MIN_CUT of
    a real cut is discarded rather than producing a stutter.
    """
    forced = sorted({round(t["t"], 3) for t in impacts
                     if MIN_CUT < t["t"] < audio_dur - MIN_CUT})
    points = set(forced)
    cursor = 0.0
    while cursor < audio_dur - MIN_CUT:            # regular beats between the impacts
        cursor += target
        if cursor < audio_dur - MIN_CUT:
            points.add(round(cursor, 3))
    ordered, last = [0.0], 0.0
    for t in sorted(points):
        keep = t in forced or t - last >= MIN_CUT
        # an impact always wins: drop the filler cut sitting right before it
        if t in forced and ordered and t - ordered[-1] < MIN_CUT and ordered[-1] != 0.0:
            ordered.pop()
        if keep and t - (ordered[-1] if ordered else 0.0) >= MIN_CUT:
            ordered.append(t)
            last = t
    if audio_dur - ordered[-1] < MIN_CUT and len(ordered) > 1:
        ordered.pop()
    ordered.append(audio_dur)
    return ordered


# ---------------------------------------------------------------- planner

def build_timeline(word_timings, clips: Sequence[ClipSource], *,
                   audio_duration: float | None = None,
                   target_shot: float = 2.2,
                   impact_words: Iterable[str] = (),
                   punch_scales: Sequence[float] = PUNCH_SCALES) -> TimelinePlan:
    """Build the cut plan. The speech spine decides the length, never the footage.

    `target_shot` is the nominal shot length; impact onsets override it. Clips are used
    round-robin, each time continuing where that clip was last left off, so a long source
    yields fresh footage instead of replaying its opening.
    """
    words = load_word_timings(word_timings)
    if not clips:
        raise ValueError("build_timeline needs at least one clip source.")
    audio_dur = float(audio_duration or (words[-1]["end"] if words else 0.0))
    if audio_dur <= 0:
        raise ValueError("build_timeline needs a positive audio duration.")

    impacts = impact_moments(words, impact_words)
    points = _cut_points(audio_dur, impacts, max(MIN_CUT, float(target_shot)))
    impact_at = {round(i["t"], 2): i["word"] for i in impacts}

    # each clip keeps its own read head so we walk THROUGH the footage, not over its start
    heads = {c.path: c.usable[0] for c in clips}
    plan = TimelinePlan(audio_duration=audio_dur, impact_hits=impacts)
    ci = 0
    idx = 0

    for a, b in zip(points, points[1:]):
        want = b - a
        if want < MIN_CUT:
            continue
        # split anything longer than MAX_STATIC into a wide piece + a punched-in piece
        pieces = [(a, b, 1.0, "beat")]
        if want > MAX_STATIC:
            mid = a + want / 2.0
            scale = punch_scales[idx % len(punch_scales)]
            pieces = [(a, mid, 1.0, "beat"), (mid, b, scale, "jump")]

        for (pa, pb, scale, reason) in pieces:
            need = pb - pa
            src = clips[ci % len(clips)]
            ci += 1
            # Same source twice in a row is the same angle across a cut - it reads as one
            # long static shot even though a cut happened. Punch the second one in so the
            # framing actually changes. Without this, a run of 2.2s beats produced ZERO
            # punch-ins because no single slot ever crossed MAX_STATIC.
            if plan.cuts and plan.cuts[-1].source == src.path and scale == 1.0:
                scale = punch_scales[idx % len(punch_scales)]
                reason = "jump"
            lo, hi = src.usable
            head = heads.get(src.path, lo)
            if head + need > hi:                       # exhausted: rewind this clip
                head = lo
            if need > src.usable_length:               # clip shorter than the slot
                head = lo
            heads[src.path] = head + need
            word = impact_at.get(round(pa, 2))
            plan.cuts.append(Cut(
                index=idx, start=pa, end=pb, source=src.path, source_in=head,
                scale=scale, anchor=src.face_center or (0.5, 0.5),
                reason=(f"impact:{word}" if word else reason),
                impact_word=word))
            idx += 1
    return plan


# ---------------------------------------------------------------- renderer bridge

def plan_to_scenes(plan: TimelinePlan, *, name: str = "Cut") -> list[dict]:
    """Convert the plan into the scene dicts the existing renderer already understands.

    Keeps this module a drop-in: the renderer keeps its scene contract, the cutting
    decisions move here.
    """
    scenes = []
    for c in plan.cuts:
        scenes.append({
            "id": f"{c.index + 1:02d}",
            "name": f"{name} {c.index + 1}",
            "start": round(c.start, 3),
            "end": round(c.end, 3),
            "clip": Path(c.source).name,
            "asset": Path(c.source).name,
            "seedance": True,
            "seedance_start_trim": round(c.source_in, 3),
            "timeline_speed": 1.0,
            "render_caption": False,
            "overlays": [],
            "blur_captions": False,
            "punch_scale": round(c.scale, 3),
            "punch_anchor": list(c.anchor),
            "cut_reason": c.reason,
        })
    return scenes
