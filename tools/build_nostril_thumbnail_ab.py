from pathlib import Path
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps


ROOT = Path(r"D:\data\AutoShortsClaude\generated_assets\nostril_thumbnail_review")
OUT = Path(r"C:\Users\USCSt\Desktop\Nostrils_Thumbnail_AB_Test_V2")
OUT.mkdir(parents=True, exist_ok=True)
W, H = 1280, 720
FONT = r"C:\Windows\Fonts\arialbd.ttf"
FONT_HEAVY = r"C:\Windows\Fonts\impact.ttf"


def font(size, heavy=True):
    return ImageFont.truetype(FONT_HEAVY if heavy else FONT, size)


def base(path, crop=None):
    image = Image.open(path).convert("RGB")
    if crop:
        image = image.crop(crop)
    return ImageOps.fit(image, (W, H), Image.Resampling.LANCZOS)


def text_block(draw, xy, lines, colors, sizes, spacing=2, stroke=8):
    x, y = xy
    for line, color, size in zip(lines, colors, sizes):
        f = font(size)
        draw.text((x + 7, y + 9), line, font=f, fill=(0, 0, 0, 165), stroke_width=stroke,
                  stroke_fill=(0, 0, 0, 165))
        draw.text((x, y), line, font=f, fill=color, stroke_width=stroke,
                  stroke_fill=(12, 14, 18))
        y += draw.textbbox((x, y), line, font=f, stroke_width=stroke)[3] - y + spacing


def arrow(draw, start, end, color=(246, 61, 54), width=24):
    draw.line((start, end), fill=(20, 20, 20), width=width + 12)
    draw.line((start, end), fill=color, width=width)
    import math
    ang = math.atan2(end[1] - start[1], end[0] - start[0])
    spread, length = 0.62, 68
    p1 = (end[0] - length * math.cos(ang - spread), end[1] - length * math.sin(ang - spread))
    p2 = (end[0] - length * math.cos(ang + spread), end[1] - length * math.sin(ang + spread))
    draw.polygon((end, p1, p2), fill=(20, 20, 20))
    length = 57
    p1 = (end[0] - length * math.cos(ang - spread), end[1] - length * math.sin(ang - spread))
    p2 = (end[0] - length * math.cos(ang + spread), end[1] - length * math.sin(ang + spread))
    draw.polygon((end, p1, p2), fill=color)


def finish(image, name):
    # Crisp, saturated thumbnail treatment without changing the underlying video drawing.
    image = ImageEnhance.Color(image).enhance(1.12)
    image = ImageEnhance.Contrast(image).enhance(1.08)
    image = ImageEnhance.Sharpness(image).enhance(1.35)
    image.save(OUT / name, quality=96, subsampling=0)


# A — mechanism/curiosity: preserve the unmistakable sleeping-nostril frame.
im = base(ROOT / "exact_385.jpg")
shade = Image.new("RGBA", (W, H), (0, 0, 0, 0))
sd = ImageDraw.Draw(shade)
for x in range(650):
    alpha = int(205 * (1 - x / 720))
    sd.line((x, 0, x, H), fill=(8, 16, 33, max(0, alpha)))
im = Image.alpha_composite(im.convert("RGBA"), shade)
d = ImageDraw.Draw(im)
text_block(d, (52, 76), ["ONE SIDE", "IS ASLEEP"], [(255, 255, 255), (255, 220, 48)], [84, 92])
arrow(d, (520, 565), (865, 474))
d.rounded_rectangle((50, 605, 475, 677), radius=24, fill=(255, 255, 255, 235), outline=(12, 14, 18), width=6)
d.text((76, 618), "RIGHT NOW.", font=font(42), fill=(15, 19, 29))
finish(im.convert("RGB"), "A_ONE_SIDE_IS_ASLEEP.jpg")


# B — time/switch concept: a different composition centered on the hourglass.
im = base(ROOT / "exact_165.jpg")
overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
od = ImageDraw.Draw(overlay)
od.rounded_rectangle((28, 28, 1252, 185), radius=34, fill=(13, 15, 20, 232))
im = Image.alpha_composite(im.convert("RGBA"), overlay)
d = ImageDraw.Draw(im)
d.text((64, 51), "YOUR NOSE SWITCHES SIDES", font=font(66), fill=(255, 255, 255),
       stroke_width=3, stroke_fill=(0, 0, 0))
d.rounded_rectangle((430, 585, 850, 692), radius=28, fill=(250, 194, 43), outline=(18, 18, 18), width=7)
d.text((488, 606), "EVERY FEW HOURS", font=font(40), fill=(15, 15, 18))
arrow(d, (278, 545), (485, 430), color=(241, 55, 48), width=20)
arrow(d, (1000, 545), (805, 430), color=(241, 55, 48), width=20)
finish(im.convert("RGB"), "B_YOUR_NOSE_SWITCHES.jpg")


# C — human metaphor/pattern interrupt: the workers make the mechanism instantly scannable.
# Remove the frame's original heading and enlarge the workers; our headline owns the hierarchy.
im = base(ROOT / "exact_110.jpg", crop=(0, 330, 1920, 1080))
panel = Image.new("RGBA", (W, H), (0, 0, 0, 0))
pd = ImageDraw.Draw(panel)
pd.rounded_rectangle((32, 34, 515, 686), radius=38, fill=(10, 18, 31, 236),
                     outline=(255, 214, 52, 255), width=7)
im = Image.alpha_composite(im.convert("RGBA"), panel)
d = ImageDraw.Draw(im)
text_block(d, (70, 100), ["YOUR NOSE", "WORKS IN", "SHIFTS"],
           [(255, 255, 255), (255, 255, 255), (255, 214, 52)], [66, 66, 92], spacing=0, stroke=5)
d.rounded_rectangle((75, 535, 456, 635), radius=25, fill=(239, 65, 57), outline=(10, 12, 16), width=6)
d.text((112, 557), "WITHOUT YOU KNOWING", font=font(30), fill=(255, 255, 255))
arrow(d, (505, 570), (800, 505), color=(239, 65, 57), width=22)
finish(im.convert("RGB"), "C_NOSE_SHIFT_WORK.jpg")

(OUT / "titles.txt").write_text(
    "A — Why You Never Breathe Through Both Nostrils at Once\n"
    "B — Your Nose Secretly Switches Sides Every Few Hours\n"
    "C — One of Your Nostrils Is Always Resting — Here’s Why\n",
    encoding="utf-8",
)
print(OUT)
