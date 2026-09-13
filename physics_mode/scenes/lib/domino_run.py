"""A run of standing slabs toppling in sequence - line, arc or inward spiral.

    blender -b -noaudio -P domino_run.py -- '{"path":"spiral","count":160,"out_dir":"..."}'

This is a HAND-MAINTAINED parametric scene. A model picks the scene and fills in PARAMS;
it never writes geometry. Everything that could be solved has been solved rather than
guessed, and the numbers are derived in the comments where they are used.

The three things that decide whether this looks satisfying or broken:

  SPACING. The chain has to propagate for hundreds of slabs without a single stall. The
  parameter is a pitch (centre-to-centre) as a fraction of slab height, but the physics
  lives in the GAP (pitch minus thickness) - see the derivation above `clamp_gap`.

  SIMULATION FIRST, CAMERA SECOND. The scene is stepped through the solver before a single
  camera key is written, so the tracking camera follows the wave the solver actually
  produced instead of a predicted one. Same data feeds impacts.json, so every click lands
  on the frame a slab was really struck.

  COLLISION MARGIN. Blender's default rigid-body margin is 0.04m. A slab is ~0.08m thick,
  so the default margin inflates every slab by half its own thickness: a row built with a
  visible gap is already interpenetrating in the solver and the whole line collapses at
  once. `use_margin` + a sub-millimetre margin is not a nicety here, it is the difference
  between a domino run and a falling wall.
"""

import json
import math
import sys

import bpy
import mathutils

# --------------------------------------------------------------------------- parameters

PARAMS = {
    "seed": {
        "range": [0, 9999], "default": 0,
        "note": "shifts every slab by a fraction of a millimetre before the run starts. "
                "The topple is chaotic, so a different seed sends the same run down "
                "differently. Vary it so two videos of the same idea are not identical.",
    },

    # ---- the run itself
    "count": {
        "range": [24, 320], "default": 120,
        "note": "How many slabs stand in the run. More is better looking but takes longer "
                "to fall: the wave needs roughly 0.08 * count * sqrt(slab_height) seconds "
                "to travel the whole run (120 slabs at the default size is about 6s). If "
                "auto_length is on the render simply lasts until the last slab is down."},
    "path": {
        "choices": ["line", "arc", "spiral"], "default": "arc",
        "note": "Shape the run is laid along. 'line' is a straight row running away from "
                "the camera; 'arc' is a single sweeping curve; 'spiral' winds inward and "
                "ends in the middle, which is the most satisfying of the three but fills "
                "a tall 9:16 frame less completely because a spiral is round."},
    "spacing": {
        "range": [0.30, 0.95], "default": 0.55,
        "note": "Centre-to-centre distance between neighbouring slabs, as a fraction of "
                "slab height. 0.55 is the sweet spot. Low values make the run behave like "
                "a leaning wall (each slab shoves the next instead of striking it); high "
                "values make each slab fall further before it reaches the next, so the "
                "wave is slow and eventually stops reaching at all. Values outside the "
                "band that still propagates are clamped, so a bad number cannot stall the "
                "run - it just stops changing anything."},
    "arc_span_deg": {
        "range": [30, 320], "default": 150,
        "note": "path='arc' only: total turn from the first slab to the last, in degrees. "
                "The radius is derived from this and the length of the run, so a bigger "
                "span means a tighter curve."},
    "spiral_turns": {
        "range": [1.0, 5.0], "default": 2.5,
        "note": "path='spiral' only: how many revolutions the run makes on its way in. "
                "Reduced automatically if the innermost turn would come out too tight to "
                "keep toppling."},

    # ---- what the slabs are
    "material": {
        "choices": ["wood", "stone", "concrete", "steel", "ceramic", "glass"],
        "default": "ceramic",
        "note": "What the slabs are made of. Purely a look-and-sound choice: every slab is "
                "the same material, and a gravity-driven topple does not care about mass, "
                "so the run behaves identically for all of them. 'glass' is the prettiest "
                "and the most expensive - it needs roughly double the samples before the "
                "refractions stop looking grainy."},
    "slab_height": {
        "range": [0.15, 1.20], "default": 0.42,
        "note": "Height of one slab in metres. Everything else scales off it. Bigger slabs "
                "do NOT finish faster: the run gets proportionally longer while the wave "
                "only speeds up with the square root of the height."},
    "slab_width_ratio": {
        "range": [0.35, 0.80], "default": 0.50,
        "note": "Slab width as a fraction of its height (0.5 = a real domino). Wider slabs "
                "read better from far away but need a gentler curve to stay in contact."},
    "slab_thickness_ratio": {
        "range": [0.10, 0.30], "default": 0.19,
        "note": "Slab thickness as a fraction of its height. This sets the balance angle "
                "(atan(thickness/height)) - thin slabs tip from a smaller nudge and topple "
                "faster; thick ones are sluggish and can absorb the hit."},

    # ---- colour
    "colour_mode": {
        "choices": ["material", "alternate", "gradient"], "default": "gradient",
        "note": "'material' = every slab the material's own colour. 'alternate' = two "
                "tones, which makes each individual slab readable. 'gradient' = a hue ramp "
                "along the whole run, which is what makes the travelling wave obvious at "
                "phone size - you can see exactly where the front is."},
    "hue_start": {
        "range": [0.0, 1.0], "default": 0.02,
        "note": "colour_mode='gradient': hue at the first slab (0 = red, 0.33 = green, "
                "0.66 = blue)."},
    "hue_span": {
        "range": [0.0, 1.0], "default": 0.75,
        "note": "colour_mode='gradient': how far around the colour wheel the ramp travels "
                "from the first slab to the last."},
    "gradient_saturation": {
        "range": [0.0, 1.0], "default": 0.80,
        "note": "How saturated the gradient is. Drop it towards 0.4 for steel or glass, "
                "where a fully saturated tint looks like coloured plastic."},

    # ---- the trigger
    "start_tilt_deg": {
        "range": [0.0, 30.0], "default": 0.0,
        "note": "How far the first slab already leans at frame 1, which is what starts the "
                "whole run. 0 means auto: the balance angle atan(thickness/height) plus "
                "3 degrees, i.e. just far enough that gravity alone takes it over. Nothing "
                "else touches the run - there is no pusher object to keep in frame."},

    # ---- camera
    "camera": {
        "choices": ["overview", "track"], "default": "overview",
        "note": "'overview' watches the whole path from above at a downward angle and never "
                "moves - the safe choice, everything stays in frame by construction. "
                "'track' chases the falling front along the path in a three-quarter view, "
                "which is far more dramatic on a long run but only ever shows a few metres "
                "of it."},
    "camera_pitch_deg": {
        "range": [0.0, 80.0], "default": 0.0,
        "note": "How far above horizontal the camera looks down. 0 means auto (36 for a "
                "line, 42 for an arc, 48 for a spiral). Raised automatically if it would "
                "be shallow enough to let the empty world background into the top of the "
                "frame. Do not push it towards 80: overhead flattens the topple and the "
                "collision stops reading as a collision."},
    "lens": {
        "range": [0.0, 70.0], "default": 0.0,
        "note": "Focal length in mm. 0 means auto (34 for the overview, 30 for the tracking "
                "camera). Wide is deliberate: perspective makes the near end of the run "
                "loom and the far end recede, which is the only way a flat ground pattern "
                "fills a tall 9:16 frame."},
    "track_span": {
        "range": [1.5, 12.0], "default": 4.5,
        "note": "camera='track': how many metres of the run are visible at once. Small "
                "values feel fast and lose the sense of scale; large values turn the "
                "tracking shot back into an overview."},
    "track_yaw_deg": {
        "range": [0.0, 90.0], "default": 0.0,
        "note": "camera='track': how far round the side of the run the chase camera sits. "
                "0 means auto, which puts the visible strip of run on the frame diagonal - "
                "about 18 degrees off straight-behind, and worth roughly three times the "
                "frame coverage of any other angle. Large values are a deliberate choice, "
                "not a better one: at 90 the run crosses the frame as a thin band and uses "
                "6% of the height of a 9:16 short."},

    # ---- floor and light
    "floor_checker": {
        "choices": [True, False], "default": True,
        "note": "Put a low-contrast checker on the floor. It costs nothing and stops the "
                "empty parts of a tall frame reading as dead black - it also gives the eye "
                "a scale to measure the run against."},
    "sun_energy": {
        "range": [0.5, 8.0], "default": 2.6,
        "note": "Key light strength. A sun, not a lamp, on purpose: its brightness does not "
                "fall off with distance, so one value lights a 3m spiral and a 40m line "
                "identically and nothing has to be re-tuned when count changes."},

    # ---- length and events
    "auto_length": {
        "choices": [True, False], "default": True,
        "note": "Let the render end when the run does. The scene is simulated before it is "
                "rendered, so the finishing frame is known: with this on the video is "
                "trimmed to the last slab plus a short settle, or extended up to "
                "max_seconds if `seconds` would have cut the run in half. Turn it off to "
                "get exactly `seconds` of video whatever the run does."},
    "max_seconds": {
        "range": [2.0, 20.0], "default": 10.0,
        "note": "Hard ceiling on the render length when auto_length is on. Also the length "
                "the physics cache is sized for, so nothing can simulate past it."},
    "settle_seconds": {
        "range": [0.0, 2.5], "default": 0.8,
        "note": "How long to keep rolling after the last slab is struck, so the pile is "
                "seen coming to rest instead of the video cutting on the final click."},
    "max_events": {
        "range": [8, 240], "default": 44,
        "note": "Cap on how many clicks are written to impacts.json. A 200-slab run really "
                "does produce 200 contacts, but the mixer opens one audio input per event, "
                "so the list is thinned evenly (first and last always kept) and the "
                "survivors carry a little more weight."},
    "motion_blur": {
        "choices": [False, True], "default": False,
        "note": "Cycles motion blur. It is the single biggest look upgrade available here - "
                "at 30fps a fast wave strobes without it - but it makes Cycles evaluate the "
                "rigid-body cache at sub-frame times, so turn it on for one test render and "
                "look at the frames before trusting it on a long job."},

    # ---- render contract (supplied by the runner)
    "out_dir": {"default": "//out", "note": "Where frames, preview.png and impacts.json go."},
    "res_x": {"range": [256, 2160], "default": 1080, "note": "Render width."},
    "res_y": {"range": [256, 3840], "default": 1920, "note": "Render height. The camera is "
              "solved for whichever axis is larger, so 9:16 works without extra tuning."},
    "samples": {"range": [8, 512], "default": 24, "note": "Cycles samples per pixel. 24 with "
                "denoising is enough for everything except glass."},
    "fps": {"range": [12, 60], "default": 30, "note": "Frames per second."},
    "seconds": {"range": [1.0, 20.0], "default": 6.0,
                "note": "Target length. See auto_length - the finished clip can be shorter "
                        "if the run ends early or longer if it would have been cut off."},
    "preview_frame": {"range": [0, 600], "default": 0,
                      "note": "If greater than 0, simulate up to this frame, render it "
                              "alone to preview.png and stop. Used for the approval gate."},
}

argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
P = json.loads(argv[0]) if argv else {}

import random as _random

# A fraction of the slab height. 0.0025 is a few hundredths of a millimetre on a default
# slab - invisible, and nowhere near the spacing margin that keeps the chain alive.
JITTER = 0.0025
SEED = int(P.get("seed", 0) or 0)
_RNG = _random.Random(SEED)


def _default(key):
    return PARAMS[key]["default"]


def pf(key) -> float:
    try:
        return float(P.get(key, _default(key)))
    except (TypeError, ValueError):
        return float(_default(key))


def pi(key) -> int:
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
RES_X, RES_Y = pi("res_x"), pi("res_y")
SAMPLES = pi("samples")
FPS = max(1, pi("fps"))
SECONDS = pf("seconds")
PREVIEW = pi("preview_frame")

# Slabs of these six materials. `density` is only used to give the rigid bodies a
# plausible mass - the run itself is gravity driven and propagates identically whatever
# the mass is, as long as every slab has the SAME one.
MATERIALS = {
    "wood":     {"colour": (0.40, 0.24, 0.11), "rough": 0.55, "metal": 0.0, "density": 700},
    "stone":    {"colour": (0.31, 0.31, 0.34), "rough": 0.72, "metal": 0.0, "density": 2400},
    "concrete": {"colour": (0.40, 0.39, 0.37), "rough": 0.86, "metal": 0.0, "density": 2350},
    "steel":    {"colour": (0.62, 0.63, 0.67), "rough": 0.22, "metal": 1.0, "density": 7800},
    "ceramic":  {"colour": (0.88, 0.88, 0.91), "rough": 0.13, "metal": 0.0, "density": 2300},
    "glass":    {"colour": (0.80, 0.90, 0.95), "rough": 0.04, "metal": 0.0, "density": 2500,
                 "transmission": 1.0, "ior": 1.45},
}
# Auto camera pitch per path shape, chosen by sweeping pitch against lens and measuring
# how much of the frame the run's footprint actually covers (see diagonal_offset). A line
# and an arc are flat at 38/44 degrees; a spiral is round, so it only gains from being
# looked down on harder - 52 is where the gain stops being worth the flattening.
AUTO_PITCH = {"line": 38.0, "arc": 44.0, "spiral": 52.0}
GRADIENT_STEPS = 14          # shared materials, not one per slab
TILT_TRIGGER_DEG = 7.0       # a slab is "struck" once it leans this far off vertical
MAX_TURN_DEG = 12.0          # hardest corner one slab may turn - see curve_radius_floor


# ------------------------------------------------------------------------------ helpers


def use_gpu(sc) -> str:
    """Point Cycles at the GPU. OptiX first (RTX ray-tracing cores), then CUDA."""
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


def hue_rgba(hue: float, sat: float, val: float):
    c = mathutils.Color((0.0, 0.0, 0.0))
    c.hsv = (hue % 1.0, max(0.0, min(1.0, sat)), max(0.0, min(1.0, val)))
    return (c.r, c.g, c.b, 1.0)


def slab_material(name: str, rgba, spec: dict):
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    b = m.node_tree.nodes["Principled BSDF"]
    b.inputs["Base Color"].default_value = rgba
    b.inputs["Roughness"].default_value = spec["rough"]
    b.inputs["Metallic"].default_value = spec["metal"]
    if spec.get("transmission"):
        # Blender renamed this socket ("Transmission" -> "Transmission Weight"). Try both
        # rather than pin a version: a KeyError here would kill the whole render.
        for socket in ("Transmission Weight", "Transmission"):
            try:
                b.inputs[socket].default_value = float(spec["transmission"])
                break
            except KeyError:
                continue
        try:
            b.inputs["IOR"].default_value = float(spec.get("ior", 1.45))
        except KeyError:
            pass
    return m


def box_mesh(name: str, sx: float, sy: float, sz: float, material, bevel: float):
    """One shared mesh datablock per palette colour.

    The dimensions go into the VERTICES, never into object scale. A rigid body's BOX shape
    is taken from the object, and an object left at scale 1 keeps a collision box exactly
    the size of the thing you can see - with a scaled object the two drift apart as soon as
    anything else in the pipeline touches the transform.

    Building the mesh once and re-using the datablock also avoids 300 primitive_cube_add()
    operator calls, each of which pushes a depsgraph update over the whole scene.
    """
    hx, hy, hz = sx / 2.0, sy / 2.0, sz / 2.0
    verts = [(-hx, -hy, -hz), (hx, -hy, -hz), (hx, hy, -hz), (-hx, hy, -hz),
             (-hx, -hy, hz), (hx, -hy, hz), (hx, hy, hz), (-hx, hy, hz)]
    # Wound so every normal points outwards (checked with the right-hand rule per face).
    faces = [(0, 3, 2, 1), (4, 5, 6, 7), (0, 1, 5, 4),
             (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7)]
    me = bpy.data.meshes.new(name)
    me.from_pydata(verts, [], faces)
    me.update()
    if bevel > 0.0:
        # A hard-edged box in a dark scene loses its silhouette against its neighbours. A
        # bevel a few percent of the thickness catches a highlight along every edge, which
        # is what makes a row of slabs read as separate objects instead of a striped wall.
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
        except Exception:  # noqa: BLE001 - bevel is cosmetic, never worth failing over
            pass
    me.materials.append(material)
    return me


# ---------------------------------------------------------------------- spacing solution


def clamp_gap(gap: float, h: float) -> float:
    """Force the edge-to-edge gap into the band where the chain reaction actually runs.

    A slab tips about its leading bottom edge, so its top-front corner travels on a circle
    of radius `h`. After rotating by alpha it has reached forward by h*sin(alpha) and is at
    height h*cos(alpha). It therefore touches its neighbour at

        alpha_contact = asin(gap / h)      contact height = h * sqrt(1 - (gap/h)^2)

    which gives both ends of the useful band directly:

      * gap -> h  the slab is nearly flat on the floor before it reaches the next one, so
        it arrives with no height and no leverage. Past about 0.85h it misses entirely and
        the run just stops. 0.74h is taken as the ceiling, leaving contact at 0.67h - still
        comfortably above the neighbour's centre of mass at h/2, which is what actually
        matters: a strike BELOW the centre of mass pushes the slab along the floor instead
        of tipping it.
      * gap -> 0  contact happens at alpha ~ 0, before the slab has passed its own balance
        angle atan(t/h) ~ 11 degrees, so it is still being held up by its own base when it
        meets the next one. The row leans as one body and hands almost no energy forward;
        on a curve the inner corners jam against each other and it dies. 0.22h keeps
        contact at alpha ~ 13 degrees, just past balance.

    The default spacing of 0.55h pitch minus 0.19h thickness lands on a gap of 0.36h:
    contact at 21 degrees and at 93% of the slab height, near the top of the next slab.
    That is the fastest, most reliable part of the band.
    """
    return max(0.22 * h, min(0.74 * h, gap))


def curve_radius_floor(gap: float, pitch: float, h: float, w: float) -> float:
    """Smallest turn radius the run survives. Two separate limits, whichever binds.

    FAN. On a curve the slabs splay: with a turn of dtheta = pitch/R per slab, the two
    outer corners are (w/2)*dtheta further apart than the centres and the two inner corners
    are (w/2)*dtheta closer. The run dies if the outer corners open past the gap ceiling
    and it jams if the inner corners close onto each other, so the fan has to fit in
    whatever slack the chosen gap has left on each side:

        (w * pitch) / (2R) <= min(0.80h - gap, gap - 0.08h)

    with a 15% margin. This binds for wide slabs.

    TURN ANGLE. The fan limit alone is not enough, and it fails silently: with narrow slabs
    the fan term goes to nothing and it will happily accept a 40-degree turn per slab, at
    which point a slab is no longer striking the face of its neighbour but poking a corner
    at it, and the centres are joined by a chord that is nothing like the arc length that
    was stepped along. Capping dtheta at MAX_TURN_DEG fixes both: at 12 degrees the strike
    is still across most of the face, and chord and arc agree to within 0.2%, so the gap
    solved for on a straight is the gap that actually gets built on the curve.
    """
    slack = min(0.80 * h - gap, gap - 0.08 * h)
    fan = 50.0 if slack <= 1e-4 else 1.15 * (w * pitch) / (2.0 * slack)
    return max(fan, pitch / math.radians(MAX_TURN_DEG))


# ------------------------------------------------------------------------------ the path


def build_path(shape: str, count: int, pitch: float, w: float, r_floor: float):
    """Lay out the run. Returns [(x, y, tangent_x, tangent_y, arclength), ...] and a note.

    Every shape is walked by ARC LENGTH in steps of `pitch`, so the spacing between
    neighbours is the spacing that was solved for, on a straight and on a curve alike.
    """
    pts = []
    note = shape
    if shape == "line":
        for i in range(count):
            # Runs along +Y, away from the camera. In a 9:16 frame a receding line uses the
            # tall axis; the same line laid across X would be over in a third of the frame.
            pts.append((0.0, i * pitch, 0.0, 1.0, i * pitch))
        return pts, note

    length = max(pitch, (count - 1) * pitch)

    if shape == "arc":
        span = math.radians(max(5.0, pf("arc_span_deg")))
        radius = max(length / span, r_floor)
        span = length / radius              # if the radius had to be raised, the arc opens
        # Centre at (R, 0) so the run starts at the origin heading +Y and curves toward +X.
        for i in range(count):
            a = (i * pitch) / radius
            pts.append((radius - radius * math.cos(a), radius * math.sin(a),
                        math.sin(a), math.cos(a), i * pitch))
        note = f"arc R={radius:.2f} span={math.degrees(span):.0f}deg"
        return pts, note

    # ---- spiral, wound from the outside inwards: the wave converges on the middle, which
    # is the most satisfying ending of the three, and it keeps the whole run near the
    # camera instead of trailing off to a vanishing point.
    turns = max(1.0, pf("spiral_turns"))
    # Radial distance between consecutive turns must clear a slab's width with room to
    # spare, or a falling slab reaches across into the turn beside it and the run
    # short-circuits into its own tail. 2.0 widths is the clearance.
    b = 2.0 * w / (2.0 * math.pi)
    r_min = max(r_floor, w * 1.5)
    for _ in range(20):
        dphi = 2.0 * math.pi * turns
        # Archimedean spiral length is close to (mean radius * total angle) whenever the
        # radius is much larger than b, which it is here - good enough to pick r_out, and
        # the actual placement below is stepped numerically anyway.
        r_avg = length / dphi
        r_in = r_avg - b * dphi / 2.0
        if r_in >= r_min or turns <= 1.0:
            break
        turns = max(1.0, turns - 0.25)      # unwind: fewer turns -> larger mean radius
    dphi = 2.0 * math.pi * turns
    # At least one full turn's worth of radius above the floor. Without this a short run
    # (the estimate scales with its length) starts inside r_min and places NO slabs at all.
    r_out = max(length / dphi + b * dphi / 2.0, r_min + b * 2.0 * math.pi)

    phi, r, s = 0.0, r_out, 0.0
    while len(pts) < count and r > r_min:
        cx, cy = math.cos(phi), math.sin(phi)
        tx, ty = -b * cx - r * cy, -b * cy + r * cx      # d/dphi of (r cos, r sin), r'=-b
        m = math.hypot(tx, ty) or 1.0
        pts.append((r * cx, r * cy, tx / m, ty / m, s))
        phi += pitch / math.hypot(r, b)
        r = r_out - b * phi
        s += pitch
    note = f"spiral turns={turns:.2f} r_out={r_out:.2f} r_in={r:.2f} placed={len(pts)}"
    return pts, note


def path_at(pts, pitch: float, s: float):
    """Position and unit tangent at arc length `s`, clamped to the ends of the run."""
    n = len(pts)
    if n == 1:
        a = pts[0]
        return mathutils.Vector((a[0], a[1], 0.0)), mathutils.Vector((a[2], a[3], 0.0))
    k = max(0.0, min(float(n - 1), s / pitch))
    i0 = min(n - 2, int(math.floor(k)))
    t = k - i0
    a, b = pts[i0], pts[i0 + 1]
    tx, ty = a[2] + (b[2] - a[2]) * t, a[3] + (b[3] - a[3]) * t
    m = math.hypot(tx, ty) or 1.0
    return (mathutils.Vector((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, 0.0)),
            mathutils.Vector((tx / m, ty / m, 0.0)))


# ---------------------------------------------------------------------------- the camera


def half_angles(lens: float, res_x: int, res_y: int):
    """tan of the half field of view, vertical and horizontal.

    Blender fits the 36mm sensor to the LARGER resolution axis. In 9:16 that is the HEIGHT,
    so the visible height at distance d is 36/lens*d and the width follows from the aspect
    ratio - the exact opposite of the landscape intuition, and the reason a camera distance
    that was guessed rather than solved is always wrong in a vertical format.
    """
    if res_y >= res_x:
        v = 18.0 / lens
        return v, v * (res_x / float(res_y))
    hh = 18.0 / lens
    return hh * (res_y / float(res_x)), hh


def solve_distance(points, centre, u, lens, fill_h, fill_w, res_x, res_y) -> float:
    """Back the camera off along `u` until every point is inside the frame.

    `u` is the unit direction from the subject centre TOWARDS the camera. Because the
    camera is aimed at `centre`, that direction is exactly +Z in camera space, so a point
    at camera-space offset q from the centre sits at (qx, qy, qz - D) once the camera is D
    away. Requiring |qx| <= kx*(D - qz) and |qy| <= ky*(D - qz) turns into a lower bound on
    D per point, and the answer is the largest of them. No fitting, no iteration, and
    nothing can end up outside the frame.
    """
    quat = (-u).to_track_quat("-Z", "Y")
    rot_t = quat.to_matrix().transposed()
    tan_v, tan_h = half_angles(lens, res_x, res_y)
    ky, kx = tan_v * fill_h, tan_h * fill_w
    dist = 0.0
    for p in points:
        q = rot_t @ (mathutils.Vector(p) - centre)
        dist = max(dist, q.z + abs(q.y) / ky, q.z + abs(q.x) / kx)
    return dist


def diagonal_offset(pitch: float, res_x: int, res_y: int) -> float:
    """How far to swing the camera OFF the run's long axis, in radians.

    Looking straight down the length of a run is the obvious framing and it is wrong: a
    straight line pointed at the camera projects to a thread. Measured on the 120-slab
    default it covered 90% of the frame height and 2.6% of its width - 97% of a vertical
    short would have been empty floor.

    Swinging the camera off the axis by `off` puts the run on a diagonal instead. Under an
    orthographic approximation the axis projects to sin(off) across the frame and
    cos(off)*sin(pitch) up it, so asking for the two to be in the frame's own aspect ratio

        cos(off) sin(pitch) / sin(off) = res_y / res_x
        =>  off = atan( sin(pitch) * res_x / res_y )

    lays the run exactly along the frame diagonal. At the default pitch that is about 19
    degrees, and it takes the same 120-slab line from 2.6% of the width to 92% of it. The
    formula holds for landscape output too - it simply returns a bigger angle.
    """
    return math.atan(math.sin(pitch) * (float(res_x) / float(res_y)))


def principal_azimuth(points) -> float:
    """Angle of the long axis of the run's ground footprint (2x2 PCA, closed form).

    Looking ALONG that axis is what puts the length of the run down the tall axis of the
    frame. It works out for all three shapes without a per-shape special case: a line gives
    its own direction, an arc gives its chord, and a spiral is near-circular so any answer
    is as good as any other.
    """
    n = float(len(points)) or 1.0
    mx = sum(p[0] for p in points) / n
    my = sum(p[1] for p in points) / n
    sxx = sum((p[0] - mx) ** 2 for p in points)
    syy = sum((p[1] - my) ** 2 for p in points)
    sxy = sum((p[0] - mx) * (p[1] - my) for p in points)
    if abs(sxx - syy) < 1e-9 and abs(sxy) < 1e-9:
        return 0.0
    return 0.5 * math.atan2(2.0 * sxy, sxx - syy)


def aim_camera(cam, position, target):
    cam.location = position
    cam.rotation_euler = (target - position).to_track_quat("-Z", "Y").to_euler()


# ------------------------------------------------------------------------ wave bookkeeping


def front_arclength(n_frames: int, pairs, pitch: float):
    """Arc length of the toppling front, one value per frame, from real topple frames.

    `pairs` is (frame, arclength) for every slab that has been struck. Between two known
    slabs the front is interpolated, so the camera glides instead of jumping slab to slab;
    past the last known one it keeps going at the speed the wave was last measured at,
    which is what makes the tracking camera usable on a preview frame where only part of
    the run has been simulated.
    """
    if not pairs:
        return [0.0] * (n_frames + 1)
    if len(pairs) >= 4:
        a, b = pairs[(len(pairs) * 2) // 3], pairs[-1]
        speed = (b[1] - a[1]) / max(1, b[0] - a[0])
    else:
        speed = pitch / 3.0
    speed = max(speed, pitch / 12.0)
    out, j = [], 0
    for f in range(n_frames + 1):
        if f <= pairs[0][0]:
            out.append(pairs[0][1])
            continue
        while j + 1 < len(pairs) and pairs[j + 1][0] <= f:
            j += 1
        if j + 1 < len(pairs):
            f0, s0 = pairs[j]
            f1, s1 = pairs[j + 1]
            out.append(s0 + (s1 - s0) * ((f - f0) / max(1, f1 - f0)))
        else:
            f0, s0 = pairs[-1]
            out.append(s0 + speed * (f - f0))
    return out


def smooth(values, window: int):
    if window < 2:
        return values
    half, n = window // 2, len(values)
    return [sum(values[max(0, i - half):min(n, i + half + 1)])
            / (min(n, i + half + 1) - max(0, i - half)) for i in range(n)]


# --------------------------------------------------------------------------------- build


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
    # Geometry is 300-odd instances of at most 14 shared meshes, so keeping it resident
    # between frames costs almost no memory and saves the per-frame sync on every frame.
    sc.render.use_persistent_data = True
    if pb("motion_blur"):
        sc.render.use_motion_blur = True
        sc.render.motion_blur_shutter = 0.35
    # AgX (the default view transform) is deliberately desaturating, which flattens the
    # colour ramp the wave is read from. The punchy look pulls it back; the name has moved
    # between releases, so try the known spellings and accept the default if none take.
    for look in ("AgX - Punchy", "Punchy", "AgX - Medium High Contrast"):
        try:
            sc.view_settings.look = look
            break
        except Exception:  # noqa: BLE001 - purely cosmetic
            continue
    # LINEAR keys, set BEFORE the first insert. Camera keys land on every frame and the
    # curve is already smoothed by hand, so bezier handles would only add overshoot -
    # and Blender 5.2 moved Action.fcurves behind slotted actions, so there is no fixing
    # interpolation after the fact.
    bpy.context.preferences.edit.keyframe_new_interpolation_type = "LINEAR"

    sc.world = bpy.data.worlds.new("W")
    sc.world.use_nodes = True
    sc.world.node_tree.nodes["Background"].inputs[0].default_value = (0.014, 0.014, 0.018, 1)

    # ---- slab dimensions and the spacing that keeps the chain alive
    h = max(0.05, pf("slab_height"))
    w = h * max(0.2, pf("slab_width_ratio"))
    t = h * max(0.05, pf("slab_thickness_ratio"))
    pitch_raw = h * pf("spacing")
    gap = clamp_gap(pitch_raw - t, h)
    pitch = gap + t
    r_floor = curve_radius_floor(gap, pitch, h, w)
    shape = ps("path")
    if shape not in ("line", "arc", "spiral"):
        shape = "arc"
    count = max(2, pi("count"))

    path, path_note = build_path(shape, count, pitch, w, r_floor)
    if len(path) < 2:
        # A spiral can run out of radius before it has placed anything worth watching.
        # An arc always fits, so it is the fallback rather than failing the render.
        shape = "arc"
        path, path_note = build_path(shape, count, pitch, w, r_floor)
        path_note += " (spiral did not fit; fell back to arc)"
    count = len(path)
    balance_deg = math.degrees(math.atan2(t, h))
    tilt_deg = pf("start_tilt_deg") or (balance_deg + 3.0)

    # ---- floor
    span_hint = max(4.0, max(abs(p[0]) for p in path) + max(abs(p[1]) for p in path))
    bpy.ops.mesh.primitive_plane_add(size=max(60.0, span_hint * 6.0), location=(0, 0, 0))
    floor = bpy.context.object
    floor.name = "Floor"
    floor.data.materials.append(floor_material(h))
    bpy.ops.rigidbody.object_add(type="PASSIVE")
    # A slab has to PIVOT on its leading edge. With a slippery floor it slides forward
    # instead and hands nothing to the slab in front of it, which is the other classic way
    # a run dies, so both surfaces are deliberately grippy.
    floor.rigid_body.friction = 0.96
    floor.rigid_body.restitution = 0.0

    # ---- palette
    spec = MATERIALS.get(ps("material"), MATERIALS["ceramic"])
    mode = ps("colour_mode")
    sat, val = pf("gradient_saturation"), 0.78
    if mode == "gradient":
        colours = [hue_rgba(pf("hue_start") + pf("hue_span") * (k / max(1, GRADIENT_STEPS - 1)),
                            sat, val) for k in range(GRADIENT_STEPS)]
    elif mode == "alternate":
        base = spec["colour"]
        colours = [(base[0], base[1], base[2], 1.0),
                   (min(1.0, base[0] * 0.45 + 0.05), min(1.0, base[1] * 0.45 + 0.07),
                    min(1.0, base[2] * 0.45 + 0.12), 1.0)]
    else:
        colours = [(spec["colour"][0], spec["colour"][1], spec["colour"][2], 1.0)]
    meshes = [box_mesh(f"SlabMesh{k}", w, t, h,
                       slab_material(f"Slab{k}", rgba, spec), t * 0.06)
              for k, rgba in enumerate(colours)]

    # ---- the slabs
    coll = sc.collection
    slabs = []
    for i, (x, y, tx, ty, _s) in enumerate(path):
        # Local +Y is the slab's thickness axis and must point along the run, so the flat
        # face is what the neighbour behind it strikes. rotation_z = atan2(-Tx, Ty) sends
        # local +Y onto the tangent (local +Y maps to (-sin z, cos z)).
        heading = math.atan2(-tx, ty)
        if mode == "gradient":
            k = int(round((i / max(1, count - 1)) * (len(meshes) - 1)))
        else:
            k = i % len(meshes)
        ob = bpy.data.objects.new(f"Slab_{i:03d}", meshes[k])
        coll.objects.link(ob)
        # Seeded micro-jitter. A domino run is chaotic once it starts, so a fraction of a
        # millimetre of offset changes how the wave travels and how the tail lands - which
        # is what stops the library from producing the same video twice for the same
        # prompt. Kept far below the spacing tolerance so the chain still never stalls.
        ob.location = (x + _RNG.uniform(-JITTER, JITTER) * h,
                       y + _RNG.uniform(-JITTER, JITTER) * h, h / 2.0)
        ob.rotation_euler = (0.0, 0.0, heading + _RNG.uniform(-JITTER, JITTER))
        if i == 0 and tilt_deg > 0.0:
            # The whole run is started by leaning the FIRST slab past its own balance angle
            # atan(t/h) - no pusher object, nothing extra to keep inside the frame, and the
            # start is identical on every render. It has to pivot about its leading bottom
            # edge, not about its centre, or it would start buried in the floor: rotate
            # about the local X axis through (0, +t/2, -h/2) and move the origin by the
            # difference between where that edge was and where the rotation put it.
            rz = mathutils.Matrix.Rotation(heading, 3, "Z")
            rx = mathutils.Matrix.Rotation(-math.radians(tilt_deg), 3, "X")
            edge = mathutils.Vector((0.0, t / 2.0, -h / 2.0))
            ob.rotation_euler = (rz @ rx).to_euler()
            ob.location = (mathutils.Vector((x, y, h / 2.0))
                           + (rz @ edge) - ((rz @ rx) @ edge))
        slabs.append(ob)

    add_rigid_bodies(slabs)
    # Re-assert the floor. add_rigid_bodies works on a selection, and a floor that got
    # swept into it would become an ACTIVE body and fall out from under the entire run -
    # a failure that renders perfectly happily and is baffling to diagnose from the frames.
    floor.rigid_body.type = "PASSIVE"
    slab_mass = spec["density"] * w * t * h
    for i, ob in enumerate(slabs):
        rb = ob.rigid_body
        if rb is None:
            continue
        rb.mass = max(0.05, slab_mass)
        rb.collision_shape = "BOX"
        rb.friction = 0.75
        rb.restitution = 0.0          # dominoes do not bounce; any bounce reads as rubber
        rb.use_margin = True
        # NOT the 0.04 default, which is half the thickness of a slab and would have the
        # whole row already overlapping before anything moves. Not 0.0 either - Bullet
        # wants a sliver of margin for stable contacts between flat faces.
        rb.collision_margin = max(0.0005, min(0.004, t * 0.04))
        rb.use_deactivation = True
        # Everything except the first slab starts ASLEEP. A few hundred boxes standing on
        # their ends will otherwise vibrate themselves over before anything reaches them.
        # Bullet wakes a sleeping body on contact, which is exactly the chain reaction.
        rb.use_start_deactivated = (i > 0)
        try:
            # The default sleep thresholds (0.4 m/s, 0.5 rad/s) are generous enough to
            # class a slab that has only just begun to tip as "at rest". Dropping them
            # keeps a slow section of a long run from being put back to sleep mid-fall.
            rb.deactivate_linear_velocity = 0.01
            rb.deactivate_angular_velocity = 0.02
        except AttributeError:
            pass

    # ---- lights (all invisible to camera: an area light is geometry to Cycles and will
    # otherwise render as a glowing rectangle hanging in the shot)
    footprint = [(p[0], p[1]) for p in path]
    axis = principal_azimuth(footprint)
    cx = sum(p[0] for p in footprint) / count
    cy = sum(p[1] for p in footprint) / count
    reach = max(1.0, max(math.hypot(p[0] - cx, p[1] - cy) for p in footprint))

    tracking = ps("camera") == "track"
    lens = pf("lens") or (30.0 if tracking else 32.0)
    tan_v, _ = half_angles(lens, RES_X, RES_Y)
    pitch_deg = pf("camera_pitch_deg") or AUTO_PITCH.get(shape, 44.0)
    # Below (half the vertical field of view + a little) the top of the frame looks out
    # over the horizon and fills the top of a 9:16 short with empty world background.
    pitch_deg = max(pitch_deg, math.degrees(math.atan(tan_v)) + 5.0)
    cam_pitch = math.radians(pitch_deg)

    # Camera azimuth: along the run's long axis, from the end the run STARTS at, so the
    # wave travels away from the viewer and up the frame - then swung off that axis by
    # diagonal_offset so the run lies on the frame diagonal instead of pointing at the lens.
    look = mathutils.Vector((math.cos(axis), math.sin(axis), 0.0))
    if look.dot(mathutils.Vector((path[0][0] - cx, path[0][1] - cy, 0.0))) < 0:
        look = -look
    cam_azimuth = (math.atan2(look.y, look.x)
                   + diagonal_offset(cam_pitch, RES_X, RES_Y))

    # Lights are placed relative to the finished camera azimuth, so the key stays a
    # three-quarter key whatever the path shape did to the framing.
    build_lights(cam_azimuth, cx, cy, reach, h)

    bpy.ops.object.camera_add(location=(0, 0, 1))
    cam = bpy.context.object
    cam.data.lens = lens
    cam.data.clip_end = max(400.0, reach * 20.0)
    sc.camera = cam
    if not tracking:
        place_overview(cam, path, h, w, cam_azimuth, cam_pitch, lens)

    # ---- solver. Thin boxes in glancing contact are the worst case for a rigid-body
    # solver: at the default 10 substeps a slab can pass a corner through its neighbour
    # between steps and the run skips a link. Simulation time is trivial next to Cycles.
    rw = sc.rigidbody_world
    rw.substeps_per_frame = 20
    rw.solver_iterations = 24

    target = max(2, int(round(SECONDS * FPS)))
    ceiling = max(target, int(round(pf("max_seconds") * FPS))) if pb("auto_length") else target
    sim_end = max(2, PREVIEW) if PREVIEW else ceiling
    rw.point_cache.frame_start = 1
    rw.point_cache.frame_end = sim_end
    sc.frame_end = sim_end

    # Printed so the operator can see roughly how long the wave needs before committing to
    # a render. A domino wave travels at about 2.2*sqrt(g*h) - a figure from the published
    # measurements, not from this scene - so length/6.9*sqrt(h) seconds. It is ONLY a
    # printed estimate: the length actually rendered comes from the simulation below.
    est = ((count - 1) * pitch) / (6.9 * math.sqrt(h))
    print(f"SCENE_INFO path={path_note} count={count} pitch={pitch:.3f} gap={gap:.3f} "
          f"({gap / h:.2f}h) slab={w:.3f}x{t:.3f}x{h:.3f} tilt={tilt_deg:.1f}deg "
          f"(balance {balance_deg:.1f}) est_wave={est:.1f}s")

    # ---- simulate BEFORE anything is rendered or the camera is keyed. A rigid-body scene
    # only advances when frames are stepped in order: jumping straight to frame 90 renders
    # the setup pose, not frame 90. Stepping is also the only honest source for the frames
    # in impacts.json - a sound placed on an estimated frame lands next to the picture.
    topple = simulate(sc, slabs, sim_end, path, stop_when_done=not PREVIEW)

    # Sorted by frame, not by position in the run: a slab clipped from the side by a
    # neighbouring turn of a spiral can go down out of order, and front_arclength assumes
    # its pairs arrive in time order.
    struck = sorted([(f, s) for (f, s) in topple if f], key=lambda p: p[0])
    last_frame = max((f for f, _ in struck), default=1)
    stalled = len(struck) < count

    if PREVIEW:
        if tracking:
            key_tracking_camera(cam, path, pitch, struck, sim_end, h, w, lens)
        sc.frame_set(PREVIEW)
        sc.render.image_settings.file_format = "PNG"
        sc.render.image_settings.color_mode = "RGB"
        sc.render.filepath = f"{OUT}/preview.png"
        bpy.ops.render.render(write_still=True)
        print(f"SCENE_OK preview={PREVIEW} {path_note} count={count} "
              f"toppled={len(struck)}")
        return

    settle = int(round(pf("settle_seconds") * FPS))
    if pb("auto_length"):
        end = min(ceiling, max(int(1.5 * FPS), last_frame + settle))
    else:
        end = target
    sc.frame_end = end
    if tracking:
        key_tracking_camera(cam, path, pitch, struck, end, h, w, lens)

    write_events(struck, count, end)

    if stalled:
        # Not fatal - the footage of a run that gets four fifths of the way is still usable
        # and the operator should be told rather than left to spot it in the render.
        print(f"SCENE_NOTE run stalled after {len(struck)}/{count} slabs "
              f"(gap={gap / h:.2f}h) - try spacing nearer 0.55")

    sc.render.image_settings.file_format = "PNG"
    sc.render.image_settings.color_mode = "RGB"
    sc.render.filepath = f"{OUT}/frame_"
    bpy.ops.render.render(animation=True)
    print(f"SCENE_OK path={shape} count={count} toppled={len(struck)} frames={end} "
          f"seconds={end / FPS:.2f} camera={ps('camera')} material={ps('material')}")


# ------------------------------------------------------------------------- build helpers


def floor_material(h: float):
    """Dark floor with an optional low-contrast checker.

    A featureless dark plane leaves whole thirds of a tall frame reading as nothing, and a
    domino run has no other scenery. The checker is deliberately weak - it gives the eye a
    ruler to measure the run against without competing with the slabs for attention.
    """
    m = bpy.data.materials.new("Floor")
    m.use_nodes = True
    nt = m.node_tree
    bsdf = nt.nodes["Principled BSDF"]
    bsdf.inputs["Roughness"].default_value = 0.42
    bsdf.inputs["Base Color"].default_value = (0.045, 0.046, 0.052, 1)
    if not pb("floor_checker"):
        return m
    tex = nt.nodes.new("ShaderNodeTexChecker")
    tex.inputs["Color1"].default_value = (0.058, 0.059, 0.066, 1)
    tex.inputs["Color2"].default_value = (0.028, 0.029, 0.034, 1)
    # Object coordinates, not the default generated ones: generated coords are normalised
    # over the plane's bounding box, so the checker would resize itself whenever the floor
    # does. In object space one unit is one metre and the cell size below means metres.
    coord = nt.nodes.new("ShaderNodeTexCoord")
    tex.inputs["Scale"].default_value = 1.0 / max(0.25, h * 2.4)
    nt.links.new(coord.outputs["Object"], tex.inputs["Vector"])
    nt.links.new(tex.outputs["Color"], bsdf.inputs["Base Color"])
    return m


def add_rigid_bodies(slabs):
    """Give every slab an ACTIVE rigid body.

    The bulk operator does it in one depsgraph update; the per-object one is the fallback
    because it is the call proven in the other scenes here. Whichever runs, the result is
    verified per object - a slab silently left without a body would hang in mid air.
    """
    try:
        bpy.ops.object.select_all(action="DESELECT")
        for ob in slabs:
            ob.select_set(True)
        bpy.context.view_layer.objects.active = slabs[0]
        bpy.ops.rigidbody.objects_add(type="ACTIVE")
    except Exception:  # noqa: BLE001 - operator missing or poll failed; fall through
        pass
    for ob in slabs:
        if ob.rigid_body is not None:
            continue
        bpy.context.view_layer.objects.active = ob
        bpy.ops.rigidbody.object_add(type="ACTIVE")


def build_lights(cam_azimuth: float, cx: float, cy: float, reach: float, h: float):
    """A sun key plus two area lights, placed relative to the camera, all camera-invisible.

    The key is a SUN because a domino run can be three metres across or forty: sun
    irradiance does not fall off with distance, so one energy value lights every count and
    every slab size the same. The two area lights DO fall off, so their power is scaled by
    the square of their distance - that is what keeps a long run from being lit like a
    close-up of a short one.
    """
    sun_dir = mathutils.Vector((math.cos(cam_azimuth + math.radians(55)) * math.cos(math.radians(42)),
                                math.sin(cam_azimuth + math.radians(55)) * math.cos(math.radians(42)),
                                math.sin(math.radians(42))))
    bpy.ops.object.light_add(type="SUN", location=(cx, cy, reach + 6.0))
    sun = bpy.context.object
    sun.data.energy = pf("sun_energy")
    sun.data.angle = math.radians(6.0)        # softens the shadow edge without going flat
    sun.rotation_euler = (-sun_dir).to_track_quat("-Z", "Y").to_euler()

    target = mathutils.Vector((cx, cy, h * 0.5))

    # Fill from the camera side, so the faces pointed at the lens are not pure shadow.
    fill_dir = mathutils.Vector((math.cos(cam_azimuth - math.radians(40)) * 0.82,
                                 math.sin(cam_azimuth - math.radians(40)) * 0.82,
                                 0.57))
    fill_d = reach * 1.4 + h * 6.0
    fill_at = mathutils.Vector((cx, cy, 0.0)) + fill_dir * fill_d
    bpy.ops.object.light_add(type="AREA", location=fill_at)
    fill = bpy.context.object
    # Watts, and an area light's illuminance falls off with the square of the distance, so
    # the power is solved from where the light ended up. A fixed number lit a three metre
    # spiral and a forty metre line completely differently.
    fill.data.energy = 14.0 * fill_d * fill_d
    fill.data.size = max(2.0, reach * 0.7)
    aim_camera(fill, fill_at, target)

    # Cool rim from behind, to separate the standing slabs from the dark floor.
    rim_dir = mathutils.Vector((-math.cos(cam_azimuth + math.radians(25)) * 0.78,
                                -math.sin(cam_azimuth + math.radians(25)) * 0.78,
                                0.62))
    rim_d = reach * 1.3 + h * 5.0
    rim_at = mathutils.Vector((cx, cy, 0.0)) + rim_dir * rim_d
    bpy.ops.object.light_add(type="AREA", location=rim_at)
    rim = bpy.context.object
    rim.data.energy = 20.0 * rim_d * rim_d
    rim.data.size = max(2.0, reach * 0.6)
    rim.data.color = (0.55, 0.72, 1.0)
    aim_camera(rim, rim_at, target)

    for light in (sun, fill, rim):
        light.visible_camera = False


def framing_points(path, h: float, w: float):
    """Every point the shot has to contain, standing AND fallen.

    A run occupies more ground after it falls than before: each slab lies up to its own
    height further along the path. Framing on the standing row alone puts the finished
    pile half out of shot, which is the half the viewer is watching by then.
    """
    pts = []
    for x, y, tx, ty, _s in path:
        for along in (-0.30 * h, 1.10 * h):
            for side in (-0.65 * w, 0.65 * w):
                px = x + tx * along - ty * side
                py = y + ty * along + tx * side
                pts.append((px, py, 0.0))
                pts.append((px, py, h))
    return pts


def place_overview(cam, path, h: float, w: float, azimuth: float, pitch: float, lens: float):
    """Static camera that holds the entire run, before and after it falls."""
    pts = framing_points(path, h, w)
    centre = mathutils.Vector((sum(p[0] for p in pts) / len(pts),
                               sum(p[1] for p in pts) / len(pts),
                               h * 0.35))
    u = mathutils.Vector((math.cos(azimuth) * math.cos(pitch),
                          math.sin(azimuth) * math.cos(pitch),
                          math.sin(pitch)))
    # 0.90 of the height and 0.92 of the width: enough edge that the run is not touching
    # the frame border, not so much that the short is mostly floor.
    dist = solve_distance(pts, centre, u, lens, 0.90, 0.92, RES_X, RES_Y)
    dist = max(dist, h * 4.0)
    aim_camera(cam, centre + u * dist, centre)


def key_tracking_camera(cam, path, pitch: float, struck, end: int,
                        h: float, w: float, lens: float):
    """Chase the toppling front, keyed from the frames the solver actually produced.

    The distance is solved ONCE, from a window of run `track_span` long, so the framing
    stays constant while the camera travels - re-solving per frame would make the camera
    breathe every time the path curved. Only the position along the path changes.
    """
    span = max(1.5, pf("track_span"))
    pitch_a = pf("camera_pitch_deg") or 26.0
    tan_v, _ = half_angles(lens, RES_X, RES_Y)
    pitch_a = math.radians(max(pitch_a, math.degrees(math.atan(tan_v)) + 5.0))
    # Same problem the overview camera has, in miniature: the visible window is a strip
    # 4.5m long and 0.34m wide, so chasing from straight behind fills the height and 20%
    # of the width, and going side on does the reverse. diagonal_offset puts the strip on
    # the frame diagonal - swept against a brute-force search over yaw for every span and
    # lens in range, it lands within 3% of the best possible frame coverage every time.
    yaw = math.radians(pf("track_yaw_deg")) or diagonal_offset(pitch_a, RES_X, RES_Y)

    # The visible window, as offsets from the aim point: a third of it behind the aim (the
    # fallen tail) and two thirds ahead (the standing row still to go). Local X is the path
    # NORMAL and local Y is the path TANGENT - the same convention u_local below is built
    # in, and the same one the per-frame placement uses. Getting these two the wrong way
    # round solves the distance for a camera at right angles to the one that gets built.
    local = []
    for along in (-0.5 * span, 0.5 * span):
        for side in (-0.8 * w, 0.8 * w):
            local.append((side, along, -0.45 * h))
            local.append((side, along, 0.55 * h))
    u_local = mathutils.Vector((math.sin(yaw) * math.cos(pitch_a),
                                -math.cos(yaw) * math.cos(pitch_a),
                                math.sin(pitch_a)))
    dist = solve_distance(local, mathutils.Vector((0, 0, 0)), u_local,
                          lens, 0.88, 0.90, RES_X, RES_Y)
    dist = max(dist, span * 0.4)

    front = smooth(front_arclength(end, struck, pitch), 9)
    lead = 0.15 * span
    for f in range(1, end + 1):
        pos, tan = path_at(path, pitch, front[min(f, len(front) - 1)] + lead)
        normal = mathutils.Vector((-tan.y, tan.x, 0.0))
        # yaw measured from "directly behind the front": 0 looks straight down the row,
        # 90 is side on. The default 34 keeps both the pile and the standing run in shot.
        horizontal = (-tan) * math.cos(yaw) + normal * math.sin(yaw)
        u = horizontal * math.cos(pitch_a) + mathutils.Vector((0, 0, 1)) * math.sin(pitch_a)
        aim = pos + mathutils.Vector((0.0, 0.0, h * 0.45))
        aim_camera(cam, aim + u * dist, aim)
        cam.keyframe_insert("location", frame=f)
        cam.keyframe_insert("rotation_euler", frame=f)


# ---------------------------------------------------------------------------- simulation


def simulate(sc, slabs, sim_end: int, path, stop_when_done: bool = True):
    """Step the solver and record the frame each slab was struck.

    A slab's world +Z axis is straight up while it stands. The frame its tilt first passes
    TILT_TRIGGER_DEG is the frame the slab in front of it made contact - which is the click
    the viewer hears, so that frame is what goes into impacts.json. Sleeping slabs sit at
    exactly their initial transform, so there are no false positives to filter.

    Returns [(frame or None, arclength)] per slab, in run order.
    """
    limit = math.cos(math.radians(TILT_TRIGGER_DEG))
    result = [[None, p[4]] for p in path]
    pending = list(range(len(slabs)))
    up = mathutils.Vector((0.0, 0.0, 1.0))
    dg = bpy.context.evaluated_depsgraph_get()
    done_at = None
    for f in range(1, sim_end + 1):
        sc.frame_set(f)
        dg.update()
        still = []
        for i in pending:
            m = slabs[i].evaluated_get(dg).matrix_world
            if (m.to_quaternion() @ up).z < limit:
                result[i][0] = f
            else:
                still.append(i)
        pending = still
        if not pending and done_at is None:
            done_at = f
        # Everything is down and the settle has been simulated: no reason to keep going,
        # and on a short run this is the difference between a 4 second and a 10 second bake.
        # Never taken for a preview - the point cache would then stop short of the frame
        # about to be rendered, and a rigid body only advances when frames are STEPPED, so
        # jumping to an unsimulated frame shows the setup pose instead of the action.
        if (stop_when_done and done_at is not None
                and f >= done_at + int(pf("settle_seconds") * FPS) + 2):
            break
    return [(r[0], r[1]) for r in result]


def write_events(struck, count: int, end: int):
    """impacts.json - one click per contact, thinned to something a mixer can open.

    Loudness is taken from the local wave speed: where the front is moving fast the slabs
    are being hit hard and the clicks are tight together, which is exactly the part that
    should be loudest. It is measured from the gaps between the recorded frames, so it
    tracks the simulation rather than a curve someone drew.
    """
    events = []
    intervals = [struck[i][0] - struck[i - 1][0] for i in range(1, len(struck))
                 if struck[i][0] and struck[i - 1][0]]
    intervals = [d for d in intervals if d > 0]
    ref = sorted(intervals)[len(intervals) // 2] if intervals else 3.0
    for i in range(1, len(struck)):
        frame = struck[i][0]
        if not frame or frame > end:
            continue
        delta = max(1, frame - (struck[i - 1][0] or frame - 1))
        # A click at the median wave speed lands at 0.65, just under the 0.7 the mixer uses
        # to decide whether an event also gets a debris tail. Only the fast stretch of the
        # run earns tails - every click dragging a rattle behind it smears the rhythm that
        # makes a domino run pleasant to listen to in the first place.
        strength = 0.28 + 0.34 * min(2.0, ref / float(delta))
        events.append({"frame": int(frame), "kind": "impact",
                       "strength": round(max(0.2, min(1.0, strength)), 3)})

    cap = max(4, pi("max_events"))
    if len(events) > cap:
        step = len(events) / float(cap)
        thinned = [events[min(len(events) - 1, int(k * step))] for k in range(cap)]
        thinned[-1] = events[-1]
        for ev in thinned:                      # fewer clicks, each carrying a bit more
            ev["strength"] = round(min(1.0, ev["strength"] * 1.12), 3)
        events = thinned

    with open(f"{OUT}/impacts.json", "w", encoding="utf-8") as fh:
        json.dump({"fps": FPS, "events": events,
                   "slabs": count, "toppled": sum(1 for f, _ in struck if f),
                   "frames": end}, fh, indent=1)


main()
