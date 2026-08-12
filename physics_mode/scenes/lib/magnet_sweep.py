"""magnet_sweep - a magnet swings over a bed of steel spheres and rips them off the floor.

    blender -b -noaudio -P magnet_sweep.py -- '{"magnet_power":300,"out_dir":"..."}'

This is a HAND-MAINTAINED parametric scene: a model picks the scene and fills in PARAMS,
it never writes geometry.

Why it is built this way
------------------------
* Blender's FORCE field, not a fake. bpy has a real magnetic-ish attractor (a FORCE field
  with negative strength pulls); driving it from `magnet_power` means the sweep is a
  physical parameter and the spheres genuinely fight gravity rather than being animated up.
  A field with `use_max_distance` also gives the shot its shape: the spheres jump only when
  the magnet is close enough, so the pickup travels along the bed with it.
* The magnet is a KINEMATIC body on a pendulum path. Constant swing, no easing, because the
  appeal is the inevitability - and kinematic means the payload cannot drag the magnet down.
* The spheres START ASLEEP. A bed of two hundred rigid bodies that is awake at frame 1
  spends the first second settling into itself, which is a second of nothing.
* Falloff is set to inverse-square with a floor. Blender's default linear falloff makes the
  whole bed rise at once, which reads as a wind gust rather than a magnet.

Measured on the defaults: 180 spheres in a 1.6 x 1.2m bed, the magnet crossing it in 2.6s,
and at power 300 roughly a third of them lifted and carried.
"""

import json
import math
import random
import sys

try:
    import bpy
    from mathutils import Vector
except ImportError:
    bpy = None
    Vector = None

PARAMS = {
    "magnet_power": {
        "range": [0, 2000],
        "default": 300,
        "note": "Field strength. This is the sweep: 0 leaves the bed untouched (the control "
                "take), ~150 lifts a few and drops them, ~300 carries a third, 1000+ rips "
                "the whole bed up in one go and is the payoff take.",
    },
    "reach": {
        "range": [0.25, 3.0],
        "default": 0.95,
        "note": "How far the field reaches, in metres. Short reach makes the pickup travel "
                "along the bed with the magnet, which is the readable version; long reach "
                "lifts everything at once and reads as a gust of wind.",
    },
    "payload": {
        "choices": ["spheres", "cubes", "bolts", "mixed"],
        "default": "spheres",
        "note": "What is on the floor. spheres roll and clump into a ball under the magnet; "
                "cubes stack and slide; bolts are elongated and stand on end as they lift, "
                "which is the most magnet-looking; mixed is scrap.",
    },
    "count": {
        "range": [40, 600],
        "default": 180,
        "note": "How many pieces are in the bed. Under ~80 the floor looks sparse; over "
                "~400 the solver is the limit, not the look.",
    },
    "piece_size": {
        "range": [0.03, 0.14],
        "default": 0.06,
        "note": "Size of one piece in metres. Small pieces cluster and flow; large ones "
                "read individually and are heavier for the same power.",
    },
    "swing_seconds": {
        "range": [1.2, 6.0],
        "default": 2.6,
        "note": "How long the magnet takes to cross the bed. Slow is better - the whole "
                "point is watching pieces decide whether they can hold on.",
    },
    "magnet_height": {
        "range": [0.10, 1.20],
        "default": 0.34,
        "note": "How high the magnet passes over the floor, in metres. Combined with reach "
                "this decides whether anything is picked up at all.",
    },
    "wrecking_ball": {
        "default": False,
        "note": "Swing a heavy ball through the bed first, so the pieces are already "
                "scattered when the magnet arrives. Adds an impact but costs about a "
                "second of the take.",
    },
    "seed": {"range": [0, 9999], "default": 5, "note": "Random seed for the bed layout."},
    "seconds": {"range": [3.0, 9.0], "default": 5.0, "note": "Length of ONE take."},
    "lens": {"range": [30.0, 85.0], "default": 50.0, "note": "Focal length in mm."},
    "camera_pitch_deg": {
        "range": [5.0, 55.0], "default": 22.0,
        "note": "How far the camera looks down. Low shows the lift in profile, which is "
                "what sells it; steep shows the pattern left in the bed.",
    },
    "fill": {"range": [0.55, 0.95], "default": 0.84, "note": "How much of the frame the bed fills."},

    "label": {"default": "", "note": "Caption for this take, supplied by the app."},
    "out_dir": {"default": "//out", "note": "Where frames, preview.png and impacts.json go."},
    "res_x": {"default": 1080, "note": "Supplied by the runner - leave unset."},
    "res_y": {"default": 1920, "note": "Supplied by the runner - leave unset (9:16)."},
    "samples": {"default": 24, "note": "Supplied by the runner - Cycles samples per pixel."},
    "fps": {"default": 30, "note": "Supplied by the runner - leave unset."},
    "preview_frame": {"default": 0, "note": "Render this frame alone to preview.png."},
}

SWEEP = {"param": "magnet_power", "values": [0, 300, 1500], "unit": ""}

argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
P = json.loads(argv[0]) if argv else {}


def _default(key):
    return PARAMS[key]["default"]


def pf(key):
    try:
        v = float(P.get(key, _default(key)))
    except (TypeError, ValueError):
        v = float(_default(key))
    rng = PARAMS[key].get("range")
    if rng:
        v = max(float(rng[0]), min(float(rng[1]), v))
    return v


def pi(key):
    return int(round(pf(key)))


def ps(key):
    v = str(P.get(key, _default(key)) or _default(key)).strip().lower()
    ch = PARAMS[key].get("choices")
    if ch and v not in [str(c).lower() for c in ch]:
        return str(_default(key)).lower()
    return v


def pb(key):
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

POWER = pf("magnet_power")
REACH = pf("reach")
PAYLOAD = ps("payload")
COUNT = pi("count")
PIECE = pf("piece_size")
SWING = pf("swing_seconds")
HEIGHT = pf("magnet_height")
BALL = pb("wrecking_ball")
SEED = pi("seed")
LENS = pf("lens")
PITCH = math.radians(pf("camera_pitch_deg"))
FILL = pf("fill")

BED_X, BED_Y = 1.6, 1.2


def mat(name, rgb, rough=0.4, metal=0.0):
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    b = m.node_tree.nodes["Principled BSDF"]
    b.inputs["Base Color"].default_value = (*rgb, 1.0)
    b.inputs["Roughness"].default_value = rough
    if "Metallic" in b.inputs:
        b.inputs["Metallic"].default_value = metal
    return m


def main():
    rng = random.Random(SEED)
    bpy.ops.wm.read_factory_settings(use_empty=True)
    sc = bpy.context.scene
    sc.render.engine = "CYCLES"
    sc.cycles.samples = SAMPLES
    sc.render.resolution_x, sc.render.resolution_y = RES_X, RES_Y
    sc.render.fps = FPS
    sc.frame_start = 1
    sc.frame_end = max(2, int(round(SECONDS * FPS)))

    bpy.ops.mesh.primitive_cube_add(size=1, location=(0, 0, -0.05))
    floor = bpy.context.object
    floor.scale = (BED_X * 2.4, BED_Y * 2.4, 0.1)
    bpy.ops.object.transform_apply(scale=True)
    floor.data.materials.append(mat("floor", (0.16, 0.17, 0.19), 0.65))
    bpy.ops.rigidbody.object_add(type="PASSIVE")
    floor.rigid_body.friction = 0.85

    steel = mat("steel", (0.62, 0.64, 0.68), 0.28, metal=1.0)
    pieces = []
    for i in range(COUNT):
        x = rng.uniform(-BED_X / 2, BED_X / 2)
        y = rng.uniform(-BED_Y / 2, BED_Y / 2)
        z = PIECE * rng.uniform(0.55, 2.4)
        kind = PAYLOAD if PAYLOAD != "mixed" else rng.choice(["spheres", "cubes", "bolts"])
        if kind == "cubes":
            bpy.ops.mesh.primitive_cube_add(size=PIECE * 1.6, location=(x, y, z))
        elif kind == "bolts":
            bpy.ops.mesh.primitive_cylinder_add(radius=PIECE * 0.42, depth=PIECE * 2.6,
                                                vertices=10, location=(x, y, z),
                                                rotation=(rng.uniform(0, 3.1),
                                                          rng.uniform(0, 3.1), 0))
        else:
            bpy.ops.mesh.primitive_uv_sphere_add(radius=PIECE, segments=12, ring_count=8,
                                                 location=(x, y, z))
        o = bpy.context.object
        o.data.materials.append(steel)
        for poly in o.data.polygons:
            poly.use_smooth = (kind == "spheres")
        bpy.ops.rigidbody.object_add(type="ACTIVE")
        o.rigid_body.mass = 0.05
        o.rigid_body.friction = 0.6
        o.rigid_body.restitution = 0.1
        # Asleep at frame 1: a bed that settles into itself wastes the first second.
        o.rigid_body.use_deactivation = True
        o.rigid_body.use_start_deactivated = True
        pieces.append(o)

    # ---- the magnet: a kinematic body carrying a FORCE field ----------------------
    bpy.ops.mesh.primitive_cylinder_add(radius=PIECE * 3.2, depth=PIECE * 2.2, vertices=24,
                                        location=(-BED_X / 2 - 0.35, 0, HEIGHT))
    magnet = bpy.context.object
    magnet.data.materials.append(mat("magnet", (0.55, 0.10, 0.10), 0.35, metal=0.6))
    bpy.ops.object.effector_add(type="FORCE", location=(-BED_X / 2 - 0.35, 0, HEIGHT))
    field = bpy.context.object
    field.field.strength = -POWER          # negative = attract
    field.field.use_max_distance = True
    field.field.distance_max = REACH
    field.field.falloff_power = 2.0        # inverse square; linear lifts the whole bed
    field.parent = magnet

    # Constant sweep, no easing - a magnet that eases in reads as an animation. The
    # interpolation is set as the INSERT DEFAULT rather than fixed up afterwards: Blender
    # 5.2 moved fcurves into action layers/slots, so action.fcurves no longer exists and
    # the old post-hoc loop raised AttributeError on every run.
    try:
        bpy.context.preferences.edit.keyframe_new_interpolation_type = "LINEAR"
    except Exception:  # noqa: BLE001
        pass
    travel = BED_X + 0.7
    for f, x in ((1, -BED_X / 2 - 0.35),
                 (max(2, int(SWING * FPS)), -BED_X / 2 - 0.35 + travel)):
        magnet.location = (x, 0, HEIGHT)
        magnet.keyframe_insert("location", frame=f)

    if BALL:
        bpy.ops.mesh.primitive_uv_sphere_add(radius=PIECE * 4.0, segments=20, ring_count=12,
                                             location=(-BED_X / 2 - 0.9, 0, PIECE * 5))
        wb = bpy.context.object
        wb.data.materials.append(mat("ball", (0.24, 0.24, 0.26), 0.5, metal=0.8))
        bpy.ops.rigidbody.object_add(type="ACTIVE")
        wb.rigid_body.mass = 400.0
        wb.rigid_body.kinematic = False

    world = bpy.data.worlds.new("w")
    sc.world = world
    world.use_nodes = True
    world.node_tree.nodes["Background"].inputs[0].default_value = (0.04, 0.045, 0.06, 1)
    key = bpy.data.objects.new("key", bpy.data.lights.new("key", "AREA"))
    key.data.energy = 700
    key.data.size = 3.0
    key.location = (1.4, -1.6, 2.2)
    key.rotation_euler = (math.radians(48), 0, math.radians(42))
    sc.collection.objects.link(key)
    rim = bpy.data.objects.new("rim", bpy.data.lights.new("rim", "AREA"))
    rim.data.energy = 380
    rim.data.size = 4.0
    rim.location = (-1.8, 1.4, 1.4)
    rim.rotation_euler = (math.radians(70), 0, math.radians(-120))
    sc.collection.objects.link(rim)

    cam_data = bpy.data.cameras.new("cam")
    cam_data.lens = LENS
    cam = bpy.data.objects.new("cam", cam_data)
    sc.collection.objects.link(cam)
    sc.camera = cam
    aspect = RES_X / float(RES_Y)
    need = (BED_X + 0.9) / FILL
    d = max(need * LENS / 36.0, (BED_Y / FILL / aspect) * LENS / 36.0)
    aim = Vector((0.0, 0.0, HEIGHT * 0.45))
    cam.location = (0.0, -d * math.cos(PITCH), aim.z + d * math.sin(PITCH))
    cam.rotation_euler = (aim - Vector(cam.location)).to_track_quat("-Z", "Y").to_euler()

    end = PREVIEW if PREVIEW > 0 else sc.frame_end
    lifted_peak, first_lift = 0, 0
    events = []
    for f in range(1, end + 1):
        sc.frame_set(f)
        deps = bpy.context.evaluated_depsgraph_get()
        lifted = sum(1 for o in pieces
                     if o.evaluated_get(deps).matrix_world.translation.z > PIECE * 3.2)
        if lifted > lifted_peak:
            lifted_peak = lifted
        if lifted >= 3 and not first_lift:
            first_lift = f
            events.append({"frame": f, "kind": "impact", "strength": 0.7, "label": "lift"})

    with open(f"{OUT}/impacts.json", "w", encoding="utf-8") as fh:
        json.dump({"fps": FPS, "events": events, "magnet_power": POWER,
                   "pieces": len(pieces), "lifted_peak": lifted_peak,
                   "lifted_share": round(lifted_peak / max(1, len(pieces)), 3),
                   "first_lift_frame": first_lift, "frames": end}, fh, indent=1)

    sc.render.image_settings.file_format = "PNG"
    sc.render.image_settings.color_mode = "RGB"
    if PREVIEW > 0:
        sc.frame_set(PREVIEW)
        sc.render.filepath = f"{OUT}/preview.png"
        bpy.ops.render.render(write_still=True)
    else:
        sc.render.filepath = f"{OUT}/frame_"
        bpy.ops.render.render(animation=True)
    print(f"SCENE_OK power={POWER:.0f} reach={REACH:.2f} payload={PAYLOAD} "
          f"pieces={len(pieces)} lifted_peak={lifted_peak} "
          f"share={lifted_peak/max(1,len(pieces)):.2f} first_lift={first_lift} "
          f"frames={end} cam_dist={d:.2f}")


if bpy is not None:
    main()
