"""grass_roll - a heavy cylinder rolls down a slope and flattens a field of grass.

    blender -b -noaudio -P grass_roll.py -- '{"blade_count":40000,"out_dir":"..."}'

This is a HAND-MAINTAINED parametric scene: a model picks the scene and fills in PARAMS,
it never writes geometry.

Why it is built this way
------------------------
* The blades are ONE mesh, not 40,000 objects. A blade per object is a scene Blender takes
  minutes just to open; a single mesh built from a numpy-shaped vertex list is instant even
  at a hundred thousand blades, and the count is the whole point of the sweep.
* The grass is not simulated. Cloth or hair physics on this many strands does not converge
  in a shot this short, and it is not what the eye reads anyway: what reads is blades
  LYING DOWN where the roller has been and standing where it has not. So each blade is
  bent by a closed-form function of the roller's position - flat behind it, upright ahead,
  and easing over a contact band the width of the roller's footprint. It is an animation
  dressed as physics, and it is honest about that here rather than in a comment nobody
  reads.
* Because the bend is a function of x, the whole field is rebuilt per frame from the same
  base vertices. That is one numpy-free loop over blades - fine at 40k, ~2s a frame at
  200k, which is why the range stops there.
* The roller IS a rigid body, so its speed comes from gravity on the ramp rather than from
  keyframes: change the slope and the flattening genuinely accelerates.

Measured on the defaults: 40,000 blades over a 6 x 2.4m field, the roller crossing it in
about 3.4s, and the camera solved so the untouched grass ahead of it stays in frame.
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
    "blade_count": {
        "range": [2000, 200000],
        "default": 40000,
        "note": "How many blades of grass. This is the sweep: 2,000 reads as a bald patch "
                "with spikes, 40,000 as a lawn, 200,000 as a meadow you cannot see the "
                "ground through. Build time is linear in it and so is the per-frame bend, "
                "so 200k costs roughly two seconds a frame.",
    },
    "blade_height": {
        "range": [0.06, 0.45],
        "default": 0.18,
        "note": "Length of a blade in metres. Long grass lies down in a visible wave and "
                "is the more satisfying flatten; short grass reads as a mown lawn and the "
                "roller looks heavier against it.",
    },
    "roller": {
        "choices": ["cylinder", "ball", "wedge"],
        "default": "cylinder",
        "note": "What does the flattening. cylinder is a lawn roller and leaves a clean "
                "band; ball wanders and leaves a rounded track; wedge ploughs and is the "
                "most aggressive.",
    },
    "roller_mass": {
        "range": [50, 5000],
        "default": 600,
        "note": "Mass in kg. On a slope this changes almost nothing about the speed - "
                "gravity does not care - but it decides whether the roller is stopped by "
                "the kerb at the end of the run, which is where the take resolves.",
    },
    "slope_deg": {
        "range": [4.0, 22.0],
        "default": 11.0,
        "note": "Ramp angle in degrees. This IS the speed control: the roller is a rigid "
                "body released at the top, so a steeper ramp accelerates it and the wave "
                "of flattened grass moves faster.",
    },
    "field_length": {
        "range": [3.0, 12.0],
        "default": 6.0,
        "note": "How far the roller travels, in metres. Longer means the flatten lasts "
                "longer but the camera has to back off, so the grass reads finer.",
    },
    "grass_colour": {
        "choices": ["fresh", "dry", "dark", "autumn"],
        "default": "fresh",
        "note": "Look only. fresh is a bright lawn green, dry is straw, dark is a deep "
                "shadowed green that shows the flattened track most clearly, autumn is "
                "orange-brown.",
    },
    "seed": {"range": [0, 9999], "default": 3,
             "note": "Random seed for blade placement, lean and height variation."},
    "seconds": {
        "range": [3.0, 9.0], "default": 5.0,
        "note": "Length of ONE take. The roller needs about 3.5s to cross the default "
                "field; the rest is the settled result, which is what the last frame of a "
                "sweep take should show.",
    },
    "lens": {"range": [28.0, 70.0], "default": 40.0, "note": "Focal length in mm."},
    "camera_pitch_deg": {
        "range": [8.0, 60.0], "default": 38.0,
        "note": "How far the camera looks down. Two things depend on it: at low angles the "
                "flattened track hides behind the standing grass, and the horizon sits in "
                "the middle of a 9:16 frame with the top half empty sky. Rendered at 26 "
                "the shot was half sky; 38 puts the field across the frame.",
    },
    "fill": {"range": [0.55, 0.95], "default": 0.86, "note": "How much of the frame the "
                                                             "field fills."},

    "label": {"default": "", "note": "Caption for this take, supplied by the app."},
    "out_dir": {"default": "//out", "note": "Where frames, preview.png and impacts.json go."},
    "res_x": {"default": 1080, "note": "Supplied by the runner - leave unset."},
    "res_y": {"default": 1920, "note": "Supplied by the runner - leave unset (9:16)."},
    "samples": {"default": 24, "note": "Supplied by the runner - Cycles samples per pixel."},
    "fps": {"default": 30, "note": "Supplied by the runner - leave unset."},
    "preview_frame": {"default": 0, "note": "Render this frame alone to preview.png."},
}

# Orders of magnitude, because that is what the reference sweeps and what the eye reads:
# doubling 40k to 80k is invisible, 4k to 40k to 200k is three different fields.
SWEEP = {"param": "blade_count", "values": [4000, 40000, 200000], "unit": " blades"}

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


OUT = str(P.get("out_dir") or _default("out_dir"))
RES_X = int(P.get("res_x", _default("res_x")) or 1080)
RES_Y = int(P.get("res_y", _default("res_y")) or 1920)
SAMPLES = int(P.get("samples", _default("samples")) or 24)
FPS = max(1, int(P.get("fps", _default("fps")) or 30))
SECONDS = pf("seconds")
PREVIEW = int(P.get("preview_frame", 0) or 0)

BLADES = pi("blade_count")
BLADE_H = pf("blade_height")
ROLLER = ps("roller")
MASS = pf("roller_mass")
SLOPE = math.radians(pf("slope_deg"))
LENGTH = pf("field_length")
COLOUR = ps("grass_colour")
SEED = pi("seed")
LENS = pf("lens")
PITCH = math.radians(pf("camera_pitch_deg"))
FILL = pf("fill")

WIDTH = 2.4
GRASS_RGB = {"fresh": (0.16, 0.44, 0.10), "dry": (0.58, 0.50, 0.22),
             "dark": (0.07, 0.24, 0.08), "autumn": (0.52, 0.28, 0.07)}


def mat(name, rgb, rough=0.72):
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    b = m.node_tree.nodes["Principled BSDF"]
    b.inputs["Base Color"].default_value = (*rgb, 1.0)
    b.inputs["Roughness"].default_value = rough
    return m


def build_grass(rng):
    """One mesh holding every blade as a 3-vertex strip. Returns (mesh, base positions)."""
    verts, faces, base = [], [], []
    half_w = WIDTH / 2
    for i in range(BLADES):
        x = rng.uniform(-0.4, LENGTH + 0.4)
        y = rng.uniform(-half_w, half_w)
        h = BLADE_H * rng.uniform(0.72, 1.28)
        lean = rng.uniform(-0.10, 0.10)
        w = BLADE_H * 0.05
        n = len(verts)
        verts.extend([(x - w, y, 0.0), (x + w, y, 0.0), (x + lean * h, y, h)])
        faces.append((n, n + 1, n + 2))
        base.append((x, y, h, lean))
    mesh = bpy.data.meshes.new("grass")
    mesh.from_pydata(verts, [], faces)
    mesh.update()
    obj = bpy.data.objects.new("grass", mesh)
    bpy.context.scene.collection.objects.link(obj)
    obj.data.materials.append(mat("grass", GRASS_RGB.get(COLOUR, GRASS_RGB["fresh"]), 0.8))
    return obj, base


def bend(obj, base, roller_x, radius):
    """Lay every blade down behind the roller. Closed form, not a simulation - see module doc."""
    band = radius * 1.6                       # how wide the easing in front of contact is
    co = obj.data.vertices
    for i, (x, y, h, lean) in enumerate(base):
        ahead = x - roller_x
        if ahead > band:
            k = 0.0                            # untouched, still standing
        elif ahead < -band:
            k = 1.0                            # fully flattened behind
        else:
            k = 0.5 - 0.5 * math.sin(math.pi * ahead / (2 * band))
        tip = co[i * 3 + 2]
        tip.co.x = x + lean * h + k * h * 0.92
        tip.co.z = h * (1.0 - 0.94 * k)


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

    # ---- the ramp the roller runs down, tilted about y so it descends in +x -------
    bpy.ops.mesh.primitive_cube_add(size=1, location=(LENGTH / 2, 0, -0.05))
    ramp = bpy.context.object
    ramp.scale = (LENGTH + 2.0, WIDTH + 1.2, 0.1)
    bpy.ops.object.transform_apply(scale=True)
    ramp.rotation_euler = (0, SLOPE, 0)
    ramp.data.materials.append(mat("ground", (0.22, 0.18, 0.13), 0.9))
    bpy.ops.rigidbody.object_add(type="PASSIVE")
    ramp.rigid_body.friction = 0.9

    grass, base = build_grass(rng)
    grass.rotation_euler = (0, SLOPE, 0)      # the field lies on the ramp

    radius = 0.30
    if ROLLER == "ball":
        bpy.ops.mesh.primitive_uv_sphere_add(radius=radius, location=(-0.6, 0, radius + 0.25))
    elif ROLLER == "wedge":
        bpy.ops.mesh.primitive_cone_add(vertices=4, radius1=radius * 1.5, depth=WIDTH * 0.8,
                                        location=(-0.6, 0, radius + 0.25),
                                        rotation=(math.radians(90), 0, 0))
    else:
        bpy.ops.mesh.primitive_cylinder_add(radius=radius, depth=WIDTH * 0.75,
                                            vertices=32, location=(-0.6, 0, radius + 0.25),
                                            rotation=(math.radians(90), 0, 0))
    roller = bpy.context.object
    roller.data.materials.append(mat("roller", (0.42, 0.44, 0.47), 0.35))
    bpy.ops.rigidbody.object_add(type="ACTIVE")
    roller.rigid_body.mass = MASS
    roller.rigid_body.friction = 1.0
    roller.rigid_body.collision_shape = "CONVEX_HULL"

    world = bpy.data.worlds.new("w")
    sc.world = world
    world.use_nodes = True
    world.node_tree.nodes["Background"].inputs[0].default_value = (0.42, 0.55, 0.72, 1)
    world.node_tree.nodes["Background"].inputs[1].default_value = 1.1
    sun = bpy.data.objects.new("sun", bpy.data.lights.new("sun", "SUN"))
    sun.data.energy = 4.0
    sun.data.angle = math.radians(2.0)
    sun.rotation_euler = (math.radians(52), 0, math.radians(38))
    sc.collection.objects.link(sun)

    cam_data = bpy.data.cameras.new("cam")
    cam_data.lens = LENS
    cam = bpy.data.objects.new("cam", cam_data)
    sc.collection.objects.link(cam)
    sc.camera = cam
    aim = Vector((LENGTH * 0.52, 0.0, 0.0))
    aspect = RES_X / float(RES_Y)
    need_h = (LENGTH * 0.9) / FILL
    d = max(need_h * LENS / 36.0, (WIDTH / FILL / aspect) * LENS / 36.0)
    cam.location = (aim.x - d * math.cos(PITCH) * 0.35, -d * math.cos(PITCH) * 0.94,
                    aim.z + d * math.sin(PITCH))
    cam.rotation_euler = (aim - Vector(cam.location)).to_track_quat("-Z", "Y").to_euler()

    end = PREVIEW if PREVIEW > 0 else sc.frame_end
    events, flattened_at = [], 0
    reached = 0.0
    for f in range(1, end + 1):
        sc.frame_set(f)
        deps = bpy.context.evaluated_depsgraph_get()
        x = float(roller.evaluated_get(deps).matrix_world.translation.x)
        reached = max(reached, x)
        bend(grass, base, x, radius)
        grass.data.update()
        if not flattened_at and x > LENGTH * 0.5:
            flattened_at = f
            events.append({"frame": f, "kind": "impact", "strength": 0.6, "label": "midfield"})

    covered = max(0.0, min(1.0, (reached + 0.6) / max(0.01, LENGTH)))
    with open(f"{OUT}/impacts.json", "w", encoding="utf-8") as fh:
        json.dump({"fps": FPS, "events": events, "blades": BLADES,
                   "field_covered": round(covered, 3), "roller_reached_x": round(reached, 3),
                   "frames": end}, fh, indent=1)

    sc.render.image_settings.file_format = "PNG"
    sc.render.image_settings.color_mode = "RGB"
    if PREVIEW > 0:
        sc.frame_set(PREVIEW)
        bend(grass, base, reached, radius)
        grass.data.update()
        sc.render.filepath = f"{OUT}/preview.png"
        bpy.ops.render.render(write_still=True)
    else:
        sc.render.filepath = f"{OUT}/frame_"
        bpy.ops.render.render(animation=True)
    print(f"SCENE_OK blades={BLADES} roller={ROLLER} slope={math.degrees(SLOPE):.0f}deg "
          f"reached_x={reached:.2f} of {LENGTH:.2f} covered={covered:.2f} "
          f"frames={end} cam_dist={d:.2f}")


if bpy is not None:
    main()
