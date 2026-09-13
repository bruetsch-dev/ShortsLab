"""Module 4 - Visual Overlays & Engagement Hacks.

`add_visual_pointer(x, y, asset_path, timestamp)` is the whole interface: a transparent
PNG lands at a normalised coordinate for a beat or two, and the matching "pop" is
registered at the same timestamp so the sound and the image always arrive together -
adding them separately is how they drift apart.

`OverlayPlan.to_filter_complex()` emits the ffmpeg overlay chain, so the plan can be
rendered without a second implementation of the same maths.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

DEFAULT_HOLD = 1.4        # seconds an overlay stays on screen
FADE = 0.12


@dataclass
class Overlay:
    asset: str
    x: float                  # 0..1, fraction of frame width (centre of the asset)
    y: float                  # 0..1, fraction of frame height
    start: float
    duration: float = DEFAULT_HOLD
    scale: float = 1.0        # relative to the asset's own size
    fade: float = FADE
    sfx: str | None = None

    @property
    def end(self) -> float:
        return round(self.start + self.duration, 3)

    def as_dict(self) -> dict:
        return {"asset": self.asset, "x": round(self.x, 4), "y": round(self.y, 4),
                "start": round(self.start, 3), "duration": round(self.duration, 3),
                "end": self.end, "scale": round(self.scale, 3),
                "fade": self.fade, "sfx": self.sfx}


@dataclass
class OverlayPlan:
    width: int = 1080
    height: int = 1920
    overlays: list[Overlay] = field(default_factory=list)
    sfx_events: list[dict] = field(default_factory=list)

    # ---------------------------------------------------------- the interface

    def add_visual_pointer(self, coordinate_x: float, coordinate_y: float,
                           asset_path: str, timestamp: float, *,
                           duration: float = DEFAULT_HOLD, scale: float = 1.0,
                           sfx_timeline=None, sfx_gain: float = 0.6) -> Overlay:
        """Place a transparent PNG at (x, y) from `timestamp`, with its sound.

        Coordinates accept either normalised 0..1 or absolute pixels - anything above 1
        is read as pixels and converted, so callers working from face boxes or from a
        layout grid can both use this without converting first.
        """
        p = Path(str(asset_path))
        if not p.exists():
            raise FileNotFoundError(f"Overlay asset not found: {asset_path}")
        x = coordinate_x / self.width if coordinate_x > 1 else float(coordinate_x)
        y = coordinate_y / self.height if coordinate_y > 1 else float(coordinate_y)
        x = min(max(x, 0.0), 1.0)
        y = min(max(y, 0.0), 1.0)
        ov = Overlay(asset=str(p), x=x, y=y, start=round(float(timestamp), 3),
                     duration=float(duration), scale=float(scale))
        # register the sound at the SAME timestamp - a separate call is how the ding
        # and the arrow end up on different frames
        if sfx_timeline is not None:
            ev = sfx_timeline.on_overlay(ov.start, gain=sfx_gain)
            if ev:
                ov.sfx = ev["path"]
                self.sfx_events.append(ev)
        self.overlays.append(ov)
        return ov

    # ---------------------------------------------------------- render

    def to_filter_complex(self, base_label: str = "0:v") -> tuple[str, list[str]]:
        """(filter_complex string, extra ffmpeg inputs) for the overlay chain.

        Each overlay is scaled, alpha-faded in and out, and centred on its coordinate;
        `enable` keeps it on screen only for its window.
        """
        if not self.overlays:
            return "", []
        inputs, parts, cur = [], [], base_label
        for i, ov in enumerate(self.overlays):
            inputs.append(ov.asset)
            src = f"{i + 1}:v"
            lbl = f"ov{i}"
            fade_out_at = max(0.0, ov.duration - ov.fade)
            parts.append(
                f"[{src}]format=rgba,scale=iw*{ov.scale:.3f}:-1,"
                f"fade=t=in:st=0:d={ov.fade}:alpha=1,"
                f"fade=t=out:st={fade_out_at:.3f}:d={ov.fade}:alpha=1,"
                f"setpts=PTS-STARTPTS+{ov.start:.3f}/TB[{lbl}]")
            out = f"v{i}"
            parts.append(
                f"[{cur}][{lbl}]overlay="
                f"x={ov.x:.4f}*W-w/2:y={ov.y:.4f}*H-h/2:"
                f"enable='between(t,{ov.start:.3f},{ov.end:.3f})'[{out}]")
            cur = out
        parts.append(f"[{cur}]null[vout]")
        return ";".join(parts), inputs

    def as_dict(self) -> dict:
        return {"width": self.width, "height": self.height,
                "overlays": [o.as_dict() for o in self.overlays],
                "sfx_events": self.sfx_events}


def pointers_from_cuts(plan, asset: str, *, every: int = 3,
                       sfx_timeline=None, width: int = 1080,
                       height: int = 1920) -> OverlayPlan:
    """Drop a pointer on every nth cut, anchored on that cut's punch anchor.

    Used to sprinkle arrows without hand-authoring each one; the anchor comes from the
    cut so the arrow points where the framing already leans.
    """
    op = OverlayPlan(width=width, height=height)
    for i, c in enumerate(getattr(plan, "cuts", [])):
        if i == 0 or i % every:
            continue
        ax, ay = getattr(c, "anchor", (0.5, 0.5))
        op.add_visual_pointer(ax, max(0.12, ay - 0.12), asset,
                              c.start + 0.12, sfx_timeline=sfx_timeline)
    return op
