"""CulturePin YouTube channel logo - PIXEL style.

The pin IS the brand (CulturePin): a chunky pixel location-pin whose round head is a little globe
(world = travel / cultures / society) with the Japan hinomaru, a pointed base, ink outline and the
site's hard offset shadow, on warm parchment. Drawn on a small grid and scaled with NEAREST so the
pixels stay crisp. YouTube crops avatars to a CIRCLE, so the pin is centred.

Run: python scripts/make_youtube_logo.py  ->  youtube_logo.png (+ circle preview)
"""

from pathlib import Path
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent

PARCH = (239, 231, 214)
INK = (23, 21, 15)
OCEAN = (47, 111, 214)
OCEAN_D = (36, 86, 168)
LAND = (86, 156, 92)
LAND_HI = (120, 186, 120)
RED = (214, 30, 40)          # hinomaru / pin body red
TRANSP = (0, 0, 0, 0)

G = 34


def build():
    grid = [[PARCH + (255,) for _ in range(G)] for _ in range(G)]

    def put(x, y, c):
        if 0 <= x < G and 0 <= y < G:
            grid[y][x] = c if len(c) == 4 else c + (255,)

    hx, hy, hr = 16.5, 13.0, 9.5           # globe/head centre + radius
    tipy = 30                              # pin tip

    def head(x, y):
        return (x - hx) ** 2 + (y - hy) ** 2 <= hr ** 2

    def body(x, y):                        # triangular point below the head
        if y <= hy:
            return False
        frac = (tipy - y) / (tipy - hy)
        halfw = hr * 0.92 * frac
        return abs(x - hx) <= halfw and y <= tipy

    sil = {(x, y) for y in range(G) for x in range(G) if head(x, y) or body(x, y)}

    # 1) HARD offset shadow (ink, down-right) - site pixel style
    for (x, y) in sil:
        put(x + 1, y + 1, INK)

    # 2) ink outline of the whole pin silhouette
    for (x, y) in sil:
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            if (x + dx, y + dy) not in sil:
                put(x + dx, y + dy, INK)

    # 3) fill: red pin body (point), blue-ocean globe head
    for (x, y) in sil:
        put(x, y, RED)
    for (x, y) in sil:
        if head(x, y):
            put(x, y, OCEAN)

    # 4) green continents on the globe head
    land = {(12, 8), (13, 8), (12, 9), (13, 9), (14, 9), (12, 10), (13, 10), (11, 11), (12, 11),
            (12, 12), (13, 12), (13, 13),
            (19, 8), (20, 8), (19, 9), (20, 9), (21, 9), (19, 10), (20, 10), (21, 10), (20, 11),
            (21, 11), (20, 12),
            (15, 16), (16, 16), (16, 17)}
    for (x, y) in land:
        if head(x, y):
            put(x, y, LAND)
    for (x, y) in land:                    # light top edge
        if head(x, y - 1) and (x, y - 1) not in land and grid[y - 1][x][:3] == OCEAN:
            put(x, y - 1, LAND_HI)

    # 5) meridian + equator hint (darker ocean) on remaining ocean
    for y in range(G):
        for x in range(G):
            if head(x, y) and grid[y][x][:3] == OCEAN:
                if abs(x - hx) < 0.9 or abs(y - hy) < 0.9:
                    put(x, y, OCEAN_D)

    # 6) small Japan hinomaru dot lower-right on the globe (the pin "marks" Japan)
    for (x, y) in [(20, 16), (21, 16), (20, 17), (21, 17)]:
        if head(x, y):
            put(x, y, RED)

    small = Image.new("RGBA", (G, G), TRANSP)
    small.putdata([tuple(grid[y][x]) for y in range(G) for x in range(G)])
    return small


def main():
    small = build()
    big = small.resize((1024, 1024), Image.NEAREST)
    big.save(ROOT / "youtube_logo.png")
    mask = Image.new("L", (1024, 1024), 0)
    ImageDraw.Draw(mask).ellipse([0, 0, 1023, 1023], fill=255)
    out = Image.new("RGBA", (1024, 1024), TRANSP)
    out.paste(big, (0, 0), mask)
    out.save(ROOT / "youtube_logo_circle.png")
    print("Wrote pixel CulturePin logo: youtube_logo.png + youtube_logo_circle.png")


if __name__ == "__main__":
    main()
