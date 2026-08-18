"""Marbles running down a tube or an open half-pipe and dropping out into a catcher.

    blender -b -noaudio -P marble_tube.py -- '{"track_path":"helix","out_dir":"..."}'

This file is HAND MAINTAINED. A model picks the scene and fills in PARAMS; it never writes
geometry. Everything that could be solved has been solved rather than guessed, and the
derivation sits next to the number it produced.

THE PLEASURE IS THE JOURNEY, NOT THE ARRIVAL. That decides the whole design:

  THE TRACK IS REAL. It is built as a chain of rings of flat staves - twelve of them for a
  closed tube, seven for an open half-pipe - laid along a centreline that is walked by arc
  length. Every stave is its own PASSIVE box rigid body, so the marbles are not animated
  along a path: they are dropped in at the top and the solver rolls them down. What you
  watch is what Bullet did.

  THE TURNS ARE BANKED, AND THE BANK IS SOLVED. A marble that has fallen s metres down a
  slope theta is doing v^2 = (10/7) g sin(theta) s (a rolling sphere, not a sliding block -
  two sevenths of the energy goes into spin). In a turn of radius R that asks for a bank of
  atan(v^2 / gR). The channel is rolled about its own tangent by exactly that angle, capped
  at BANK_MAX and smoothed over half a metre so the transition is a real superelevation
  ramp. Without it a switchback tight enough to fit a 9:16 frame throws every marble
  straight over the outer wall - which is what the first version did.

  NOTHING FLOATS AND NOTHING IS FAKED. The marbles wait behind real drop pins that retract
  into the channel floor at their release frame. The track stands on posts that reach the
  ground. The catcher is placed where the BALLISTIC ARC out of the end of the track puts
  the marbles, solved for the exit speed the slope actually produces.

Impact frames in impacts.json come from a pre-pass that steps the solver without rendering
and differentiates every marble's position twice: an impact is a PEAK in the frame-to-frame
change of velocity, which is what separates a wall strike from the steady bend of a turn.
A sound is placed on the frame named here, so an estimate would be heard as a mistake.
"""

# --------------------------------------------------------------------------- parameters
# Read by the model that chooses this scene. It cannot see the shot, so the notes are
# written for someone who has never seen it.
PARAMS = {
    "seed": {
        "range": [0, 9999], "default": 0,
        "note": "changes where each marble sits across the channel when it starts (by a "
                "fraction of a millimetre), how the release frames are dithered, and which "
                "colour each marble gets. A marble entering the first banked turn a hair "
                "off centre takes a different line for the rest of the track, so two runs "
                "with the same everything else but a different seed are visibly different "
                "videos. Vary it so the same prompt never produces the same short twice.",
    },

    # ---- the track
    "track_path": {
        "choices": ["switchback", "helix"], "default": "switchback",
        "note": "the route the track takes. 'switchback' is the classic marble-run ramp: "
                "straight runs across the frame joined by banked 180 degree U-bends, each "
                "run lower than the last, so the track stacks up the tall axis of a 9:16 "
                "frame. 'helix' is the spiral corkscrew - one continuous banked turn "
                "winding down a column, which is the most hypnotic of the two and the one "
                "to pick for a closed transparent tube.",
    },
    "track_style": {
        "choices": ["tube", "half_pipe"], "default": "tube",
        "note": "the cross-section. 'tube' is a closed twelve-sided transparent tube - the "
                "marbles are seen THROUGH it, which is the signature look and why the "
                "material must stay see-through. 'half_pipe' is an open seven-sided channel "
                "curling up past horizontal on both sides, so you look straight down into "
                "it; that one may be opaque. A closed tube on a helix is the 'spiral tube' "
                "shot; an open half-pipe on a switchback is the wooden-toy shot.",
    },
    "track_length": {
        "range": [6.0, 34.0], "default": 13.0,
        "note": "how many metres of track the marbles travel, measured along the channel. "
                "This is the length of the JOURNEY and it is the main thing to raise if the "
                "clip should feel long. It costs render time (a longer track is more "
                "geometry) but the frame count is set by `seconds`, not by this. Note that "
                "the apparatus is only ever as TALL as length * sin(slope), so a short "
                "track on a shallow slope has almost no drop and comes out squat in a 9:16 "
                "frame - raise slope_deg along with a short length.",
    },
    "slope_deg": {
        "range": [6.0, 30.0], "default": 13.0,
        "note": "how steeply the track falls, in degrees, everywhere including through the "
                "turns. This is the SPEED control and it bites hard: a rolling marble "
                "reaches sqrt((10/7) g sin(slope) * track_length), so 13 degrees over 13 "
                "metres is about 5.5 m/s and 25 degrees over 30 metres is over 11 m/s - at "
                "30fps that second one crosses a third of the frame between frames and "
                "reads as a streak. Stay in the low teens unless the idea IS a speed run.",
    },
    "turns": {
        "range": [0, 12], "default": 0,
        "note": "0 means AUTO, which is almost always right: the number of U-bends (or, on "
                "a helix, the number of revolutions) is chosen so the finished apparatus "
                "comes out roughly as tall-and-narrow as a 9:16 frame, and the camera then "
                "has nothing to waste. Set it by hand only to force a look - a low number "
                "gives long lazy runs and a wide picture, a high number gives a tight "
                "stack. Reduced automatically if the turns would not fit in the length or "
                "would come out tighter than a marble can hold.",
    },
    "tube_radius": {
        "range": [0.05, 0.30], "default": 0.11,
        "note": "inner radius of the channel in metres. Forced into 1.6 to 5 times the "
                "marble radius: below that the marble does not fit, above it the marble is "
                "a speck rattling around inside a drainpipe. Leave it near 2.5x ball_size "
                "unless you want the loose, rattly look.",
    },
    "tube_material": {
        "choices": ["acrylic", "glass", "steel", "wood", "matte"], "default": "acrylic",
        "note": "'acrylic' is see-through using straight alpha - no refraction, so the "
                "marbles inside stay perfectly readable at phone size, and it is by far the "
                "cheapest to render. 'glass' is real refraction: prettier, distorts the "
                "marbles behind it, and roughly triples render time. 'steel', 'wood' and "
                "'matte' are solid and only make sense with track_style='half_pipe' - "
                "picked together with a closed tube they are overridden back to acrylic, "
                "because a closed opaque tube is a video of a pipe.",
    },
    "tube_tint": {
        "choices": ["clear", "blue", "teal", "amber", "rose", "lime", "smoke"],
        "default": "teal",
        "note": "colour of the see-through materials, and the stain on the wooden one. "
                "'clear' is the cleanest read; the tints are what make the track itself an "
                "object rather than an absence. Ignored by 'steel' and 'matte'.",
    },

    # ---- the marbles
    "ball_count": {
        "range": [1, 24], "default": 10,
        "note": "how many marbles run. They queue at the top of the track behind their drop "
                "pins, so the count costs track: the queue is reduced automatically if it "
                "would eat more than 45% of the length. 8-14 is the sweet spot - enough "
                "that the track is never empty, few enough that each one can be followed.",
    },
    "ball_size": {
        "range": [0.02, 0.09], "default": 0.042,
        "note": "marble RADIUS in metres (0.042 is a chunky 84mm ball - deliberately large, "
                "because a real 16mm marble is four pixels wide on a phone). tube_radius "
                "follows this if the two are inconsistent.",
    },
    "ball_material": {
        "choices": ["glass", "steel", "marble", "wood", "neon"], "default": "glass",
        "note": "what the marbles are. Sets colour, roughness, real density (so a steel one "
                "weighs 7800 kg/m3 and a wooden one 700) and bounce. 'glass' is the classic "
                "and refracts, 'steel' is heavy and mirror-bright, 'marble' is a matte "
                "coloured ceramic that reads best at small size, 'neon' glows and is the "
                "only one that stays visible through a dark tint.",
    },
    "ball_palette": {
        "choices": ["rainbow", "two_tone", "single"], "default": "rainbow",
        "note": "'rainbow' gives every marble its own hue, which is what lets the eye track "
                "an individual one the whole way down - the single biggest readability win "
                "here. 'two_tone' alternates two colours. 'single' makes them identical, "
                "which suits steel and looks like ball bearings.",
    },
    "release": {
        "choices": ["one_by_one", "bursts", "all"], "default": "one_by_one",
        "note": "how the drop pins let the marbles go. 'one_by_one' sends a single marble "
                "every `release_gap` seconds, so there is always one on the track and the "
                "clip has a steady pulse - the safest choice. 'bursts' sends `burst_size` "
                "at a time. 'all' drops every pin on the same frame and the whole queue "
                "leaves together as a train, which is the most spectacular and the one "
                "where marbles catch and shunt each other in the turns.",
    },
    "release_gap": {
        "range": [0.15, 1.60], "default": 0.40,
        "note": "seconds between releases (or between bursts). Ignored by release='all'. "
                "Multiply by ball_count to see how long the loading alone takes - 10 "
                "marbles at 0.4s is 4 seconds before the last one has even started, so "
                "raise `seconds` or lower this when the count is high.",
    },
    "burst_size": {
        "range": [2, 6], "default": 3,
        "note": "release='bursts' only: how many marbles leave together in each burst.",
    },

    # ---- the landing
    "catcher": {
        "choices": ["bowl", "tray", "floor"], "default": "bowl",
        "note": "what the marbles fall into at the end. 'bowl' is a round open bin they "
                "rattle round inside - the most satisfying arrival and the loudest. 'tray' "
                "is a square open box. 'floor' has them land on the ground and scatter, "
                "which is the loosest look and the one where they roll out of frame.",
    },
    "drop_height": {
        "range": [0.15, 1.60], "default": 0.55,
        "note": "how far the marbles fall through open air after they leave the end of the "
                "track, in metres. The catcher is positioned wherever the ballistic arc out "
                "of the tube actually puts them, so this is purely a look choice: small is "
                "a neat delivery, large is a visible flight and a harder landing.",
    },

    # ---- camera
    "camera": {
        "choices": ["wide", "follow"], "default": "wide",
        "note": "'wide' is one locked-off frame holding the whole apparatus, top of the "
                "track to catcher, solved from the simulation so nothing can leave it - it "
                "shows the marble travelling a path, which is the point of the format. "
                "'follow' tracks the leading marble down (translating only, never panning) "
                "and is much more immersive but hides how far it has come. Pick 'follow' "
                "for a long track where 'wide' would shrink the marbles to nothing.",
    },
    "follow_span": {
        "range": [0.8, 5.0], "default": 2.0,
        "note": "camera='follow' only: how many metres around the leading marble stay in "
                "shot. Small is fast and claustrophobic, large drifts back towards a wide.",
    },
    "lens": {
        "range": [24.0, 70.0], "default": 38.0,
        "note": "focal length in mm. Short exaggerates the depth of a helix and makes the "
                "near side of the spiral loom; long flattens it into a diagram. The camera "
                "distance is solved from this - never try to set a distance.",
    },
    "yaw_deg": {
        "range": [-70.0, 70.0], "default": -16.0,
        "note": "camera swing around the apparatus. 0 looks at the switchback dead on, with "
                "the runs crossing the frame horizontally; a bit of yaw turns the U-bends "
                "from flat semicircles into something with depth. On a helix any yaw works "
                "and the value only decides which side of the spiral the marbles pass on.",
    },
    "pitch_deg": {
        "range": [62.0, 88.0], "default": 76.0,
        "note": "camera angle measured from straight down the +Z axis: 90 is level with the "
                "subject, 76 looks 14 degrees down. Slightly down is right - it lets you "
                "see INTO an open half-pipe. Below about 66 it turns into a plan view and "
                "the drop out of the end of the track stops reading as a drop.",
    },
    "fill": {
        "range": [0.55, 0.94], "default": 0.84,
        "note": "how much of the frame HEIGHT the apparatus is allowed to occupy. The "
                "camera distance is solved from this.",
    },

    # ---- world
    "floor_checker": {
        "choices": [True, False], "default": True,
        "note": "a low-contrast checker on the ground. It costs nothing, gives the eye a "
                "ruler to measure the track against, and stops the bottom of a tall frame "
                "reading as dead black.",
    },

    # ---- length and events
    "auto_length": {
        "choices": [True, False], "default": True,
        "note": "let the render end when the run does. The scene is simulated before it is "
                "rendered, so the finishing frame is known: with this on the clip is "
                "trimmed to the last marble settling plus `settle_seconds`, or extended up "
                "to max_seconds if `seconds` would have cut the run off mid-track. Turn it "
                "off to get exactly `seconds` whatever happens.",
    },
    "max_seconds": {
        "range": [3.0, 16.0], "default": 11.0,
        "note": "hard ceiling on the rendered length when auto_length is on, and the length "
                "the physics cache is sized for, so nothing can simulate past it.",
    },
    "settle_seconds": {
        "range": [0.0, 2.5], "default": 0.7,
        "note": "how long to keep rolling after the last marble arrives, so the catcher is "
                "seen coming to rest instead of the video cutting on the final click.",
    },
    "max_events": {
        "range": [8, 200], "default": 52,
        "note": "cap on how many impacts are written to impacts.json. A dozen marbles down "
                "a banked track really do produce hundreds of contacts, but the mixer opens "
                "one audio input per event, so the list is thinned to the loudest ones "
                "(first and last always kept).",
    },

    "label": {
        "default": "",
        "note": "optional text burned into the top of the frame, for parameter sweeps "
                "(e.g. '25 degrees'). Empty means no text.",
    },

    # --- supplied by the app, not by the model choosing the scene ---
    "out_dir": {"default": "//out", "note": "app-supplied: where frames and impacts.json go."},
    "res_x": {"default": 1080, "note": "app-supplied render width."},
    "res_y": {"default": 1920, "note": "app-supplied render height (9:16 vertical)."},
    "samples": {"default": 32, "note": "app-supplied Cycles samples. Raise towards 64 for "
                                       "tube_material='glass' or ball_material='glass'."},
    "fps": {"default": 30, "note": "app-supplied frame rate."},
    "seconds": {"default": 6.0, "note": "app-supplied target length; see auto_length."},
    "preview_frame": {"default": 0, "note": "app-supplied: >0 simulates up to that frame, "
                                            "renders it alone to preview.png and stops."},
}

import json
import math
import random as _random
import sys

try:
    import bpy
    import mathutils
    from mathutils import Vector
except ModuleNotFoundError:      # outside Blender the module still imports, so the app can
    bpy = mathutils = None       # read PARAMS without starting a 3D session
    Vector = None

argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
P = json.loads(argv[0]) if argv else {}


def _default(key):
    return PARAMS[key]["default"]


def pf(key) -> float:
    try:
        return float(P.get(key, _default(key)))
    except (TypeError, ValueError):
        return float(_default(key))


def pi_(key) -> int:
    try:
        return int(round(float(P.get(key, _default(key)))))
    except (TypeError, ValueError):
        return int(_default(key))


def ps(key) -> str:
    v = P.get(key, _default(key))
    return str(v if v is not None else _default(key)).strip().lower()


def pb(key) -> bool:
    v = P.get(key, _default(key))
    if isinstance(v, str):
        return v.strip().lower() not in ("", "0", "false", "no", "off")
    return bool(v)


OUT = str(P.get("out_dir") or _default("out_dir"))
RES_X, RES_Y = pi_("res_x"), pi_("res_y")
SAMPLES = pi_("samples")
FPS = max(1, pi_("fps"))
SECONDS = pf("seconds")
PREVIEW = pi_("preview_frame")
LABEL = str(P.get("label", "") or "")

SEED = int(P.get("seed", 0) or 0)
_RNG = _random.Random(SEED)

G = 9.81
# A SOLID SPHERE that rolls without slipping only puts 5/7 of gravity into translation, the
# other 2/7 goes into spin: a = (5/7) g sin(theta), so v^2 = 2 a s = (10/7) g sin(theta) s.
# Using the sliding-block 2 g sin(theta) s here overestimates every speed by 18%, which is
# enough to bank the turns wrong and to throw the catcher a third of a metre too far.
ROLL = 10.0 / 7.0
PRE_ROLL = 8              # frames of stillness before the first pin drops
FACET_DEG = 30.0          # one stave spans this much of the cross-section
N_FACET = int(round(360.0 / FACET_DEG))          # 12 staves make a closed tube
HALF_PIPE_J = 3           # half-pipe keeps staves -3..+3, i.e. +-105 degrees: past
                          # horizontal on both sides, so a marble riding a banked turn is
                          # still held even when it is well up the wall
BANK_MAX = math.radians(45.0)     # beyond this a banked turn reads as a barrel roll
MAX_STAVES = 1500         # object budget for the track; ring spacing is opened to fit
# Hardest corner ONE ring of staves may turn. It is not a smoothness preference: each ring
# is a set of flat plates tangent to the channel, so two rings Dpsi apart meet at
# (R-rt)*(1/cos(Dpsi/2) - 1) INSIDE the channel, and past about 20 degrees per ring that
# ridge is big enough for a marble to clip. At 17 degrees it is under 5mm on a 110mm
# channel, which a marble rides straight over.
MAX_TURN = 0.30           # radians per ring
RIG = (0.10, 0.11, 0.13, 1)

# density kg/m3, and how the marble behaves in the solver. Bullet COMBINES friction and
# restitution by MULTIPLYING the two bodies' values, so these look higher than they read:
# a 0.6 marble on a 0.6 channel is an effective 0.36, and rolling without slipping needs
# only (2/7) tan(slope) ~ 0.07. Restitution is where the character is - steel pings, wood
# lands dead.
BALL_SPECS = {
    "glass":  {"color": (0.72, 0.88, 0.95, 1), "rough": 0.04, "metal": 0.0, "trans": 1.0,
               "density": 2500, "rest": 0.55, "fric": 0.55, "emit": 0.0, "sat": 0.85},
    "steel":  {"color": (0.72, 0.73, 0.76, 1), "rough": 0.12, "metal": 1.0, "trans": 0.0,
               "density": 7800, "rest": 0.60, "fric": 0.45, "emit": 0.0, "sat": 0.10},
    "marble": {"color": (0.88, 0.86, 0.84, 1), "rough": 0.30, "metal": 0.0, "trans": 0.0,
               "density": 2600, "rest": 0.45, "fric": 0.65, "emit": 0.0, "sat": 0.75},
    "wood":   {"color": (0.50, 0.32, 0.16, 1), "rough": 0.60, "metal": 0.0, "trans": 0.0,
               "density": 700,  "rest": 0.35, "fric": 0.75, "emit": 0.0, "sat": 0.55},
    "neon":   {"color": (0.30, 0.95, 0.75, 1), "rough": 0.25, "metal": 0.0, "trans": 0.0,
               "density": 1400, "rest": 0.50, "fric": 0.60, "emit": 2.6,  "sat": 0.95},
}
TINTS = {
    "clear": (0.86, 0.90, 0.93), "blue": (0.35, 0.55, 0.95), "teal": (0.30, 0.82, 0.80),
    "amber": (0.95, 0.65, 0.22), "rose": (0.95, 0.42, 0.62), "lime": (0.62, 0.92, 0.30),
    "smoke": (0.34, 0.35, 0.38),
}


# ------------------------------------------------------------------------------ helpers


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def use_gpu(sc) -> str:
    """Point Cycles at the GPU. OptiX first - RTX cards have the ray-tracing cores - then
    CUDA. On CPU a 200-frame track at 1080x1920 runs for hours, which is not a mode."""
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


def mat(name, rgba, rough=0.5, metal=0.0, trans=0.0, alpha=1.0, emit=0.0, ior=1.45):
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    b = m.node_tree.nodes["Principled BSDF"]
    _set(b, ("Base Color",), rgba)
    _set(b, ("Roughness",), rough)
    _set(b, ("Metallic",), metal)
    if trans:
        _set(b, ("Transmission Weight", "Transmission"), trans)
        _set(b, ("IOR",), ior)
    if alpha < 1.0:
        # Straight alpha, not transmission. A ray passes through unbent, so a marble seen
        # through the tube stays exactly where it is instead of being displaced by
        # refraction - at phone size that readability is worth more than the glass look,
        # and it renders in a third of the time because there is nothing to sample.
        _set(b, ("Alpha",), alpha)
    if emit:
        _set(b, ("Emission Color", "Emission"), rgba)
        _set(b, ("Emission Strength",), emit)
    return m


def hue_rgba(hue, sat, val):
    c = mathutils.Color((0.0, 0.0, 0.0))
    c.hsv = (hue % 1.0, clamp(sat, 0.0, 1.0), clamp(val, 0.0, 1.0))
    return (c.r, c.g, c.b, 1.0)


def box_mesh(name, sx, sy, sz, material=None):
    """One shared mesh datablock at real size.

    The dimensions go into the VERTICES, never into object scale: a rigid body's BOX shape
    is derived from the object, and an object left at scale 1 keeps a collision box exactly
    the size of the thing you can see. Building the mesh once and instancing it also avoids
    a thousand primitive_cube_add() calls, each of which pushes a depsgraph update over the
    whole scene - that alone was 40 seconds of build time on the first version.
    """
    hx, hy, hz = sx / 2.0, sy / 2.0, sz / 2.0
    verts = [(-hx, -hy, -hz), (hx, -hy, -hz), (hx, hy, -hz), (-hx, hy, -hz),
             (-hx, -hy, hz), (hx, -hy, hz), (hx, hy, hz), (-hx, hy, hz)]
    faces = [(0, 3, 2, 1), (4, 5, 6, 7), (0, 1, 5, 4),
             (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7)]
    me = bpy.data.meshes.new(name)
    me.from_pydata(verts, [], faces)
    me.update()
    if material is not None:
        me.materials.append(material)
    return me


def place(ob, pos, ex, ey, ez):
    """Put `ob` at `pos` with its local X/Y/Z along the given unit vectors.

    A rotation matrix is built from the three axes as COLUMNS (mathutils.Matrix takes rows,
    hence the transpose). Going through eulers instead is where track geometry usually goes
    wrong: a banked, descending, turning frame has no natural euler order and the answer
    flips somewhere in the middle of every helix.
    """
    rot = mathutils.Matrix((ex, ey, ez)).transposed().to_4x4()
    ob.matrix_world = mathutils.Matrix.Translation(pos) @ rot


def new_object(name, mesh, coll):
    ob = bpy.data.objects.new(name, mesh)
    coll.objects.link(ob)
    return ob


def add_rigid(objs, body_type, kinematic=False):
    """Give every object in `objs` a rigid body of `body_type`.

    The plural operator does the whole selection in one scene update; the per-object
    fallback is there because it is not worth losing a render if the operator's poll fails.
    Whichever ran, the result is verified per object - a stave silently left without a body
    is a hole in the track that the marbles fall straight through.
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
    except Exception:  # noqa: BLE001 - operator unavailable in this build
        pass
    for o in objs:
        if o.rigid_body is None:
            bpy.context.view_layer.objects.active = o
            bpy.ops.rigidbody.object_add(type=body_type)
        rb = o.rigid_body
        if rb is None:
            # Loud rather than silent. A stave without a body is a hole the marbles fall
            # straight through, and that renders perfectly happily - a clip of marbles
            # dropping out of the side of the track is far worse than a failed run.
            raise RuntimeError(f"rigid body could not be added to {o.name}")
        rb.type = body_type
        if kinematic:
            rb.kinematic = True


def smooth(values, half):
    if half < 1 or not values:
        return list(values)
    n = len(values)
    return [sum(values[max(0, i - half):min(n, i + half + 1)])
            / (min(n, i + half + 1) - max(0, i - half)) for i in range(n)]


def half_angles(lens):
    """tan of the half field of view, vertical and horizontal.

    Blender fits the 36mm sensor to the LARGER resolution axis. In 9:16 that is the HEIGHT,
    so the visible height at distance d is 36/lens*d and the width follows from the aspect
    ratio - the exact opposite of the landscape intuition, and the reason a hard-coded
    camera distance is always wrong in a vertical format.
    """
    if RES_Y >= RES_X:
        v = 18.0 / lens
        return v, v * (RES_X / float(RES_Y))
    h = 18.0 / lens
    return h * (RES_Y / float(RES_X)), h


def solve_distance(points, centre, u, lens, fill_v, fill_h) -> float:
    """Back the camera off along `u` until every point is inside the frame.

    `u` is the unit direction from the subject centre TOWARDS the camera. Because the
    camera is aimed at `centre`, that direction is exactly +Z in camera space, so a point
    at camera-space offset q sits at (qx, qy, qz - D) once the camera is D away. Requiring
    |qx| <= kx*(D - qz) and |qy| <= ky*(D - qz) turns into a lower bound on D per point and
    the answer is the largest of them. Closed form, no iteration, nothing can end up
    outside the frame.
    """
    quat = (-u).to_track_quat("-Z", "Y")
    rot_t = quat.to_matrix().transposed()
    tan_v, tan_h = half_angles(lens)
    ky, kx = tan_v * fill_v, tan_h * fill_h
    dist = 0.0
    for p in points:
        q = rot_t @ (Vector(p) - centre)
        dist = max(dist, q.z + abs(q.y) / ky, q.z + abs(q.x) / kx)
    return dist


# --------------------------------------------------------------------------- the geometry


def resolve_sizes():
    """Marble radius, channel radius and wall thickness, forced into a working set.

    A marble bigger than the channel is a scene that renders a tube full of nothing; a
    marble a tenth of the channel is a speck in a drainpipe. 1.6x is the smallest ratio
    that still lets the marble sit in the flat bottom stave without touching the two beside
    it (the twelve-sided section is circumscribed about `rt`, so its bottom facet is a
    chord 0.54*rt wide).
    """
    rb = clamp(pf("ball_size"), 0.02, 0.09)
    rt = clamp(pf("tube_radius"), 1.6 * rb, 5.0 * rb)
    # Wall thickness is a TUNNELLING budget as much as a look: a marble crossing more than
    # a wall thickness per solver substep passes straight through the outside of a turn.
    # The substep count below is solved from this and the top speed.
    t = max(0.018, 0.22 * rt)
    return rb, rt, t


def auto_switchback(H, drop, rt, v_top):
    """Pick the number of U-bends, the bend radius and the straight length.

    The DROP of a switchback does not depend on how many bends it has - that is fixed by
    length and slope. Only the WIDTH changes: more bends means shorter straights. So the
    number of bends is the one free variable that decides the aspect ratio of the finished
    apparatus, and the right answer is whatever makes it as tall-and-narrow as the frame.

    Two hard limits sit on the radius: no more than 35% of the horizontal path may be spent
    in bends (the rest has to be straight or it stops being a switchback), and a bend may
    not be tighter than 3.2 channel radii or the marbles simply cannot be held in it even
    banked. `v_top^2 / 21` is the radius at which the ideal bank would be 65 degrees; the
    bends are usually tighter than that and the bank hits its cap, which is fine - the
    marble then rides part-way up the outer wall, which is what a banked turn looks like.
    """
    want = (RES_Y / float(RES_X)) * 0.90
    best = None
    for k in range(1, 13):
        r = clamp(min(0.35 * H / (k * math.pi), v_top * v_top / 21.0), 3.2 * rt, 2.2)
        lr = (H - k * math.pi * r) / (k + 1.0)
        if lr < 2.5 * r or lr <= 0.4:
            continue
        width = lr + 2.0 * r
        score = abs(math.log(max(1e-3, (drop / width)) / want))
        if best is None or score < best[0]:
            best = (score, k, r, lr)
    if best is None:
        # Nothing fit - one long bend at the smallest legal radius, which always fits.
        r = max(3.2 * rt, 0.30)
        return 1, r, max(0.5, (H - math.pi * r) / 2.0)
    return best[1], best[2], best[3]


def auto_helix(H, drop, rt, wall):
    """Pick the helix radius and revolutions.

    Solved straight from the frame: the apparatus is 2R wide and `drop` tall, so asking for
    drop/(2R + 2rt) to equal the frame's own aspect gives R directly, and the revolutions
    follow from the length that is left. Two limits then bind - the radius may not go below
    3.2 channel radii (a marble cannot hold a tighter turn) and consecutive coils must
    clear each other vertically by more than the channel is thick, or the spiral welds
    itself into a solid cylinder.
    """
    want = (RES_Y / float(RES_X)) * 0.90
    r = max(3.2 * rt, (drop / want - 2.0 * rt) / 2.0)
    n = H / (2.0 * math.pi * r)
    for _ in range(24):
        n = max(1.0, n)
        r = H / (2.0 * math.pi * n)
        if r >= 3.2 * rt and drop / n >= 2.6 * (rt + wall):
            break
        n -= 0.25
    return max(1.0, n), max(3.2 * rt, H / (2.0 * math.pi * max(1.0, n)))


def centreline(style, S, theta, rt, wall, n_staves):
    """Walk the track and return one sample per ring.

    Both routes are the same walk with a different CURVATURE profile, which is what keeps
    the banking, the stave placement and the framing identical for the two of them. The
    horizontal path is integrated at 1cm steps with a midpoint rule (second order, so an
    arc closes on itself to well under a millimetre over thirty metres) and rings are read
    off it at the ring spacing. Height comes from arc length, not from the horizontal
    distance: z = z_top - s*sin(theta), so the slope really is constant everywhere,
    including all the way through the bends. A track that flattens in its turns stalls.

    The ring spacing is chosen here rather than passed in, because it can only be decided
    once the tightest bend is known: it is the smaller of a plain look limit and the
    spacing at which one ring turns MAX_TURN, then opened up if that would blow the stave
    budget. Deciding it before the route is laid out is how the first version ended up
    approximating a 0.16m bend with 50-degree facets.

    Returns (samples, note, kappa_max, turns_used, seg) where each sample is
    (P, T, kappa, s) with P a 3D point, T the 3D unit tangent, kappa the signed horizontal
    curvature (positive = turning left) and s the arc length from the top.
    """
    H = S * math.cos(theta)                 # horizontal ground distance covered
    drop = S * math.sin(theta)
    # Design speed at the very bottom, used only to size the bends. See ROLL.
    v_top = math.sqrt(max(0.01, ROLL * G * math.sin(theta) * S))
    legs = []                               # (length, curvature) along the HORIZONTAL path
    if style == "helix":
        turns_req = pf("turns")
        if turns_req and turns_req > 0:
            n = clamp(turns_req, 1.0, 14.0)
            r = H / (2.0 * math.pi * n)
            if r < 3.2 * rt or drop / n < 2.6 * (rt + wall):
                n, r = auto_helix(H, drop, rt, wall)
            else:
                r = H / (2.0 * math.pi * n)
        else:
            n, r = auto_helix(H, drop, rt, wall)
        n = H / (2.0 * math.pi * r)
        legs = [(H, 1.0 / r)]
        start = (r, 0.0, math.pi / 2.0)     # on the +X side, heading +Y: a ccw coil
        note = f"helix R={r:.2f} turns={n:.2f} coil_pitch={drop / max(n, 0.01):.2f}m"
        turns_used = n
        r_min = r
    else:
        k = pi_("turns")
        if k > 0:
            r = clamp(min(0.35 * H / (k * math.pi), v_top * v_top / 21.0), 3.2 * rt, 2.2)
            lr = (H - k * math.pi * r) / (k + 1.0)
            if lr < 2.5 * r or lr <= 0.4:
                k, r, lr = auto_switchback(H, drop, rt, v_top)
        else:
            k, r, lr = auto_switchback(H, drop, rt, v_top)
        for i in range(k + 1):
            legs.append((lr, 0.0))
            if i < k:
                # Every bend turns the SAME way. Turning left out of a +X run ends heading
                # -X two radii further on in +Y; turning left again out of that run brings
                # the path back to where it started in Y. So the whole serpentine lives in
                # a band only 2R deep and costs the shot almost no width, while the runs
                # stack down the tall axis of the frame.
                legs.append((math.pi * r, 1.0 / r))
        start = (0.0, 0.0, 0.0)
        note = f"switchback bends={k} R={r:.2f} run={lr:.2f}m"
        turns_used = k
        r_min = r

    # Ring spacing, now that the tightest bend is known. Three limits: 0.30m so a straight
    # never reads as a run of visible flats, MAX_TURN*r_min so a bend never builds a ridge
    # a marble can clip, and the stave budget, which is the only one allowed to make the
    # spacing COARSER.
    rings_max = max(6.0, MAX_STAVES / float(n_staves) - 1.0)
    seg = clamp(min(0.30, MAX_TURN * r_min), S / rings_max, 0.42)

    total_leg = sum(L for L, _ in legs) or H
    fine = 0.01
    n_fine = max(4, int(math.ceil(total_leg / fine)))
    dh = total_leg / n_fine
    x, y, hdg = start
    xs, ys, hs, ks = [], [], [], []
    edges, acc = [], 0.0
    for L, kk in legs:
        edges.append((acc, acc + L, kk))
        acc += L

    def kappa_at(h):
        for a, b, kk in edges:
            if h < b:
                return kk
        return edges[-1][2]

    for i in range(n_fine + 1):
        h = i * dh
        kk = kappa_at(min(h, total_leg - 1e-6))
        xs.append(x)
        ys.append(y)
        hs.append(hdg)
        ks.append(kk)
        x += math.cos(hdg + 0.5 * kk * dh) * dh
        y += math.sin(hdg + 0.5 * kk * dh) * dh
        hdg += kk * dh

    n_rings = max(4, int(round(S / seg)))
    z_top = 0.0                              # relative; the caller lifts the whole track
    samples = []
    ct, st = math.cos(theta), math.sin(theta)
    kappa_max = 0.0
    for i in range(n_rings + 1):
        s = S * (i / float(n_rings))
        j = clamp(int(round((s * ct) / dh)), 0, n_fine)
        hdg = hs[j]
        T = Vector((math.cos(hdg) * ct, math.sin(hdg) * ct, -st))
        Pt = Vector((xs[j], ys[j], z_top - s * st))
        samples.append([Pt, T, ks[j], s])
        kappa_max = max(kappa_max, abs(ks[j]))
    return samples, note, kappa_max, turns_used, seg


def bank_frames(samples, theta, s_front):
    """Roll each ring about its own tangent by the bank the speed there actually needs.

    A marble at arc length s is doing v^2 = (10/7) g sin(theta) (s - s_front) - measured
    from where the FRONT marble is released, so the loading rack at the top stays dead
    level and a marble parked against a pin does not sit on a slanted floor. In a bend of
    curvature k that asks for a bank of atan(v^2 k / g): tilt the channel by that and the
    marble presses straight into the floor instead of climbing the wall.

    The angle is then SMOOTHED over about half a metre of track, which is what turns the
    step from a straight into a bend into a transition ramp. Real track does exactly this
    and it is not cosmetic: a bank that appears in one ring launches the marble off the
    kink. Capped at BANK_MAX - past that it reads as a barrel roll, and the residual just
    puts the marble part-way up the outer wall, which is where a fast marble belongs.

    Fills each sample with its banked (U, S) frame: U is the channel's own up, S its left.
    """
    sinth = math.sin(theta)
    raw = []
    for Pt, T, kk, s in samples:
        v2 = ROLL * G * sinth * max(0.0, s - s_front) * 1.15   # 15% for the later marbles,
        ideal = math.atan(v2 * abs(kk) / G)                    # which start higher up
        raw.append(math.copysign(min(ideal, BANK_MAX), kk))
    step = samples[1][3] - samples[0][3] if len(samples) > 1 else 0.2
    # Transition ramp: half a metre on a normal track, less on a small one, because a ramp
    # longer than the straights it sits between would smear the bank across the whole run
    # and tilt the loading rack the marbles are parked on.
    ramp = min(0.55, 0.06 * samples[-1][3])
    beta = smooth(raw, max(1, int(round(ramp / max(step, 1e-3)))))
    frames = []
    up = Vector((0.0, 0.0, 1.0))
    for i, (Pt, T, kk, s) in enumerate(samples):
        # The horizontal left of the tangent is already perpendicular to it (the tangent's
        # only vertical part is the slope), so no orthogonalisation is needed here.
        hx, hy = T.x, T.y
        m = math.hypot(hx, hy) or 1.0
        S0 = Vector((-hy / m, hx / m, 0.0))
        U0 = T.cross(S0).normalized()
        b = beta[i]
        U = (U0 * math.cos(b) + S0 * math.sin(b)).normalized()
        Sv = (S0 * math.cos(b) - U0 * math.sin(b)).normalized()
        frames.append({"P": Pt, "T": T.normalized(), "U": U, "S": Sv,
                       "k": kk, "s": s, "bank": b})
    return frames


def frame_at(frames, s):
    """Interpolate the channel frame at arc length `s` (used to place marbles and pins)."""
    n = len(frames)
    if n == 1:
        return frames[0]
    total = frames[-1]["s"]
    q = clamp(s / max(total, 1e-6), 0.0, 1.0) * (n - 1)
    i = min(n - 2, int(math.floor(q)))
    f = q - i
    a, b = frames[i], frames[i + 1]
    out = {}
    out["P"] = a["P"].lerp(b["P"], f)
    for key in ("T", "U", "S"):
        out[key] = (a[key].lerp(b[key], f)).normalized()
    out["s"] = s
    out["k"] = a["k"] + (b["k"] - a["k"]) * f
    out["bank"] = a["bank"] + (b["bank"] - a["bank"]) * f
    return out


def stave_angles(style):
    """Which of the twelve facets exist. Facet 0 is always the FLOOR of the channel."""
    if style == "half_pipe":
        return list(range(-HALF_PIPE_J, HALF_PIPE_J + 1))
    return list(range(N_FACET))


def build_track(frames, style, rt, wall, seg, kappa_max, material, coll):
    """Lay a ring of flat staves at every sample. Returns the stave objects.

    Every stave is a separate PASSIVE box, not one welded triangle mesh. Bullet rolls a
    sphere across a static triangle mesh badly - it catches on the internal edges between
    triangles and the marble stutters - whereas box against sphere is the best-conditioned
    pair the solver has. The cost is object count, which is why the ring spacing opens up
    until the total fits MAX_STAVES.

    Consecutive rings deliberately OVERLAP along the track. At the outside of a bend the
    arc between two ring centres grows by (rt+wall)*kappa, so a stave cut to the ring
    spacing leaves a slot on the outer wall exactly where a marble is pressed hardest -
    which is where it escapes. The extra length below closes that for the tightest bend on
    the whole track, and the surplus overlap on the straights costs nothing because static
    bodies never interact with each other.
    """
    chord = 2.0 * rt * math.tan(math.radians(FACET_DEG) / 2.0)
    seg_len = seg * (1.0 + (rt + wall) * kappa_max) * 1.06
    me = box_mesh("StaveMesh", chord * 1.06, seg_len, wall, material)
    angles = stave_angles(style)
    objs = []
    for i, fr in enumerate(frames):
        Pt, T, U, Sv = fr["P"], fr["T"], fr["U"], fr["S"]
        for j in angles:
            phi = math.radians(FACET_DEG) * j
            # phi = 0 is the floor of the channel, +-90 the side walls, 180 the lid.
            rad = (-U * math.cos(phi) + Sv * math.sin(phi)).normalized()
            tang = (U * math.sin(phi) + Sv * math.cos(phi)).normalized()
            ob = new_object(f"Stave_{i:03d}_{j:+d}", me, coll)
            place(ob, Pt + rad * (rt + wall / 2.0), tang, T, rad)
            objs.append(ob)
    return objs


def build_posts(frames, rt, wall, keep_out, rig_mat, coll):
    """Vertical posts from the ground up to the underside of the track.

    A marble run hanging in mid-air is the single fastest way to make a render look like a
    render. The posts are placed every ~2m of track, skipped wherever they would stand
    inside the catcher, and they are what gives the eye something to read the height
    against. Returns (objects, framing points at their feet).
    """
    step = max(1, int(round(2.0 / max(1e-3, frames[1]["s"] - frames[0]["s"]))))
    side = max(0.045, rt * 0.42)
    objs, pts = [], []
    for i in range(step // 2, len(frames), step):
        Pt = frames[i]["P"]
        if keep_out and math.hypot(Pt.x - keep_out[0], Pt.y - keep_out[1]) < keep_out[2]:
            continue
        top = Pt.z - (rt + wall)
        if top < 0.12:
            continue
        me = box_mesh(f"PostMesh{i}", side, side, top, rig_mat)
        ob = new_object(f"Post_{i:03d}", me, coll)
        place(ob, Vector((Pt.x, Pt.y, top / 2.0)),
              Vector((1, 0, 0)), Vector((0, 1, 0)), Vector((0, 0, 1)))
        objs.append(ob)
        pts.append((Pt.x, Pt.y, 0.0))
    return objs, pts


PIN_T = 0.020        # drop pin thickness along the track


def make_groups(n, release, burst):
    """Which marbles share a gate. Group 0 is released first and sits nearest the exit."""
    if release == "all":
        return [list(range(n))]
    if release == "bursts":
        b = max(2, min(6, burst))
        return [list(range(i, min(n, i + b))) for i in range(0, n, b)]
    return [[i] for i in range(n)]


def queue_layout(rb, groups, s_top, s_limit):
    """Where every marble and every drop pin sits along the track.

    Order along the track from the top down is: the LAST group's marbles, that group's pin,
    then the group before it, and so on, with the FIRST group to be released nearest the
    exit. That ordering is the whole trick - a group's pin sits immediately below its own
    marbles and immediately above the next group's, so no pin is ever in front of a marble
    it does not hold, and releasing one group never disturbs another.

    Returns (ball_s per ball index, pin_s per group, fits) - `fits` is False when the whole
    queue does not stay inside `s_limit`, and the caller answers that by running fewer
    marbles rather than by dropping some out of the middle of the release order.
    """
    ball_s, pin_s = {}, {}
    cursor = s_top
    for g in range(len(groups) - 1, -1, -1):
        placed = []
        for _idx in groups[g]:
            placed.append(cursor + rb)
            cursor = placed[-1] + rb * 1.04
        pin_at = cursor + 0.35 * rb + PIN_T / 2.0
        cursor = pin_at + PIN_T / 2.0 + 0.10
        if cursor > s_limit:
            return ball_s, pin_s, False
        for idx, sv in zip(groups[g], placed):
            ball_s[idx] = sv
        pin_s[g] = pin_at
    return ball_s, pin_s, True


def build_catcher(kind, centre, radius, rb, rig_mat, coll):
    """The bin the marbles arrive in. Returns (objects, framing points, rim height)."""
    wall = 0.035
    height = max(0.26, 7.0 * rb)
    base_z = 0.0
    objs, pts = [], []
    if kind == "floor":
        return objs, pts, base_z + rb
    if kind == "tray":
        me_floor = box_mesh("TrayFloor", radius * 2.0, radius * 2.0, 0.05, rig_mat)
        ob = new_object("TrayFloor", me_floor, coll)
        place(ob, Vector((centre[0], centre[1], 0.025)),
              Vector((1, 0, 0)), Vector((0, 1, 0)), Vector((0, 0, 1)))
        objs.append(ob)
        me_lx = box_mesh("TrayWallX", wall, radius * 2.0 + 2 * wall, height, rig_mat)
        me_ly = box_mesh("TrayWallY", radius * 2.0 + 2 * wall, wall, height, rig_mat)
        for sx in (-1, 1):
            ob = new_object(f"TrayWX{sx}", me_lx, coll)
            place(ob, Vector((centre[0] + sx * (radius + wall / 2.0), centre[1],
                              0.05 + height / 2.0)),
                  Vector((1, 0, 0)), Vector((0, 1, 0)), Vector((0, 0, 1)))
            objs.append(ob)
            ob = new_object(f"TrayWY{sx}", me_ly, coll)
            place(ob, Vector((centre[0], centre[1] + sx * (radius + wall / 2.0),
                              0.05 + height / 2.0)),
                  Vector((1, 0, 0)), Vector((0, 1, 0)), Vector((0, 0, 1)))
            objs.append(ob)
        for sx in (-1, 1):
            for sy in (-1, 1):
                pts.append((centre[0] + sx * (radius + wall), centre[1] + sy * (radius + wall),
                            0.0))
                pts.append((centre[0] + sx * (radius + wall), centre[1] + sy * (radius + wall),
                            0.05 + height))
        return objs, pts, 0.05 + rb
    # ---- bowl: a ring of staves leaning outwards on a round base
    n = 18
    lean = math.radians(14.0)
    chord = 2.0 * radius * math.tan(math.pi / n) * 1.08
    bpy.ops.mesh.primitive_cylinder_add(radius=radius + wall, depth=0.06,
                                        vertices=48, location=(centre[0], centre[1], 0.03))
    base = bpy.context.object
    base.name = "BowlBase"
    base.data.materials.append(rig_mat)
    objs.append(base)
    me = box_mesh("BowlStave", chord, wall, height, rig_mat)
    up = Vector((0, 0, 1))
    for i in range(n):
        a = 2.0 * math.pi * i / n
        outward = Vector((math.cos(a), math.sin(a), 0.0))
        tangential = Vector((-math.sin(a), math.cos(a), 0.0))
        up_w = (up * math.cos(lean) + outward * math.sin(lean)).normalized()
        normal = (outward * math.cos(lean) - up * math.sin(lean)).normalized()
        ob = new_object(f"BowlStave{i:02d}", me, coll)
        pos = Vector((centre[0], centre[1], 0.06)) + outward * radius + up_w * (height / 2.0)
        place(ob, pos, tangential, normal, up_w)
        objs.append(ob)
        pts.append((pos.x + outward.x * wall, pos.y + outward.y * wall, 0.0))
        top = pos + up_w * (height / 2.0)
        pts.append((top.x, top.y, top.z))
    return objs, pts, 0.06 + rb


def build_floor(reach):
    bpy.ops.mesh.primitive_plane_add(size=max(80.0, reach * 8.0), location=(0, 0, 0))
    floor = bpy.context.object
    floor.name = "Floor"
    m = bpy.data.materials.new("FloorMat")
    m.use_nodes = True
    nt = m.node_tree
    b = nt.nodes["Principled BSDF"]
    b.inputs["Roughness"].default_value = 0.42
    # Not black. The catcher sits on this and a third of a 9:16 frame is ground - pure
    # black down there is a third of the video doing nothing.
    b.inputs["Base Color"].default_value = (0.052, 0.052, 0.058, 1)
    if pb("floor_checker"):
        tex = nt.nodes.new("ShaderNodeTexChecker")
        tex.inputs["Color1"].default_value = (0.066, 0.067, 0.074, 1)
        tex.inputs["Color2"].default_value = (0.032, 0.033, 0.038, 1)
        # Object coordinates, not the default generated ones: generated coords are
        # normalised over the plane's bounding box, so the checker would resize itself
        # whenever the floor does. In object space one unit is one metre.
        coord = nt.nodes.new("ShaderNodeTexCoord")
        tex.inputs["Scale"].default_value = 1.0 / 0.35
        nt.links.new(coord.outputs["Object"], tex.inputs["Vector"])
        nt.links.new(tex.outputs["Color"], b.inputs["Base Color"])
    floor.data.materials.append(m)
    bpy.ops.rigidbody.object_add(type="PASSIVE")
    floor.rigid_body.friction = 0.85
    floor.rigid_body.restitution = 0.20
    return floor


def build_lights(centre, reach, height):
    """Key, fill, rim and a wide ground light, all invisible to camera rays.

    Energy is not a taste number. An area light's irradiance falls off as 1/d^2, and the
    one value ever eyeballed in this library is 1800W at 5m - that is 72W per square metre
    of distance, and every light below is that figure carried to its own distance. It is
    why a 4m helix and a 30m switchback come out lit the same instead of the long one
    going black when the camera backs off.
    """
    def area(name, loc, size, aim, scale=1.0, color=(1, 1, 1)):
        bpy.ops.object.light_add(type="AREA", location=loc)
        lt = bpy.context.object
        lt.name = name
        lt.data.size = size
        d = math.dist(loc, aim)
        lt.data.energy = 72.0 * d * d * scale
        lt.data.color = color
        lt.rotation_euler = (Vector(aim) - Vector(loc)).to_track_quat("-Z", "Y").to_euler()
        # A light is geometry to Cycles. Leave this out and there is a glowing white
        # rectangle hanging in the shot - it has happened, on camera.
        lt.visible_camera = False
        return lt

    r = max(reach, 2.0)
    h = max(height, 1.5)
    cx, cy = centre[0], centre[1]
    area("Key", (cx + r * 1.05, cy - r * 1.15, h * 1.05 + r * 0.4), r * 0.75,
         (cx, cy, h * 0.55), 1.0)
    area("Fill", (cx - r * 1.25, cy - r * 0.75, h * 0.60), r * 0.7,
         (cx, cy, h * 0.45), 0.30)
    # Cool rim from behind: without it a transparent tube against a near-black world has no
    # edge at all and the track disappears between the marbles.
    area("Rim", (cx - r * 0.30, cy + r * 1.30, h * 0.95), r * 0.6,
         (cx, cy, h * 0.55), 0.45, (0.60, 0.75, 1.0))
    # The light that stops the bottom third of the frame being a black band. Aimed at the
    # ground beside the catcher, wide and soft.
    area("Ground", (cx + r * 0.35, cy - r * 0.85, h * 0.42 + 0.6), r * 1.3,
         (cx, cy, 0.0), 0.40)


def build_label(cam, lens):
    """Sweep label, parented into CAMERA SPACE so it cannot leave the frame.

    Parented through the data API, which leaves the parent inverse as IDENTITY - the
    location below really is camera space. Setting matrix_parent_inverse to the camera's
    inverse (the reflex from object parenting) turns it back into world coordinates and the
    text lands somewhere off-scene.
    """
    bpy.ops.object.text_add(location=(0, 0, 0))
    txt = bpy.context.object
    txt.data.body = LABEL
    txt.data.align_x = "CENTER"
    txt.data.align_y = "CENTER"
    txt.data.size = 0.20
    txt.data.extrude = 0.004
    txt.data.materials.append(mat("LabelMat", (1, 1, 1, 1), rough=0.9, emit=1.5))
    txt.parent = cam
    txt.rotation_euler = (0, 0, 0)
    txt.location = (0.0, (36.0 / lens * 2.6 / 2.0) * 0.68, -2.6)
    return txt


# ------------------------------------------------------------------- simulate, then frame


def prepass(sc, balls, last_release, sim_end, stop_when_done, settle_frames):
    """Step the whole simulation WITHOUT rendering and write down what actually happened.

    Three things come out of this and nothing else in the file guesses at any of them: the
    marble positions the camera has to hold, the frames a sound belongs on, and the frame
    the run is over. It costs one extra pass of the solver; the render then re-runs the
    same deterministic simulation from frame 1 and reproduces it exactly.

    A rigid body only advances when frames are STEPPED IN ORDER - jumping straight to frame
    120 evaluates the setup pose, not frame 120 - so this loop is also what makes a preview
    of a late frame show anything at all.
    """
    dg = bpy.context.evaluated_depsgraph_get()
    n = len(balls)
    tracks = [[] for _ in range(n)]
    done_at = None
    rest_run = 0
    for f in range(1, sim_end + 1):
        sc.frame_set(f)
        dg.update()
        fastest = 0.0
        for i, b in enumerate(balls):
            p = b.evaluated_get(dg).matrix_world.translation
            tracks[i].append((p.x, p.y, p.z))
            if len(tracks[i]) > 1:
                a = tracks[i][-2]
                fastest = max(fastest, math.dist(a, (p.x, p.y, p.z)) * FPS)
        if f > last_release + 2 and fastest < 0.28:
            rest_run += 1
        else:
            rest_run = 0
        if rest_run >= 6 and done_at is None:
            done_at = f
        if stop_when_done and done_at is not None and f >= done_at + settle_frames + 2:
            break
    return tracks, done_at


def peaks_of(series, min_gap, floor):
    """Local maxima of a per-frame series, at least `min_gap` frames apart.

    A marble that hits a wall changes velocity in ONE frame. A marble going round a banked
    bend also changes velocity every frame - steadily, by v^2*k/fps - so a plain threshold
    on the change fires continuously through every turn and buries the real hits in mud.
    Taking peaks instead is what separates the two: the turn is a plateau, the strike is a
    spike sitting on top of it.
    """
    picked = []
    n = len(series)
    for i in range(1, n - 1):
        v = series[i]
        if v < floor:
            continue
        if v < max(series[max(0, i - 2):i] + series[i + 1:min(n, i + 3)] or [0.0]):
            continue
        if picked and i - picked[-1][0] < min_gap:
            if v > picked[-1][1]:
                picked[-1] = (i, v)
            continue
        picked.append((i, v))
    return picked


def events_from(tracks, rim_z, end):
    """impacts.json events, every one of them taken from the simulated motion.

    For each marble the frame-to-frame velocity is differenced once more, giving the change
    of velocity per frame. Free fall alone contributes g/fps = 0.33 m/s at 30fps and a
    smooth roll contributes less, so anything peaking above IMPACT_FLOOR is a contact: a
    wall, the outside of a bend, another marble, or the catcher. Strength is that change
    normalised against 5 m/s, which is about what arriving in the bowl produces.
    """
    impact_floor = 0.95
    per_frame = {}
    for pts in tracks:
        n = len(pts)
        if n < 4:
            continue
        vel = [(0.0, 0.0, 0.0)]
        for f in range(1, n):
            a, b = pts[f - 1], pts[f]
            vel.append(((b[0] - a[0]) * FPS, (b[1] - a[1]) * FPS, (b[2] - a[2]) * FPS))
        dv = [0.0, 0.0]
        for f in range(2, n):
            a, b = vel[f - 1], vel[f]
            dv.append(math.dist(a, b))
        top = max(dv) or 1.0
        for idx, val in peaks_of(dv, 3, max(impact_floor, 0.20 * top)):
            frame = idx + 1                        # dv[idx] is the change arriving at idx
            if frame < 1 or frame > end:
                continue
            landed = pts[min(idx, n - 1)][2] <= rim_z + 0.05
            slot = per_frame.setdefault(frame, [0.0, landed])
            slot[0] += val
            slot[1] = slot[1] or landed
    events = []
    for frame in sorted(per_frame):
        val, landed = per_frame[frame]
        events.append({"frame": int(frame), "kind": "impact",
                       "strength": round(clamp(val / 5.0, 0.16, 1.0), 3),
                       "source": "catcher" if landed else "track"})
    cap = max(4, pi_("max_events"))
    if len(events) > cap:
        # Keep the LOUDEST, not every nth: on a marble run the quiet ones are a marble
        # brushing a wall and the loud ones are the arrivals, and the arrivals are the
        # whole sound design. First and last are always kept so the clip still opens and
        # closes on a hit.
        keep = {0, len(events) - 1}
        order = sorted(range(len(events)), key=lambda i: -events[i]["strength"])
        for i in order:
            if len(keep) >= cap:
                break
            keep.add(i)
        events = [events[i] for i in sorted(keep)]
    return events


def solve_camera(sc, static_pts, tracks, bounds, lens, end):
    """Place and, in follow mode, key the camera. Returns (cam, note).

    The direction to the camera is fixed by yaw and pitch and NEVER changes: the camera
    translates, it does not pan. A panning camera on a helix swings through the near side
    of the spiral, and a panning camera on a switchback follows the marble left and right
    into a mess.
    """
    yaw = math.radians(clamp(pf("yaw_deg"), -70.0, 70.0))
    pitch = math.radians(clamp(pf("pitch_deg"), 62.0, 88.0))
    fill_v = clamp(pf("fill"), 0.55, 0.94)
    fill_h = min(0.95, fill_v * 1.06)
    d = Vector((math.sin(yaw) * math.sin(pitch),
                -math.cos(yaw) * math.sin(pitch),
                math.cos(pitch))).normalized()
    quat = (-d).to_track_quat("-Z", "Y")

    bpy.ops.object.camera_add(location=(0, -8, 3))
    cam = bpy.context.object
    cam.name = "Cam"
    cam.data.lens = lens
    cam.data.clip_end = 500.0
    cam.rotation_euler = quat.to_euler()
    sc.camera = cam

    lo, hi = bounds
    def inside(p):
        # A marble that bounced out of the catcher and rolled away must not be allowed to
        # drag the whole frame back with it. Anything outside the apparatus plus a metre
        # is simply not framed for - it is a stray, and it is allowed to leave.
        return all(lo[k] - 1.0 <= p[k] <= hi[k] + 1.0 for k in range(3))

    if ps("camera") != "follow":
        pts = list(static_pts)
        for tr in tracks:
            pts += [p for p in tr if inside(p)]
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        zs = [p[2] for p in pts]
        centre = Vector(((min(xs) + max(xs)) / 2.0, (min(ys) + max(ys)) / 2.0,
                         (min(zs) + max(zs)) / 2.0))
        dist = max(1.2, solve_distance(pts, centre, d, lens, fill_v, fill_h))
        pos = centre + d * dist
        cam.location = (pos.x, pos.y, max(0.30, pos.z))
        return cam, f"wide dist={dist:.2f}"

    # ---- follow: aim at the LEADING marble, which is simply the lowest one that has been
    # released. Once it drops into the catcher it stays the lowest, so the shot settles on
    # the arrival by itself instead of needing a rule for when to stop following.
    span = clamp(pf("follow_span"), 0.8, 5.0)
    tan_v, tan_h = half_angles(lens)
    dist = max(1.2, max(span / (fill_v * tan_v), span / (fill_h * tan_h)))
    top = Vector(((lo[0] + hi[0]) / 2.0, (lo[1] + hi[1]) / 2.0, hi[2]))
    aim = []
    for f in range(end):
        best = None
        for tr in tracks:
            if f >= len(tr):
                continue
            p = tr[f]
            if not inside(p):
                continue
            if best is None or p[2] < best[2]:
                best = p
        aim.append(Vector(best) if best else top.copy())
    # Smoothed over +-8 frames (half a second). A camera operator does not twitch at every
    # bounce, and on a switchback the leading marble reverses across the frame at every
    # bend - unsmoothed, the camera slams with it.
    ax = smooth([a.x for a in aim], 8)
    ay = smooth([a.y for a in aim], 8)
    az = smooth([a.z for a in aim], 8)
    for f in range(1, end + 1):
        i = min(f - 1, len(ax) - 1)
        pos = Vector((ax[i], ay[i], az[i])) + d * dist
        cam.location = (pos.x, pos.y, max(0.30, pos.z))
        cam.keyframe_insert("location", frame=f)
    return cam, f"follow dist={dist:.2f} span={span:.2f}"


# -------------------------------------------------------------------------------- build


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
    sc.frame_start = 1
    # A thousand instances of two shared meshes: keeping them resident between frames costs
    # almost nothing and saves the per-frame sync on every single frame.
    sc.render.use_persistent_data = True
    # LINEAR keys, set BEFORE the first insert. The camera has a key on every frame and the
    # easing is already in the positions, so bezier handles would only add overshoot - and
    # Blender 5.2 moved Action.fcurves behind slotted actions, so interpolation can no
    # longer be corrected after the fact.
    bpy.context.preferences.edit.keyframe_new_interpolation_type = "LINEAR"

    sc.world = bpy.data.worlds.new("W")
    sc.world.use_nodes = True
    sc.world.node_tree.nodes["Background"].inputs[0].default_value = (0.013, 0.014, 0.018, 1)

    coll = sc.collection
    style = ps("track_style")
    if style not in ("tube", "half_pipe"):
        style = "tube"
    route = ps("track_path")
    if route not in ("switchback", "helix"):
        route = "switchback"

    rb, rt, wall = resolve_sizes()
    S = clamp(pf("track_length"), 6.0, 34.0)
    theta = math.radians(clamp(pf("slope_deg"), 6.0, 30.0))

    n_staves = len(stave_angles(style))
    samples, path_note, kappa_max, turns_used, seg = centreline(
        route, S, theta, rt, wall, n_staves)

    # ---- the queue of marbles, and how much track it eats. The loading rack is real
    # geometry occupying the top of the track, so the count is reduced until it fits inside
    # 45% of the length - dropping marbles off the END of the release order rather than out
    # of the middle of it, which would leave the clip opening on an empty track.
    n_req = int(clamp(pi_("ball_count"), 1, 24))
    release = ps("release")
    if release not in ("all", "one_by_one", "bursts"):
        release = "one_by_one"
    limit = min(0.45 * S, S - 1.2)
    n_balls = n_req
    while True:
        groups = make_groups(n_balls, release, pi_("burst_size"))
        ball_s, pin_s, fits = queue_layout(rb, groups, 0.22, limit)
        if fits or n_balls <= 1:
            break
        n_balls -= 1
    s_front = max(ball_s.values()) if ball_s else 0.3

    frames = bank_frames(samples, theta, s_front)

    # ---- lift the whole track so its exit sits `drop_height` above the catcher rim
    rim_guess = (0.06 if ps("catcher") == "bowl" else 0.05) + rb
    if ps("catcher") == "floor":
        rim_guess = rb
    z_exit = rim_guess + clamp(pf("drop_height"), 0.15, 1.60)
    lift = z_exit - frames[-1]["P"].z
    for fr in frames:
        fr["P"] = fr["P"] + Vector((0.0, 0.0, lift))

    # ---- where the marbles land, solved as a projectile out of the end of the tube.
    # `v_free` is the free-rolling exit speed (see ROLL) and it is an UPPER bound: rubbing
    # the outside of banked bends takes a real bite out of it, and how big a bite depends
    # on the route. So rather than trust one number, the arc is solved twice - at 45% and
    # at 100% of it - and the catcher is centred between the two landings and made wide
    # enough to cover both. Anything in that band arrives in the bin.
    exit_fr = frames[-1]
    v_free = math.sqrt(max(0.01, ROLL * G * math.sin(theta) * max(0.2, S - s_front)))
    Pe, Te = exit_fr["P"], exit_fr["T"]
    horiz = Vector((Te.x, Te.y, 0.0))
    hn = horiz.normalized() if horiz.length > 1e-6 else Vector((1, 0, 0))

    def landing(v):
        u = v * Te.z                       # vertical component of the exit velocity (down)
        fall = max(0.02, Pe.z - rim_guess)
        t = (u + math.sqrt(u * u + 2.0 * G * fall)) / G
        return Pe + hn * (v * horiz.length * t), t

    p_lo, _ = landing(0.45 * v_free)
    p_hi, t_hi = landing(1.00 * v_free)
    land = (p_lo + p_hi) * 0.5
    spread = (p_hi - p_lo).length * 0.5
    # Big enough to hold the marbles that arrive AND to cover the whole range of exit
    # speeds. sqrt(count) because they pack in two dimensions on the bottom.
    catch_r = max(0.34, spread + 3.0 * rb + rb * math.sqrt(max(1, n_balls)) * 1.5)

    # ---- materials
    tint = TINTS.get(ps("tube_tint"), TINTS["teal"])
    tube_kind = ps("tube_material")
    if tube_kind not in ("acrylic", "glass", "steel", "wood", "matte"):
        tube_kind = "acrylic"
    forced = ""
    if style == "tube" and tube_kind in ("steel", "wood", "matte"):
        # A closed opaque tube is a video of a pipe. Nothing about this scene works if the
        # marbles cannot be seen, so the material gives way rather than the shot.
        tube_kind, forced = "acrylic", " (opaque material overridden: closed tube)"
    if tube_kind == "glass":
        tube_mat = mat("Tube", (tint[0], tint[1], tint[2], 1), rough=0.03, trans=0.95)
    elif tube_kind == "acrylic":
        tube_mat = mat("Tube", (tint[0], tint[1], tint[2], 1), rough=0.10, alpha=0.30)
    elif tube_kind == "steel":
        tube_mat = mat("Tube", (0.62, 0.63, 0.68, 1), rough=0.24, metal=1.0)
    elif tube_kind == "wood":
        tube_mat = mat("Tube", (tint[0] * 0.55 + 0.18, tint[1] * 0.38 + 0.12,
                                tint[2] * 0.28 + 0.06, 1), rough=0.62)
    else:
        tube_mat = mat("Tube", (0.30, 0.31, 0.34, 1), rough=0.80)
    rig_mat = mat("Rig", RIG, rough=0.45, metal=0.75)

    spec = BALL_SPECS.get(ps("ball_material"), BALL_SPECS["glass"])
    palette = ps("ball_palette")
    hue0 = _RNG.random()                 # the seed decides the colours, so two runs of the
    n_col = 1 if palette == "single" else (2 if palette == "two_tone" else max(1, n_balls))
    ball_mats = []                       # same prompt never come back the same colour
    for i in range(n_col):
        if palette == "single" or spec["sat"] < 0.2:
            rgba = spec["color"]
        else:
            rgba = hue_rgba(hue0 + i / float(max(1, n_col)), spec["sat"], 0.85)
        ball_mats.append(mat(f"Ball{i}", rgba, rough=spec["rough"], metal=spec["metal"],
                             trans=spec["trans"], emit=spec["emit"]))

    # ---- floor first: it is what creates the rigid body world every other body joins
    reach = max(2.0, max(abs(fr["P"].x) for fr in frames) * 2.0,
                max(abs(fr["P"].y) for fr in frames) * 2.0, catch_r * 2.0)
    floor = build_floor(reach)

    staves = build_track(frames, style, rt, wall, seg, kappa_max, tube_mat, coll)
    posts, post_pts = build_posts(frames, rt, wall, (land.x, land.y, catch_r * 1.15),
                                  rig_mat, coll)
    catch_objs, catch_pts, rim_z = build_catcher(ps("catcher"), (land.x, land.y),
                                                 catch_r, rb, rig_mat, coll)

    # ---- the drop pins: real gates, not a trick. Each one stands up out of the channel
    # floor in front of its group and retracts straight down through it at the release
    # frame, which is exactly what a marble-run gate does and means every marble in the
    # queue is genuinely held by something the solver can see.
    gap_frames = max(1, int(round(clamp(pf("release_gap"), 0.15, 1.60) * FPS)))
    pin_h = 1.30 * rb
    pin_me = box_mesh("PinMesh", 2.2 * rb, PIN_T, pin_h, rig_mat)
    pin_objs, pin_frames_at = [], {}
    for g in sorted(pin_s):
        fr = frame_at(frames, pin_s[g])
        ob = new_object(f"Pin{g:02d}", pin_me, coll)
        place(ob, fr["P"] - fr["U"] * (rt - pin_h / 2.0), fr["S"], fr["T"], fr["U"])
        pin_objs.append((g, ob))
        drop_at = PRE_ROLL if release == "all" else PRE_ROLL + g * gap_frames
        # +-1 frame of seeded dither on everything after the first gate. It is inaudible
        # as a rhythm change and it is enough to stop two marbles arriving in the catcher
        # on the same frame in every single render.
        if g > 0:
            drop_at += _RNG.choice((-1, 0, 0, 1))
        pin_frames_at[g] = max(2, drop_at)
    pins = [ob for _g, ob in pin_objs]

    # ---- the marbles. One sphere is built, then its MESH is copied once per palette
    # colour, because a material lives on the mesh when objects share a datablock. The
    # explicit deselect matters: shade_smooth works on the selection, and the catcher's
    # cylinder is still selected from the operator that made it.
    bpy.ops.object.select_all(action="DESELECT")
    bpy.ops.mesh.primitive_uv_sphere_add(radius=rb, segments=32, ring_count=16,
                                         location=(0, 0, -50))
    proto = bpy.context.object
    bpy.ops.object.shade_smooth()
    proto_me = proto.data
    ball_meshes = []
    for i, bm in enumerate(ball_mats):
        me = proto_me.copy()
        me.name = f"BallMesh{i}"
        me.materials.clear()
        me.materials.append(bm)
        ball_meshes.append(me)
    bpy.data.objects.remove(proto, do_unlink=True)

    balls, ball_release = [], {}
    lateral = max(0.0, (rt - rb) * 0.30)
    for g, idxs in enumerate(groups):
        for idx in idxs:
            fr = frame_at(frames, ball_s[idx])
            # The seed's real work. A marble entering the first banked bend a fraction of a
            # millimetre off centre takes a different line from there to the bottom, so this
            # is what makes two runs of identical parameters different videos rather than
            # the same file twice.
            off = fr["S"] * _RNG.uniform(-lateral, lateral) \
                + fr["T"] * _RNG.uniform(-0.08 * rb, 0.08 * rb)
            ob = new_object(f"Ball{idx:02d}", ball_meshes[idx % len(ball_meshes)], coll)
            place(ob, fr["P"] - fr["U"] * (rt - rb) + off,
                  Vector((1, 0, 0)), Vector((0, 1, 0)), Vector((0, 0, 1)))
            balls.append(ob)
            # Released one frame BEFORE its pin starts moving, so the solver owns the
            # contact between marble and gate instead of a kinematic body shoving its way
            # out from under a body that is still being animated.
            ball_release[ob.name] = max(2, pin_frames_at.get(g, PRE_ROLL) - 1)

    # ---- rigid bodies
    add_rigid(staves + posts + catch_objs, "PASSIVE")
    add_rigid(pins, "PASSIVE", kinematic=True)
    add_rigid(balls, "ACTIVE")
    # Re-assert the floor. add_rigid works on a selection and a floor swept into it would
    # become ACTIVE and fall out from under the entire scene - a failure that renders
    # perfectly happily and is baffling to diagnose from the frames.
    floor.rigid_body.type = "PASSIVE"
    floor.rigid_body.kinematic = False

    catch_set = {ob.name for ob in catch_objs}
    for ob in staves + posts + catch_objs + pins:
        rb_ = ob.rigid_body
        # Everything here is a box except the bowl's round base, which is the one place a
        # convex hull is cheaper than approximating a disc out of slabs.
        rb_.collision_shape = "CONVEX_HULL" if ob.name == "BowlBase" else "BOX"
        rb_.friction = 0.60
        # The catcher is the only surface that is supposed to ring: a marble arriving from
        # half a metre up has to rattle, not land dead.
        rb_.restitution = 0.45 if ob.name in catch_set else 0.30
        rb_.use_margin = True
        # NOT the 0.04 default, which would inflate every 24mm wall to nearly three times
        # its thickness and close the channel around the marbles before anything moves.
        rb_.collision_margin = 0.002

    ball_mass = spec["density"] * (4.0 / 3.0) * math.pi * rb ** 3
    for ob in balls:
        rb_ = ob.rigid_body
        rb_.mass = max(0.002, ball_mass)
        rb_.collision_shape = "SPHERE"
        rb_.friction = spec["fric"]
        rb_.restitution = spec["rest"]
        rb_.linear_damping = 0.0
        rb_.angular_damping = 0.02
        # Deactivation OFF. A marble creeping along a shallow stretch is exactly what
        # Bullet's sleep test calls "at rest", and a sleeping marble halfway down the track
        # never wakes up again. Twenty-odd bodies cost nothing to keep awake.
        rb_.use_deactivation = False
        f_rel = ball_release[ob.name]
        # Held kinematic behind its gate. The flag is keyed on the frame BEFORE the switch
        # as well as on it: a boolean fcurve interpolated linearly from a key at frame 1
        # would read as False long before the release frame and let the whole queue go at
        # once.
        rb_.kinematic = True
        ob.keyframe_insert("rigid_body.kinematic", frame=1)
        ob.keyframe_insert("rigid_body.kinematic", frame=max(1, f_rel - 1))
        rb_.kinematic = False
        ob.keyframe_insert("rigid_body.kinematic", frame=f_rel)

    for g, ob in pin_objs:
        f_drop = pin_frames_at[g]
        fr = frame_at(frames, pin_s[g])
        up_pos = ob.location.copy()
        ob.keyframe_insert("location", frame=1)
        ob.keyframe_insert("location", frame=f_drop)
        # Straight down through the channel floor, far enough that the top of the pin ends
        # below the inside of the track - and hidden by the track's own floor staves,
        # because the camera always looks slightly DOWN at the run.
        ob.location = up_pos - fr["U"] * (pin_h + 2.0 * wall + 0.012)
        ob.keyframe_insert("location", frame=f_drop + 3)

    # ---- solver. Substeps are solved from the top speed and the wall thickness: a marble
    # that travels further than one wall thickness between two substeps passes straight
    # through the outside of a bend, and that is the single failure that would empty the
    # track without any other sign that something went wrong.
    v_max = math.sqrt(max(0.01, ROLL * G * math.sin(theta) * S))
    rw = sc.rigidbody_world
    rw.substeps_per_frame = int(clamp(math.ceil(v_max * 1.3 / (FPS * wall)), 12, 60))
    rw.solver_iterations = 24

    target = max(2, int(round(SECONDS * FPS)))
    ceiling = max(target, int(round(clamp(pf("max_seconds"), 3.0, 16.0) * FPS))) \
        if pb("auto_length") else target
    last_release = max(pin_frames_at.values()) if pin_frames_at else PRE_ROLL
    sim_end = max(2, PREVIEW) if PREVIEW else ceiling
    rw.point_cache.frame_start = 1
    rw.point_cache.frame_end = sim_end
    sc.frame_end = sim_end

    lens = clamp(pf("lens"), 24.0, 70.0)
    settle = int(round(clamp(pf("settle_seconds"), 0.0, 2.5) * FPS))

    est = math.sqrt(2.0 * max(0.2, S - s_front) / max(0.05, (5.0 / 7.0) * G * math.sin(theta)))
    print(f"SCENE_INFO {path_note}{forced} style={style} S={S:.1f}m slope="
          f"{math.degrees(theta):.0f}deg rings={len(frames)} staves={len(staves)} "
          f"balls={n_balls}/{n_req} rt={rt:.3f} rb={rb:.3f} wall={wall:.3f} "
          f"substeps={rw.substeps_per_frame} v_max={v_max:.1f}m/s "
          f"descent~{est:.1f}s last_release={last_release / FPS:.1f}s "
          f"catcher_r={catch_r:.2f} flight={t_hi:.2f}s")

    # ---- simulate BEFORE the camera is placed and before anything is rendered
    tracks, done_at = prepass(
        sc, balls, last_release, sim_end,
        stop_when_done=(not PREVIEW) and pb("auto_length"), settle_frames=settle)

    if PREVIEW:
        end = sim_end
    elif not pb("auto_length"):
        end = target
    elif done_at:
        end = int(clamp(done_at + settle, int(1.2 * FPS), ceiling))
    else:
        # Nothing settled inside the ceiling - a marble is still creeping somewhere. Run to
        # whichever is longer, the requested length or the time the last marble needs to
        # get down (release + descent + settle), and let max_seconds cap it. Falling back
        # to `seconds` here is what would cut a long run off halfway down the track.
        end = int(clamp(last_release + est * FPS + settle, target, ceiling))
    end = max(2, min(end, sim_end))
    sc.frame_end = end

    # ---- framing points: the whole apparatus, standing still
    static_pts = []
    for fr in frames:
        for a in (fr["U"], -fr["U"], fr["S"], -fr["S"]):
            p = fr["P"] + a * (rt + wall)
            static_pts.append((p.x, p.y, p.z))
    static_pts += catch_pts + post_pts
    if ps("catcher") == "floor":
        static_pts += [(land.x + sx * catch_r, land.y + sy * catch_r, 0.0)
                       for sx in (-1, 1) for sy in (-1, 1)]
    xs = [p[0] for p in static_pts]
    ys = [p[1] for p in static_pts]
    zs = [p[2] for p in static_pts] + [0.0]
    bounds = ((min(xs), min(ys), min(zs)), (max(xs), max(ys), max(zs)))

    build_lights(((bounds[0][0] + bounds[1][0]) / 2.0, (bounds[0][1] + bounds[1][1]) / 2.0),
                 max(bounds[1][0] - bounds[0][0], bounds[1][1] - bounds[0][1]) / 2.0 + 0.6,
                 bounds[1][2])
    cam, cam_note = solve_camera(sc, static_pts, tracks, bounds, lens, end)
    if LABEL:
        build_label(cam, lens)

    if tube_kind == "acrylic":
        # Alpha is not free in Cycles: every see-through surface a ray crosses spends one
        # TRANSPARENT bounce, and the default budget is 8. Looking into a closed tube costs
        # two before the marble, and looking through several coils of a helix costs a dozen
        # - past the budget the ray is simply terminated and the tube goes black in exactly
        # the places the marbles are. Transparent bounces are nearly free to trace, so this
        # is raised well past what any framing needs.
        sc.cycles.transparent_max_bounces = 32
    if tube_kind == "glass" or spec["trans"]:
        # A dozen refracting facets between the camera and a marble is minutes per frame at
        # the default bounce budget and still noisy. Six transmission bounces is enough to
        # read as glass in a tube; beyond that only the caustics improve.
        sc.cycles.max_bounces = 10
        sc.cycles.transmission_bounces = 6

    events = events_from(tracks, rim_z, end)
    with open(f"{OUT}/impacts.json", "w", encoding="utf-8") as fh:
        json.dump({"fps": FPS, "events": events,
                   "track": {"path": route, "style": style, "length": round(S, 2),
                             "slope_deg": round(math.degrees(theta), 1),
                             "turns": round(float(turns_used), 2),
                             "rings": len(frames), "note": path_note},
                   "balls": n_balls, "release": release,
                   "catcher": ps("catcher"),
                   "exit_speed": round(v_free, 2),
                   "frames": end, "seconds": round(end / float(FPS), 2)}, fh, indent=1)

    tail = (f"{route}/{style} balls={n_balls} events={len(events)} "
            f"frames={end} ({end / FPS:.2f}s) {cam_note} {path_note}")

    sc.render.image_settings.file_format = "PNG"      # Blender 5.2 has no FFMPEG output
    sc.render.image_settings.color_mode = "RGB"
    if PREVIEW:
        n = max(1, min(sim_end, PREVIEW))
        sc.frame_set(n)
        sc.render.filepath = f"{OUT}/preview.png"
        bpy.ops.render.render(write_still=True)
        print(f"SCENE_OK preview={n} {tail}")
        return

    sc.render.filepath = f"{OUT}/frame_"
    bpy.ops.render.render(animation=True)
    print(f"SCENE_OK {tail}")


if bpy is not None and __name__ == "__main__":
    main()
