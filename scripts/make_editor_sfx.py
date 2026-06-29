"""Regenerate the synthesized 'editor pack' of short TikTok-documentary SFX.

The synthesis lives in pipeline.ensure_editor_sfx_pack so the app can also generate it on demand
at render time (the bundled CC0 library lacks whoosh/pop/ding/riser/shutter/reverse sounds). This
script just forces a fresh rebuild into soundeffects/shorts_ready/editor_pack/.

Run:  python scripts/make_editor_sfx.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pipeline  # noqa: E402

if __name__ == "__main__":
    folder = pipeline.ensure_editor_sfx_pack(ROOT / "soundeffects", force=True)
    n = len(list(Path(folder).glob("*.wav")))
    print(f"editor_pack: wrote {n} clips to {folder}")
