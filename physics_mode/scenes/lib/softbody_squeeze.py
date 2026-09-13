"""softbody_squeeze - a jelly body is dropped onto a hole it does not fit through.

    blender -b -noaudio -P softbody_squeeze.py -- '{"softness":70,"body":"cube",
                                                    "obstacle":"hole_plate","out_dir":"..."}'

This is a HAND-MAINTAINED parametric scene: a model picks the scene and fills in PARAMS,
it never writes geometry.

Why this scene exists
---------------------
Every soft-body short in the reference folder sweeps the same value - "softness 0% -> 100%",
"how soft does the cube need to be to fit in the hole" - and the whole library was rigid
bodies, so none of it could be reproduced. The appeal is a single question the viewer can
hold in their head: at what softness does it get through? That means the shot needs a gap
the body cannot pass at 0% and can pass at 100%, and everything below is in service of
making that boundary land somewhere in the middle of the sweep.

Why it is built this way
------------------------
* The jelly is a CLOTH modifier with pressure, not Blender's Soft Body. Soft Body needs a
  goal mesh to keep any shape and collapses to a puddle without one, and its self-collision
  is unreliable; cloth-with-pressure is a closed surface holding its own volume, which is
  what jelly is. Stiffness and pressure both scale from `softness`, because a soft body
  that keeps full internal pressure squeezes flat and springs back like a balloon instead
  of flowing like jelly.
* The mesh is subdivided to a roughly even quad size. Cloth solves per edge, so a coarse
  mesh cannot bend around a rim - the corners catch and the body sits on the hole looking
  rigid at any softness. `mesh_detail` is the single knob for that, and it is the main cost.
* The hole is built from FOUR SLABS, not a boolean. A boolean-cut plate leaves an n-gon rim
  that cloth collision snags on; four boxes give a clean square aperture whose inner edges
  are real faces the solver already handles well.
* The drop height is small on purpose. A body arriving fast enough to punch through tells
  the viewer nothing about softness - it tells them about momentum. Low and slow makes the
  deformation, not the impact, the thing on screen.
* The camera distance is SOLVED from the action box, not guessed: Blender fits the sensor
  to the LARGER resolution axis, so in 9:16 the 36mm sensor maps to frame HEIGHT and the
  visible width is that times 9/16. Both constraints are checked and the wider one wins.

What it measures and prints
---------------------------
The deformed mesh is evaluated every frame through the dependency graph, so the numbers in
SCENE_OK and impacts.json are the real simulation, not the intent: how far the body
squeezed (its narrowest width as a fraction of its rest width), whether it actually passed
the plate, and on which frame. A take where nothing passes is a valid answer to the
question - it is what 0% looks like - so the scene reports it rather than failing.
"""

import json
import math
import sys

try:
    import bpy
    from mathutils import Vector
except ImportError:                      # importable without Blender, for the catalogue
    bpy = None
    Vector = None

# --------------------------------------------------------------------------- parameters
PARAMS = {
    "softness": {
        "range": [0, 100],
        "default": 60,
        "note": "The whole point of the scene, in percent. 0 is a stiff rubber block that "
                "bounces off the rim and sits on top; 100 is loose jelly that pours "
                "through. The interesting takes are 40-80, where it hangs in the hole "
                "and slowly gives. Drives cloth stiffness AND internal pressure together.",
    },
    "body": {
        "choices": ["cube", "sphere", "capsule", "torus"],
        "default": "cube",
        "note": "Shape of the jelly. cube has corners that catch on the rim and is the "
                "clearest read; sphere is the most fluid; capsule stands up and folds; "
                "torus is the most fragile in the solver - it can self-tangle at high "
                "softness, which looks good but is the slowest to compute.",
    },
    "obstacle": {
        "choices": ["hole_plate", "stairs_drain"],
        "default": "hole_plate",
        "note": "hole_plate is one square aperture the body has to get through - the "
                "cleanest question. stairs_drain drops it down three steps first, so it "
                "arrives deformed and tumbling and the hole is the payoff.",
    },
    "gap_ratio": {
        "range": [0.30, 0.95],
        "default": 0.66,
        "note": "Hole width as a fraction of the body's width. This number decides whether "
                "the sweep says anything, and it was found by rendering rather than "
                "reasoning: at 0.55 even 100% jelly only necked into the hole and hung "
                "there, so all three takes ended the same way. At 0.66, measured over an "
                "8s take, 0% sits on the plate (lowest point +0.04 above it), 55% dips in "
                "and stalls (-0.10), and 100% necks to a fifth of its width and drops "
                "clear on frame 77. Above ~0.85 a stiff body fits too and the sweep is "
                "flat again.",
    },
    "body_size": {
        "range": [0.30, 1.20],
        "default": 0.70,
        "note": "Edge length (or diameter) of the body in metres. Everything else - hole, "
                "plate, drop height, camera - is derived from it, so this mostly changes "
                "the solver cost, not the framing.",
    },
    "mesh_detail": {
        "range": [2, 6],
        "default": 4,
        "note": "Subdivision cuts per side. 2 is blocky and cannot round over the rim; 6 "
                "is smooth and roughly six times the solve time. 4 is the point where the "
                "silhouette stops reading as a polygon.",
    },
    "drop_height": {
        "range": [0.15, 1.50],
        "default": 0.45,
        "note": "Gap between the bottom of the body and the plate at frame 1, in metres. "
                "Low is better: this scene is about deformation, and a fast arrival "
                "replaces the answer with momentum.",
    },
    "material": {
        "choices": ["jelly_red", "jelly_green", "milk", "honey", "slime"],
        "default": "jelly_red",
        "note": "Look only, no physics. jelly_* and slime are translucent and show the "
                "squeeze from inside; milk is opaque and reads the silhouette hardest; "
                "honey is dark amber and the slowest to render.",
    },
    "seconds": {
        "range": [3.0, 12.0],
        "default": 7.0,
        "note": "Length of ONE take. A soft body needs about two seconds after contact to "
                "finish giving, and the soft end of the sweep needs roughly 2.5s MORE to "
                "finish going through - at 5s it was still in the hole when the take "
                "ended, which reads as a cut rather than a result.",
    },
    "backdrop": {
        "default": True,
        "note": "Lit wall behind the action. Off gives black surroundings, which suits a "
                "dark edit but loses the shadow that sells the contact.",
    },
    "lens": {
        "range": [35.0, 85.0],
        "default": 50.0,
        "note": "Focal length in mm. Longer flattens the plate into a clean line and is "
                "the safer look; shorter shows more of the hole's depth.",
    },
    "camera_pitch_deg": {
        "range": [0.0, 45.0],
        "default": 20.0,
        "note": "How far the camera looks DOWN at the plate. This is not a taste setting: "
                "at 0 the hole is edge-on and invisible, and the shot becomes a blob on a "
                "grey bar. 15-25 shows the aperture as a square while keeping the squeeze "
                "in profile; past ~35 the body hides the hole it is going through.",
    },
    "fill": {
        "range": [0.55, 0.95],
        "default": 0.80,
        "note": "How much of the frame the action box fills. Above ~0.9 the body touches "
                "the edge as it spreads on impact.",
    },

    # ---- supplied by the runner, not by the model choosing the scene -----------------
    "label": {"default": "", "note": "Caption for this take, supplied by the app when it "
                                     "sweeps. Empty means the scene writes its own."},
    "out_dir": {"default": "//out", "note": "Where frames, preview.png and impacts.json go."},
    "res_x": {"default": 1080, "note": "Supplied by the runner - leave unset."},
    "res_y": {"default": 1920, "note": "Supplied by the runner - leave unset (9:16)."},
    "samples": {"default": 24, "note": "Supplied by the runner - Cycles samples per pixel."},
    "fps": {"default": 30, "note": "Supplied by the runner - leave unset."},
    "preview_frame": {"default": 0,
                      "note": "If greater than zero, simulate to this frame, render it "
                              "alone to preview.png and stop. Pick a frame after contact."},
}

# Three takes that answer the question: refuses, hangs, pours.
SWEEP = {"param": "softness", "values": [0, 55, 100], "unit": "%"}

argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
P = json.loads(argv[0]) if argv else {}


def _default(key):
    return PARAMS[key]["default"]


def pf(key) -> float:
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

SOFT = pf("softness") / 100.0
BODY = ps("body")
OBSTACLE = ps("obstacle")
GAP_RATIO = pf("gap_ratio")
SIZE = pf("body_size")
DETAIL = pi("mesh_detail")
DROP = pf("drop_height")
MATERIAL = ps("material")
BACKDROP = pb("backdrop")
LENS = pf("lens")
FILL = pf("fill")

# Colour, roughness and transmission per material. Transmission is what makes a squeeze
# readable from outside - an opaque body only shows its outline.
LOOK = {
    "jelly_red":   ((0.72, 0.06, 0.10), 0.12, 0.72),
    "jelly_green": ((0.22, 0.68, 0.20), 0.12, 0.72),
    "milk":        ((0.94, 0.93, 0.90), 0.32, 0.00),
    "honey":       ((0.62, 0.34, 0.03), 0.08, 0.80),
    "slime":       ((0.45, 0.85, 0.25), 0.18, 0.55),
}


def clear():
    bpy.ops.wm.read_factory_settings(use_empty=True)


def mat(name, rgb, rough, transmission=0.0, emit=0.0):
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    b = m.node_tree.nodes["Principled BSDF"]
    b.inputs["Base Color"].default_value = (*rgb, 1.0)
    b.inputs["Roughness"].default_value = rough
    if transmission and "Transmission Weight" in b.inputs:
        b.inputs["Transmission Weight"].default_value = transmission
        b.inputs["IOR"].default_value = 1.36
    if emit and "Emission Strength" in b.inputs:
        b.inputs["Emission Color"].default_value = (*rgb, 1.0)
        b.inputs["Emission Strength"].default_value = emit
    return m


def put(obj, material, smooth=True):
    obj.data.materials.clear()
    obj.data.materials.append(material)
    for poly in obj.data.polygons:
        poly.use_smooth = smooth
    return obj


def slab(name, location, scale, material):
    bpy.ops.mesh.primitive_cube_add(size=1.0, location=location)
    o = bpy.context.object
    o.name = name
    o.scale = scale
    bpy.ops.object.transform_apply(scale=True)
    put(o, material, smooth=False)
    bpy.ops.object.modifier_add(type="COLLISION")
    # A thin collider needs a thicker skin or a fast body tunnels straight through it.
    o.collision.thickness_outer = 0.008
    o.collision.thickness_inner = 0.04
    o.collision.damping = 0.35
    return o


def build_body(z):
    """The jelly, subdivided evenly and given cloth-with-pressure."""
    if BODY == "sphere":
        bpy.ops.mesh.primitive_uv_sphere_add(radius=SIZE / 2, location=(0, 0, z),
                                             segments=8 * DETAIL, ring_count=4 * DETAIL)
    elif BODY == "capsule":
        bpy.ops.mesh.primitive_cylinder_add(radius=SIZE / 2.6, depth=SIZE * 1.35,
                                            vertices=6 * DETAIL, location=(0, 0, z))
    elif BODY == "torus":
        bpy.ops.mesh.primitive_torus_add(major_radius=SIZE / 2, minor_radius=SIZE / 6,
                                         major_segments=8 * DETAIL,
                                         minor_segments=3 * DETAIL, location=(0, 0, z))
    else:
        bpy.ops.mesh.primitive_cube_add(size=SIZE, location=(0, 0, z))
        bpy.ops.object.mode_set(mode="EDIT")
        bpy.ops.mesh.subdivide(number_cuts=max(1, DETAIL * 3))
        bpy.ops.object.mode_set(mode="OBJECT")
    o = bpy.context.object
    o.name = "jelly"
    rgb, rough, trans = LOOK.get(MATERIAL, LOOK["jelly_red"])
    put(o, mat("jelly", rgb, rough, trans))

    bpy.ops.object.modifier_add(type="CLOTH")
    st = o.modifiers["Cloth"].settings
    st.quality = 8
    # Mass rises with softness. Cloth mass is per vertex and it is what fights the
    # stiffness: at 0.4kg a soft body still only sagged into the hole and stopped, because
    # gravity had nothing to push with. Heavy AND soft is what pours.
    st.mass = 0.4 + 1.6 * SOFT
    # Stiffness spans two orders of magnitude across the sweep. Linear in `softness` would
    # spend most of the range looking identical, because the visible difference between 40
    # and 4 is far larger than between 400 and 364.
    stiff = 10.0 ** (2.6 - 2.2 * SOFT)          # 400 at 0%, ~2.5 at 100%
    st.tension_stiffness = stiff
    st.compression_stiffness = stiff
    st.shear_stiffness = stiff
    st.bending_stiffness = max(0.02, stiff * 0.05)
    st.tension_damping = 12.0
    st.compression_damping = 12.0
    st.bending_damping = 1.0
    st.use_pressure = True
    # Pressure has to fall with stiffness: a soft skin holding hard pressure is a balloon,
    # it springs back out of the hole instead of flowing through it.
    st.uniform_pressure_force = 6.0 * (1.0 - 0.85 * SOFT)
    st.use_pressure_volume = True
    st.target_volume = 1.0
    st.pressure_factor = 1.0
    col = o.modifiers["Cloth"].collision_settings
    col.use_self_collision = True
    # Collision margins are a SKIN: the body cannot enter an aperture narrower than its
    # own margin plus the rim's. Thin margins are why it fits through at all.
    col.self_distance_min = SIZE * 0.012
    col.distance_min = 0.006
    col.collision_quality = 5
    return o


def build_obstacle(plate_z):
    """Returns (plate_z, hole_half, obstacle objects). The aperture is four slabs."""
    grey = mat("plate", (0.30, 0.31, 0.34), 0.42)
    hole_half = SIZE * GAP_RATIO / 2.0
    span = SIZE * 3.2
    thick = SIZE * 0.16
    ring = (span / 2 + hole_half) / 2.0          # centre of each slab
    width = span / 2 - hole_half                 # its width
    objs = []
    for name, loc, sc in (
            ("plate_x+", (ring, 0, plate_z), (width, span, thick)),
            ("plate_x-", (-ring, 0, plate_z), (width, span, thick)),
            ("plate_y+", (0, ring, plate_z), (hole_half * 2, width, thick)),
            ("plate_y-", (0, -ring, plate_z), (hole_half * 2, width, thick))):
        objs.append(slab(name, loc, sc, grey))
    if OBSTACLE == "stairs_drain":
        step = mat("step", (0.38, 0.36, 0.33), 0.55)
        for i in range(3):
            objs.append(slab(f"step{i}", (SIZE * (0.9 - 0.6 * i), 0,
                                          plate_z + SIZE * (1.9 - 0.55 * i)),
                             (SIZE * 1.5, SIZE * 2.2, SIZE * 0.18), step))
    # catch floor, so the take ends on something instead of an infinite fall
    objs.append(slab("floor", (0, 0, plate_z - SIZE * 2.4), (span * 1.6, span * 1.6, thick),
                     mat("floor", (0.20, 0.21, 0.23), 0.6)))
    return hole_half, objs


def solve_camera(cam, aim, half_h, half_w):
    """Distance that fits the action box, honouring which axis the sensor maps to.

    Blender fits the 36mm sensor to the LARGER resolution axis, so in a 9:16 frame it maps
    to HEIGHT and the visible width is height * res_x/res_y. Both constraints are computed
    and the binding one wins.

    The camera looks DOWN by `camera_pitch_deg`. A level camera was the first version and
    it hid the entire point of the scene: the aperture is a hole in a horizontal plate, so
    edge-on it is invisible and the render read as a blob resting on a grey bar. Rendered
    and looked at, which is the only way that was ever going to surface.
    """
    aspect = RES_X / float(RES_Y)
    need_h = (2 * half_h) / FILL
    need_w = (2 * half_w) / FILL
    d_h = need_h * LENS / 36.0
    d_w = (need_w / aspect) * LENS / 36.0
    d = max(d_h, d_w)
    pitch = math.radians(pf("camera_pitch_deg"))
    cam.location = (0.0, -d * math.cos(pitch), aim.z + d * math.sin(pitch))
    direction = aim - Vector(cam.location)
    cam.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()
    return d, ("width" if d_w >= d_h else "height")


def main():
    clear()
    sc = bpy.context.scene
    sc.render.engine = "CYCLES"
    sc.cycles.samples = SAMPLES
    sc.render.resolution_x, sc.render.resolution_y = RES_X, RES_Y
    sc.render.fps = FPS
    sc.frame_start = 1
    sc.frame_end = max(2, int(round(SECONDS * FPS)))

    plate_z = 0.0
    hole_half, _obstacles = build_obstacle(plate_z)
    body_z = plate_z + SIZE / 2 + DROP + (SIZE * 2.3 if OBSTACLE == "stairs_drain" else 0.0)
    body = build_body(body_z)

    world = bpy.data.worlds.new("w")
    sc.world = world
    world.use_nodes = True
    bg = world.node_tree.nodes["Background"]
    bg.inputs[0].default_value = (0.05, 0.055, 0.07, 1)
    bg.inputs[1].default_value = 1.0 if BACKDROP else 0.25

    key = bpy.data.objects.new("key", bpy.data.lights.new("key", "AREA"))
    key.data.energy = 900
    key.data.size = SIZE * 6
    key.location = (SIZE * 3.0, -SIZE * 3.4, plate_z + SIZE * 4.2)
    key.rotation_euler = (math.radians(52), 0, math.radians(40))
    sc.collection.objects.link(key)
    rim = bpy.data.objects.new("rim", bpy.data.lights.new("rim", "AREA"))
    rim.data.energy = 420
    rim.data.size = SIZE * 8
    rim.location = (-SIZE * 3.4, SIZE * 2.6, plate_z + SIZE * 2.2)
    rim.rotation_euler = (math.radians(74), 0, math.radians(-125))
    sc.collection.objects.link(rim)
    if BACKDROP:
        wall = slab("wall", (0, SIZE * 3.2, plate_z + SIZE * 1.2),
                    (SIZE * 9, SIZE * 0.2, SIZE * 9),
                    mat("wall", (0.12, 0.13, 0.16), 0.85))
        wall.modifiers.clear()          # a backdrop must not collide with the jelly

    cam_data = bpy.data.cameras.new("cam")
    cam_data.lens = LENS
    cam = bpy.data.objects.new("cam", cam_data)
    sc.collection.objects.link(cam)
    sc.camera = cam
    top = body_z + SIZE * 0.6
    bottom = plate_z - SIZE * 2.4
    aim = Vector((0.0, 0.0, (top + bottom) / 2))
    # The width that must be VISIBLE is the body and its spread, not the whole plate: a
    # 9:16 frame is only 0.56 as wide as it is tall, so asking to see the full plate pushes
    # the camera back until the action is a detail in the middle. The plate running off
    # both edges is what the reference shorts look like anyway - it reads as the floor.
    dist, binding = solve_camera(cam, aim, (top - bottom) / 2, SIZE * 0.9)

    # ---- simulate, measuring the real deformed mesh every frame --------------------
    end = PREVIEW if PREVIEW > 0 else sc.frame_end
    # Bake explicitly. Stepping frames with frame_set() is enough to advance cloth in the
    # UI, where playback keeps filling the point cache - in `blender -b` it is not, and the
    # first run of this scene rendered a jelly cube that never moved and reported a squeeze
    # of exactly 1.000. That number is the only reason the bug was visible at all.
    bpy.context.view_layer.objects.active = body
    body.select_set(True)
    sc.frame_set(sc.frame_start)
    bpy.ops.ptcache.bake_all(bake=True)
    rest_w = SIZE
    narrowest = 1.0          # narrowest NECK at the plate, not the overall width
    lowest = None
    passed_frame = 0
    events = []
    prev_low = None
    band = max(0.02, SIZE * 0.09)      # a slice of the body level with the aperture
    for f in range(1, end + 1):
        sc.frame_set(f)
        deps = bpy.context.evaluated_depsgraph_get()
        ev = body.evaluated_get(deps)
        mesh = ev.to_mesh()
        pts = [(ev.matrix_world @ v.co) for v in mesh.vertices]
        if pts:
            low = min(p.z for p in pts)
            high = max(p.z for p in pts)
            lowest = low if lowest is None else min(lowest, low)
            # The question the scene asks is whether the body NECKS DOWN to the hole. Its
            # bounding box answers the opposite question: jelly landing on the plate
            # spreads, so the box only ever gets wider and the first version of this metric
            # reported exactly 1.000 for every take, at both ends of the sweep.
            neck = [p.x for p in pts if abs(p.z - plate_z) <= band]
            if len(neck) >= 4:
                narrowest = min(narrowest, (max(neck) - min(neck)) / max(1e-6, rest_w))
            if not passed_frame and high < plate_z - SIZE * 0.05:
                passed_frame = f
                events.append({"frame": f, "kind": "impact", "strength": 0.9,
                               "label": "through"})
            if prev_low is not None and low <= plate_z + SIZE * 0.06 < prev_low:
                events.append({"frame": f, "kind": "impact", "strength": 0.75,
                               "label": "contact"})
            prev_low = low
        ev.to_mesh_clear()

    events = sorted({e["frame"]: e for e in events}.values(), key=lambda e: e["frame"])[:8]
    report = {"fps": FPS, "events": events, "softness_pct": round(SOFT * 100),
              "body": BODY, "obstacle": OBSTACLE, "gap_ratio": GAP_RATIO,
              "narrowest_neck_ratio": round(narrowest, 3), "lowest_z": (None if lowest is None else round(lowest, 3)),
              "passed_frame": passed_frame, "passed": bool(passed_frame),
              "frames": end}
    with open(f"{OUT}/impacts.json", "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=1)

    sc.render.image_settings.file_format = "PNG"     # 5.2 has no FFMPEG output at all
    sc.render.image_settings.color_mode = "RGB"
    if PREVIEW > 0:
        sc.frame_set(PREVIEW)
        sc.render.filepath = f"{OUT}/preview.png"
        bpy.ops.render.render(write_still=True)
    else:
        sc.render.filepath = f"{OUT}/frame_"
        bpy.ops.render.render(animation=True)
    print(f"SCENE_OK body={BODY} obstacle={OBSTACLE} softness={SOFT*100:.0f}% "
          f"gap={GAP_RATIO:.2f} hole={hole_half*2:.3f}m size={SIZE:.2f}m "
          f"neck={narrowest:.3f} lowest={lowest if lowest is None else round(lowest,3)} passed={bool(passed_frame)} "
          f"passed_frame={passed_frame} events={len(events)} frames={end} "
          f"cam_dist={dist:.2f} binding={binding}")


if bpy is not None:
    main()
