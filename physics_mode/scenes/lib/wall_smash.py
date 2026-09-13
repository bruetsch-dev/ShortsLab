"""A wrecking ball on a real chain swings in and demolishes a large wall.

The wall is glass, brick, concrete or wood, and it comes apart along its own courses.
(This first line is the scene's description in the catalogue - physics_mode/library.py
shows it to the model that chooses a scene, so it has to say what HAPPENS.)

    blender -b -noaudio -P wall_smash.py -- '{"material":"brick","ball_mass":6000,...}'

This file is SOURCE, not generated output. A model picks the scene and fills PARAMS; it
never writes geometry. Everything below is solved from the wall's own dimensions, so any
combination of the declared parameters still produces a framed, lit, physically staged
shot.

WHAT MAKES THIS ONE RELIABLE
----------------------------
* The camera distance is SOLVED, never guessed. Every point that matters (all eight wall
  corners, the ball at every frame from the moment of impact onward, the strip of floor
  the debris lands on) is projected into camera space and the distance is taken as the
  maximum over all of their in-frame constraints. See solve_shot().
* The ball stays KINEMATIC on its pendulum for the whole shot. That is a deliberate
  physical statement, not a shortcut: a crane ball is driven by a chain and a boom, it is
  not a free body. It buys three things at once - the chain is exactly taut on every
  frame (no rubber-band stretching after release), a 6-tonne ball cannot be bounced back
  by a wall it should obliterate, and the shot cannot fail in a way we did not author.
  Bullet treats a kinematic body as infinite mass, so the wall is genuinely shoved.
* ball_mass therefore reads as SIZE and as FOLLOW-THROUGH rather than as solver momentum:
  the radius is the real radius of a solid steel sphere of that mass, and the fraction of
  swing energy the ball keeps after breaking through rises with mass. A mass sweep gives
  three visibly different demolitions, which is the entire point of the format.
* impacts.json comes from stepping the depsgraph and watching the actual pieces: the hit
  frame is the frame a piece first left its rest position, and every debris event is a
  piece that was falling on one frame and stopped on the next. Nothing is estimated.
* Every parameter is clamped to its declared range, and a wall smaller than MIN_PIECES is
  grown until it reaches it. The parameters are a request; a shot that reads on a phone is
  the guarantee. stage_shot() and solve_timing() are pure maths and import without bpy, so
  the framing can be checked before an hour of GPU time is spent finding out.

FRAMING NOTES THAT COST US RENDERS
----------------------------------
* Blender fits the 36mm sensor to the LARGER resolution axis, so in 9:16 the visible
  HEIGHT is 36/lens * distance and the width is that times 9/16. Both are enforced.
* The pivot is pushed ABOVE the top edge of the frame (the arm is lengthened until it is).
  A wrecking ball hangs from a crane that is out of shot; an anchor floating in mid-air
  inside the frame reads as a bug, and framing the anchor shrinks the wall to a pebble.
* The swing is timed so the ball is fully inside the frame for at least `lead_seconds`
  before it lands. Framing the ball's whole arc would push the camera so far back that
  the wall covered a fifth of the frame - the approach is allowed to start off-screen,
  the arrival is not.
* A backdrop cylinder encloses the set. Pure black behind a dark grey wall gives you a
  silhouette and an empty upper third; a huge, barely-lit surface gives falloff, and the
  rim light behind the wall paints a glow the debris reads against.
"""

import json
import math
import random
import sys

try:
    import bpy
    from mathutils import Matrix, Quaternion, Vector
except ImportError:  # importable outside Blender so PARAMS can be read without bpy
    bpy = None
    Quaternion = Vector = None


# --------------------------------------------------------------------------- parameters

PARAMS = {
    "material": {
        "choices": ["glass", "brick", "concrete", "wood"],
        "default": "brick",
        "note": "What the wall is built of. Sets colour, roughness, piece shape and real "
                "density (so wood flies further than concrete). glass is translucent and "
                "the slowest to render; brick is the classic wrecking-ball target.",
    },
    "wall_cols": {
        "range": [6, 34], "default": 14,
        "note": "Wall width in pieces. Width in metres is wall_cols * piece_height * the "
                "material's piece aspect (brick 2:1, wood 3:1, glass 1:1).",
    },
    "wall_rows": {
        "range": [8, 44], "default": 22,
        "note": "Wall height in pieces. Height in metres is wall_rows * piece_height. "
                "wall_cols * wall_rows * wall_depth is the piece count - keep it above "
                "200 or the destruction looks cheap.",
    },
    "wall_depth": {
        "range": [1, 4], "default": 2,
        "note": "Wall thickness in pieces. 1 is a screen, 2 is a wall, 3+ is a bunker "
                "that a light ball will not get through.",
    },
    "piece_height": {
        "range": [0.14, 0.60], "default": 0.28,
        "note": "Height of one piece in metres - this is what sets the real-world scale "
                "of the whole shot and therefore how fast everything appears to fall. "
                "0.28 gives a 6m wall at the default row count.",
    },
    "ball_mass": {
        "range": [300, 30000], "default": 6000,
        "note": "Wrecking ball mass in kg. Drives the ball's radius (a real solid steel "
                "sphere of that mass) and how much of its swing it keeps after punching "
                "through, so heavier means bigger, louder and further into the wall. "
                "1500kg dents it, 6000kg breaks through, 20000kg takes the wall with it. "
                "Above ~20000 the ball needs so much room that the wall gets smaller in "
                "frame - the best parameter to sweep, but sweep it around 1500-20000.",
    },
    "chain_links": {
        "range": [8, 14], "default": 11,
        "note": "Number of separate interlocking links between the crane and the ball. "
                "They are placed along the pivot-to-ball line on every frame.",
    },
    "camera_angle": {
        "choices": ["front", "three_quarter", "low"],
        "default": "three_quarter",
        "note": "front = near head-on at the wall face, ball crosses from the left. "
                "three_quarter = wall seen at 42 degrees, the most readable and the "
                "default. low = near ground level looking slightly up, wall towers.",
    },
    "impact_second": {
        "range": [0.35, 2.50], "default": 0.85,
        "note": "When the ball lands, in seconds from the first frame. Raised "
                "automatically if the swing would be too fast to collide cleanly or too "
                "fast to see coming.",
    },
    "hit_height": {
        "range": [0.20, 0.85], "default": 0.46,
        "note": "Where on the wall the ball strikes, as a fraction of wall height. Below "
                "0.5 knocks the base out and the top falls in one slab; above 0.6 punches "
                "a hole and leaves the wall standing.",
    },
    "hit_offset": {
        "range": [-0.60, 0.60], "default": 0.0,
        "note": "Sideways strike position as a fraction of half the wall width. 0 is dead "
                "centre and blows out symmetrically; 0.35 takes one end off.",
    },
    "swing_from_deg": {
        "range": [25, 78], "default": 56,
        "note": "How far back the ball is drawn before the swing, in degrees from "
                "straight down. Bigger means a longer, faster arc.",
    },
    "lens": {
        "range": [24, 80], "default": 42,
        "note": "Camera focal length in mm. Short is wide and dramatic with more floor in "
                "shot; long flattens the wall and pushes the camera far back.",
    },
    "lead_seconds": {
        "range": [0.10, 1.20], "default": 0.30,
        "note": "How long the ball must be fully visible in frame before it hits. The "
                "swing is slowed down until this holds.",
    },
    "exposure": {
        # Deliberately not [-3.0, 3.0]: library.clamp() rounds a parameter to an integer
        # when both ends of its range and its default are whole numbers, which would throw
        # away every fractional stop. Half-stop ends keep this a float end to end.
        "range": [-2.5, 2.5], "default": 0.0,
        "note": "Stops of exposure on top of the solved lighting. Use this to fix a dark "
                "or blown preview instead of touching the lights.",
    },
    "seed": {
        "range": [0, 100000], "default": 7,
        "note": "Seeds the per-piece shade variation and the sub-millimetre placement "
                "jitter that keeps the collapse from looking like folding cardboard.",
    },
    "seconds": {
        "range": [2.0, 15.0], "default": 5.0,
        "note": "Length of the shot in seconds. Needs about 3s after the impact for the "
                "rubble to actually settle.",
    },
    "fps": {"range": [12, 60], "default": 30, "note": "Frames per second."},
    "res_x": {"range": [256, 2160], "default": 1080, "note": "Render width in pixels."},
    "res_y": {"range": [256, 3840], "default": 1920, "note": "Render height in pixels."},
    "samples": {"range": [4, 512], "default": 24,
                "note": "Cycles samples per pixel. 24 with denoising is enough here; "
                        "glass wants 48+."},
    "preview_frame": {
        "range": [0, 4000], "default": 0,
        "note": "If greater than zero, simulate up to this frame, render it alone to "
                "preview.png and stop. Use a frame after the impact - an establishing "
                "frame tells you nothing about whether the shot is worth the GPU time.",
    },
    "label": {
        "default": "",
        "note": "Optional caption burned into the corner of the frame, e.g. '6000 kg'. "
                "Empty means no caption. Used when the app sweeps a parameter.",
    },
    "out_dir": {"default": "//out", "note": "Where frames, preview.png and impacts.json go."},
}

# The app may render this scene once per value and join the takes - the "1500kg / 6000kg /
# 20000kg" format the mode was built for (physics_mode/library.py reads this literal).
# Mass is the parameter worth sweeping because it moves THREE things at once: the ball's
# real size, how far it carries on through the wall, and how hard the hit reads. Sweeping
# something like wall_cols instead gives three shades of the same demolition.
SWEEP = {"param": "ball_mass", "values": [1500, 6000, 20000], "unit": "kg"}

# Per-material physical and visual character. `aspect` is piece length / piece height,
# `depth` is piece thickness / piece height, `density` is kg/m3 of the real material -
# piece mass is density * volume, so a wooden plank really does fly further than a
# concrete block of the same size. `bond` lays odd courses half a piece over, which is
# what makes a masonry wall read as masonry.
MATERIALS = {
    "brick": dict(color=(0.40, 0.135, 0.085), rough=0.82, metal=0.0, transmission=0.0,
                  aspect=2.0, depth=1.0, density=1900, bond=True,
                  friction=0.92, restitution=0.02, shade=0.20),
    "concrete": dict(color=(0.30, 0.30, 0.315), rough=0.88, metal=0.0, transmission=0.0,
                     aspect=1.5, depth=1.0, density=2400, bond=True,
                     friction=0.95, restitution=0.02, shade=0.13),
    "glass": dict(color=(0.52, 0.78, 0.83), rough=0.06, metal=0.0, transmission=0.88,
                  aspect=1.0, depth=0.55, density=2500, bond=False,
                  friction=0.35, restitution=0.10, shade=0.05),
    "wood": dict(color=(0.36, 0.21, 0.095), rough=0.68, metal=0.0, transmission=0.0,
                 aspect=3.0, depth=0.45, density=650, bond=True,
                 friction=0.80, restitution=0.05, shade=0.26),
}

# Camera presets: yaw is measured from the wall's front normal, NEGATIVE so the camera
# sits on the +X side and the ball - which always swings along +Y - crosses the frame
# from left to right. Pitch is positive looking down.
CAMERA_ANGLES = {
    "front":         dict(yaw=-18.0, pitch=9.0, aim_z=0.50),
    "three_quarter": dict(yaw=-42.0, pitch=11.0, aim_z=0.50),
    "low":           dict(yaw=-34.0, pitch=-11.0, aim_z=0.42),
}

STEEL_DENSITY = 7850.0          # kg/m3, for turning ball_mass into a believable radius
GRAVITY = 9.81
CAM_MIN_Z = 0.50                # metres: the lens never gets closer to the floor than this
MIN_PIECES = 200                # below this a wall falls over instead of coming apart
MAX_PIECES = 3000               # above this the bake costs more than the shot is worth
# Widest the wall may be relative to its height. A wall much wider than it is tall cannot
# fill a 9:16 frame: the camera has to pull back until the WIDTH fits and the wall ends up
# a strip across the middle of a very tall picture. Measured on a 34x8 request: 18% of the
# frame height, and the frame was then so tall that the crane pivot could not be lifted out
# of it either, so the chain ended in mid air. 1.6 keeps the wall over 40% of the height.
MAX_W_OVER_H = 1.6
# How far past the wall the ball may carry after breaking through, as a fraction of wall
# height. The follow-through is an ANGLE, so on a long chain even a modest one is a huge
# distance: at the top of the mass range the ball swung 20m clear of the wall and the
# camera had to pull back until the wall was a quarter of the frame. Capping the TRAVEL
# keeps the shot on the wall, and physically it only says the wall took more out of it.
TRAVEL_CAP = 0.75


def _params() -> dict:
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    return json.loads(argv[0]) if argv else {}


def _get(P: dict, key: str):
    """One parameter, clamped to its declared range / choices.

    Clamping rather than trusting the caller is deliberate: a model fills these in and a
    single out-of-range number (a wall 400 pieces wide, a negative lens) would otherwise
    burn an hour of GPU time before failing.
    """
    spec = PARAMS[key]
    value = P.get(key, spec["default"])
    if "choices" in spec:
        return value if value in spec["choices"] else spec["default"]
    if "range" in spec:
        try:
            value = float(value)
        except (TypeError, ValueError):
            value = float(spec["default"])
        lo, hi = spec["range"]
        value = min(max(value, lo), hi)
        if isinstance(spec["default"], int):
            value = int(round(value))
        return value
    return value


# ------------------------------------------------------------------------- scene basics


def use_gpu(sc) -> str:
    """Point Cycles at the GPU. On CPU a frame of this scene costs ~20s, which turns a
    five second shot into an hour and makes the mode unusable. OptiX first (RTX cards
    have the ray tracing cores), then CUDA, then whatever else is compiled in."""
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


def mat(name, rgba, rough=0.6, metal=0.0, transmission=0.0, emission=0.0):
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    b = m.node_tree.nodes["Principled BSDF"]
    b.inputs["Base Color"].default_value = (*rgba[:3], 1.0)
    b.inputs["Roughness"].default_value = rough
    b.inputs["Metallic"].default_value = metal
    if transmission:
        # Named-socket lookups in a try: Blender renamed these between versions and a
        # KeyError here would kill the whole render over a material tweak.
        for key in ("Transmission Weight", "Transmission"):
            try:
                b.inputs[key].default_value = transmission
                break
            except KeyError:
                continue
        try:
            b.inputs["IOR"].default_value = 1.45
        except KeyError:
            pass
    if emission:
        try:
            b.inputs["Emission Color"].default_value = (*rgba[:3], 1.0)
            b.inputs["Emission Strength"].default_value = emission
        except KeyError:
            pass
    return m


def box_mesh(name, dims, material):
    """A box mesh of exact dimensions, built by hand.

    Deliberately NOT primitive_cube_add per piece: six hundred operator calls cost seconds
    each time the scene is built, and primitive_cube_add(size=1) already spans one unit -
    reaching for scale=0.5 to get a full-size box is the mistake that cost a rebuild here
    once. Several hundred objects share a handful of these meshes.
    """
    hx, hy, hz = dims[0] / 2.0, dims[1] / 2.0, dims[2] / 2.0
    verts = [(-hx, -hy, -hz), (hx, -hy, -hz), (hx, hy, -hz), (-hx, hy, -hz),
             (-hx, -hy, hz), (hx, -hy, hz), (hx, hy, hz), (-hx, hy, hz)]
    faces = [(0, 3, 2, 1), (4, 5, 6, 7), (0, 1, 5, 4),
             (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7)]  # wound so normals face outwards
    me = bpy.data.meshes.new(name)
    me.from_pydata(verts, [], faces)
    me.validate()
    me.update()
    me.materials.append(material)
    return me


def add_rigid(objs, kind="ACTIVE"):
    """Give every object in `objs` a rigid body. Adds them in one operator call when this
    build has the plural operator, otherwise one at a time - six hundred single calls are
    slow but they are never wrong."""
    for o in bpy.context.selected_objects:
        o.select_set(False)
    for o in objs:
        o.select_set(True)
    bpy.context.view_layer.objects.active = objs[0]
    try:
        bpy.ops.rigidbody.objects_add(type=kind)
        return
    except (AttributeError, RuntimeError, TypeError):
        pass
    for o in objs:
        bpy.context.view_layer.objects.active = o
        bpy.ops.rigidbody.object_add(type=kind)


def aim_at(obj, target):
    """Point an object's local -Z at `target` - lights and cameras both emit along -Z."""
    d = Vector(target) - obj.location
    obj.rotation_euler = d.to_track_quat("-Z", "Y").to_euler()


# ------------------------------------------------------------------------------ the wall


def row_slots(cols, L, bonded, row):
    """Where the pieces of one course sit: [(centre x, length), ...], spanning exactly
    [-W/2, +W/2] with no gap and no overhang.

    Running bond: odd courses step over by half a piece and are closed off with a half
    piece at each end, so the wall keeps its exact width and the vertical joints never
    line up - which is what stops it shearing as one flat sheet. Half at each end plus
    (cols-1) whole ones is exactly cols*L again; getting that wrong leaves either a hole
    at the end of every other course or a brick hanging in mid air.
    """
    W = cols * L
    if bonded and row % 2:
        slots = [(-W / 2 + L / 4, L / 2)]
        slots += [(-W / 2 + L / 2 + (i + 0.5) * L, L) for i in range(cols - 1)]
        slots.append((W / 2 - L / 4, L / 2))
        return slots
    return [(-W / 2 + (i + 0.5) * L, L) for i in range(cols)]


def build_wall(spec, cols, rows, layers, piece_h, seed):
    """The wall, as separate rigid bodies. Returns (pieces, W, H, T, piece dims).

    Blender 5.2 has no Cell Fracture add-on, and the reference footage never showed
    voronoi shards anyway - a real wall comes apart along its courses, which is exactly
    what a bonded grid of boxes does under the solver.
    """
    rng = random.Random(seed)
    L = piece_h * spec["aspect"]                 # piece length along the wall
    Tp = piece_h * spec["depth"]                 # piece thickness through the wall
    W, H, T = cols * L, rows * piece_h, layers * Tp

    # Mortar gaps. The horizontal joint is the wide one (7% of a piece, ~2cm here) because
    # that is the seam the eye reads as courses; the vertical joint is only 3% so the rows
    # do not visibly hover above each other before they are hit. Flush boxes render as one
    # smooth slab and the destruction then lands as a glitch rather than a collapse.
    gap_x, gap_y, gap_z = 0.07 * L, 0.06 * Tp, 0.03 * piece_h
    piece_mass = spec["density"] * (L - gap_x) * (Tp - gap_y) * (piece_h - gap_z)

    # Four shade variants per material, as four meshes sharing nothing but their shape.
    # Per-object material overrides would do the same job through material_slots[].link,
    # but a handful of extra mesh datablocks cannot break, and a brick wall in one flat
    # colour looks printed on.
    shades = []
    for i in range(4):
        j = spec["shade"] * (i / 3.0 - 0.5)
        col = tuple(min(1.0, max(0.02, c * (1.0 + j))) for c in spec["color"])
        m = mat(f"piece_{i}", col, rough=min(1.0, spec["rough"] * (1.0 + 0.35 * j)),
                metal=spec["metal"], transmission=spec["transmission"])
        shades.append(m)
    full = [box_mesh(f"piece_full_{i}", (L - gap_x, Tp - gap_y, piece_h - gap_z), m)
            for i, m in enumerate(shades)]
    half = [box_mesh(f"piece_half_{i}", (L / 2 - gap_x, Tp - gap_y, piece_h - gap_z), m)
            for i, m in enumerate(shades)]

    coll = bpy.context.scene.collection
    pieces = []
    for r in range(rows):
        z = r * piece_h + piece_h / 2.0
        for x, slot_len in row_slots(cols, L, spec["bond"], r):
            meshes = full if slot_len > L * 0.75 else half
            for d in range(layers):
                y = (d - (layers - 1) / 2.0) * Tp
                # Jitter is a fifth of the mortar gap: enough that the courses do not
                # topple as one rigid sheet, far too small to start the frame already
                # interpenetrating (which wakes the stack and explodes it).
                jx, jy = rng.uniform(-0.2, 0.2) * gap_x, rng.uniform(-0.2, 0.2) * gap_y
                obj = bpy.data.objects.new("piece", meshes[rng.randrange(4)])
                obj.location = (x + jx, y + jy, z)
                coll.objects.link(obj)
                pieces.append(obj)

    add_rigid(pieces, "ACTIVE")
    for p in pieces:
        rb = p.rigid_body
        rb.mass = piece_mass
        rb.friction = spec["friction"]
        rb.restitution = spec["restitution"]
        rb.collision_shape = "BOX"
        rb.use_margin = True
        rb.collision_margin = 0.0        # Bullet's default 0.04 leaves a visible float
        rb.linear_damping = 0.04         # takes the edge off deep-penetration push-out
        rb.angular_damping = 0.10
        # A stack that is awake jitters itself apart before anything reaches it.
        rb.use_deactivation = True
        rb.use_start_deactivated = True
    return pieces, W, H, T, (L, Tp, piece_h), piece_mass


# --------------------------------------------------------------------------- the framing


def camera_basis(yaw, pitch):
    """Forward / right / up for a camera yawed around Z and pitched down by `pitch`."""
    f = Vector((math.sin(yaw) * math.cos(pitch),
                math.cos(yaw) * math.cos(pitch),
                -math.sin(pitch)))
    f.normalize()
    q = f.to_track_quat("-Z", "Y")
    m = q.to_matrix()
    return f, m.col[0].copy(), m.col[1].copy(), q


def tangents(lens, res_x, res_y, margin=0.05):
    """Half-frame tangents. Blender fits the 36mm sensor to the LARGER pixel axis, so in
    9:16 the sensor maps to HEIGHT: visible height = 36/lens * distance. Getting this
    backwards is how a shot ends up with the subject cropped off the sides."""
    if res_y >= res_x:
        ty = 18.0 / lens
        tx = ty * (res_x / res_y)
    else:
        tx = 18.0 / lens
        ty = tx * (res_y / res_x)
    return tx * (1.0 - margin), ty * (1.0 - margin)


def solve_shot(points, aim, yaw, pitch, tx, ty):
    """Camera position that puts every point in `points` inside the frame.

    For a camera at C = aim - f*D, a point p has camera-space right/up offsets that do not
    depend on D at all, and depth z0 + D. So "inside the frame" is |x| <= (z0+D)*tx, i.e.
    D >= |x|/tx - z0 - one inequality per point per axis, and the answer is their maximum.
    No search, no guessing, no distance constant to tune.

    The aim then slides sideways/up until the content sits centred in the frame, because a
    correct distance with everything crammed against one edge is still a bad shot.
    """
    f, r, u, q = camera_basis(yaw, pitch)
    aim = Vector(aim)

    def distance(a):
        D = 1.0
        for p in points:
            d = Vector(p) - a
            z0, x, y = d.dot(f), d.dot(r), d.dot(u)
            # ...and never let anything end up behind or on top of the lens.
            D = max(D, abs(x) / tx - z0, abs(y) / ty - z0, 0.6 - z0)
        return D

    D = distance(aim)
    for _ in range(6):
        sx, sy, depth = [], [], []
        for p in points:
            d = Vector(p) - aim
            z = d.dot(f) + D
            sx.append(d.dot(r) / z)
            sy.append(d.dot(u) / z)
            depth.append(z)
        mid_x = (min(sx) + max(sx)) / 2.0
        mid_y = (min(sy) + max(sy)) / 2.0
        mean_z = sum(depth) / len(depth)
        if abs(mid_x) < 1e-4 and abs(mid_y) < 1e-4:
            break
        aim = aim + r * (mid_x * mean_z) + u * (mid_y * mean_z)
        # Re-solved after every shift, not once at the start: sliding the aim to centre
        # the content changes which point is the binding one, and returning a distance
        # that was solved for the previous aim crops whatever moved outwards.
        D = distance(aim)
    return aim - f * D, aim, f, r, u, q, D


def screen_of(p, cam_pos, f, r, u, tx, ty):
    """Normalised screen position, +-1 at the frame edges. z<=0 means behind the camera."""
    d = Vector(p) - cam_pos
    z = d.dot(f)
    if z <= 1e-4:
        return 9.9, 9.9, z
    return d.dot(r) / (z * tx), d.dot(u) / (z * ty), z


def sphere_in_frame(c, radius, cam_pos, f, r, u, tx, ty):
    sx, sy, z = screen_of(c, cam_pos, f, r, u, tx, ty)
    if z <= 1e-4:
        return False
    return abs(sx) + radius / (z * tx) <= 1.0 and abs(sy) + radius / (z * ty) <= 1.0


# ------------------------------------------------------------------------------ the ball


def ball_radius_for(mass, wall_h):
    """Radius of a solid steel sphere of that mass, kept inside a sane share of the wall.

    Real wrecking balls are small next to the buildings they demolish, and the honest
    number is the one that makes a mass sweep read: 1.5t is 0.36m, 6t is 0.57m, 20t is
    0.85m. The clamp only exists so an absurd mass cannot produce a marble or a moon.
    """
    r = (3.0 * mass / (4.0 * math.pi * STEEL_DENSITY)) ** (1.0 / 3.0)
    return min(max(r, 0.045 * wall_h), 0.22 * wall_h)


def swing_schedule(a_start, a_return, contact_frame, fps, arm):
    """Angle from straight-down on every frame. Positive is past the wall.

    Before the hit the crane is DRIVING the ball, so the arc is faster than gravity alone
    would swing it (a free pendulum this long takes ~1.4s to fall through 56 degrees and a
    1.4s wind-up is dead air in a short). t*t accelerates the way a swing does.

    After the hit the ball is a free pendulum again, at the real period for its arm and at
    a fraction of its amplitude - the wall ate the rest. The velocity discontinuity at the
    contact frame is not an artefact, it IS the wall being hit: the ball visibly slams to
    a slower motion at the instant it makes contact.
    """
    period = 2.0 * math.pi * math.sqrt(arm / GRAVITY)

    def angle(frame):
        if frame <= contact_frame:
            t = (frame - 1) / max(1, contact_frame - 1)
            return -a_start * (1.0 - t * t)
        dt = (frame - contact_frame) / fps
        return a_return * math.sin(2.0 * math.pi * dt / period) * math.exp(-0.22 * dt)

    return angle


def stage_shot(W, H, T, R, hit_x, hit_z, a_return, angle_key, lens, res_x, res_y):
    """Solve the camera and the chain length together. Pure maths - no bpy, so it can be
    exercised without Blender, which is the only way any of this gets checked before an
    hour of GPU time is spent on it.

    Returns a dict with the camera frame, the distance, the arm length and the pivot.
    """
    cam_cfg = CAMERA_ANGLES[angle_key]
    yaw = math.radians(cam_cfg["yaw"])
    pitch = math.radians(cam_cfg["pitch"])
    tx, ty = tangents(lens, res_x, res_y)
    contact_y = -T / 2.0 - R
    # The floor strip the debris lands on, on the far side of the wall - it has to be in
    # frame and lit, otherwise the pieces leave the picture and the payoff goes with them.
    pad = [(sx * W / 2.0, T / 2.0 + fy * 0.5 * H, 0.0)
           for sx in (-1, 1) for fy in (0.0, 1.0)]

    def shot_for(arm, pitch):
        """Frame the wall, the landing strip, and the ball everywhere it can be from the
        moment of contact onwards.

        The post-impact arc is sampled by ANGLE across the whole band the ball can reach
        (-a_return..+a_return), not by frame: the swing timing is solved afterwards and
        framing that depended on a provisional contact frame would silently stop covering
        the ball as soon as that number moved.

        The approach is deliberately NOT in this set. Framing the whole arc pushes the
        camera back until the wall covers a fifth of the frame; the arrival is what has to
        be visible, and lead-in visibility is bought with time instead (solve_timing).
        """
        pivot = Vector((hit_x, contact_y, hit_z + arm))
        pts = [(sx * W / 2.0, sy * T / 2.0, sz * H)
               for sx in (-1, 1) for sy in (-1, 1) for sz in (0.0, 1.0)] + pad
        _, r0, u0, _ = camera_basis(yaw, pitch)
        # The follow-through is capped by DISTANCE here, not by angle - see TRAVEL_CAP.
        # The arm only ever grows in the loop below, so this only ever tightens: no
        # oscillation between a longer chain and a wider swing.
        a_eff = min(a_return, math.asin(min(0.98, TRAVEL_CAP * H / arm)))
        for i in range(25):
            a = -a_eff + (2.0 * a_eff) * i / 24.0
            c = pivot + Vector((0.0, math.sin(a), -math.cos(a))) * arm
            pts += [tuple(c + r0 * R), tuple(c - r0 * R),
                    tuple(c + u0 * R), tuple(c - u0 * R)]
        return solve_shot(pts, (hit_x * 0.4, T / 2.0, cam_cfg["aim_z"] * H),
                          yaw, pitch, tx, ty), pivot, a_eff

    # Lengthen the arm until the pivot sits above the top edge of the frame. A wrecking
    # ball hangs off a crane that is out of shot; an anchor floating in mid-frame reads as
    # a modelling bug, and framing the anchor as well would shrink the wall to nothing.
    arm = 1.35 * H
    pivot_out = False
    for _ in range(60):
        (cam_pos, aim, f, r, u, q, D), pivot, a_eff = shot_for(arm, pitch)
        _, py, pz = screen_of(pivot, cam_pos, f, r, u, tx, ty)
        # 1.12, not 1.0: screen_of measures against the SAFE frame, which tangents() has
        # already pulled 5% inside the real one. A pivot at 1.0 here is still 5% inside
        # the rendered picture - visibly a floating anchor, and it passed the check.
        if pz > 0 and py >= 1.12:
            pivot_out = True
            break
        arm *= 1.05

    # A camera that has ended up underground is worse than one at a compromised angle.
    # cam_z = aim_z + D*sin(pitch), so the floor sets a LOWER bound on the pitch - the
    # `low` preset looks up, and taking the sign of this the other way round flipped it
    # into a downward-looking shot, which is the one thing that angle must never be.
    # Iterated, because raising the pitch re-solves the distance and a pitch computed from
    # the old distance lands just under the target (measured: 0.44 against 0.45).
    for _ in range(4):
        if cam_pos.z >= CAM_MIN_Z:
            break
        floor_pitch = math.asin(min(0.9, max(-0.9, (CAM_MIN_Z - aim.z) / max(1.0, D))))
        if floor_pitch <= pitch + 1e-5:
            break
        pitch = floor_pitch
        (cam_pos, aim, f, r, u, q, D), pivot, a_eff = shot_for(arm, pitch)

    return {"cam_pos": cam_pos, "aim": aim, "f": f, "r": r, "u": u, "q": q, "D": D,
            "tx": tx, "ty": ty, "arm": arm, "pivot": pivot, "pivot_out": pivot_out,
            "pitch": pitch, "a_return": a_eff}


def solve_timing(shot, R, a_start, fps, total, impact_second, lead_seconds):
    """When the ball lands, from two hard constraints rather than from taste.

    Returns (contact_frame, note, degrees at which the ball enters the frame).
    """
    arm, pivot = shot["arm"], shot["pivot"]
    contact_frame = max(2, int(round(impact_second * fps)))
    # 1. The ball must not step further than its own radius between frames, or a fast
    #    kinematic sphere skips past a course of pieces without ever overlapping it.
    #    Peak angular rate of the t*t ease is 2*a_start/(cf-1) radians per frame.
    need_step = math.ceil(1.0 + arm * 2.0 * a_start / R)
    # 2. The ball must be fully in frame for lead_seconds before it lands. Walk the arc
    #    back from the contact point to find the angle at which it enters the frame, then
    #    solve the ease for the number of frames that leaves between entry and impact.
    lead_frames = max(2, int(round(lead_seconds * fps)))
    enter = 0.0
    for deg in range(0, int(math.degrees(a_start)) + 1):
        a = -math.radians(deg)
        c = pivot + Vector((0.0, math.sin(a), -math.cos(a))) * arm
        if not sphere_in_frame(c, R, shot["cam_pos"], shot["f"], shot["r"], shot["u"],
                               shot["tx"], shot["ty"]):
            break
        enter = math.radians(deg)
    # angle = -a_start*(1-t^2)  =>  t at the entry angle
    t_enter = math.sqrt(max(0.0, 1.0 - enter / a_start)) if a_start > 0 else 0.0
    need_lead = math.ceil(lead_frames / max(0.05, 1.0 - t_enter))
    contact_frame = max(contact_frame, need_step, need_lead)
    note = ""
    if contact_frame > int(0.55 * total):
        contact_frame = max(2, int(0.55 * total))
        note = " (clamped: the shot is too short for a clean swing)"
    return contact_frame, note, math.degrees(enter)


# ----------------------------------------------------------------------------------- run


def main():
    P = _params()
    OUT = P.get("out_dir", PARAMS["out_dir"]["default"])
    RES_X, RES_Y = _get(P, "res_x"), _get(P, "res_y")
    SAMPLES, FPS = _get(P, "samples"), _get(P, "fps")
    SECONDS = _get(P, "seconds")
    PREVIEW = _get(P, "preview_frame")
    LABEL = str(P.get("label", ""))
    spec = MATERIALS[_get(P, "material")]
    material_name = _get(P, "material")
    cols, rows, layers = _get(P, "wall_cols"), _get(P, "wall_rows"), _get(P, "wall_depth")
    piece_h = _get(P, "piece_height")
    # The target must be BIG. A correct simulation of a small target still looks cheap:
    # under ~200 pieces a wall topples as a few slabs instead of coming apart. The
    # parameters are a request, this is the guarantee - grow both axes together so the
    # wall keeps the proportions that were asked for.
    grown = ""
    while cols * rows * layers < MIN_PIECES and (cols < PARAMS["wall_cols"]["range"][1]
                                                 or rows < PARAMS["wall_rows"]["range"][1]):
        cols = min(PARAMS["wall_cols"]["range"][1], cols + max(1, cols // 8))
        rows = min(PARAMS["wall_rows"]["range"][1], rows + max(1, rows // 8))
        grown = f" (grown from the requested size to reach {MIN_PIECES} pieces)"
    # ...and the shape has to work in a vertical frame - see MAX_W_OVER_H. Height is grown
    # first, because a taller wall is simply a better shot; the width is only cut back if
    # the row count has run out of room. Both directions keep the piece count over the
    # floor: the narrowest this can leave a maxed-out wall is 24x44.
    aspect = spec["aspect"]
    if cols * aspect > MAX_W_OVER_H * rows:
        rows = min(PARAMS["wall_rows"]["range"][1],
                   int(math.ceil(cols * aspect / MAX_W_OVER_H)))
        grown = " (made taller to fill a vertical frame)"
    if cols * aspect > MAX_W_OVER_H * rows:
        cols = max(PARAMS["wall_cols"]["range"][0], int(MAX_W_OVER_H * rows / aspect))
        grown = " (narrowed to fill a vertical frame)"
    # A ceiling too, taken out of the thickness first: depth is the dimension you can
    # least see, and every piece is a rigid body that has to be simulated and lit.
    while cols * rows * layers > MAX_PIECES and layers > 1:
        layers -= 1
    ball_mass = _get(P, "ball_mass")
    n_links = _get(P, "chain_links")
    angle_key = _get(P, "camera_angle")
    lens = _get(P, "lens")
    seed = _get(P, "seed")

    bpy.ops.wm.read_factory_settings(use_empty=True)
    sc = bpy.context.scene
    sc.render.engine = "CYCLES"       # headless EEVEE has no GPU context and falls back
    sc.cycles.samples = SAMPLES       # to a software rasteriser: ~19s/frame against 3s
    sc.cycles.use_denoising = True
    sc.cycles.max_bounces = 8
    sc.cycles.transmission_bounces = 10 if spec["transmission"] else 4
    print("CYCLES_DEVICE " + use_gpu(sc))
    sc.render.resolution_x, sc.render.resolution_y = RES_X, RES_Y
    sc.render.fps = FPS
    sc.frame_start = 1
    sc.frame_end = max(2, int(round(SECONDS * FPS)))
    # Linear keys, set BEFORE inserting any. There is a key on every frame and the motion
    # functions already carry their easing, so bezier handles would only overshoot between
    # them - and Blender 5.2 moved Action.fcurves behind slotted actions, so fixing the
    # interpolation after the fact is no longer a one-liner.
    bpy.context.preferences.edit.keyframe_new_interpolation_type = "LINEAR"
    # AgX with a punchy look: the default view transform is flat, and this is watched on a
    # phone. Every name here has moved between releases, hence the fallbacks.
    for tf in ("AgX", "Filmic", "Standard"):
        try:
            sc.view_settings.view_transform = tf
            break
        except TypeError:
            continue
    for look in ("AgX - Punchy", "Punchy", "AgX - Medium High Contrast", "None"):
        try:
            sc.view_settings.look = look
            break
        except TypeError:
            continue
    sc.view_settings.exposure = _get(P, "exposure")

    sc.world = bpy.data.worlds.new("W")
    sc.world.use_nodes = True
    sc.world.node_tree.nodes["Background"].inputs[0].default_value = (0.008, 0.009, 0.013, 1)

    # ---- wall first: everything else is solved from its dimensions
    pieces, W, H, T, (L, Tp, _), piece_mass = build_wall(
        spec, cols, rows, layers, piece_h, seed)

    # ---- floor and backdrop
    bpy.ops.mesh.primitive_plane_add(size=max(120.0, 30.0 * H), location=(0, 0, 0))
    floor = bpy.context.object
    floor.data.materials.append(mat("Floor", (0.055, 0.055, 0.062), rough=0.55))
    add_rigid([floor], "PASSIVE")
    floor.rigid_body.friction = 0.95
    floor.rigid_body.restitution = 0.03

    # A cyclorama, not a void. Radius 7x the wall so debris never reaches it and the
    # camera is comfortably inside; being that far out it only picks up a few percent of
    # the light, which is exactly the soft falloff that keeps the upper third of a 9:16
    # frame from going flat black.
    back_r = 7.0 * max(W, H)
    bpy.ops.mesh.primitive_cylinder_add(vertices=64, radius=back_r, depth=10.0 * H,
                                        location=(0, 0, 5.0 * H - 0.2))
    back = bpy.context.object
    back.data.materials.append(mat("Backdrop", (0.05, 0.055, 0.072), rough=0.9))

    # ---- pendulum, solved from the contact point backwards
    R = ball_radius_for(ball_mass, H)
    hit_x = _get(P, "hit_offset") * W / 2.0
    hit_z = _get(P, "hit_height") * H
    a_start = math.radians(_get(P, "swing_from_deg"))
    # Fraction of the swing the ball keeps after breaking through, from its mass: a 1.5t
    # ball is stopped near the face, a 20t ball carries on and takes the rest with it.
    keep = min(0.72, max(0.12, 0.38 * (ball_mass / 6000.0) ** 0.30))
    a_return = a_start * keep

    total = sc.frame_end
    # Camera, chain length and swing timing: all solved, none of them typed in. The
    # pendulum's contact point is the wall's front face, so the arm and the pivot fall out
    # of where the ball has to end up rather than the other way round.
    shot = stage_shot(W, H, T, R, hit_x, hit_z, a_return, angle_key, lens, RES_X, RES_Y)
    cam_pos, q, D = shot["cam_pos"], shot["q"], shot["D"]
    arm, pivot = shot["arm"], shot["pivot"]
    # The follow-through the shot was actually framed for: capped by TRAVEL_CAP once the
    # chain length is known. Using the requested angle here instead would swing the ball
    # straight out of the frame the camera was solved for.
    a_return = shot["a_return"]
    contact_frame, clipped, enter_deg = solve_timing(
        shot, R, a_start, FPS, total, _get(P, "impact_second"), _get(P, "lead_seconds"))
    angle_at = swing_schedule(a_start, a_return, contact_frame, FPS, arm)

    def ball_at(frame):
        a = angle_at(frame)
        return pivot + Vector((0.0, math.sin(a), -math.cos(a))) * arm

    # ---- the ball itself
    bpy.ops.mesh.primitive_uv_sphere_add(radius=R, segments=56, ring_count=28,
                                         location=tuple(ball_at(1)))
    ball = bpy.context.object
    ball.name = "Ball"
    ball.data.materials.append(mat("Steel", (0.30, 0.30, 0.33), rough=0.28, metal=1.0))
    bpy.ops.object.shade_smooth()
    add_rigid([ball], "ACTIVE")
    ball.rigid_body.mass = ball_mass
    ball.rigid_body.collision_shape = "SPHERE"
    ball.rigid_body.friction = 0.7
    ball.rigid_body.restitution = 0.0
    # Kinematic for the whole shot - see the module docstring. Bullet treats it as
    # infinite mass, so it shoves the wall and nothing the wall does can deflect it.
    ball.rigid_body.kinematic = True
    for fr in range(1, total + 1):
        ball.location = ball_at(fr)
        ball.keyframe_insert("location", frame=fr)

    # ---- the chain. A CHAIN IS NOT A ROD: separate interlocking links, re-placed on
    # every frame. Because the ball stays on the pendulum the chain length never changes,
    # so the links keep a constant pitch and the chain is exactly taut throughout.
    chain_len = arm - R
    pitch_len = chain_len / n_links
    steel = mat("ChainSteel", (0.16, 0.165, 0.18), rough=0.42, metal=1.0)
    links = []
    for i in range(n_links):
        try:
            bpy.ops.mesh.primitive_torus_add(
                major_radius=pitch_len * 0.62, minor_radius=pitch_len * 0.15,
                major_segments=16, minor_segments=8, location=(0, 0, 0))
            lk = bpy.context.object
            bpy.ops.object.shade_smooth()
        except (AttributeError, RuntimeError, TypeError):
            # A link that is a stretched box still reads as a chain at this size; a
            # missing operator must not take the shot down with it.
            lk = bpy.data.objects.new(
                "link", box_mesh(f"link{i}", (pitch_len * 1.3, pitch_len * 0.34,
                                              pitch_len * 0.34), steel))
            bpy.context.scene.collection.objects.link(lk)
        if lk.data.materials:
            lk.data.materials[0] = steel
        else:
            lk.data.materials.append(steel)
        # The torus ring lies in local XY with its hole along local Z, so pointing local X
        # down the chain puts the ring plane along the chain and the hole across it -
        # which is what a link looks like. 1.25 along X stretches it into a proper oval so
        # neighbours overlap instead of sitting in a dotted line.
        lk.scale = (1.25, 1.0, 1.0)
        links.append(lk)

    # The link frame is built by hand instead of with to_track_quat, and that matters: the
    # chain hangs vertically at the exact instant of impact, and asking for "local X along
    # the chain, local Z upwards" is degenerate there - the roll about the chain axis is
    # unconstrained, so it flips and snaps every link ninety degrees on the hero frame.
    # The pendulum only ever swings in the Y-Z plane, so world X is permanently square to
    # the chain and gives a frame that is continuous the whole way through.
    prev_euler = None
    for fr in range(1, total + 1):
        bottom = ball_at(fr)
        ex = (bottom - pivot).normalized()      # local X runs down the chain
        ez = Vector((1.0, 0.0, 0.0))            # local Z across the swing plane
        ey = ez.cross(ex)                       # right handed: X cross Y = Z
        q_link = Matrix((ex, ey, ez)).transposed().to_quaternion()
        e_even = q_link.to_euler("XYZ", prev_euler) if prev_euler else q_link.to_euler()
        prev_euler = e_even
        # Every other link is rolled 90 degrees about the chain, which is what makes a
        # chain look interlocked rather than like a stack of washers.
        e_odd = (q_link @ Quaternion((1.0, 0.0, 0.0), math.pi / 2)).to_euler("XYZ", e_even)
        for i, lk in enumerate(links):
            lk.location = pivot + ex * ((i + 0.5) * pitch_len)
            lk.rotation_euler = e_odd if i % 2 else e_even
            lk.keyframe_insert("location", frame=fr)
            lk.keyframe_insert("rotation_euler", frame=fr)

    # ---- camera
    bpy.ops.object.camera_add(location=tuple(cam_pos), rotation=q.to_euler())
    cam = bpy.context.object
    cam.data.lens = lens
    cam.data.clip_end = max(1000.0, back_r * 3.0)
    sc.camera = cam

    if LABEL:
        bpy.ops.object.text_add(location=(0, 0, 0))
        txt = bpy.context.object
        txt.data.body = LABEL
        txt.data.align_x = "CENTER"
        txt.data.align_y = "CENTER"
        txt.data.size = 0.012 * D
        txt.data.extrude = 0.0006 * D
        # Emissive on purpose: parented in front of the camera the caption sits outside
        # every light in the scene and a diffuse one renders as a grey smudge.
        txt.data.materials.append(mat("Label", (1, 1, 1), rough=0.9, emission=2.6))
        # Parented with an IDENTITY parent inverse so the location below really is camera
        # space. Setting matrix_parent_inverse to the camera's inverse (the reflex from
        # object parenting) turns these back into world coordinates and the caption lands
        # somewhere off in the set. A text object faces its local +Z and sits at negative
        # Z in front of the camera, so zero rotation already faces the lens.
        txt.parent = cam
        txt.rotation_euler = (0, 0, 0)
        d_txt = 0.12 * D
        txt.location = (0.0, (18.0 / lens * d_txt) * 0.80, -d_txt)

    # ---- lights. Positions are multiples of the wall height and energies scale with H*H,
    # because illuminance falls off with the square of the distance - without that, making
    # the wall bigger silently under-exposes the whole shot.
    k = H * H

    def area(loc, target, size, energy, color=(1, 1, 1)):
        bpy.ops.object.light_add(type="AREA", location=loc)
        lt = bpy.context.object
        lt.data.size = size
        lt.data.energy = energy
        lt.data.color = color
        aim_at(lt, target)
        # Lights are geometry to Cycles. A visible emitter parked in front of the wall
        # shows up as a white rectangle across the shot.
        lt.visible_camera = False
        return lt

    area((0.9 * W, -1.1 * W, 1.6 * H), (0, 0, 0.5 * H), 0.8 * W, 200 * k, (1.0, 0.97, 0.92))
    area((-1.2 * W, -0.8 * W, 0.9 * H), (0, 0, 0.45 * H), 0.9 * W, 48 * k, (0.72, 0.82, 1.0))
    # Rim from behind and above the wall: it separates the debris cloud from the backdrop,
    # which is the difference between "pieces flying" and "a dark smudge".
    area((-0.4 * W, 1.6 * W, 1.7 * H), (0, T / 2, 0.7 * H), 0.7 * W, 120 * k, (0.78, 0.88, 1.0))
    # And a wide wash straight down onto the strip of floor the rubble lands on. No third
    # of the frame may be empty and dark, and that third is the floor.
    area((0.0, T / 2 + 0.35 * H, 1.3 * H), (0, T / 2 + 0.35 * H, 0), 1.6 * W, 80 * k)

    # ---- solver
    if sc.rigidbody_world is None:
        bpy.ops.rigidbody.world_add()
    rw = sc.rigidbody_world
    rw.point_cache.frame_start = 1
    rw.point_cache.frame_end = sc.frame_end
    rw.substeps_per_frame = 12
    rw.solver_iterations = 20

    step = arm * 2.0 * a_start / max(1, contact_frame - 1)
    print(f"WALL {material_name} {cols}x{rows}x{layers}={len(pieces)} pieces{grown} "
          f"{W:.2f}x{T:.2f}x{H:.2f}m piece={L:.2f}x{Tp:.2f}x{piece_h:.2f}m "
          f"{piece_mass:.1f}kg")
    print(f"BALL r={R:.2f}m mass={ball_mass:.0f}kg arm={arm:.2f}m keep={keep:.2f} "
          f"follow_through={math.degrees(a_return):.0f}deg={arm * math.sin(a_return):.1f}m "
          f"step={step:.2f}m/frame ({step / (2 * R):.2f} ball diameters)")
    print(f"SHOT {angle_key} lens={lens:.0f} dist={D:.2f} cam_z={cam_pos.z:.2f} "
          f"contact_frame={contact_frame}{clipped} ball_enters_at={enter_deg:.0f}deg "
          f"pivot_above_frame={shot['pivot_out']}")

    if PREVIEW:
        # Step the solver frame by frame up to the preview. Jumping straight there renders
        # a wall that has never been touched - and the preview is the approval gate, so a
        # wrong one is worse than a slow one.
        n = min(int(PREVIEW), sc.frame_end)
        dg_pre = bpy.context.evaluated_depsgraph_get()
        for fr in range(1, n + 1):
            sc.frame_set(fr)
            dg_pre.update()
        sc.render.image_settings.file_format = "PNG"
        sc.render.filepath = f"{OUT}/preview.png"
        bpy.ops.render.render(write_still=True)
        print(f"SCENE_OK preview={n} pieces={len(pieces)} contact_frame={contact_frame}")
        return

    # ---- simulate and watch the real motion. Every frame written to impacts.json is a
    # frame on which something measurably happened: nothing here is derived from the
    # clip length or from where we expected the ball to be.
    dg = bpy.context.evaluated_depsgraph_get()
    rest = [p.location.copy() for p in pieces]
    # 5% of a piece. The stack starts deactivated, so an untouched piece moves EXACTLY
    # zero and there is no noise to reject - and the sound has to land on the frame the
    # wall was struck, not three frames later when a block has already travelled 8cm.
    moved_by = 0.05 * piece_h
    fall_thr = 0.30 * piece_h                  # metres per frame that counts as falling
    prev_z = [v.z for v in rest]
    prev_vz = [0.0] * len(pieces)
    impact_frame = None
    weight = {}
    for fr in range(1, sc.frame_end + 1):
        sc.frame_set(fr)
        dg.update()
        for i, p in enumerate(pieces):
            pos = p.evaluated_get(dg).matrix_world.translation
            if impact_frame is None and (pos - rest[i]).length > moved_by:
                impact_frame = fr
            vz = pos.z - prev_z[i]
            # Falling hard on the previous frame, stopped on this one: it hit the floor,
            # the ball or another piece. Weighted by how fast it arrived.
            if prev_vz[i] < -fall_thr and vz > 0.4 * prev_vz[i]:
                weight[fr] = weight.get(fr, 0.0) + (-prev_vz[i])
            prev_vz[i], prev_z[i] = vz, pos.z

    events = []
    if impact_frame:
        events.append({"frame": int(impact_frame), "kind": "impact", "strength": 1.0})
    # Thin the landings down to the moments a listener could actually separate: strongest
    # first, never within 4 frames of one already taken. A sound on every one of two
    # hundred landings is a wash of noise, not a collapse.
    top = max(weight.values()) if weight else 0.0
    taken = [impact_frame] if impact_frame else []
    for fr in sorted(weight, key=lambda w: -weight[w]):
        if len(events) >= 12:
            break
        if any(abs(fr - t) < 4 for t in taken):
            continue
        taken.append(fr)
        s = 0.25 + 0.75 * (weight[fr] / top) ** 0.6
        events.append({"frame": int(fr), "kind": "debris",
                       "strength": round(min(1.0, max(0.2, s)), 3)})
    events.sort(key=lambda e: e["frame"])
    with open(f"{OUT}/impacts.json", "w", encoding="utf-8") as fh:
        json.dump({"fps": FPS, "events": events, "impact_frame": impact_frame,
                   "material": material_name, "ball_mass": ball_mass,
                   "pieces": len(pieces)}, fh, indent=1)
    if impact_frame is None:
        print("WARN the ball never moved a piece - check hit_height/hit_offset")

    sc.render.image_settings.file_format = "PNG"   # 5.2 has no FFMPEG output at all
    sc.render.image_settings.color_mode = "RGB"
    sc.render.filepath = f"{OUT}/frame_"
    bpy.ops.render.render(animation=True)
    print(f"SCENE_OK material={material_name} pieces={len(pieces)} "
          f"frames={sc.frame_end} impact={impact_frame} events={len(events)}")


if bpy is not None:
    main()
