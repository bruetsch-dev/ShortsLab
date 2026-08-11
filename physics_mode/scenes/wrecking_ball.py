"""Wrecking ball versus a brick block, swept by ball mass. Runs inside Blender: bpy only.

    blender -b -noaudio -P wrecking_ball.py -- '{"mass": 10, "label": "10kg", "out_dir": "..."}'

The block is BUILT from a grid of bricks rather than fractured on impact. Blender 5.2
dropped the Cell Fracture add-on, and the reference footage never showed voronoi shards
anyway - it breaks into sticks and little cubes, which is what a brick grid does under a
rigid-body solver.

The pendulum is solved from the CONTACT POINT backwards: the pivot sits directly above
where the ball must strike, so swinging to vertical lands it on the block's face. Placing
the pivot first and hoping put the ball 3.4m wide of the block and below the floor.

Impact frames are written to impacts.json so the app can place its sounds on the frame
the collision actually happens instead of guessing.
"""

import json
import math
import sys

import bpy

argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
P = json.loads(argv[0]) if argv else {}
MASS = float(P.get("mass", 10.0))
LABEL = str(P.get("label", f"{MASS:g}kg"))
OUT = P.get("out_dir", "//out")
RES_X, RES_Y = int(P.get("res_x", 1080)), int(P.get("res_y", 1920))
SAMPLES = int(P.get("samples", 24))
FPS = int(P.get("fps", 30))
SECONDS = float(P.get("seconds", 4.0))
# A TOWER, not a cube. A 6x6x8 block sits in the bottom third of a 9:16 frame with a
# screen of empty floor above it - measured on the first finished render, the structure
# filled 29% of the frame height. Tall and narrow is what the vertical format is for.
GRID_X, GRID_Y, GRID_Z = (int(P.get("grid_x", 5)), int(P.get("grid_y", 5)),
                          int(P.get("grid_z", 12)))
BRICK = float(P.get("brick", 0.22))
BRICK_MASS = float(P.get("brick_mass", 2.2))
SWING_FRAMES = int(P.get("swing_frames", 14))
PREVIEW = int(P.get("preview_frame", 0))       # >0: render only this frame, for framing


def use_gpu(sc) -> str:
    """Point Cycles at the GPU. Measured 20.7s/frame on CPU at 1080x1920 - a three-value
    sweep would take over two hours, which is not a mode anyone would use. OptiX first
    (RTX cards have the ray-tracing cores), CUDA as the fallback."""
    try:
        prefs = bpy.context.preferences.addons["cycles"].preferences
    except KeyError:
        return "CPU (cycles preferences unavailable)"
    for backend in ("OPTIX", "CUDA", "HIP", "ONEAPI"):
        try:
            prefs.compute_device_type = backend
            prefs.refresh_devices()
        except Exception:  # noqa: BLE001 - backend not compiled in this build
            continue
        found = [d for d in prefs.devices if d.type == backend]
        if not found:
            continue
        for d in prefs.devices:
            d.use = d.type in (backend, "CPU")
        sc.cycles.device = "GPU"
        return f"{backend}: " + ", ".join(d.name for d in found)
    return "CPU (no GPU backend)"


def mat(name, rgba, rough=0.6, metal=0.0):
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    b = m.node_tree.nodes["Principled BSDF"]
    b.inputs["Base Color"].default_value = rgba
    b.inputs["Roughness"].default_value = rough
    b.inputs["Metallic"].default_value = metal
    return m


def main():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    sc = bpy.context.scene
    sc.render.engine = "CYCLES"          # headless EEVEE falls back to software: 19s/frame
    sc.cycles.samples = SAMPLES
    sc.cycles.use_denoising = True
    print("CYCLES_DEVICE " + use_gpu(sc))
    sc.render.resolution_x, sc.render.resolution_y = RES_X, RES_Y
    sc.render.fps = FPS
    sc.frame_start = 1
    sc.frame_end = int(SECONDS * FPS)

    sc.world = bpy.data.worlds.new("W")
    sc.world.use_nodes = True
    sc.world.node_tree.nodes["Background"].inputs[0].default_value = (0.015, 0.015, 0.018, 1)

    bpy.ops.mesh.primitive_plane_add(size=80, location=(0, 0, 0))
    floor = bpy.context.object
    floor.data.materials.append(mat("Floor", (0.045, 0.045, 0.05, 1), rough=0.4))
    bpy.ops.rigidbody.object_add(type="PASSIVE")
    floor.rigid_body.friction = 0.95

    # ---- block of bricks
    yellow = mat("Yellow", (0.93, 0.70, 0.06, 1), rough=0.45)
    w = GRID_X * BRICK
    d = GRID_Y * BRICK
    h = GRID_Z * BRICK
    for ix in range(GRID_X):
        for iy in range(GRID_Y):
            for iz in range(GRID_Z):
                # Slightly smaller than its slot, so a visible seam runs between bricks.
                # Flush cubes render as one smooth slab and the viewer cannot read the
                # structure as stacked - which makes the moment it comes apart land as a
                # glitch instead of a collapse.
                bpy.ops.mesh.primitive_cube_add(
                    size=BRICK * 0.93,
                    location=((ix - (GRID_X - 1) / 2) * BRICK,
                              (iy - (GRID_Y - 1) / 2) * BRICK,
                              iz * BRICK + BRICK / 2))
                b = bpy.context.object
                b.data.materials.append(yellow)
                bpy.ops.rigidbody.object_add(type="ACTIVE")
                # Heavier than the lightest ball on purpose. At 0.35kg a 1kg ball
                # demolished the block just as thoroughly as a 50kg one, which kills the
                # whole point of the sweep - the contrast IS the video.
                b.rigid_body.mass = BRICK_MASS
                b.rigid_body.friction = 1.0
                b.rigid_body.collision_shape = "BOX"
                b.rigid_body.collision_margin = 0.0
                b.rigid_body.use_margin = True
                # asleep until struck, otherwise the stack jitters itself apart
                b.rigid_body.use_deactivation = True
                b.rigid_body.use_start_deactivated = True

    # ---- pendulum solved from the contact point
    radius = 0.40
    hit_x = -(w / 2.0) - radius          # ball surface meets the block's left face
    hit_z = h * 0.55                     # a little above centre: topples rather than slides
    arm = 3.2
    pivot = (hit_x, 0.0, hit_z + arm)    # straight above the contact point
    start_deg = -68.0

    bpy.ops.mesh.primitive_uv_sphere_add(radius=radius, segments=48, ring_count=24,
                                         location=(hit_x, 0, hit_z))
    ball = bpy.context.object
    ball.name = "Ball"
    ball.data.materials.append(mat("Steel", (0.62, 0.62, 0.66, 1), rough=0.18, metal=1.0))
    bpy.ops.object.shade_smooth()
    bpy.ops.rigidbody.object_add(type="ACTIVE")
    ball.rigid_body.mass = MASS
    ball.rigid_body.collision_shape = "SPHERE"
    ball.rigid_body.friction = 0.6
    ball.rigid_body.restitution = 0.05

    for f in range(1, SWING_FRAMES + 1):
        t = (f - 1) / max(1, SWING_FRAMES - 1)
        ease = t * t                       # accelerates like a real swing
        ang = math.radians(start_deg * (1.0 - ease))
        ball.location = (pivot[0] + arm * math.sin(ang), 0.0,
                         pivot[2] - arm * math.cos(ang))
        ball.rigid_body.kinematic = True
        ball.keyframe_insert("location", frame=f)
        ball.keyframe_insert('rigid_body.kinematic', frame=f)
    ball.rigid_body.kinematic = False      # released carrying the animated velocity
    ball.keyframe_insert('rigid_body.kinematic', frame=SWING_FRAMES + 1)

    # ---- camera framed so the tower fills the vertical frame
    # Blender fits the sensor to the LARGER resolution axis, so in 9:16 the 36mm sensor
    # maps to frame HEIGHT: visible height = 36/lens * distance. Solving for the tower
    # covering ~52% of frame height (leaving room for debris to fly) rather than picking
    # a distance and hoping. 0.38 rather than 0.52: at half the frame height the camera
    # sat on top of the action and debris left frame immediately - a sweep needs enough
    # air around the tower to read the scale of what is happening to it.
    lens = 45.0
    fill = float(P.get("frame_fill", 0.38))
    cam_dist = (h / fill) * lens / 36.0
    bpy.ops.object.camera_add(location=(0.5, -cam_dist, h * 0.50 + 0.35),
                              rotation=(math.radians(89), 0, math.radians(4)))
    cam = bpy.context.object
    cam.data.lens = lens
    sc.camera = cam

    bpy.ops.object.light_add(type="AREA", location=(3.5, -4.5, h + 4))
    k = bpy.context.object
    k.data.energy = 1800
    k.data.size = 5
    bpy.ops.object.light_add(type="AREA", location=(-5, -2.5, h + 2))
    r = bpy.context.object
    r.data.energy = 700
    r.data.size = 4

    # ---- label, parented into camera space so it never leaves frame
    bpy.ops.object.text_add(location=(0, 0, 0))
    txt = bpy.context.object
    txt.data.body = LABEL
    txt.data.align_x = "CENTER"
    txt.data.align_y = "CENTER"
    txt.data.size = 0.22
    txt.data.extrude = 0.004
    txt.data.materials.append(mat("Label", (1, 1, 1, 1), rough=0.9))
    # Parented with an IDENTITY parent-inverse so the location below really is camera
    # space. Setting matrix_parent_inverse to the camera's inverse (the reflex from
    # object parenting) turns these back into world coordinates and the label lands
    # somewhere off-scene. A text object's readable face points along its local +Z, and
    # it sits at negative Z in front of the camera, so zero rotation already faces it.
    txt.parent = cam
    txt.rotation_euler = (0, 0, 0)
    # Frame half-height at 2.6m is 36/lens*2.6/2: y=1.05 once put the label 94% of the way
    # up and its own height pushed it out of frame entirely. 66% of the half-height keeps
    # it clear of both the top edge and the tower.
    txt.location = (0.0, (36.0 / lens * 2.6 / 2.0) * 0.66, -2.6)

    rw = sc.rigidbody_world
    rw.point_cache.frame_end = sc.frame_end
    rw.substeps_per_frame = 12
    rw.solver_iterations = 20

    if PREVIEW:
        sc.frame_set(PREVIEW)
        sc.render.image_settings.file_format = "PNG"
        sc.render.filepath = f"{OUT}/preview.png"
        bpy.ops.render.render(write_still=True)
        print(f"SCENE_OK preview={PREVIEW} block=({w:.2f}x{d:.2f}x{h:.2f}) "
              f"hit=({hit_x:.2f},{hit_z:.2f}) cam_dist={cam_dist:.2f}")
        return

    # ---- bake, and record the frame the ball first touches the block: the app needs
    # the real contact frame to place its impact sound, not an estimate.
    contact = None
    dg = bpy.context.evaluated_depsgraph_get()
    for f in range(1, sc.frame_end + 1):
        sc.frame_set(f)
        dg.update()
        bx = ball.evaluated_get(dg).matrix_world.translation.x
        if contact is None and bx + radius >= -w / 2.0 - 0.02:
            contact = f
    (bpy.path.abspath(f"{OUT}/impacts.json") and None)
    with open(f"{OUT}/impacts.json", "w", encoding="utf-8") as fh:
        json.dump({"contact_frame": contact, "fps": FPS,
                   "contact_seconds": round((contact or 0) / FPS, 3),
                   "mass": MASS, "label": LABEL}, fh, indent=1)

    sc.render.image_settings.file_format = "PNG"
    sc.render.image_settings.color_mode = "RGB"
    sc.render.filepath = f"{OUT}/frame_"
    bpy.ops.render.render(animation=True)
    print(f"SCENE_OK mass={MASS} label={LABEL} frames={sc.frame_end} contact={contact}")


main()
