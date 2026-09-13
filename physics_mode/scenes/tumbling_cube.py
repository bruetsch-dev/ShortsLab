"""A cube tumbling along a tiled floor, dropping into glowing gaps and rolling back out.

    blender -b -noaudio -P tumbling_cube.py -- '{"loop_tiles": 4, "out_dir": "..."}'

This one is CHOREOGRAPHED, not simulated, and that is deliberate. The whole appeal of the
format is that the cube slots into each gap exactly - a rigid-body solver tumbling a cube
down a floor diverges within two rolls and never lands square again. The motion is still
derived from real cube physics: each roll pivots about the leading bottom EDGE, which is
how a cube actually tips, so it reads as weight rather than as a sliding sprite.

It renders ONE loop period, which the app repeats. Because the tile pattern repeats every
`hole_every` tiles and the camera tracks at constant speed, the last frame lands exactly
where the first began - the render loops seamlessly with no crossfade. (The reference
clip this was built from does not: it cuts mid-tumble.)

Landing frames go to impacts.json so the app can put a sound on each one.
"""

import json
import math
import sys

import bpy

argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
P = json.loads(argv[0]) if argv else {}
OUT = P.get("out_dir", "//out")
RES_X, RES_Y = int(P.get("res_x", 1080)), int(P.get("res_y", 1920))
SAMPLES = int(P.get("samples", 24))
FPS = int(P.get("fps", 30))
TILE = float(P.get("tile", 1.0))
ROLL_FRAMES = int(P.get("roll_frames", 18))     # frames for one 90-degree tip
LOOP_TILES = int(P.get("loop_tiles", 6))        # rolls per rendered loop
HOLE_EVERY = int(P.get("hole_every", 3))        # every Nth tile is a gap
DROP = float(P.get("drop", 0.42))               # how deep the cube sits in a gap
PREVIEW = int(P.get("preview_frame", 0))

# The pattern must divide the loop, otherwise the last frame does not match the first.
if LOOP_TILES % HOLE_EVERY:
    LOOP_TILES = HOLE_EVERY * max(1, round(LOOP_TILES / HOLE_EVERY))


def is_hole(index: int) -> bool:
    return index % HOLE_EVERY == 0


def mat(name, rgba, rough=0.75, emit=0.0):
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    b = m.node_tree.nodes["Principled BSDF"]
    b.inputs["Base Color"].default_value = rgba
    b.inputs["Roughness"].default_value = rough
    if emit:
        b.inputs["Emission Color"].default_value = rgba
        b.inputs["Emission Strength"].default_value = emit
    return m


def smoothstep(x: float) -> float:
    x = min(1.0, max(0.0, x))
    return x * x * (3 - 2 * x)


def roll_pose(phase: float, start_x: float):
    """Cube centre and rotation `phase` (0..1) through one tip over its leading edge.

    The pivot is the bottom edge the cube rolls over, at start_x + TILE/2 on the floor.
    Rotating the centre about that edge is the entire motion - no separate translation.
    """
    ang = (math.pi / 2) * phase
    px = start_x + TILE / 2.0
    vx, vz = -TILE / 2.0, TILE / 2.0          # pivot -> centre, before rotating
    cx = px + vx * math.cos(ang) + vz * math.sin(ang)
    cz = -vx * math.sin(ang) + vz * math.cos(ang)
    return cx, cz, ang


def main():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    sc = bpy.context.scene
    sc.render.engine = "CYCLES"
    sc.cycles.samples = SAMPLES
    sc.cycles.use_denoising = True
    sc.render.resolution_x, sc.render.resolution_y = RES_X, RES_Y
    sc.render.fps = FPS
    total = LOOP_TILES * ROLL_FRAMES
    sc.frame_start = 1
    sc.frame_end = total

    try:
        prefs = bpy.context.preferences.addons["cycles"].preferences
        for backend in ("OPTIX", "CUDA", "HIP", "ONEAPI"):
            try:
                prefs.compute_device_type = backend
                prefs.refresh_devices()
            except Exception:  # noqa: BLE001
                continue
            if [d for d in prefs.devices if d.type == backend]:
                for d in prefs.devices:
                    d.use = d.type in (backend, "CPU")
                sc.cycles.device = "GPU"
                print(f"CYCLES_DEVICE {backend}")
                break
    except KeyError:
        pass

    # Linear keys, set BEFORE inserting any: there is a keyframe on every single frame
    # and the pose function already carries the easing, so bezier handles would only add
    # overshoot between them. Blender 5.2 moved Action.fcurves behind slotted actions, so
    # fixing interpolation after the fact is no longer a one-liner.
    bpy.context.preferences.edit.keyframe_new_interpolation_type = "LINEAR"

    sc.world = bpy.data.worlds.new("W")
    sc.world.use_nodes = True
    sc.world.node_tree.nodes["Background"].inputs[0].default_value = (0.02, 0.02, 0.03, 1)

    stone = mat("Stone", (0.30, 0.20, 0.26, 1), rough=0.85)
    pit = mat("Pit", (0.05, 0.05, 0.07, 1), rough=0.9)
    # Emission 3.2, not 14: at high strength the gap clips to pure white and reads as a
    # bright TILE rather than as light coming up out of a hole.
    glow = mat("Glow", (0.05, 0.45, 1.0, 1), rough=0.4, emit=3.2)

    # ---- floor: a continuous tiled surface, holes only in the row the cube travels.
    # Scattering gaps across every row turned the floor into a checkerboard with nothing
    # solid to read the cube's path against.
    lo, hi = -12, LOOP_TILES + 16
    depth = DROP + 0.55
    for i in range(lo, hi):
        for j in (-3, -2, -1, 0, 1, 2, 3):
            if j == 0 and is_hole(i):
                # the shaft: four walls and a glowing floor, so it is visibly a recess
                # scale IS the dimension here: primitive_cube_add(size=1) already spans
                # one unit, so a scale of 0.5 makes a half-size tile, not a full one.
                for dx, dy, sx, sy in ((-0.5, 0, 0.06, 1.0), (0.5, 0, 0.06, 1.0),
                                       (0, -0.5, 1.0, 0.06), (0, 0.5, 1.0, 0.06)):
                    bpy.ops.mesh.primitive_cube_add(
                        size=1, location=(i * TILE + dx * TILE * 0.97,
                                          dy * TILE * 0.97, -depth / 2))
                    wobj = bpy.context.object
                    wobj.scale = (TILE * sx * 0.97, TILE * sy * 0.97, depth)
                    wobj.data.materials.append(pit)
                bpy.ops.mesh.primitive_plane_add(size=TILE * 0.94,
                                                 location=(i * TILE, 0, -depth + 0.01))
                bpy.context.object.data.materials.append(glow)
                continue
            bpy.ops.mesh.primitive_cube_add(size=1, location=(i * TILE, j * TILE, -0.25))
            t = bpy.context.object
            t.scale = (TILE * 0.97, TILE * 0.97, 0.5)
            t.data.materials.append(stone)

    # ---- the cube, same size as a tile so it fits a gap exactly
    bpy.ops.mesh.primitive_cube_add(size=TILE * 0.96, location=(0, 0, TILE / 2))
    cube = bpy.context.object
    cube.name = "Cube"
    cube.data.materials.append(mat("CubeStone", (0.42, 0.28, 0.34, 1), rough=0.8))
    bpy.ops.object.modifier_add(type="BEVEL")
    cube.modifiers["Bevel"].width = TILE * 0.02
    cube.modifiers["Bevel"].segments = 3
    # Flat shading on purpose: a cube's appeal is its hard edges. The bevel only catches a
    # highlight along them so they read against the dark background.

    # ---- animate: one keyframe per frame. The pose is a closed-form function of the
    # frame, so the loop is exact by construction rather than by trimming afterwards.
    landings = []
    for f in range(1, total + 1):
        k = (f - 1) / ROLL_FRAMES
        roll = int(k)
        phase = k - roll
        cx, cz, ang = roll_pose(phase, roll * TILE)
        # Sitting in a gap: eased down over the last third of the roll that lands in it,
        # and back up over the first third of the roll that leaves it.
        dz = 0.0
        if is_hole(roll + 1):
            dz -= DROP * smoothstep((phase - 0.62) / 0.38)
        if is_hole(roll):
            dz -= DROP * (1.0 - smoothstep(phase / 0.38))
        cube.location = (cx, 0.0, cz + dz)
        cube.rotation_euler = (0.0, roll * (math.pi / 2) + ang, 0.0)
        cube.keyframe_insert("location", frame=f)
        cube.keyframe_insert("rotation_euler", frame=f)
        if phase == 0.0 and f > 1:
            landings.append({"frame": f, "kind": "hole" if is_hole(roll) else "tile",
                             "strength": 1.0 if is_hole(roll) else 0.6})

    # ---- camera tracks at constant speed: one tile per roll, so over the loop it moves
    # exactly the pattern period and the framing returns to where it started.
    bpy.ops.object.camera_add(location=(0, 0, 0))
    cam = bpy.context.object
    cam.data.lens = 42
    sc.camera = cam
    for f in range(1, total + 1):
        x = ((f - 1) / ROLL_FRAMES) * TILE
        cam.location = (x + 2.2, -7.6, 7.4)
        cam.rotation_euler = (math.radians(46), 0, math.radians(16))
        cam.keyframe_insert("location", frame=f)
        cam.keyframe_insert("rotation_euler", frame=f)

    bpy.ops.object.light_add(type="AREA", location=(3, -5, 7))
    key = bpy.context.object
    key.data.energy = 900
    key.data.size = 7
    key.rotation_euler = (math.radians(28), 0, 0)
    bpy.ops.object.light_add(type="AREA", location=(-4, -3, 4))
    fill = bpy.context.object
    fill.data.energy = 260
    fill.data.size = 6

    if PREVIEW:
        sc.frame_set(PREVIEW)
        sc.render.image_settings.file_format = "PNG"
        sc.render.filepath = f"{OUT}/preview.png"
        bpy.ops.render.render(write_still=True)
        print(f"SCENE_OK preview={PREVIEW} tiles={LOOP_TILES} frames={total}")
        return

    with open(f"{OUT}/impacts.json", "w", encoding="utf-8") as fh:
        json.dump({"fps": FPS, "loop_frames": total, "events": landings}, fh, indent=1)

    sc.render.image_settings.file_format = "PNG"
    sc.render.image_settings.color_mode = "RGB"
    sc.render.filepath = f"{OUT}/frame_"
    bpy.ops.render.render(animation=True)
    print(f"SCENE_OK loop_tiles={LOOP_TILES} frames={total} landings={len(landings)}")


main()
