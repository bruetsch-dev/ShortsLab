"""Write and render one crude low-poly Blender shot per beat.

Same shape as physics_mode.authoring - the model writes a whole bpy script, we RUN it, and
a crash comes back with its traceback for repair - but the contract is the opposite in
spirit. There, correctness of the simulation was the point. Here the look is deliberately
cheap, so the rules exist to keep shots FAST and CONSISTENT with each other, not accurate.

Figures are written by the model too. A blocky person made of eight boxes moving stiffly is
the target, not an accident, so there is no rig library to get in the way.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

import agent_core

from physics_mode.blender_runner import frames_to_clip, run_scene

GENERATED = Path(__file__).resolve().parent / "generated"
SHOT_MODEL = "anthropic/claude-opus-4.8"
MAX_ATTEMPTS = 3

CONTRACT = """You write ONE complete Python script that runs inside Blender 5.2 headless
(`blender -b -noaudio -P shot.py -- '<json>'`), using only `bpy`, `math`, `json`, `sys`
and `random`. No external assets, no add-ons, no downloads.

The look is CRUDE LOW POLY on purpose - the PS1-era cheap 3D look. It does not need to
look good. It needs to be readable and fast.

HARD REQUIREMENTS - the script is run and rejected if any is missing:

1. Parameters come from argv after "--" as JSON:
       argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
       P = json.loads(argv[0]) if argv else {}
   Honour out_dir, res_x, res_y, samples, fps, seconds, preview_frame.

2. Use EEVEE, not Cycles:
       sc.render.engine = "BLENDER_EEVEE_NEXT"
   The style has no soft shadows or bounce light to lose, and EEVEE renders it in a
   fraction of the time. If that engine name is unavailable, fall back to "CYCLES" with
   samples set to 8.

3. If preview_frame > 0: `sc.frame_set(preview_frame)`, render ONE still to
   `{out_dir}/preview.png`, print `SCENE_OK preview=<n>`, return without rendering the
   animation. Otherwise render frames 1..int(seconds*fps) to filepath `{out_dir}/frame_`
   as PNG.

4. Print a line starting with `SCENE_OK` last on success.

BLENDER 5.2 - these break the script if ignored:
  * `image_settings.file_format = "FFMPEG"` does not exist. PNG frames only.
  * `Action.fcurves` is gone. Set
    `bpy.context.preferences.edit.keyframe_new_interpolation_type = "LINEAR"` BEFORE
    inserting keys instead of fixing curves afterwards.
  * `Mesh.use_auto_smooth` is gone. Never touch it.
  * The Cell Fracture add-on is gone.

HOW TO BUILD IT:
  * Everything is boxes, spheres, cylinders and planes. Flat shaded. No bevels, no
    subdivision, no smooth shading, no procedural node graphs beyond a Base Color.
  * PEOPLE are eight or so boxes: head, torso, two arms, two legs, maybe feet. Parent the
    limbs to the torso and keyframe crude rotations. Stiff and robotic is CORRECT - do not
    attempt weight painting, armatures or IK.
  * Animate by keyframing object location and rotation directly. Motion can be simple:
    walking is legs swinging back and forth while the body slides forward.
  * Give the ground a plane and put the objects ON it. Nothing floats unless it is meant
    to be in the air.
  * Colour: flat, saturated, slightly wrong - that is the aesthetic. Keep the background
    a solid dark colour so the shapes read.
  * LIGHTS MUST NOT BE VISIBLE: set `light_object.visible_camera = False` on every light.

CAMERA - this is a 9:16 vertical short:
  * Blender fits the sensor to the LARGER resolution axis, so in 9:16 the visible HEIGHT
    is `36 / lens * distance`. Solve the camera distance from the size of what you built
    so the subject fills roughly half the frame height. Never hard-code a distance and
    hope.
  * Point the camera AT the subject's centre. A shot that renders empty background is a
    failed shot.
  * Mostly locked off. A slow push-in or a slow track is fine; nothing fast.

Return ONLY a JSON object:
  "scene_py": the complete script as one string
  "notes":    one sentence on what the shot shows
"""


VISION_MODEL = "google/gemini-3.5-flash-lite"


def judge_frame(path, wanted: str, model: str = VISION_MODEL) -> tuple[bool, str]:
    """Ask a vision model whether the frame actually shows the shot that was ordered.

    The pixel heuristic below only catches a frame with NOTHING in it. It cannot catch the
    more common failure: a frame with the wrong thing in it, or the right things hidden
    behind each other. One cheap vision call per shot can, and it is the only check that
    compares the picture against what the shot was supposed to be.

    Returns (ok, reason). On any error it returns ok - a flaky vision call must not be able
    to fail a shot that renders perfectly well.
    """
    try:
        out = agent_core._post_llm_json(
            model,
            [{"role": "system", "content":
              "You check frames from a deliberately crude low-poly 3D animation. Ugly, "
              "blocky and simple is CORRECT and never a reason to fail. Fail a frame ONLY "
              "if it does not show the requested subject at all: an empty background, the "
              "camera pointing the wrong way, the subject off-frame, or a solid colour. "
              'Return JSON {"shows_it": true|false, "reason": "<short>"}.'},
             {"role": "user", "content": [
                 {"type": "text", "text": f"The shot should show: {wanted}"},
                 {"type": "image_url",
                  "image_url": {"url": agent_core.image_data_url(path)}}]}],
            max_tokens=300, temperature=0.0, timeout=180) or {}
    except Exception:  # noqa: BLE001
        return True, "vision check unavailable"
    if out.get("shows_it") is False:
        return False, str(out.get("reason") or "the frame does not show the shot")
    return True, str(out.get("reason") or "ok")


def _slug(text: str, index: int) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", str(text or "shot").lower()).strip("_")
    return f"{index:02d}_{(s[:30] or 'shot')}"


def author_shot(scene_text: str, index: int, seconds: float, work_dir: Path,
                status_cb=None, model: str = SHOT_MODEL,
                blender: str | None = None) -> Path:
    """Write, test and repair one shot script. Returns the path to the working script."""
    log = status_cb or print
    GENERATED.mkdir(parents=True, exist_ok=True)
    probe = Path(work_dir) / "probe" / f"{index:02d}"
    probe.mkdir(parents=True, exist_ok=True)
    messages = [
        {"role": "system", "content": CONTRACT},
        {"role": "user", "content": f"Shot {index}, about {seconds:.1f} seconds:\n\n"
                                    f"{scene_text}"},
    ]
    last = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        log(f"  shot {index}: writing scene (attempt {attempt})")
        out = agent_core._post_llm_json(model, messages, max_tokens=8000,
                                        temperature=0.3, timeout=420) or {}
        code = str(out.get("scene_py") or "")
        if not code.strip():
            last = "no scene_py returned"
            messages.append({"role": "user", "content":
                             "You returned no scene_py. Return the JSON object with the "
                             "complete script in scene_py."})
            continue
        path = GENERATED / f"{_slug(scene_text, index)}_{time.strftime('%H%M%S')}.py"
        path.write_text(code, encoding="utf-8")
        ok, msg = run_scene(path, probe,
                            {"res_x": 270, "res_y": 480, "samples": 8, "fps": 24,
                             "seconds": seconds, "preview_frame": 4},
                            blender=blender, timeout=600)
        shot = probe / "preview.png"
        if ok and shot.is_file() and _looks_empty(shot):
            ok, msg = False, ("The script ran but rendered an EMPTY frame - only "
                              "background. The camera is not pointing at what you built. "
                              "Compute the camera position from the actual bounds of your "
                              "objects and aim it at their centre.")
        if ok and shot.is_file():
            seen, why = judge_frame(shot, scene_text)
            if not seen:
                ok, msg = False, (
                    f"The frame rendered, but it does not show the shot: {why}. Rebuild "
                    "the framing: work out the centre and size of the objects you created "
                    "and place the camera from those numbers so they fill the frame.")
                log(f"  shot {index}: rejected by vision - {why}")
        if ok and shot.is_file():
            log(f"  shot {index}: ok")
            return path
        last = msg[-1200:]
        messages.append({"role": "assistant", "content": "(previous script)"})
        messages.append({"role": "user", "content":
                         f"Your script failed. Blender said:\n\n{last}\n\nReturn the same "
                         "JSON object with scene_py fixed."})
    raise RuntimeError(f"Shot {index} never rendered. Last error: {last[-300:]}")


def _looks_empty(path) -> bool:
    """Reject a frame with no subject in it.

    Global statistics do not work here. A frame of nothing but sky over ground measured
    edge 3.62 / spread 36.4 against 4.01 / 34.4 for a shot full of steel dominoes - the
    horizon is one enormous edge and it looks exactly like detail to any average.

    What does separate them is WHERE hard edges are: an empty frame has them along a
    single horizon, a real one has them scattered over the subject. Splitting the frame
    into a 12x12 grid and counting cells containing an edge above 80 gives 0.125 for the
    empty frame against 0.23-0.63 for every good one measured.

    This is a cheap pre-filter, not a judge - it catches a frame with nothing in it, not a
    frame with the wrong thing in it. That is what the approval gate is for.
    """
    try:
        import numpy as np
        from PIL import Image, ImageFilter
    except ImportError:
        return False
    try:
        im = Image.open(path).convert("L").resize((144, 256))
    except Exception:  # noqa: BLE001
        return False
    e = np.asarray(im.filter(ImageFilter.FIND_EDGES), dtype=float)
    grid = 12
    ch, cw = e.shape[0] // grid, e.shape[1] // grid
    hits = sum(1 for r in range(grid) for c in range(grid)
               if e[r * ch:(r + 1) * ch, c * cw:(c + 1) * cw].max() > 80)
    return hits / float(grid * grid) < 0.18


def render_shot(script: Path, out_dir: Path, seconds: float, *, res=(540, 960),
                samples: int = 8, fps: int = 24, status_cb=None,
                blender: str | None = None) -> Path | None:
    """Render one authored shot to a clip. Returns the clip path, or None on failure."""
    out_dir = Path(out_dir)
    ok, msg = run_scene(script, out_dir,
                        {"res_x": res[0], "res_y": res[1], "samples": samples,
                         "fps": fps, "seconds": seconds},
                        blender=blender, status_cb=status_cb, timeout=3600)
    if not ok:
        (status_cb or print)(f"  shot failed at full length: {msg[-200:]}")
        return None
    clip = out_dir / "clip.mp4"
    return Path(clip) if frames_to_clip(out_dir, clip, fps=fps) else None
