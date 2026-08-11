"""Run Blender headless and sweep one parameter across several values.

Blender is found once and cached. Each sweep value is a SEPARATE process: a physics run
leaves a baked cache and mutated scene state behind, and reusing one process would let
the first value's simulation bleed into the second's.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

# Cycles, not EEVEE. Headless EEVEE has no GPU context and falls back to a software
# rasteriser: measured 19.2s per frame against Cycles' 3.0s on the same test scene.
DEFAULT_ENGINE = "CYCLES"
_FRAME = re.compile(r"Fra:(\d+)")
SEARCH = [
    Path(r"D:\data\Blender\blender.exe"),
    Path(r"C:\Program Files\Blender Foundation"),
    Path(r"C:\Program Files (x86)\Blender Foundation"),
]
_CACHED: str | None = None


class BlenderNotFound(RuntimeError):
    pass


def blender_path(explicit: str | None = None) -> str:
    """Path to blender.exe. Checks the env var, then known install roots, then PATH."""
    global _CACHED
    if explicit:
        return explicit
    if _CACHED:
        return _CACHED
    env = os.environ.get("BLENDER_EXE")
    if env and Path(env).exists():
        _CACHED = env
        return env
    for root in SEARCH:
        if root.is_file():
            _CACHED = str(root)
            return _CACHED
        if root.is_dir():
            hit = next((p for p in root.rglob("blender.exe")), None)
            if hit:
                _CACHED = str(hit)
                return _CACHED
    which = shutil.which("blender")
    if which:
        _CACHED = which
        return which
    raise BlenderNotFound(
        "Blender not found. Install it, or set BLENDER_EXE to blender.exe.")


@dataclass
class SweepResult:
    label: str
    value: float
    frames_dir: str
    clip: str | None = None
    seconds: float = 0.0
    ok: bool = False
    error: str = ""

    def as_dict(self) -> dict:
        return {"label": self.label, "value": self.value, "clip": self.clip,
                "frames_dir": self.frames_dir, "seconds": round(self.seconds, 1),
                "ok": self.ok, "error": self.error}


def run_scene(script: str | Path, out_dir: str | Path, params: dict, *,
              blender: str | None = None, timeout: int = 7200,
              status_cb=None) -> tuple[bool, str]:
    """Run one scene script headless with `params` handed over as JSON after `--`."""
    exe = blender_path(blender)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    cmd = [exe, "-b", "-noaudio", "-P", str(script), "--",
           json.dumps({**params, "out_dir": str(out)})]
    if status_cb:
        status_cb(f"blender: {Path(script).name} {params}")
    total = int(float(params.get("seconds", 4.0)) * int(params.get("fps", 30)))
    # Streamed rather than captured in one go: a full-resolution frame costs ~9s even on
    # the GPU, so a three-value sweep runs the better part of an hour. Without per-frame
    # reporting the app's progress box sits dead the whole time and the run looks hung.
    lines: list[str] = []
    last = 0.0
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace", bufsize=1)
    deadline = time.time() + timeout
    # Read by chunk, not by line: Blender redraws its render status with CARRIAGE RETURNS,
    # so iterating over stdout lines holds the entire render inside one unterminated line
    # and nothing is reported until the process exits.
    buf = ""
    try:
        while True:
            chunk = proc.stdout.read(256)   # type: ignore[union-attr]
            if not chunk:
                break
            if time.time() > deadline:
                proc.kill()
                return False, f"timed out after {timeout}s"
            buf += chunk
            parts = re.split(r"[\r\n]", buf)
            buf = parts.pop()
            for part in parts:
                if part.strip():
                    lines.append(part.rstrip())
                m = _FRAME.search(part)
                if m and status_cb and time.time() - last > 4.0:
                    last = time.time()
                    n = int(m.group(1))
                    status_cb(f"  frame {n}/{total}" if total else f"  frame {n}")
        if buf.strip():
            lines.append(buf.rstrip())
        proc.wait(timeout=max(1, int(deadline - time.time())))
    except subprocess.TimeoutExpired:
        proc.kill()
        return False, f"timed out after {timeout}s"
    out = "\n".join(lines)
    tail = "\n".join(lines[-25:])
    if proc.returncode != 0:
        return False, f"exit {proc.returncode}\n{tail}"
    # The script prints its own verdict; a zero exit code alone is not proof, Blender
    # returns 0 for a run that rendered nothing.
    if "SCENE_OK" not in out:
        return False, f"no SCENE_OK in output\n{tail}"
    return True, tail


def frames_to_clip(frames_dir: str | Path, out_path: str | Path, fps: int = 30,
                   ffmpeg: str = "ffmpeg") -> str | None:
    """Encode a scene's PNG frames into one clip. Blender 5.2 has no FFMPEG output."""
    frames_dir = Path(frames_dir)
    if not sorted(frames_dir.glob("frame_*.png")):
        return None
    out_path = Path(out_path)
    r = subprocess.run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                        "-framerate", str(fps), "-i", str(frames_dir / "frame_%04d.png"),
                        "-c:v", "libx264", "-crf", "17", "-preset", "medium",
                        "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out_path)],
                       capture_output=True, text=True, timeout=1800)
    return str(out_path) if out_path.exists() and r.returncode == 0 else None


def render_sweep(script: str | Path, out_dir: str | Path, *,
                 param: str, values: Sequence[float],
                 labels: Sequence[str] | None = None,
                 base_params: dict | None = None,
                 blender: str | None = None, status_cb=None,
                 timeout: int = 7200) -> list[SweepResult]:
    """Render the same scene once per value of `param`. One process per value."""
    out_dir = Path(out_dir)
    labels = list(labels or [f"{v:g}" for v in values])
    results: list[SweepResult] = []
    for i, value in enumerate(values):
        label = labels[i] if i < len(labels) else f"{value:g}"
        sub = out_dir / f"{param}_{value:g}".replace(".", "_")
        params = dict(base_params or {})
        params[param] = value
        params["label"] = label
        t0 = time.time()
        ok, msg = run_scene(script, sub, params, blender=blender,
                            timeout=timeout, status_cb=status_cb)
        clip = sub / "clip.mp4"
        if ok:
            frames_to_clip(sub, clip, fps=int(params.get("fps", 30)))
        res = SweepResult(label=label, value=float(value), frames_dir=str(sub),
                          clip=str(clip) if clip.exists() else None,
                          seconds=time.time() - t0, ok=ok,
                          error="" if ok else msg[-500:])
        results.append(res)
        if status_cb:
            status_cb(f"  {label}: {'ok' if ok else 'FAILED'} in {res.seconds:.0f}s")
        (out_dir / "sweep.json").write_text(
            json.dumps([r.as_dict() for r in results], indent=1), encoding="utf-8")
    return results


def concat_clips(results: Iterable[SweepResult], out_path: str | Path,
                 ffmpeg: str = "ffmpeg") -> str | None:
    """Join the sweep's clips in order into the finished short."""
    clips = [r.clip for r in results if r.ok and r.clip and Path(r.clip).exists()]
    if not clips:
        return None
    out_path = Path(out_path)
    listing = out_path.with_suffix(".txt")
    listing.write_text("".join(f"file '{Path(c).as_posix()}'\n" for c in clips),
                       encoding="utf-8")
    subprocess.run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                    "-f", "concat", "-safe", "0", "-i", str(listing),
                    "-c:v", "libx264", "-crf", "18", "-preset", "medium",
                    "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out_path)],
                   capture_output=True, timeout=1800)
    listing.unlink(missing_ok=True)
    return str(out_path) if out_path.exists() else None
