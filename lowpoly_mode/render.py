"""Render one shot spec through Blender. No model is involved past this point.

The spec is handed to `blender_kit/render_shot.py`, which is a fixed script - the same one
every time. Nothing here can fail for a reason a model caused, so there is no repair loop
and no retry budget: a failure here is a bug in our code and should be read as one.
"""

from __future__ import annotations

import json
from pathlib import Path

from physics_mode.blender_runner import frames_to_clip, run_scene

KIT = Path(__file__).resolve().parent / "blender_kit"
SCRIPT = KIT / "render_shot.py"


def render_shot(spec: dict, out_dir, seconds: float, *, res=(540, 960), fps: int = 24,
                status_cb=None, blender: str | None = None) -> Path | None:
    """Build and encode one shot. Returns the clip path, or None if Blender failed."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "spec.json").write_text(json.dumps(spec, indent=1), encoding="utf-8")
    ok, msg = run_scene(SCRIPT, out_dir,
                        {"res_x": res[0], "res_y": res[1], "fps": fps,
                         "seconds": float(seconds), "kit_dir": str(KIT), "spec": spec},
                        blender=blender, status_cb=status_cb, timeout=1800)
    if not ok:
        (status_cb or print)(f"  shot failed: {msg[-300:]}")
        return None
    clip = out_dir / "clip.mp4"
    return Path(clip) if frames_to_clip(out_dir, clip, fps=fps) else None


def preview_shot(spec: dict, out_dir, seconds: float = 2.0, frame: int = 6,
                 res=(360, 640), status_cb=None, blender: str | None = None) -> Path | None:
    """One still from a shot, for the approval gate."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ok, _msg = run_scene(SCRIPT, out_dir,
                         {"res_x": res[0], "res_y": res[1], "fps": 24,
                          "seconds": float(seconds), "preview_frame": int(frame),
                          "kit_dir": str(KIT), "spec": spec},
                         blender=blender, status_cb=status_cb, timeout=600)
    shot = out_dir / "preview.png"
    return shot if ok and shot.is_file() else None
