"""Reusable low-poly building blocks, imported by generated shot scripts inside Blender.

The first version of this mode let the model invent geometry, staging and camera from
scratch for every shot. It produced valid scripts that rendered floating rectangles and
close-ups of nothing, because nothing in a text contract tells you whether your camera is
actually looking at your subject.

So the parts that are hard to get right blind live here, written once:

  * `cat()` - one low-poly cat, always the same proportions, with named parts and poses.
  * `frame()` - solves the camera from the BOUNDING BOX of whatever you pass it. A shot
    can no longer point at empty space; that failure mode is gone by construction.
  * `setup()` / `finish()` - the render contract (EEVEE, 9:16, preview frame, PNG frames,
    SCENE_OK), so a generated script cannot get it subtly wrong.

A shot script is then only composition: put the cat here, put a box there, frame it.
"""

import json
import math
import os
import sys

import bpy

# ---------------------------------------------------------------- materials


def mat(name, rgb, rough=0.85, emit=0.0):
    m = bpy.data.materials.get(name)
    if m:
        return m
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    b = m.node_tree.nodes["Principled BSDF"]
    b.inputs["Base Color"].default_value = (*rgb, 1)
    b.inputs["Roughness"].default_value = rough
    if emit:
        b.inputs["Emission Color"].default_value = (*rgb, 1)
        b.inputs["Emission Strength"].default_value = emit
    return m


FUR = (0.62, 0.45, 0.30)
FUR_DARK = (0.30, 0.24, 0.20)
FUR_GREY = (0.55, 0.55, 0.58)
FUR_WHITE = (0.88, 0.86, 0.82)
PINK = (0.90, 0.55, 0.58)
DARK = (0.07, 0.07, 0.09)


def _limb(name, size, joint, material, parent=None, axis="z"):
    """A limb whose ORIGIN sits at its joint, with the geometry hanging off it.

    A box rotates about its own centre, so a leg keyed to swing pivots at its knee and a
    tail swings away from the body entirely - which is exactly what the first cat did.
    Moving the mesh off the origin puts the pivot where the joint is.
    """
    bpy.ops.mesh.primitive_cube_add(size=1, location=joint)
    o = bpy.context.object
    o.name = name
    o.scale = size
    o.data.materials.append(material)
    # push the geometry down (or back) by half its length, in LOCAL space
    off = {"z": (0, 0, -0.5), "y": (0, 0.5, 0)}[axis]
    for v in o.data.vertices:
        v.co.x += off[0]
        v.co.y += off[1]
        v.co.z += off[2]
    if parent is not None:
        o.parent = parent
        o.matrix_parent_inverse = parent.matrix_world.inverted()
    return o


def _box(name, size, loc, material, parent=None, rot=(0, 0, 0)):
    """A box given by its real DIMENSIONS, not a scale factor.

    primitive_cube_add(size=1) already spans one unit, so passing a scale of 0.5 makes a
    half-size box - a trap that cost a whole rebuild in the physics scenes.
    """
    bpy.ops.mesh.primitive_cube_add(size=1, location=loc)
    o = bpy.context.object
    o.name = name
    o.scale = size
    o.rotation_euler = rot
    o.data.materials.append(material)
    if parent is not None:
        o.parent = parent
        o.matrix_parent_inverse = parent.matrix_world.inverted()
    return o


def _cone(name, radius, depth, loc, material, parent=None, rot=(0, 0, 0), verts=3):
    bpy.ops.mesh.primitive_cone_add(vertices=verts, radius1=radius, depth=depth,
                                    location=loc, rotation=rot)
    o = bpy.context.object
    o.name = name
    o.data.materials.append(material)
    if parent is not None:
        o.parent = parent
        o.matrix_parent_inverse = parent.matrix_world.inverted()
    return o


# ---------------------------------------------------------------- the cat


class Cat:
    """One low-poly cat. `root` is an empty at its feet - move and rotate THAT.

    Every part is kept as an attribute so a shot can pose the head, tail or a leg without
    guessing object names.
    """

    def __init__(self, root, parts, scale=1.0):
        self.root = root
        self.parts = parts
        self.scale = scale
        for k, v in parts.items():
            setattr(self, k, v)

    # --- placement
    def place(self, x=0.0, y=0.0, z=0.0, facing_deg=0.0):
        self.root.location = (x, y, z)
        self.root.rotation_euler = (0, 0, math.radians(facing_deg))
        return self

    def key(self, frame):
        """Keyframe the whole cat as it currently stands."""
        for o in [self.root] + list(self.parts.values()):
            o.keyframe_insert("location", frame=frame)
            o.keyframe_insert("rotation_euler", frame=frame)
        return self

    # --- poses
    def pose_stand(self):
        for leg in (self.leg_fl, self.leg_fr, self.leg_bl, self.leg_br):
            leg.rotation_euler = (0, 0, 0)
        self.body.rotation_euler = (0, 0, 0)
        self.head.rotation_euler = (0, 0, 0)
        return self

    def pose_sit(self):
        """Rear folded FORWARD under the body, front legs straight, chest up.

        The cat faces -Y, and a limb hangs below its hip joint, so a POSITIVE rotation
        about X swings the paw backwards - which laid the first attempt flat on its side.
        Folding under the body is negative, and the root must not sink or the whole cat
        drops through the floor with it.
        """
        self.root.location.z = 0.0
        self.body.rotation_euler = (math.radians(-22), 0, 0)
        self.body.location.z = self._body_z - 0.06 * self.scale
        for leg in (self.leg_bl, self.leg_br):
            leg.rotation_euler = (math.radians(-72), 0, 0)
        for leg in (self.leg_fl, self.leg_fr):
            leg.rotation_euler = (0, 0, 0)
        self.head.rotation_euler = (math.radians(10), 0, 0)
        self.tail.rotation_euler = (math.radians(6), 0, 0)
        return self

    def pose_walk(self, phase):
        """`phase` 0..1 through one stride. Diagonal pairs, as cats actually move."""
        a = math.radians(26) * math.sin(2 * math.pi * phase)
        self.leg_fl.rotation_euler = (a, 0, 0)
        self.leg_br.rotation_euler = (a, 0, 0)
        self.leg_fr.rotation_euler = (-a, 0, 0)
        self.leg_bl.rotation_euler = (-a, 0, 0)
        # a small vertical bob at twice the stride rate reads as weight
        self.body.location.z = self._body_z + 0.012 * self.scale * math.sin(
            4 * math.pi * phase)
        self.tail.rotation_euler = (math.radians(-18),
                                    math.radians(9) * math.sin(2 * math.pi * phase), 0)
        return self

    def look(self, yaw_deg=0.0, pitch_deg=0.0):
        self.head.rotation_euler = (math.radians(pitch_deg), 0, math.radians(yaw_deg))
        return self

    def tail_sway(self, phase, amount_deg=22.0):
        self.tail.rotation_euler = (math.radians(-25), 0,
                                    math.radians(amount_deg) * math.sin(2 * math.pi * phase))
        return self

    def objects(self):
        return [self.root] + list(self.parts.values())


def cat(color=FUR, scale=1.0, name="Cat"):
    """Build a cat. Returns a `Cat`; move it with .place(), pose it with .pose_*()."""
    fur = mat(f"{name}_fur", color)
    dark = mat(f"{name}_dark", DARK, rough=0.5)
    pink = mat(f"{name}_pink", PINK)

    bpy.ops.object.empty_add(type="PLAIN_AXES", location=(0, 0, 0))
    root = bpy.context.object
    root.name = name
    root.empty_display_size = 0.15

    s = scale
    body_z = 0.30 * s
    body = _box(f"{name}_body", (0.30 * s, 0.60 * s, 0.26 * s), (0, 0, body_z), fur, root)
    head = _box(f"{name}_head", (0.26 * s, 0.24 * s, 0.24 * s),
                (0, -0.40 * s, 0.42 * s), fur, root)
    _box(f"{name}_muzzle", (0.14 * s, 0.09 * s, 0.10 * s),
         (0, -0.54 * s, 0.37 * s), fur, head)
    _box(f"{name}_nose", (0.05 * s, 0.03 * s, 0.035 * s),
         (0, -0.59 * s, 0.39 * s), pink, head)
    for side in (-1, 1):
        _box(f"{name}_eye{side}", (0.055 * s, 0.02 * s, 0.055 * s),
             (0.075 * s * side, -0.52 * s, 0.46 * s), dark, head)
        # three-sided cones: an ear is a triangle, and three verts is as low-poly as it gets
        _cone(f"{name}_ear{side}", 0.085 * s, 0.16 * s,
              (0.095 * s * side, -0.36 * s, 0.57 * s), fur, head,
              rot=(0, 0, math.radians(90)))
    legs = {}
    for key, (dx, dy) in {"leg_fl": (-1, -1), "leg_fr": (1, -1),
                          "leg_bl": (-1, 1), "leg_br": (1, 1)}.items():
        # jointed at the shoulder/hip, hanging down to the floor
        legs[key] = _limb(f"{name}_{key}", (0.09 * s, 0.09 * s, 0.30 * s),
                          (0.11 * s * dx, 0.22 * s * dy, 0.30 * s), fur, root, axis="z")
    # jointed where it meets the body, running backwards
    tail = _limb(f"{name}_tail", (0.07 * s, 0.42 * s, 0.07 * s),
                 (0, 0.28 * s, 0.38 * s), fur, root, axis="y")
    tail.rotation_euler = (math.radians(-28), 0, 0)
    parts = {"body": body, "head": head, "tail": tail, **legs}
    c = Cat(root, parts, scale=s)
    c._body_z = body_z
    return c


# ---------------------------------------------------------------- world


def ground(size=40.0, color=(0.10, 0.11, 0.14)):
    bpy.ops.mesh.primitive_plane_add(size=size, location=(0, 0, 0))
    o = bpy.context.object
    o.name = "Ground"
    o.data.materials.append(mat("Ground", color))
    return o


def prop_box(size, loc, color=(0.45, 0.35, 0.28), rot_deg=0.0, name="Prop"):
    """A box prop sitting ON the ground: give the size, it works out its own height."""
    return _box(name, size, (loc[0], loc[1], size[2] / 2.0),
                mat(f"{name}_m", color), rot=(0, 0, math.radians(rot_deg)))


def lights(strength=3.0):
    """Key and fill, both invisible to camera.

    An area light is geometry to the renderer and shows up as a glowing rectangle in the
    shot unless you say otherwise - it happened, on camera, in an earlier build.
    """
    bpy.ops.object.light_add(type="AREA", location=(4, -5, 7))
    k = bpy.context.object
    k.data.energy = 900 * strength
    k.data.size = 8
    k.rotation_euler = (math.radians(30), 0, math.radians(20))
    k.visible_camera = False
    bpy.ops.object.light_add(type="AREA", location=(-5, -3, 4))
    f = bpy.context.object
    f.data.energy = 260 * strength
    f.data.size = 6
    f.visible_camera = False
    return k, f


def _bounds(objects):
    xs, ys, zs = [], [], []
    deps = bpy.context.evaluated_depsgraph_get()
    for o in objects:
        if o.type == "EMPTY":
            continue
        ob = o.evaluated_get(deps)
        for corner in ob.bound_box:
            v = ob.matrix_world @ __import__("mathutils").Vector(corner)
            xs.append(v.x), ys.append(v.y), zs.append(v.z)
    if not xs:
        return (0, 0, 0), 1.0
    centre = ((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2, (min(zs) + max(zs)) / 2)
    span = max(max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs), 0.2)
    return centre, span


def frame(subjects, fill=0.5, lens=42.0, yaw_deg=-35.0, pitch_deg=68.0):
    """Point the camera at `subjects` and back off until they fill `fill` of the height.

    Blender fits the sensor to the LARGER resolution axis, so in 9:16 the visible height
    is 36/lens * distance - solve that for the distance instead of guessing one. This is
    the whole reason a generated shot can no longer render empty background.
    """
    import mathutils
    if not isinstance(subjects, (list, tuple)):
        subjects = [subjects]
    flat = []
    for s in subjects:
        flat.extend(s.objects() if isinstance(s, Cat) else [s])
    centre, span = _bounds(flat)
    dist = (span / max(0.15, fill)) * lens / 36.0
    yaw, pitch = math.radians(yaw_deg), math.radians(pitch_deg)
    offset = mathutils.Vector((math.sin(yaw) * math.sin(pitch),
                               -math.cos(yaw) * math.sin(pitch),
                               math.cos(pitch))) * dist
    bpy.ops.object.camera_add(location=(centre[0] + offset.x, centre[1] + offset.y,
                                        centre[2] + offset.z))
    cam = bpy.context.object
    cam.data.lens = lens
    direction = mathutils.Vector(centre) - cam.location
    cam.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()
    bpy.context.scene.camera = cam
    return cam


# ---------------------------------------------------------------- render contract


def params():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    return json.loads(argv[0]) if argv else {}


def setup(P, world_color=(0.03, 0.035, 0.05)):
    """Fresh scene, EEVEE, 9:16, linear keys. Returns the scene."""
    bpy.ops.wm.read_factory_settings(use_empty=True)
    sc = bpy.context.scene
    try:
        sc.render.engine = "BLENDER_EEVEE_NEXT"
    except TypeError:
        sc.render.engine = "CYCLES"
        sc.cycles.samples = int(P.get("samples", 8))
    sc.render.resolution_x = int(P.get("res_x", 540))
    sc.render.resolution_y = int(P.get("res_y", 960))
    sc.render.fps = int(P.get("fps", 24))
    sc.frame_start = 1
    sc.frame_end = max(1, int(float(P.get("seconds", 3.0)) * sc.render.fps))
    sc.world = bpy.data.worlds.new("W")
    sc.world.use_nodes = True
    sc.world.node_tree.nodes["Background"].inputs[0].default_value = (*world_color, 1)
    bpy.context.preferences.edit.keyframe_new_interpolation_type = "LINEAR"
    return sc


def finish(P, sc, note=""):
    """Render: one still for a preview frame, otherwise the PNG sequence. Prints SCENE_OK."""
    out = P.get("out_dir", "//out")
    preview = int(P.get("preview_frame", 0))
    sc.render.image_settings.file_format = "PNG"
    sc.render.image_settings.color_mode = "RGB"
    if preview:
        sc.frame_set(min(preview, sc.frame_end))
        sc.render.filepath = os.path.join(out, "preview.png")
        bpy.ops.render.render(write_still=True)
        print(f"SCENE_OK preview={preview} {note}")
        return
    sc.render.filepath = os.path.join(out, "frame_")
    bpy.ops.render.render(animation=True)
    print(f"SCENE_OK frames={sc.frame_end} {note}")
