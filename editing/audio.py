"""Module 1 - Audio & Sound Design Engine.

The audio is the spine. Three jobs:

`trim_silence` strips every pause longer than `MAX_SILENCE` out of the TTS take and
returns the surviving speech PLUS a `TimeMap`. The map matters more than the audio: cut
plans, captions and SFX are all keyed to timestamps, and every one of them shifts when
silence is removed. Trimming without remapping desyncs the whole edit.

`build_bgm_bed` loops a music track to length, ducks it against the voice and mixes it
to a level that stays under the speech.

`SfxTimeline` collects event triggers - a cut, an overlay appearing - and resolves each
to a sound file and a timestamp, so the mixer gets a plain list instead of scattered
per-feature logic.
"""

from __future__ import annotations

import math
from bisect import bisect_right
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

try:
    from pydub import AudioSegment
    from pydub.silence import detect_nonsilent
except Exception:  # pragma: no cover - callers degrade to "no trimming"
    AudioSegment = None
    detect_nonsilent = None

MAX_SILENCE = 0.30        # anything longer than this is removed
KEEP_PAD = 0.08           # breathing room kept on both sides of every kept chunk
SILENCE_DBFS = -38        # below this counts as silence
BGM_VOLUME = 0.12         # 10-15% of the mix, per spec
DUCK_DB = -9.0            # how far the bed drops while the voice is speaking


# ---------------------------------------------------------------- time mapping

@dataclass
class TimeMap:
    """Maps ORIGINAL timestamps to their position after silence removal.

    `kept` is the list of (start, end) windows that survived, in original time.
    """

    kept: list[tuple[float, float]] = field(default_factory=list)

    @property
    def removed(self) -> float:
        if not self.kept:
            return 0.0
        span = self.kept[-1][1] - self.kept[0][0]
        return round(span - sum(e - s for s, e in self.kept), 3)

    def __call__(self, t: float) -> float:
        return self.map(t)

    def map(self, t: float) -> float:
        """Original time -> new time. Times inside a removed gap snap to the cut point."""
        if not self.kept:
            return float(t)
        acc = 0.0
        for s, e in self.kept:
            if t < s:
                return round(acc, 4)
            if t <= e:
                return round(acc + (t - s), 4)
            acc += e - s
        return round(acc, 4)

    def map_words(self, words: Sequence[dict], min_dur: float = 0.10) -> list[dict]:
        """Re-time a word list. NO word is ever dropped.

        An earlier version discarded words whose window had been removed, and it ate
        real speech: "a cold" and the closing "a bright, fake smile!" vanished from a
        test script because they were spoken quietly enough to look like silence. A
        caption missing words the viewer HEARS is worse than one a few frames off, so a
        collapsed word is kept and given `min_dur`, pushed after its predecessor.
        """
        out = []
        for w in words or []:
            s, e = self.map(float(w["start"])), self.map(float(w["end"]))
            if out and s < out[-1]["end"]:
                s = out[-1]["end"]
            if e - s < min_dur:
                e = s + min_dur
            out.append({**w, "start": round(s, 4), "end": round(e, 4)})
        return out

    def coverage(self, words: Sequence[dict]) -> dict:
        """How much of the word list survived inside kept speech - a data sanity check."""
        if not words:
            return {"words": 0, "outside": 0, "beyond_audio": 0}
        end = self.kept[-1][1] if self.kept else 0.0
        outside = sum(1 for w in words
                      if not any(a <= float(w["start"]) <= b for a, b in self.kept))
        beyond = sum(1 for w in words if float(w["start"]) > end)
        return {"words": len(words), "outside": outside, "beyond_audio": beyond}


# ---------------------------------------------------------------- silence

def trim_silence(in_path, out_path, *, max_silence: float = MAX_SILENCE,
                 silence_dbfs: int = SILENCE_DBFS, pad: float = KEEP_PAD):
    """Strip pauses longer than `max_silence`. Returns (out_path, TimeMap).

    Raises RuntimeError when pydub is unavailable rather than silently returning the
    untouched file - a caller that thinks it trimmed but did not would place every
    later cut on the wrong frame.
    """
    if AudioSegment is None or detect_nonsilent is None:
        raise RuntimeError("trim_silence needs pydub (pip install pydub).")
    audio = AudioSegment.from_file(str(in_path))
    min_sil_ms = int(max_silence * 1000)
    # The threshold follows the TAKE, not a fixed number. At a flat -38 dBFS a quietly
    # delivered line ("a cold...") registered as silence and was cut out with it. A
    # threshold anchored to this take's own level tracks a soft narrator and a loud one.
    thresh = min(float(silence_dbfs), silence_floor(audio, margin=18.0))
    chunks = detect_nonsilent(audio, min_silence_len=min_sil_ms,
                              silence_thresh=thresh, seek_step=5)
    if not chunks:                              # all quiet: hand back what came in
        audio.export(str(out_path), format=Path(out_path).suffix.lstrip(".") or "wav")
        return str(out_path), TimeMap(kept=[(0.0, len(audio) / 1000.0)])

    pad_ms = int(pad * 1000)
    kept, merged = [], []
    for s, e in chunks:
        s = max(0, s - pad_ms)
        e = min(len(audio), e + pad_ms)
        if merged and s <= merged[-1][1]:       # padding made them touch -> one chunk
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))

    out = AudioSegment.empty()
    for s, e in merged:
        out += audio[s:e]
        kept.append((round(s / 1000.0, 4), round(e / 1000.0, 4)))
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    out.export(str(out_path), format=Path(out_path).suffix.lstrip(".") or "wav")
    return str(out_path), TimeMap(kept=kept)


# ---------------------------------------------------------------- music bed

def build_bgm_bed(music_path, out_path, duration: float, *,
                  volume: float = BGM_VOLUME, fade: float = 1.2,
                  voice_path=None, duck_db: float = DUCK_DB):
    """Loop `music_path` to `duration`, fade both ends, mix down, optionally duck.

    Ducking is applied per 100ms window against the voice envelope, so the bed only
    steps back where speech actually is instead of sitting low for the whole video.
    """
    if AudioSegment is None:
        raise RuntimeError("build_bgm_bed needs pydub (pip install pydub).")
    music = AudioSegment.from_file(str(music_path))
    if len(music) < 200:
        raise ValueError("Music file is too short to loop.")
    need_ms = int(duration * 1000)
    bed = music * (math.ceil(need_ms / len(music)))
    bed = bed[:need_ms]
    bed = bed + (20 * math.log10(max(volume, 1e-4)))      # linear volume -> dB
    fade_ms = int(min(fade, duration / 4.0) * 1000)
    if fade_ms > 0:
        bed = bed.fade_in(fade_ms).fade_out(fade_ms)

    if voice_path and Path(str(voice_path)).exists():
        voice = AudioSegment.from_file(str(voice_path))
        step = 100
        ducked = AudioSegment.empty()
        for i in range(0, len(bed), step):
            win = bed[i:i + step]
            v = voice[i:i + step]
            if len(v) and v.dBFS > silence_floor(voice):
                win = win + duck_db
            ducked += win
        bed = ducked
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    bed.export(str(out_path), format=Path(out_path).suffix.lstrip(".") or "wav")
    return str(out_path)


def silence_floor(seg, margin: float = 12.0) -> float:
    """A per-take silence threshold: the take's own level minus a margin."""
    try:
        return float(seg.dBFS) - margin
    except Exception:  # noqa: BLE001
        return -45.0


# ---------------------------------------------------------------- sfx events

# event kind -> filename fragment searched for in the sfx library
EVENT_SOUNDS = {
    "cut": ("whoosh", "swipe", "transition"),
    "overlay": ("pop", "ding", "blink"),
    "impact": ("impact", "boom", "hit"),
    "caption": ("pop", "click"),
}


@dataclass
class SfxTimeline:
    """Event-triggered sound effects, resolved against a library folder."""

    library: str
    _events: list[dict] = field(default_factory=list)

    def _resolve(self, kind: str) -> str | None:
        wants = EVENT_SOUNDS.get(kind, ())
        files = [p for p in Path(self.library).rglob("*")
                 if p.suffix.lower() in (".mp3", ".wav", ".ogg", ".m4a")]
        for frag in wants:
            for p in files:
                if frag in p.name.lower():
                    return str(p)
        return None

    def on_cut(self, t: float, gain: float = 0.55):
        return self.add("cut", t, gain)

    def on_overlay(self, t: float, gain: float = 0.6):
        return self.add("overlay", t, gain)

    def add(self, kind: str, t: float, gain: float = 0.55):
        path = self._resolve(kind)
        if not path:
            return None
        ev = {"kind": kind, "start": round(float(t), 3), "path": path,
              "volume": round(float(gain), 3)}
        self._events.append(ev)
        return ev

    def from_cuts(self, cuts: Iterable, *, skip_first: bool = True):
        """One whoosh per cut. The first cut is the video opening, not a transition."""
        for i, c in enumerate(cuts):
            if skip_first and i == 0:
                continue
            self.on_cut(getattr(c, "start", c.get("start") if isinstance(c, dict) else 0.0))
        return self

    def events(self, min_gap: float = 0.12) -> list[dict]:
        """Sorted, de-clustered: two triggers closer than `min_gap` become one."""
        out = []
        for ev in sorted(self._events, key=lambda e: e["start"]):
            if out and ev["start"] - out[-1]["start"] < min_gap:
                continue
            out.append(ev)
        return out
