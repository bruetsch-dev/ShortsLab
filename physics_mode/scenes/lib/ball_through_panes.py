"""A heavy sphere punches through a row of standing panes, one pane per impact.

    blender -b -noaudio -P ball_through_panes.py -- '{"pane_count": 4, "out_dir": "..."}'

This is a hand-maintained, PARAMETRIC scene. Nothing here is generated per run: a model
picks the scene and fills in PARAMS, it never writes geometry.

THE SHOT, and why it is built this way - all of it measured, not guessed:

  The panes stand in a row along +X and the camera looks DOWN that row from a shallow
  angle, so the row recedes INTO the frame instead of spanning it. A side-on view of the
  same row was solved first: at 9:16 the horizontal extent of four panes pushes the camera
  back to 16m and the nearest pane then covers 22% of the frame height - a correct
  simulation that looks cheap. Down the row the same panes cover 48%.

  The ball travels -X, i.e. TOWARDS the lens. The far pane is hit first and is small in
  frame; each following pane is nearer and bigger, so the impacts escalate and the last
  one - the largest object in the shot - is the payoff. Reverse the direction and the
  video peaks in its first half second.

  Panes break one at a time because every piece starts DEACTIVATED. A sleeping rigid body
  only wakes when something touches it, so the panes further down the row stand perfectly
  still while the current one is being destroyed. That single flag is what turns one
  collision into a sequence of separate, individually audible impacts.

  The seam between pieces is a BEVEL in the shared piece mesh, not a gap. A gap wide
  enough to see (the trick the wrecking-ball scene uses for its bricks) also breaks
  contact between neighbours, and then the wake cannot propagate: the ball punches a clean
  hole and the rest of the pane hangs in mid-air, unbroken and floating. Here the pieces
  touch, and the chamfer catches a highlight to draw the seam instead.

IMPACT TIMING is read back out of the baked simulation - the depsgraph is stepped frame by
frame and the ball's real position is compared against each pane - never predicted from
the launch speed. A sound gets placed on the frame this file names.
"""

import json
import math
import random
import sys

import bpy
import mathutils

# --------------------------------------------------------------------------- parameters
# Every value the scene accepts, with the note a model reads to choose it. This dict is
# also the only place a default is written down, so the documentation cannot drift from
# the code (see `_p` below).

PARAMS = {
    # ---- render contract, filled in by the runner
    "out_dir": {"default": "//out",
                "note": "where frames, preview.png and impacts.json are written"},
    "res_x": {"default": 1080, "range": [270, 2160], "note": "render width in pixels"},
    "res_y": {"default": 1920, "range": [480, 3840],
              "note": "render height in pixels; 1080x1920 is the vertical 9:16 target"},
    "fps": {"default": 30, "range": [24, 60], "note": "frames per second"},
    "seconds": {"default": 5.0, "range": [2.5, 12.0],
                "note": "clip length. The impacts all land inside the first ~40% of it; "
                        "the rest is the debris falling and settling, which is the part "
                        "people actually watch to the end"},
    "samples": {"default": 24, "range": [8, 128], "note": "Cycles samples per pixel"},
    "preview_frame": {"default": 0, "range": [0, 900],
                      "note": "0 renders the animation. Above 0 the simulation is stepped "
                              "up to that frame and that frame alone is rendered to "
                              "preview.png, for checking framing before committing"},
    "label": {"default": "",
              "note": "optional text burnt into the top of the frame, e.g. '120 kg'. "
                      "Empty means no text object is created at all"},

    # ---- the panes
    "pane_count": {"default": 4, "range": [2, 8],
                   "note": "how many panes stand in the row. Each one is its own impact, "
                           "so this is also how many sounds the clip will carry. The whole "
                           "row must fit the frame, so a long row shrinks every pane: "
                           "measured, (pane_count-1)*pane_spacing beyond about three times "
                           "pane_height drops the nearest pane under a third of the frame "
                           "height. Four panes 1.5m apart against 2.6m panes leaves the "
                           "nearest one filling 44%"},
    "pane_spacing": {"default": 1.5, "range": [0.8, 3.0],
                     "note": "metres between panes - it costs frame the same way "
                             "pane_count does. Smaller values make the impacts follow each "
                             "other faster; below about 1.0 the debris of one pane is still "
                             "in the air when the next one is hit"},
    "pane_width": {"default": 1.5, "range": [0.8, 2.6], "note": "pane width in metres"},
    "pane_height": {"default": 2.6, "range": [1.4, 3.4],
                    "note": "pane height in metres. This is what the camera fills the "
                            "frame with, so taller panes give a bigger, fuller shot"},
    "pane_thickness": {"default": 0.09, "range": [0.04, 0.30],
                       "note": "pane thickness in metres. Thick panes stop the ball sooner"},
    "pane_pieces": {"default": 260, "range": [80, 600],
                    "note": "pieces EACH pane shatters into - a grid of separate rigid "
                            "bodies, because Blender 5.2 has no fracture add-on. Below "
                            "about 150 the break reads as a wall of bricks, not as a pane "
                            "coming apart"},
    "irregular": {"default": 0.32, "range": [0.0, 0.7],
                  "note": "how uneven the piece grid is. 0 is a perfect lattice; 0.3 gives "
                          "shards of visibly different sizes, which is what real panes do"},
    "material": {"choices": ["glass", "ice", "concrete", "wood", "brick"],
                 "default": "glass",
                 "note": "what the panes are made of: sets colour, roughness, how "
                         "see-through they are, piece weight, friction and bounce. glass "
                         "and ice are partly transparent, so the far panes stay visible "
                         "through the near ones"},
    "piece_mass": {"default": 0.0, "range": [0.0, 5.0],
                   "note": "kilograms per piece; 0 uses the material's own value. "
                           "Deliberately far below real glass: a grid of loose boxes has "
                           "no bonds to absorb the hit, so true per-piece mass stops the "
                           "ball dead in the first pane instead of letting it through"},
    "frame_rig": {"choices": [True, False], "default": True,
                  "note": "steel frames around each pane. They are immovable, so they are "
                          "still standing after the glass is gone, which keeps the shot "
                          "readable once the row is destroyed"},

    # ---- the sphere
    "ball_mass": {"default": 120.0, "range": [10.0, 600.0],
                  "note": "kilograms. Must stay well above one pane's total piece weight "
                          "or the ball stops partway down the row"},
    "ball_speed": {"default": 18.0, "range": [6.0, 45.0],
                   "note": "metres per second at release - real physical speed, not screen "
                           "speed. What the viewer sees is this multiplied by slowmo"},
    "ball_radius": {"default": 0.42, "range": [0.20, 0.80],
                    "note": "metres. A bigger ball punches a wider hole and takes more of "
                            "the pane with it, but must still fit inside the frame rig"},
    "hit_height": {"default": 0.55, "range": [0.30, 0.80],
                   "note": "where the ball crosses each pane, as a fraction of the pane "
                           "height. 0.55 is just above centre, so the pane tears apart "
                           "rather than tipping over"},

    # ---- simulation
    "slowmo": {"default": 0.30, "range": [0.08, 1.0],
               "note": "simulation speed. 0.3 runs the physics at 30% of real time while "
                       "frames advance normally - true slow motion, not a frame trick. "
                       "This is the single biggest lever on how satisfying it looks"},
    "substeps": {"default": 40, "range": [10, 120],
                 "note": "solver substeps per frame. High, because the ball crosses a thin "
                         "pane in a fraction of one frame and a coarse solver steps it "
                         "straight through without ever registering a collision"},
    "seed": {"default": 7, "range": [0, 100000],
             "note": "seed for the piece-grid jitter; same seed gives the same break"},

    # ---- camera
    "lens": {"default": 42.0, "range": [24.0, 85.0],
             "note": "focal length in mm. Short lenses exaggerate the corridor of panes, "
                     "long ones flatten them onto each other"},
    "cam_yaw_deg": {"default": -22.0, "range": [-45.0, -8.0],
                    "note": "how far the camera swings off the row axis. Near 0 the panes "
                            "hide behind each other; past -35 the row spans the frame "
                            "width and every pane shrinks. -22 keeps all of them separate"},
    "cam_pitch_deg": {"default": 11.0, "range": [2.0, 30.0],
                      "note": "downward tilt. Slight, never overhead - looking down on a "
                              "collision flattens it"},
    "cam_push": {"default": 0.05, "range": [0.0, 0.25],
                 "note": "slow push-in across the clip, as a fraction of the solved "
                         "distance. The camera ENDS at the solved framing and starts "
                         "wider, so a push can never crop past what was framed"},
    "frame_fit": {"default": 0.92, "range": [0.75, 0.99],
                  "note": "how much of the frame the solved framing may use. What is left "
                          "over is the margin flying debris travels into"},
}

argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
P = json.loads(argv[0]) if argv else {}


def _p(key, cast=None):
    """Parameter value, defaulting to the documented default in PARAMS."""
    v = P.get(key, PARAMS[key]["default"])
    return cast(v) if cast else v


OUT = _p("out_dir")
RES_X, RES_Y = _p("res_x", int), _p("res_y", int)
FPS = _p("fps", int)
SECONDS = _p("seconds", float)
SAMPLES = _p("samples", int)
PREVIEW = _p("preview_frame", int)
LABEL = str(_p("label"))

N_PANES = max(2, _p("pane_count", int))
SPACING = _p("pane_spacing", float)
PANE_W = _p("pane_width", float)
PANE_H = _p("pane_height", float)
PANE_T = _p("pane_thickness", float)
PIECES = max(24, _p("pane_pieces", int))
IRREGULAR = _p("irregular", float)
MATERIAL = str(_p("material"))
PIECE_MASS = _p("piece_mass", float)
FRAME_RIG = bool(_p("frame_rig"))

BALL_MASS = _p("ball_mass", float)
BALL_SPEED = _p("ball_speed", float)
BALL_R = _p("ball_radius", float)
HIT_FRAC = _p("hit_height", float)

SLOWMO = max(0.02, _p("slowmo", float))
SUBSTEPS = _p("substeps", int)
SEED = _p("seed", int)

LENS = _p("lens", float)
YAW = _p("cam_yaw_deg", float)
PITCH = _p("cam_pitch_deg", float)
PUSH = _p("cam_push", float)
FIT = _p("frame_fit", float)

BAR = 0.12           # steel frame bar thickness; the pane is inset inside it
BAR_D = 0.16         # frame depth along the row axis
LAUNCH_FRAMES = 12   # kinematic run-in before the ball is released
RELEASE_GAP = 0.6    # metres of free flight between release and the first pane

# Per-material look and feel. `mass` is PER PIECE and is the number that decides whether
# the ball makes it down the row at all - see the piece_mass note in PARAMS.
MATERIALS = {
    "glass":    dict(color=(0.70, 0.84, 0.92), rough=0.05, metal=0.0, alpha=0.40,
                     mass=0.30, friction=0.25, restitution=0.20),
    "ice":      dict(color=(0.78, 0.90, 0.96), rough=0.16, metal=0.0, alpha=0.55,
                     mass=0.35, friction=0.08, restitution=0.15),
    "concrete": dict(color=(0.50, 0.50, 0.48), rough=0.88, metal=0.0, alpha=1.0,
                     mass=0.90, friction=0.90, restitution=0.02),
    "wood":     dict(color=(0.44, 0.27, 0.13), rough=0.62, metal=0.0, alpha=1.0,
                     mass=0.45, friction=0.70, restitution=0.15),
    "brick":    dict(color=(0.54, 0.22, 0.16), rough=0.82, metal=0.0, alpha=1.0,
                     mass=0.80, friction=0.95, restitution=0.03),
}
MAT = MATERIALS.get(MATERIAL, MATERIALS["glass"])


# ------------------------------------------------------------------------------- helpers


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


def mat(name, rgb, rough=0.6, metal=0.0, alpha=1.0):
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    b = m.node_tree.nodes["Principled BSDF"]
    b.inputs["Base Color"].default_value = (*rgb, 1.0)
    b.inputs["Roughness"].default_value = rough
    b.inputs["Metallic"].default_value = metal
    if alpha < 1.0:
        # Alpha, not a Glass BSDF. Transmission at 24 samples is a noise generator; an
        # alpha-blended pane with low roughness still catches every specular highlight off
        # the backdrop, and that is what actually makes glass read as glass.
        b.inputs["Alpha"].default_value = alpha
    return m


# --- vector maths, kept local so the camera solve never depends on the depsgraph -------

def sub(a, b): return (a[0] - b[0], a[1] - b[1], a[2] - b[2])
def add(a, b): return (a[0] + b[0], a[1] + b[1], a[2] + b[2])
def mul(a, s): return (a[0] * s, a[1] * s, a[2] * s)
def dot(a, b): return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0])


def norm(a):
    n = math.sqrt(dot(a, a)) or 1.0
    return (a[0] / n, a[1] / n, a[2] / n)


def frustum_tans(lens, res_x, res_y):
    """Half-angle tangents as (width, height).

    Blender fits the 36mm sensor to the LARGER resolution axis, so in 9:16 the sensor maps
    to the frame HEIGHT and the visible height at distance d is 36/lens*d. Every framing
    bug in this project traces back to assuming it maps to the width.
    """
    big = 18.0 / lens
    if res_y >= res_x:
        return big * res_x / res_y, big
    return big, big * res_y / res_x


def cam_basis(yaw_deg, pitch_deg):
    """Forward (the camera's -Z), right (+X) and up (+Y) for a yaw/pitch aim."""
    y, p = math.radians(yaw_deg), math.radians(pitch_deg)
    f = norm((math.cos(p) * math.cos(y), math.cos(p) * math.sin(y), -math.sin(p)))
    r = norm(cross(f, (0.0, 0.0, 1.0)))
    u = norm(cross(r, f))
    return f, r, u


def project(p, cam_loc, basis, tans):
    """World point -> screen coordinates in [-1, 1]. Outside that box is out of frame."""
    f, r, u = basis
    tw, th = tans
    v = sub(p, cam_loc)
    fwd = dot(v, f)
    if fwd <= 1e-4:
        return (9.9, 9.9)                       # behind the camera
    return (dot(v, r) / fwd / tw, dot(v, u) / fwd / th)


def solve_distance(points, aim, basis, tans, fit):
    """Smallest distance back along the view axis that still holds every point in frame.

    Solved, never hard-coded. Pulling the camera back shrinks every point's screen offset
    monotonically, so a bisection finds the tightest framing that keeps the whole row -
    and the ball at both ends of its flight - inside `fit` of the frame.
    """
    f = basis[0]

    def fits(d):
        c = sub(aim, mul(f, d))
        for p in points:
            sx, sy = project(p, c, basis, tans)
            if abs(sx) > fit or abs(sy) > fit:
                return False
        return True

    hi = 2.0
    for _ in range(60):
        if fits(hi):
            break
        hi *= 1.5
    lo = 0.0
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        if fits(mid):
            hi = mid
        else:
            lo = mid
    return hi


def cuts(count, total, jitter, rng):
    """`count` consecutive spans covering `total`, each within +-jitter of the average."""
    raw = [rng.uniform(1.0 - jitter, 1.0 + jitter) for _ in range(count)]
    scale = total / sum(raw)
    out, acc = [0.0], 0.0
    for r in raw:
        acc += r * scale
        out.append(acc)
    out[-1] = total
    return out


def unit_cube(name, bevel):
    """A 1x1x1 cube mesh, chamfered, meant to be SHARED by many objects.

    Shared on purpose: a thousand pieces built with primitive_cube_add are a thousand
    meshes and a thousand operator round-trips. Per-piece size comes from object SCALE
    instead, which the BOX collision shape honours, so the solver still sees the real
    dimensions of every piece.

    create_cube(size=1) spans ONE unit corner to corner, so an object scale of (a, b, c)
    IS the piece's size in metres. Halving the scale halves the piece, it does not set it.

    The chamfer is the visible seam. Without it a wall of touching boxes renders as one
    smooth slab and the destruction reads as a glitch rather than as a collapse.
    """
    import bmesh
    bm = bmesh.new()
    bmesh.ops.create_cube(bm, size=1.0)
    if bevel > 0:
        try:
            bmesh.ops.bevel(bm, geom=bm.verts[:] + bm.edges[:] + bm.faces[:],
                            offset=bevel, segments=2, affect="EDGES", clamp_overlap=True)
        except (TypeError, RuntimeError) as exc:   # op signature moved: seams go flat
            print(f"WARN piece bevel unavailable ({exc}); pieces get hard edges")
    me = bpy.data.meshes.new(name)
    bm.to_mesh(me)
    bm.free()
    return me


def rigid(obj, kind="ACTIVE"):
    """Add a rigid body through the operator - the only path this project trusts.

    That is one operator call per piece, around a thousand of them. Measured against the
    render they are noise, and linking objects into the rigid body collection by hand does
    not reliably populate obj.rigid_body on every build.
    """
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    bpy.ops.rigidbody.object_add(type=kind)
    obj.select_set(False)
    return obj.rigid_body


def box(name, mesh, loc, scale, material):
    obj = bpy.data.objects.new(name, mesh)
    obj.location = loc
    obj.scale = scale
    if material and not mesh.materials:
        mesh.materials.append(material)
    bpy.context.scene.collection.objects.link(obj)
    return obj


def backdrop_material(half_height):
    """Emissive vertical gradient. This is why no third of the frame is empty and dark.

    A lit wall behind the row also silhouettes the pieces and gives the glass something to
    reflect - against a black world the panes were nearly invisible until they broke.
    Pure emission, so it costs no extra samples and cannot go noisy.
    """
    m = bpy.data.materials.new("Backdrop")
    m.use_nodes = True
    try:
        nt = m.node_tree
        nt.nodes.clear()
        out = nt.nodes.new("ShaderNodeOutputMaterial")
        emi = nt.nodes.new("ShaderNodeEmission")
        ramp = nt.nodes.new("ShaderNodeValToRGB")
        rng = nt.nodes.new("ShaderNodeMapRange")
        sep = nt.nodes.new("ShaderNodeSeparateXYZ")
        tex = nt.nodes.new("ShaderNodeTexCoord")
        # Object coordinates: the plane's local +Y is its screen-up axis once it has been
        # rotated to face the camera, so the ramp runs bottom-to-top of frame.
        nt.links.new(tex.outputs["Object"], sep.inputs["Vector"])
        nt.links.new(sep.outputs["Y"], rng.inputs["Value"])
        rng.inputs["From Min"].default_value = -half_height
        rng.inputs["From Max"].default_value = half_height
        nt.links.new(rng.outputs["Result"], ramp.inputs["Fac"])
        ramp.color_ramp.elements[0].position = 0.0
        ramp.color_ramp.elements[0].color = (0.30, 0.34, 0.42, 1.0)     # lit horizon
        ramp.color_ramp.elements[1].position = 1.0
        ramp.color_ramp.elements[1].color = (0.05, 0.06, 0.08, 1.0)     # dark at the top
        nt.links.new(ramp.outputs["Color"], emi.inputs["Color"])
        emi.inputs["Strength"].default_value = 1.0
        nt.links.new(emi.outputs["Emission"], out.inputs["Surface"])
        return m
    except Exception as exc:  # noqa: BLE001 - node names moved; a flat panel still works
        print(f"WARN backdrop gradient failed ({exc}); using a flat panel")
        bpy.data.materials.remove(m)
        return mat("BackdropFlat", (0.13, 0.15, 0.19), rough=0.9)


# ---------------------------------------------------------------------------------- main


def main():
    rng = random.Random(SEED)
    bpy.ops.wm.read_factory_settings(use_empty=True)
    sc = bpy.context.scene
    sc.render.engine = "CYCLES"      # headless EEVEE has no GPU context: ~19s/frame
    sc.cycles.samples = SAMPLES
    sc.cycles.use_denoising = True
    print("CYCLES_DEVICE " + use_gpu(sc))
    sc.render.resolution_x, sc.render.resolution_y = RES_X, RES_Y
    sc.render.fps = FPS
    sc.frame_start = 1
    sc.frame_end = max(2, int(SECONDS * FPS))
    preview_at = min(PREVIEW, sc.frame_end) if PREVIEW else 0
    # Set BEFORE the first key is inserted. Blender 5.2 moved Action.fcurves behind
    # slotted actions, so interpolation can no longer be repaired after the fact.
    bpy.context.preferences.edit.keyframe_new_interpolation_type = "LINEAR"

    sc.world = bpy.data.worlds.new("W")
    sc.world.use_nodes = True
    sc.world.node_tree.nodes["Background"].inputs[0].default_value = (0.01, 0.012, 0.016, 1)

    # ---- layout, all metres. Pane i sits at x = i*spacing and the ball travels -X, so it
    # meets pane n-1 first and pane 0 - nearest to the lens, biggest in frame - last.
    pane_x = [i * SPACING for i in range(N_PANES)]
    far_x = pane_x[-1]
    total_h = PANE_H + 2 * BAR                    # frame outer height, standing on the floor
    # Clamped so an extreme radius/height combination cannot bury the ball in the floor.
    hit_z = max(BAR + PANE_H * HIT_FRAC, BALL_R + 0.15)
    # A ball wider than the frame opening would smash into immovable steel and stop dead,
    # so the rig steps aside rather than silently ruining the run.
    rig_on = FRAME_RIG and (2 * BALL_R < PANE_W * 0.92)
    if FRAME_RIG and not rig_on:
        print("WARN ball is too wide for the frame opening; frame rig disabled")
    if 2 * BALL_R > PANE_W:
        print("WARN ball is wider than the pane: it will shove panes aside, not pierce them")

    # ---- camera solved from the geometry --------------------------------------------
    # Key points: every corner of every pane frame, the ball where it is released, and the
    # ball one overrun past the last pane. They are built from the same numbers the
    # geometry is built from, so the solve cannot disagree with what is in the scene.
    key_points = []
    for px in pane_x:
        for dx in (-BAR_D / 2, BAR_D / 2):
            for dy in (-(PANE_W / 2 + BAR), PANE_W / 2 + BAR):
                for dz in (0.0, total_h):
                    key_points.append((px + dx, dy, dz))
    release_x = far_x + RELEASE_GAP + BALL_R
    overrun = 1.2
    key_points += [(release_x + BALL_R, 0.0, hit_z + BALL_R),
                   (release_x + BALL_R, 0.0, hit_z - BALL_R),
                   (-overrun, 0.0, hit_z + BALL_R),
                   (-overrun, 0.0, max(0.0, hit_z - BALL_R - 0.3))]

    basis = cam_basis(YAW, PITCH)
    tans = frustum_tans(LENS, RES_X, RES_Y)
    xs = [p[0] for p in key_points]
    ys = [p[1] for p in key_points]
    zs = [p[2] for p in key_points]
    aim = ((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2, (min(zs) + max(zs)) / 2)
    dist = solve_distance(key_points, aim, basis, tans, FIT)
    cam_end = sub(aim, mul(basis[0], dist))                    # tightest, at the last frame
    cam_start = sub(aim, mul(basis[0], dist * (1.0 + PUSH)))   # wider, at the first frame

    bpy.ops.object.camera_add(location=cam_start)
    cam = bpy.context.object
    cam.data.lens = LENS
    # Aimed by hand rather than with a track constraint, so the analytic projection used
    # for the framing check and the transform Blender renders with are the same thing.
    cam.rotation_euler = mathutils.Vector(
        sub(aim, cam_start)).to_track_quat("-Z", "Y").to_euler()
    sc.camera = cam
    if PUSH > 0.0:
        cam.location = cam_start
        cam.keyframe_insert("location", frame=1)
        cam.location = cam_end
        cam.keyframe_insert("location", frame=sc.frame_end)
    scale_ref = dist / 9.0        # lights are sized and powered relative to the solve

    # ---- floor -----------------------------------------------------------------------
    bpy.ops.mesh.primitive_plane_add(size=160, location=(0, 0, 0))
    floor = bpy.context.object
    floor.name = "Floor"
    # Dark but polished: the backdrop and the lights reflect in it, so the bottom of the
    # frame - where all the debris ends up - carries light instead of going to black.
    floor.data.materials.append(mat("Floor", (0.045, 0.047, 0.052), rough=0.28))
    body = rigid(floor, "PASSIVE")
    body.friction = 0.9
    body.restitution = 0.05

    # ---- backdrop, sized from the frustum so it always covers the whole frame ---------
    back_dist = dist + far_x + 10.0
    span = 2.0 * max(tans[0], tans[1]) * back_dist * 1.3
    bpy.ops.mesh.primitive_plane_add(size=span, location=(0, 0, 0))
    backdrop = bpy.context.object
    backdrop.name = "Backdrop"
    backdrop.location = add(cam_end, mul(basis[0], back_dist))
    backdrop.rotation_euler = mathutils.Vector(
        mul(basis[0], -1.0)).to_track_quat("Z", "Y").to_euler()
    backdrop.data.materials.append(backdrop_material(span / 2.0))
    backdrop.visible_shadow = False

    # ---- panes -----------------------------------------------------------------------
    pane_mat = mat(f"Pane_{MATERIAL}", MAT["color"], rough=MAT["rough"],
                   metal=MAT["metal"], alpha=MAT["alpha"])
    steel = mat("Steel", (0.30, 0.31, 0.34), rough=0.35, metal=1.0)

    # Roughly square pieces: the column count is solved from the pane's aspect ratio
    # rather than picked, so pane_pieces means the same thing at any pane size.
    cols = max(3, int(round(math.sqrt(max(1.0, PIECES * PANE_W / PANE_H)))))
    rows = max(3, int(round(PIECES / cols)))
    piece_total = cols * rows * N_PANES
    piece_mass = PIECE_MASS if PIECE_MASS > 0 else MAT["mass"]
    avg_cell = (PANE_W / cols) * (PANE_H / rows)
    # 5% of the unit cube: at 1080 wide a 12cm piece then carries a ~2px chamfer, enough
    # to catch a highlight, not enough to stop the piece reading as a solid slab.
    piece_mesh = unit_cube("Piece", 0.05)
    piece_mesh.materials.append(pane_mat)
    rig_mesh = None
    if rig_on:
        rig_mesh = unit_cube("RigBar", 0.02)
        rig_mesh.materials.append(steel)

    pane_watch = []       # per pane: a sample of pieces, watched for the break frame
    for i, px in enumerate(pane_x):
        ys_cut = cuts(cols, PANE_W, IRREGULAR, rng)
        zs_cut = cuts(rows, PANE_H, IRREGULAR, rng)
        watched = []
        for ci in range(cols):
            y0, y1 = ys_cut[ci], ys_cut[ci + 1]
            for ri in range(rows):
                z0, z1 = zs_cut[ri], zs_cut[ri + 1]
                sy, sz = (y1 - y0), (z1 - z0)
                loc = (px, -PANE_W / 2 + (y0 + y1) / 2, BAR + (z0 + z1) / 2)
                # 1% shy of its cell: pieces still touch inside the collision margin set
                # below - so a wake propagates through the whole pane - while no two of
                # them render z-fighting against each other.
                obj = box(f"P{i}_{ci}_{ri}", piece_mesh, loc,
                          (PANE_T, sy * 0.99, sz * 0.99), pane_mat)
                rb = rigid(obj, "ACTIVE")
                rb.mass = max(0.01, piece_mass * (sy * sz) / avg_cell)
                rb.friction = MAT["friction"]
                rb.restitution = MAT["restitution"]
                rb.collision_shape = "BOX"
                rb.use_margin = True
                rb.collision_margin = 0.006     # keeps neighbours in contact
                # ASLEEP until something hits it. This is what makes each pane its own
                # event: the panes further down the row do not stir, do not settle and do
                # not jitter themselves apart while the current one is exploding.
                rb.use_deactivation = True
                rb.use_start_deactivated = True
                if (ci * rows + ri) % 7 == 0:
                    watched.append((obj, mathutils.Vector(loc)))
        pane_watch.append(watched)

        if rig_on:
            half = PANE_W / 2 + BAR / 2
            bars = [((px, -half, total_h / 2), (BAR_D, BAR, total_h)),      # left post
                    ((px, half, total_h / 2), (BAR_D, BAR, total_h)),       # right post
                    ((px, 0.0, BAR / 2), (BAR_D, PANE_W + 2 * BAR, BAR)),   # bottom rail
                    ((px, 0.0, total_h - BAR / 2), (BAR_D, PANE_W + 2 * BAR, BAR))]
            for bi, (loc, scale) in enumerate(bars):
                obj = box(f"Rig{i}_{bi}", rig_mesh, loc, scale, steel)
                rb = rigid(obj, "PASSIVE")      # immovable: the rig survives the hit
                rb.friction = 0.8

    # ---- the sphere -------------------------------------------------------------------
    bpy.ops.mesh.primitive_uv_sphere_add(radius=BALL_R, segments=56, ring_count=28,
                                         location=(release_x, 0, hit_z))
    ball = bpy.context.object
    ball.name = "Ball"
    ball.data.materials.append(mat("BallSteel", (0.58, 0.59, 0.63), rough=0.14, metal=1.0))
    bpy.ops.object.shade_smooth()
    rb = rigid(ball, "ACTIVE")
    rb.mass = BALL_MASS
    rb.collision_shape = "SPHERE"
    rb.friction = 0.5
    rb.restitution = 0.05
    rb.use_deactivation = False          # never allowed to fall asleep mid-flight

    # The launch is a straight kinematic run-in and then a release. Blender derives the
    # released velocity from the last kinematic step, and that step is measured in SIM
    # time, which slowmo scales. So the per-frame displacement below is the SCREEN speed
    # (speed*slowmo) and the physical speed Bullet hands the ball is the full speed.
    screen_speed = BALL_SPEED * SLOWMO
    per_frame = screen_speed / FPS
    # Fired on a shallow arc, not flat. Over the traverse gravity pulls the ball down by
    # g*t^2/2, and flat it would be hitting the last pane near the floor. With vz = g*T/2
    # it crosses the first and the last pane at the same height. T is computed from 85% of
    # the launch speed because the panes really do slow it down on the way through.
    traverse = max(0.05, (far_x + RELEASE_GAP) / max(1.0, BALL_SPEED * 0.85))
    vz = 9.81 * traverse / 2.0
    # Metres of rise per metre travelled. The ball moves towards -X, so its height DROPS
    # with increasing x: getting this sign backwards launches it downwards and gravity
    # then drives it into the floor before the last pane.
    slope = vz / BALL_SPEED
    for f in range(1, LAUNCH_FRAMES + 1):
        x = release_x + (LAUNCH_FRAMES - f) * per_frame     # still to run before release
        ball.location = (x, 0.0, hit_z - slope * (x - far_x))
        ball.rigid_body.kinematic = True
        ball.keyframe_insert("location", frame=f)
        ball.keyframe_insert("rigid_body.kinematic", frame=f)
    ball.rigid_body.kinematic = False     # released carrying the animated velocity
    ball.keyframe_insert("rigid_body.kinematic", frame=LAUNCH_FRAMES + 1)

    # ---- lights ------------------------------------------------------------------------
    # Energy scales with the solved distance squared, so a two-pane shot and an eight-pane
    # shot come out at the same exposure instead of one of them blowing out.
    def area_light(name, loc, energy, size, aim_at):
        bpy.ops.object.light_add(type="AREA", location=loc)
        lt = bpy.context.object
        lt.name = name
        lt.data.energy = energy * scale_ref ** 2
        lt.data.size = size * scale_ref
        lt.rotation_euler = mathutils.Vector(
            sub(aim_at, loc)).to_track_quat("-Z", "Y").to_euler()
        # Lights are geometry to Cycles. Left visible, a 5m panel hangs in the top of the
        # frame as a white rectangle.
        lt.visible_camera = False
        return lt

    mid_x = far_x / 2.0
    # Key, high on the camera side of the row.
    area_light("Key", (mid_x + 1.0, 4.2 * scale_ref, 6.0 * scale_ref), 2400, 5.0,
               (mid_x, 0, hit_z))
    # Rim from behind the row: rakes the piece edges so the shards glint as they tumble.
    area_light("Rim", (far_x + 2.0, -4.0 * scale_ref, 2.2 * scale_ref), 1400, 3.0,
               (mid_x, 0, hit_z))
    # The floor in front of the nearest pane is where most of the debris ends up, and an
    # unlit landing zone is the most common reason one of these frames looks half empty.
    area_light("Landing", (-1.5, 2.0 * scale_ref, 3.4 * scale_ref), 1100, 4.0,
               (-0.6, 0, 0))

    # ---- optional label, parented into camera space ------------------------------------
    if LABEL:
        bpy.ops.object.text_add(location=(0, 0, 0))
        txt = bpy.context.object
        txt.data.body = LABEL
        txt.data.align_x = "CENTER"
        txt.data.align_y = "CENTER"
        txt.data.size = 0.20
        txt.data.extrude = 0.004
        txt.data.materials.append(mat("Label", (1, 1, 1), rough=0.9))
        # IDENTITY parent inverse, so the location below really is camera space. Setting
        # matrix_parent_inverse to the camera's inverse - the reflex from object parenting
        # - turns these back into world coordinates and the label leaves the scene.
        txt.parent = cam
        txt.rotation_euler = (0, 0, 0)
        # Half the frame height 2.6m in front of the lens is 18/lens*2.6; 70% of that
        # keeps the text clear of both the top edge and the far pane.
        txt.location = (0.0, (18.0 / LENS * 2.6) * 0.70, -2.6)

    # ---- rigid body world ---------------------------------------------------------------
    rw = sc.rigidbody_world
    rw.point_cache.frame_start = 1
    rw.point_cache.frame_end = sc.frame_end
    rw.substeps_per_frame = SUBSTEPS
    rw.solver_iterations = 24
    # True slow motion: the solver advances slowmo/fps seconds per frame. It also buys
    # collision accuracy - at 18 m/s with slowmo 0.3 the ball moves 18*(0.3/30)/40 = 4.5mm
    # per substep, far inside a 9cm pane, so it cannot step through one uncontacted.
    rw.time_scale = SLOWMO

    # ---- framing self-check --------------------------------------------------------------
    # Printed, not assumed. Every number in here has been wrong at least once.
    pane_fill = []
    for px in pane_x:
        pts = [(px + dx, dy, dz)
               for dx in (-BAR_D / 2, BAR_D / 2)
               for dy in (-(PANE_W / 2 + BAR), PANE_W / 2 + BAR)
               for dz in (0.0, total_h)]
        sxy = [project(p, cam_end, basis, tans) for p in pts]
        pane_fill.append((max(s[1] for s in sxy) - min(s[1] for s in sxy)) / 2.0)
    outside = sum(1 for p in key_points
                  if max(abs(v) for v in project(p, cam_end, basis, tans)) > 1.0)
    print(f"FRAMING dist={dist:.2f} cam=({cam_end[0]:.2f},{cam_end[1]:.2f},{cam_end[2]:.2f}) "
          f"near_pane_fill={pane_fill[0]:.2f} far_pane_fill={pane_fill[-1]:.2f} "
          f"pieces={piece_total} grid={cols}x{rows} hit_z={hit_z:.2f}")
    if outside:
        print(f"WARN {outside} framing points fall outside the frame")
    if pane_fill[0] < 0.30:
        # Swinging the camera does not rescue this: at eight panes 3m apart the fill only
        # moves from 0.13 to 0.22 across the whole legal yaw range. The row itself is too
        # long for a 9:16 frame and only the layout can fix it.
        print(f"WARN nearest pane covers only {pane_fill[0] * 100:.0f}% of the frame "
              f"height - the row is too long for 9:16. Cut pane_count or pane_spacing, or "
              f"raise pane_height; the camera cannot make this shot big")

    # ---- simulate ------------------------------------------------------------------------
    # Stepped frame by frame, in order: a rigid body cache is only valid if it is built
    # sequentially, and every impact frame below comes out of these positions - the ball's
    # real, post-collision motion - rather than out of the launch speed.
    events = []
    pending = list(range(N_PANES - 1, -1, -1))      # hit order: the far pane first
    broke = {}
    ball_floor = None
    prev_x = None
    measured = []
    free_frames = max(1, min(4, int(RELEASE_GAP / max(1e-6, per_frame))))
    last_frame = preview_at or sc.frame_end
    for f in range(1, last_frame + 1):
        sc.frame_set(f)
        dg = bpy.context.evaluated_depsgraph_get()
        pos = ball.evaluated_get(dg).matrix_world.translation
        bx, bz = pos.x, pos.z
        if prev_x is not None and f > LAUNCH_FRAMES + 1 and len(measured) < free_frames:
            measured.append(abs(bx - prev_x))
        # A while, not an if: at low slowmo and high speed the ball can clear two panes
        # inside one frame, and both of them still have to be named.
        while pending and bx - BALL_R <= pane_x[pending[0]] + PANE_T / 2:
            i = pending.pop(0)                  # leading surface has reached its near face
            step = abs(bx - prev_x) if prev_x is not None else 0.0
            speed = step * FPS / SLOWMO                            # physical m/s
            # Strength blends how fast the ball still is with how big the pane is on
            # screen. The ball slows down the further it gets, and the near panes are the
            # ones that fill the frame - which is exactly how they should sound.
            near_gain = 0.62 + 0.38 * (pane_fill[i] / max(1e-3, pane_fill[0]))
            strength = max(0.15, min(1.0, (speed / max(1.0, BALL_SPEED)) * near_gain))
            events.append({"frame": f, "kind": "impact",
                           "strength": round(strength, 3),
                           "pane": i, "source": "pane", "material": MATERIAL,
                           "speed": round(speed, 2)})
            broke[i] = None
        # A pane counts as broken only once its own pieces have actually moved - proof
        # the contact did something, rather than the ball merely being in the right place.
        for i, watched in enumerate(pane_watch):
            if broke.get(i, "-") is None:
                for obj, home in watched:
                    if (obj.evaluated_get(dg).matrix_world.translation - home).length > 0.03:
                        broke[i] = f
                        break
        if ball_floor is None and f > LAUNCH_FRAMES and bz <= BALL_R + 0.03:
            ball_floor = f
        prev_x = bx

    if preview_at:
        # Simulated up to the preview frame above, never jumped to: a rigid body cache
        # that has not been stepped renders the scene in its start pose, so a preview of
        # frame 60 would show four intact panes and prove nothing at all.
        sc.render.image_settings.file_format = "PNG"
        sc.render.filepath = f"{OUT}/preview.png"
        bpy.ops.render.render(write_still=True)
        print(f"SCENE_OK preview={preview_at} panes={N_PANES} pieces={piece_total} "
              f"impacts={[e['frame'] for e in events]}")
        return

    if ball_floor:
        events.append({"frame": ball_floor, "kind": "impact", "strength": 0.45,
                       "source": "ball_floor", "material": "floor"})
    events.sort(key=lambda e: e["frame"])
    for e in events:
        if e.get("source") == "pane":
            e["broke_frame"] = broke.get(e["pane"])
            e["broke"] = broke.get(e["pane"]) is not None

    with open(f"{OUT}/impacts.json", "w", encoding="utf-8") as fh:
        json.dump({"fps": FPS, "events": events, "slowmo": SLOWMO,
                   "material": MATERIAL, "pane_count": N_PANES,
                   "pieces": piece_total}, fh, indent=1)

    # Diagnostics worth having in the log: if the released speed does not match what was
    # asked for, the kinematic handoff is wrong and every impact after it lands soft.
    if measured:
        avg = sum(measured) / len(measured) * FPS / SLOWMO
        print(f"BALL_SPEED asked={BALL_SPEED:.1f} measured={avg:.1f} m/s")
        if abs(avg - BALL_SPEED) > 0.35 * BALL_SPEED:
            print("WARN released speed differs from the launch speed by more than a third")
    missed = [e["pane"] for e in events if e.get("source") == "pane" and not e.get("broke")]
    if missed:
        print(f"WARN panes {missed} were reached but never came apart")
    hits = sum(1 for e in events if e.get("source") == "pane")
    if hits < N_PANES:
        print(f"WARN only {hits} of {N_PANES} panes were hit - the ball stopped short, or "
              f"the clip is too short to hold them all")

    sc.render.image_settings.file_format = "PNG"    # Blender 5.2 has no FFMPEG output
    sc.render.image_settings.color_mode = "RGB"
    sc.render.filepath = f"{OUT}/frame_"
    bpy.ops.render.render(animation=True)
    print(f"SCENE_OK material={MATERIAL} panes={N_PANES} pieces={piece_total} "
          f"frames={sc.frame_end} impacts={[e['frame'] for e in events]}")


main()
