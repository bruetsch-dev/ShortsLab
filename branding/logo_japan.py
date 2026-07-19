"""Procedural logo for the Japan-Facts Shorts channel.

Bold flat avatar built for YouTube's circular crop: indigo night sky, big red sun with a soft
glow, black torii gate standing in a seigaiha wave sea. Supersampled, exported 2048x2048.
"""
import math
import os
from PIL import Image, ImageDraw, ImageFilter

SS = 4
SIZE = 2048
W = H = 1024 * SS

INDIGO = (27, 35, 58)           # night sky
SUN = (216, 56, 48)             # hinomaru red
SUN_DEEP = (176, 44, 42)        # inner shade for a subtle 2-tone sun
INK = (17, 17, 21)              # torii silhouette
CREAM = (243, 236, 222)         # foam / accents
WAVE_BLUE = (48, 68, 108)

CX, CY = W / 2, H / 2
R = W / 2

img = Image.new("RGB", (W, H), INDIGO)
d = ImageDraw.Draw(img)


def ellipse(dr, cx, cy, rx, ry, color):
    dr.ellipse([cx - rx, cy - ry, cx + rx, cy + ry], fill=color)


# ---------------------------------------------------------------- soft sun glow (blurred layer)
SUN_CY = CY * 0.80
SUN_R = R * 0.50
glow = Image.new("RGB", (W, H), INDIGO)
dg = ImageDraw.Draw(glow)
ellipse(dg, CX, SUN_CY, SUN_R * 1.45, SUN_R * 1.45, (120, 48, 54))
glow = glow.filter(ImageFilter.GaussianBlur(R * 0.10))
img = Image.blend(img, glow, 0.85)
d = ImageDraw.Draw(img)

# ---------------------------------------------------------------- the sun (2-tone)
ellipse(d, CX, SUN_CY, SUN_R, SUN_R, SUN_DEEP)
ellipse(d, CX, SUN_CY - SUN_R * 0.06, SUN_R * 0.94, SUN_R * 0.94, SUN)

# ---------------------------------------------------------------- 4-point star sparkles
def star(cx, cy, r):
    d.polygon([(cx, cy - r), (cx + r * 0.28, cy - r * 0.28), (cx + r, cy),
               (cx + r * 0.28, cy + r * 0.28), (cx, cy + r), (cx - r * 0.28, cy + r * 0.28),
               (cx - r, cy), (cx - r * 0.28, cy - r * 0.28)], fill=CREAM)

star(CX - R * 0.55, CY * 0.42, R * 0.055)
star(CX + R * 0.58, CY * 0.50, R * 0.04)
star(CX + R * 0.40, CY * 0.26, R * 0.028)

# ---------------------------------------------------------------- torii silhouette
tw = SUN_R * 1.62                # kasagi (top beam) width
top_y = CY * 0.52
k_h = SUN_R * 0.15               # kasagi thickness
base_y = CY * 1.60
leg_w = SUN_R * 0.14
lean = leg_w * 0.5

# kasagi: gently curved top with upswept tips (polygon along two arcs)
def kasagi():
    n = 40
    pts_top, pts_bot = [], []
    for i in range(n + 1):
        t = i / n                       # 0..1 across the beam
        x = CX - tw / 2 - k_h * 0.5 + (tw + k_h) * t
        curve = math.sin(t * math.pi)   # 0 at tips, 1 mid
        sweep = (1 - curve) * k_h * 1.1 # tips ride UP
        pts_top.append((x, top_y - sweep))
        pts_bot.append((x, top_y + k_h - sweep * 0.55))
    d.polygon(pts_top + pts_bot[::-1], fill=INK)

kasagi()
# shimaki: slimmer beam directly under the kasagi
d.rectangle([CX - tw * 0.44, top_y + k_h * 0.9,
             CX + tw * 0.44, top_y + k_h * 1.55], fill=INK)
# nuki: lower cross beam with a real gap
n_y = top_y + k_h * 1.55 + SUN_R * 0.30
d.rectangle([CX - tw * 0.40, n_y, CX + tw * 0.40, n_y + k_h * 0.75], fill=INK)
# gakuzuka: center strut between shimaki and nuki
d.rectangle([CX - leg_w * 0.40, top_y + k_h * 1.55, CX + leg_w * 0.40, n_y], fill=INK)
# legs, leaning slightly inward
for s in (-1, 1):
    x_top = CX + s * tw * 0.38
    x_bot = x_top - s * lean
    d.polygon([(x_top - leg_w / 2, top_y + k_h * 0.8),
               (x_top + leg_w / 2, top_y + k_h * 0.8),
               (x_bot + leg_w / 2, base_y),
               (x_bot - leg_w / 2, base_y)], fill=INK)

# ---------------------------------------------------------------- seigaiha sea
# classic construction: rows of concentric-ring circles, each LOWER row painted after the one
# above so only the upper scallop of every circle stays visible; rows continue past the bottom
# edge so no full circle is ever left exposed.
wave_top = CY * 1.52
row_step = SUN_R * 0.18
arc_r = row_step * 2.1
row = 0
y = wave_top
while y < H + arc_r:
    off = arc_r if row % 2 else 0
    x = -arc_r + off
    while x < W + arc_r:
        for k, col in ((1.0, CREAM), (0.80, WAVE_BLUE), (0.58, CREAM),
                       (0.36, WAVE_BLUE), (0.16, CREAM)):
            ellipse(d, x, y, arc_r * k, arc_r * k, col)
        x += arc_r * 2
    row += 1
    y += row_step

# ---------------------------------------------------------------- circular crop + ring
mask = Image.new("L", (W, H), 0)
ImageDraw.Draw(mask).ellipse([0, 0, W, H], fill=255)
img = Image.composite(img, Image.new("RGB", (W, H), (12, 14, 22)), mask)
ring = ImageDraw.Draw(img)
rw = int(R * 0.022)
pad = int(R * 0.055)
ring.ellipse([pad, pad, W - pad, H - pad], outline=CREAM, width=rw)

# ---------------------------------------------------------------- export
img = img.filter(ImageFilter.GaussianBlur(SS * 0.28))
final = img.resize((SIZE, SIZE), Image.LANCZOS)
OUT = r"D:\data\AutoShortsClaude\branding\logo_japan.png"
final.save(OUT, optimize=True)
print("saved", OUT, round(os.path.getsize(OUT) / 1e6, 2), "MB")
