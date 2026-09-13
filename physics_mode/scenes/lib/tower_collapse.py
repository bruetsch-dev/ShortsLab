"""A slender tower of stacked blocks is hit and goes over: a weight dropped on its top, a ball dropped into a chute that turns the fall into a side impact, a swinging wrecking ball, or a horizontal ram.

    blender -b -noaudio -P tower_collapse.py -- '{"material": "brick", "out_dir": "..."}'

This file is HAND MAINTAINED. A model picks the scene and fills PARAMS; it never writes
geometry. Everything here is parametric, and every number that was solved rather than
guessed says so in a comment next to it.

THE PAYOFF IS THE TOPPLE, NOT THE STRIKE. That single sentence decides most of the design:

  * The tower is laid in a RUNNING BOND - every layer offset half a block diagonally from
    the one below - so it goes over as a structure that breaks up on the way down. Stacked
    in aligned columns it is not a tower at all, it is nine independent columns of cubes
    that slump straight into a heap the moment the base leaves.
  * The striker hits at `strike_height` and is built WIDE ENOUGH TO SPAN THE TOWER, so all
    of the columns in the struck layer go at once. A striker narrower than the tower punches
    a hole through it and the top drops vertically into the gap - correct physics, dull shot.
  * The camera is SOLVED FROM THE SIMULATION. A tower of height H sweeps an arc of radius H
    as it falls, so the picture it needs at the end is roughly H wide - four times the width
    it needs at the start. Framing for the end wastes the first three seconds on empty sky;
    framing for the start throws the fall out of shot. So the whole simulation is stepped
    once WITHOUT rendering, the bounds of the blocks are recorded per frame, and the camera
    is keyed to pull back exactly as fast as the debris field grows. Nothing is estimated.

Impact frames in impacts.json come from that same pre-pass: the first frame a block actually
moves is the strike, and the frames where many blocks arrive on the floor are the landings.
A sound gets placed on the frame named here, so a guess would be heard as a mistake.
"""

# --------------------------------------------------------------------------- parameters
# Read by the model that chooses this scene. Notes are written for someone who has never
# seen the shot, because that is exactly who reads them.
PARAMS = {
    "seed": {
        "range": [0, 9999], "default": 0,
        "note": "changes how the blocks are microscopically offset before the strike. The "
                "collapse is chaotic, so two runs with the same everything else but a "
                "different seed fall completely differently. Vary it to avoid repeating a "
                "video.",
    },

    "material": {
        "choices": ["brick", "concrete", "glass", "wood", "stone"],
        "default": "brick",
        "note": "what the tower is built of. Sets colour, roughness and real density, so it "
                "also sets how heavy each block is: wood 620 kg/m3 flies, stone 2700 "
                "slumps. Glass is translucent and roughly doubles render time.",
    },
    "blocks_tall": {
        "range": [12, 40], "default": 26,
        "note": "how many layers high the tower is. Height in metres is this times "
                "block_size. Raised automatically if needed to keep the tower above 200 "
                "pieces - fewer pieces than that and the collapse looks cheap.",
    },
    "cols": {
        "range": [3, 5], "default": 3,
        "note": "blocks across each layer, in both directions (3 means a 3x3 layer of 9). "
                "Keep it low: the tower must be slender or it does not topple, it crumbles.",
    },
    "block_size": {
        "range": [0.16, 0.45], "default": 0.28,
        "note": "edge length of one block in metres. Each block is built at 0.93 of its "
                "slot so a seam stays visible between them.",
    },
    "striker": {
        "choices": ["ball", "ram", "weight", "chute"],
        "default": "weight",
        "note": "what hits the tower, and from WHERE. The rule here is that a striker "
                "arrives from above - a weight flying in horizontally reads as a cannon "
                "rather than as gravity. 'weight' is dropped from a crane onto the TOP, "
                "off-centre, and pushes it over. 'chute' is fired in sideways and turned "
                "downward by a quarter-turn chute, so the impact is still vertical but the "
                "delivery is visible. 'ball' swings in on a real chain from a crane - the "
                "one sideways arrival that is kept, because a swinging wrecking ball is "
                "the thing itself and not a substitute for falling. 'ram' hits flat along "
                "a trestle and is the only purely horizontal option left.",
    },
    "strike_height": {
        "range": [0.02, 0.60], "default": 0.12,
        "note": "where the striker hits, as a fraction of tower height. 0.12 is a base "
                "strike: the tower goes over in one piece. Above ~0.4 the top half snaps "
                "off instead. IGNORED by the 'weight' striker, which lands on the top.",
    },
    "strike_speed": {
        "range": [4.0, 20.0], "default": 9.0,
        "note": "impact speed in m/s. The 'ball' is limited by what its swing can actually "
                "produce (about 9 m/s) and is clamped to that; 'ram' and 'weight' reach "
                "whatever is asked - the weight simply gets dropped from higher.",
    },
    "striker_mass_ratio": {
        "range": [5.0, 80.0], "default": 25.0,
        "note": "how heavy the striker is, counted in blocks. 25 means it weighs as much "
                "as 25 blocks of the chosen material. Below about 8 it bounces off.",
    },
    "eccentric": {
        "range": [0.0, 1.0], "default": 0.6,
        "note": "'weight' striker only: how far off centre it lands, as a fraction of the "
                "tower's half width. 0 drives the tower straight down into a heap; 0.6 is "
                "well outside the middle third, which is where a load has to land for the "
                "tower to go over instead of collapsing on the spot; 1.0 is the very edge.",
    },
    "seconds": {
        "range": [4.0, 9.0], "default": 5.5,
        "note": "clip length. The tower needs roughly 2s after the strike to finish going "
                "over and settle; shorter than 4.5s cuts the payoff off.",
    },
    "camera_follow": {
        "choices": [True, False], "default": True,
        "note": "True: the camera starts tight on the standing tower and cranes back as the "
                "collapse spreads, so the fall always stays in shot. False: one locked-off "
                "frame wide enough for everything that will happen (calmer, smaller tower).",
    },
    "fill": {
        "range": [0.55, 0.92], "default": 0.78,
        "note": "how much of the frame HEIGHT the action is allowed to occupy. The camera "
                "distance is solved from this - do not try to set a distance.",
    },
    "lens": {
        "range": [28.0, 70.0], "default": 40.0,
        "note": "focal length in mm. Short reads as dramatic and stretches the fall toward "
                "camera; long flattens it. The camera distance follows automatically.",
    },
    "yaw_deg": {
        "range": [-45.0, 45.0], "default": -18.0,
        "note": "camera swing around the tower. 0 is dead-on front with the tower falling "
                "to the right; negative moves the camera toward the striker's side.",
    },
    "pitch_deg": {
        "range": [64.0, 88.0], "default": 78.0,
        "note": "camera angle measured from straight down the +Z axis: 90 is level with the "
                "subject, 78 looks 12 degrees down. Below ~70 it starts to flatten the "
                "collapse into a top-down view, which kills it.",
    },
    "label": {
        "default": "",
        "note": "optional text burned into the corner of the frame, for parameter sweeps "
                "(e.g. '9 m/s'). Empty means no text.",
    },
    # --- supplied by the app, not by the model choosing the scene ---
    "out_dir": {"default": "//out", "note": "app-supplied: where frames and impacts.json go."},
    "res_x": {"default": 1080, "note": "app-supplied render width."},
    "res_y": {"default": 1920, "note": "app-supplied render height (9:16 vertical)."},
    "samples": {"default": 24, "note": "app-supplied Cycles samples."},
    "fps": {"default": 30, "note": "app-supplied frame rate."},
    "preview_frame": {"default": 0, "note": "app-supplied: >0 renders that ONE frame as "
                                            "preview.png and stops."},
}

import json
import math
import sys

try:
    import bpy
    import mathutils
except ModuleNotFoundError:      # outside Blender the module still imports, so the app can
    bpy = mathutils = None       # read PARAMS without starting a 3D session

argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
P = json.loads(argv[0]) if argv else {}

import random as _random

# Jitter as a FRACTION of a block: 0.004 is well under a millimetre on a 0.2m block, far
# too small to see in a still, and more than enough to send a chaotic collapse elsewhere.
JITTER = 0.004
SEED = int(P.get("seed", 0) or 0)
_RNG = _random.Random(SEED)

OUT = P.get("out_dir", "//out")
RES_X, RES_Y = int(P.get("res_x", 1080)), int(P.get("res_y", 1920))
SAMPLES = int(P.get("samples", 24))
FPS = int(P.get("fps", 30))
SECONDS = float(P.get("seconds", 5.5))
PREVIEW = int(P.get("preview_frame", 0))

MATERIAL = str(P.get("material", "brick")).lower()
COLS = max(3, min(5, int(P.get("cols", 3))))
BLOCKS_TALL = max(12, min(40, int(P.get("blocks_tall", 26))))
BLOCK = max(0.16, min(0.45, float(P.get("block_size", 0.28))))
STRIKER = str(P.get("striker", "ball")).lower()
STRIKE_H = max(0.02, min(0.60, float(P.get("strike_height", 0.12))))
STRIKE_V = max(4.0, min(20.0, float(P.get("strike_speed", 9.0))))
MASS_RATIO = max(5.0, min(80.0, float(P.get("striker_mass_ratio", 25.0))))
ECCENTRIC = max(0.0, min(1.0, float(P.get("eccentric", 0.6))))
FOLLOW = bool(P.get("camera_follow", True))
FILL = max(0.55, min(0.92, float(P.get("fill", 0.78))))
LENS = max(28.0, min(70.0, float(P.get("lens", 40.0))))
YAW = math.radians(float(P.get("yaw_deg", -18.0)))
PITCH = math.radians(max(64.0, min(88.0, float(P.get("pitch_deg", 78.0)))))
LABEL = str(P.get("label", "") or "")

# At least 200 pieces, enforced rather than requested. A 3x3 tower needs 23 layers to get
# there; anything less and there is simply not enough debris on screen for the collapse to
# be worth watching. Height is what goes up, never the width - a fat tower does not topple.
if COLS * COLS * BLOCKS_TALL < 200:
    BLOCKS_TALL = min(40, -(-200 // (COLS * COLS)))

G = 9.81
PRE_ROLL = 5          # frames of stillness before anything moves, so the strike registers
STEEL = (0.55, 0.56, 0.60, 1)
RIG = (0.12, 0.13, 0.15, 1)

# Real densities in kg/m3: they are what makes wood explode outward and stone slump.
# `fric`/`rest` are the solver's friction and bounce. Glass gets low friction (it slides
# out from under itself) and the only meaningful restitution in the set.
MATERIALS = {
    "brick":    {"color": (0.44, 0.17, 0.11, 1), "rough": 0.80, "trans": 0.0,
                 "density": 1900, "fric": 0.95, "rest": 0.02},
    "concrete": {"color": (0.46, 0.45, 0.43, 1), "rough": 0.88, "trans": 0.0,
                 "density": 2400, "fric": 0.90, "rest": 0.02},
    "glass":    {"color": (0.70, 0.86, 0.90, 1), "rough": 0.10, "trans": 0.80,
                 "density": 2500, "fric": 0.35, "rest": 0.20},
    "wood":     {"color": (0.45, 0.28, 0.13, 1), "rough": 0.70, "trans": 0.0,
                 "density": 620,  "fric": 0.80, "rest": 0.10},
    "stone":    {"color": (0.31, 0.31, 0.34, 1), "rough": 0.85, "trans": 0.0,
                 "density": 2700, "fric": 1.00, "rest": 0.02},
}
MAT = MATERIALS.get(MATERIAL, MATERIALS["brick"])


# ------------------------------------------------------------------------------ helpers


def use_gpu(sc) -> str:
    """Point Cycles at the GPU. OptiX first - RTX cards have the ray-tracing cores - then
    CUDA. On CPU a 165-frame collapse at 1080x1920 runs for hours, which is not a mode."""
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


def mat(name, rgba, rough=0.7, metal=0.0, trans=0.0, emit=0.0):
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


def box(name, size, loc, material, rot=(0, 0, 0), solid=False):
    """A box of exactly `size` metres at `loc`.

    primitive_cube_add(size=1) already spans ONE unit, so the scale below IS the dimension,
    not half of it. That mistake cost a full rebuild once. The scale is applied immediately
    afterwards so the mesh really is that size: a rigid body derives its collision shape
    from the object, and an unapplied non-uniform scale is the kind of thing that behaves
    differently in the sim than it looks in the viewport.

    `solid=True` gives it a PASSIVE body so the simulation collides with it. Every piece of
    rig the action can reach needs that - a ram released above a decorative trestle sinks
    straight through it on the frame it is let go.
    """
    bpy.ops.mesh.primitive_cube_add(size=1, location=loc, rotation=rot)
    ob = bpy.context.object
    ob.name = name
    ob.scale = size
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    ob.data.materials.append(material)
    if solid:
        bpy.ops.rigidbody.object_add(type="PASSIVE")
        ob.rigid_body.friction = 0.7
        ob.rigid_body.restitution = 0.0
    return ob


def _pct(vals, q):
    """Value at quantile q of an already-sorted list.

    The frame is fitted to the 3rd..97th percentile of the blocks rather than to their
    absolute extremes, because ONE brick skittering off across the floor would otherwise
    drag the camera back and shrink the collapse everybody came to watch."""
    if not vals:
        return 0.0
    i = int(round(q * (len(vals) - 1)))
    return vals[max(0, min(len(vals) - 1, i))]


def _smooth(seq, k):
    if k <= 0:
        return list(seq)
    n = len(seq)
    out = []
    for i in range(n):
        lo, hi = max(0, i - k), min(n, i + k + 1)
        out.append(sum(seq[lo:hi]) / float(hi - lo))
    return out


# ------------------------------------------------------------------------------- build


def build_floor():
    bpy.ops.mesh.primitive_plane_add(size=240, location=(0, 0, 0))
    floor = bpy.context.object
    floor.name = "Floor"
    # Not black. The tower lands on this, and a third of a 9:16 frame is floor - pure black
    # down there is a third of the video doing nothing. 0.05 with a half-rough finish picks
    # up the key light and gives the debris something to read against.
    floor.data.materials.append(mat("FloorMat", (0.052, 0.050, 0.055, 1), rough=0.45))
    bpy.ops.rigidbody.object_add(type="PASSIVE")
    floor.rigid_body.friction = 0.95
    floor.rigid_body.restitution = 0.0
    return floor


def build_tower():
    """Return (blocks, width, depth, height). Running bond, asleep, seams visible."""
    piece = BLOCK * 0.93           # 7% gap: without it the stack renders as one smooth slab
    volume = piece ** 3
    block_mass = MAT["density"] * volume
    # Five tints of the same material, dealt out by position. One flat colour over 234
    # cubes reads as a single extruded object; five reads as courses of masonry, and the
    # variation is what makes the seams visible once it is moving.
    mats = []
    for i in range(5):
        f = 0.82 + 0.09 * i
        c = MAT["color"]
        mats.append(mat(f"Block{i}", (c[0] * f, c[1] * f, c[2] * f, 1),
                        rough=MAT["rough"], trans=MAT["trans"]))
    blocks = []
    for iz in range(BLOCKS_TALL):
        # Running bond: every other layer sits a quarter of a block off in +X and -X in Y,
        # so each block bridges the four below it instead of resting on exactly one. This
        # is the difference between a tower going over and nine columns of cubes slumping.
        s = 1.0 if iz % 2 else -1.0
        ox, oy = s * BLOCK * 0.25, -s * BLOCK * 0.25
        for ix in range(COLS):
            for iy in range(COLS):
                x = (ix - (COLS - 1) / 2.0) * BLOCK + ox
                y = (iy - (COLS - 1) / 2.0) * BLOCK + oy
                z = iz * BLOCK + BLOCK / 2.0
                # Sub-millimetre placement jitter, driven by the seed. A rigid-body
                # collapse is chaotic: without it, identical parameters render a
                # pixel-identical video every time, and the library would produce the same
                # short twice for the same prompt. With it, the same tower falls a
                # different way each run while still looking deliberately built.
                x += _RNG.uniform(-JITTER, JITTER) * BLOCK
                y += _RNG.uniform(-JITTER, JITTER) * BLOCK
                bpy.ops.mesh.primitive_cube_add(
                    size=piece, location=(x, y, z),
                    rotation=(0, 0, _RNG.uniform(-JITTER, JITTER)))
                b = bpy.context.object
                b.data.materials.append(mats[(ix + iy + iz) % 5])
                bpy.ops.rigidbody.object_add(type="ACTIVE")
                rb = b.rigid_body
                rb.mass = block_mass
                rb.friction = MAT["fric"]
                rb.restitution = MAT["rest"]
                rb.collision_shape = "BOX"
                rb.use_margin = True
                rb.collision_margin = 0.0
                # Asleep until something touches it. A 26-layer stack left awake jitters
                # itself apart in the first half second and falls over on its own.
                rb.use_deactivation = True
                rb.use_start_deactivated = True
                blocks.append(b)
    span = (COLS - 1) * BLOCK + BLOCK * 0.5 + piece      # includes the bond offset
    return blocks, span, span, BLOCKS_TALL * BLOCK, block_mass


def build_crane(pivot_x, pivot_z, rig_mat):
    """Mast and jib for the hanging strikers. Returns the points the camera must contain.

    The mast stands BEHIND the pivot in depth (+Y) rather than beside it, so the whole rig
    costs the shot no frame width at all - it sits behind the swing where a real crane
    would be, and the tower keeps the middle of the picture.
    """
    back = 2.4
    top = pivot_z + 0.55
    box("CraneMast", (0.34, 0.34, top), (pivot_x, back, top / 2.0), rig_mat, solid=True)
    jib_len = back + 0.25
    box("CraneJib", (0.26, jib_len, 0.26),
        (pivot_x, back / 2.0 - 0.12, pivot_z + 0.30), rig_mat, solid=True)
    # A diagonal brace, because a bare L of two boxes reads as a placeholder
    brace = math.hypot(back * 0.75, top * 0.45)
    box("CraneBrace", (0.16, brace, 0.16),
        (pivot_x, back * 0.62, top * 0.70), rig_mat,
        rot=(math.atan2(top * 0.45, back * 0.75), 0, 0), solid=True)
    return [(pivot_x, back, top), (pivot_x, back, 0.0), (pivot_x, 0.0, pivot_z + 0.30)]


def build_chain(pivot, n_links, link_gap, chain_mat):
    """A CHAIN, not a rod: `n_links` separate tori, re-placed on every keyframe.

    A single stretched cylinder from pivot to ball is the giveaway that nobody built a
    chain. These are quaternion-keyed rather than euler-keyed: the swing turns through more
    than 90 degrees and an euler representation flips somewhere in the middle of it, which
    spins every link a full turn between two frames.
    """
    links = []
    for i in range(n_links):
        bpy.ops.mesh.primitive_torus_add(
            major_radius=link_gap * 0.62, minor_radius=link_gap * 0.20,
            major_segments=24, minor_segments=10, location=pivot)
        lk = bpy.context.object
        lk.name = f"Link{i:02d}"
        lk.data.materials.append(chain_mat)
        lk.rotation_mode = "QUATERNION"
        bpy.ops.object.shade_smooth()
        links.append(lk)
    return links


def place_chain(links, pivot, tip, frame, prev_quats):
    """Lay the links along pivot -> tip on ONE frame and key them there."""
    v = mathutils.Vector((tip[0] - pivot[0], tip[1] - pivot[1], tip[2] - pivot[2]))
    length = max(1e-4, v.length)
    d = v / length
    step = length / len(links)
    base = d.to_track_quat("Z", "Y")
    for i, lk in enumerate(links):
        p = mathutils.Vector(pivot) + d * (step * (i + 0.5))
        # Every second link turned a quarter turn about the chain axis - that alternation
        # IS what a chain looks like; without it this is a row of identical rings.
        q = base.copy()
        if i % 2:
            q = mathutils.Quaternion(d, math.radians(90.0)) @ q
        # Keep the sign continuous with the previous frame, or the shortest-path
        # interpolation between two keys takes the long way round and the link tumbles.
        if prev_quats.get(i) is not None and q.dot(prev_quats[i]) < 0:
            q = -q
        prev_quats[i] = q
        lk.location = p
        lk.rotation_quaternion = q
        lk.keyframe_insert("location", frame=frame)
        lk.keyframe_insert("rotation_quaternion", frame=frame)


def pendulum_table(theta0, arm, steps=1200):
    """Time to reach each angle for a REAL pendulum released from rest at theta0.

    Not the small-angle cosine: the ball is released near horizontal, where the harmonic
    approximation is out by more than 15% and the swing visibly arrives too early. Speed at
    angle t comes straight from energy, v = sqrt(2 g L (cos t - cos theta0)), and the times
    are integrated from that. The integrand is singular at the release angle, so each step
    is evaluated at its MIDPOINT - the first step is the only one carrying real error and
    it is the slowest part of the swing, where nobody can see it.
    """
    dth = theta0 / steps
    ts, ths, t = [0.0], [theta0], 0.0
    for i in range(steps):
        mid = theta0 - (i + 0.5) * dth
        w = math.sqrt(max(1e-9, (2.0 * G / arm) * (math.cos(mid) - math.cos(theta0))))
        t += dth / w
        ts.append(t)
        ths.append(theta0 - (i + 1) * dth)
    return ts, ths


def theta_at(ts, ths, t):
    if t <= 0:
        return ths[0]
    if t >= ts[-1]:
        return ths[-1]
    lo, hi = 0, len(ts) - 1
    while hi - lo > 1:
        m = (lo + hi) // 2
        if ts[m] <= t:
            lo = m
        else:
            hi = m
    f = (t - ts[lo]) / max(1e-9, ts[hi] - ts[lo])
    return ths[lo] + (ths[hi] - ths[lo]) * f


# ------------------------------------------------------------------------- the strikers
# Each returns (object, release_frame, rig_points, extra_info). The object is a rigid body
# that is KINEMATIC up to release_frame and dynamic after it, so it arrives carrying the
# velocity of the motion that was animated onto it.


def striker_ball(tower_w, tower_h, hit_z, mass, steel, rig_mat):
    # Size for COVERAGE, mass from the ratio - the two are deliberately unrelated. The ball
    # has to span the tower's depth so every column in the struck layer goes at once; a
    # ball sized from its own steel density would be a third of this and would drill a
    # neat hole through the middle instead.
    radius = max(0.30, 0.62 * tower_w)
    hit_z = max(hit_z, radius + 0.06)          # the ball may not be buried in the floor
    hit_x = -tower_w / 2.0 - radius
    # Arm length, then the release angle that DELIVERS the requested impact speed:
    # v = sqrt(2 g L (1 - cos theta0)). Past about 105 degrees the ball starts the swing
    # above its own pivot, which looks wrong, so the arm is what gives way first and the
    # speed is clamped to whatever that arm can honestly produce.
    arm = max(2.4, min(5.0, 0.48 * tower_h))
    cos0 = 1.0 - (STRIKE_V ** 2) / (2.0 * G * arm)
    cos_min = math.cos(math.radians(105.0))
    clamped = cos0 < cos_min
    cos0 = max(cos_min, min(0.94, cos0))
    theta0 = math.acos(cos0)
    speed = math.sqrt(2.0 * G * arm * (1.0 - cos0))
    pivot = (hit_x, 0.0, hit_z + arm)

    bpy.ops.mesh.primitive_uv_sphere_add(radius=radius, segments=48, ring_count=24,
                                         location=(pivot[0] - arm * math.sin(theta0), 0.0,
                                                   pivot[2] - arm * math.cos(theta0)))
    ball = bpy.context.object
    ball.name = "Ball"
    ball.data.materials.append(steel)
    bpy.ops.object.shade_smooth()
    bpy.ops.rigidbody.object_add(type="ACTIVE")
    ball.rigid_body.mass = mass
    ball.rigid_body.collision_shape = "SPHERE"
    ball.rigid_body.friction = 0.6
    ball.rigid_body.restitution = 0.04

    ts, ths = pendulum_table(theta0, arm)
    swing_frames = max(6, int(math.ceil(ts[-1] * FPS)))
    # Released two frames short of the bottom: the last stretch is covered dynamically so
    # the solver owns the contact instead of a kinematic body shoving its way in.
    release = PRE_ROLL + max(1, swing_frames - 2)
    n_links = max(8, min(14, int(round((arm - radius) / 0.32))))
    links = build_chain(pivot, n_links, (arm - radius) / n_links, rig_mat)
    prev_q = {}
    path = {}
    for f in range(1, release + 1):
        t = max(0.0, (f - PRE_ROLL)) / float(FPS)
        th = theta_at(ts, ths, t)
        pos = (pivot[0] - arm * math.sin(th), 0.0, pivot[2] - arm * math.cos(th))
        ball.location = pos
        ball.rigid_body.kinematic = True
        ball.keyframe_insert("location", frame=f)
        ball.keyframe_insert("rigid_body.kinematic", frame=f)
        place_chain(links, pivot, pos, f, prev_q)
        path[f] = pos
    ball.rigid_body.kinematic = False
    ball.keyframe_insert("rigid_body.kinematic", frame=release + 1)

    rig = build_crane(pivot[0], pivot[2], rig_mat)
    info = {"kind": "ball", "speed": speed, "radius": radius, "arm": arm, "size": radius,
            "theta0_deg": math.degrees(theta0), "clamped": clamped,
            "pivot": pivot, "links": links, "prev_q": prev_q, "hit_z": hit_z}
    return ball, release, rig + [pivot], info


def striker_ram(tower_w, tower_d, hit_z, mass, steel, rig_mat):
    length, height = 1.10, max(0.34, min(0.9, tower_w * 0.7))
    width = tower_d * 1.25                      # wider than the tower: hits every column
    # Snapped either onto the floor or onto a trestle, never in between: a ram asked to
    # strike at 0.02 of a 7 m tower ends up 2 cm off the ground, which is a hovering steel
    # block for the whole run-up.
    hit_z = max(hit_z, height / 2.0)
    if hit_z - height / 2.0 < 0.05:
        hit_z = height / 2.0
    face = -tower_w / 2.0
    # 2.2 m of run-up, not the 3.4 that reads better on paper. The run-up is permanent
    # scenery on the -X side, and the camera has to contain it for the whole clip while the
    # tower falls the other way: at 3.4 the solved distance came out 50% further than the
    # same shot with the swinging ball and the tower dropped to a third of the frame height.
    # A shorter run-up just means a harder acceleration, which is what a piston does anyway.
    run_up = 2.2
    # Constant acceleration over the run-up, so that it arrives at exactly STRIKE_V:
    # v^2 = 2 a s. The trestle it rides on is solid from the floor up to the strike height,
    # which is how a strike above floor level happens without anything floating.
    accel = STRIKE_V ** 2 / (2.0 * run_up)
    t_run = STRIKE_V / accel
    start_x = face - length / 2.0 - run_up

    ram = box("Ram", (length, width, height), (start_x, 0, hit_z), steel)
    bpy.ops.rigidbody.object_add(type="ACTIVE")
    ram.rigid_body.mass = mass
    ram.rigid_body.collision_shape = "BOX"
    ram.rigid_body.friction = 0.7
    ram.rigid_body.restitution = 0.02

    rail_top = hit_z - height / 2.0
    rig_pts = []
    if rail_top > 0.04:
        rail_x0 = start_x - length
        rail_x1 = face - 0.04
        box("RamTrestle", (rail_x1 - rail_x0, width * 1.25, rail_top),
            ((rail_x0 + rail_x1) / 2.0, 0, rail_top / 2.0), rig_mat, solid=True)
        # Only the tower end of the rail is a point the camera must hold. The ram itself is
        # always in the frame (the solver tracks it every frame), so the rail runs out of
        # shot behind it the way a rail should, instead of dragging the whole frame wider.
        rig_pts = [(rail_x1, 0, rail_top)]

    frames = max(4, int(math.ceil(t_run * FPS)))
    release = PRE_ROLL + max(1, frames - 2)
    for f in range(1, release + 1):
        t = max(0.0, (f - PRE_ROLL)) / float(FPS)
        x = start_x + 0.5 * accel * t * t
        ram.location = (x, 0.0, hit_z)
        ram.rigid_body.kinematic = True
        ram.keyframe_insert("location", frame=f)
        ram.keyframe_insert("rigid_body.kinematic", frame=f)
    ram.rigid_body.kinematic = False
    ram.keyframe_insert("rigid_body.kinematic", frame=release + 1)
    info = {"kind": "ram", "speed": STRIKE_V, "hit_z": hit_z, "clamped": False,
            "size": max(length, width, height) / 2.0}
    return ram, release, rig_pts, info


def striker_weight(tower_w, tower_d, tower_h, mass, steel, rig_mat):
    """Dropped from a crane onto the TOP of the tower, off-centre.

    `strike_height` does not apply here and is ignored: a weight falling straight down
    meets the tower at its top, and the topple comes from landing off the centre line
    rather than from where on the side it lands.
    """
    size = max(0.5, tower_d * 1.15)
    height = max(0.45, size * 0.62)
    # Drop height from the requested speed - free fall, v^2 = 2 g h - capped at 40% of the
    # tower, because the crane has to hold the weight ABOVE the drop and every metre of
    # crane is a metre of frame the tower does not get.
    drop = min(0.40 * tower_h, STRIKE_V ** 2 / (2.0 * G))
    speed = math.sqrt(2.0 * G * drop)
    # Off-centre as a fraction of the tower's half width: a load landing inside the middle
    # third pushes the tower straight down into a heap, outside it tips the tower over.
    x0 = ECCENTRIC * tower_w / 2.0
    z_contact = tower_h + height / 2.0
    z0 = z_contact + drop

    w = box("Weight", (size, size, height), (x0, 0, z0), steel)
    bpy.ops.rigidbody.object_add(type="ACTIVE")
    w.rigid_body.mass = mass
    w.rigid_body.collision_shape = "BOX"
    w.rigid_body.friction = 0.8
    w.rigid_body.restitution = 0.0

    t_fall = math.sqrt(2.0 * drop / G)
    frames = max(3, int(math.ceil(t_fall * FPS)))
    release = PRE_ROLL + max(1, frames - 2)
    for f in range(1, release + 1):
        t = max(0.0, (f - PRE_ROLL)) / float(FPS)
        w.location = (x0, 0.0, max(z_contact, z0 - 0.5 * G * t * t))
        w.rigid_body.kinematic = True
        w.keyframe_insert("location", frame=f)
        w.keyframe_insert("rigid_body.kinematic", frame=f)
    w.rigid_body.kinematic = False
    w.keyframe_insert("rigid_body.kinematic", frame=release + 1)

    # The crane it was hanging from, with the chain still hanging where the hook let go.
    # The chain is vertical and stays put - the weight simply drops out of it - which is
    # why it needs no per-frame placement here.
    hang = 1.0
    pivot_z = z0 + height / 2.0 + hang
    rig = build_crane(x0, pivot_z, rig_mat)
    links = build_chain((x0, 0.0, pivot_z), 9, hang / 9.0, rig_mat)
    place_chain(links, (x0, 0.0, pivot_z), (x0, 0.0, pivot_z - hang), 1, {})
    info = {"kind": "weight", "speed": speed, "drop": drop, "clamped": False,
            "hit_z": tower_h, "size": max(size, height) / 2.0}
    return w, release, rig + [(x0, 0.0, pivot_z)], info


# ------------------------------------------------------------------- simulate, then frame


def striker_chute(tower_w, tower_d, tower_h, mass, steel, rig_mat):
    """Fired in sideways, turned downwards by a curved chute, lands on the tower top.

    The house rule for this library is that a striker arrives from ABOVE - a weight that
    flies in horizontally reads as a cannon, not as gravity. A ram is the one delivery that
    cannot obey that, so instead of dropping it, this sends it through a quarter-turn
    chute: it enters horizontally at the top of frame, the curve takes the horizontal
    speed and hands it back pointing down, and the tower is hit vertically. The swinging
    wrecking ball keeps its sideways arc, because a wrecking ball swinging is the thing
    itself and not a substitute for falling.

    The chute is built from BOXES, not a tube mesh. A closed tube needs mesh collision,
    which is where a fast small body tunnels through a thin wall; a trough of short flat
    plates on the OUTSIDE of the curve is what the ball is pressed into anyway, and every
    plate is a primitive the solver handles exactly.
    """
    radius = max(0.9, tower_d * 1.9)
    ball_r = max(0.16, tower_d * 0.34)
    # The ball is DROPPED and the bend turns that fall into a SIDE impact: it enters the
    # chute travelling straight down and leaves it travelling horizontally into the tower.
    # So the exit sits at the strike height beside the tower, the centre of curvature is
    # one radius above the exit, and the entry is one radius to the side of the centre:
    #     entry = (cx + R, cz    )  falling
    #     exit  = (cx,     cz - R)  travelling in -x, into the tower
    exit_x = tower_w / 2.0 + ball_r * 1.15
    exit_z = max(ball_r * 1.3, STRIKE_H)
    centre = (exit_x, 0.0, exit_z + radius)

    plate_w = ball_r * 3.4
    seg_len = radius * (math.pi / 2.0) / 15.0 * 1.6
    segments = 16
    for i in range(segments):
        t = i / float(segments - 1)
        a = t * (math.pi / 2.0)                                 # 0 = entry (top), 90 = exit
        ca, sa = math.cos(a), -math.sin(a)                       # exit lies BELOW the centre
        # Plate tangent to the arc: VERTICAL where the ball drops in, FLAT where it leaves
        # sideways. Inverted, a wall stands across the entry and the ball lands on its lid.
        tilt = -(math.pi / 2.0 - a)
        # BOTH walls. A single trough only holds the ball where its own weight presses it
        # into the floor; through a quarter turn the useful side changes from below to
        # behind, and an unguided ball simply leaves at the first segment.
        for sign, tag in ((1.0, "out"), (-1.0, "in")):
            off = radius + sign * ball_r * 1.35
            seg = box("Chute%s%02d" % (tag, i), (seg_len, plate_w, ball_r * 0.3),
                      (centre[0] + ca * off, 0.0, centre[2] + sa * off), rig_mat)
            seg.rotation_euler = (0.0, tilt, 0.0)
            bpy.ops.rigidbody.object_add(type="PASSIVE")
            seg.rigid_body.friction = 0.2
            seg.rigid_body.restitution = 0.05
        for side in (-1, 1):                                    # side rails
            rail = box("ChuteRail%02d%s" % (i, "+" if side > 0 else "-"),
                       (seg_len, ball_r * 0.3, ball_r * 3.0),
                       (centre[0] + ca * radius, side * plate_w / 2.0,
                        centre[2] + sa * radius), rig_mat)
            rail.rotation_euler = (0.0, tilt, 0.0)
            bpy.ops.rigidbody.object_add(type="PASSIVE")
            rail.rigid_body.friction = 0.2

    entry_x = centre[0] + radius
    entry_z = centre[2]
    # Free fall, so the drop height IS the requested impact speed: v^2 = 2 g h. No
    # keyframes and no launcher - the ball is simply let go above the mouth, which is the
    # whole point of routing a fall into a side impact rather than firing something in.
    speed = STRIKE_V
    drop = min(3.5 * radius, speed ** 2 / (2.0 * G))
    release = PRE_ROLL

    bpy.ops.mesh.primitive_uv_sphere_add(radius=ball_r, segments=36, ring_count=18,
                                         location=(entry_x, 0.0, entry_z + drop))
    ball = bpy.context.object
    ball.name = "ChuteBall"
    ball.data.materials.append(steel)
    for poly in ball.data.polygons:
        poly.use_smooth = True
    bpy.ops.rigidbody.object_add(type="ACTIVE")
    ball.rigid_body.mass = mass
    ball.rigid_body.collision_shape = "SPHERE"
    ball.rigid_body.friction = 0.4
    ball.rigid_body.restitution = 0.05

    # The ball hangs still for the pre-roll, then gravity does everything. Keeping it
    # kinematic for those frames stops it drifting before the shot has started.
    ball.rigid_body.kinematic = True
    ball.keyframe_insert("rigid_body.kinematic", frame=1)
    ball.keyframe_insert("rigid_body.kinematic", frame=max(1, release))
    ball.rigid_body.kinematic = False
    ball.keyframe_insert("rigid_body.kinematic", frame=release + 1)

    info = {"kind": "chute", "speed": speed, "drop": drop, "clamped": False,
            "hit_z": exit_z, "size": ball_r,
            "chute_radius": radius, "exit_z": exit_z}
    # The camera must contain the drop point and the chute mouth, or the delivery that
    # explains the side impact happens off screen and the shot looks like a cannon again.
    return ball, release, [(entry_x, 0.0, entry_z + drop), (exit_x, 0.0, exit_z)], info


def prepass(sc, blocks, striker, rest, frame_end):
    """Step the whole simulation WITHOUT rendering and write down what actually happened.

    Two things come out of this and nothing else in the file guesses at either: the frames
    the camera has to cover, and the frames a sound belongs on. It costs one extra pass of
    the solver; the render then re-runs the same deterministic simulation from frame 1 and
    reproduces it exactly.
    """
    dg = bpy.context.evaluated_depsgraph_get()
    boxes, striker_pos, landings = [], [], []
    strike_frame = None
    move_gate = BLOCK * 0.10          # a sleeping block moves 0mm; 10% of a block is a hit
    land_z = BLOCK * 0.62             # a block resting on the floor sits at 0.465 * BLOCK
    prev_z = [p[2] for p in rest]
    for f in range(1, frame_end + 1):
        sc.frame_set(f)
        dg.update()
        xs, ys, zs = [], [], []
        landed = 0
        moved = 0.0
        for i, b in enumerate(blocks):
            p = b.evaluated_get(dg).matrix_world.translation
            xs.append(p.x)
            ys.append(p.y)
            zs.append(p.z)
            # Full 3D displacement, not just height. A base strike shoves the bottom blocks
            # sideways along the floor and barely changes their z at all, so a z-only test
            # reports the strike several frames late - and a sound placed late is heard.
            moved = max(moved, math.dist((p.x, p.y, p.z), rest[i]))
            if prev_z[i] > land_z >= p.z and (prev_z[i] - p.z) > 0.012:
                landed += 1
            prev_z[i] = p.z
        xs.sort()
        ys.sort()
        zs.sort()
        boxes.append((_pct(xs, 0.03), _pct(xs, 0.97), _pct(ys, 0.03), _pct(ys, 0.97),
                      0.0, max(_pct(zs, 0.97), BLOCK)))
        sp = striker.evaluated_get(dg).matrix_world.translation
        striker_pos.append((sp.x, sp.y, sp.z))
        landings.append(landed)
        if strike_frame is None and moved > move_gate:
            strike_frame = f
    return boxes, striker_pos, landings, strike_frame


def events_from(landings, striker_pos, strike_frame):
    """impacts.json events, all of them taken from the pre-pass.

    The strike is the frame the first block moved. The landings are the local peaks of
    "how many blocks reached the floor this frame" - a tower going over does not land once,
    it lands in three or four distinct waves, and those waves are the rhythm of the sound.
    Peaks are kept 5 frames apart and capped at eight, because a sound on every one of the
    forty frames where something touched down is mud, not impact.
    """
    events = []
    if strike_frame:
        # Impact speed straight off the recorded path, in m/s, referenced to 12 m/s = full.
        i = max(1, strike_frame - 1)
        a, b = striker_pos[i - 1], striker_pos[min(len(striker_pos) - 1, i)]
        v = math.dist(a, b) * FPS
        events.append({"frame": int(strike_frame), "kind": "impact",
                       "strength": round(max(0.4, min(1.0, v / 12.0)), 3)})
    peak = max(landings) if landings else 0
    if peak:
        picked = []
        order = sorted(range(len(landings)), key=lambda i: -landings[i])
        for i in order:
            f = i + 1
            if landings[i] < max(2, peak * 0.18):
                break
            if any(abs(f - g) < 5 for g in picked):
                continue
            if strike_frame and abs(f - strike_frame) < 4:
                continue
            picked.append(f)
            if len(picked) >= 8:
                break
        for f in sorted(picked):
            s = 0.3 + 0.7 * (landings[f - 1] / float(peak))
            events.append({"frame": int(f), "kind": "land", "strength": round(min(1.0, s), 3)})
    events.sort(key=lambda e: e["frame"])
    return events


def solve_camera(sc, boxes, striker_pos, striker_size, fixed_points, frame_end):
    """Key the camera so everything that matters is inside the frame on every frame.

    Blender fits the sensor to the LARGER resolution axis, so in 9:16 the visible HEIGHT is
    36 / lens * distance and the width is that times 9/16. Rather than solving one distance
    from one bounding sphere, every corner of the per-frame bounds is projected onto the
    camera's own right/up/forward axes and the distance that just contains it is worked out
    exactly - which is the only version that survives the camera being both yawed and tilted
    down. The rotation never changes: the camera cranes, it does not pan.
    """
    d = mathutils.Vector((math.sin(YAW) * math.sin(PITCH),
                          -math.cos(YAW) * math.sin(PITCH),
                          math.cos(PITCH)))       # unit vector from the subject to the camera
    d.normalize()
    quat = (-d).to_track_quat("-Z", "Y")
    rot = quat.to_matrix()
    right, up = rot.col[0], rot.col[1]
    fwd = -rot.col[2]
    # Half-frame per metre of depth, honouring which axis the sensor is fitted to.
    if RES_Y >= RES_X:
        hh = 18.0 / LENS
        hw = hh * (RES_X / float(RES_Y))
    else:
        hw = 18.0 / LENS
        hh = hw * (RES_Y / float(RES_X))
    fill_v = FILL
    fill_h = min(0.92, FILL * 1.10)               # the fall spreads sideways; let it use it

    def corners(bx, f):
        x0, x1, y0, y1, z0, z1 = bx
        pts = [(x, y, z) for x in (x0, x1) for y in (y0, y1) for z in (z0, z1)]
        sx, sy, sz = striker_pos[f - 1]
        r = striker_size
        pts += [(sx + i * r, sy + j * r, sz + k * r)
                for i in (-1, 1) for j in (-1, 1) for k in (-1, 1)]
        return pts + fixed_points

    def need(pts, c):
        want = 0.0
        for p in pts:
            q = (p[0] - c[0], p[1] - c[1], p[2] - c[2])
            dep = q[0] * fwd[0] + q[1] * fwd[1] + q[2] * fwd[2]
            v = abs(q[0] * up[0] + q[1] * up[1] + q[2] * up[2])
            h = abs(q[0] * right[0] + q[1] * right[1] + q[2] * right[2])
            want = max(want, v / (fill_v * hh) - dep, h / (fill_h * hw) - dep)
        return want

    all_pts = [corners(boxes[f - 1], f) for f in range(1, frame_end + 1)]
    centres = []
    for pts in all_pts:
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        zs = [p[2] for p in pts]
        centres.append([(min(xs) + max(xs)) / 2.0, (min(ys) + max(ys)) / 2.0,
                        (min(zs) + max(zs)) / 2.0])

    bpy.ops.object.camera_add(location=(0, -10, 3))
    cam = bpy.context.object
    cam.name = "Cam"
    cam.data.lens = LENS
    cam.rotation_euler = quat.to_euler()
    sc.camera = cam
    min_dist = 2.0 + BLOCK * COLS * 2.0

    if not FOLLOW:
        # One locked-off frame: the union of every frame's bounds, solved once.
        flat = [p for pts in all_pts for p in pts]
        xs = [p[0] for p in flat]
        ys = [p[1] for p in flat]
        zs = [p[2] for p in flat]
        c = [(min(xs) + max(xs)) / 2.0, (min(ys) + max(ys)) / 2.0,
             (min(zs) + max(zs)) / 2.0]
        dist = max(min_dist, max(need(pts, c) for pts in all_pts))
        cam.location = (c[0] + d.x * dist, c[1] + d.y * dist,
                        max(0.6, c[2] + d.z * dist))
        return cam, dist

    # A camera operator does not chase every brick: the aim point is smoothed hard over
    # +-12 frames (0.8s), and the distance is then re-solved AGAINST THE SMOOTHED AIM so
    # the framing is still exact after the smoothing rather than approximately right.
    cx = _smooth([c[0] for c in centres], 12)
    cy = _smooth([c[1] for c in centres], 12)
    cz = _smooth([c[2] for c in centres], 12)
    req = [need(all_pts[i], (cx[i], cy[i], cz[i])) for i in range(frame_end)]
    # Pull back 10 frames BEFORE it is needed and never push back in. Anticipating reads as
    # an operator who can see what is coming; a camera that creeps forward again between
    # two waves of debris reads as a mistake.
    look = 10
    lead = [max(req[i:min(frame_end, i + look + 1)] or [req[i]]) for i in range(frame_end)]
    run = []
    m = 0.0
    for v in lead:
        m = max(m, v)
        run.append(m)
    soft = _smooth(run, 6)
    dist = [max(min_dist, soft[i], req[i]) for i in range(frame_end)]

    for f in range(1, frame_end + 1):
        i = f - 1
        cam.location = (cx[i] + d.x * dist[i], cy[i] + d.y * dist[i],
                        max(0.6, cz[i] + d.z * dist[i]))
        cam.keyframe_insert("location", frame=f)
    return cam, max(dist)


def build_lights(tower_h):
    """Key, fill and a wide light lying along the ground where the tower lands.

    Energy is not a taste number: an area light's irradiance falls off as 1/d^2, and the
    only value that was ever eyeballed here is 1800 W at 5 m on the wrecking-ball scene.
    Everything below is that value carried to this scene's distance - 1800 / 25 = 72 W per
    square metre of distance - which is why a 40-block tower is lit the same as a 12-block
    one instead of going black when the camera backs off.
    """
    def area(name, loc, size, aim, scale=1.0):
        bpy.ops.object.light_add(type="AREA", location=loc)
        lt = bpy.context.object
        lt.name = name
        lt.data.size = size
        dist = math.dist(loc, aim)
        lt.data.energy = 72.0 * dist * dist * scale
        v = mathutils.Vector(aim) - mathutils.Vector(loc)
        lt.rotation_euler = v.to_track_quat("-Z", "Y").to_euler()
        # A light is geometry to Cycles. Leave this out and there is a glowing white
        # rectangle hanging in the shot - it has happened, on camera.
        lt.visible_camera = False
        return lt

    s = max(tower_h, 4.0)
    area("Key", (s * 0.75, -s * 0.85, s * 1.15), s * 0.55, (0, 0, s * 0.45), 1.0)
    area("Fill", (-s * 0.95, -s * 0.45, s * 0.55), s * 0.5, (0, 0, s * 0.35), 0.30)
    # The tower falls toward +X and the debris ends up spread across the floor there. This
    # is the light that stops the lower third of the frame from being a black band.
    area("Landing", (s * 0.55, -s * 0.30, s * 0.42), s * 0.8, (s * 0.55, 0, 0), 0.45)
    # A rim from behind separates the tower from a near-black world.
    area("Rim", (-s * 0.25, s * 1.1, s * 0.95), s * 0.45, (0, 0, s * 0.55), 0.35)


def build_label(cam):
    """Sweep label, parented into camera space so it cannot leave the frame.

    Parented through the data API, which leaves the parent inverse as IDENTITY - the
    location below really is camera space. Setting matrix_parent_inverse to the camera's
    inverse (the reflex from object parenting) turns it back into world coordinates and
    the text lands somewhere off-scene. This matters more here than usual because this
    camera moves the whole time.
    """
    bpy.ops.object.text_add(location=(0, 0, 0))
    txt = bpy.context.object
    txt.data.body = LABEL
    txt.data.align_x = "CENTER"
    txt.data.align_y = "CENTER"
    txt.data.size = 0.20
    txt.data.extrude = 0.004
    txt.data.materials.append(mat("LabelMat", (1, 1, 1, 1), rough=0.9, emit=1.4))
    txt.parent = cam
    txt.rotation_euler = (0, 0, 0)
    # Frame half-height 2.6 m in front of the lens is 36/lens*2.6/2; 66% of that keeps the
    # text clear of the top edge once its own height is counted.
    txt.location = (0.0, (36.0 / LENS * 2.6 / 2.0) * 0.66, -2.6)
    return txt


# -------------------------------------------------------------------------------- main


def main():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    sc = bpy.context.scene
    sc.render.engine = "CYCLES"        # headless EEVEE has no GPU context: ~19s/frame
    sc.cycles.samples = SAMPLES
    sc.cycles.use_denoising = True
    print("CYCLES_DEVICE " + use_gpu(sc))
    sc.render.resolution_x, sc.render.resolution_y = RES_X, RES_Y
    sc.render.fps = FPS
    frame_end = max(60, int(SECONDS * FPS))
    sc.frame_start = 1
    sc.frame_end = frame_end
    if MAT["trans"]:
        # 234 transmissive cubes with the default bounce budget is minutes per frame and
        # still noisy. Four transmission bounces is enough to read as glass in a stack.
        sc.cycles.max_bounces = 6
        sc.cycles.transmission_bounces = 4
    # Linear keys, set BEFORE the first insert: there is a key on every frame of the swing
    # and of the camera move, and the easing is already baked into the positions, so bezier
    # handles would only add overshoot between them. Blender 5.2 put Action.fcurves behind
    # slotted actions, so this can no longer be corrected after the fact.
    bpy.context.preferences.edit.keyframe_new_interpolation_type = "LINEAR"

    sc.world = bpy.data.worlds.new("W")
    sc.world.use_nodes = True
    sc.world.node_tree.nodes["Background"].inputs[0].default_value = (0.014, 0.014, 0.018, 1)

    steel = mat("Steel", STEEL, rough=0.22, metal=1.0)
    rig_mat = mat("Rig", RIG, rough=0.5, metal=0.85)
    build_floor()
    blocks, tower_w, tower_d, tower_h, block_mass = build_tower()
    rest = [tuple(b.location) for b in blocks]
    mass = block_mass * MASS_RATIO
    hit_z = STRIKE_H * tower_h

    if STRIKER == "ram":
        striker, release, rig_pts, info = striker_ram(
            tower_w, tower_d, hit_z, mass, steel, rig_mat)
    elif STRIKER == "chute":
        striker, release, rig_pts, info = striker_chute(
            tower_w, tower_d, tower_h, mass, steel, rig_mat)
    elif STRIKER == "weight":
        striker, release, rig_pts, info = striker_weight(
            tower_w, tower_d, tower_h, mass, steel, rig_mat)
    else:
        striker, release, rig_pts, info = striker_ball(
            tower_w, tower_h, hit_z, mass, steel, rig_mat)
    striker_size = info["size"]

    rw = sc.rigidbody_world
    rw.point_cache.frame_end = frame_end
    # 12 substeps / 20 iterations: below that a 25-block-mass striker at 9 m/s tunnels
    # straight through a 0.26 m block instead of hitting it.
    rw.substeps_per_frame = 12
    rw.solver_iterations = 20

    boxes, striker_pos, landings, strike_frame = prepass(
        sc, blocks, striker, rest, frame_end)

    # The chain keeps following the ball after it is let go, using the positions the
    # simulation actually produced. It stretches by a few percent as the ball ploughs
    # forward and drops - a real one would too, and a chain frozen mid-swing while the ball
    # carries on is far more obvious.
    if info["kind"] == "ball":
        for f in range(release + 1, frame_end + 1):
            place_chain(info["links"], info["pivot"], striker_pos[f - 1], f, info["prev_q"])

    build_lights(tower_h)
    cam, cam_dist = solve_camera(sc, boxes, striker_pos, striker_size,
                                 [tuple(p) for p in rig_pts], frame_end)
    if LABEL:
        build_label(cam)

    events = events_from(landings, striker_pos, strike_frame)
    with open(f"{OUT}/impacts.json", "w", encoding="utf-8") as fh:
        json.dump({"fps": FPS, "events": events,
                   "strike_frame": strike_frame,
                   "material": MATERIAL, "striker": info["kind"],
                   "pieces": len(blocks),
                   "tower": {"width": round(tower_w, 3), "height": round(tower_h, 3),
                             "blocks_tall": BLOCKS_TALL, "cols": COLS},
                   "impact_speed": round(info["speed"], 2)}, fh, indent=1)

    tail = (f"pieces={len(blocks)} tower={tower_w:.2f}x{tower_h:.2f}m "
            f"striker={info['kind']} v={info['speed']:.1f}m/s mass={mass:.0f}kg "
            f"strike={strike_frame} events={len(events)} cam_dist={cam_dist:.1f}"
            + (" speed_clamped_by_swing" if info.get("clamped") else ""))

    if PREVIEW:
        n = max(1, min(frame_end, PREVIEW))
        sc.frame_set(n)
        sc.render.image_settings.file_format = "PNG"
        sc.render.filepath = f"{OUT}/preview.png"
        bpy.ops.render.render(write_still=True)
        print(f"SCENE_OK preview={n} {tail}")
        return

    sc.render.image_settings.file_format = "PNG"
    sc.render.image_settings.color_mode = "RGB"
    sc.render.filepath = f"{OUT}/frame_"
    bpy.ops.render.render(animation=True)
    print(f"SCENE_OK frames={frame_end} {tail}")


if bpy is not None and __name__ == "__main__":
    main()
