"""Build one physics-sweep short end to end.

    sweep the parameter -> encode each run -> join -> lay the material sounds on top

Every step writes into the project folder, so a failed run can be inspected and resumed
rather than repeated from scratch.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

from . import mmaudio, sound
from .blender_runner import concat_clips, frames_to_clip, render_sweep, run_scene

SCENES = Path(__file__).resolve().parent / "scenes"
PRESETS = {
    # label, scene file, parameter, values, unit
    "wrecking_ball": {
        "title": "Wrecking ball vs brick block",
        "scene": "wrecking_ball.py",
        "param": "mass",
        "values": [1, 10, 50],
        "unit": "kg",
        "seconds": 4.0,
    },
    # A LOOP preset renders one period and repeats it. The motion is periodic by
    # construction, so repeating costs nothing and an 8s video is one 2.4s render.
    "tumbling_cube": {
        "title": "Tumbling cube into glowing gaps",
        "scene": "tumbling_cube.py",
        "kind": "loop",
        "seconds": 9.6,
        "unit": "",
    },
}


def preview_frame(out_dir, preset: str = "wrecking_ball", *, values=None,
                  res=(540, 960), samples: int = 20, frame: int | None = None,
                  status_cb=None, custom: dict | None = None,
                  blender: str | None = None) -> str:
    """Render ONE cheap frame of the preset, for the approval gate.

    Deliberately mid-action rather than frame 1: an empty establishing shot tells the
    user nothing about whether the shot is worth an hour of GPU time.
    """
    cfg = config_for(preset, custom)
    out = Path(out_dir) / "preview"
    out.mkdir(parents=True, exist_ok=True)
    params = {"res_x": res[0], "res_y": res[1], "samples": samples,
              "seconds": float(cfg.get("seconds") or 4.0)}
    if cfg.get("kind") == "loop" or cfg.get("authored"):
        params["preview_frame"] = frame or 10
    else:
        vals = list(values or cfg["values"])
        mid = vals[len(vals) // 2]
        params[cfg["param"]] = mid
        params["label"] = f"{mid:g}{cfg['unit']}"
        # ~2/3 through: the collision has happened and the result is visible
        params["preview_frame"] = frame or int(cfg["seconds"] * 30 * 0.62)
    ok, msg = run_scene(_scene_path(cfg), out, params, blender=blender,
                        status_cb=status_cb)
    shot = out / "preview.png"
    if not ok or not shot.is_file():
        raise RuntimeError(f"Preview frame failed: {msg[-400:]}")
    return str(shot)


def _log(cb, msg):
    (cb or print)(msg)


def _probe(path, ffprobe="ffprobe") -> float:
    r = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration",
                        "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    try:
        return float((r.stdout or "").strip())
    except ValueError:
        return 0.0


def config_for(preset: str = "wrecking_ball", custom: dict | None = None) -> dict:
    """Preset config, or one synthesised from an authored scene.

    An authored scene is just a preset whose file happens to live in scenes/generated, so
    everything downstream treats the two identically.
    """
    if not custom:
        return PRESETS[preset]
    return {"title": custom.get("title") or "Custom physics scene",
            "scene": Path(custom["scene_path"]),
            "kind": "loop" if custom.get("kind") == "loop" else "single",
            "seconds": float(custom.get("seconds") or 4.0),
            "unit": "", "authored": True}


def _scene_path(cfg) -> Path:
    scene = cfg["scene"]
    return Path(scene) if isinstance(scene, Path) else SCENES / scene


def _lay_sound(silent: Path, plan, final: Path, *, title: str, total: float,
               status_cb, ffmpeg: str, use_mmaudio: bool = True):
    """Put audio on the cut: MMAudio V2 first, locally synthesised material as fallback.

    MMAudio watches the video and scores it, so it needs no event list at all - measured on
    the wrecking-ball sweep its three loudest moments landed at 0.5/4.6/8.5s against
    impacts at 0.47/4.47/8.47s. The local synthesis stays because a mode that goes silent
    when the network or the balance does is worse than one that sounds simpler.
    """
    if use_mmaudio:
        got = mmaudio.generate(silent, final, prompt=mmaudio.prompt_for(title),
                               duration=total, status_cb=status_cb, ffmpeg=ffmpeg)
        if got:
            return Path(got), "mmaudio"
    mixed = sound.mux(silent, plan, final, ffmpeg=ffmpeg, duration=total)
    if mixed:
        _log(status_cb, f"Physics: local sound mixed ({len(plan.events)} events)")
        return Path(mixed), "local"
    _log(status_cb, "Physics: no sound available - keeping the silent cut.")
    return Path(silent), "silent"


def build(out_dir, preset: str = "wrecking_ball", *, values=None,
          res=(1080, 1920), samples: int = 24, fps: int = 30,
          seconds: float | None = None, status_cb=None, custom: dict | None = None,
          blender: str | None = None, ffmpeg: str = "ffmpeg") -> dict:
    """Render the sweep and return {"video": ..., "sweep": [...], "sound": {...}}."""
    cfg = config_for(preset, custom)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    secs = float(seconds or cfg["seconds"])
    if cfg.get("authored") and cfg.get("kind") != "loop":
        return _build_single(cfg, preset, out, res=res, samples=samples, fps=fps,
                             seconds=secs, status_cb=status_cb, blender=blender,
                             ffmpeg=ffmpeg)
    values = list(values or cfg.get("values") or [])
    labels = [f"{v:g}{cfg['unit']}" for v in values]

    if cfg.get("kind") == "loop":
        return _build_loop(cfg, preset, out, res=res, samples=samples, fps=fps,
                           seconds=secs, status_cb=status_cb, blender=blender,
                           ffmpeg=ffmpeg)

    _log(status_cb, f"Physics: {cfg['title']} - sweeping {cfg['param']} over {labels}")
    results = render_sweep(
        _scene_path(cfg), out / "runs",
        param=cfg["param"], values=values, labels=labels,
        base_params={"res_x": res[0], "res_y": res[1], "samples": samples,
                     "fps": fps, "seconds": secs},
        blender=blender, status_cb=status_cb)

    ok = [r for r in results if r.ok]
    if not ok:
        raise RuntimeError("Physics: every sweep value failed - "
                           + (results[0].error if results else "no runs"))
    if len(ok) < len(results):
        _log(status_cb, f"Physics: {len(results) - len(ok)} of {len(results)} values "
                        "failed; building from the rest.")

    silent = out / "sweep_silent.mp4"
    joined = concat_clips(ok, silent, ffmpeg=ffmpeg)
    if not joined:
        raise RuntimeError("Physics: the sweep clips could not be joined.")
    total = _probe(joined)
    _log(status_cb, f"Physics: joined {len(ok)} run(s) -> {total:.1f}s")

    rows = []
    for r in ok:
        rows.append({"label": r.label, "value": r.value, "frames_dir": r.frames_dir,
                     "seconds_len": _probe(r.clip) if r.clip else secs})
    plan = sound.plan_for_sweep(rows, clip_seconds=secs)
    final, audio_src = _lay_sound(Path(joined), plan, out / "video.mp4",
                                  title=cfg["title"], total=total,
                                  status_cb=status_cb, ffmpeg=ffmpeg)

    report = {"preset": preset, "title": cfg["title"], "param": cfg["param"],
              "values": values, "video": str(final), "seconds": round(total, 2),
              "audio_source": audio_src,
              "sweep": [r.as_dict() for r in results], "sound": plan.as_dict()}
    (out / "report.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    return report


def _build_loop(cfg, preset, out: Path, *, res, samples, fps, seconds, status_cb,
                blender, ffmpeg) -> dict:
    """Render one loop period and repeat it to length.

    The scene guarantees its last frame continues into its first, so this is a hard cut
    with no crossfade and no visible seam - and an 8 second video costs one 2.4 second
    render.
    """
    _log(status_cb, f"Physics: {cfg['title']} - rendering one seamless loop")
    sub = out / "runs" / "loop"
    ok, msg = run_scene(_scene_path(cfg), sub,
                        {"res_x": res[0], "res_y": res[1], "samples": samples,
                         "fps": fps},
                        blender=blender, status_cb=status_cb)
    if not ok:
        raise RuntimeError(f"Physics: the loop failed to render - {msg[-400:]}")
    one = sub / "clip.mp4"
    if not frames_to_clip(sub, one, fps=fps):
        raise RuntimeError("Physics: the loop frames could not be encoded.")
    loop_len = _probe(one)
    repeats = max(1, round(seconds / loop_len)) if loop_len else 1
    _log(status_cb, f"Physics: loop is {loop_len:.2f}s - repeating it {repeats}x")
    silent = out / "sweep_silent.mp4"
    listing = silent.with_suffix(".txt")
    listing.write_text("".join(f"file '{one.as_posix()}'\n" for _ in range(repeats)),
                       encoding="utf-8")
    subprocess.run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                    "-f", "concat", "-safe", "0", "-i", str(listing),
                    "-c", "copy", "-movflags", "+faststart", str(silent)],
                   capture_output=True, timeout=1800)
    listing.unlink(missing_ok=True)
    if not silent.exists():
        raise RuntimeError("Physics: the repeated loop could not be joined.")
    total = _probe(silent)
    plan = sound.plan_for_loop(sub, repeats=repeats, loop_seconds=loop_len)
    final, audio_src = _lay_sound(silent, plan, out / "video.mp4", title=cfg["title"],
                                  total=total, status_cb=status_cb, ffmpeg=ffmpeg)
    report = {"preset": preset, "title": cfg["title"], "kind": "loop",
              "loop_seconds": round(loop_len, 3), "repeats": repeats,
              "video": str(final), "seconds": round(total, 2),
              "audio_source": audio_src, "sound": plan.as_dict()}
    (out / "report.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    return report


def _build_single(cfg, preset, out: Path, *, res, samples, fps, seconds, status_cb,
                  blender, ffmpeg) -> dict:
    """One authored scene, rendered once. No sweep, no repeat - what the prompt asked for."""
    _log(status_cb, f"Physics: {cfg['title']} - rendering {seconds:.1f}s")
    sub = out / "runs" / "scene"
    ok, msg = run_scene(_scene_path(cfg), sub,
                        {"res_x": res[0], "res_y": res[1], "samples": samples,
                         "fps": fps, "seconds": seconds},
                        blender=blender, status_cb=status_cb)
    if not ok:
        raise RuntimeError(f"Physics: the scene failed to render - {msg[-400:]}")
    silent = out / "sweep_silent.mp4"
    if not frames_to_clip(sub, silent, fps=fps):
        raise RuntimeError("Physics: the frames could not be encoded.")
    total = _probe(silent)
    plan = sound.plan_for_events(sub, offset=0.0)
    final, audio_src = _lay_sound(silent, plan, out / "video.mp4", title=cfg["title"],
                                  total=total, status_cb=status_cb, ffmpeg=ffmpeg)
    report = {"preset": preset, "title": cfg["title"], "kind": "single",
              "video": str(final), "seconds": round(total, 2),
              "audio_source": audio_src, "sound": plan.as_dict()}
    (out / "report.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    return report
