"""Generate the Shortslab app icon in the site's PIXEL / retro style.

Matches the website aesthetic: warm parchment background, dark "ink" outlines, the signature
HARD offset drop-shadow (no blur), blue + signal-red accents, chunky pixels. Drawn on a small
logical grid and scaled up with NEAREST-neighbour so the pixels stay crisp and blocky.

Run: python scripts/make_app_icon.py
"""

from pathlib import Path
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"

# palette (from the site's CSS :root)
PARCH = (239, 231, 214)     # --bg-base parchment
CARD = (251, 247, 238)      # --bg-input near-white field (screen)
INK = (23, 21, 15)          # --ink near-black
RED = (232, 71, 43)         # --accent-2 signal red
BLUE = (47, 111, 214)       # --accent blue
TRANSP = (0, 0, 0, 0)

G = 32                      # logical pixel grid (GxG)
CELL = 32                  # px per logical pixel -> 1024x1024


def build():
    grid = [[TRANSP for _ in range(G)] for _ in range(G)]

    def put(x, y, c):
        if 0 <= x < G and 0 <= y < G:
            grid[y][x] = c if len(c) == 4 else c + (255,)

    def rect(x0, y0, x1, y1, c):
        for y in range(y0, y1 + 1):
            for x in range(x0, x1 + 1):
                put(x, y, c)

    def frame(x0, y0, x1, y1, c, t=1):
        for i in range(t):
            for x in range(x0, x1 + 1):
                put(x, y0 + i, c); put(x, y1 - i, c)
            for y in range(y0, y1 + 1):
                put(x0 + i, y, c); put(x1 - i, y, c)

    def notch(x0, y0, x1, y1):
        for (cx, cy) in [(x0, y0), (x1, y0), (x0, y1), (x1, y1)]:
            put(cx, cy, TRANSP)

    # 1) HARD offset shadow of the whole badge (ink, down-right) - the site's signature look
    rect(4, 5, 30, 31, INK)
    notch(4, 5, 30, 31)

    # 2) parchment badge with a thick ink border. Notch only the TOP corners (the bottom ones sit
    # on the offset shadow, so notching them would punch a hole through it).
    rect(1, 1, 27, 28, PARCH)
    frame(1, 1, 27, 28, INK, t=2)
    put(1, 1, TRANSP)
    put(27, 1, TRANSP)

    # 3) the "Short" screen: a vertical 9:16 card inside, ink-outlined
    sx0, sy0, sx1, sy1 = 8, 4, 21, 25
    rect(sx0, sy0, sx1, sy1, CARD)
    frame(sx0, sy0, sx1, sy1, INK, t=1)

    # 4) big RED play triangle (ink-outlined) centred INSIDE the screen
    tri = []
    top, bot = 8, 21
    left_x, apex_x = 11, 18
    cy = (top + bot) // 2
    for y in range(top, bot + 1):
        frac = (y - top) / max(1, (cy - top)) if y <= cy else (bot - y) / max(1, (bot - cy))
        xr = int(round(left_x + (apex_x - left_x) * frac))
        for x in range(left_x, xr + 1):
            tri.append((x, y))
    for (x, y) in tri:                                         # ink outline
        for dx, dy in ((0, 0), (1, 0), (-1, 0), (0, 1), (0, -1)):
            put(x + dx, y + dy, INK)
    for (x, y) in tri:                                         # red fill on top
        put(x, y, RED)

    # 5) blue viral 'spark' (a plus/star) top-right on the parchment - the second brand accent
    for (x, y) in [(24, 6), (23, 7), (25, 7), (24, 7), (24, 8), (22, 9), (26, 9)]:
        put(x, y, BLUE)

    # rasterize the grid with crisp nearest-neighbour pixels
    small = Image.new("RGBA", (G, G), TRANSP)
    small.putdata([grid[y][x] for y in range(G) for x in range(G)])
    return small


def main():
    STATIC.mkdir(exist_ok=True)
    small = build()
    big = small.resize((1024, 1024), Image.NEAREST)
    big.save(STATIC / "app_icon.png")
    # crisp .ico sizes straight from the grid (nearest) so pixels stay sharp at every size
    for name in ("favicon.ico", "app_icon.ico", "start_icon.ico"):
        imgs = [small.resize((s, s), Image.NEAREST) for s in (16, 32, 48, 64, 128, 256)]
        imgs[-1].save(STATIC / name, sizes=[(im.width, im.height) for im in imgs])
    print("Wrote pixel-style icon:", STATIC / "app_icon.png", "+ .ico set")


if __name__ == "__main__":
    main()
