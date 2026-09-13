"""Create a compact visual audit sheet for a folder of video sources."""
from __future__ import annotations

import sys
from pathlib import Path

import cv2
from PIL import Image, ImageDraw, ImageFont


def frame(path: Path) -> Image.Image | None:
    cap = cv2.VideoCapture(str(path))
    try:
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, int(total * .42)))
        ok, raw = cap.read()
        if not ok:
            return None
        raw = cv2.cvtColor(raw, cv2.COLOR_BGR2RGB)
        image = Image.fromarray(raw)
        image.thumbnail((190, 338))
        canvas = Image.new("RGB", (200, 380), "#121817")
        canvas.paste(image, ((200 - image.width) // 2, 4))
        ImageDraw.Draw(canvas).text((6, 348), path.stem.replace("tiktok_", ""), fill="white", font=ImageFont.load_default())
        return canvas
    finally:
        cap.release()


def main() -> int:
    source, output = map(Path, sys.argv[1:3])
    tiles = [tile for file in sorted(source.glob("*.mp4")) if (tile := frame(file))]
    cols = 6
    sheet = Image.new("RGB", (cols * 200, ((len(tiles) + cols - 1) // cols) * 380), "#080b0a")
    for index, tile in enumerate(tiles):
        sheet.paste(tile, ((index % cols) * 200, (index // cols) * 380))
    output.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(output, quality=88)
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
