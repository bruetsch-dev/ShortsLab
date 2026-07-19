"""Small Questions - YouTube channel banner, 2560x1440, white-paper doodle style.

Same recipe as the Fuji Facts banner: everything critical (icon + wordmark + tagline) auto-fit
inside YouTube's universal safe area (central 1546x423); the rest of the canvas is doodle
decoration for TV/desktop crops. Marker strokes are jittered multi-pass polylines like the
channel's own video frames."""
import math
import os
import random

from PIL import Image, ImageDraw, ImageFilter, ImageFont

random.seed(11)

SS = 2
BW, BH = 2560, 1440
W, H = BW * SS, BH * SS

PAPER = (250, 248, 244)
INK = (24, 22, 20)
YELLOW = (255, 205, 60)

CX, CY = W / 2, H / 2
SAFE_W, SAFE_H = 1546 * SS, 423 * SS
SAFE_X0 = CX - SAFE_W / 2

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


def question_mark(qx, qy, r, stroke, dot_r):
    hook = arc(qx, qy, r, r * 1.03, 150, 395, 60)
    end = hook[-1]
    stem = [end, (qx + r * 0.50, qy + r * 0.80), (qx + r * 0.16, qy + r * 1.30),
            (qx + r * 0.02, qy + r * 1.66)]
    marker(hook, stroke)
    marker(stem, stroke)
    blob(qx, qy + r * 2.25, dot_r, dot_r * 0.94, INK, wob=0.06)


def sparkle(sx, sy, s, width):
    for a in (0, 90, 45, 135):
        rr = math.radians(a)
        marker([(sx - math.cos(rr) * s, sy - math.sin(rr) * s),
                (sx + math.cos(rr) * s, sy + math.sin(rr) * s)], width, passes=1)


# ---------------------------------------------------------------- decoration (outside safe area)
# faint scattered mini question marks + sparkles across the full canvas
FAINT = tuple(int(c * 0.90) for c in PAPER[:2]) + (int(PAPER[2] * 0.88),)
for _ in range(10):
    x = random.uniform(80 * SS, W - 80 * SS)
    y = random.choice([random.uniform(60 * SS, H * 0.24), random.uniform(H * 0.76, H - 60 * SS)])
    s = random.uniform(26, 44) * SS
    pts = arc(x, y, s, s, -210, 40, 20) + [(x + s * 0.45, y + s * 1.1)]
    marker(pts, int(10 * SS), color=(205, 200, 192), passes=1)
sparkle(W * 0.06, H * 0.14, 26 * SS, int(9 * SS))
sparkle(W * 0.94, H * 0.12, 22 * SS, int(9 * SS))
sparkle(W * 0.93, H * 0.87, 26 * SS, int(9 * SS))
sparkle(W * 0.07, H * 0.86, 20 * SS, int(9 * SS))

# ---------------------------------------------------------------- safe-area lockup
def load_font(px):
    for name in ("ariblk.ttf", "arialbd.ttf", "seguisb.ttf", "arial.ttf"):
        try:
            return ImageFont.truetype(name, px)
        except OSError:
            continue
    return ImageFont.load_default()


TITLE = "SMALL QUESTIONS"
icon_w = SAFE_H * 0.86
gap = SAFE_H * 0.24
margin = SAFE_W * 0.03
size = int(SAFE_H * 0.46)
while size > 10:
    font = load_font(size)
    if icon_w + gap + d.textlength(TITLE, font=font) <= SAFE_W - 2 * margin:
        break
    size -= 4

lock_w = icon_w + gap + d.textlength(TITLE, font=font)
lock_x0 = SAFE_X0 + (SAFE_W - lock_w) / 2

# icon: yellow blob + marker question mark (the avatar in miniature)
icon_cx = lock_x0 + icon_w / 2
icon_cy = CY - SAFE_H * 0.02
blob(icon_cx, icon_cy, icon_w * 0.52, icon_w * 0.56, YELLOW, wob=0.05)
question_mark(icon_cx, icon_cy - SAFE_H * 0.16, SAFE_H * 0.16, int(SAFE_H * 0.065),
              SAFE_H * 0.055)

# wordmark: SMALL QUESTIONS in ink, with a yellow marker underline swipe
text_x = lock_x0 + icon_w + gap
base_line = CY - SAFE_H * 0.06
d.text((text_x, base_line), TITLE, font=font, fill=INK, anchor="lm")
tw = d.textlength(TITLE, font=font)
# hand-drawn underline (yellow highlighter)
u_y = base_line + size * 0.42
marker([(text_x - 6 * SS, u_y), (text_x + tw * 0.35, u_y + 3 * SS),
        (text_x + tw * 0.7, u_y - 2 * SS), (text_x + tw + 6 * SS, u_y + 2 * SS)],
       int(SAFE_H * 0.05), color=YELLOW, passes=2)

# tagline (auto-fit)
TAG = "Big answers to the questions you never thought to ask"
sub_size = int(SAFE_H * 0.115)
while sub_size > 8:
    sub_font = load_font(sub_size)
    if text_x + d.textlength(TAG, font=sub_font) <= SAFE_X0 + SAFE_W - margin:
        break
    sub_size -= 2
d.text((text_x + SAFE_W * 0.004, CY + SAFE_H * 0.30), TAG, font=sub_font, fill=INK, anchor="lm")

# ---------------------------------------------------------------- paper grain + export
img = img.filter(ImageFilter.GaussianBlur(SS * 0.3))
noise = Image.effect_noise((W // 4, H // 4), 12).resize((W, H)).convert("L")
img = Image.composite(img, Image.new("RGB", (W, H), tuple(int(c * 0.985) for c in PAPER)),
                      noise.point(lambda v: 255 - min(24, max(0, v - 115))))
final = img.resize((BW, BH), Image.LANCZOS)
OUT = r"D:\data\AutoShortsClaude\branding\banner_small_questions.png"
final.save(OUT, optimize=True)
print("saved", OUT, round(os.path.getsize(OUT) / 1e6, 2), "MB")

sx0, sy0 = int((BW - 1546) / 2), int((BH - 423) / 2)
final.crop((sx0, sy0, sx0 + 1546, sy0 + 423)).save(
    r"D:\data\AutoShortsClaude\branding\banner_small_questions_safearea.png")
print("safe-area preview saved")
