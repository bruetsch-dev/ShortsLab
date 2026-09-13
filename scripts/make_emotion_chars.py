"""Generate kawaii pixel NEKO (chibi cat) emotion characters in the Shortslab palette.

Follows the classic kawaii/chibi design rules (drawingforall.net, catdrawing.app):
  - CHIBI proportions: oversized head on a tiny body with stubby paws + a curled tail
  - big round eyes placed LOW on the face (large forehead), each with round sparkle highlights
  - tiny pink nose + tiny :3 mouth, close together right under the eyes
  - chubby cheeks with soft blush; small rounded ears (droop when sad, flatten when angry)
  - flat colors, thick ink outline + the signature hard offset shadow so it pops over footage

One cat per emotion: happy, laughing, sad, crying, shocked, scared, angry, love, cool,
thinking, surprised, confused. Red/blue accents carry the emotion (heart eyes, tears, sweat,
anger mark, sunglasses...). Drawn at 40px with PIL and scaled NEAREST so pixels stay crisp.

Run: python scripts/make_emotion_chars.py  ->  static/emotions/<emotion>.png
"""

from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "static" / "emotions"

# palette (from the site CSS :root)
CREAM = (251, 247, 238, 255)
INK = (23, 21, 15, 255)
RED = (232, 71, 43, 255)
BLUE = (47, 111, 214, 255)
PINK = (240, 150, 150, 255)
WHITE = (255, 255, 255, 255)

G = 42                      # logical canvas (GxG; 1px safety margin so outline/shadow never wrap)
CELL = 13                   # -> 546x546


def _shift(m, dy, dx):
    """np.roll without wrap-around (shapes near the border must not teleport across)."""
    out = np.zeros_like(m)
    ys, yd = (dy, None) if dy >= 0 else (None, dy)
    src_y = slice(0, m.shape[0] - dy) if dy >= 0 else slice(-dy, m.shape[0])
    dst_y = slice(dy, m.shape[0]) if dy >= 0 else slice(0, m.shape[0] + dy)
    src_x = slice(0, m.shape[1] - dx) if dx >= 0 else slice(-dx, m.shape[1])
    dst_x = slice(dx, m.shape[1]) if dx >= 0 else slice(0, m.shape[1] + dx)
    out[dst_y, dst_x] = m[src_y, src_x]
    return out

# chibi layout: BIG wide head, low eyes, tiny body + paws
HEAD = (4.0, 6.0, 36.0, 30.0)          # wide ellipse (cx 20, cy 18)
EXL, EXR, EY = 13.0, 27.0, 21.5        # eyes LOW on the face + wide apart
NOSE_Y, MOUTH_Y = 24.2, 25.4


def _draw_body(bd, emotion):
    """Chibi cat silhouette: ears + big head + small body + paws + curled tail."""
    if emotion in ("sad", "crying"):
        lear = [(3.0, 12.0), (7.5, 6.5), (15.0, 8.5)]          # droopy
        rear = [(37.0, 12.0), (32.5, 6.5), (25.0, 8.5)]
    elif emotion in ("angry", "scared"):
        lear = [(2.5, 9.0), (7.0, 5.5), (14.5, 8.0)]           # flattened out
        rear = [(37.5, 9.0), (33.0, 5.5), (25.5, 8.0)]
    else:
        lear = [(8.0, 1.5), (5.5, 10.5), (16.0, 7.0)]          # perky, short + round-ish
        rear = [(32.0, 1.5), (34.5, 10.5), (24.0, 7.0)]
    bd.polygon(lear, fill=CREAM)
    bd.polygon(rear, fill=CREAM)
    # tail: a solid chubby nub hugging the body's lower right (Pusheen-style)
    bd.ellipse((28.5, 31.0, 38.0, 38.0), fill=CREAM)
    # small body + stubby front paws
    bd.ellipse((10.5, 24.0, 29.5, 38.5), fill=CREAM)
    bd.ellipse((13.0, 33.5, 18.5, 38.5), fill=CREAM)
    bd.ellipse((21.5, 33.5, 27.0, 38.5), fill=CREAM)
    # BIG head on top (covers the body top)
    bd.ellipse(HEAD, fill=CREAM)
    return lear, rear


def _inner_ears(dr, lear, rear):
    def lerp(a, b, t):
        return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)
    for tri in (lear, rear):
        tip, b1, b2 = tri
        dr.polygon([lerp(tip, b1, 0.22), lerp(tip, b1, 0.72), lerp(tip, b2, 0.72)], fill=PINK)


def _paw_lines(dr):
    dr.line([(15.7, 35.4), (15.7, 37.8)], fill=INK, width=1)   # toe splits
    dr.line([(24.2, 35.4), (24.2, 37.8)], fill=INK, width=1)


def _eye_round(dr, ex, r=3.9):
    """Big round shiny kawaii eye: mostly black bead with a small round sparkle pair."""
    dr.ellipse((ex - r, EY - r, ex + r, EY + r), fill=INK)
    s = r * 0.42
    dr.ellipse((ex - r * 0.52, EY - r * 0.58, ex - r * 0.52 + s, EY - r * 0.58 + s), fill=WHITE)
    t = r * 0.22
    dr.ellipse((ex + r * 0.25, EY + r * 0.2, ex + r * 0.25 + t, EY + r * 0.2 + t), fill=WHITE)


def _eye_ring(dr, ex, r=4.2):
    dr.ellipse((ex - r, EY - r, ex + r, EY + r), fill=INK)
    dr.ellipse((ex - r + 1.5, EY - r + 1.5, ex + r - 1.5, EY + r - 1.5), fill=WHITE)
    dr.ellipse((ex - 1.3, EY - 1.3, ex + 1.3, EY + 1.3), fill=INK)


def _eye_happy(dr, ex):                                        # ^ content arc
    dr.arc((ex - 3.3, EY - 2.4, ex + 3.3, EY + 2.8), 180, 360, fill=INK, width=2)


def _eye_shut(dr, ex):                                         # sleepy closed lid
    dr.pieslice((ex - 3.2, EY - 2.6, ex + 3.2, EY + 2.6), 0, 180, fill=INK)


def _eye_heart(dr, ex):
    dr.ellipse((ex - 3.6, EY - 3.2, ex + 0.2, EY + 0.4), fill=RED)
    dr.ellipse((ex - 0.2, EY - 3.2, ex + 3.6, EY + 0.4), fill=RED)
    dr.polygon([(ex - 3.4, EY - 0.6), (ex + 3.4, EY - 0.6), (ex, EY + 3.8)], fill=RED)


def _nose(dr):
    dr.polygon([(19.1, NOSE_Y), (20.9, NOSE_Y), (20.0, NOSE_Y + 1.2)], fill=PINK)


def _mouth(dr, mode="omega"):
    y = MOUTH_Y
    if mode == "omega":                                        # tiny :3 right under the nose
        dr.arc((17.6, y, 20.0, y + 2.0), 0, 180, fill=INK, width=1)
        dr.arc((20.0, y, 22.4, y + 2.0), 0, 180, fill=INK, width=1)
    elif mode == "frown":
        dr.arc((17.8, y + 0.8, 22.2, y + 3.4), 180, 360, fill=INK, width=2)
    elif mode == "open":                                       # small laugh + tongue
        dr.ellipse((17.6, y, 22.4, y + 3.4), fill=INK)
        dr.ellipse((18.6, y + 1.8, 21.4, y + 3.2), fill=RED)
    elif mode == "o":
        dr.ellipse((18.6, y, 21.4, y + 2.8), fill=INK)
    elif mode == "O":
        dr.ellipse((17.8, y - 0.2, 22.2, y + 3.6), fill=INK)
    elif mode == "wavy":
        dr.line([(17.2, y + 1.2), (18.5, y + 0.4), (19.8, y + 1.2), (21.1, y + 0.4), (22.4, y + 1.2)],
                fill=INK, width=1)
    elif mode == "smirk":
        dr.line([(17.6, y + 1.0), (20.2, y + 1.5), (22.4, y + 0.4)], fill=INK, width=2)
    elif mode == "flat":
        dr.line([(18.4, y + 1.0), (21.6, y + 0.8)], fill=INK, width=2)


def _whiskers(dr):
    # whiskers read as stray pixels at this resolution - cuter without (kawaii = fewer details)
    return


def _blush(dr, color=PINK):                                    # chubby-cheek blush, wide + low
    dr.ellipse((5.4, 25.4, 9.2, 27.4), fill=color)
    dr.ellipse((30.8, 25.4, 34.6, 27.4), fill=color)


def _face(dr, emotion):
    e = emotion
    if e == "happy":
        _eye_round(dr, EXL); _eye_round(dr, EXR); _nose(dr); _mouth(dr, "omega"); _blush(dr)
    elif e == "laughing":
        _eye_happy(dr, EXL); _eye_happy(dr, EXR); _nose(dr); _mouth(dr, "open"); _blush(dr)
    elif e == "sad":
        _eye_shut(dr, EXL); _eye_shut(dr, EXR); _nose(dr); _mouth(dr, "frown")
    elif e == "crying":
        _eye_shut(dr, EXL); _eye_shut(dr, EXR); _nose(dr); _mouth(dr, "frown")
        for ex in (EXL, EXR):
            dr.rectangle((ex - 0.8, EY + 1.8, ex + 0.8, EY + 6.0), fill=BLUE)
            dr.ellipse((ex - 1.1, EY + 5.6, ex + 1.1, EY + 7.8), fill=BLUE)
    elif e == "shocked":
        _eye_ring(dr, EXL); _eye_ring(dr, EXR); _nose(dr); _mouth(dr, "O")
    elif e == "scared":
        _eye_ring(dr, EXL, r=3.6); _eye_ring(dr, EXR, r=3.6); _nose(dr); _mouth(dr, "wavy")
        dr.polygon([(31.6, 12.6), (33.8, 12.6), (32.7, 9.6)], fill=BLUE)
        dr.ellipse((31.4, 11.8, 34.0, 14.4), fill=BLUE)
    elif e == "angry":
        _eye_round(dr, EXL, r=3.2); _eye_round(dr, EXR, r=3.2)
        dr.line([(EXL - 3.8, EY - 6.0), (EXL + 2.4, EY - 3.8)], fill=INK, width=2)
        dr.line([(EXR + 3.8, EY - 6.0), (EXR - 2.4, EY - 3.8)], fill=INK, width=2)
        _nose(dr); _mouth(dr, "frown")
        dr.polygon([(21.4, MOUTH_Y + 1.2), (23.0, MOUTH_Y + 1.2), (22.2, MOUTH_Y + 2.8)], fill=WHITE)
        dr.line([(33.4, 2.4), (36.8, 5.8)], fill=RED, width=2)
        dr.line([(36.8, 2.4), (33.4, 5.8)], fill=RED, width=2)
    elif e == "love":
        _eye_heart(dr, EXL); _eye_heart(dr, EXR); _nose(dr); _mouth(dr, "omega"); _blush(dr, RED)
        for hx, hy in ((3.4, 4.4), (36.6, 3.4)):
            dr.ellipse((hx - 1.3, hy - 1.3, hx + 0.1, hy + 0.1), fill=RED)
            dr.ellipse((hx - 0.1, hy - 1.3, hx + 1.3, hy + 0.1), fill=RED)
            dr.polygon([(hx - 1.3, hy - 0.4), (hx + 1.3, hy - 0.4), (hx, hy + 1.5)], fill=RED)
    elif e == "cool":
        dr.rounded_rectangle((8.8, 18.4, 17.4, 24.6), radius=1, fill=INK)
        dr.rounded_rectangle((22.6, 18.4, 31.2, 24.6), radius=1, fill=INK)
        dr.line([(17.4, 20.2), (22.6, 20.2)], fill=INK, width=1)
        dr.line([(8.8, 20.0), (5.4, 19.2)], fill=INK, width=1)
        dr.line([(31.2, 20.0), (34.6, 19.2)], fill=INK, width=1)
        _nose(dr); _mouth(dr, "smirk")
    elif e == "thinking":
        for ex in (EXL, EXR):                                  # pupils glance up-right
            dr.ellipse((ex - 1.2, EY - 3.8, ex + 3.0, EY + 0.4), fill=INK)
            dr.ellipse((ex + 0.4, EY - 3.0, ex + 1.8, EY - 1.6), fill=WHITE)
        _nose(dr); _mouth(dr, "flat")
        for bx, by, r in ((33.6, 15.4, 0.7), (35.4, 12.4, 1.0), (37.2, 9.0, 1.3)):
            dr.ellipse((bx - r, by - r, bx + r, by + r), fill=INK)
    elif e == "surprised":
        _eye_ring(dr, EXL, r=3.8); _eye_ring(dr, EXR, r=3.8); _nose(dr); _mouth(dr, "o")
        dr.rectangle((36.0, 2.4, 37.6, 6.8), fill=RED)
        dr.ellipse((35.9, 7.8, 37.7, 9.6), fill=RED)
    elif e == "confused":
        _eye_round(dr, EXL)
        dr.arc((EXR - 3.3, EY - 2.0, EXR + 3.3, EY + 2.8), 180, 360, fill=INK, width=2)
        _nose(dr); _mouth(dr, "wavy")
        dr.arc((33.0, 2.2, 37.6, 6.8), 140, 40, fill=RED, width=2)
        dr.line([(35.6, 6.4), (35.6, 8.0)], fill=RED, width=2)
        dr.ellipse((34.8, 9.0, 36.4, 10.6), fill=RED)
    else:
        _eye_round(dr, EXL); _eye_round(dr, EXR); _nose(dr); _mouth(dr, "omega")


def build(emotion):
    # 1) chibi cat silhouette
    body = Image.new("RGBA", (G, G), (0, 0, 0, 0))
    bd = ImageDraw.Draw(body)
    lear, rear = _draw_body(bd, emotion)

    # 2) ink outline around the silhouette + hard offset shadow underneath
    a = np.array(body)
    mask = a[..., 3] > 0
    edge = np.zeros_like(mask)
    for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        edge |= _shift(mask, dy, dx)
    edge &= ~mask
    sil = mask | edge
    shadow = _shift(_shift(sil, 1, 0), 0, 1) & ~sil

    out = np.zeros((G, G, 4), dtype=np.uint8)
    out[shadow] = INK
    out[mask] = a[mask]
    out[edge] = INK
    img = Image.fromarray(out)   # uint8 HxWx4 -> RGBA

    # 3) face + details on top (post-outline so lines stay thin)
    dr = ImageDraw.Draw(img)
    _inner_ears(dr, lear, rear)
    dr.line([(10.5, 30.6), (13.5, 29.4)], fill=INK, width=1)   # chin/body crease l
    dr.line([(29.5, 30.6), (26.5, 29.4)], fill=INK, width=1)   # r
    _paw_lines(dr)
    _face(dr, emotion)
    _whiskers(dr)
    return img


EMOTIONS = ["happy", "laughing", "sad", "crying", "shocked", "scared",
            "angry", "love", "cool", "thinking", "surprised", "confused"]


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    for emo in EMOTIONS:
        small = build(emo)
        big = small.resize((G * CELL, G * CELL), Image.NEAREST)
        big.save(OUT / f"{emo}.png")
    print(f"Wrote {len(EMOTIONS)} kawaii chibi neko characters to {OUT}")


if __name__ == "__main__":
    main()
