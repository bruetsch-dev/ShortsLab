"""'Small Questions' avatar v2 - procedurally drawn, HD.

Upgrades over v1:
- Catmull-Rom smoothed paths + a STAMP brush (overlapping circles along the arc-length) with
  variable width and smooth sine wobble -> clean marker strokes, no lumpy joints.
- The whole character is inked on a transparent layer; a real sticker halo is built by
  dilating that layer's alpha (MaxFilter) instead of fat white pre-strokes.
- Marker streak texture inside the black ink, soft radial vignette, ground shadow,
  fine paper grain. Rendered at 6144 and downscaled to 2048 (HD).
"""
import math, random
from PIL import Image, ImageDraw, ImageFilter, ImageChops

random.seed(7)

FINAL = 2048
SS = 3
W = H = FINAL * SS
S = W / 1024.0                     # logical 1024-space -> pixels

YELLOW = (255, 205, 51)
YELLOW_D = (244, 189, 34)
CREAM = (255, 250, 240)
INK = (26, 24, 22)
INK_SOFT = (68, 64, 60)
RED = (235, 73, 60)
TEAL = (16, 158, 152)

# ---------------------------------------------------------------- path helpers

def catmull(points, samples_per_seg=22):
    """Catmull-Rom spline through the control points."""
    if len(points) < 3:
        return list(points)
    pts = [points[0]] + list(points) + [points[-1]]
    out = []
    for i in range(len(pts) - 3):
        p0, p1, p2, p3 = pts[i], pts[i + 1], pts[i + 2], pts[i + 3]
        for j in range(samples_per_seg):
            t = j / samples_per_seg
            t2, t3 = t * t, t * t * t
            out.append((
                0.5 * ((2 * p1[0]) + (-p0[0] + p2[0]) * t +
                       (2 * p0[0] - 5 * p1[0] + 4 * p2[0] - p3[0]) * t2 +
                       (-p0[0] + 3 * p1[0] - 3 * p2[0] + p3[0]) * t3),
                0.5 * ((2 * p1[1]) + (-p0[1] + p2[1]) * t +
                       (2 * p0[1] - 5 * p1[1] + 4 * p2[1] - p3[1]) * t2 +
                       (-p0[1] + 3 * p1[1] - 3 * p2[1] + p3[1]) * t3)))
    out.append(points[-1])
    return out


def resample(pts, step):
    """Even arc-length resampling (so brush stamps overlap uniformly)."""
    out = [pts[0]]
    acc = 0.0
    for i in range(1, len(pts)):
        x0, y0 = out[-1] if acc == 0 else pts[i - 1]
        x0, y0 = pts[i - 1]
        x1, y1 = pts[i]
        seg = math.hypot(x1 - x0, y1 - y0)
        while acc + seg >= step:
            t = (step - acc) / seg
            nx, ny = x0 + (x1 - x0) * t, y0 + (y1 - y0) * t
            out.append((nx, ny))
            x0, y0 = nx, ny
            seg = math.hypot(x1 - x0, y1 - y0)
            acc = 0.0
        acc += seg
    return out


def wobble(pts, amp, freq, phase=0.0):
    """Smooth perpendicular sine wobble along the path (hand-drawn, not jittery)."""
    out = []
    n = max(2, len(pts))
    for i, (x, y) in enumerate(pts):
        j = min(i, n - 2)
        dx = pts[j + 1][0] - pts[j][0]
        dy = pts[j + 1][1] - pts[j][1]
        L = math.hypot(dx, dy) or 1.0
        nxv, nyv = -dy / L, dx / L
        off = amp * math.sin(i / n * freq * 2 * math.pi + phase) \
            + amp * 0.4 * math.sin(i / n * freq * 5.3 * math.pi + phase * 1.7)
        out.append((x + nxv * off, y + nyv * off))
    return out


def stamp(draw, pts, w0, w1, color, step_frac=0.16):
    """Stamp overlapping circles along the path; width lerps w0 -> w1."""
    wmax = max(w0, w1)
    pts = resample(pts, max(1.5, wmax * step_frac))
    n = max(2, len(pts))
    for i, (x, y) in enumerate(pts):
        t = i / (n - 1)
        r = (w0 + (w1 - w0) * t) / 2.0
        draw.ellipse([x - r, y - r, x + r, y + r], fill=color)


def stroke(draw, ctrl, width, color=INK, taper=(1.0, 1.0), amp=2.2, freq=3.0, phase=None):
    ph = random.uniform(0, 6.28) if phase is None else phase
    pts = wobble(catmull([(x * S, y * S) for x, y in ctrl]), amp * S, freq, ph)
    stamp(draw, pts, width * S * taper[0], width * S * taper[1], color)
    return pts


def blob(draw, cx, cy, rx, ry, color, wob=0.035, n=90, rot=0.0):
    pts = []
    for i in range(n):
        a = rot + 2 * math.pi * i / n
        rr = 1 + wob * math.sin(3 * a + rot * 5) + wob * 0.6 * math.sin(7 * a + 1.3)
        pts.append(((cx + math.cos(a) * rx * rr) * S, (cy + math.sin(a) * ry * rr) * S))
    draw.polygon(pts, fill=color)


def arcpts(cx, cy, rx, ry, a0, a1, n=60):
    return [(cx + math.cos(math.radians(a0 + (a1 - a0) * i / n)) * rx,
             cy + math.sin(math.radians(a0 + (a1 - a0) * i / n)) * ry) for i in range(n + 1)]

# ================================================================ background
bg = Image.new("RGB", (W, H), YELLOW)
bd = ImageDraw.Draw(bg)
# soft radial vignette (slightly darker corners)
vig = Image.new("L", (W, H), 0)
vd = ImageDraw.Draw(vig)
for i in range(28):
    r = int(W * (0.52 + i * 0.02))
    vd.ellipse([W // 2 - r, H // 2 - r, W // 2 + r, H // 2 + r], fill=int(4 + i * 2.4))
bg = Image.composite(Image.new("RGB", (W, H), YELLOW_D), bg, vig)
bd = ImageDraw.Draw(bg)
# faint ring of tiny question marks + sparkles around the edge
faint = (235, 182, 40)
for k in range(10):
    a = k / 10 * 2 * math.pi + 0.33
    x, y = 512 + math.cos(a) * 452, 512 + math.sin(a) * 452
    s = random.uniform(15, 22)
    if k % 2 == 0:
        rot = random.uniform(-0.4, 0.4)
        pts = arcpts(x, y, s, s, -210, 40, 22) + [(x + s * 0.4, y + s * 1.05)]
        pts = [((px - x) * math.cos(rot) - (py - y) * math.sin(rot) + x,
                (px - x) * math.sin(rot) + (py - y) * math.cos(rot) + y) for px, py in pts]
        stamp(bd, [(px * S, py * S) for px, py in pts], 6.5 * S, 6.5 * S, faint)
        bd.ellipse([(x + s * 0.15) * S, (y + s * 1.5) * S, (x + s * 0.65) * S, (y + s * 2.0) * S],
                   fill=faint)
    else:
        for ang in (0, 90, 45, 135):
            r = math.radians(ang)
            stamp(bd, [((x - math.cos(r) * s) * S, (y - math.sin(r) * s) * S),
                       ((x + math.cos(r) * s) * S, (y + math.sin(r) * s) * S)], 5.5 * S, 5.5 * S, faint)
# big soft cream disc behind the mascot (pops in the circular crop)
disc = Image.new("L", (W, H), 0)
dd = ImageDraw.Draw(disc)
dd.ellipse([int(92 * S), int(76 * S), int(932 * S), int(916 * S)], fill=110)
disc = disc.filter(ImageFilter.GaussianBlur(30 * S / 4))
bg = Image.composite(Image.new("RGB", (W, H), (255, 224, 120)), bg, disc)
bd = ImageDraw.Draw(bg)
# ground shadow under the feet
sh = Image.new("L", (W, H), 0)
sd = ImageDraw.Draw(sh)
sd.ellipse([int(340 * S), int(880 * S), int(700 * S), int(936 * S)], fill=70)
sh = sh.filter(ImageFilter.GaussianBlur(9 * S))
bg = Image.composite(Image.new("RGB", (W, H), (196, 148, 22)), bg, sh)

# ================================================================ character (ink layer)
art = Image.new("RGBA", (W, H), (0, 0, 0, 0))
ad = ImageDraw.Draw(art)

QX, QY, R = 512, 352, 168          # hook center + radius
WQ = 74                            # question-mark stroke width

# hook (150deg over the top to 395deg), slightly tapered at the start
hook_ctrl = arcpts(QX, QY, R, R * 1.03, 150, 395, 26)
stroke(ad, hook_ctrl, WQ, taper=(0.82, 1.0), amp=1.6, freq=2.2, phase=1.1)
# stem flowing out of the hook down to the body
stem_ctrl = [hook_ctrl[-1], (QX + R * 0.50, QY + R * 0.86),
             (QX + R * 0.16, QY + R * 1.34), (QX + 0.0, QY + R * 1.78)]
stroke(ad, stem_ctrl, WQ, taper=(1.0, 0.86), amp=1.4, freq=1.8, phase=2.4)
# dot = round body
DX, DY, DR = 512, 720, 62
blob(ad, DX, DY, DR * 1.18, DR, INK, wob=0.03)

# marker streak inside the ink (ONE subtle lighter scratch along the hook)
ctrl = arcpts(QX, QY, R - WQ * 0.16, (R - WQ * 0.16) * 1.03, 170, 380, 24)
stroke(ad, ctrl, 2.6, color=INK_SOFT + (60,), amp=1.0, freq=3.0, phase=1.7)

# ---- face inside the hook opening
FX, FY = 540, 342
for exi, ex in enumerate((FX - 62, FX + 62)):
    blob(ad, ex, FY, 34, 38, CREAM, wob=0.05)
    stroke(ad, arcpts(ex, FY, 34, 38, 0, 360, 24), 6.5, amp=0.8, freq=2, phase=exi)  # eye outline
    blob(ad, ex + 9, FY - 6, 14.5, 16, INK, wob=0.06)                                # pupil (up-right)
    blob(ad, ex + 15, FY - 13, 4.6, 4.6, CREAM, wob=0.0)                             # highlight
# brows - one raised ("hm?")
stroke(ad, [(FX - 92, FY - 56), (FX - 64, FY - 66), (FX - 36, FY - 68)], 11, amp=0.8, freq=1.5)
stroke(ad, [(FX + 34, FY - 84), (FX + 66, FY - 88), (FX + 94, FY - 78)], 11, amp=0.8, freq=1.5)
# open smile with tongue
mouth = arcpts(FX + 2, FY + 52, 34, 26, 10, 170, 24)
blob(ad, FX + 2, FY + 62, 30, 16, INK, wob=0.04)
blob(ad, FX + 2, FY + 70, 16, 8, (240, 120, 110), wob=0.05)
stroke(ad, mouth, 8, amp=0.7, freq=1.6)
# blush
for bx in (FX - 96, FX + 100):
    blob(ad, bx, FY + 34, 15, 9, (245, 150, 120, 160), wob=0.08)

# ---- arms
# left arm raised, scratching the hook ("thinking")
arm1 = [(QX - R * 0.92, QY + R * 0.28), (QX - R * 1.38, QY - R * 0.10), (QX - R * 1.30, QY - R * 0.72)]
p1 = stroke(ad, arm1, 24, taper=(1.0, 0.9), amp=1.3, freq=1.6)
hx, hy = p1[-1][0] / S, p1[-1][1] / S
for a in (-55, -20, 15, 50):
    r = math.radians(a)
    stroke(ad, [(hx, hy), (hx + math.sin(r) * 30, hy - math.cos(r) * 30)], 13, taper=(1.0, 0.75),
           amp=0.6, freq=1)
# right arm waving
arm2 = [(QX + R * 0.88, QY + R * 0.82), (QX + R * 1.46, QY + R * 1.02), (QX + R * 1.66, QY + R * 0.56)]
p2 = stroke(ad, arm2, 24, taper=(1.0, 0.9), amp=1.3, freq=1.6)
hx, hy = p2[-1][0] / S, p2[-1][1] / S
for a in (-65, -25, 15, 55):
    r = math.radians(a)
    stroke(ad, [(hx, hy), (hx + math.sin(r) * 32, hy - math.cos(r) * 32)], 13, taper=(1.0, 0.75),
           amp=0.6, freq=1)

# ---- legs + red sneakers (fully inside the frame this time)
for sxn in (-1, 1):
    lx = DX + sxn * 30
    leg = stroke(ad, [(lx, DY + DR * 0.7), (lx + sxn * 8, DY + DR * 0.7 + 68)], 20,
                 taper=(1.0, 0.92), amp=0.7, freq=1)
    fx, fy = leg[-1][0] / S, leg[-1][1] / S
    blob(ad, fx + sxn * 24, fy + 12, 52, 26, RED, wob=0.05)                 # shoe
    blob(ad, fx + sxn * 24, fy + 26, 56, 11, CREAM, wob=0.06)               # sole
    stroke(ad, arcpts(fx + sxn * 24, fy + 12, 52, 26, 180, 360, 20), 7, amp=0.8, freq=2)  # outline
    stroke(ad, [(fx + sxn * 2, fy + 2), (fx + sxn * 30, fy - 2)], 5.5, color=CREAM,
           amp=0.4, freq=1)                                                 # lace
    blob(ad, fx + sxn * 44, fy + 8, 7, 7, CREAM, wob=0.0)                   # toe dot

# ---- teal idea-spark near the raised hand
sx_, sy_ = QX - R * 1.52, QY - R * 1.02
for ang in (0, 90, 45, 135):
    r = math.radians(ang)
    stroke(ad, [(sx_ - math.cos(r) * 26, sy_ - math.sin(r) * 26),
                (sx_ + math.cos(r) * 26, sy_ + math.sin(r) * 26)], 10, color=TEAL,
           amp=0.5, freq=1)
blob(ad, sx_, sy_, 6.5, 6.5, YELLOW, wob=0.0)

# ================================================================ sticker halo + composite
alpha = art.split()[3]
halo = alpha.filter(ImageFilter.MaxFilter(int(2 * round(13 * S / 2) + 1)))
halo = halo.filter(ImageFilter.GaussianBlur(1.2 * S)).point(lambda v: 255 if v > 40 else 0)
halo_img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
halo_img.paste(Image.new("RGB", (W, H), CREAM), (0, 0), halo)
canvas = bg.convert("RGBA")
canvas.alpha_composite(halo_img)
canvas.alpha_composite(art)
canvas = canvas.convert("RGB")

# fine paper grain: MULTIPLY by near-white noise (previous subtract/scale murdered the exposure)
noise = Image.effect_noise((W // 3, H // 3), 9).resize((W, H)).convert("L")
grain = noise.point(lambda v: 255 - max(0, min(10, v - 122)))   # 245..255 only
canvas = ImageChops.multiply(canvas, Image.merge("RGB", (grain, grain, grain)))

final = canvas.filter(ImageFilter.GaussianBlur(SS * 0.28)).resize((FINAL, FINAL), Image.LANCZOS)
OUT = r"C:\Users\USCSt\AppData\Local\Temp\claude\D--data-AutoShortsClaude\47c6a5fe-85c4-4b1d-9e6d-5e7eb0afa196\scratchpad\pfp_code_hd.png"
final.save(OUT)
print("saved", OUT, final.size)
