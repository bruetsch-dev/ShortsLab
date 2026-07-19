"""Fuji Facts - YouTube channel banner, 2560x1440.

Brand style from logo_japan_fuji: indigo night, red sun, Fuji with yuki-gata snow cap,
seigaiha sea. All critical content (wordmark + icon) inside YouTube's universal safe area
(central 1546x423); the rest of the canvas is decoration for TV/desktop crops."""
import os
from PIL import Image, ImageDraw, ImageFilter, ImageFont

SS = 2
BW, BH = 2560, 1440
W, H = BW * SS, BH * SS

INDIGO = (30, 38, 62)
INDIGO_LO = (23, 29, 49)
RED = (222, 52, 45)
INK = (20, 20, 26)
SNOW = (243, 236, 222)
WAVE_BLUE = (48, 68, 108)

CX, CY = W / 2, H / 2
SAFE_W, SAFE_H = 1546 * SS, 423 * SS
SAFE_X0, SAFE_Y0 = CX - SAFE_W / 2, CY - SAFE_H / 2

img = Image.new("RGB", (W, H), INDIGO)
d = ImageDraw.Draw(img)


def ellipse(dr, cx, cy, r, color):
    dr.ellipse([cx - r, cy - r, cx + r, cy + r], fill=color)


# ---------------------------------------------------------------- sky bands
d.rectangle([0, 0, W, H * 0.22], fill=INDIGO_LO)
d.rectangle([0, H * 0.22, W, H * 0.36],
            fill=tuple((a + b) // 2 for a, b in zip(INDIGO_LO, INDIGO)))

# ---------------------------------------------------------------- big background Fuji (decor)
peak_y = H * 0.34
base_y = H * 0.86
half = W * 0.30
top_half = W * 0.045
fuji_cx = W * 0.775


def fuji(dr, cx, peak, base, hw, thw, body, snow, cap_frac=0.30, teeth=4):
    def fw(y):
        return thw + (y - peak) * (hw - thw) / (base - peak)
    dr.polygon([(cx - thw, peak), (cx + thw, peak),
                (cx + hw, base), (cx - hw, base)], fill=body)
    cap_hi = peak + (base - peak) * cap_frac * 0.62
    cap_lo = peak + (base - peak) * cap_frac
    x_l, x_r = cx - fw(cap_hi), cx + fw(cap_hi)
    zig = []
    for i in range(2 * teeth + 1):
        x = x_l + (x_r - x_l) * i / (2 * teeth)
        zig.append((x, cap_hi if i % 2 == 0 else cap_lo))
    dr.polygon([(cx - thw, peak), (cx + thw, peak)] + zig[::-1], fill=snow)


# tone-on-tone silhouette only (no cap, no sun): depth layer that never fights the wordmark
DIM = (24, 31, 52)
d.polygon([(fuji_cx - top_half, peak_y), (fuji_cx + top_half, peak_y),
           (fuji_cx + half, base_y), (fuji_cx - half, base_y)], fill=DIM)
# red sun accent high in the sky, clear of the safe area
ellipse(d, W * 0.885, H * 0.155, W * 0.052, RED)

# ---------------------------------------------------------------- seigaiha sea (bottom decor)
wave_top = H * 0.80
row_step = H * 0.045
arc_r = row_step * 2.1
row = 0
y = wave_top
while y < H + arc_r:
    off = arc_r if row % 2 else 0
    x = -arc_r + off
    while x < W + arc_r:
        for k, col in ((1.0, SNOW), (0.80, WAVE_BLUE), (0.58, SNOW), (0.36, WAVE_BLUE)):
            ellipse(d, x, y + 0, arc_r * k, col) if False else d.ellipse(
                [x - arc_r * k, y - arc_r * k, x + arc_r * k, y + arc_r * k], fill=col)
        x += arc_r * 2
    row += 1
    y += row_step

# ---------------------------------------------------------------- safe-area content
def load_font(px):
    for name in ("ariblk.ttf", "arialbd.ttf", "seguisb.ttf", "arial.ttf"):
        try:
            return ImageFont.truetype(name, px)
        except OSError:
            continue
    return ImageFont.load_default()

# auto-fit: the whole lockup (icon + gap + wordmark) must sit INSIDE the safe area with margin
icon_w = SAFE_H * 0.84            # icon slot width (mini fuji base)
gap = SAFE_H * 0.22
margin = SAFE_W * 0.03
size = int(SAFE_H * 0.52)
while size > 10:
    font = load_font(size)
    text_w = d.textlength("FUJI FACTS", font=font)
    if icon_w + gap + text_w <= SAFE_W - 2 * margin:
        break
    size -= 4

lock_w = icon_w + gap + d.textlength("FUJI FACTS", font=font)
lock_x0 = SAFE_X0 + (SAFE_W - lock_w) / 2

# mini fuji + sun icon
icon_cx = lock_x0 + icon_w / 2
icon_base = CY + SAFE_H * 0.26
icon_peak = CY - SAFE_H * 0.22
fuji(d, icon_cx, icon_peak, icon_base, icon_w / 2, SAFE_H * 0.075, INK, SNOW)
ellipse(d, icon_cx + SAFE_H * 0.30, icon_peak - SAFE_H * 0.02, SAFE_H * 0.14, RED)

# wordmark: FUJI (snow) FACTS (red)
text_x = lock_x0 + icon_w + gap
base_line = CY - SAFE_H * 0.04
w1 = d.textlength("FUJI ", font=font)
d.text((text_x, base_line), "FUJI ", font=font, fill=SNOW, anchor="lm")
d.text((text_x + w1, base_line), "FACTS", font=font, fill=RED, anchor="lm")
# tagline under the wordmark (auto-fit inside the safe area as well)
TAG = "Things you never knew about Japan"
sub_size = int(SAFE_H * 0.13)
while sub_size > 8:
    sub_font = load_font(sub_size)
    if text_x + d.textlength(TAG, font=sub_font) <= SAFE_X0 + SAFE_W - margin:
        break
    sub_size -= 2
d.text((text_x + SAFE_W * 0.005, CY + SAFE_H * 0.28), TAG, font=sub_font, fill=SNOW, anchor="lm")

# ---------------------------------------------------------------- export
img = img.filter(ImageFilter.GaussianBlur(SS * 0.3))
final = img.resize((BW, BH), Image.LANCZOS)
OUT = r"D:\data\AutoShortsClaude\branding\banner_fuji.png"
final.save(OUT, optimize=True)
print("saved", OUT, round(os.path.getsize(OUT) / 1e6, 2), "MB")

# safe-area crop preview (what EVERY device shows)
sx0, sy0 = int((BW - 1546) / 2), int((BH - 423) / 2)
final.crop((sx0, sy0, sx0 + 1546, sy0 + 423)).save(
    r"D:\data\AutoShortsClaude\branding\banner_fuji_safearea.png")
print("safe-area preview saved")
