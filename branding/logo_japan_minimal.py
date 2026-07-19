"""Japan-Facts Shorts avatar, minimal flat edition (reference: red/black/white, big red disc,
torii silhouette - differentiated by our curved kasagi + a seigaiha wave strip clipped inside
the disc). 2048x2048, circular-crop safe."""
import math
import os
from PIL import Image, ImageDraw, ImageFilter

SS = 4
SIZE = 2048
W = H = 1024 * SS

PAPER = (247, 243, 235)         # warm white
RED = (222, 49, 42)
RED_DEEP = (190, 38, 34)        # wave strip tone inside the disc
INK = (18, 17, 19)

CX, CY = W / 2, H / 2
R = W / 2

img = Image.new("RGB", (W, H), PAPER)
d = ImageDraw.Draw(img)


def ellipse(dr, cx, cy, rx, ry, color):
    dr.ellipse([cx - rx, cy - ry, cx + rx, cy + ry], fill=color)


# ---------------------------------------------------------------- the red sun disc
SUN_R = R * 0.72
ellipse(d, CX, CY, SUN_R, SUN_R, RED)

# ---------------------------------------------------------------- seigaiha strip INSIDE the disc
# drawn on a separate layer, then masked to the sun circle so the scallops never leave the disc
wave = Image.new("RGBA", (W, H), (0, 0, 0, 0))
dw = ImageDraw.Draw(wave)
wave_top = CY + SUN_R * 0.42
row_step = SUN_R * 0.115
arc_r = row_step * 2.1
row = 0
y = wave_top
while y < CY + SUN_R + arc_r:
    off = arc_r if row % 2 else 0
    x = CX - SUN_R - arc_r + off
    while x < CX + SUN_R + arc_r:
        for k, col in ((1.0, RED_DEEP), (0.80, RED), (0.58, RED_DEEP), (0.36, RED)):
            dw.ellipse([x - arc_r * k, y - arc_r * k, x + arc_r * k, y + arc_r * k], fill=col)
        x += arc_r * 2
    row += 1
    y += row_step
sun_mask = Image.new("L", (W, H), 0)
ellipse(ImageDraw.Draw(sun_mask), CX, CY, SUN_R, SUN_R, 255)
img.paste(wave, (0, 0), Image.composite(wave.split()[3], Image.new("L", (W, H), 0), sun_mask))
d = ImageDraw.Draw(img)

# ---------------------------------------------------------------- torii silhouette (curved kasagi)
tw = SUN_R * 1.24                # kasagi width (tips reach past the disc edge a touch)
top_y = CY - SUN_R * 0.62
k_h = SUN_R * 0.145
base_y = CY + SUN_R * 0.68      # legs end inside the wave strip
leg_w = SUN_R * 0.125
lean = leg_w * 0.5

def kasagi():
    n = 48
    pts_top, pts_bot = [], []
    for i in range(n + 1):
        t = i / n
        x = CX - tw / 2 - k_h * 0.5 + (tw + k_h) * t
        curve = math.sin(t * math.pi)
        sweep = (1 - curve) * k_h * 1.15
        pts_top.append((x, top_y - sweep))
        pts_bot.append((x, top_y + k_h - sweep * 0.55))
    d.polygon(pts_top + pts_bot[::-1], fill=INK)

kasagi()
d.rectangle([CX - tw * 0.44, top_y + k_h * 0.9,
             CX + tw * 0.44, top_y + k_h * 1.6], fill=INK)          # shimaki
n_y = top_y + k_h * 1.6 + SUN_R * 0.30
d.rectangle([CX - tw * 0.40, n_y, CX + tw * 0.40, n_y + k_h * 0.78], fill=INK)   # nuki
d.rectangle([CX - leg_w * 0.40, top_y + k_h * 1.6, CX + leg_w * 0.40, n_y], fill=INK)
for s in (-1, 1):
    x_top = CX + s * tw * 0.375
    x_bot = x_top - s * lean
    d.polygon([(x_top - leg_w / 2, top_y + k_h * 0.8),
               (x_top + leg_w / 2, top_y + k_h * 0.8),
               (x_bot + leg_w / 2, base_y),
               (x_bot - leg_w / 2, base_y)], fill=INK)

# ---------------------------------------------------------------- circular crop
mask = Image.new("L", (W, H), 0)
ImageDraw.Draw(mask).ellipse([0, 0, W, H], fill=255)
img = Image.composite(img, Image.new("RGB", (W, H), PAPER), mask)

img = img.filter(ImageFilter.GaussianBlur(SS * 0.28))
final = img.resize((SIZE, SIZE), Image.LANCZOS)
OUT = r"D:\data\AutoShortsClaude\branding\logo_japan_minimal.png"
final.save(OUT, optimize=True)
print("saved", OUT, round(os.path.getsize(OUT) / 1e6, 2), "MB")
