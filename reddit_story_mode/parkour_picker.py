"""Pick a background Minecraft-parkour clip from a LOCAL approved pool only.

Never scrapes video from YouTube/TikTok/etc. The pool is a local folder the user fills with
their own approved .mp4 files.
"""

import os
import random
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PARKOUR_DIR = Path(os.environ.get("PARKOUR_POOL_DIR") or (ROOT / "assets" / "parkour_pool"))
_VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".webm"}

EMPTY_MESSAGE = ("No approved Minecraft parkour clips found. "
                 "Add .mp4 files to assets/parkour_pool/.")


def list_clips():
    if not PARKOUR_DIR.exists():
        return []
    return sorted([p for p in PARKOUR_DIR.iterdir()
                   if p.is_file() and p.suffix.lower() in _VIDEO_EXTS and p.stat().st_size > 4096])


def pick_clip(status_cb=None, seed=None):
    """Return a random approved parkour clip Path. Raises RuntimeError with a readable message
    when the pool is empty."""
    PARKOUR_DIR.mkdir(parents=True, exist_ok=True)
    clips = list_clips()
    if not clips:
        raise RuntimeError(EMPTY_MESSAGE)
    rng = random.Random(seed) if seed is not None else random
    choice = rng.choice(clips)
    if status_cb:
        status_cb(f"Selected parkour clip: {choice.name}")
    return choice
