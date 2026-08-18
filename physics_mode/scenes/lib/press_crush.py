"""press_crush - a massive flat press descends at constant speed and crushes a pile.

    blender -b -noaudio -P press_crush.py -- '{"material":"concrete","shape":"blocks",
                                               "count":280,"out_dir":"..."}'

This is a HAND-MAINTAINED parametric scene: a model picks the scene and fills in PARAMS,
it never writes geometry. Every number below is either measured, solved at build time, or
explained where a reader would otherwise have to reverse-engineer it.

Why it is built this way
------------------------
* The press is a KINEMATIC rigid body on LINEAR keyframes. Constant speed is the entire
  appeal - a press that eases in reads as an animation, a press that never slows reads as
  a machine. Kinematic also means the pile cannot push back, which is what "hydraulic"
  amounts to in a solver: infinite mass, velocity taken straight from the keys.
* The pile is a stack of separate rigid bodies that START ASLEEP. Blender 5.2 has no Cell
  Fracture add-on, so breakables are grids of bodies; and a loose pile that is awake at
  frame 1 spends the whole approach shuffling itself flat before the press ever arrives.
  Asleep it is dead still, and the wake propagates DOWN through the contacts as the press
  travels - which is exactly the cascade the shot is about.
* "Crush to the floor" stops one third of a piece short of it. A kinematic press ordered
  to z=0 has to put the debris somewhere and Bullet's answer is to fire it through the
  floor. Leaving a sliver of clearance makes it squirt out sideways instead, which is the
  shot anyway.
* The camera distance is SOLVED, not guessed. Blender fits the sensor to the LARGER
  resolution axis, so in 9:16 the visible HEIGHT is 36/lens*distance and the visible width
  is that times 9/16 - which means the WIDTH is usually the binding constraint here, not
  the height. Rather than juggle two closed forms, solve_camera() projects the corners of
  everything that matters through the real camera and backs off until the worst of them
  sits inside `frame_fill` of the frame.
* Three ram columns are parented to the press and run far enough up to leave the frame.
  The band above the slab would otherwise go black as the press descends; the rams grow
  into it instead, so no third of the frame is ever empty and dark. The guide posts and
  the lit backdrop do the same job for the corners.

Impact frames come out of the STEPPED simulation - first contact, the crunch bursts while
the press travels, and the bottom-out - and go to impacts.json, so the app puts each sound
on the frame the picture actually shows it on.

Measured on the default parameters (offline projection of the solved camera): 234 pieces,
a 1.56 x 0.78 x 2.96m pile filling 52% of the frame height, the press slab's top edge at
94% of it, every fit point inside the frame, and the camera solve settling in 3 passes.
"""

import json
import math
import random
import sys

import bpy
from mathutils import Matrix, Vector

# --------------------------------------------------------------------------- parameters
# A later model reads this to fill values and cannot see the scene, so every note has to
# stand on its own. "choices" for enums, "range": [lo, hi] for numbers - numbers are
# CLAMPED to their range at read time, because a value outside it is how a scene ends up
# rendering an empty frame for twenty minutes.
PARAMS = {
    # ---- what is being crushed -------------------------------------------------------
    "shape": {
        "choices": ["blocks", "spheres", "cylinders", "mixed"],
        "default": "blocks",
        "note": "What the pile is made of. blocks stack solidly and crumple layer by "
                "layer; spheres squirt out sideways the moment they are touched and are "
                "the most chaotic; cylinders stand on end and buckle; mixed picks per "
                "piece and looks like scrap.",
    },
    "material": {
        "choices": ["glass", "brick", "concrete", "wood", "steel", "plastic"],
        "default": "concrete",
        "note": "Look AND physics of the pieces - colour, roughness, density, friction "
                "and bounce all come from this. concrete and brick grind and stay put, "
                "wood is light and skittery, steel is heavy and shiny, plastic bounces, "
                "glass is see-through and by far the slowest to render (transmission on "
                "hundreds of pieces) - budget roughly double the frame time for it.",
    },
    "count": {
        "range": [200, 720],
        "default": 280,
        "note": "How many pieces are in the pile. Approximate: the pile is a grid, so the "
                "real count is the nearest grid that also keeps the pile a sensible shape "
                "for a vertical frame, and it never drops below 200 because a small "
                "target looks cheap however correct the simulation is. If this and "
                "piece_size cannot be reconciled with pile_height, the HEIGHT gives. The "
                "real numbers are printed.",
    },
    "piece_size": {
        "range": [0.16, 0.45],
        "default": 0.26,
        "note": "Size of one piece in metres. Smaller pieces mean more of them and a "
                "finer-grained crush; larger pieces read as chunks and each one is "
                "individually followable. This drives everything else about the pile.",
    },
    "pile_height": {
        "range": [1.6, 4.0],
        "default": 2.9,
        "note": "How tall the pile stands, in metres. This is the length of the crush "
                "stroke and therefore how long the satisfying part lasts. Tall piles fill "
                "more of the vertical frame; the camera is solved from it either way. "
                "Treated as a preference, not a promise: many large pieces cannot be "
                "stacked into a short pile, and the height is what gets stretched.",
    },
    "seed": {
        "range": [0, 9999],
        "default": 7,
        "note": "Random seed for the per-piece jitter, rotation and colour variation. "
                "Change it for a different-looking pile with identical dimensions.",
    },

    # ---- the press ------------------------------------------------------------------
    "press_speed": {
        "range": [0.15, 3.0],
        "default": 0.65,
        "note": "Descent speed in metres per second, held exactly constant - the slow "
                "inevitability is the appeal, so low values are usually better. The "
                "stroke is roughly pile_height + approach_gap, so the descent takes about "
                "that many metres divided by this; below ~0.45 m/s a tall pile needs "
                "`seconds` raised to match, or fit_stroke will raise the speed instead.",
    },
    "crush_to": {
        "choices": ["floor", "height"],
        "default": "floor",
        "note": "Whether the press keeps going until it is (almost) on the floor, "
                "flattening everything, or halts at `stop_height` with the pile still "
                "part-crushed underneath it. 'floor' is the more final-feeling ending.",
    },
    "stop_height": {
        "range": [0.0, 2.0],
        "default": 0.8,
        "note": "Height above the floor, in metres, where the press face halts - only "
                "used when crush_to is 'height'. Clamped to leave a real stroke: it can "
                "never be set so high that the press barely touches the pile.",
    },
    "approach_gap": {
        "range": [0.2, 2.5],
        "default": 0.9,
        "note": "Metres of clear air between the press face and the top of the pile at "
                "frame 1, so the press is visibly travelling before it touches anything. "
                "Divided by press_speed this is the length of the anticipation. Large "
                "values push the camera back and shrink the pile in frame.",
    },
    "press_overhang": {
        "range": [1.0, 1.8],
        "default": 1.18,
        "note": "How much wider the press slab is than the pile footprint. Above 1.0 the "
                "slab visibly overhangs and pieces get squeezed out from under its edges, "
                "which is where most of the flying debris comes from.",
    },
    "press_thickness": {
        "range": [0.20, 1.00],
        "default": 0.45,
        "note": "Thickness of the press slab in metres. Thick reads as heavy; it also "
                "costs vertical frame space, since the whole slab has to stay in shot.",
    },
    "fit_stroke": {
        "choices": [True, False],
        "default": True,
        "note": "If the stroke would not finish inside `seconds`, raise the speed just "
                "enough that it does (and print the real speed). Turn this off only if "
                "an unfinished, still-descending ending is deliberate - otherwise the "
                "render pays full price for a video with no payoff.",
    },
    "settle_seconds": {
        "range": [0.0, 2.0],
        "default": 0.7,
        "note": "Tail held after the press reaches its stop, for the debris to finish "
                "clattering. Reserved out of the clip length when the stroke is fitted.",
    },

    # ---- staging and look -------------------------------------------------------------
    "show_frame": {
        "choices": [True, False],
        "default": True,
        "note": "Two guide posts either side of the press. They make it read as a machine "
                "rather than a floating slab, they fill the top of the frame, and debris "
                "clatters off them back into shot instead of leaving it.",
    },
    "motion_blur": {
        "choices": [True, False],
        "default": False,
        "note": "Motion blur on the flying debris. Looks better on a phone and costs "
                "roughly a third more render time; off by default so the cost of a run "
                "stays predictable.",
    },
    "frame_fill": {
        "range": [0.70, 0.98],
        "default": 0.92,
        "note": "Fraction of the frame that the press, the pile, the guide posts and the "
                "landing floor are allowed to span. 0.92 leaves a thin safety margin. "
                "Lower it to pull back and give debris more room to fly.",
    },
    "lens": {
        "range": [28.0, 70.0],
        "default": 42.0,
        "note": "Camera focal length in mm. Short lenses exaggerate the press coming "
                "down at the viewer; long lenses flatten it and make the pile read as a "
                "wall. The distance is re-solved for whatever is chosen.",
    },
    "pitch_deg": {
        "range": [4.0, 28.0],
        "default": 12.0,
        "note": "How far the camera looks DOWN, in degrees. A slight angle shows the top "
                "faces of the pile and the floor where debris lands; overhead flattens "
                "the collision and is why this is capped well below top-down.",
    },
    "seconds": {
        "range": [3.0, 14.0],
        "default": 7.0,
        "note": "Length of the clip. The whole stroke plus settle_seconds has to fit in "
                "here - see fit_stroke. 7s is what the default press speed and pile "
                "height need; halving it does not make a shorter video of the same shot, "
                "it makes the press twice as fast.",
    },

    # ---- supplied by the runner: leave unset -----------------------------------------
    "out_dir": {"default": "//out",
                "note": "Supplied by the runner - leave unset. Where frames, preview.png "
                        "and impacts.json are written."},
    "res_x": {"default": 1080, "note": "Supplied by the runner - leave unset."},
    "res_y": {"default": 1920, "note": "Supplied by the runner - leave unset."},
    "samples": {"default": 24, "note": "Supplied by the runner - leave unset. Cycles "
                                       "samples per pixel, denoised afterwards."},
    "fps": {"default": 30, "note": "Supplied by the runner - leave unset."},
    "preview_frame": {"default": 0,
                      "note": "Supplied by the runner - leave unset. >0 renders that one "
                             "frame to preview.png for the approval gate and stops."},
}

argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
P = json.loads(argv[0]) if argv else {}


def _num(key):
    """A number from P, clamped to the range PARAMS declares for it."""
    spec = PARAMS[key]
    val = P.get(key, spec["default"])
    try:
        val = float(val)
    except (TypeError, ValueError):
        val = float(spec["default"])
    lo, hi = spec.get("range", (None, None))
    if lo is not None:
        val = max(float(lo), min(float(hi), val))
    return val


def _int(key):
    return int(round(_num(key)))


def _enum(key):
    """A choice from P, falling back to the default if the model invented a value."""
    spec = PARAMS[key]
    val = P.get(key, spec["default"])
    return val if val in spec["choices"] else spec["default"]


def _bool(key):
    val = P.get(key, PARAMS[key]["default"])
    if isinstance(val, str):
        return val.strip().lower() in ("1", "true", "yes", "on")
    return bool(val)


OUT = P.get("out_dir", PARAMS["out_dir"]["default"])
RES_X, RES_Y = _int("res_x"), _int("res_y")
SAMPLES = _int("samples")
FPS = max(1, _int("fps"))
SECONDS = _num("seconds")
try:                                    # "74", 74.0 and 74 all mean frame 74
    PREVIEW = max(0, int(float(P.get("preview_frame", 0) or 0)))
except (TypeError, ValueError):
    PREVIEW = 0

SHAPE = _enum("shape")
MATERIAL = _enum("material")
COUNT = _int("count")
PIECE = _num("piece_size")
PILE_H = _num("pile_height")
SEED = _int("seed")

PRESS_SPEED = _num("press_speed")
CRUSH_TO = _enum("crush_to")
STOP_HEIGHT = _num("stop_height")
APPROACH = _num("approach_gap")
OVERHANG = _num("press_overhang")
THICK = _num("press_thickness")
FIT_STROKE = _bool("fit_stroke")
SETTLE = _num("settle_seconds")

SHOW_FRAME = _bool("show_frame")
MOTION_BLUR = _bool("motion_blur")
FILL = _num("frame_fill")
LENS = _num("lens")
PITCH = _num("pitch_deg")

# Piece geometry is the same box for every shape, so one vertical pitch stacks them all.
# The body is 0.86 of its grid cell: that leaves a real seam sideways between neighbours
# (a flush grid renders as one smooth slab and the destruction reads as a glitch), while
# VERTICALLY the pitch is the body height plus 2%, so pieces are touching and nothing
# drops when the stack wakes up.
BODY = PIECE * 0.86
PITCH_Z = BODY * 1.02
# How far a piece may be nudged and turned inside its cell. SOLVED, not picked: a square
# of side BODY turned by `a` spans BODY*(cos a + sin a) = BODY*sqrt(2)*sin(a + 45deg), and
# the two neighbours can both have jittered towards each other, so the space really
# available is PIECE - 2*JIT. A piece that starts overlapping its neighbour looks fine
# while the stack is asleep and then kicks itself free the instant the press wakes it -
# the pile appears to explode a moment before anything touches it.
JIT = PIECE * 0.03
_SLACK = (PIECE - 2.0 * JIT) / BODY
YAW = max(0.0, min(0.12, math.asin(max(-1.0, min(1.0, _SLACK / math.sqrt(2.0))))
                   - math.pi / 4.0))

#                      base colour        rough  metal  transmit  density  fric  restit
MATERIALS = {
    "glass":    ((0.55, 0.80, 0.86, 1.0), 0.06,  0.0,   0.85,     2500,    0.30, 0.18),
    "brick":    ((0.52, 0.21, 0.14, 1.0), 0.85,  0.0,   0.0,      1900,    0.95, 0.02),
    "concrete": ((0.52, 0.52, 0.50, 1.0), 0.88,  0.0,   0.0,      2400,    0.98, 0.01),
    "wood":     ((0.60, 0.38, 0.17, 1.0), 0.70,  0.0,   0.0,       650,    0.72, 0.16),
    "steel":    ((0.60, 0.62, 0.68, 1.0), 0.22,  1.0,   0.0,      7800,    0.45, 0.28),
    "plastic":  ((0.10, 0.52, 0.92, 1.0), 0.34,  0.0,   0.0,      1000,    0.55, 0.42),
}
SHAPES = {"blocks": "block", "spheres": "sphere", "cylinders": "cylinder"}


# ------------------------------------------------------------------------------ helpers


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def use_gpu(sc) -> str:
    """Point Cycles at the GPU. OptiX first (RTX cards have the ray-tracing cores), then
    CUDA. On CPU a 1080x1920 frame of this scene is minutes rather than seconds, which is
    not a mode anyone would wait for."""
    try:
        prefs = bpy.context.preferences.addons["cycles"].preferences
    except KeyError:
        return "CPU (cycles preferences unavailable)"
    for backend in ("OPTIX", "CUDA", "HIP", "ONEAPI"):
        try:
            prefs.compute_device_type = backend
            prefs.refresh_devices()
        except Exception:  # noqa: BLE001 - backend not compiled into this build
            continue
        found = [d for d in prefs.devices if d.type == backend]
        if not found:
            continue
        for d in prefs.devices:
            d.use = d.type in (backend, "CPU")
        sc.cycles.device = "GPU"
        return f"{backend}: " + ", ".join(d.name for d in found)
    return "CPU (no GPU backend)"


def mat(name, rgba, rough=0.6, metal=0.0, transmit=0.0):
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    b = m.node_tree.nodes["Principled BSDF"]
    b.inputs["Base Color"].default_value = rgba
    b.inputs["Roughness"].default_value = rough
    b.inputs["Metallic"].default_value = metal
    if transmit:
        # Renamed between Blender generations ("Transmission" -> "Transmission Weight"),
        # and a KeyError here would take the whole scene down over a look detail.
        for key in ("Transmission Weight", "Transmission"):
            try:
                b.inputs[key].default_value = transmit
                break
            except KeyError:
                continue
        try:
            b.inputs["IOR"].default_value = 1.45
        except KeyError:
            pass
    return m


def area_light(name, loc, target, energy, size, color=(1, 1, 1), size_y=None):
    """An area light aimed at `target`, hidden from camera rays.

    Lights are geometry to Cycles: a multi-metre emissive rectangle parked in front of the
    press renders as a white slab across the frame unless visible_camera is turned off.
    """
    bpy.ops.object.light_add(type="AREA", location=loc)
    lt = bpy.context.object
    lt.name = name
    lt.data.energy = energy
    if size_y is not None:
        lt.data.shape = "RECTANGLE"
        lt.data.size = size
        lt.data.size_y = size_y
    else:
        lt.data.size = size
    lt.data.color = color
    lt.visible_camera = False
    if target is not None:
        d = Vector(target) - lt.location
        lt.rotation_euler = d.to_track_quat("-Z", "Y").to_euler()
    return lt


def solve_camera(sc, points, lens, fill, pitch_deg):
    """Back the camera off until every point in `points` sits inside `fill` of the frame.

    Closed-form framing works for one axis. Here the pile, the press at its start height,
    the guide posts and the landing floor all have to fit at once, in 9:16, through a
    tilted camera - and in that combination it is usually the WIDTH that runs out first,
    not the height. So project the real corners through the real camera and iterate:

      worst = 2 * max|ndc - 0.5|      (1.0 means a point is exactly on the frame edge)
      distance *= worst / fill        (off-axis extent falls off as 1/distance)

    which converges in a handful of steps. The aim height is re-centred each pass from the
    projected extent, so the content ends up vertically centred instead of only guaranteed
    to be inside. view_layer.update() is mandatory before every projection: without it
    world_to_camera_view reads the camera's stale matrix_world and the solve chases a
    camera that is not where the loop just put it.
    """
    from bpy_extras.object_utils import world_to_camera_view

    xs = [p[0] for p in points]
    zs = [p[2] for p in points]
    # A first guess only. The width requirement is turned into a height requirement with
    # the render's own aspect, because in 9:16 the sensor covers the height.
    aspect = RES_Y / max(1, RES_X)
    span = max(max(zs) - min(zs), (max(xs) - min(xs)) * aspect)
    aim_z = (max(zs) + min(zs)) / 2.0
    dist = max(1.0, (span / fill) * lens / 36.0)
    # The aim height may not wander outside the content by more than its own span. Without
    # a bound a bad step feeds itself: raising the camera pushes the content lower, which
    # asks for the camera to be raised again.
    lo_z, hi_z = min(zs) - span, max(zs) + span

    bpy.ops.object.camera_add(location=(0, 0, 0))
    cam = bpy.context.object
    cam.name = "Cam"
    cam.data.lens = lens
    sc.camera = cam
    pitch = math.radians(pitch_deg)

    def place():
        # Camera on the -Y side, pulled back and lifted so it looks DOWN by `pitch`.
        # rotation (90deg - pitch, 0, 0) turns the default -Z view direction into
        # (0, cos pitch, -sin pitch), which is exactly the direction back to the aim.
        cam.location = (0.0, -dist * math.cos(pitch), aim_z + dist * math.sin(pitch))
        cam.rotation_euler = (math.radians(90.0) - pitch, 0.0, 0.0)
        bpy.context.view_layer.update()
        bad, lo_y, hi_y, behind = 0.0, 1e9, -1e9, False
        for p in points:
            co = world_to_camera_view(sc, cam, Vector(p))
            if co.z <= 0.0:            # behind the camera: far too close
                behind = True
                continue
            bad = max(bad, abs(co.x - 0.5) * 2.0, abs(co.y - 0.5) * 2.0)
            lo_y, hi_y = min(lo_y, co.y), max(hi_y, co.y)
        return bad, (lo_y + hi_y) / 2.0, behind

    worst = 1.0
    for _ in range(60):
        worst, mid_y, behind = place()
        if behind:
            dist *= 1.8
            continue
        if abs(worst - fill) < 0.005 and abs(mid_y - 0.5) < 0.005:
            break
        # Frame height in world units at the aim plane: the 36mm sensor maps to the LARGER
        # resolution axis, which in 9:16 is the height.
        #
        # MIND THE SIGN. The camera's rotation is fixed and it is POSITIONED relative to
        # the aim, so raising the aim raises the camera and pushes the content DOWN the
        # frame. Correcting towards the centre therefore moves the aim the same way the
        # content already is, not the opposite way; the intuitive `(0.5 - mid_y)` is a
        # positive feedback loop that walks the camera out to 1e12 metres in 60 steps.
        aim_z = clamp(aim_z + (mid_y - 0.5) * (36.0 / lens * dist) * 0.6, lo_z, hi_z)
        dist = clamp(dist * clamp(worst / fill, 0.6, 2.0), 0.5, 400.0)
    # Commit the last aim/distance the loop produced. Breaking out of the loop without
    # this leaves the camera standing at the second-to-last pose it was tested in.
    worst, _mid, _behind = place()
    if worst > fill * 1.02:
        # Did not settle inside the budget: give up the tight fit rather than the frame.
        dist = clamp(dist * (worst / fill) * 1.02, 0.5, 400.0)
        worst, _mid, _behind = place()
    return cam, dist, aim_z, worst


def add_rigid(objs, body_type):
    """Give every object in `objs` a rigid body.

    The plural operator does the whole selection in one scene update; the per-object
    fallback is there because it is not worth losing a render if the operator is missing.
    """
    if not objs:
        return
    try:
        bpy.ops.object.select_all(action="DESELECT")
        for o in objs:
            o.select_set(True)
        bpy.context.view_layer.objects.active = objs[0]
        bpy.ops.rigidbody.objects_add(type=body_type)
        bpy.ops.object.select_all(action="DESELECT")
        return
    except Exception:  # noqa: BLE001 - operator unavailable in this build
        pass
    for o in objs:
        if o.rigid_body:               # the bulk call may have got part of the way
            continue
        bpy.context.view_layer.objects.active = o
        bpy.ops.rigidbody.object_add(type=body_type)


def peaks(series, min_gap, floor_frac):
    """Local maxima of a (frame, value) series - the crunch bursts.

    Taken from the recorded motion rather than from a fixed threshold, so a gentle press on
    a light pile still names its moments instead of going silent.
    """
    if not series:
        return []
    top = max(v for _, v in series) or 1
    vals = {f: v for f, v in series}
    picked = []
    for i, (f, v) in enumerate(series):
        if v < floor_frac * top:
            continue
        window = [vals.get(f + d, -1) for d in (-2, -1, 1, 2)]
        if v < max(window):
            continue
        if picked and f - picked[-1][0] < min_gap:
            if v > picked[-1][1]:
                picked[-1] = (f, v)
            continue
        picked.append((f, v))
    return [(f, v / top) for f, v in picked]


# --------------------------------------------------------------------------------- build


def main():
    rng = random.Random(SEED)
    bpy.ops.wm.read_factory_settings(use_empty=True)
    sc = bpy.context.scene
    sc.render.engine = "CYCLES"          # headless EEVEE has no GPU context and crawls
    sc.cycles.samples = SAMPLES
    sc.cycles.use_denoising = True
    # Glass and polished steel throw fireflies that 24 samples cannot resolve; killing
    # caustics and blurring sharp glossy bounces is what makes a low-sample frame clean.
    sc.cycles.caustics_reflective = False
    sc.cycles.caustics_refractive = False
    sc.cycles.blur_glossy = 1.0
    print("CYCLES_DEVICE " + use_gpu(sc))
    sc.render.resolution_x, sc.render.resolution_y = RES_X, RES_Y
    sc.render.fps = FPS
    sc.frame_start = 1
    sc.frame_end = max(2, int(round(SECONDS * FPS)))
    if MOTION_BLUR:
        sc.render.use_motion_blur = True
        try:
            sc.render.motion_blur_shutter = 0.4
        except AttributeError:
            pass
    try:
        sc.view_settings.look = "AgX - Medium High Contrast"
    except Exception:  # noqa: BLE001 - look names differ between builds
        pass

    # LINEAR keys, set BEFORE anything is inserted. The press must travel at a constant
    # speed and bezier handles would ease it in and out; Blender 5.2 moved Action.fcurves
    # behind slotted actions, so there is no fixing the curves afterwards.
    bpy.context.preferences.edit.keyframe_new_interpolation_type = "LINEAR"

    sc.world = bpy.data.worlds.new("W")
    sc.world.use_nodes = True
    sc.world.node_tree.nodes["Background"].inputs[0].default_value = (0.008, 0.009, 0.012, 1)

    try:
        bpy.ops.rigidbody.world_add()
    except Exception:  # noqa: BLE001 - already present
        pass
    rw = sc.rigidbody_world
    rw.substeps_per_frame = 14
    rw.solver_iterations = 20
    if rw.point_cache:
        rw.point_cache.frame_start = sc.frame_start
        rw.point_cache.frame_end = sc.frame_end

    # ---- pile dimensions ------------------------------------------------------------
    # count, piece_size and pile_height can contradict each other - 200 pieces of 0.42m is
    # 15 cubic metres, and no arrangement of that is 1.6m tall. Taking the height as fixed
    # and widening to reach the count produced a 4.2m x 1.5m slab filling 13% of the frame
    # height: a correct simulation of nothing worth watching. So all three grid dimensions
    # are searched together and scored, and the HEIGHT is what gives when they conflict.
    #
    #   - hit the requested count                                    (weight 1.0)
    #   - stay near the requested height                             (weight 1.2)
    #   - never wider than 0.85x its own height, never narrower than
    #     0.38x - the first wastes a vertical frame, the second is
    #     a needle that topples instead of being crushed             (weight 2.0)
    #
    # ny comes out at ~0.55 nx: depth is nearly free in 9:16, since it costs no frame
    # width, but it is also barely seen, so pieces spent on it are pieces half wasted.
    best = None
    for lay in range(4, 41):
        top = lay * PITCH_Z
        for cols in range(3, 17):
            rows = max(2, int(cols * 0.55 + 0.5))
            tot = cols * rows * lay
            # Below 200 pieces a target looks cheap however correct the simulation is.
            if tot < 200:
                continue
            wide = (cols * PIECE) / top
            score = (abs(tot - COUNT) / max(1.0, float(COUNT))
                     + 1.2 * abs(top - PILE_H) / max(0.5, PILE_H)
                     + 2.0 * max(0.0, wide - 0.85)
                     + 2.0 * max(0.0, 0.38 - wide))
            if best is None or score < best[0]:
                best = (score, cols, rows, lay, tot)
    _score, nx, ny, layers, total = best
    pile_w, pile_d = nx * PIECE, ny * PIECE
    pile_top = layers * PITCH_Z

    press_w = pile_w * OVERHANG
    press_d = max(pile_d * OVERHANG, press_w * 0.55)   # a slab, not a plank
    post_thick = 0.14
    post_gap = 0.26                                     # clear air beside the press edge
    post_outer = press_w / 2.0 + post_gap + post_thick

    press_face_start = pile_top + APPROACH
    # A kinematic press told to reach z=0 has to put a piece's worth of debris somewhere,
    # and Bullet's answer is through the floor. A third of a piece of clearance makes it
    # squirt out sideways instead.
    floor_clear = max(0.06, BODY * 0.33)
    if CRUSH_TO == "floor":
        stop_z = floor_clear
    else:
        # Clamped so there is always a real stroke - a stop height above the pile would
        # render a press that never touches anything.
        stop_z = clamp(STOP_HEIGHT, floor_clear, pile_top * 0.85)

    # ---- timing ----------------------------------------------------------------------
    stroke = max(0.05, press_face_start - stop_z)
    speed = PRESS_SPEED
    settle_frames = int(round(SETTLE * FPS))
    avail = max(2, sc.frame_end - settle_frames - 1)
    need = stroke / speed * FPS
    fitted = False
    if need > avail and FIT_STROKE:
        speed = stroke / (avail / FPS)
        need = avail
        fitted = True
    # Deliberately NOT clamped to frame_end. With fit_stroke off and a speed too low to
    # finish, the arrival key belongs past the end of the clip so the press is still on its
    # way down when the video stops - clamping it here would quietly speed the press up to
    # whatever finishes on time, which is the opposite of what fit_stroke off asked for.
    f_stop = max(2, int(round(1 + need)))
    # The reported speed is the one the rounded keyframe actually produces, not the request.
    speed_eff = stroke / max(1e-6, (f_stop - 1) / FPS)

    # ---- floor, bed and backdrop -----------------------------------------------------
    bpy.ops.mesh.primitive_plane_add(size=90, location=(0, 0, 0))
    floor = bpy.context.object
    floor.name = "Floor"
    floor.data.materials.append(mat("FloorMat", (0.035, 0.036, 0.042, 1), rough=0.42))
    bpy.ops.rigidbody.object_add(type="PASSIVE")
    floor.rigid_body.friction = 0.95
    floor.rigid_body.restitution = 0.0

    # The bed plate is sunk so its TOP is flush with z=0: it is a material contrast that
    # anchors the bottom of the frame, and the floor plane keeps doing all the colliding.
    bpy.ops.mesh.primitive_cube_add(size=1, location=(0, 0, -0.06))
    bed = bpy.context.object
    bed.name = "Bed"
    bed.scale = (press_w * 1.30, press_d * 1.45, 0.12)
    bed.data.materials.append(mat("BedMat", (0.16, 0.17, 0.19, 1), rough=0.30, metal=0.85))

    # A wall behind everything. Without it the top corners of the frame are the black
    # world and a third of the shot is empty and dark once the press has come down. No
    # collider on it: it stands 4m behind a pile whose debris is squeezed sideways at
    # walking pace, and a zero-thickness static mesh is a tunnelling risk for no gain.
    back_y = pile_d / 2.0 + 4.2
    bpy.ops.mesh.primitive_plane_add(size=60, location=(0, back_y, 10.0),
                                     rotation=(math.radians(90), 0, 0))
    wall = bpy.context.object
    wall.name = "Backdrop"
    wall.data.materials.append(mat("WallMat", (0.055, 0.062, 0.078, 1), rough=0.92))

    # ---- the pile ---------------------------------------------------------------------
    base_rgba, rough, metal, transmit, density, fric, restit = MATERIALS[MATERIAL]
    # Four colour variants rather than one flat material: 200+ identical pieces read as a
    # texture, a little variation lets the eye follow individual chunks as they fly.
    variants = []
    for i in range(4):
        k = 1.0 + (i - 1.5) * 0.11
        rgba = (clamp(base_rgba[0] * k, 0.0, 1.0), clamp(base_rgba[1] * k, 0.0, 1.0),
                clamp(base_rgba[2] * k, 0.0, 1.0), 1.0)
        variants.append(mat(f"{MATERIAL}_{i}", rgba, rough=rough * (0.9 + 0.07 * i),
                            metal=metal, transmit=transmit))

    # Only the RATIOS between masses matter - the press is kinematic, so it is effectively
    # infinitely heavy - and Bullet's default tolerances are happiest around 1kg, so the
    # real densities are scaled to land there rather than used raw.
    piece_mass = max(0.08, density * (PIECE ** 3) * 0.05)
    pieces, shapes = [], []
    for iz in range(layers):
        for ix in range(nx):
            for iy in range(ny):
                kind = SHAPES.get(SHAPE) or rng.choice(("block", "sphere", "cylinder"))
                loc = ((ix - (nx - 1) / 2.0) * PIECE + rng.uniform(-JIT, JIT),
                       (iy - (ny - 1) / 2.0) * PIECE + rng.uniform(-JIT, JIT),
                       BODY / 2.0 + iz * PITCH_Z)
                if kind == "sphere":
                    bpy.ops.mesh.primitive_uv_sphere_add(radius=BODY / 2.0, segments=20,
                                                         ring_count=10, location=loc)
                    shape = "SPHERE"
                    bpy.ops.object.shade_smooth()
                elif kind == "cylinder":
                    # Flat-shaded on 24 sides: Mesh.use_auto_smooth is gone in 5.2, so
                    # smooth shading would round off the caps too and they would read as
                    # blobs.
                    bpy.ops.mesh.primitive_cylinder_add(radius=BODY / 2.0, depth=BODY,
                                                        vertices=24, location=loc)
                    shape = "CYLINDER"
                else:
                    # primitive_cube_add(size=BODY) already spans BODY units - no scaling.
                    # (size=1 with a scale of 0.5 makes a HALF-size box; that mistake cost
                    # this project a full rebuild once.)
                    bpy.ops.mesh.primitive_cube_add(size=BODY, location=loc)
                    shape = "BOX"
                pc = bpy.context.object
                pc.rotation_euler = (0.0, 0.0, rng.uniform(-YAW, YAW))
                pc.data.materials.append(variants[rng.randrange(len(variants))])
                # Kept alongside, not on the object: bpy_struct rejects attributes it does
                # not define, so pc.some_name = ... raises rather than sticking.
                pieces.append(pc)
                shapes.append(shape)

    add_rigid(pieces, "ACTIVE")
    for pc, shape in zip(pieces, shapes):
        rb = pc.rigid_body
        rb.mass = piece_mass
        rb.friction = fric
        rb.restitution = restit
        rb.collision_shape = shape
        # A sustained squeeze is not a single impact: with a zero margin the solver lets
        # compressed pieces interpenetrate and then fires them apart when it catches up.
        # A few millimetres of margin holds them without a visible floating gap.
        rb.use_margin = True
        rb.collision_margin = 0.004
        rb.linear_damping = 0.06
        rb.angular_damping = 0.10
        # Asleep until struck, or the pile shuffles itself flat during the approach. The
        # thresholds are then dropped almost to zero so that a piece being slowly squeezed
        # under the press cannot fall asleep again and get passed straight through.
        rb.use_deactivation = True
        rb.use_start_deactivated = True
        rb.deactivate_linear_velocity = 0.02
        rb.deactivate_angular_velocity = 0.02

    # ---- press slab, ram and guide posts ----------------------------------------------
    steel = mat("PressSteel", (0.20, 0.21, 0.24, 1), rough=0.26, metal=0.92)
    hydraulic = mat("Ram", (0.68, 0.70, 0.74, 1), rough=0.14, metal=1.0)
    bpy.ops.mesh.primitive_cube_add(size=1, location=(0, 0, press_face_start + THICK / 2.0))
    press = bpy.context.object
    press.name = "Press"
    press.scale = (press_w, press_d, THICK)     # size=1 cube: scale IS the dimension
    # ...and then baked into the mesh. The scale MUST NOT survive on the object, because
    # the ram columns are parented to the press and a child inherits its parent's scale:
    # leaving (2.2, 1.2, 0.45) on the press turns the rams into squashed lozenges.
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    press.data.materials.append(steel)
    bpy.ops.object.modifier_add(type="BEVEL")
    press.modifiers["Bevel"].width = 0.012
    press.modifiers["Bevel"].segments = 2
    bpy.ops.rigidbody.object_add(type="ACTIVE")
    press.rigid_body.mass = 5000.0              # ignored while kinematic; honest anyway
    press.rigid_body.collision_shape = "BOX"
    press.rigid_body.friction = 0.85
    press.rigid_body.restitution = 0.0
    press.rigid_body.use_margin = True
    press.rigid_body.collision_margin = 0.004
    press.rigid_body.kinematic = True           # never released: a press does not bounce

    # Two keys, LINEAR: down at a dead constant speed. A third holds it there while the
    # debris settles - but only if it actually arrives inside the clip, otherwise that key
    # would drag the press to its stop by the last frame no matter how slow it was set.
    press.location = (0.0, 0.0, press_face_start + THICK / 2.0)
    press.keyframe_insert("location", frame=sc.frame_start)
    press.location = (0.0, 0.0, stop_z + THICK / 2.0)
    press.keyframe_insert("location", frame=f_stop)
    if f_stop < sc.frame_end:
        press.keyframe_insert("location", frame=sc.frame_end)

    # Ram columns, parented to the press so they are visibly what carries it down. Long
    # enough to leave the top of the frame from the press's LOWEST position, so as the
    # press descends the columns grow into the band it vacates and no third of the frame
    # is ever empty and dark. Not longer than that: a 30m tower throws a hard shadow bar
    # across the backdrop for nothing.
    ram_len = max(8.0, press_face_start + 6.0)
    # Setting .parent in Python leaves the parent inverse at identity, so these locations
    # really are press-local. Assigning the parent's inverse matrix here - the reflex from
    # object parenting - turns them back into world coordinates and drops the columns
    # somewhere off-scene.
    for dx, radius in ((0.0, press_w * 0.15), (-press_w * 0.30, press_w * 0.062),
                       (press_w * 0.30, press_w * 0.062)):
        bpy.ops.mesh.primitive_cylinder_add(radius=radius, depth=ram_len, vertices=28,
                                            location=(0, 0, 0))
        ram = bpy.context.object
        ram.name = "Ram"
        ram.data.materials.append(hydraulic)
        ram.parent = press
        ram.matrix_parent_inverse = Matrix.Identity(4)
        ram.location = (dx, 0.0, THICK / 2.0 + ram_len / 2.0)

    posts = []
    if SHOW_FRAME:
        for sx in (-1.0, 1.0):
            bpy.ops.mesh.primitive_cube_add(
                size=1, location=(sx * (post_outer - post_thick / 2.0), 0.0, 5.0))
            post = bpy.context.object
            post.name = "GuidePost"
            post.scale = (post_thick, press_d * 0.55, 10.0)
            post.data.materials.append(steel)
            posts.append(post)
        add_rigid(posts, "PASSIVE")
        for post in posts:
            post.rigid_body.friction = 0.5
            post.rigid_body.restitution = 0.25

    # ---- light -------------------------------------------------------------------------
    # Key from the front left, high, aimed at the middle of the pile.
    area_light("Key", (-3.4, -4.2, pile_top + 3.2), (0, 0, pile_top * 0.55),
               2600.0, 6.0, (1.0, 0.97, 0.92))
    # Fill from the front right, softer and cooler, so the shadow side is not black.
    area_light("Fill", (3.6, -3.6, pile_top * 0.8), (0, 0, pile_top * 0.45),
               900.0, 5.0, (0.80, 0.88, 1.0))
    # Rim from behind and above, for a bright edge along the top of the press slab.
    area_light("Rim", (1.6, back_y - 1.2, press_face_start + 2.4),
               (0, 0, press_face_start), 1500.0, 4.0, (0.75, 0.86, 1.0))
    # The landing floor. Debris ends up spread across the bed and out to the posts, and an
    # unlit floor turns the bottom of the frame into a black band with rubble hiding in it.
    area_light("FloorWash", (0.0, -2.2, 4.4), (0, 0.2, 0.0),
               1500.0, post_outer * 2.4, (1.0, 0.98, 0.95), size_y=3.0)
    # A cove light at the foot of the backdrop: it puts a gradient up the wall so the top
    # of the frame reads as a room, and silhouettes the press against it.
    area_light("Cove", (0.0, back_y - 1.0, 0.30), (0, back_y, 6.0),
               2200.0, post_outer * 3.0, (0.42, 0.60, 0.95), size_y=0.6)

    # ---- camera ------------------------------------------------------------------------
    # Everything the shot is about, in frame at frame 1: the whole pile, the whole press
    # slab at its start height (a press parked above the frame edge with its ram hanging
    # into view passes every numeric check and is still a rejected frame), the guide posts
    # across their width, and the floor out to where debris lands.
    land_y = pile_d / 2.0 + 0.55
    edge_x = post_outer if SHOW_FRAME else press_w / 2.0 + 0.45
    fit = []
    for sx in (-1.0, 1.0):
        for sy in (-1.0, 1.0):
            for z in (0.0, pile_top):
                fit.append((sx * pile_w / 2.0, sy * pile_d / 2.0, z))
            for z in (press_face_start, press_face_start + THICK):
                fit.append((sx * press_w / 2.0, sy * press_d / 2.0, z))
    # The landing floor sampled as the ellipse debris actually covers, not as the
    # rectangle that bounds it. The near corners of that rectangle sit almost a metre
    # closer to the camera than the pile and therefore project widest of anything in the
    # scene - fitting them backs the camera off ~20% for a patch of floor nothing ever
    # lands on, and the pile pays for it by shrinking.
    for sx in (-1.0, 1.0):
        fit.append((sx * edge_x, 0.0, 0.0))                     # widest reach, mid depth
        fit.append((sx * edge_x, -pile_d / 2.0, 0.0))
        fit.append((sx * press_w / 2.0, -land_y, 0.0))          # nearest reach, narrower
    fit.append((0.0, -land_y, 0.0))
    cam, cam_dist, aim_z, achieved = solve_camera(sc, fit, LENS, FILL, PITCH)
    frame_h = 36.0 / LENS * cam_dist
    pile_fill = pile_top / frame_h if frame_h else 0.0

    # ---- preview ------------------------------------------------------------------------
    if PREVIEW:
        # Stepped, not jumped. Blender's rigid-body point cache only advances one frame at
        # a time: frame_set() straight to frame 90 on an unbaked cache renders the pile
        # exactly as it was at frame 1, and the approval gate then approves a shot nobody
        # has seen. Stepping costs a second or two.
        target = int(clamp(PREVIEW, sc.frame_start, sc.frame_end))
        dg = bpy.context.evaluated_depsgraph_get()
        for f in range(sc.frame_start, target + 1):
            sc.frame_set(f)
            dg.update()
        sc.render.image_settings.file_format = "PNG"
        sc.render.filepath = f"{OUT}/preview.png"
        bpy.ops.render.render(write_still=True)
        print(f"SCENE_OK preview={target} pieces={len(pieces)} shape={SHAPE} "
              f"material={MATERIAL} pile={pile_w:.2f}x{pile_d:.2f}x{pile_top:.2f} "
              f"cam_dist={cam_dist:.2f} pile_fill={pile_fill:.2f} fit={achieved:.2f}")
        return

    # ---- simulate and read the impacts off the real motion --------------------------
    # Nothing here is estimated from the keyframes: the contact frame is the frame the
    # measured top of the pile meets the measured press face, the crunches are the frames
    # the pieces measurably moved, and the bottom-out is the frame the press measurably
    # stopped. A sound gets placed on whatever frame is named, so a guess would be audible.
    dg = bpy.context.evaluated_depsgraph_get()
    half = BODY / 2.0
    move_eps = PIECE * 0.06                 # ~0.5 m/s at 30fps: a piece that really moved
    contact = None
    bottom = None
    prev_pos = None
    prev_press = None
    moving_series = []
    for f in range(sc.frame_start, sc.frame_end + 1):
        sc.frame_set(f)
        dg.update()
        pos = [pc.evaluated_get(dg).matrix_world.translation.copy() for pc in pieces]
        press_z = press.evaluated_get(dg).matrix_world.translation.z
        face = press_z - THICK / 2.0
        if contact is None:
            top = max(p.z for p in pos) + half
            if face <= top + BODY * 0.06:
                contact = f
        if prev_press is not None and bottom is None and f > sc.frame_start + 1:
            # The press ARRIVED on the previous frame - this is the first frame it failed
            # to move on. Naming f here would put the bottom-out sound a frame late.
            if abs(press_z - prev_press) < 1e-5:
                bottom = f - 1
        if prev_pos is not None:
            moved = sum(1 for a, b in zip(pos, prev_pos) if (a - b).length > move_eps)
            moving_series.append((f, moved))
        prev_pos, prev_press = pos, press_z

    events = []
    if contact:
        events.append({"frame": int(contact), "kind": "impact", "strength": 1.0,
                       "label": "contact"})
    after = [(f, v) for f, v in moving_series if contact and f > contact + 1]
    min_gap = max(3, int(FPS * 0.18))
    for f, rel in peaks(after, min_gap, 0.20):
        events.append({"frame": int(f), "kind": "impact",
                       "strength": round(clamp(rel * 0.9, 0.25, 0.95), 3),
                       "label": "crunch"})
    if bottom and bottom <= sc.frame_end:
        events.append({"frame": int(bottom), "kind": "impact",
                       "strength": 0.85 if CRUSH_TO == "floor" else 0.7,
                       "label": "bottom"})
    # More than a dozen hits in six seconds stops being rhythm and turns to mush, so keep
    # the loudest - but never drop the contact or the bottom-out, which are the two frames
    # the whole shot is built around.
    keep = [e for e in events if e["label"] in ("contact", "bottom")]
    rest = sorted((e for e in events if e["label"] == "crunch"),
                  key=lambda e: -e["strength"])[:max(0, 12 - len(keep))]
    events = sorted(keep + rest, key=lambda e: e["frame"])

    with open(f"{OUT}/impacts.json", "w", encoding="utf-8") as fh:
        json.dump({"fps": FPS, "events": events,
                   "contact_frame": contact, "bottom_frame": bottom,
                   "pieces": len(pieces), "shape": SHAPE, "material": MATERIAL,
                   "press_speed": round(speed_eff, 3)}, fh, indent=1)

    sc.render.image_settings.file_format = "PNG"     # 5.2 has no FFMPEG output at all
    sc.render.image_settings.color_mode = "RGB"
    sc.render.filepath = f"{OUT}/frame_"
    bpy.ops.render.render(animation=True)
    print(f"SCENE_OK pieces={len(pieces)} shape={SHAPE} material={MATERIAL} "
          f"pile={pile_w:.2f}x{pile_d:.2f}x{pile_top:.2f} grid={nx}x{ny}x{layers} "
          f"speed={speed_eff:.2f}m/s{' (fitted)' if fitted else ''} "
          f"stroke={stroke:.2f}m stop_z={stop_z:.2f} contact={contact} bottom={bottom} "
          f"events={len(events)} frames={sc.frame_end} cam_dist={cam_dist:.2f} "
          f"aim_z={aim_z:.2f} pile_fill={pile_fill:.2f} fit={achieved:.2f}")


main()
