"""Subtitles for every mode, cut by one set of rules.

The subtitles are not decoration on top of a finished edit - they ARE the edit clock for the
sketch and longform modes, and the record of what was said for the clip modes. Each mode used
to invent its own idea of a caption, so the same kind of narration came out shaped differently
depending on which button produced it.

The limits below are MEASURED off a subtitle file the user cut by hand and called correct, not
chosen from a style guide: 547 cues, at most 2 display lines of at most 44 characters, at most
70 characters and 11 words per cue, 0.41-3.92 seconds long, and 72% ending on a sentence.
"""
from __future__ import annotations

import re
from pathlib import Path

SRT_MAX_CHARS = 70
SRT_MAX_LINE_CHARS = 44
SRT_MAX_WORDS = 11
SRT_MAX_SECONDS = 4.0
SRT_MIN_SECONDS = 0.35
SRT_GAP = 0.03
SRT_PAUSE = 0.5


def srt_clock(seconds):
    """SubRip's own time format: HH:MM:SS,mmm."""
    total = max(0.0, float(seconds or 0.0))
    ms = int(round(total * 1000.0))
    hours, ms = divmod(ms, 3600000)
    minutes, ms = divmod(ms, 60000)
    secs, ms = divmod(ms, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"


def wrap_subtitle(text, width=SRT_MAX_LINE_CHARS):
    """Break one cue into at most two display lines, balanced.

    Splitting at the width limit alone leaves a second line holding one word. Aiming at the
    middle and then taking the nearest word boundary keeps both lines readable.
    """
    words = str(text or "").split()
    if not words:
        return []
    if len(" ".join(words)) <= width:
        return [" ".join(words)]
    best, target = None, len(" ".join(words)) / 2.0
    for cut in range(1, len(words)):
        left = " ".join(words[:cut])
        right = " ".join(words[cut:])
        if len(left) > width:
            break
        score = (max(len(left), len(right)) > width, abs(len(left) - target))
        if best is None or score < best[0]:
            best = (score, [left, right])
    return best[1] if best else [" ".join(words)]


def _word_text(entry):
    return str(entry.get("word") or entry.get("w") or "").strip()


def subtitle_cues(aligned):
    """Group forced-aligned words into subtitle cues.

    `aligned` is voice_align's output: one entry per spoken word with its real start and end.
    A cue closes on a finished sentence, on a real pause, or when it would outgrow the reading
    limits above - in that order of preference, so most cues end where a thought does.
    """
    cues, current = [], []

    def flush():
        if not current:
            return
        start = float(current[0].get("start") or 0.0)
        end = float(current[-1].get("end") or start)
        cues.append({"start": round(start, 3),
                     "end": round(max(end, start + 0.05), 3),
                     "text": " ".join(_word_text(w) for w in current if _word_text(w)),
                     "words": [{"w": _word_text(w), "s": round(float(w.get("start") or 0.0), 2)}
                               for w in current]})
        current.clear()

    for index, entry in enumerate(aligned or ()):
        word = _word_text(entry)
        if not word:
            continue
        if current:
            grown = len(" ".join(_word_text(w) for w in current)) + 1 + len(word)
            span = float(entry.get("end") or 0.0) - float(current[0].get("start") or 0.0)
            if grown > SRT_MAX_CHARS or len(current) + 1 > SRT_MAX_WORDS or span > SRT_MAX_SECONDS:
                flush()
        current.append(entry)
        # A closing quote or bracket sits AFTER the full stop; strip it before asking.
        if word.rstrip("\"')]»”’").endswith((".", "!", "?", ":", "…")):
            flush()
            continue
        nxt = aligned[index + 1] if index + 1 < len(aligned) else None
        if nxt and float(nxt.get("start") or 0.0) - float(entry.get("end") or 0.0) >= SRT_PAUSE:
            flush()
    flush()

    return tidy_cues(cues)


def tidy_cues(cues):
    """No cue may run into the next one, and none may flash by unreadably."""
    cues = list(cues or ())
    for i, cue in enumerate(cues):
        cue["start"] = round(max(0.0, float(cue.get("start") or 0.0)), 3)
        cue["end"] = round(max(float(cue.get("end") or 0.0), cue["start"] + 0.05), 3)
        if i + 1 < len(cues):
            nxt = float(cues[i + 1].get("start") or 0.0)
            cue["end"] = round(min(cue["end"], max(cue["start"] + 0.05, nxt - SRT_GAP)), 3)
        if cue["end"] - cue["start"] < SRT_MIN_SECONDS:
            room = (float(cues[i + 1]["start"]) - SRT_GAP) if i + 1 < len(cues) else cue["end"] + 5.0
            cue["end"] = round(max(cue["start"] + 0.05,
                                   min(room, cue["start"] + SRT_MIN_SECONDS)), 3)
    return cues


def write_srt(cues, out_path):
    """Write cues as a SubRip file. Returns the path."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    blocks = []
    for number, cue in enumerate(cues, start=1):
        body = chr(10).join(wrap_subtitle(cue.get("text")))
        blocks.append(f"{number}{chr(10)}"
                      f"{srt_clock(cue.get('start'))} --> {srt_clock(cue.get('end'))}{chr(10)}"
                      f"{body}{chr(10)}")
    out_path.write_text(chr(10).join(blocks), encoding="utf-8")
    return out_path


_SRT_TIME = re.compile(r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)")


def parse_srt(source):
    """Read a SubRip file (or its text) into [{start, end, text}].

    Tolerant on purpose: the file may come from this app, from a subtitle editor, or hand-typed,
    so a stray blank line or a missing cue number must not lose the rest of the timings.
    """
    path = Path(str(source)) if not str(source).lstrip().startswith(("1", "﻿1")) else None
    try:
        raw = path.read_text(encoding="utf-8-sig") if path and path.is_file() else str(source)
    except OSError:
        raw = str(source)
    cues = []
    for block in re.split(r"\n\s*\n", raw.replace("\r\n", "\n").strip()):
        lines = [ln for ln in block.strip().splitlines() if ln.strip()]
        stamp = next((_SRT_TIME.search(ln) for ln in lines if _SRT_TIME.search(ln)), None)
        if not stamp:
            continue
        g = [int(x) for x in stamp.groups()]
        body = lines[lines.index(next(ln for ln in lines if _SRT_TIME.search(ln))) + 1:]
        cues.append({"start": round(g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000.0, 3),
                     "end": round(g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000.0, 3),
                     "text": " ".join(" ".join(body).split())})
    return cues
