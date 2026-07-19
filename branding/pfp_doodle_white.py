"""Small Questions avatar, white-paper doodle edition.

Matches the channel's video frames exactly: thick black marker doodle on white paper - a
curious stick figure looking up at a big hand-drawn question mark. Every stroke is a jittered
multi-pass polyline (hand-inked look). Supersampled 4x, exported 2048x2048, circular-crop safe.
"""
import math
import os
import random

from PIL import Image, ImageDraw, ImageFilter

random.seed(7)

SS = 4
OUT_SIZE = 2048
W = H = 1024 * SS

PAPER = (250, 248, 244)          # warm paper white
INK = (24, 22, 20)               # marker black
YELLOW = (255, 205, 60)          # single accent (the channel's yellow)

img = Image.new("RGB", (W, H), PAPER)
d = ImageDraw.Draw(img)


def jitter(pts, amt):
    return [(x + random.uniform(-amt, amt), y + random.uniform(-amt, amt)) for x, y in pts]


def marker(pts, width, color=INK, passes=3, wobble=None):
    wobble = wobble if wobble is not None else width * 0.10
    for _ in range(passes):
        jp = jitter(pts, wobble)
        w = int(width * random.uniform(0.92, 1.05))
        for i in range(len(jp) - 1):
            d.line([jp[i], jp[i + 1]], fill=color, width=w)
        for (x, y) in jp:
            r = w / 2
            d.ellipse([x - r, y - r, x + r, y + r], fill=color)


def blob(cx, cy, rx, ry, color, wob=0.05, n=64):
    pts = []
    for i in range(n):
        a = 2 * math.pi * i / n
        rr = 1 + random.uniform(-wob, wob)
        pts.append((cx + math.cos(a) * rx * rr, cy + math.sin(a) * ry * rr))
    d.polygon(pts, fill=color)


def arc(cx, cy, rx, ry, a0, a1, n=48):
    return [(cx + math.cos(math.radians(a0 + (a1 - a0) * i / n)) * rx,
             cy + math.sin(math.radians(a0 + (a1 - a0) * i / n)) * ry)
            for i in range(n + 1)]


CX = W / 2
SCALE = W / 1024

# ---------------------------------------------------------------- the BIG question mark
QX = CX + 100 * SCALE
QY = 330 * SCALE
R = 180 * SCALE
STROKE = int(74 * SCALE)

# yellow blob behind the question mark = the accent that makes the avatar pop in a feed
blob(QX + 10 * SCALE, QY + 120 * SCALE, R * 1.9, R * 2.1, YELLOW, wob=0.04)

hook = arc(QX, QY, R, R * 1.03, 150, 395, 72)
end = hook[-1]
stem = [end,
        (QX + R * 0.50, QY + R * 0.80),
        (QX + R * 0.16, QY + R * 1.30),
        (QX + R * 0.02, QY + R * 1.66)]
marker(hook, STROKE)
marker(stem, STROKE)
# the dot
blob(QX - 2 * SCALE, QY + R * 2.25, 62 * SCALE, 58 * SCALE, INK, wob=0.06)

# ---------------------------------------------------------------- curious stick figure
FX = CX - 250 * SCALE
FY = 640 * SCALE
HEAD_R = 78 * SCALE
LINE = int(24 * SCALE)

# head (open circle, marker)
marker(arc(FX, FY - 190 * SCALE, HEAD_R, HEAD_R, 0, 360, 60), LINE)
# eyes looking UP-RIGHT at the question mark
for ex in (-26, 24):
    blob(FX + ex * SCALE + 10 * SCALE, FY - 210 * SCALE, 9 * SCALE, 10 * SCALE, INK)
# small open mouth (wonder)
marker(arc(FX + 12 * SCALE, FY - 152 * SCALE, 16 * SCALE, 12 * SCALE, 20, 160, 16),
       int(10 * SCALE), passes=2)
# body
marker([(FX, FY - 190 * SCALE + HEAD_R), (FX - 6 * SCALE, FY + 60 * SCALE)], LINE)
# left arm scratching the head
marker([(FX - 4 * SCALE, FY - 60 * SCALE),
        (FX - 90 * SCALE, FY - 140 * SCALE),
        (FX - 60 * SCALE, FY - 235 * SCALE)], LINE)
# right arm pointing up at the question mark
marker([(FX - 2 * SCALE, FY - 50 * SCALE),
        (FX + 110 * SCALE, FY - 110 * SCALE),
        (FX + 185 * SCALE, FY - 195 * SCALE)], LINE)
# legs
marker([(FX - 6 * SCALE, FY + 60 * SCALE), (FX - 70 * SCALE, FY + 230 * SCALE)], LINE)
marker([(FX - 6 * SCALE, FY + 60 * SCALE), (FX + 60 * SCALE, FY + 230 * SCALE)], LINE)
# feet
marker([(FX - 70 * SCALE, FY + 230 * SCALE), (FX - 110 * SCALE, FY + 226 * SCALE)], LINE)
marker([(FX + 60 * SCALE, FY + 230 * SCALE), (FX + 100 * SCALE, FY + 226 * SCALE)], LINE)

# ---------------------------------------------------------------- thought dots figure -> "?"
for i, (dx, dy, r) in enumerate(((120, -290, 9), (185, -350, 12), (250, -415, 15))):
    blob(FX + dx * SCALE, FY + dy * SCALE, r * SCALE, r * SCALE, INK, wob=0.08)

# ---------------------------------------------------------------- tiny sparkles (marker)
for (sx, sy, s) in ((190 * SCALE, 210 * SCALE, 20), (830 * SCALE, 180 * SCALE, 16),
                    (860 * SCALE, 760 * SCALE, 18)):
    for a in (0, 90, 45, 135):
        r = math.radians(a)
        marker([(sx - math.cos(r) * s * SCALE, sy - math.sin(r) * s * SCALE),
                (sx + math.cos(r) * s * SCALE, sy + math.sin(r) * s * SCALE)],
               int(8 * SCALE), passes=1)

# ---------------------------------------------------------------- paper grain + export
img = img.filter(ImageFilter.GaussianBlur(SS * 0.32))
noise = Image.effect_noise((W // 4, H // 4), 13).resize((W, H)).convert("L")
img = Image.composite(img, Image.new("RGB", (W, H), tuple(int(c * 0.985) for c in PAPER)),
                      noise.point(lambda v: 255 - min(26, max(0, v - 114))))
final = img.resize((OUT_SIZE, OUT_SIZE), Image.LANCZOS)
OUT = r"D:\data\AutoShortsClaude\branding\pfp_doodle_white.png"
final.save(OUT, optimize=True)
print("saved", OUT, round(os.path.getsize(OUT) / 1e6, 2), "MB")
