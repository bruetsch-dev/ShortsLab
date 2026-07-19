"""Japan-Facts Shorts avatar, Fuji edition - clearly distinct from the torii-on-red-disc
reference: indigo night, big red sun, Mount Fuji silhouette with a cream snow cap, seigaiha
sea. Flat and bold, circular-crop safe, 2048x2048."""
import os
from PIL import Image, ImageDraw, ImageFilter

SS = 4
SIZE = 2048
W = H = 1024 * SS

INDIGO = (30, 38, 62)
INDIGO_LO = (23, 29, 49)
RED = (222, 52, 45)
INK = (20, 20, 26)              # Fuji body
SNOW = (243, 236, 222)
WAVE_BLUE = (48, 68, 108)
CREAM = SNOW

CX, CY = W / 2, H / 2
R = W / 2

img = Image.new("RGB", (W, H), INDIGO)
d = ImageDraw.Draw(img)


def ellipse(dr, cx, cy, rx, ry, color):
    dr.ellipse([cx - rx, cy - ry, cx + rx, cy + ry], fill=color)


# subtle vertical sky shade: darker top band
d.rectangle([0, 0, W, CY * 0.5], fill=INDIGO_LO)
d.rectangle([0, CY * 0.5, W, CY * 0.75], fill=tuple((a + b) // 2 for a, b in zip(INDIGO_LO, INDIGO)))

# ---------------------------------------------------------------- red sun, high and slightly right
SUN_R = R * 0.34
SUN_CX, SUN_CY = CX + R * 0.22, CY * 0.52
ellipse(d, SUN_CX, SUN_CY, SUN_R, SUN_R, RED)

# ---------------------------------------------------------------- Mount Fuji
peak_y = CY * 0.66
base_y = CY * 1.50
half = R * 0.88                  # half base width
top_half = R * 0.115             # flat crater half width

def slope(x0, y0, x1, y1, n=60, bow=0.16):
    """Concave mountain flank: sag toward the valley (classic Fuji sweep)."""
    pts = []
    for i in range(n + 1):
        t = i / n
        x = x0 + (x1 - x0) * t
        y = y0 + (y1 - y0) * t + bow * (y1 - y0) * (t * (1 - t)) * 2
        pts.append((x, y))
    return pts

left = slope(CX - top_half, peak_y, CX - half, base_y)
right = slope(CX + top_half, peak_y, CX + half, base_y)
fuji = left[::-1] + right       # peak-left down to base-left, then peak-right to base-right
d.polygon([(CX - half, base_y)] + left[::-1] + right + [(CX + half, base_y)], fill=INK)

# snow cap: clean yuki-gata zigzag lower edge
def flank_halfwidth(y):
    return top_half + (y - peak_y) * (half - top_half) / (base_y - peak_y)

cap_hi = peak_y + (base_y - peak_y) * 0.185   # zigzag upper level
cap_lo = peak_y + (base_y - peak_y) * 0.30    # tooth tips (lower level)
x_l = CX - flank_halfwidth(cap_hi)
x_r = CX + flank_halfwidth(cap_hi)
teeth = 4
zig = []
for i in range(2 * teeth + 1):                 # x across, alternating hi/lo, ends on hi
    x = x_l + (x_r - x_l) * i / (2 * teeth)
    zig.append((x, cap_hi if i % 2 == 0 else cap_lo))
d.polygon([(CX - top_half, peak_y), (CX + top_half, peak_y)] + zig[::-1], fill=SNOW)

# ---------------------------------------------------------------- seigaiha sea
wave_top = CY * 1.44
row_step = R * 0.085
arc_r = row_step * 2.1
row = 0
y = wave_top
while y < H + arc_r:
    off = arc_r if row % 2 else 0
    x = -arc_r + off
    while x < W + arc_r:
        for k, col in ((1.0, CREAM), (0.80, WAVE_BLUE), (0.58, CREAM), (0.36, WAVE_BLUE)):
            ellipse(d, x, y, arc_r * k, arc_r * k, col)
        x += arc_r * 2
    row += 1
    y += row_step

# ---------------------------------------------------------------- circular crop
mask = Image.new("L", (W, H), 0)
ImageDraw.Draw(mask).ellipse([0, 0, W, H], fill=255)
img = Image.composite(img, Image.new("RGB", (W, H), (12, 14, 22)), mask)

img = img.filter(ImageFilter.GaussianBlur(SS * 0.28))
final = img.resize((SIZE, SIZE), Image.LANCZOS)
OUT = r"D:\data\AutoShortsClaude\branding\logo_japan_fuji.png"
final.save(OUT, optimize=True)
print("saved", OUT, round(os.path.getsize(OUT) / 1e6, 2), "MB")
