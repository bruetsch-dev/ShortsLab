"""bounce_ladder - the same drop rendered once per bounciness value, measured against a gauge.

    blender -b -noaudio -P bounce_ladder.py -- '{"restitution":0.55,"label":"55%",
                                                 "drop_object":"ball","out_dir":"..."}'

This is a HAND-MAINTAINED parametric scene: a model picks the scene and fills in PARAMS, it
never writes geometry. Every number below is either measured, solved at build time, or
explained where a reader would otherwise have to reverse-engineer it.

The format is "1kg / 10kg / 50kg" applied to elasticity: the app renders this file once per
value of `restitution` and joins the takes, each captioned with its own number. One take
also stands on its own - it is a complete drop with a gauge and a caption.

Why it is built this way
------------------------
* THE FRAME MAY NOT DEPEND ON THE SWEPT VALUE. This is the rule the whole file is arranged
  around. If each take solved its camera from its own simulated trajectory, the 20% take
  would be framed tight on a dead ball and the 90% take pulled back, and the two would look
  like the SAME bounce - which destroys the only thing the video is about. So the camera is
  solved from the drop height and the object size alone, which are identical across the
  sweep, and the simulation is never allowed near it. The seed is identical across takes
  too, for the same reason: the app varies one parameter, so everything else must not move.
* BULLET MULTIPLIES RESTITUTION, it does not average it or take the maximum. The combined
  bounciness of a contact is body0.restitution * body1.restitution, and Blender gives every
  new rigid body a restitution of 0.0, so a ball with restitution 0.9 dropped on a default
  floor does not bounce AT ALL. Every passive surface here is therefore pinned to 1.0, which
  makes the swept value the true coefficient of the contact. Friction combines the same way
  (product), so the object carries friction 1.0 and the surface carries the real number.
* A COEFFICIENT OF RESTITUTION IS A VELOCITY RATIO, so the rebound HEIGHT goes as its
  square: e=0.9 comes back to 81% of the drop, e=0.55 to 30%, e=0.2 to 4%. That squaring is
  what makes the format work - the takes look nothing like each other - and it is why the
  low end of a sweep is a thud rather than a bounce. It is spelled out in the note on
  `restitution` because the model choosing the values cannot see the shot.
* THE GAUGE TICKS ARE PARALLAX-CORRECTED, and the correction is enormous - this is not a
  refinement, it is the difference between a gauge and a lie. The ticks live on the backdrop
  a metre and a half behind the drop, and every ray from a downward-tilted lens keeps
  descending on its way there, so a tick built at its true height projects ABOVE the object
  it is measuring. Measured offline on the default parameters: the 25% tick has to sit 0.59m
  LOWER on the wall than 25% of the drop, and a tick built naively would have the object
  reading a fifth of a drop short. Each one is placed at the backdrop height that projects
  onto the same image row as the real height at the drop plane (see `wall_z_matching`), which
  brings the residual to under a hundredth of a pixel.
* THE PEAK LINE IS MEASURED, NOT DRAWN. After the simulation has been stepped, the apex of
  the first rebound is known, and a bright line plus its percentage pops in on exactly that
  frame. It is the frame the object is actually at the top, so the number cannot lie.
* Impact frames in impacts.json also come from the stepped simulation - every local minimum
  of the object's height near the floor, with the strength taken from the descent speed that
  was recorded on the way in. A sound placed on an estimated frame is heard as a mistake.
"""

import json
import math
import random as _random
import sys

try:
    import bpy
    import mathutils
except ModuleNotFoundError:      # outside Blender the module still imports, so the app can
    bpy = mathutils = None       # read PARAMS without starting a 3D session

# --------------------------------------------------------------------------- parameters
# Read by the model that chooses this scene. It has never seen the shot and cannot open the
# file, so every note has to stand on its own. "choices" for enums, "range": [lo, hi] for
# numbers - numbers are clamped at read time, because a value outside its range is how a
# scene ends up rendering an empty frame for twenty minutes.
PARAMS = {
    "restitution": {
        "range": [0.0, 0.98], "default": 0.55,
        "note": "THE POINT OF THE SCENE, and the value the app sweeps. Coefficient of "
                "restitution of the contact: the fraction of SPEED the object keeps when it "
                "hits. Rebound HEIGHT is the square of it - 0.2 comes back to 4% of the drop "
                "(a dead thud), 0.55 to 30%, 0.9 to 81%, 0.98 to 96% (near perpetual). Pick "
                "sweep values that are far apart in the SQUARE, not in the number: 0.2/0.55/"
                "0.9 reads as thud / half / almost-all, while 0.6/0.7/0.8 all look the same. "
                "Above 0.98 the solver starts handing back more energy than it was given and "
                "the object climbs above its own drop line, which reads as a bug.",
    },
    "seed": {
        "range": [0, 9999], "default": 0,
        "note": "Changes the release: a few millimetres of horizontal offset, the axis and "
                "rate of the slow spin the object carries when it lets go, and the exact "
                "arrangement and sizes of a cluster. A cube landing on a different corner "
                "bounces somewhere completely different, so this is what stops two videos of "
                "the same idea being identical. It is held FIXED across the takes of one "
                "sweep - only restitution changes there - so the comparison stays honest.",
    },

    # ---- the caption -----------------------------------------------------------------
    "label_unit": {
        "choices": ["percent", "value", "none"], "default": "percent",
        "note": "How the take is captioned when the app has not supplied its own text. "
                "'percent' writes the restitution as 20% / 55% / 90%, which is what a viewer "
                "can read at phone size. 'value' writes the bare 0.55. 'none' leaves the "
                "frame clean. An explicit caption from the app always wins over this.",
    },
    "label_title": {
        "default": "BOUNCE",
        "note": "Small word set above the number, e.g. 'BOUNCE', 'ELASTICITY', 'RUBBER'. "
                "Empty means the number alone. Keep it to one short word - it is rendered "
                "at about half the height of the number and anything longer stops being "
                "readable on a phone.",
    },

    # ---- what is dropped -------------------------------------------------------------
    "drop_object": {
        "choices": ["ball", "cube", "cluster"], "default": "ball",
        "note": "'ball' is the clean read: it bounces straight up and the apex is "
                "unambiguous, which is what a comparison wants. 'cube' lands on a corner and "
                "tumbles, so the takes are livelier but the height is harder to read. "
                "'cluster' drops a handful of small spheres together - the most satisfying at "
                "the bouncy end of a sweep, where it turns into popcorn, and the most inert "
                "at the dead end, where it just piles up.",
    },
    "object_size": {
        "range": [0.08, 0.70], "default": 0.30,
        "note": "Diameter of the ball, edge of the cube, or diameter of the whole bunch for "
                "'cluster', in metres. Bounce height does not depend on it or on mass at all "
                "- this is purely how big the thing reads in frame. Roughly a tenth of "
                "drop_height is a good silhouette; much smaller and it is a dot on a phone.",
    },
    "cluster_count": {
        "range": [4, 40], "default": 14,
        "note": "drop_object='cluster' only: how many pieces are in the bunch. Piece size is "
                "derived so the bunch keeps about the same total volume however many there "
                "are. Above ~25 the individual bounces stop being readable and it becomes a "
                "spray, which is a fine look but a worse measurement.",
    },
    "object_material": {
        "choices": ["rubber", "steel", "marble", "wood", "glass", "plastic"],
        "default": "rubber",
        "note": "LOOK and density only - it does NOT set the bounce, `restitution` does, and "
                "mass has no effect on rebound height whatsoever. Choose it for contrast "
                "against the surface: bright rubber or plastic on a dark plate reads best at "
                "phone size. 'glass' is translucent and roughly doubles render time.",
    },
    "drop_height": {
        "range": [0.80, 6.00], "default": 3.00,
        "note": "Height of the CENTRE of the object above the floor at release, in metres. "
                "It sets the whole picture: the camera distance is solved from it and the "
                "gauge ticks are fractions of it, so it is the one number that decides how "
                "the shot is framed. It also sets the pace - the fall alone takes "
                "sqrt(2h/g) seconds, 0.78s at 3m - so a tall drop spends more of the clip "
                "falling and shows fewer bounces.",
    },
    "surface": {
        "choices": ["steel_plate", "concrete", "marble", "wood", "glass", "rubber_mat"],
        "default": "steel_plate",
        "note": "What it lands on. Sets colour, finish and friction only: the bounce belongs "
                "to `restitution` and the floor is pinned to a restitution of 1.0 so the "
                "swept number is the true coefficient. Polished ones ('steel_plate', "
                "'marble', 'glass') give a soft reflection under the object, which is worth a "
                "lot on a dark floor; 'rubber_mat' is the flattest and grippiest.",
    },

    # ---- the rig ---------------------------------------------------------------------
    "gauge": {
        "choices": [True, False], "default": True,
        "note": "Ticks on the backdrop at 25/50/75/100% of the drop, labelled, with the 100% "
                "line through the object as it hangs. This is what turns three takes into a "
                "comparison instead of three drops - leave it on for a sweep. Forced off if "
                "`backdrop` is off, because the ticks would be floating in black.",
    },
    "peak_marker": {
        "choices": [True, False], "default": True,
        "note": "A bright line that pops in at the top of the FIRST rebound, on the frame the "
                "object is really there, carrying the measured height as a percentage of the "
                "drop. It stays for the rest of the take, so the take ends on a readable "
                "result rather than on a still floor. Note it is a different number from the "
                "caption: the caption is the restitution, this is its square. Forced off if "
                "`backdrop` is off - it is drawn on the wall and has nothing to sit on.",
    },
    "backdrop": {
        "choices": [True, False], "default": True,
        "note": "A lit wall behind the drop, sized to cover the frame. Keep it on: a drop is "
                "mostly empty air, and without it the top two thirds of a vertical frame are "
                "black, which is the single fastest way to make a short look cheap.",
    },
    "catch_rim": {
        "choices": [True, False], "default": True,
        "note": "A low arena rim on the floor, open towards the camera so the contact is "
                "never hidden. Mostly it exists for 'cluster', where bouncy pieces would "
                "otherwise scatter out of frame, but it also gives the empty floor a shape to "
                "read. It is the same on every take, so it cannot bias a comparison.",
    },
    "hold_seconds": {
        "range": [0.0, 1.20], "default": 0.35,
        "note": "How long the object hangs still at the drop line before it is let go. It is "
                "not dead time: it is what makes the viewer register the height the fall "
                "starts from, which is the whole reference the video is measured against. "
                "The object turns slowly during it and carries that spin into the drop.",
    },

    # ---- camera ----------------------------------------------------------------------
    "lens": {
        "range": [24.0, 70.0], "default": 42.0,
        "note": "Focal length in mm. The distance is solved from it, never set - short reads "
                "as dramatic and makes the floor sweep away underneath, long flattens the "
                "scene and makes the gauge easier to read. 42 is the compromise.",
    },
    "camera_pitch_deg": {
        "range": [2.0, 32.0], "default": 10.0,
        "note": "How far above horizontal the camera sits, looking down. Small on purpose: "
                "this shot is ABOUT height, and every degree of downward angle foreshortens "
                "it. Below ~5 the floor becomes an edge-on line and the bottom of the frame "
                "goes dead; above ~20 a 90% bounce and a 50% bounce start to look alike.",
    },
    "camera_yaw_deg": {
        "range": [-25.0, 25.0], "default": 0.0,
        "note": "Swing around the drop. 0 is dead-on, which is deliberate here rather than "
                "lazy: square to the backdrop the gauge ticks project as exactly horizontal "
                "lines and can be read against the object. Yaw them and the ticks slant "
                "slightly, so use small values, and only for a scene without the gauge.",
    },
    "fill": {
        "range": [0.60, 0.95], "default": 0.86,
        "note": "How much of the frame the action is allowed to occupy. It scales all three "
                "framing limits at once - the floor at the bottom, the drop line below the "
                "caption band at the top, and the arena width at the sides. Do not try to "
                "set a camera distance; there is none to set.",
    },
    "camera_push": {
        "range": [0.0, 0.20], "default": 0.05,
        "note": "A slow push in over the take, as a fraction of the camera distance. The "
                "distance is solved for the END of the push, so nothing can leave the frame "
                "on the way. Small values only - this is a lab shot, and a push that reads "
                "as a move takes attention off the thing being measured.",
    },

    # ---- length and events -----------------------------------------------------------
    "max_events": {
        "range": [8, 120], "default": 40,
        "note": "Cap on how many impacts are written to impacts.json. A bouncy cluster really "
                "does produce hundreds of contacts, but the mixer opens one audio input per "
                "event, so the quietest are dropped and simultaneous ones are merged into "
                "one louder hit.",
    },
    "seconds": {
        "range": [3.0, 8.0], "default": 5.0,
        "note": "Length of ONE take. 5s at a 3m drop covers the hold, the fall and the first "
                "few bounces. Longer only helps the bouncy end of a sweep: at restitution 0.2 "
                "the object is already still after about two seconds, and the rest of the "
                "take is the peak line and the caption sitting there as a result card.",
    },

    # ---- supplied by the runner, not by the model choosing the scene -----------------
    "label": {
        "default": "",
        "note": "Caption for this take, supplied by the app when it sweeps. If it is empty, "
                "or is just the swept number itself, the scene writes its own from "
                "`label_unit` instead.",
    },
    "out_dir": {"default": "//out", "note": "Where frames, preview.png and impacts.json go."},
    "res_x": {"default": 1080, "note": "Supplied by the runner - leave unset."},
    "res_y": {"default": 1920, "note": "Supplied by the runner - leave unset (9:16)."},
    "samples": {"default": 24, "note": "Supplied by the runner - Cycles samples per pixel."},
    "fps": {"default": 30, "note": "Supplied by the runner - leave unset."},
    "preview_frame": {
        "default": 0,
        "note": "If greater than zero, simulate up to this frame, render it alone to "
                "preview.png and stop. Use a frame after the first bounce - an establishing "
                "frame of an object hanging still says nothing about the take.",
    },
}

# The app renders this file once per value and joins the takes (physics_mode/library.py
# reads this literal). Restitution is the only parameter worth sweeping here: it is the one
# the scene exists to show, and it is the one whose effect SQUARES, so three values that
# look mild on paper produce three completely different videos. The unit is empty because
# the scene captions itself - `label_unit` turns 0.2 into "20%".
SWEEP = {"param": "restitution", "values": [0.2, 0.55, 0.9], "unit": ""}

argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
P = json.loads(argv[0]) if argv else {}


def _default(key):
    return PARAMS[key]["default"]


def pf(key) -> float:
    """A number from P, clamped to the range PARAMS declares for it."""
    try:
        v = float(P.get(key, _default(key)))
    except (TypeError, ValueError):
        v = float(_default(key))
    rng = PARAMS[key].get("range")
    if rng:
        v = max(float(rng[0]), min(float(rng[1]), v))
    return v


def pi(key) -> int:
    return int(round(pf(key)))


def ps(key) -> str:
    """A choice from P, falling back to the default if the model invented a value."""
    v = str(P.get(key, _default(key)) or _default(key)).strip().lower()
    choices = PARAMS[key].get("choices")
    if choices and v not in [str(c).lower() for c in choices]:
        return str(_default(key)).lower()
    return v


def pb(key) -> bool:
    v = P.get(key, _default(key))
    if isinstance(v, str):
        return v.strip().lower() not in ("", "0", "false", "no", "off")
    return bool(v)


OUT = str(P.get("out_dir") or _default("out_dir"))
RES_X = int(P.get("res_x", _default("res_x")) or 1080)
RES_Y = int(P.get("res_y", _default("res_y")) or 1920)
SAMPLES = int(P.get("samples", _default("samples")) or 24)
FPS = max(1, int(P.get("fps", _default("fps")) or 30))
SECONDS = pf("seconds")
PREVIEW = int(P.get("preview_frame", 0) or 0)

SEED = int(P.get("seed", 0) or 0)
_RNG = _random.Random(SEED)

RESTITUTION = pf("restitution")
OBJECT = ps("drop_object")
OBJ_SIZE = pf("object_size")
CLUSTER_N = pi("cluster_count")
OBJ_MAT = ps("object_material")
SURFACE = ps("surface")
DROP_H = pf("drop_height")
GAUGE = pb("gauge") and pb("backdrop")      # ticks with no wall would be floating in black
PEAK = pb("peak_marker") and pb("backdrop")
BACKDROP = pb("backdrop")
RIM = pb("catch_rim")
HOLD_S = pf("hold_seconds")
LENS = pf("lens")
PITCH = math.radians(pf("camera_pitch_deg"))
YAW = math.radians(pf("camera_yaw_deg"))
FILL = pf("fill")
PUSH = pf("camera_push")
MAX_EVENTS = pi("max_events")

G = 9.81

# Framing limits, in units of the half frame, all scaled by `fill`. They are three separate
# numbers rather than one because the top of this frame is not free: the caption lives
# there. See build_label() for where 0.70 and 0.845 come from.
#
# The ceilings on each are not decoration. Checked by projecting the solved camera offline:
# at fill=0.95 the unclamped numbers put the near corners of the arena floor at -1.035 of
# the half frame - outside the picture - and the drop line at 0.608, which is inside the
# caption's own 0.604..0.796 band. A `fill` above about 0.9 therefore stops buying anything
# and starts costing the shot, so it is capped here instead of in the range, where a model
# reading "up to 0.95" would have no way of knowing.
TOP = min(FILL * 0.64, 0.58)     # the drop line may not project above this: caption at 0.70
BOT = min(FILL * 1.09, 0.96)     # the arena floor must stay above this (measured downwards)
SIDE = min(FILL * 1.07, 0.96)    # the arena width must fit inside this

# How far sideways the action is allowed to be planned for, as a fraction of the drop
# height. This is what the arena rim radius and the frame width are built from, and it is
# deliberately a CONSTANT per object kind rather than something measured from the
# simulation - a value measured per take would frame each take differently and wreck the
# comparison. A ball dropped square barely moves sideways; a cube walks off its corners;
# a cluster sprays, which is why it gets the rim.
SPREAD = {"ball": 0.26, "cube": 0.32, "cluster": 0.40}

# colour, roughness, metallic, transmission, density kg/m3
OBJECT_MATERIALS = {
    "rubber":  ((0.86, 0.15, 0.09, 1), 0.62, 0.0, 0.0, 1100),
    "steel":   ((0.60, 0.62, 0.68, 1), 0.22, 1.0, 0.0, 7800),
    "marble":  ((0.88, 0.87, 0.82, 1), 0.16, 0.0, 0.0, 2700),
    "wood":    ((0.56, 0.35, 0.16, 1), 0.62, 0.0, 0.0, 700),
    "glass":   ((0.74, 0.88, 0.92, 1), 0.05, 0.0, 0.88, 2500),
    "plastic": ((0.10, 0.58, 0.95, 1), 0.34, 0.0, 0.0, 950),
}
# colour, roughness, metallic, friction. None is anywhere near black: a third of a 9:16
# frame is floor, and a black floor is a third of the video doing nothing.
SURFACES = {
    "steel_plate": ((0.150, 0.155, 0.175, 1), 0.26, 0.85, 0.45),
    "concrete":    ((0.190, 0.188, 0.180, 1), 0.90, 0.00, 0.90),
    "marble":      ((0.320, 0.315, 0.300, 1), 0.13, 0.00, 0.35),
    "wood":        ((0.200, 0.130, 0.065, 1), 0.62, 0.00, 0.70),
    "glass":       ((0.100, 0.125, 0.140, 1), 0.04, 0.00, 0.25),
    "rubber_mat":  ((0.105, 0.105, 0.115, 1), 0.86, 0.00, 1.00),
}
BACKDROP_COLOUR = (0.075, 0.077, 0.088, 1)
TICK_COLOUR = (0.62, 0.64, 0.70, 1)
PEAK_COLOUR = (0.16, 1.00, 0.48, 1)


# ------------------------------------------------------------------------------ helpers


def use_gpu(sc) -> str:
    """Point Cycles at the GPU. OptiX first - RTX cards have the ray-tracing cores - then
    CUDA. On CPU a 150-frame take at 1080x1920 runs for hours, and a sweep is three of
    them, which is not a mode anyone would use."""
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


def _set(node, names, value):
    """Set the first socket that exists. Principled's socket names have moved twice
    (Transmission -> Transmission Weight, Emission -> Emission Color), and a scene that
    dies on a KeyError renders nothing at all."""
    for n in names:
        if n in node.inputs:
            node.inputs[n].default_value = value
            return True
    return False


def mat(name, rgba, rough=0.6, metal=0.0, trans=0.0, emit=0.0):
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    b = m.node_tree.nodes["Principled BSDF"]
    _set(b, ("Base Color",), rgba)
    _set(b, ("Roughness",), rough)
    _set(b, ("Metallic",), metal)
    if trans:
        _set(b, ("Transmission Weight", "Transmission"), trans)
        _set(b, ("IOR",), 1.45)
    if emit:
        _set(b, ("Emission Color", "Emission"), rgba)
        _set(b, ("Emission Strength",), emit)
    return m


def box(name, size, loc, material, rot=(0, 0, 0)):
    """A box of exactly `size` metres at `loc`.

    primitive_cube_add(size=1) already spans ONE unit, so the scale below IS the dimension,
    not half of it - that mistake cost a full rebuild once. The scale is applied straight
    away so the mesh really is that size: a rigid body takes its collision shape from the
    object, and an unapplied non-uniform scale behaves differently in the solver than it
    looks in the viewport.
    """
    bpy.ops.mesh.primitive_cube_add(size=1, location=loc, rotation=rot)
    ob = bpy.context.object
    ob.name = name
    ob.scale = size
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    ob.data.materials.append(material)
    return ob


def passive(ob, friction):
    """Make `ob` an immovable surface the drop can bounce off.

    restitution 1.0 is not a decoration and not a taste choice. Bullet's combined
    restitution for a contact is body0.restitution * body1.restitution, so whatever the
    floor carries multiplies the swept value. At Blender's default of 0.0 the ball does not
    bounce at all no matter what `restitution` says; at 1.0 the swept number IS the
    coefficient of the contact, which is the only version where the caption is true.
    Friction combines the same way, so the surface carries the real coefficient and the
    dropped object carries 1.0.
    """
    bpy.ops.rigidbody.object_add(type="PASSIVE")
    rb = ob.rigid_body
    rb.collision_shape = "BOX"
    rb.friction = friction
    rb.restitution = 1.0
    rb.use_margin = True
    # NOT the 0.04 default. A 0.30m ball inflated by 4cm bounces off a surface 4cm above
    # the floor it can be seen touching, and on a lit plate with a contact reflection that
    # gap is plainly visible. Not 0.0 either - Bullet wants a sliver for stable contacts.
    rb.collision_margin = 0.002
    return ob


def smoothstep(x: float) -> float:
    x = max(0.0, min(1.0, x))
    return x * x * (3.0 - 2.0 * x)


# ------------------------------------------------------------------------- the geometry


def cluster_layout():
    """(piece radius, lattice slots) for the cluster. Deterministic - no RNG in here.

    The piece radius is SOLVED from the count, not picked as a fraction of the bunch. N
    spheres of radius r sitting on a lattice of pitch 2.25r fill a bunch of radius R when
    (R - r) / 2.25r = (3N/4pi)^(1/3), so r = R / (1 + 2.25c). Picked instead as a fixed
    fraction of R, the default bunch of fourteen came out with room for exactly ONE piece
    and the scene quietly dropped the other thirteen - a "cluster" that was a single ball.
    The closed form is only an estimate of a lattice count, so it is checked and the pieces
    shrunk until they genuinely fit.

    A lattice rather than rejection sampling: two spheres that start interpenetrating are an
    explosion on frame one, and sampling a tight bunch at random can fail to place them all
    however long it is given. The pitch of 2.25r against the largest piece the jitter can
    produce (1.05r) leaves 0.15r of air between every pair.
    """
    bunch_r = OBJ_SIZE * 0.5
    c = (3.0 * max(1, CLUSTER_N) / (4.0 * math.pi)) ** (1.0 / 3.0)
    piece_r = max(0.008, bunch_r / (1.0 + 2.25 * c))

    def lattice(r):
        step = r * 2.25
        out = []
        k = int(math.ceil(bunch_r / step)) + 1
        for ix in range(-k, k + 1):
            for iy in range(-k, k + 1):
                for iz in range(-k, k + 1):
                    p = mathutils.Vector((ix * step, iy * step, iz * step))
                    if p.length + r * 1.05 <= bunch_r + 1e-6:
                        out.append(p)
        return out

    slots = lattice(piece_r)
    for _ in range(8):
        if len(slots) >= CLUSTER_N:
            break
        piece_r *= 0.88
        slots = lattice(piece_r)
    return piece_r, slots


def piece_radius() -> float:
    """Radius of ONE dropped piece - half the object for a ball or cube, one marble for a
    cluster. One source of truth: the framing, the gauge zero and the geometry all read it,
    and a cluster whose pieces came out smaller than first estimated must not leave the
    gauge measuring from a height nothing ever rests at."""
    return cluster_layout()[0] if OBJECT == "cluster" else OBJ_SIZE * 0.5


def rest_height(piece_r: float) -> float:
    """Height of the object's CENTRE when it is lying still on the floor.

    Everything the gauge shows is measured between this and the release height, so it is
    the zero of the scale. A cube resting on a corner sits higher than this; the ticks are
    drawn for a cube resting flat, which is where it ends up.
    """
    return piece_r


def build_floor(surf):
    """A thick slab whose TOP face is exactly z=0, not a plane.

    A plane gets a convex-hull collision shape whose margin has to be fought; a slab takes
    an exact BOX shape, and from a low camera it has an edge, so the floor reads as a plate
    rather than as the absence of anything.
    """
    colour, rough, metal, friction = surf
    ob = box("Floor", (80.0, 80.0, 1.0), (0.0, 0.0, -0.5),
             mat("FloorMat", colour, rough=rough, metal=metal))
    return passive(ob, friction)


def build_rim(radius: float, height: float, surf, gap_dir: float):
    """A low arena rim, OPEN towards the camera.

    A closed ring would put its near wall across the one thing the shot is about - the
    moment of contact - so the 100 degrees facing the lens are left out. The rest keeps a
    bouncy cluster inside the frame the camera was solved for, and gives the empty floor an
    ellipse to read, which is worth as much again in a frame that is mostly air.

    `gap_dir` is the azimuth the CAMERA is at, not a constant: with a yawed camera a gap
    pinned to -Y is no longer the part of the rim in front of the lens, and the near wall
    comes back across the contact - which is the whole reason the gap exists.
    """
    colour, rough, metal, friction = surf
    m = mat("RimMat", (colour[0] * 1.25, colour[1] * 1.25, colour[2] * 1.25, 1),
            rough=min(0.95, rough + 0.12), metal=metal)
    n = 36
    seg = 2.0 * math.pi / n
    thick = max(0.02, radius * 0.055)
    built = []
    for i in range(n):
        a = i * seg
        off = a - gap_dir
        delta = math.degrees(math.atan2(math.sin(off), math.cos(off)))
        if abs(delta) < 50.0:
            continue
        built.append(passive(box(f"Rim{i:02d}",
                                 (radius * seg * 1.08, thick, height),
                                 (radius * math.cos(a), radius * math.sin(a), height / 2.0),
                                 m, rot=(0, 0, a + math.pi / 2.0)), friction))
    return built


def build_object(release_z: float, obj_spec):
    """Build what gets dropped and hold it at the drop line until it is released.

    Returns (bodies, piece_radius, release_frame). Every body is KINEMATIC for the hold and
    dynamic after it, which is how the drop starts from rest at a known height: a body that
    is simply placed and switched on has whatever velocity the solver's first step gives
    it, and the drop height in the caption stops being the drop height in the picture.

    The hold is also where the seed gets in. The object turns slowly on a seeded axis while
    it hangs, and Bullet works out the angular velocity of a kinematic body from the motion
    it was given, so it carries that spin into the fall - the same mechanism the wrecking
    ball uses to arrive swinging. A cube that hits on a different corner bounces somewhere
    entirely different, which is the whole point of having a seed.
    """
    colour, rough, metal, trans, density = obj_spec
    hold_frames = int(round(HOLD_S * FPS))
    bodies = []

    # Millimetres, not centimetres: enough to change which corner a cube lands on, far too
    # little to move the object inside a frame that was solved for a fixed box.
    jx = _RNG.uniform(-0.012, 0.012) * DROP_H
    jy = _RNG.uniform(-0.012, 0.012) * DROP_H
    axis = mathutils.Vector((_RNG.uniform(-1, 1), _RNG.uniform(-1, 1),
                             _RNG.uniform(-1, 1)))
    if axis.length < 1e-6:
        axis = mathutils.Vector((1.0, 0.0, 0.0))
    axis.normalize()

    if OBJECT == "cluster":
        piece_r, slots = cluster_layout()
        _RNG.shuffle(slots)
        if not slots:
            slots = [mathutils.Vector((0.0, 0.0, 0.0))]
        mats = [mat(f"Piece{i}", (min(1.0, colour[0] * f), min(1.0, colour[1] * f),
                                  min(1.0, colour[2] * f), 1), rough=rough, metal=metal,
                    trans=trans)
                for i, f in enumerate((0.72, 0.86, 1.0, 1.16, 1.34))]
        for i in range(min(CLUSTER_N, len(slots))):
            p = slots[i]
            r = piece_r * _RNG.uniform(0.88, 1.05)
            bpy.ops.mesh.primitive_uv_sphere_add(
                radius=r, segments=28, ring_count=14,
                location=(jx + p.x, jy + p.y, release_z + p.z))
            ob = bpy.context.object
            ob.name = f"Piece{i:02d}"
            ob.data.materials.append(mats[i % len(mats)])
            bpy.ops.object.shade_smooth()
            bpy.ops.rigidbody.object_add(type="ACTIVE")
            rb = ob.rigid_body
            rb.collision_shape = "SPHERE"
            rb.mass = max(0.01, density * (4.0 / 3.0) * math.pi * r ** 3)
            bodies.append(ob)
    elif OBJECT == "cube":
        piece_r = OBJ_SIZE * 0.5
        m = mat("Cube", colour, rough=rough, metal=metal, trans=trans)
        bpy.ops.mesh.primitive_cube_add(size=OBJ_SIZE, location=(jx, jy, release_z))
        ob = bpy.context.object
        ob.name = "Cube"
        ob.data.materials.append(m)
        bpy.ops.object.modifier_add(type="BEVEL")
        ob.modifiers["Bevel"].width = OBJ_SIZE * 0.03
        ob.modifiers["Bevel"].segments = 3
        bpy.ops.rigidbody.object_add(type="ACTIVE")
        ob.rigid_body.collision_shape = "BOX"
        ob.rigid_body.mass = max(0.01, density * OBJ_SIZE ** 3)
        ob.rigid_body.use_margin = True
        ob.rigid_body.collision_margin = min(0.004, OBJ_SIZE * 0.02)
        bodies.append(ob)
    else:
        piece_r = OBJ_SIZE * 0.5
        m = mat("Ball", colour, rough=rough, metal=metal, trans=trans)
        bpy.ops.mesh.primitive_uv_sphere_add(radius=piece_r, segments=56, ring_count=28,
                                             location=(jx, jy, release_z))
        ob = bpy.context.object
        ob.name = "Ball"
        ob.data.materials.append(m)
        bpy.ops.object.shade_smooth()
        bpy.ops.rigidbody.object_add(type="ACTIVE")
        ob.rigid_body.collision_shape = "SPHERE"
        ob.rigid_body.mass = max(0.01, density * (4.0 / 3.0) * math.pi * piece_r ** 3)
        bodies.append(ob)

    for ob in bodies:
        rb = ob.rigid_body
        rb.restitution = RESTITUTION
        # 1.0 because Bullet MULTIPLIES the two frictions - see passive(). The surface's
        # own number is then the effective coefficient of the contact.
        rb.friction = 1.0
        rb.use_deactivation = True      # let a dead take actually come to rest
        rb.use_start_deactivated = False

    if hold_frames >= 1:
        # A single spin rate for the whole take, so every take of a sweep starts identically.
        rate = _RNG.uniform(0.35, 1.6) if OBJECT != "cluster" else 0.0
        for ob in bodies:
            ob.rotation_mode = "QUATERNION"
        prev = {}
        for f in range(1, hold_frames + 1):
            ang = rate * (f - 1) / float(FPS)
            q = mathutils.Quaternion(axis, ang)
            for i, ob in enumerate(bodies):
                # Keep the sign continuous with the previous key or the shortest-path
                # interpolation between two keys takes the long way round and the object
                # snaps back a full turn between frames.
                qq = q.copy()
                if prev.get(i) is not None and qq.dot(prev[i]) < 0:
                    qq = -qq
                prev[i] = qq
                ob.rotation_quaternion = qq
                ob.rigid_body.kinematic = True
                # The location is keyed too and never changes in Z. It has to be exactly
                # still: any vertical drift during the hold is a velocity at release, and
                # the drop stops starting from the height the gauge says it does.
                ob.keyframe_insert("location", frame=f)
                ob.keyframe_insert("rotation_quaternion", frame=f)
                ob.keyframe_insert("rigid_body.kinematic", frame=f)
        for ob in bodies:
            ob.rigid_body.kinematic = False
            ob.keyframe_insert("rigid_body.kinematic", frame=hold_frames + 1)

    return bodies, piece_r, hold_frames + 1


# --------------------------------------------------------------------------- the camera


def camera_basis():
    """(u, quat, right, up, fwd). `u` points from the subject TOWARDS the camera."""
    u = mathutils.Vector((math.sin(YAW) * math.cos(PITCH),
                          -math.cos(YAW) * math.cos(PITCH),
                          math.sin(PITCH)))
    u.normalize()
    quat = (-u).to_track_quat("-Z", "Y")
    rot = quat.to_matrix()
    return u, quat, rot.col[0], rot.col[1], -rot.col[2]


def camera_at(aim_z: float, u, dist: float, frame: int, frame_end: int):
    """Where the camera really is on `frame`, push included.

    The gauge and the peak line are corrected for parallax against a camera position, so
    they need the position on the frame that matters rather than a nominal one - see the
    call sites. With the default push of 5% the two ends of the take are 27cm apart, which
    is a couple of pixels of tick drift; correcting at the right frame removes even that.
    """
    if PUSH <= 0.0 or frame_end <= 1:
        return mathutils.Vector((0.0, 0.0, aim_z)) + u * dist
    t = smoothstep((max(1, min(frame_end, frame)) - 1) / float(frame_end - 1))
    return mathutils.Vector((0.0, 0.0, aim_z)) + u * (dist * (1.0 + PUSH * (1.0 - t)))


def half_angles():
    """tan of the half field of view, vertical and horizontal.

    Blender fits the 36mm sensor to the LARGER resolution axis. In 9:16 that is the HEIGHT,
    so the visible height at distance d is 36/lens*d and the width follows from the aspect
    ratio - the exact opposite of the landscape intuition, and the reason a camera distance
    that was guessed rather than solved is always wrong in a vertical format.
    """
    if RES_Y >= RES_X:
        v = 18.0 / LENS
        return v, v * (RES_X / float(RES_Y))
    h = 18.0 / LENS
    return h * (RES_Y / float(RES_X)), h


def solve_camera(points, span: float):
    """Return (aim_z, distance): the closest camera that satisfies all three limits.

    This is a two-unknown problem, not the usual one, because the top of the frame is
    SPOKEN FOR - the caption lives there - so the action cannot simply be centred. The
    unknowns are how high the camera aims and how far back it stands, and the constraints
    are: the highest point of the action projects exactly at TOP, the lowest stays above
    -BOT, and the widest stays inside SIDE.

    Both are solved by bisection rather than in closed form because the projection is
    perspective and the camera is tilted: raising the camera lowers every point in the
    image monotonically, and backing it off shrinks the whole picture towards the point
    that is pinned at the top, so both searches are on monotone functions and 40 steps of
    bisection are exact to well under a pixel.

    Nothing here reads the simulation, and that is the point: every take of a sweep gets
    the same numbers out of this function, so the bounces can be compared.
    """
    u, _, right, up, fwd = camera_basis()
    tan_v, tan_h = half_angles()
    pts = [mathutils.Vector(p) for p in points]

    def rows(aim_z, dist):
        c = mathutils.Vector((0.0, 0.0, aim_z)) + u * dist
        lo = hi = None
        wide = 0.0
        for p in pts:
            q = p - c
            depth = q.dot(fwd)
            if depth <= 0.05:            # behind the lens: this camera cannot work
                return None
            y = q.dot(up) / (depth * tan_v)
            wide = max(wide, abs(q.dot(right)) / (depth * tan_h))
            lo = y if lo is None else min(lo, y)
            hi = y if hi is None else max(hi, y)
        return lo, hi, wide

    def fit_aim(dist):
        """Aim height that puts the topmost point exactly on TOP."""
        lo_a, hi_a = -6.0 * span, 8.0 * span
        for _ in range(48):
            mid = (lo_a + hi_a) / 2.0
            r = rows(mid, dist)
            # A None here means a point fell behind the lens, which only happens with the
            # camera pushed absurdly low; treating it as "still too high in frame" walks
            # the search back out of that corner.
            if r is None or r[1] > TOP:
                lo_a = mid
            else:
                hi_a = mid
        return (lo_a + hi_a) / 2.0

    def ok(dist):
        r = rows(fit_aim(dist), dist)
        return r is not None and r[0] >= -BOT and r[2] <= SIDE

    lo_d, hi_d = 0.5 * span, 80.0 * span
    if not ok(hi_d):
        return fit_aim(hi_d), hi_d
    for _ in range(44):
        mid = (lo_d + hi_d) / 2.0
        if ok(mid):
            hi_d = mid
        else:
            lo_d = mid
    return fit_aim(hi_d), hi_d


def wall_z_matching(z_true: float, cam, up, fwd, wall_y: float, x: float = 0.0) -> float:
    """Height on the backdrop that projects onto the same image row as (0, 0, z_true).

    The gauge is the whole argument of this scene, so it may not be off by a bounce. The
    ticks sit on a wall a metre and a half behind the drop and the camera looks down: a ray
    from the lens through the object at the drop plane keeps descending and meets the wall
    LOWER, so a tick built at its true height sits above the object it measures and the
    bounce reads short. Not by a little - 0.59m at the 25% tick on the default parameters.

    The image row of a point is (q.up)/(q.fwd) with q = P - C. Both are affine in the
    unknown z, so setting the wall point's row equal to the true point's row k gives

        base.up + up_z * z = k * (base.fwd + fwd_z * z)
        z = (k * base.fwd - base.up) / (up_z - k * fwd_z)

    with base = (x, wall_y, 0) - C. Solved at the tick's centre, which is where the object
    hangs; with the default yaw of 0 the camera is square to the wall and the answer holds
    across the whole tick, and a yawed camera slants the ticks slightly by construction.
    """
    qt = mathutils.Vector((0.0, 0.0, z_true)) - cam
    denom = qt.dot(fwd)
    if abs(denom) < 1e-6:
        return z_true
    k = qt.dot(up) / denom
    base = mathutils.Vector((x, wall_y, 0.0)) - cam
    den = up.z - k * fwd.z
    if abs(den) < 1e-6:
        return z_true
    return (k * base.dot(fwd) - base.dot(up)) / den


# ------------------------------------------------------------------------ the lit set


def build_backdrop(cam, fwd, wall_y: float, aim_z: float):
    """A wall behind the drop, sized from the camera so it covers the frame.

    Sized, not guessed: the visible half-height at any depth is tan_v * depth, so the wall
    is built from the depth it actually sits at and a margin. A backdrop that stops inside
    the frame is worse than none at all - it puts a hard horizontal edge across the shot
    with black above it.
    """
    tan_v, tan_h = half_angles()
    centre = mathutils.Vector((0.0, wall_y, aim_z))
    depth = (centre - cam).dot(fwd)
    hh, hw = tan_v * depth, tan_h * depth
    view = cam + fwd * depth
    width = hw * 2.6
    top = view.z + hh * 1.30
    height = top + 0.8                       # starts below the floor: no seam at the join
    ob = box("Backdrop", (width, 0.40, height),
             (view.x, wall_y + 0.20, top - height / 2.0),
             mat("BackdropMat", BACKDROP_COLOUR, rough=0.72))
    passive(ob, 0.8)
    return ob, width, hw


def build_gauge(cam, up, fwd, wall_y: float, tick_span: float,
                z_rest: float, z_release: float):
    """Ticks at 25/50/75/100% of the drop, on the wall, parallax-corrected.

    The scale runs from the object's RESTING centre height (0%) to its centre at release
    (100%), so a bounce read against it is exactly the fraction of the drop that came back
    - which is the number the format is about. The 100% line passes through the object as
    it hangs, which is what makes the scale self-explanatory without a word of narration.
    """
    made = []
    thick = max(0.008, DROP_H * 0.006)
    # 6.2% of the drop height puts the tick labels at about 3% of the frame height - 60px
    # on a 1920 frame, which is the smallest text that survives a phone screen. Bigger and
    # the longest of them ("100%") runs out of the frame past the end of its own tick.
    label_size = max(0.055, DROP_H * 0.062)
    # The ticks stand 2cm PROUD of the wall, and the correction is solved at the depth they
    # are really at, not at the wall face. 2cm sounds like nothing; the correction changes by
    # about 36cm per metre of depth here, so guessing the wrong plane would cost 7mm - small,
    # but the whole point of this function is that the reading is not approximate.
    tick_y = wall_y - 0.02
    for frac in (0.25, 0.50, 0.75, 1.00):
        z_true = z_rest + frac * (z_release - z_rest)
        z_wall = wall_z_matching(z_true, cam, up, fwd, tick_y)
        bright = frac >= 0.999
        m = mat(f"Tick{int(frac * 100)}",
                TICK_COLOUR if bright else (TICK_COLOUR[0] * 0.55, TICK_COLOUR[1] * 0.55,
                                            TICK_COLOUR[2] * 0.55, 1),
                rough=0.6, emit=1.5 if bright else 0.45)
        made.append(box(f"Tick{int(frac * 100)}",
                        (tick_span, 0.03, thick * (1.6 if bright else 1.0)),
                        (0.0, tick_y, z_wall), m))
        # Same depth and same height as its tick, so the two cannot drift apart in the
        # picture; it is clear of the bar in X, so they never intersect.
        bpy.ops.object.text_add(location=(tick_span / 2.0 + label_size * 0.5,
                                          tick_y, z_wall))
        txt = bpy.context.object
        txt.data.body = f"{int(frac * 100)}%"
        txt.data.align_x = "LEFT"
        txt.data.align_y = "CENTER"
        txt.data.size = label_size
        txt.data.extrude = 0.002
        txt.data.materials.append(m)
        # A text object's readable face is its local +Z. Rotating +90 about X sends +Z to
        # -Y, which is where the camera is, so the wall text faces the lens.
        txt.rotation_euler = (math.pi / 2.0, 0.0, 0.0)
        made.append(txt)
    return made


def build_peak_marker(cam, up, fwd, wall_y: float, tick_span: float,
                      z_peak: float, frac: float, frame: int, frame_end: int):
    """The measured apex of the first rebound, popped in on the frame it happens.

    Scale-keyed rather than hidden: two keys at 0.001 and one at 1.0 the next frame make it
    appear in a single frame under LINEAR interpolation, and scale is the one visibility
    control that behaves identically in every renderer and every Blender version.
    """
    # A hand in front of the gauge ticks, so that a rebound landing exactly on one of them
    # crosses in front of it instead of fighting it for the same 3cm of space - and, again,
    # corrected at the depth it is really at rather than at the wall face.
    peak_y = wall_y - 0.10
    z_wall = wall_z_matching(z_peak, cam, up, fwd, peak_y)
    m = mat("PeakMat", PEAK_COLOUR, rough=0.5, emit=3.2)
    bar = box("PeakLine", (tick_span * 1.02, 0.03, max(0.011, DROP_H * 0.009)),
              (0.0, peak_y, z_wall), m)
    size = max(0.060, DROP_H * 0.068)
    bpy.ops.object.text_add(location=(-tick_span * 0.51 - size * 0.5, peak_y, z_wall))
    txt = bpy.context.object
    txt.data.body = f"{int(round(frac * 100))}%"
    txt.data.align_x = "RIGHT"
    txt.data.align_y = "CENTER"
    txt.data.size = size
    txt.data.extrude = 0.002
    txt.data.materials.append(m)
    txt.rotation_euler = (math.pi / 2.0, 0.0, 0.0)
    pop = max(2, min(frame_end, frame))
    for ob in (bar, txt):
        ob.scale = (0.001, 0.001, 0.001)
        ob.keyframe_insert("scale", frame=1)
        ob.keyframe_insert("scale", frame=pop - 1)
        ob.scale = (1.0, 1.0, 1.0)
        ob.keyframe_insert("scale", frame=pop)
    return [bar, txt]


def build_lights(wall_y: float, arena_r: float):
    """Key, fill, a wash across the backdrop and a light lying along the floor.

    Energy is not a taste number here either: an area light's irradiance falls off as
    1/d^2, so every lamp is placed first and its power derived from where it ended up -
    72 W per square metre of distance, which is the 1800 W at 5 m that was eyeballed once
    on the wrecking-ball scene and has been carried by division ever since. Without it, a
    6 m drop is lit like a black hole and a 1 m drop is blown out.
    """
    s = max(DROP_H, 1.2)

    def area(name, loc, size, aim, scale=1.0, colour=(1, 1, 1)):
        bpy.ops.object.light_add(type="AREA", location=loc)
        lt = bpy.context.object
        lt.name = name
        lt.data.size = size
        lt.data.color = colour
        dist = math.dist(loc, aim)
        lt.data.energy = 72.0 * dist * dist * scale
        v = mathutils.Vector(aim) - mathutils.Vector(loc)
        lt.rotation_euler = v.to_track_quat("-Z", "Y").to_euler()
        # A light is geometry to Cycles. Leave this out and there is a glowing white
        # rectangle hanging in the shot - it has happened, on camera.
        lt.visible_camera = False
        return lt

    area("Key", (-1.05 * s, -1.15 * s, 1.30 * s), 0.60 * s, (0.0, 0.0, 0.40 * s), 1.0)
    area("Fill", (1.30 * s, -0.85 * s, 0.60 * s), 0.55 * s, (0.0, 0.0, 0.45 * s), 0.30)
    # The wash sits between the arena and the wall, low and wide, aimed backwards. It is
    # what keeps the empty air the object falls through from being a black band, and it is
    # placed outside the rim radius so it is never inside the arena it is lighting.
    area("Wash", (0.0, max(wall_y * 0.55, arena_r * 1.35), 0.10 * s), 1.6 * s,
         (0.0, wall_y, 0.55 * s), 0.42)
    # Along the floor towards the camera: the bottom of a 9:16 frame is all floor and it
    # cannot be the darkest part of the picture.
    area("Ground", (0.35 * s, -0.75 * s, 0.85 * s), 1.3 * s, (0.0, -0.45 * arena_r, 0.0), 0.40)
    # Cool rim from behind, so the object separates from the backdrop at every height.
    area("Rim", (0.55 * s, wall_y * 0.75, 1.05 * s), 0.5 * s, (0.0, 0.0, 0.45 * s), 0.26,
         colour=(0.62, 0.76, 1.0))


def take_label() -> str:
    """The caption for this take.

    The app hands over a label when it sweeps, but it builds it from the raw value, so a
    restitution of 0.55 arrives as the string "0.55". If what arrived is just the swept
    number, it is replaced by the scene's own formatting - which is the whole reason
    `label_unit` exists. Anything else the app sends is taken verbatim.
    """
    given = str(P.get("label", "") or "").strip()
    if given:
        try:
            if abs(float(given.rstrip("%")) - RESTITUTION) > 1e-6:
                return given
        except ValueError:
            return given
    unit = ps("label_unit")
    if unit == "none":
        return ""
    if unit == "value":
        return f"{RESTITUTION:g}"
    return f"{int(round(RESTITUTION * 100))}%"


def build_label(cam, text: str, title: str):
    """Caption, parented into camera space so it cannot leave the frame.

    Parented through the data API, which leaves the parent inverse as IDENTITY - the
    locations below really are camera space. Setting matrix_parent_inverse to the camera's
    inverse (the reflex from object parenting) turns them back into world coordinates and
    the caption lands somewhere off in the set.

    The sizes are fractions of the visible height at the caption's own depth, not metres,
    so changing the lens does not change how big the caption reads. 0.70 and 0.845 of the
    half frame are where the number and its title sit, and TOP above is 0.64*fill precisely
    so that the drop line stays underneath them.
    """
    tan_v, _ = half_angles()
    depth = 1.4
    frame_h = 2.0 * tan_v * depth
    m = mat("LabelMat", (1, 1, 1, 1), rough=0.9, emit=2.6)
    made = []
    for body, frac, size in ((text, 0.700, 0.096), (title, 0.845, 0.050)):
        if not body:
            continue
        bpy.ops.object.text_add(location=(0, 0, 0))
        txt = bpy.context.object
        txt.data.body = body
        txt.data.align_x = "CENTER"
        txt.data.align_y = "CENTER"
        txt.data.size = size * frame_h
        txt.data.extrude = 0.004 * frame_h
        txt.data.materials.append(m)
        txt.parent = cam
        txt.rotation_euler = (0, 0, 0)
        # A text object faces its local +Z and sits at negative Z in front of the camera,
        # so zero rotation already faces the lens.
        txt.location = (0.0, tan_v * depth * frac, -depth)
        made.append(txt)
    return made


# ---------------------------------------------------------------------------- simulation


def prepass(sc, bodies, frame_end: int):
    """Step the solver without rendering and write down where everything actually was.

    A rigid-body scene only advances when frames are stepped IN ORDER: jumping straight to
    frame 90 evaluates the setup pose, not frame 90. So this pass is needed anyway before a
    preview frame can be rendered, and while it runs it is the one honest source for the
    contact frames and the rebound height. The render afterwards re-runs the same
    deterministic simulation from frame 1 and reproduces it exactly.
    """
    dg = bpy.context.evaluated_depsgraph_get()
    tracks = [[] for _ in bodies]
    for f in range(1, frame_end + 1):
        sc.frame_set(f)
        dg.update()
        for i, ob in enumerate(bodies):
            p = ob.evaluated_get(dg).matrix_world.translation
            tracks[i].append((p.x, p.y, p.z))
    return tracks


def contacts_of(track, z_rest: float, piece_r: float, v0: float):
    """Frames this body hit something, from its recorded height alone.

    A bounce is a local minimum of height that happens near the floor - there is nothing
    else in this scene that can turn a falling body around. The strength is the descent
    speed measured over the frames leading INTO that minimum, referenced to the speed the
    first landing arrives at, so the first hit is 1.0 and every later one is quieter in the
    same proportion the bounce lost. Nothing is derived from the drop height on paper: at
    30fps the recorded minimum is within one frame of the true contact, and the speed it
    carried is what the microphone would have heard.

    The gate is deliberately loose, for two separate reasons that each cost a missed impact:

    * A cube that lands on a CORNER has its centre at 0.87 of its half-edge above the floor
      rather than level with it, so a gate cut tight around the resting height misses it.
    * At 30fps a fast bounce is never SAMPLED near the floor. An impact at 7.7 m/s covers
      26cm between two frames, so the lowest recorded height can sit 13cm above the surface
      the object plainly touched - measured on a four-piece bunch, where it silently threw
      away the first landing of two pieces and left the apex being read off objects that
      were still falling. One frame of impact travel is therefore part of the gate.

    Nothing is lost by being generous: a body in free flight has no local minimum at all,
    and a piece settling onto a pile only produces minima too slow to clear the strength
    floor in merge_events.
    """
    out = []
    n = len(track)
    gate = z_rest + max(0.9 * piece_r, 0.02 * DROP_H, 1.1 * v0 / FPS)
    for f in range(2, n):
        z0, z1, z2 = track[f - 2][2], track[f - 1][2], track[f][2]
        if not (z0 > z1 <= z2 and z1 <= gate):
            continue
        v = 0.0
        for k in range(max(1, f - 3), f):
            v = max(v, (track[k - 1][2] - track[k][2]) * FPS)
        if v <= 0.0:
            continue
        # frame numbers are 1-based, and index f-1 is the minimum
        out.append((f, min(1.0, max(0.0, (v / max(1e-6, v0)) ** 0.8))))
    return out


def merge_events(raw, cap: int):
    """One event per moment, not one per piece.

    Fourteen marbles landing inside the same 30th of a second is ONE sound, and a mixer
    handed fourteen inputs on the same frame produces mud rather than a louder hit. They
    are combined in quadrature, which is how uncorrelated sounds actually add, and the
    quietest are dropped until the list is short enough to open.
    """
    events = []
    for frame, strength in sorted(raw):
        if events and frame - events[-1]["frame"] <= 1:
            prev = events[-1]
            prev["strength"] = min(1.0, math.hypot(prev["strength"], strength))
            continue
        events.append({"frame": int(frame), "kind": "impact",
                       "strength": round(float(strength), 3)})
    events = [e for e in events if e["strength"] >= 0.05]
    if len(events) > cap:
        keep = sorted(events, key=lambda e: -e["strength"])[:cap]
        events = sorted(keep, key=lambda e: e["frame"])
    for e in events:
        e["strength"] = round(min(1.0, max(0.05, e["strength"])), 3)
    return events


def first_rebound(track, start_frame: int, z_rest: float, z_release: float, window: int):
    """(apex height, apex frame, fraction of the drop) for the first bounce after landing.

    Measured, not predicted. The solver never returns exactly restitution^2 - contact
    happens over a finite number of substeps, and Bullet zeroes restitution below a small
    relative velocity so that things can actually settle - so the only number worth putting
    on screen is the one the picture shows.

    Two details that both cost a wrong number on screen once:

    * `start_frame` is the frame the LAST body first landed, not the first. A cluster is
      dropped as a bunch, so its top pieces are still on their way down when the bottom one
      lands; searching from the first contact finds those falling pieces near the top of the
      frame and calls them a rebound, which reported the same height whatever the
      restitution was.
    * The apex is the maximum over a WINDOW rather than the first turning point. A cluster
      does not rise smoothly - pieces bounce off each other - so a "stop at the first frame
      that is lower" rule stops on the first jostle. The window is the flight time of a
      perfectly elastic rebound, which cannot end before the real apex, and later apexes are
      always lower than the first, so a plain maximum over it is the right answer.

    For a cluster the fraction can come out a few percent ABOVE restitution squared, because
    the top of a bunch starts half a bunch above the drop line the gauge is scaled to. That
    is not an error: the number is the height the line is drawn at, read off the same scale
    the viewer is reading, which is exactly what the marker claims to be.
    """
    hi = min(len(track), max(1, start_frame) + max(1, window))
    best_z, best_f = None, None
    for f in range(max(1, start_frame), hi + 1):
        z = track[f - 1][2]
        if best_z is None or z > best_z:
            best_z, best_f = z, f
    if best_z is None:
        return None, None, 0.0
    fall = max(1e-6, z_release - z_rest)
    return best_z, best_f, max(0.0, (best_z - z_rest) / fall)


# --------------------------------------------------------------------------------- main


def main():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    sc = bpy.context.scene
    sc.render.engine = "CYCLES"        # headless EEVEE has no GPU context: ~19s/frame
    sc.cycles.samples = SAMPLES
    sc.cycles.use_denoising = True
    print("CYCLES_DEVICE " + use_gpu(sc))
    sc.render.resolution_x, sc.render.resolution_y = RES_X, RES_Y
    sc.render.resolution_percentage = 100
    sc.render.fps = FPS
    frame_end = max(30, int(round(SECONDS * FPS)))
    sc.frame_start = 1
    sc.frame_end = frame_end
    sc.render.use_persistent_data = True
    obj_spec = OBJECT_MATERIALS.get(OBJ_MAT, OBJECT_MATERIALS["rubber"])
    if obj_spec[3]:
        # A transmissive object against a lit backdrop needs a couple of bounces to read as
        # glass; the default budget is minutes per frame for no visible gain here.
        sc.cycles.max_bounces = 8
        sc.cycles.transmission_bounces = 6
    # LINEAR keys, set BEFORE the first insert. There is a key on every frame of the hold
    # and of the camera push and the easing is already baked into the values, so bezier
    # handles would only add overshoot - and the marker's one-frame pop would ramp in over
    # half a second. Blender 5.2 moved Action.fcurves behind slotted actions, so this can no
    # longer be corrected after the fact.
    bpy.context.preferences.edit.keyframe_new_interpolation_type = "LINEAR"

    sc.world = bpy.data.worlds.new("W")
    sc.world.use_nodes = True
    sc.world.node_tree.nodes["Background"].inputs[0].default_value = (0.013, 0.013, 0.017, 1)

    # ---- the numbers the whole shot is built from. None of them may depend on the swept
    # value, or the takes stop being comparable - see the note at the top of the file.
    piece_r = piece_radius()
    z_rest = rest_height(piece_r)
    obj_half = OBJ_SIZE * 0.5
    # The object may not start inside the floor, and a drop shorter than a couple of its own
    # diameters is not a drop. Raising it is the only repair that keeps the shot honest -
    # the caption never claims a height, so a raised drop is still an honest take.
    z_release = max(DROP_H, z_rest * 3.0 + 0.05, obj_half * 1.6)
    arena_r = max(SPREAD.get(OBJECT, 0.30) * z_release, obj_half * 2.4)
    rim_h = max(0.05, 0.075 * z_release)
    wall_y = arena_r * 1.9 + obj_half

    # Everything the camera must hold: the arena floor it lands in, the rim around it, and
    # the object where it hangs at the start.
    fit = []
    for sx in (-1.0, 0.0, 1.0):
        for sy in (-1.0, 0.0, 1.0):
            fit.append((sx * arena_r, sy * arena_r, 0.0))
            if RIM:
                fit.append((sx * arena_r, sy * arena_r, rim_h))
    for sx in (-1.0, 1.0):
        for sz in (-1.0, 1.0):
            fit.append((sx * obj_half, 0.0, z_release + sz * obj_half))
    aim_z, dist = solve_camera(fit, span=max(z_release, arena_r * 2.0, 1.0))

    u, quat, right, up, fwd = camera_basis()
    cam_start = camera_at(aim_z, u, dist, 1, frame_end)
    bpy.ops.object.camera_add(location=tuple(cam_start), rotation=quat.to_euler())
    cam = bpy.context.object
    cam.name = "Cam"
    cam.data.lens = LENS
    cam.data.clip_end = max(400.0, dist * 8.0)
    sc.camera = cam
    if PUSH > 0.0:
        # The distance was solved for the END of the push, so the tightest frame of the
        # take is the one that was checked. Everything before it is wider by construction.
        for f in range(1, frame_end + 1):
            cam.location = camera_at(aim_z, u, dist, f, frame_end)
            cam.keyframe_insert("location", frame=f)

    # ---- the set. Everything with a rigid body is built BEFORE the simulation is stepped;
    # anything added afterwards would be in the render but not in the pre-pass, and the two
    # would no longer be the same simulation.
    surf = SURFACES.get(SURFACE, SURFACES["steel_plate"])
    build_floor(surf)
    tick_span = arena_r * 2.0
    if BACKDROP:
        # Sized from the camera at frame 1, which is the FURTHEST it ever stands: the visible
        # area at the backdrop only shrinks as the push comes in, so a wall that covers the
        # first frame covers every frame.
        _, _, wall_hw = build_backdrop(cam_start, fwd, wall_y, aim_z)
        tick_span = wall_hw * 1.15
    if RIM:
        build_rim(arena_r, rim_h, surf, math.atan2(u.y, u.x))
    if GAUGE:
        # Corrected against the camera in the MIDDLE of the push rather than at either end.
        # The correction is exact for one camera position and the camera moves, so the middle
        # halves the worst-case error across the take - about a pixel at the default push.
        build_gauge(camera_at(aim_z, u, dist, frame_end // 2, frame_end),
                    up, fwd, wall_y, tick_span, z_rest, z_release)
    build_lights(wall_y, arena_r)

    bodies, piece_r, release_frame = build_object(z_release, obj_spec)

    rw = sc.rigidbody_world
    rw.point_cache.frame_start = 1
    rw.point_cache.frame_end = frame_end
    # 20 substeps is 600Hz. Restitution is applied per contact, and at the default 10 a
    # bounce resolved over two substeps comes back visibly short of the coefficient it was
    # given - which in this scene is the number printed on the screen.
    rw.substeps_per_frame = 20
    rw.solver_iterations = 24

    label = take_label()
    build_label(cam, label, str(P.get("label_title", _default("label_title")) or "").strip())

    # ---- simulate, then read the two things off it that nothing else may guess at
    tracks = prepass(sc, bodies, frame_end)
    v0 = math.sqrt(2.0 * G * max(1e-6, z_release - z_rest))
    per_body = [contacts_of(track, z_rest, piece_r, v0) for track in tracks]
    events = merge_events([e for hits in per_body for e in hits], MAX_EVENTS)
    firsts = [hits[0][0] for hits in per_body if hits]
    first_contact = min(firsts) if firsts else None
    landed = max(firsts) if firsts else None      # the frame the LAST piece arrived

    # For a cluster the marker follows the HIGHEST piece: the centroid of fourteen spheres
    # spraying apart is a number nobody can see on screen, and the top of the spray is what
    # the eye actually measures against the ticks.
    top_track = [(0.0, 0.0, max(t[f][2] for t in tracks)) for f in range(frame_end)]
    peak_z = peak_f = None
    peak_frac = 0.0
    if landed:
        peak_z, peak_f, peak_frac = first_rebound(
            top_track, landed, z_rest, z_release, int(math.ceil(2.0 * v0 / G * FPS)) + 2)
    if PEAK and peak_z is not None and peak_f is not None:
        # Corrected against the camera on the frame the marker actually appears, which is
        # known here because the simulation has already been stepped. This one can be exact,
        # so it is.
        build_peak_marker(camera_at(aim_z, u, dist, peak_f, frame_end), up, fwd, wall_y,
                          tick_span, peak_z, peak_frac, peak_f, frame_end)

    with open(f"{OUT}/impacts.json", "w", encoding="utf-8") as fh:
        json.dump({"fps": FPS, "events": events,
                   "restitution": RESTITUTION, "label": label,
                   "drop_object": OBJECT, "pieces": len(bodies),
                   "drop_height": round(z_release, 3),
                   "rest_height": round(z_rest, 4),
                   "release_frame": int(release_frame),
                   "first_contact": first_contact,
                   "rebound_fraction": round(peak_frac, 4),
                   "rebound_frame": int(peak_f) if peak_f else None,
                   "expected_fraction": round(RESTITUTION ** 2, 4),
                   "impact_speed": round(v0, 2)}, fh, indent=1)

    tail = (f"restitution={RESTITUTION:g} label={label or '-'} object={OBJECT} "
            f"pieces={len(bodies)} drop={z_release:.2f}m rebound={peak_frac * 100:.0f}% "
            f"(expected {RESTITUTION ** 2 * 100:.0f}%) contact={first_contact} "
            f"events={len(events)} cam_dist={dist:.2f} aim_z={aim_z:.2f} arena={arena_r:.2f}")
    if not events:
        # Not fatal: at restitution 0 on a cluster the pieces can settle without ever
        # producing a clean minimum, and the footage is still the footage. The operator
        # should be told rather than left to find a silent clip in the finished cut.
        print("SCENE_NOTE no contact was detected - impacts.json has no events")

    if PREVIEW:
        n = max(1, min(frame_end, PREVIEW))
        # Stepped, not jumped. The pre-pass already filled the cache, but the peak marker was
        # added to the scene AFTER it, and a rigid body whose cache has been invalidated
        # shows its SETUP POSE when a frame is jumped to - which would put an object hanging
        # at the drop line into an approval frame that is supposed to show the bounce. The
        # animation render steps in order anyway; this makes the preview do the same, for a
        # fraction of a second.
        for f in range(1, n + 1):
            sc.frame_set(f)
        sc.render.image_settings.file_format = "PNG"
        sc.render.filepath = f"{OUT}/preview.png"
        bpy.ops.render.render(write_still=True)
        print(f"SCENE_OK preview={n} {tail}")
        return

    sc.render.image_settings.file_format = "PNG"    # Blender 5.2 has no FFMPEG output
    sc.render.image_settings.color_mode = "RGB"
    sc.render.filepath = f"{OUT}/frame_"
    bpy.ops.render.render(animation=True)
    print(f"SCENE_OK frames={frame_end} seconds={frame_end / FPS:.2f} {tail}")


if bpy is not None and __name__ == "__main__":
    main()
