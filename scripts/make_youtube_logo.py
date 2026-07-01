"""Pixel-art YouTube channel logo for a TRAVEL / CULTURE / SOCIETY channel.

A chunky pixel globe (world = travel, cultures, society) with meridian/equator lines and a red
travel location-pin, in the same warm-parchment + ink + blue + red palette as the app. Drawn on a
small grid and scaled with NEAREST so pixels stay crisp. YouTube crops avatars to a CIRCLE, so the
globe is centred and the important parts stay away from the corners.

Run: python scripts/make_youtube_logo.py  ->  writes youtube_logo.png (+ a circle-cropped preview)
"""

from pathlib import Path
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent

PARCH = (239, 231, 214)     # background
INK = (23, 21, 15)          # outlines
OCEAN = (47, 111, 214)      # blue seas (--accent)
OCEAN_D = (36, 86, 168)     # darker ocean for meridian shading
LAND = (86, 156, 92)        # green continents
LAND_HI = (120, 186, 120)   # lighter land top
RED = (232, 71, 43)         # travel pin (--accent-2)
TRANSP = (0, 0, 0, 0)

G = 32


def build():
    grid = [[PARCH + (255,) for _ in range(G)] for _ in range(G)]

    def put(x, y, c):
        if 0 <= x < G and 0 <= y < G:
            grid[y][x] = c if len(c) == 4 else c + (255,)

    # globe centre + radius
    cx, cy, r = 15.5, 16.5, 12.0
    inside = lambda x, y: (x - cx) ** 2 + (y - cy) ** 2 <= r ** 2
    ring = lambda x, y: (r - 1.1) ** 2 <= (x - cx) ** 2 + (y - cy) ** 2 <= (r + 0.4) ** 2

    # 1) ocean fill + ink ring
    for y in range(G):
        for x in range(G):
            if inside(x, y):
                put(x, y, OCEAN)
            if ring(x, y):
                put(x, y, INK)

    # 2) continents (hand-placed green blobs that read as land masses)
    land = {
        # left mass (americas-ish)
        (9, 9), (10, 9), (9, 10), (10, 10), (11, 10), (9, 11), (10, 11), (8, 12), (9, 12), (10, 12),
        (9, 13), (10, 13), (10, 14), (11, 14), (10, 15), (11, 15), (11, 16), (12, 16),
        # centre/right mass (afro-eurasia-ish)
        (16, 8), (17, 8), (18, 9), (16, 9), (17, 9), (19, 9), (16, 10), (17, 10), (18, 10), (19, 10),
        (20, 10), (17, 11), (18, 11), (19, 11), (20, 11), (21, 11), (18, 12), (19, 12), (20, 12),
        (17, 13), (18, 13), (19, 13), (18, 14), (19, 14), (20, 14),
        # lower-right island (oceania-ish)
        (21, 18), (22, 18), (21, 19), (14, 20), (15, 20), (16, 21),
    }
    for (x, y) in land:
        if inside(x, y):
            put(x, y, LAND)
    # a lighter top edge on the land for a touch of depth
    for (x, y) in land:
        if inside(x, y) and (x, y - 1) not in land and grid[y - 1][x][:3] == OCEAN:
            put(x, y - 1, LAND_HI) if inside(x, y - 1) else None

    # 3) meridian + equator lines (subtle darker-ocean curves) over remaining ocean only
    for y in range(G):
        for x in range(G):
            if inside(x, y) and grid[y][x][:3] == OCEAN:
                if abs(x - cx) < 0.9:                      # central meridian
                    put(x, y, OCEAN_D)
                if abs(y - cy) < 0.9:                      # equator
                    put(x, y, OCEAN_D)
                # a curved side meridian
                if abs((x - cx) - 5.5 * (1 - ((y - cy) / r) ** 2) ** 0.5) < 0.7:
                    put(x, y, OCEAN_D)
                if abs((x - cx) + 5.5 * (1 - ((y - cy) / r) ** 2) ** 0.5) < 0.7:
                    put(x, y, OCEAN_D)

    # 4) red travel location-pin (SOLID teardrop), top-right
    px, py = 24, 5                                         # head centre
    pin = set()
    for y in range(2, 8):                                  # round head
        for x in range(21, 28):
            if (x - px) ** 2 + ((y - py) * 1.05) ** 2 <= 6.3:
                pin.add((x, y))
    pin |= {(px, 8), (px, 9), (px, 10)}                    # neck -> point
    pin |= {(px - 1, 8), (px + 1, 8)}
    # ink outline around the SILHOUETTE only, then red fill, then a hole in the head
    for (x, y) in pin:
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (-1, -1), (1, -1), (-1, 1)):
            if (x + dx, y + dy) not in pin:
                put(x + dx, y + dy, INK)
    for (x, y) in pin:
        put(x, y, RED)
    for (x, y) in [(px, py - 1), (px - 1, py), (px, py), (px + 1, py), (px, py + 1)]:
        put(x, y, PARCH)                                   # white/parchment centre hole

    small = Image.new("RGBA", (G, G), TRANSP)
    small.putdata([tuple(grid[y][x]) for y in range(G) for x in range(G)])
    return small


def main():
    small = build()
    big = small.resize((1024, 1024), Image.NEAREST)
    out = ROOT / "youtube_logo.png"
    big.save(out)
    # circle-cropped preview (how YouTube shows the avatar)
    mask = Image.new("L", (1024, 1024), 0)
    ImageDraw.Draw(mask).ellipse([0, 0, 1023, 1023], fill=255)
    circ = Image.new("RGBA", (1024, 1024), TRANSP)
    circ.paste(big, (0, 0), mask)
    circ.save(ROOT / "youtube_logo_circle.png")
    print("Wrote:", out, "and youtube_logo_circle.png (circle preview)")


if __name__ == "__main__":
    main()
