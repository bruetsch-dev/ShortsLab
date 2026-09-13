"""Module 3 - Dynamic Captioning System.

Replaces the single-word white caption with grouped, styled, colour-coded text that
pops in.

`group_words` batches word timings into 1-3 word cards sized by READING TIME, not word
count: a card carrying long words is given fewer of them, so every card stays on screen
long enough to be read.

`classify` marks the words worth colouring - strong verbs, negatives, numbers - so the
eye lands on the meaning instead of on every word equally.

`CaptionStyle` carries the typography (heavy face, thick stroke, drop shadow) and
`pop_scale` returns the per-frame scale of the 110%-to-100% spawn animation, so the
renderer only has to apply a number.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

WHITE = "#FFFFFF"
YELLOW = "#FFFF00"
RED = "#FF0000"

MAX_WORDS = 3
MIN_WORDS = 1
READ_CPS = 13.0          # characters a viewer comfortably reads per second
MIN_CARD = 0.28          # a card shorter than this is unreadable, merge it forward
POP_FRAMES = 4           # frames the 110% -> 100% spawn takes
POP_START = 1.10

# Colour buckets. Deliberately a readable word list rather than a tagger dependency:
# it is inspectable, editable and has no model to keep in sync.
NEGATIVE = {
    "never", "no", "not", "nothing", "nobody", "worst", "bad", "wrong", "fail",
    "failed", "broke", "broken", "dead", "die", "dying", "lost", "lose", "quit",
    "fired", "banned", "illegal", "trapped", "stuck", "alone", "empty", "hate",
    "angry", "scared", "afraid", "nightmare", "brutal", "harsh", "cruel", "toxic",
    "debt", "poor", "ruined", "destroyed", "collapse", "crisis", "danger",
}
STRONG = {
    "shocking", "insane", "crazy", "unbelievable", "secret", "hidden", "exposed",
    "caught", "suddenly", "instantly", "immediately", "everything", "everyone",
    "always", "forever", "huge", "massive", "tiny", "perfect", "free", "forced",
    "must", "only", "first", "last", "real", "truth", "actually", "literally",
    "explodes", "vanished", "transforms", "reveals", "demands", "refuses",
}
_STRIP = re.compile(r"[^\w']+", re.UNICODE)
_NUMERIC = re.compile(r"\d")


def _norm(word: str) -> str:
    return _STRIP.sub("", str(word or "")).lower()


def classify(word: str) -> str:
    """Colour for one word: red for negatives, yellow for strong words and numbers."""
    w = _norm(word)
    if not w:
        return WHITE
    if w in NEGATIVE:
        return RED
    if w in STRONG or _NUMERIC.search(str(word)):
        return YELLOW
    return WHITE


# ---------------------------------------------------------------- style

@dataclass
class CaptionStyle:
    font: str = "Montserrat-Black"
    font_size: int = 96
    stroke_width: int = 10          # thick black outline - readable on any background
    stroke_color: str = "#000000"
    shadow_offset: tuple[int, int] = (0, 6)
    shadow_color: str = "#000000AA"
    base_color: str = WHITE
    uppercase: bool = True
    pop_frames: int = POP_FRAMES
    pop_start: float = POP_START

    def pop_scale(self, frame_index: int) -> float:
        """Scale for the nth frame of a card: 110% shrinking to 100%, then flat."""
        if frame_index >= self.pop_frames:
            return 1.0
        t = (frame_index + 1) / float(max(1, self.pop_frames))
        return round(self.pop_start + (1.0 - self.pop_start) * t, 4)

    def as_dict(self) -> dict:
        return {"font": self.font, "font_size": self.font_size,
                "stroke_width": self.stroke_width, "stroke_color": self.stroke_color,
                "shadow_offset": list(self.shadow_offset),
                "shadow_color": self.shadow_color, "base_color": self.base_color,
                "uppercase": self.uppercase, "pop_frames": self.pop_frames,
                "pop_start": self.pop_start}


@dataclass
class CaptionCard:
    start: float
    end: float
    words: list[dict] = field(default_factory=list)   # {word, start, end, color}

    @property
    def text(self) -> str:
        return " ".join(w["word"] for w in self.words)

    @property
    def duration(self) -> float:
        return round(self.end - self.start, 3)

    def as_dict(self) -> dict:
        return {"start": round(self.start, 3), "end": round(self.end, 3),
                "text": self.text,
                "words": [{"word": w["word"], "color": w["color"],
                           "start": round(w["start"] - self.start, 3),
                           "end": round(w["end"] - self.start, 3)} for w in self.words]}


# ---------------------------------------------------------------- grouping

def group_words(words: Sequence[dict], *, max_words: int = MAX_WORDS,
                read_cps: float = READ_CPS, uppercase: bool = True) -> list[CaptionCard]:
    """Batch word timings into 1-3 word cards sized by reading time.

    A card closes when adding the next word would push it past what the viewer can read
    in the time the card is on screen. Long words therefore travel alone, short ones
    group - the old fixed rule left "extraordinary transformation" on screen for the
    same 0.4s as "is a".
    """
    cards: list[CaptionCard] = []
    cur: list[dict] = []

    def flush():
        if not cur:
            return
        cards.append(CaptionCard(start=cur[0]["start"], end=cur[-1]["end"],
                                 words=list(cur)))
        cur.clear()

    for w in words or []:
        word = str(w.get("word", "")).strip()
        if not word:
            continue
        item = {"word": word.upper() if uppercase else word,
                "start": float(w["start"]), "end": float(w["end"]),
                "color": classify(word)}
        if not cur:
            cur.append(item)
            continue
        span = item["end"] - cur[0]["start"]
        chars = sum(len(x["word"]) for x in cur) + len(item["word"]) + len(cur)
        too_long = len(cur) >= max_words
        too_dense = span > 0 and (chars / span) > read_cps
        if too_long or too_dense:
            flush()
        cur.append(item)
    flush()

    # a card too short to read is merged into the next one
    merged: list[CaptionCard] = []
    for card in cards:
        if merged and card.duration < MIN_CARD and len(merged[-1].words) < max_words:
            merged[-1].words.extend(card.words)
            merged[-1].end = card.end
        else:
            merged.append(card)
    return merged


def build_caption_track(words: Sequence[dict], style: CaptionStyle | None = None) -> dict:
    """Everything the renderer needs: the style block plus the cards."""
    style = style or CaptionStyle()
    cards = group_words(words, uppercase=style.uppercase)
    return {"style": style.as_dict(),
            "cards": [c.as_dict() for c in cards],
            "card_count": len(cards),
            "colored_words": sum(1 for c in cards for w in c.words
                                 if w["color"] != WHITE)}


def find_font(name: str = "Montserrat-Black", search: Sequence[str] = ()) -> str | None:
    """Locate a font file by name; returns None so the caller can fall back loudly."""
    roots = [Path(p) for p in search] or [Path("assets/fonts"), Path("C:/Windows/Fonts")]
    stem = name.lower().replace("-", "").replace(" ", "")
    for root in roots:
        if not root.exists():
            continue
        for p in root.rglob("*"):
            if p.suffix.lower() in (".ttf", ".otf") and \
               p.stem.lower().replace("-", "").replace(" ", "") == stem:
                return str(p)
    return None
