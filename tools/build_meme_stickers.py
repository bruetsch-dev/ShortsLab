from pathlib import Path
from io import BytesIO

from PIL import Image, ImageFilter, ImageOps
from rembg import new_session, remove


ROOT = Path(r"D:\data\AutoShortsClaude")
OUT = ROOT / "assets" / "meme_stickers"
REVIEW = Path(r"C:\Users\USCSt\.codex\attachments\meme-reaction-review")
HUH = Path(r"C:\Users\USCSt\AppData\Local\Temp\codex-clipboard-aefc326f-3dae-4649-9507-7e1ff52b6dc0.png")


def sticker(source, destination: Path, session) -> None:
    if isinstance(source, Image.Image):
        buffer = BytesIO()
        source.save(buffer, format="PNG")
        raw = buffer.getvalue()
    else:
        raw = source.read_bytes()
    cut = Image.open(BytesIO(remove(raw, session=session, alpha_matting=False, post_process_mask=True))).convert("RGBA")
    alpha = cut.getchannel("A")
    bbox = alpha.getbbox()
    if not bbox:
        raise RuntimeError(f"No foreground detected: {destination.name}")
    cut = cut.crop(bbox)
    alpha = cut.getchannel("A")

    pad = max(20, round(max(cut.size) * 0.09))
    outline_width = max(8, round(max(cut.size) * 0.035))
    key_width = max(2, round(max(cut.size) * 0.004))
    canvas_size = (cut.width + 2 * pad, cut.height + 2 * pad)
    subject_alpha = Image.new("L", canvas_size)
    subject_alpha.paste(alpha, (pad, pad))
    white_mask = subject_alpha.filter(ImageFilter.MaxFilter(outline_width * 2 + 1))
    key_mask = white_mask.filter(ImageFilter.MaxFilter(key_width * 2 + 1))

    canvas = Image.new("RGBA", canvas_size, (0, 0, 0, 0))
    key = Image.new("RGBA", canvas_size, (42, 42, 42, 255))
    white = Image.new("RGBA", canvas_size, (255, 255, 255, 255))
    canvas.alpha_composite(Image.composite(key, Image.new("RGBA", canvas_size), key_mask))
    canvas.alpha_composite(Image.composite(white, Image.new("RGBA", canvas_size), white_mask))
    canvas.alpha_composite(cut, (pad, pad))
    destination.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(destination, optimize=True)


def main() -> None:
    sources = [("00_huh_dog", HUH)]
    sources.extend((p.stem, p) for p in sorted(REVIEW.iterdir()) if p.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"} and p.name != "10_scared_dog_belle.png")
    scared = Image.open(REVIEW / "10_scared_dog_belle.png").convert("RGB")
    edges = [0, scared.width // 3, (scared.width * 2) // 3, scared.width]
    for index in range(3):
        sources.append((f"10_scared_dog_belle_{index + 1}", scared.crop((edges[index], 0, edges[index + 1], scared.height))))
    session = new_session("u2net")
    for name, source in sources:
        destination = OUT / f"{name}.png"
        source_name = source.name if isinstance(source, Path) else f"panel {name[-1]}"
        print(f"{source_name} -> {destination.name}", flush=True)
        sticker(source, destination, session)


if __name__ == "__main__":
    main()
