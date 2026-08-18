"""An object tumbling along a tiled floor, dropping into holes it fits exactly, bouncing out.

    blender -b -noaudio -P gap_run.py -- '{"shape":"cube","gap_every":3,"out_dir":"..."}'

This file is HAND MAINTAINED SOURCE. A model picks the scene and fills in PARAMS; it never
writes geometry. It is the parametric version of scenes/tumbling_cube.py, and it keeps the
three things that made that one work - the pivot-about-the-leading-edge roll, the
constant-speed tracking camera, the exact loop period - while making everything else a
parameter.

THE TRICK IS THAT IT NEVER STAYS IN THE HOLE. The viewer's eye reads a square object
arriving over a square hole of exactly its own size and predicts that it drops in and
stops. It drops in and comes straight back out, every time, on a beat. That is the whole
video, so three things are load-bearing:

  IT MUST FIT. The hole is the object's own footprint plus 1%, solved from the object, not
  typed in. A hole 10% too big turns a magic trick into a thing falling down a shaft.

  IT MUST LAND SQUARE. That is why the motion is CHOREOGRAPHED rather than simulated, the
  same decision tumbling_cube made and for the same reason: a rigid-body solver tumbling a
  box down a floor diverges within two rolls and never lands square on a hole again. The
  motion is still derived from real rolling: each roll pivots about the leading bottom
  EDGE (a cube) or rolls on the leading corner ARC (a rounded cube) or is true rolling
  contact (a cylinder), so it reads as weight rather than as a sprite being slid along.
  Nothing about that is estimated - see roll_travel(), where the advance per roll is
  derived for each shape and the tile pitch is then set to it, which is what keeps the
  object landing dead centre on hole after hole with no accumulating drift.

  IT MUST LOOP. One period is rendered and the app repeats it (LOOP = True below). The
  gap pattern is generated modulo the loop length, the camera is keyed from a circularly
  smoothed track, and the pose is a closed-form function of the frame, so frame N+1 is
  frame 1 translated by exactly one loop distance. The cut is hard, with no crossfade.
  (The reference clip this came from does not loop: it cuts mid-tumble.)

IMPACTS ARE MEASURED, NOT COUNTED. impacts.json is written from a pass that steps the
depsgraph frame by frame and reads the EVALUATED transform of the object - the frame its
descent is arrested near a surface is a contact, its speed the frame before is the
strength, and the depth it was at says whether it hit a tile or the bottom of a hole.
Nothing is read back out of the animation formula, so a change to the easing, or an
interpolation surprise, shows up in the sound instead of hiding in it.

A note on seeds: the gap pattern, the starting tile, the tile speckle, the glow variation
and which side the camera sits on all come from `seed`. Two runs of the same prompt land
on different rhythms rather than on the same video twice.
"""

# --------------------------------------------------------------------------- parameters
# Read by the model that picks this scene. It cannot see the render, so every note is
# written for someone who has never watched the shot.

PARAMS = {
    "seed": {
        "range": [0, 9999], "default": 0,
        "note": "Changes the RHYTHM, not just the dressing: which tiles are holes, which "
                "tile the loop opens on, the speckle across the floor, the glow variation "
                "between holes and which side the camera sits on. Two runs with the same "
                "everything else and a different seed are two different videos. Vary it.",
    },

    # ---- the object
    "shape": {
        "choices": ["cube", "rounded_cube", "cylinder"], "default": "cube",
        "note": "What is tumbling. 'cube' tips 90 degrees per tile over its leading bottom "
                "edge and lands flat - the hardest, most graphic read and the reference "
                "look. 'rounded_cube' rolls on its corner arcs, which is softer and reads "
                "as a heavier, more toy-like object. 'cylinder' does not tip at all, it "
                "rolls continuously and only reacts at the holes, so it is the calmest of "
                "the three and the only one with no per-tile impact.",
    },
    "corner_radius": {
        "range": [0.04, 0.18], "default": 0.10,
        "note": "shape='rounded_cube' only: corner radius as a fraction of the object. The "
                "roll is solved from it exactly (a rounded square advances less per turn "
                "than a sharp one), so the object still lands centred on every hole. Large "
                "values make the object noticeably wider than one tile - which is fine, the "
                "hole is sized from the object - but past about 0.15 it stops reading as a "
                "cube at all.",
    },
    "cylinder_length": {
        "range": [0.35, 0.95], "default": 0.80,
        "note": "shape='cylinder' only: length along the axis, as a fraction of the "
                "diameter. Short discs read as coins and spin more visibly; long ones read "
                "as rollers. Kept under 1.0 so it always drops through a square hole cut "
                "to its diameter.",
    },
    "object_material": {
        "choices": ["marble", "stone", "concrete", "slate", "wood", "steel", "copper",
                    "plastic", "glass"], "default": "marble",
        "note": "What the tumbling object is made of. Pick something that separates from "
                "floor_material or the whole shot goes to mush - light object on a dark "
                "floor is the safe pairing and the one the format is usually cut with. "
                "'glass' is the prettiest and roughly doubles render time; give it 60+ "
                "samples or the refractions stay grainy.",
    },

    # ---- the floor
    "tile_size": {
        "range": [0.45, 1.60], "default": 1.00,
        "note": "Edge of one floor tile in metres, which is also the distance the object "
                "travels per roll and therefore the scale of the whole shot. It does NOT "
                "change how fast the loop plays (roll_frames does that) - it changes how "
                "big everything feels, because the lighting and the camera are solved from "
                "it. Leave it at 1.0 unless the idea is specifically a tiny object.",
    },
    "floor_material": {
        "choices": ["slate", "stone", "concrete", "marble", "wood", "steel"],
        "default": "slate",
        "note": "What the floor is made of. Dark floors ('slate', 'stone') let the glow "
                "out of the holes carry the shot and are the default look; 'marble' and "
                "'steel' are brighter and need a dark object to read against.",
    },
    "gap_every": {
        "range": [2.0, 6.0], "default": 3.0,
        "note": "Average number of tiles between holes. The exact positions are seeded, so "
                "this sets the DENSITY and the seed sets the pattern - which is what stops "
                "the loop sounding like a metronome. 2 is relentless (a hole almost every "
                "tile), 3 is the reference rhythm, 5-6 leaves long solid runs that make "
                "each drop land harder. Holes are never placed adjacent to each other.",
    },
    "gap_depth": {
        "range": [0.25, 1.60], "default": 1.05,
        "note": "How deep each hole is, in tiles - and therefore how far the object falls, "
                "because it drops all the way to the bottom and bounces off it. Below ~0.5 "
                "it dips rather than drops. At 1.0 and above the object disappears "
                "completely below the floor line for a few frames before it comes back, "
                "which is the strongest version of the trick.",
    },
    "glow_colour": {
        "choices": ["cyan", "aqua", "violet", "magenta", "amber", "lime", "ember", "white"],
        "default": "cyan",
        "note": "Colour of the light coming up out of the holes. This is the only saturated "
                "colour in the frame and it is what makes the holes read as holes rather "
                "than as dark tiles, so pick something that fights the floor: 'cyan' or "
                "'violet' under a grey floor, 'amber' or 'ember' under a cool one.",
    },
    "glow_strength": {
        "range": [0.8, 8.0], "default": 3.2,
        "note": "Emission strength at the bottom of each hole. 3.2 is the tuned value. "
                "Above about 6 the hole clips to flat white and stops reading as a recess - "
                "it looks like a glowing TILE, which is the exact opposite of the effect. "
                "Below 1.5 the holes go black and the shot loses its only colour.",
    },

    # ---- timing
    "roll_frames": {
        "range": [6, 30], "default": 14,
        "note": "Frames for one 90-degree roll from tile to tile - this is the SPEED knob, "
                "and at 30fps it is directly a duration: 14 frames is 0.47s per tile, 8 is "
                "brisk and hypnotic, 22 is slow and heavy. Below about 8 the roll strobes "
                "at 30fps and the object smears.",
    },
    "drop_frames": {
        "range": [3, 14], "default": 6,
        "note": "Frames for the fall into a hole, and again for the bounce back out. The "
                "fall accelerates and the rise decelerates (both are parabolas), so it "
                "reads as a real drop and a real rebound rather than a slide. Short values "
                "make the object snap in and out, which is the punchier read; long ones "
                "give a floatier, lower-gravity feel.",
    },
    "loop_tiles": {
        # The range starts at 0 because 0 is the "solve it yourself" value and anything
        # else would be clamped straight back up to the minimum before the scene ever ran.
        # Real values are 4 to 20; 1, 2 and 3 are raised to 4.
        "range": [0, 20], "default": 0,
        "note": "How many tiles one loop covers, i.e. the length of the rendered period. 0 "
                "means solve it from `seconds` so the period comes out close to the length "
                "asked for. The gap pattern repeats over exactly this many tiles, so a "
                "small number is a short tight loop that the viewer starts to recognise, "
                "and a large one hides the repeat at the cost of render time. Only one "
                "period is ever rendered - the app repeats it.",
    },

    # ---- camera
    "camera": {
        "choices": ["alongside", "behind"], "default": "alongside",
        "note": "'alongside' rides beside and just ahead of the object, so it tumbles "
                "toward the lens and you watch it disappear into each hole in profile - "
                "this is the reference framing and the one where the 90-degree tip reads "
                "best. 'behind' chases up the row from behind and slightly to one side, so "
                "the tiles recede up the tall axis of the frame and you can see the holes "
                "coming before the object reaches them; it fills a 9:16 frame better but "
                "flattens the tip.",
    },
    "view_tiles": {
        "range": [2.0, 7.0], "default": 3.4,
        "note": "How many tiles of floor are in shot along the direction of travel. This is "
                "the zoom: the camera distance is SOLVED from it, so do not look for a "
                "distance parameter. 2.5 is tight and abstract (you lose the sense of a "
                "run), 5+ shows several holes at once and turns the shot into a landscape.",
    },
    "lens": {
        "range": [0.0, 80.0], "default": 0.0,
        "note": "Focal length in mm. 0 means auto (42 alongside, 34 behind). Short lenses "
                "exaggerate the depth of the holes, which is the effect this scene lives "
                "on; long ones flatten the floor into a graphic pattern, which is a "
                "deliberate different look rather than a worse one.",
    },
    "camera_pitch_deg": {
        "range": [0.0, 62.0], "default": 0.0,
        "note": "How far above horizontal the camera looks down. 0 means auto (42 "
                "alongside, 33 behind). Raised automatically if it would be shallow enough "
                "to let the empty world background in over the horizon. Never goes above "
                "62: top-down kills the depth of the holes, and the depth is the trick.",
    },

    # ---- light
    "light_energy": {
        "range": [0.3, 3.0], "default": 1.0,
        "note": "Overall brightness multiplier for the four-light rig (key, fill, rim and a "
                "wide wash along the floor ahead). The rig's power is solved from its own "
                "distance and from tile_size, so this is a taste knob and not a repair "
                "tool - 1.0 is exposed correctly at every tile size.",
    },

    # ---- render contract (supplied by the runner, not by the model choosing the scene)
    "out_dir": {"default": "//out", "note": "app-supplied: where frames, preview.png and "
                                            "impacts.json are written."},
    "res_x": {"range": [256, 2160], "default": 1080, "note": "app-supplied render width."},
    "res_y": {"range": [256, 3840], "default": 1920,
              "note": "app-supplied render height. The camera is solved for whichever axis "
                      "is larger, so 9:16 needs no extra tuning."},
    "samples": {"range": [8, 512], "default": 24,
                "note": "app-supplied Cycles samples. 24 with denoising is enough for "
                        "everything except a glass object."},
    "fps": {"range": [12, 60], "default": 30, "note": "app-supplied frame rate."},
    "seconds": {"range": [1.0, 20.0], "default": 4.0,
                "note": "Target length of ONE loop period. Only used when loop_tiles is 0, "
                        "and only as a target - the period always comes out a whole number "
                        "of tiles. The finished video is this repeated to length."},
    "preview_frame": {"range": [0, 900], "default": 0,
                      "note": "app-supplied: if greater than 0, render that ONE frame to "
                              "preview.png and stop. Used for the approval gate."},
}

# One period is a complete, seamless cycle: the app renders this once and repeats it to
# length instead of rendering the full duration. Read by physics_mode/library.py.
LOOP = True

import json
import math
import random
import sys

try:
    import bpy
    from mathutils import Vector
except ImportError:      # importable outside Blender so PARAMS can be read without bpy
    bpy = None
    Vector = None

argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
P = json.loads(argv[0]) if argv else {}


def _default(key):
    return PARAMS[key]["default"]


def pf(key) -> float:
    """Float from P, clamped to the range PARAMS declares. Bad input falls to the default."""
    spec = PARAMS[key]
    try:
        v = float(P.get(key, spec["default"]))
    except (TypeError, ValueError):
        v = float(spec["default"])
    rng = spec.get("range")
    if rng:
        v = max(float(rng[0]), min(float(rng[1]), v))
    return v


def pi(key) -> int:
    return int(round(pf(key)))


def ps(key) -> str:
    v = P.get(key, _default(key))
    v = str(v if v is not None else _default(key)).strip().lower()
    choices = PARAMS[key].get("choices") or []
    return v if v in choices else str(_default(key))


OUT = str(P.get("out_dir") or _default("out_dir"))
RES_X, RES_Y = pi("res_x"), pi("res_y")
SAMPLES = pi("samples")
FPS = max(1, pi("fps"))
SECONDS = pf("seconds")
PREVIEW = pi("preview_frame")
SEED = int(P.get("seed", 0) or 0)
RNG = random.Random(SEED)

# Colours are linear, not sRGB - Cycles works in linear and these were picked by eye in a
# render, so do not "correct" them.
MATERIALS = {
    "slate":    {"colour": (0.055, 0.060, 0.075), "rough": 0.52, "metal": 0.0},
    "stone":    {"colour": (0.180, 0.180, 0.195), "rough": 0.80, "metal": 0.0},
    "concrete": {"colour": (0.230, 0.225, 0.215), "rough": 0.88, "metal": 0.0},
    "marble":   {"colour": (0.720, 0.715, 0.690), "rough": 0.16, "metal": 0.0},
    "wood":     {"colour": (0.235, 0.130, 0.062), "rough": 0.58, "metal": 0.0},
    "steel":    {"colour": (0.520, 0.535, 0.570), "rough": 0.24, "metal": 1.0},
    "copper":   {"colour": (0.720, 0.330, 0.150), "rough": 0.30, "metal": 1.0},
    "plastic":  {"colour": (0.820, 0.180, 0.290), "rough": 0.34, "metal": 0.0},
    "glass":    {"colour": (0.780, 0.870, 0.900), "rough": 0.04, "metal": 0.0,
                 "trans": 0.92, "ior": 1.46},
}
GLOWS = {
    "cyan":    (0.05, 0.42, 1.00),
    "aqua":    (0.08, 0.90, 0.78),
    "violet":  (0.42, 0.16, 1.00),
    "magenta": (1.00, 0.10, 0.55),
    "amber":   (1.00, 0.50, 0.08),
    "lime":    (0.38, 1.00, 0.16),
    "ember":   (1.00, 0.20, 0.04),
    "white":   (0.90, 0.94, 1.00),
}

# Fixed, deliberately not exposed - each one is a decision rather than a preference.
BOTTOM_HOLD = 1        # frames held on the floor of the hole: the impact needs one beat
FALL_OVERLAP = 0.22    # fraction of the last roll during which the object is already
#                        falling. It tips over the near lip of the hole and drops from
#                        there; landing flat and only THEN dropping reads as hesitation.
SEAM = 0.972           # tile size as a fraction of the pitch. The dark line between tiles
#                        is the grout, and it is also what stops a floor of one flat
#                        colour from reading as a single extruded slab.
TILE_THICK = 0.42      # tile depth in pitch units - only ever seen at the hole edges
WALL_THICK = 0.055     # hole wall thickness in pitch units
TINTS = 6              # shared tile materials, dealt out by a seeded per-tile draw
FAR_TILES = 22         # hardest cap on how far ahead the tile field is built; past this
#                        the four filler planes carry the distance for a fraction of the
#                        object count
LAT_TILES = 9          # same, sideways

# ------------------------------------------------------------------------------ helpers


def use_gpu(sc) -> str:
    """Point Cycles at the GPU. OptiX first (RTX ray-tracing cores), then CUDA.

    Copied verbatim from the sibling scenes - on CPU a 200-frame loop at 1080x1920 runs
    for hours, which is not a mode.
    """
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


def _set(node, names, value) -> bool:
    """Set the first socket that exists.

    Principled's socket names have moved twice (Transmission -> Transmission Weight,
    Emission -> Emission Color) and a scene that dies on a KeyError renders nothing at all.
    """
    for n in names:
        if n in node.inputs:
            node.inputs[n].default_value = value
            return True
    return False


def mat(name, rgb, rough=0.7, metal=0.0, trans=0.0, ior=1.45, emit=0.0, emit_rgb=None):
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    b = m.node_tree.nodes["Principled BSDF"]
    _set(b, ("Base Color",), (rgb[0], rgb[1], rgb[2], 1.0))
    _set(b, ("Roughness",), rough)
    _set(b, ("Metallic",), metal)
    if trans:
        _set(b, ("Transmission Weight", "Transmission"), trans)
        _set(b, ("IOR",), ior)
    if emit:
        e = emit_rgb or rgb
        _set(b, ("Emission Color", "Emission"), (e[0], e[1], e[2], 1.0))
        _set(b, ("Emission Strength",), emit)
    return m


def from_spec(name, spec, tint=1.0):
    c = spec["colour"]
    return mat(name, (c[0] * tint, c[1] * tint, c[2] * tint), rough=spec["rough"],
               metal=spec.get("metal", 0.0), trans=spec.get("trans", 0.0),
               ior=spec.get("ior", 1.45))


def unit_box_mesh(name, material, bevel=0.014):
    """A 1x1x1 box datablock, centred on its origin, with a small bevel baked in.

    One datablock is instanced a few hundred times and the DIMENSIONS go into the object's
    scale. That is safe here precisely because nothing in this scene is a rigid body - a
    rigid body derives its collision shape from the object and an unapplied non-uniform
    scale is exactly how the sim and the picture drift apart. The bevel is baked into the
    mesh rather than added as a modifier per tile: 500 bevel modifiers is minutes of
    evaluation per frame, and the bevel is what makes a tile edge catch the glow.
    """
    h = 0.5
    verts = [(-h, -h, -h), (h, -h, -h), (h, h, -h), (-h, h, -h),
             (-h, -h, h), (h, -h, h), (h, h, h), (-h, h, h)]
    faces = [(0, 3, 2, 1), (4, 5, 6, 7), (0, 1, 5, 4),
             (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7)]
    me = bpy.data.meshes.new(name)
    me.from_pydata(verts, [], faces)
    me.update()
    if bevel > 0.0:
        try:
            import bmesh
            bm = bmesh.new()
            bm.from_mesh(me)
            bmesh.ops.bevel(bm, geom=list(bm.verts) + list(bm.edges) + list(bm.faces),
                            offset=bevel, offset_type="OFFSET", segments=2,
                            profile=0.5, affect="EDGES", clamp_overlap=True)
            bm.to_mesh(me)
            bm.free()
            me.update()
        except Exception:  # noqa: BLE001 - cosmetic, never worth failing the render over
            pass
    me.materials.append(material)
    return me


def unit_plane_mesh(name, material):
    """A 1x1 plane at z=0 facing +Z. Used for the glowing floor of every hole."""
    h = 0.5
    me = bpy.data.meshes.new(name)
    me.from_pydata([(-h, -h, 0.0), (h, -h, 0.0), (h, h, 0.0), (-h, h, 0.0)], [],
                   [(0, 1, 2, 3)])
    me.update()
    me.materials.append(material)
    return me


def place(mesh, location, scale, name):
    ob = bpy.data.objects.new(name, mesh)
    ob.location = location
    ob.scale = scale
    bpy.context.scene.collection.objects.link(ob)
    return ob


def half_angles(lens, res_x, res_y):
    """tan of the half field of view, vertical and horizontal.

    Blender fits the 36mm sensor to the LARGER resolution axis. In 9:16 that is the HEIGHT,
    so the visible height at distance d is 36/lens*d and the width follows from the aspect
    ratio - the opposite of the landscape intuition, and the reason a camera distance that
    was guessed rather than solved is always wrong in a vertical format.
    """
    if res_y >= res_x:
        v = 18.0 / lens
        return v, v * (res_x / float(res_y))
    hh = 18.0 / lens
    return hh * (res_y / float(res_x)), hh


def solve_distance(points, u, lens, fill_h, fill_w, res_x, res_y) -> float:
    """Back the camera off along `u` until every point is inside the frame.

    `u` is the unit direction from the aim point TOWARDS the camera, and the camera is
    aimed at the origin of `points`, so that direction is exactly +Z in camera space: a
    point at camera-space offset q sits at (qx, qy, qz - D) once the camera is D away.
    |qx| <= kx*(D - qz) and |qy| <= ky*(D - qz) each give a lower bound on D, and the
    answer is the largest of them. No fitting, no iteration, nothing can end up outside
    the frame.
    """
    quat = (-u).to_track_quat("-Z", "Y")
    rot_t = quat.to_matrix().transposed()
    tan_v, tan_h = half_angles(lens, res_x, res_y)
    ky, kx = tan_v * fill_h, tan_h * fill_w
    dist = 0.0
    for p in points:
        q = rot_t @ Vector(p)
        dist = max(dist, q.z + abs(q.y) / ky, q.z + abs(q.x) / kx)
    return dist


def diagonal_offset(pitch: float, res_x: int, res_y: int) -> float:
    """How far to swing the camera off the row's long axis, in radians.

    Looking straight down a row is the obvious framing and it is wrong: a line pointed at
    the camera projects to a thread. Swinging off the axis by `off` puts it on a diagonal
    instead, and asking for the projected across-frame and up-frame extents to be in the
    frame's own aspect ratio gives off = atan(sin(pitch) * res_x / res_y). Derived and
    measured in domino_run.py; reused here unchanged.
    """
    return math.atan(math.sin(pitch) * (float(res_x) / float(res_y)))


def circ_smooth(vals, window):
    """Moving average with the ends joined.

    Wrapping is the whole point: the camera track has to be periodic to the frame, and a
    normal moving average flattens the two ends differently and leaves a visible hitch at
    the loop seam. Indices wrap, so the smoothed track is periodic by construction.
    """
    n = len(vals)
    half = max(1, int(window) // 2)
    if n < 3 or half < 1:
        return list(vals)
    half = min(half, n // 2)
    span = 2 * half + 1
    return [sum(vals[(i + k) % n] for k in range(-half, half + 1)) / span
            for i in range(n)]


# ------------------------------------------------------------------- the shape, and how it rolls
# Everything downstream is driven by three numbers per shape: how far it advances per roll
# (which becomes the tile pitch, so it can never drift off the holes), how wide it is (which
# becomes the hole), and where its centre is at each instant of a roll.


def solve_shape(shape: str, pitch: float, loop_tiles: int) -> dict:
    """Solve the object's size and roll kinematics against a tile pitch of `pitch`.

    CUBE. Side S = pitch. It tips about the leading bottom edge, so the centre swings on a
    circle of radius S/sqrt(2) about that edge and advances exactly S per 90 degrees.

    ROUNDED CUBE. A rounded square does NOT advance its own width per quarter turn. While a
    corner arc of radius r is in contact, the arc rolls on the floor: the centre of that arc
    stays at height r and travels r*(pi/2), and the body's centre travels that plus the two
    straight offsets, so the advance is (S - 2r) + r*pi/2 = S - r*(2 - pi/2). Setting that
    equal to the pitch gives the side S = pitch + r*(2 - pi/2) - i.e. the object comes out a
    few percent WIDER than one tile, and the hole is cut to the object, not to the tile. The
    two tiles either side of a hole are trimmed to make room (see build_floor). Doing it the
    other way round - forcing the object to one tile wide and pretending the advance is a
    full tile - drifts by 0.43*r every roll and walks off the holes within one loop.

    CYLINDER. It rolls continuously, so its rotation must also come back to where it started
    or the loop seam shows on the end caps: over one loop it turns loop_distance / R radians.
    That does NOT have to be a whole number of turns, only a whole number of HALF turns - the
    marker on each cap is a bar straight across the diameter, so the object is identical to
    itself after 180 degrees. Half turns give twice as many candidate radii, which matters:
    restricted to whole turns, a four-tile loop has no radius between 0.6 and 1.0 tiles at
    all and the object comes out as a small coin in a large hole. R is then whichever
    candidate lands closest to a tile-sized object.
    """
    if shape == "cylinder":
        loop_dist = loop_tiles * pitch
        target_r = 0.47 * pitch                       # a hair under half a tile
        best = None
        half_turn = int(round(loop_dist / (math.pi * target_r)))   # in half turns
        for k in range(max(1, half_turn - 4), half_turn + 5):
            r = loop_dist / (math.pi * k)             # k half turns over the loop
            if not 0.36 * pitch <= r <= 0.50 * pitch:
                continue
            score = abs(r - target_r)
            if best is None or score < best[0]:
                best = (score, r, k / 2.0)
        if best is None:
            # Nothing in the band closes the rotation. Take a tile-sized object and accept
            # that the cap markers jump at the loop seam - it is one frame on a rolling
            # cylinder and it beats a coin rattling around in a hatch. Printed, not silent.
            best = (0.0, 0.47 * pitch, loop_dist / (2.0 * math.pi * 0.47 * pitch))
            print("SCENE_NOTE cylinder rotation does not close over this loop length - "
                  "the end-cap markers will jump at the seam; try another loop_tiles.")
        _, radius, turns = best
        length = 2.0 * radius * pf("cylinder_length")
        return {"kind": "cylinder", "radius": radius, "turns": turns,
                "width": 2.0 * radius, "depth": length, "rest_z": radius,
                "half": (radius, length / 2.0, radius),
                "note": f"cylinder r={radius:.3f} ({2 * radius / pitch:.2f} tiles) "
                        f"turns/loop={turns:g}"}

    if shape == "rounded_cube":
        # r is expressed against the pitch, then S follows; solving the other way round
        # would need a fixed point and gains nothing.
        r = pf("corner_radius") * pitch
        side = pitch + r * (2.0 - math.pi / 2.0)
        return {"kind": "rounded_cube", "side": side, "radius": r,
                "width": side, "depth": side, "rest_z": side / 2.0,
                "half": (side / 2.0, side / 2.0, side / 2.0),
                "note": f"rounded cube {side:.3f} ({side / pitch:.3f} tiles) r={r:.3f}"}

    side = pitch
    return {"kind": "cube", "side": side, "radius": 0.0,
            "width": side, "depth": side, "rest_z": side / 2.0,
            "half": (side / 2.0, side / 2.0, side / 2.0),
            "note": f"cube {side:.3f} (1.000 tiles)"}


def roll_pose(body: dict, pitch: float, tile: int, phase: float):
    """Centre (x, z) and Y rotation `phase` (0..1) of the way from `tile` to `tile+1`.

    Blender's +Y rotation maps (x, z) -> (x cos a + z sin a, -x sin a + z cos a), which is
    the direction a body rolling toward +X turns. Every branch below returns the rest pose
    at phase 0 and the next tile's rest pose at phase 1, exactly - that identity is what
    makes the loop close.
    """
    x0 = tile * pitch
    if body["kind"] == "cylinder":
        # No tipping: contact rolling, so the turn is the distance over the radius. Taken
        # from the ABSOLUTE x, not accumulated per roll, so a pause in a hole (where x does
        # not advance) pauses the spin too, and nothing can drift out of phase.
        x = x0 + phase * pitch
        return x, body["rest_z"], x / body["radius"]

    ang = (math.pi / 2.0) * phase
    if body["kind"] == "rounded_cube":
        r, side = body["radius"], body["side"]
        a = side / 2.0 - r                       # corner-centre offset from the centre
        # The arc's centre rolls: forward r*ang, always at height r.
        cx = x0 + a + r * ang
        vx, vz = -a, a                           # centre relative to the arc centre, at a=0
        return (cx + vx * math.cos(ang) + vz * math.sin(ang),
                r + (-vx * math.sin(ang) + vz * math.cos(ang)),
                ang)

    # Cube: pivot about the leading bottom edge at x0 + side/2, z = 0.
    side = body["side"]
    px = x0 + side / 2.0
    vx, vz = -side / 2.0, side / 2.0
    return (px + vx * math.cos(ang) + vz * math.sin(ang),
            -vx * math.sin(ang) + vz * math.cos(ang),
            ang)


def rolled_top(body: dict, ang: float) -> float:
    """Half-height of the object's axis-aligned box after rotating by `ang` about Y.

    Exact for a box (a box's corners are its extreme points) and a slight over-estimate for
    the rounded one, which only ever means the frame is a shade wider than it had to be.
    """
    if body["kind"] == "cylinder":
        return body["radius"]        # a circle is the same height whichever way it is turned
    hx, _hy, hz = body["half"]
    return hx * abs(math.sin(ang)) + hz * abs(math.cos(ang))


# ------------------------------------------------------------------------- the gap pattern


def gap_pattern(loop_tiles: int, every: float) -> list:
    """Which tiles of the period are holes. Seeded, periodic, never two in a row.

    Generated MODULO the loop length, which is what lets the pattern - and therefore the
    rhythm of the drops - repeat exactly when the app loops the render. Tile 0 is always
    solid so the loop opens (and the preview frame lands) on the object standing on floor
    rather than hovering over a hole.

    Holes are kept at least two tiles apart. Adjacent holes read as one trench, and the
    thin wall between them is the only thing the object would have to pivot on.
    """
    want = max(1, int(round(loop_tiles / max(2.0, every))))
    slots = list(range(1, loop_tiles))
    RNG.shuffle(slots)
    chosen = []
    for s in slots:
        if len(chosen) >= want:
            break
        # Cyclic distance: tile loop_tiles is tile 0 again, so a hole at the very end and
        # one at the very start would be neighbours across the loop seam.
        if all(min((s - c) % loop_tiles, (c - s) % loop_tiles) >= 2 for c in chosen):
            chosen.append(s)
    return sorted(chosen)


# ----------------------------------------------------------------------------- the motion


def build_motion(body, pitch, gaps, loop_tiles, roll_frames, drop_frames, depth):
    """One pose (x, z, angle) per frame of the loop period.

    The beat of a gap tile is: tip over the near lip, fall (accelerating) to the bottom,
    hold for a frame, spring back out (decelerating) to floor level, roll on. The fall
    STARTS INSIDE the last roll - the object tips over the lip of the hole and drops from
    there, because landing flat on top of the hole and only then sinking reads as
    hesitation, which is the one thing this shot cannot afford.

    The last frame emitted for a hole is a hair below floor level rather than exactly on it,
    so that it is not a duplicate of the first frame of the next roll: two identical frames
    in a row is a visible stall at 30fps.
    """
    poses = []
    overlap = max(1, int(round(roll_frames * FALL_OVERLAP)))
    # A tipping shape only knows its angle WITHIN one roll, so the quarter turns it has
    # already made are added here. The cylinder's angle is absolute already (it comes from
    # the distance travelled), which is what keeps its spin in phase after a pause in a hole.
    turned = 0.0 if body["kind"] == "cylinder" else math.pi / 2.0
    for r in range(loop_tiles):
        into_gap = ((r + 1) % loop_tiles) in gaps
        fall_n = overlap + drop_frames
        for k in range(roll_frames):
            x, z, ang = roll_pose(body, pitch, r, k / float(roll_frames))
            ang += turned * r
            j = k - (roll_frames - overlap)
            if into_gap and j >= 0:
                t = (j + 1) / float(fall_n)
                z -= depth * t * t
            poses.append((x, z, ang))
        if not into_gap:
            continue
        xe, ze, ae = roll_pose(body, pitch, r, 1.0)
        ae += turned * r
        for j in range(overlap, fall_n):                    # the rest of the fall
            t = (j + 1) / float(fall_n)
            poses.append((xe, ze - depth * t * t, ae))
        for _ in range(BOTTOM_HOLD):                        # the beat it lands on
            poses.append((xe, ze - depth, ae))
        for j in range(drop_frames):                        # and straight back out
            t = (j + 1) / float(drop_frames + 1)
            poses.append((xe, ze - depth * (1.0 - t) ** 2, ae))
    return poses


def camera_track(poses, pitch, loop_tiles, window):
    """Camera x per frame: constant speed, adjusted by the object's own periodic wander.

    The object advances in steps and stops dead in every hole; the camera cannot. Its base
    is a constant speed of one loop distance over the period, which is what makes the render
    loop. The object's deviation from that base is periodic by construction, so smoothing it
    with a WRAPPED moving average and adding it back keeps the object near the middle of the
    frame without ever adding a jerk, and leaves the track exactly periodic - track(f + N) is
    track(f) plus one loop distance, to the float. With a wide window this is constant speed
    with a slight breath; with an infinite one it is exactly the constant-speed camera
    tumbling_cube used.
    """
    n = len(poses)
    loop_dist = loop_tiles * pitch
    base = [poses[0][0] + loop_dist * (i / float(n)) for i in range(n)]
    dev = [poses[i][0] - base[i] for i in range(n)]
    smooth = circ_smooth(dev, window)
    return [base[i] + smooth[i] for i in range(n)]


# ------------------------------------------------------------------------------ the floor


def build_floor(field, gaps, loop_tiles, pitch, hole, depth, floor_spec, glow_rgb,
                glow_strength):
    """The tiled floor, with a walled, glowing shaft wherever the pattern says hole.

    Holes are only ever cut in the row the object travels along (j = 0). Scattering them
    over every row turns the floor into a checkerboard with nothing solid left to read the
    object's path against - that was tried, and the path disappeared.

    A hole is the OBJECT's footprint, not a tile: for a rounded cube the object is a few
    percent wider than the pitch, so the two tiles either side are trimmed back to make room
    (`lo`/`hi` below). That is why they are built with an explicit span instead of a size.
    """
    x0, x1, y0, y1 = field
    grout = pitch * (1.0 - SEAM) / 2.0
    half_tile = pitch * SEAM / 2.0
    half_hole = hole / 2.0
    tile_h = pitch * TILE_THICK

    tiles = [unit_box_mesh(f"TileMesh{k}",
                           from_spec(f"Floor{k}", floor_spec,
                                     tint=0.80 + 0.09 * k), bevel=0.012)
             for k in range(TINTS)]
    # The shaft is a shade darker than the floor and rougher: it is lit almost entirely by
    # the glow at the bottom, and a shiny wall there mirrors the emitter into a stripe.
    wall_mesh = unit_box_mesh("ShaftMesh", mat("Shaft", tuple(
        c * 0.35 for c in floor_spec["colour"]), rough=0.75), bevel=0.0)
    # A few glow variants so consecutive holes are not identical. The hue wanders, the
    # strength does not - a dim hole in a row of bright ones reads as a mistake.
    glow_meshes = []
    for k in range(4):
        jitter = [max(0.0, min(1.0, c * (1.0 + RNG.uniform(-0.10, 0.10)) + 0.01))
                  for c in glow_rgb]
        glow_meshes.append(unit_plane_mesh(f"GlowMesh{k}", mat(
            f"Glow{k}", jitter, rough=0.4, emit=glow_strength, emit_rgb=jitter)))

    def is_gap(i):
        return (i % loop_tiles) in gaps

    for i in range(x0, x1 + 1):
        gap_here = is_gap(i)
        cx = i * pitch
        for j in range(y0, y1 + 1):
            cy = j * pitch
            if j == 0 and gap_here:
                continue                                  # the hole itself, built below
            lo_x, hi_x = cx - half_tile, cx + half_tile
            lo_y, hi_y = cy - half_tile, cy + half_tile
            if j == 0:
                if is_gap(i - 1):
                    lo_x = max(lo_x, (i - 1) * pitch + half_hole + grout)
                if is_gap(i + 1):
                    hi_x = min(hi_x, (i + 1) * pitch - half_hole - grout)
            elif abs(j) == 1 and gap_here:
                # sideways neighbour of a hole - trimmed the same way
                if j == 1:
                    lo_y = max(lo_y, half_hole + grout)
                else:
                    hi_y = min(hi_y, -half_hole - grout)
            if hi_x - lo_x < pitch * 0.05 or hi_y - lo_y < pitch * 0.05:
                continue
            k = (abs(i * 73856093) ^ abs(j * 19349663) ^ abs(SEED * 83492791)) % TINTS
            place(tiles[k], ((lo_x + hi_x) / 2.0, (lo_y + hi_y) / 2.0, -tile_h / 2.0),
                  (hi_x - lo_x, hi_y - lo_y, tile_h), f"Tile_{i}_{j}")

        if not gap_here:
            continue
        # ---- the shaft. Its walls start 0.004 of a tile BELOW the floor surface: with both
        # top faces at exactly z = 0 the wall and the tile it slides under are coplanar and
        # Cycles z-fights along every hole edge.
        wt = pitch * WALL_THICK
        top = -pitch * 0.004
        span = hole + 2.0 * wt
        for dx, dy, sx, sy in ((-1, 0, wt, span), (1, 0, wt, span),
                               (0, -1, hole, wt), (0, 1, hole, wt)):
            place(wall_mesh,
                  (cx + dx * (half_hole + wt / 2.0), dy * (half_hole + wt / 2.0),
                   top - depth / 2.0),
                  (sx, sy, depth), f"Shaft_{i}_{dx}_{dy}")
        place(glow_meshes[abs(i) % len(glow_meshes)],
              (cx, 0.0, top - depth + pitch * 0.004), (hole, hole, 1.0), f"Glow_{i}")


def build_far_planes(field, outer, pitch, floor_spec):
    """Four planes filling the floor from the edge of the tile field out to the horizon.

    Tiling the whole visible floor at this pitch would be thousands of objects for ground
    that is 30 metres away and two pixels tall. A frame of four planes around the field
    costs four, cannot cap any hole (every hole is inside the field) and leaves one seam,
    which lands far enough away to read as the tiling losing itself in the distance.
    """
    fx0 = (field[0] - 0.5) * pitch
    fx1 = (field[1] + 0.5) * pitch
    fy0 = (field[2] - 0.5) * pitch
    fy1 = (field[3] + 0.5) * pitch
    ox0, ox1, oy0, oy1 = outer
    # 0.92, not "a bit darker": at this distance the tiles are sub-pixel and average out to
    # their mean tint (0.98) times the fraction of the ground they actually cover (the grout
    # gaps are dark), which is 0.93. Anything else puts a visible band across the far floor
    # exactly where the tiling stops.
    me = unit_plane_mesh("FarMesh", from_spec("FarFloor", floor_spec, tint=0.92))
    rects = [(ox0, fx0, oy0, oy1), (fx1, ox1, oy0, oy1),
             (fx0, fx1, oy0, fy0), (fx0, fx1, fy1, oy1)]
    for n, (a, b, c, d) in enumerate(rects):
        if b - a < pitch * 0.02 or d - c < pitch * 0.02:
            continue
        place(me, ((a + b) / 2.0, (c + d) / 2.0, 0.0), (b - a, d - c, 1.0), f"Far{n}")


# ----------------------------------------------------------------------------- the object


def build_body(body, spec, accent_rgb):
    """The tumbling object, as ONE object so a single pair of keyframes moves all of it."""
    material = from_spec("Object", spec)
    if body["kind"] == "cylinder":
        bpy.ops.mesh.primitive_cylinder_add(
            radius=body["radius"], depth=body["depth"], vertices=64,
            location=(0, 0, 0), rotation=(math.pi / 2.0, 0, 0))
        ob = bpy.context.object
        # The 90 degrees goes into the MESH, not the object: rotation_euler is animated
        # below and would otherwise overwrite the axis and roll the cylinder on its rim.
        bpy.ops.object.transform_apply(location=False, rotation=True, scale=False)
        ob.data.materials.append(material)
        try:
            # Smooth the barrel, keep the end caps and the rim flat. Blender 4.1 removed
            # Mesh.use_auto_smooth and replaced it with this operator (it adds a Smooth by
            # Angle modifier); plain shade_smooth would round the caps into the rim and
            # lose the edge the object reads its size by.
            bpy.ops.object.shade_auto_smooth(angle=math.radians(40.0))
        except Exception:  # noqa: BLE001 - older or newer build: smooth everything instead
            bpy.ops.object.shade_smooth()
        # A plain cylinder rolling on a plain floor looks like it is sliding - there is
        # nothing on it to turn. One bar across each end cap fixes that, in the glow colour
        # so it also ties the object to the holes.
        d = 2.0 * body["radius"]
        marks = []
        bar = mat("Accent", accent_rgb, rough=0.35, emit=0.55, emit_rgb=accent_rgb)
        for side in (-1, 1):
            bpy.ops.mesh.primitive_cube_add(
                size=1, location=(0, side * (body["depth"] / 2.0 + d * 0.004), 0))
            m = bpy.context.object
            m.scale = (d * 0.74, d * 0.012, d * 0.13)
            m.data.materials.append(bar)
            marks.append(m)
        bpy.ops.object.select_all(action="DESELECT")
        for m in marks:
            m.select_set(True)
        ob.select_set(True)
        bpy.context.view_layer.objects.active = ob
        bpy.ops.object.join()
        ob = bpy.context.object
    else:
        bpy.ops.mesh.primitive_cube_add(size=body["side"], location=(0, 0, 0))
        ob = bpy.context.object
        ob.data.materials.append(material)
        bpy.ops.object.modifier_add(type="BEVEL")
        bev = ob.modifiers["Bevel"]
        # For the rounded cube this bevel IS the rolling geometry: the width is the radius
        # the advance per roll was solved from, so it must not be treated as decoration.
        # For the plain cube it is 1.5%, just enough to catch a highlight along the edges
        # so they read against a dark floor. Flat shading either way - a cube's whole
        # appeal is its hard edges.
        bev.width = body["radius"] if body["kind"] == "rounded_cube" else body["side"] * 0.015
        bev.segments = 6 if body["kind"] == "rounded_cube" else 3
        bev.limit_method = "ANGLE"
    ob.name = "Body"
    ob.rotation_mode = "XYZ"
    return ob


# ----------------------------------------------------------------------------- the camera


def solve_shot(body, pitch, depth, poses, view_tiles):
    """Azimuth, pitch, lens and the ONE distance that holds the shot for the whole loop.

    The camera translates and never rotates: it rides the track at a fixed offset, so the
    framing is solved once from a window expressed relative to the aim point rather than
    per frame. Re-solving every frame would make the shot breathe every time the object
    dropped into a hole.

    The downward pitch is floored at half the vertical field of view plus 6 degrees. Below
    that the top of the frame looks out over the horizon and a 9:16 short gets a third of
    empty world background - and it is capped at 62, because looking down on a hole is how
    you stop being able to see that it is a hole.
    """
    mode = ps("camera")
    lens = pf("lens") or (42.0 if mode == "alongside" else 34.0)
    tan_v, _ = half_angles(lens, RES_X, RES_Y)
    deg = pf("camera_pitch_deg") or (42.0 if mode == "alongside" else 33.0)
    deg = min(62.0, max(deg, math.degrees(math.atan(tan_v)) + 6.0))
    cam_pitch = math.radians(deg)
    side = 1.0 if RNG.random() < 0.5 else -1.0            # seeded: which side of the row
    # BOTH modes sit on the diagonal offset, and that is not laziness. A row of tiles is a
    # long thin strip, and in a 9:16 frame there are only two azimuth families that do not
    # waste it: `off` either side of straight-down-the-row, and `off` either side of
    # straight-up-it. Anything else - including the obvious side-on tracking shot, which was
    # what this scene was first built with - lays the strip across the NARROW axis: measured
    # on the default setup, a side-on 74 degrees put the row across the frame at a width to
    # height ratio of 5.2 to 1 and needed the camera 10.4 m back; on the diagonal the row
    # runs corner to corner and the same window fits from 9.4 m.
    off = diagonal_offset(cam_pitch, RES_X, RES_Y)
    if mode == "behind":
        az = math.pi - side * off
        # Aim well ahead of the object: it is travelling away from the lens, so the holes it
        # is about to reach are the far ones and they need the room.
        lead = 0.95 * pitch
    else:
        # Ahead and off to one side, looking back: the object tumbles TOWARD the lens and
        # the hole it is about to drop into is in the foreground between the two.
        az = -side * off
        lead = 0.30 * pitch
    u = Vector((math.cos(az) * math.cos(cam_pitch),
                math.sin(az) * math.cos(cam_pitch),
                math.sin(cam_pitch)))

    top = max(z + rolled_top(body, ang) for _x, z, ang in poses)
    aim_z = (top - depth) / 2.0
    back = 0.42 * view_tiles * pitch
    fwd = 0.58 * view_tiles * pitch
    xs = (-lead - back, -lead + fwd)
    ys = (-1.15 * pitch, 1.15 * pitch)
    zs = (-depth - aim_z, top - aim_z)
    window = [(x, y, z) for x in xs for y in ys for z in zs]
    dist = max(pitch * 2.0,
               solve_distance(window, u, lens, 0.86, 0.90, RES_X, RES_Y))
    return {"az": az, "pitch": cam_pitch, "lens": lens, "u": u, "dist": dist,
            "aim_z": aim_z, "lead": lead, "mode": mode, "top": top,
            "quat": (-u).to_track_quat("-Z", "Y")}


def floor_footprint(cam_pos, quat, lens, cap):
    """Where the four corners of the frame land on z = 0. Used to size the tile field.

    In camera space the frame corners at unit depth are (+-tan_h, +-tan_v, -1); rotated into
    the world they are the four corner rays. A ray that is not pointing downwards cannot hit
    the floor at all, which the pitch floor in solve_shot already rules out - it is clamped
    to `cap` anyway rather than trusted.
    """
    tan_v, tan_h = half_angles(lens, RES_X, RES_Y)
    out = []
    for sx in (-1.0, 1.0):
        for sy in (-1.0, 1.0):
            d = quat @ Vector((sx * tan_h, sy * tan_v, -1.0))
            d.normalize()
            t = min(cap, -cam_pos.z / d.z) if d.z < -1e-4 else cap
            p = cam_pos + d * max(0.0, t)
            out.append((p.x, p.y))
    return out


def build_lights(shot, pitch, energy, glow_rgb):
    """Key, fill, rim and a grazing wash along the floor ahead. All camera-invisible.

    Placed relative to the CAMERA azimuth, so the key stays a three-quarter key whichever
    side the seed put the camera on and whichever camera mode is in use. Power is not a
    taste number: an area light's irradiance falls off as 1/d^2, so each one is 72 W per
    square metre of its own distance (the constant the sibling scenes were exposed at),
    which is what keeps a 0.5 m tile and a 1.6 m tile lit identically.

    They are returned so the caller can key them along the track - a static rig would leave
    the far half of the loop unlit, and this camera travels several metres.
    """
    az = shot["az"]
    rig = [
        # name    az offset  elevation  distance  size  power  colour              aim ahead
        ("Key",      38.0,     46.0,      3.4,    2.4,  1.00, (1.00, 0.97, 0.92),  0.0),
        ("Fill",    -44.0,     22.0,      3.6,    3.0,  0.30, (0.78, 0.86, 1.00),  0.0),
        ("Rim",     165.0,     38.0,      3.0,    2.2,  0.50, glow_rgb,            0.0),
        # The wash is the one that stops a third of a tall frame going dead: it grazes along
        # the floor ahead of the object, which in 9:16 is the whole upper part of the frame.
        ("Wash",      9.0,     15.0,      4.6,    4.5,  0.38, (0.94, 0.95, 1.00),  3.2),
    ]
    lights = []
    for name, dazi, elev, dist, size, power, colour, ahead in rig:
        a, e = az + math.radians(dazi), math.radians(elev)
        off = Vector((math.cos(a) * math.cos(e), math.sin(a) * math.cos(e),
                      math.sin(e))) * (dist * pitch)
        target = Vector((ahead * pitch, 0.0, 0.0))
        bpy.ops.object.light_add(type="AREA", location=off)
        lt = bpy.context.object
        lt.name = name
        lt.data.size = size * pitch
        d = max(0.2, (off - target).length)
        lt.data.energy = 72.0 * d * d * power * energy
        lt.data.color = colour
        lt.rotation_euler = (target - off).to_track_quat("-Z", "Y").to_euler()
        # A light is geometry to Cycles. Leave this out and there is a glowing white
        # rectangle hanging in the shot - it has happened, on camera.
        lt.visible_camera = False
        lights.append((lt, off))
    return lights


# ------------------------------------------------------------------- contacts, measured


def detect_contacts(sc, ob, total, pitch, depth):
    """Step the depsgraph and find the frames the object actually stops falling.

    The motion is authored, but this reads it back the long way round on purpose: every
    frame is stepped, the object's EVALUATED world transform is taken from the depsgraph,
    and a contact is a frame where the centre's downward velocity is arrested. Nothing is
    read out of the formula that produced the motion, so a change to the easing - or an
    interpolation surprise, which is exactly what the LINEAR key setting exists to prevent -
    lands in the sound instead of hiding behind it.

    The bounding box comes from the evaluated object too, so the bevel modifier is included
    and the depth test knows whether the object was down a hole or on the floor.

    Velocity is measured CIRCULARLY (frame 1 against frame N), because the render loops: a
    landing that falls on the seam is a real landing on every pass but the first.
    """
    dg = bpy.context.evaluated_depsgraph_get()
    cz, low = [], []
    for f in range(1, total + 1):
        sc.frame_set(f)
        dg.update()
        ev = ob.evaluated_get(dg)
        m = ev.matrix_world
        cz.append(m.translation.z)
        low.append(min((m @ Vector(c)).z for c in ev.bound_box))

    eps = 0.004 * pitch
    events = []
    for i in range(total):
        v_now = cz[i] - cz[i - 1]                       # i == 0 wraps to the last frame
        v_prev = cz[i - 1] - cz[i - 2]
        if not (v_prev < -eps and v_now > -eps):
            continue                                    # still falling, or never was
        if events and (i + 1) - events[-1]["frame"] < 2:
            continue
        # Where it landed. A hole is a full depth down; anything within a quarter of the
        # depth of the floor line is the floor.
        in_hole = low[i] < -0.25 * depth
        events.append({"frame": i + 1, "speed": abs(v_prev) * FPS,
                       "where": "gap" if in_hole else "tile"})

    if not events:
        return []
    # Self-calibrating: the loudest thing in the loop is the reference, so a scene with no
    # holes at all still produces usable levels and a deep drop is never clipped by a
    # constant somebody guessed. At the defaults a tile landing arrives at about a quarter
    # of the speed of a drop and comes out near 0.35 against 1.0, which is the right split -
    # a tap against a thud - and keeps the taps below the 0.7 the mixer uses to decide an
    # event also earns a debris tail.
    peak = max(e["speed"] for e in events) or 1.0
    out = []
    for e in events:
        s = 0.12 + 0.88 * (e["speed"] / peak) ** 0.9
        out.append({"frame": int(e["frame"]), "kind": "impact",
                    "strength": round(max(0.1, min(1.0, s)), 3),
                    "where": e["where"]})
    return out


# -------------------------------------------------------------------------------- main


def solve_loop_tiles(roll_frames: int, drop_frames: int, every: float) -> tuple:
    """How many tiles one period covers, and why.

    An explicit loop_tiles wins. Otherwise it is solved from `seconds`: a solid tile costs
    roll_frames and a hole costs an extra 2*drop_frames + a hold, and holes arrive every
    `every` tiles, so the average cost of a tile is known before anything is built.
    """
    if int(pf("loop_tiles")) > 0:
        return max(4, min(20, pi("loop_tiles"))), "loop_tiles given"
    per_tile = roll_frames + (2.0 * drop_frames + BOTTOM_HOLD) / max(2.0, every)
    want = int(round((SECONDS * FPS) / max(1.0, per_tile)))
    return max(4, min(20, want)), f"solved from seconds={SECONDS:.1f}"


def main():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    sc = bpy.context.scene
    sc.render.engine = "CYCLES"          # headless EEVEE has no GPU context and crawls
    sc.cycles.samples = SAMPLES
    sc.cycles.use_denoising = True
    print("CYCLES_DEVICE " + use_gpu(sc))
    sc.render.resolution_x, sc.render.resolution_y = RES_X, RES_Y
    sc.render.resolution_percentage = 100
    sc.render.fps = FPS
    sc.frame_start = 1
    # A few hundred instances of a handful of meshes: keeping them resident between frames
    # costs almost nothing and saves the per-frame sync on every one.
    sc.render.use_persistent_data = True
    for look in ("AgX - Punchy", "Punchy", "AgX - Medium High Contrast"):
        try:
            sc.view_settings.look = look
            break
        except Exception:  # noqa: BLE001 - purely cosmetic
            continue
    # LINEAR keys, set BEFORE the first insert. There is a keyframe on every frame and the
    # easing is already baked into the poses, so bezier handles would only add overshoot
    # between them - and overshoot on the frame the object lands is a visible bounce that
    # the contact pass would then dutifully report. Blender 5.2 moved Action.fcurves behind
    # slotted actions, so this can no longer be corrected after the fact.
    bpy.context.preferences.edit.keyframe_new_interpolation_type = "LINEAR"

    sc.world = bpy.data.worlds.new("W")
    sc.world.use_nodes = True
    sc.world.node_tree.nodes["Background"].inputs[0].default_value = (0.012, 0.012, 0.016, 1)

    # ---- the numbers everything else is solved from
    pitch = pf("tile_size")
    roll_frames = pi("roll_frames")
    drop_frames = pi("drop_frames")
    every = pf("gap_every")
    loop_tiles, why = solve_loop_tiles(roll_frames, drop_frames, every)
    shape = ps("shape")
    body = solve_shape(shape, pitch, loop_tiles)
    hole = body["width"] * 1.01      # the object's own footprint, plus a hair of clearance
    depth = pf("gap_depth") * pitch
    gaps = gap_pattern(loop_tiles, every)
    floor_spec = MATERIALS[ps("floor_material")]
    obj_spec = MATERIALS[ps("object_material")]
    glow_rgb = GLOWS[ps("glow_colour")]
    if obj_spec.get("trans"):
        sc.cycles.max_bounces = 8
        sc.cycles.transmission_bounces = 6

    poses = build_motion(body, pitch, gaps, loop_tiles, roll_frames, drop_frames, depth)
    total = len(poses)
    sc.frame_end = total
    track = camera_track(poses, pitch, loop_tiles, window=2 * roll_frames)
    shot = solve_shot(body, pitch, depth, poses, pf("view_tiles"))
    u, dist, lead, aim_z = shot["u"], shot["dist"], shot["lead"], shot["aim_z"]

    def aim_at(i):
        return Vector((track[i] + lead, 0.0, aim_z))

    # ---- the object, keyed one frame at a time. The pose is a closed-form function of the
    # frame, so the loop is exact by construction rather than by trimming afterwards.
    ob = build_body(body, obj_spec, glow_rgb)
    for i, (x, z, ang) in enumerate(poses):
        ob.location = (x, 0.0, z)
        ob.rotation_euler = (0.0, ang, 0.0)
        ob.keyframe_insert("location", frame=i + 1)
        ob.keyframe_insert("rotation_euler", frame=i + 1)

    # ---- camera: translates only, never rotates. Nothing pans, so nothing can wobble.
    bpy.ops.object.camera_add(location=(0, 0, 0))
    cam = bpy.context.object
    cam.data.lens = shot["lens"]
    cam.data.clip_end = max(500.0, dist * 40.0)
    cam.rotation_euler = shot["quat"].to_euler()
    sc.camera = cam
    lights = build_lights(shot, pitch, pf("light_energy"), glow_rgb)
    for i in range(total):
        a = aim_at(i)
        cam.location = a + u * dist
        cam.keyframe_insert("location", frame=i + 1)
        for lt, off in lights:
            lt.location = a + off
            lt.keyframe_insert("location", frame=i + 1)

    # ---- the floor is built LAST and sized from where the camera actually ended up: the
    # four corner rays of the frame are intersected with z = 0 at the start, middle and end
    # of the loop, and the tile field covers their union. Guessing an extent either leaves
    # the far edge of the floor in shot or tiles half a hectare nobody sees.
    foot = []
    for i in (0, total // 2, total - 1):
        a = aim_at(i)
        foot += floor_footprint(a + u * dist, shot["quat"], shot["lens"], 400.0 * pitch)
        foot.append((a.x, a.y))
    fx = [p[0] for p in foot]
    fy = [p[1] for p in foot]
    outer = (min(fx) - pitch, max(fx) + pitch, min(fy) - pitch, max(fy) + pitch)
    tx0 = max(int(math.floor(min(fx) / pitch)) - 1,
              int(math.floor(track[0] / pitch)) - 8)
    tx1 = min(int(math.ceil(max(fx) / pitch)) + 1,
              int(math.ceil(track[-1] / pitch)) + FAR_TILES)
    ty0 = max(int(math.floor(min(fy) / pitch)) - 1, -LAT_TILES)
    ty1 = min(int(math.ceil(max(fy) / pitch)) + 1, LAT_TILES)
    # Whatever the camera does, the tiles the object actually travels over have to exist.
    field = (min(tx0, -2), max(tx1, loop_tiles + 2), min(ty0, -2), max(ty1, 2))
    build_floor(field, gaps, loop_tiles, pitch, hole, depth, floor_spec,
                glow_rgb, pf("glow_strength"))
    build_far_planes(field, outer, pitch, floor_spec)

    events = detect_contacts(sc, ob, total, pitch, depth)
    hits = sum(1 for e in events if e["where"] == "gap")
    info = (f"shape={shape} {body['note']} pitch={pitch:.3f} hole={hole:.3f} "
            f"loop_tiles={loop_tiles} ({why}) gaps={gaps} depth={depth:.2f} "
            f"frames={total} ({total / float(FPS):.2f}s) camera={shot['mode']} "
            f"lens={shot['lens']:.0f} pitch={math.degrees(shot['pitch']):.0f}deg "
            f"dist={dist:.2f} field={field} events={len(events)} (gap {hits}) seed={SEED}")
    print("SCENE_INFO " + info)
    if total / float(FPS) > 12.0:
        print(f"SCENE_NOTE one period is {total / float(FPS):.1f}s - this is a long render "
              "for a loop; lower loop_tiles or roll_frames if that was not deliberate.")
    if not events:
        print("SCENE_NOTE no contacts were detected - the clip will be silent.")

    if PREVIEW:
        n = max(1, min(total, PREVIEW))
        sc.frame_set(n)
        sc.render.image_settings.file_format = "PNG"
        sc.render.image_settings.color_mode = "RGB"
        sc.render.filepath = f"{OUT}/preview.png"
        bpy.ops.render.render(write_still=True)
        print(f"SCENE_OK preview={n} {info}")
        return

    with open(f"{OUT}/impacts.json", "w", encoding="utf-8") as fh:
        json.dump({"fps": FPS, "loop": True, "loop_frames": total, "events": events,
                   "shape": shape, "tile": round(pitch, 4), "hole": round(hole, 4),
                   "loop_tiles": loop_tiles, "gaps": gaps, "seed": SEED}, fh, indent=1)

    sc.render.image_settings.file_format = "PNG"     # Blender 5.2 has no FFMPEG output
    sc.render.image_settings.color_mode = "RGB"
    sc.render.filepath = f"{OUT}/frame_"
    bpy.ops.render.render(animation=True)
    print(f"SCENE_OK {info}")


if bpy is not None:
    main()




