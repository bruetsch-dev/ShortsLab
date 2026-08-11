"""Turn a plain-language prompt into a Blender scene the physics mode can render.

    "a bowling ball smashing through stacked glass panes"
        -> a complete bpy script -> one preview frame -> approve -> full render

The generated script is held to the SAME contract as the hand-written scenes, so
everything downstream (preview gate, frame streaming, sound placement, the encoder) works
on it unchanged. The contract is enforced by actually running the script rather than by
trusting the model: a scene that does not print SCENE_OK, or crashes, comes back with its
traceback for repair. Three attempts, then the run gives up with the last error.

Generated scenes are kept on disk under scenes/generated so a prompt that worked can be
re-rendered, inspected, or edited by hand later.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

import agent_core

from .blender_runner import run_scene

GENERATED = Path(__file__).resolve().parent / "scenes" / "generated"
AUTHOR_MODEL = "anthropic/claude-opus-4.8"
# The user types a few words; a cheap fast model turns them into a full shot brief before
# the expensive model writes any code. Asking the code writer to also invent the staging
# from three words gets both jobs done badly.
BRIEF_MODEL = "google/gemini-3.5-flash-lite"
MAX_ATTEMPTS = 3

BRIEF_PROMPT = """You turn a creator's rough note into a precise brief for a vertical
9:16 physics animation. You are NOT writing code and NOT describing a camera app - this
becomes a real 3D simulation, so only describe things that can be built from simple
solids: boxes, spheres, cylinders, planes, and stacks of them.

Fill in everything the note leaves open, and choose well:
  - the objects, their rough sizes and materials
  - what moves, what it hits, and what happens as a result
  - whether it should LOOP seamlessly (satisfying, hypnotic, repeating) or play once
  - how long one take should be (3-6 seconds for a single action, 2-4 for a loop)
  - the camera: fixed, tracking, or slowly pushing in
  - lighting and colour, assuming a dark background
  - what the viewer is led to expect, and whether that expectation is met or broken

Do not add narration, text overlays, characters or logos. Keep it to one paragraph of
concrete physical description plus the choices above.

Return JSON: {"brief": "<the full description>", "title": "<short title>",
"loop": true|false, "seconds": <number>}"""


def expand_prompt(rough: str, status_cb=None, model: str = BRIEF_MODEL) -> dict:
    """Rough user note -> full shot brief. Falls back to the raw note if the model fails."""
    log = status_cb or print
    log("Turning your note into a shot brief...")
    try:
        out = agent_core._post_llm_json(
            model,
            [{"role": "system", "content": BRIEF_PROMPT},
             {"role": "user", "content": str(rough or "").strip()}],
            max_tokens=1400, temperature=0.5, timeout=180) or {}
    except Exception as exc:  # noqa: BLE001
        log(f"Brief step failed ({exc}); using your text as written.")
        return {"brief": str(rough or ""), "title": "", "loop": False, "seconds": 4.0}
    brief = str(out.get("brief") or "").strip() or str(rough or "")
    log(f"Brief: {brief[:220]}")
    return {"brief": brief, "title": str(out.get("title") or ""),
            "loop": bool(out.get("loop")), "seconds": float(out.get("seconds") or 4.0)}

# Everything the model cannot discover on its own: our runner's contract, and the Blender
# 5.2 API changes that every one of the hand-written scenes tripped over first.
CONTRACT = """You write ONE complete Python script that runs inside Blender 5.2 headless
(`blender -b -noaudio -P scene.py -- '<json>'`). It uses only `bpy`, `math`, `json`, `sys`
and `random`. No external assets, no downloads, no add-ons.

HARD REQUIREMENTS - the script is run and rejected if any is missing:

1. Read parameters from argv after "--" as a JSON object:
       argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
       P = json.loads(argv[0]) if argv else {}
   Honour: out_dir, res_x (1080), res_y (1920), samples (24), fps (30), seconds (4.0),
   preview_frame (0).

2. Render engine CYCLES, and enable the GPU exactly like this:
       prefs = bpy.context.preferences.addons["cycles"].preferences
       for backend in ("OPTIX", "CUDA"):
           try:
               prefs.compute_device_type = backend
               prefs.refresh_devices()
           except Exception:
               continue
           if [d for d in prefs.devices if d.type == backend]:
               for d in prefs.devices:
                   d.use = d.type in (backend, "CPU")
               bpy.context.scene.cycles.device = "GPU"
               break

3. If preview_frame > 0: `sc.frame_set(preview_frame)`, render ONE still to
   `{out_dir}/preview.png`, print `SCENE_OK preview=<n>` and return WITHOUT rendering the
   animation. Otherwise render the animation to filepath `{out_dir}/frame_` as PNG.

4. Write `{out_dir}/impacts.json`:
       {"fps": <fps>, "events": [{"frame": <int>, "kind": "impact"|"land"|"hole",
                                  "strength": <0..1>}]}
   one entry per moment the app should place a sound on. Derive the frames from the
   actual motion, never from a guess - the sound is placed on the frame you name.

5. Print a line starting with `SCENE_OK` as the very last thing on success.

BLENDER 5.2 - these WILL break the script if ignored:
  * `image_settings.file_format = "FFMPEG"` no longer exists. Render PNG frames only.
  * The Cell Fracture add-on is gone. Build breakable objects as a GRID of separate
    rigid bodies instead of fracturing one mesh.
  * `Action.fcurves` is gone (slotted actions). To control interpolation, set
    `bpy.context.preferences.edit.keyframe_new_interpolation_type = "LINEAR"` BEFORE
    inserting keys.
  * `Mesh.use_auto_smooth` is gone. Do not touch it.
  * Rigid bodies need `bpy.ops.rigidbody.object_add(type=...)`; a passive floor plane is
    required or everything falls forever.

CRAFT RULES - this is a 9:16 short, judged on how it looks:
  * Frame vertically. Blender fits the sensor to the LARGER resolution axis, so in 9:16
    the visible HEIGHT is `36 / lens * distance`. Solve the camera distance from that so
    the subject fills roughly 40-55% of the frame height. Do not guess a distance.
  * Dark background (world colour near 0.02) with a lit subject reads best in a feed.
  * LIGHTS MUST NOT BE VISIBLE. An area light is geometry to Cycles and renders as a
    glowing rectangle floating in the shot. Set `light_object.visible_camera = False` on
    every light you add, every time.
  * Nothing may float. Every object either rests on the floor, is stacked on something,
    or is visibly held by the motion you animate. A slab hanging in mid air reads as a
    bug, not as style.
  * Angle the camera slightly DOWN from just above the subject, not straight overhead: a
    top-down shot flattens the objects and the collision stops reading as a collision.
  * The subject must be the brightest thing in frame. If the floor or the background is
    brighter than what the viewer is meant to watch, lower their brightness.
  * If the prompt asks for something SATISFYING or LOOPING: make the motion periodic and
    the camera move at constant speed, so the last frame continues into the first. Say so
    in "kind": "loop" and keep it short - the app repeats it.
  * Stacks of blocks need a visible seam between them (build each ~0.93 of its slot) or
    they render as one smooth slab and the destruction reads as a glitch.
  * A rigid-body stack must start asleep (`use_start_deactivated = True`) or it jitters
    itself apart before anything hits it.

Return ONLY a JSON object:
  "title":    short human title for the video
  "scene_py": the complete script as one string
  "seconds":  how long one render should be
  "kind":     "single" or "loop"
  "sound":    one short phrase naming the material being struck (e.g. "wood", "glass")
"""


def frame_looks_empty(path) -> bool:
    """True if a rendered frame shows essentially nothing.

    SCENE_OK plus a file on disk is not proof the shot works: a scene whose camera points
    away from its own geometry renders happily and produces a flat grey rectangle.

    Edge energy alone is not enough. A deliberately dark, low-contrast shot measured 3.2
    while genuinely showing a full spiral of steel dominoes, so a single edge threshold
    threw away a good scene three times in a row. Contrast is the second opinion: the
    empty frame had edge 1.2 AND spread 12.6, the dark-but-real one edge 3.2 with spread
    34.5, and brightly lit scenes sit near edge 10 / spread 50.
    """
    try:
        from PIL import Image, ImageFilter, ImageStat
    except ImportError:
        return False
    try:
        im = Image.open(path).convert("L").resize((180, 320))
    except Exception:  # noqa: BLE001
        return False
    edge = ImageStat.Stat(im.filter(ImageFilter.FIND_EDGES)).mean[0]
    spread = ImageStat.Stat(im).stddev[0]
    return edge < 2.0 or spread < 16.0


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", str(text or "scene").lower()).strip("_")
    return (s[:40] or "scene") + "_" + time.strftime("%Y%m%d_%H%M%S")


def author_scene(prompt: str, status_cb=None, *, blender: str | None = None,
                 model: str = AUTHOR_MODEL, brief_model: str = BRIEF_MODEL,
                 work_dir: str | Path | None = None) -> dict:
    """Write, run and repair a scene for `prompt`. Returns its metadata plus the path.

    The returned dict carries `scene_path`, `preview` (a rendered still) and `kind`, which
    is everything `run.build` needs to treat it like a built-in preset.
    """
    log = status_cb or print
    agent_core.assert_wavespeed_balance(status_cb=status_cb)
    GENERATED.mkdir(parents=True, exist_ok=True)
    probe = Path(work_dir or GENERATED) / "authoring"
    probe.mkdir(parents=True, exist_ok=True)

    brief = expand_prompt(prompt, status_cb=status_cb, model=brief_model)
    ask = brief["brief"]
    if brief.get("loop"):
        ask += ('\n\nThis must LOOP seamlessly: periodic motion, constant camera speed, '
                'last frame continuing into the first. Set kind to "loop".')
    ask += f"\n\nTarget length for one take: {brief['seconds']:.1f} seconds."
    messages = [
        {"role": "system", "content": CONTRACT},
        {"role": "user", "content": f"Build this as a vertical physics short:\n\n{ask}"},
    ]
    last_error = ""
    meta: dict = {}
    for attempt in range(1, MAX_ATTEMPTS + 1):
        log(f"Writing the Blender scene (attempt {attempt}/{MAX_ATTEMPTS})...")
        meta = agent_core._post_llm_json(model, messages, max_tokens=9000,
                                         temperature=0.25, timeout=420) or {}
        code = str(meta.get("scene_py") or "")
        if not code.strip():
            last_error = "the model returned no script"
            messages.append({"role": "user", "content":
                             "You returned no scene_py. Return the JSON object with the "
                             "complete script in scene_py."})
            continue
        path = GENERATED / f"{_slug(meta.get('title') or prompt)}.py"
        path.write_text(code, encoding="utf-8")
        log(f"Testing the scene with one frame ({path.name})...")
        ok, msg = run_scene(path, probe,
                            {"res_x": 540, "res_y": 960, "samples": 16,
                             "seconds": float(meta.get("seconds") or 4.0),
                             # mid-action, not the opening frame: an authored scene
                             # often starts on a static setup that shows nothing
                             "preview_frame": max(4, int(float(
                                 meta.get("seconds") or 4.0) * 30 * 0.55))},
                            blender=blender, timeout=900)
        shot = probe / "preview.png"
        if ok and shot.is_file() and frame_looks_empty(shot):
            ok, msg = False, (
                "The script ran but rendered an EMPTY frame - nothing but background. "
                "The camera is not looking at your geometry. Compute the camera position "
                "from the actual bounds of the objects you created (their centre and "
                "size), point it at that centre, and solve the distance from "
                "36 / lens * distance = visible height. Do not hard-code a position.")
        if ok and shot.is_file():
            meta["scene_path"] = str(path)
            meta["preview"] = str(shot)
            meta["kind"] = "loop" if str(meta.get("kind")) == "loop" else "single"
            meta["seconds"] = float(meta.get("seconds") or brief["seconds"] or 4.0)
            meta["brief"] = brief["brief"]
            meta["user_prompt"] = str(prompt or "")
            log(f"Scene works: {meta.get('title') or path.stem}")
            return meta
        last_error = msg[-1800:]
        log(f"Scene failed - handing the traceback back for repair.")
        # Feed back the model's own script plus what Blender said about it. Repairing its
        # own text works far better than asking for a fresh attempt from the prompt.
        messages.append({"role": "assistant", "content": json.dumps(
            {k: v for k, v in meta.items() if k != "scene_py"})[:600]})
        messages.append({"role": "user", "content":
                         "Your script failed to run. Blender said:\n\n"
                         f"{last_error}\n\nReturn the SAME JSON object with scene_py "
                         "fixed. Keep everything that worked."})
    raise RuntimeError(f"Could not get a working scene after {MAX_ATTEMPTS} attempts. "
                       f"Last error: {last_error[-500:]}")
