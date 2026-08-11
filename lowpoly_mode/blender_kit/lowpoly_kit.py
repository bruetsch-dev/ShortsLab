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


# ---------------------------------------------------------------- creatures
# ONE parametric builder, not a library of animals. The model supplies proportions - how
# long the body is, how many legs, whether there are ears, a tail, a snout - and this
# assembles them. That keeps "the model never writes geometry" true while still allowing a
# cat, a dog, a bird, a person or a cow, which a cat-only kit could not.


class Creature:
    """A built figure. `root` is an empty at its feet - move and rotate THAT.

    `legs` is a list in build order, so the poses below work whether there are two of them
    or four.
    """

    def __init__(self, root, parts, legs, scale=1.0, body_z=0.3, biped=False):
        self.root = root
        self.parts = parts
        self.legs = legs
        self.scale = scale
        self._body_z = body_z
        self.biped = biped
        for k, v in parts.items():
            setattr(self, k, v)

    def place(self, x=0.0, y=0.0, z=0.0, facing_deg=0.0):
        self.root.location = (x, y, z)
        self.root.rotation_euler = (0, 0, math.radians(facing_deg))
        return self

    def key(self, frame):
        for o in [self.root] + list(self.parts.values()):
            o.keyframe_insert("location", frame=frame)
            o.keyframe_insert("rotation_euler", frame=frame)
        return self

    def pose_stand(self):
        for leg in self.legs:
            leg.rotation_euler = (0, 0, 0)
        self.body.rotation_euler = (0, 0, 0)
        self.body.location.z = self._body_z
        self.head.rotation_euler = (0, 0, 0)
        return self

    def pose_sit(self):
        """Rear folded forward under the body, front upright, chest lifted.

        The figure faces -Y and a limb hangs below its joint, so folding it UNDER the body
        is a NEGATIVE rotation about X. Getting that sign wrong lays the whole thing on its
        side, which is exactly how the first attempt looked.
        """
        self.body.rotation_euler = (math.radians(-22), 0, 0)
        self.body.location.z = self._body_z - 0.06 * self.scale
        for leg in (self.legs[2:] if len(self.legs) > 2 else []):
            leg.rotation_euler = (math.radians(-72), 0, 0)
        for leg in self.legs[:2]:
            leg.rotation_euler = (0, 0, 0)
        self.head.rotation_euler = (math.radians(10), 0, 0)
        if getattr(self, "tail", None):
            self.tail.rotation_euler = (math.radians(6), 0, 0)
        return self

    def pose_walk(self, phase):
        """One stride. Four legs move in diagonal pairs; two legs alternate."""
        a = math.radians(26) * math.sin(2 * math.pi * phase)
        if len(self.legs) >= 4:
            self.legs[0].rotation_euler = (a, 0, 0)
            self.legs[3].rotation_euler = (a, 0, 0)
            self.legs[1].rotation_euler = (-a, 0, 0)
            self.legs[2].rotation_euler = (-a, 0, 0)
        else:
            for i, leg in enumerate(self.legs):
                leg.rotation_euler = ((a if i % 2 == 0 else -a), 0, 0)
        self.body.location.z = self._body_z + 0.012 * self.scale * math.sin(
            4 * math.pi * phase)
        if getattr(self, "tail", None):
            self.tail.rotation_euler = (math.radians(-18),
                                        math.radians(9) * math.sin(2 * math.pi * phase), 0)
        return self

    def look(self, yaw_deg=0.0, pitch_deg=0.0):
        self.head.rotation_euler = (math.radians(pitch_deg), 0, math.radians(yaw_deg))
        return self

    def tail_sway(self, phase, amount_deg=22.0):
        if getattr(self, "tail", None):
            self.tail.rotation_euler = (
                math.radians(-25), 0,
                math.radians(amount_deg) * math.sin(2 * math.pi * phase))
        return self

    def objects(self):
        return [self.root] + list(self.parts.values())


# Proportions only - no geometry. A new animal is a new row here, not new code.
SPECIES = {
    "cat":   dict(body=(0.30, 0.60, 0.26), head=0.24, legs=4, leg=(0.09, 0.30),
                  ears="pointy", tail="long", snout=True, neck=0.0),
    "dog":   dict(body=(0.34, 0.72, 0.32), head=0.28, legs=4, leg=(0.11, 0.34),
                  ears="floppy", tail="long", snout=True, neck=0.0),
    "mouse": dict(body=(0.18, 0.30, 0.16), head=0.15, legs=4, leg=(0.05, 0.12),
                  ears="round", tail="long", snout=True, neck=0.0),
    "bear":  dict(body=(0.52, 0.90, 0.50), head=0.36, legs=4, leg=(0.16, 0.36),
                  ears="round", tail="none", snout=True, neck=0.0),
    "cow":   dict(body=(0.46, 1.00, 0.46), head=0.30, legs=4, leg=(0.12, 0.46),
                  ears="round", tail="long", snout=True, neck=0.10),
    # Bipeds get a NECK of almost nothing. The gap is measured in the same units as the
    # head, so 0.08 left the head visibly floating clear of the shoulders.
    "bird":  dict(body=(0.26, 0.30, 0.30), head=0.16, legs=2, leg=(0.045, 0.13),
                  ears="none", tail="short", snout=True, neck=0.01, wings=True),
    "human": dict(body=(0.34, 0.22, 0.62), head=0.26, legs=2, leg=(0.12, 0.52),
                  ears="none", tail="none", snout=False, neck=0.015, arms=True),
    "robot": dict(body=(0.40, 0.28, 0.58), head=0.30, legs=2, leg=(0.14, 0.46),
                  ears="none", tail="none", snout=False, neck=0.02, arms=True),
}


def creature(kind="cat", color=None, scale=1.0, name=None, **overrides):
    """Build a figure from SPECIES proportions, with any value overridable.

    An unknown kind falls back to the cat proportions rather than raising: an odd-looking
    animal still carries a shot, an exception does not.
    """
    spec = dict(SPECIES.get(str(kind).lower(), SPECIES["cat"]))
    spec.update({k: v for k, v in overrides.items() if v is not None})
    name = name or str(kind).title()
    fur = mat(name + "_skin", color or FUR)
    dark = mat(name + "_dark", DARK, rough=0.5)
    pink = mat(name + "_pink", PINK)

    bpy.ops.object.empty_add(type="PLAIN_AXES", location=(0, 0, 0))
    root = bpy.context.object
    root.name = name
    root.empty_display_size = 0.15

    s = float(scale)
    bw, bl, bh = [v * s for v in spec["body"]]
    lw, ll = [v * s for v in spec["leg"]]
    biped = int(spec.get("legs", 4)) <= 2
    hs = spec["head"] * s
    neck = float(spec.get("neck", 0.0)) * s

    body_z = ll + bh / 2
    body = _box(name + "_body", (bw, bl, bh), (0, 0, body_z), fur, root)
    head_y = 0.0 if biped else -(bl / 2 + hs / 2) * 0.85
    head_z = body_z + (bh / 2 + hs / 2 + neck if biped else bh * 0.35 + neck)
    head = _box(name + "_head", (hs, hs * 0.95, hs), (0, head_y, head_z), fur, root)

    parts = {"body": body, "head": head}
    if spec.get("snout", True):
        _box(name + "_snout", (hs * 0.55, hs * 0.40, hs * 0.42),
             (0, head_y - hs * 0.62, head_z - hs * 0.16), fur, head)
        _box(name + "_nose", (hs * 0.20, hs * 0.12, hs * 0.14),
             (0, head_y - hs * 0.84, head_z - hs * 0.10), pink, head)
    for side in (-1, 1):
        _box(name + "_eye" + str(side), (hs * 0.22, hs * 0.08, hs * 0.22),
             (hs * 0.28 * side, head_y - hs * 0.52, head_z + hs * 0.14), dark, head)

    ears = str(spec.get("ears", "none")).lower()
    if ears == "pointy":
        for side in (-1, 1):
            _cone(name + "_ear" + str(side), hs * 0.34, hs * 0.66,
                  (hs * 0.36 * side, head_y + hs * 0.10, head_z + hs * 0.72), fur, head,
                  rot=(0, 0, math.radians(90)))
    elif ears in ("round", "floppy"):
        for side in (-1, 1):
            _box(name + "_ear" + str(side), (hs * 0.10, hs * 0.30, hs * 0.34),
                 (hs * (0.55 if ears == "floppy" else 0.50) * side, head_y,
                  head_z + (hs * 0.10 if ears == "floppy" else hs * 0.60)), fur, head)

    legs = []
    slots = ([(-1, -1), (1, -1), (-1, 1), (1, 1)] if int(spec.get("legs", 4)) >= 4
             else [(-1, 0), (1, 0)])
    for i, (dx, dy) in enumerate(slots):
        legs.append(_limb(name + "_leg" + str(i), (lw, lw, ll),
                          (bw * 0.38 * dx, bl * 0.36 * dy, ll), fur, root, axis="z"))
    if spec.get("arms"):
        for i, dx in enumerate((-1, 1)):
            parts["arm" + str(i)] = _limb(
                name + "_arm" + str(i), (lw * 0.8, lw * 0.8, bh * 0.75),
                (bw * 0.62 * dx, 0, body_z + bh * 0.36), fur, root, axis="z")
    if spec.get("wings"):
        for i, dx in enumerate((-1, 1)):
            parts["wing" + str(i)] = _box(
                name + "_wing" + str(i), (bw * 0.20, bl * 0.75, bh * 0.30),
                (bw * 0.60 * dx, 0, body_z), fur, root)

    tail_kind = str(spec.get("tail", "none")).lower()
    if tail_kind != "none":
        tl = bl * (0.75 if tail_kind == "long" else 0.30)
        parts["tail"] = _limb(name + "_tail", (lw * 0.75, tl, lw * 0.75),
                              (0, bl * 0.45, body_z + bh * 0.20), fur, root, axis="y")
        parts["tail"].rotation_euler = (math.radians(-28), 0, 0)

    for i, leg in enumerate(legs):
        parts["leg" + str(i)] = leg
    return Creature(root, parts, legs, scale=s, body_z=body_z, biped=biped)


def cat(color=FUR, scale=1.0, name="Cat"):
    """Shortcut kept because plenty of shots ask for exactly this."""
    return creature("cat", color=color, scale=scale, name=name)


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
    # Force the transforms to catch up first. Framing runs after the animation has been
    # keyed, and without this the world matrices are still whatever they were when the
    # objects were built - a cat that walks in from the left gets framed at the position
    # it never occupies, and drops out of the shot entirely.
    bpy.context.view_layer.update()
    deps = bpy.context.evaluated_depsgraph_get()
    deps.update()
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
        flat.extend(s.objects() if isinstance(s, Creature) else [s])
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

# ---------------------------------------------------------------- props
# A fixed, small catalogue. The model picks from it by name; it never invents geometry,
# which is what made the first version render floating rectangles.

PALETTE = {
    "wood": (0.42, 0.30, 0.20), "cream": (0.80, 0.76, 0.68),
    "red": (0.62, 0.20, 0.18), "blue": (0.22, 0.34, 0.58),
    "green": (0.24, 0.44, 0.30), "grey": (0.34, 0.35, 0.38),
    "white": (0.85, 0.85, 0.84), "black": (0.10, 0.10, 0.12),
    "pink": (0.82, 0.52, 0.56), "yellow": (0.85, 0.70, 0.25),
}


def _colour(name, default=(0.45, 0.35, 0.28)):
    return PALETTE.get(str(name or "").lower(), default)


def prop(kind, at=(0.0, 0.0), colour="wood", size=1.0, name=None):
    """One prop from the catalogue, sitting on the floor at `at`.

    Returns the object (or a list for multi-part props) so a shot can frame it and the
    cat together. Unknown kinds fall back to a plain box rather than failing the render.
    """
    kind = str(kind or "box").lower()
    c = mat(f"p_{kind}_{colour}", _colour(colour))
    x, y = float(at[0]), float(at[1])
    n = name or f"{kind}_{abs(hash((kind, x, y))) % 9999}"
    s_ = float(size)
    if kind in ("box", "crate", "cardboard_box"):
        h = 0.55 * s_
        return _box(n, (0.7 * s_, 0.7 * s_, h), (x, y, h / 2), c)
    if kind in ("bowl", "food_bowl"):
        bpy.ops.mesh.primitive_cylinder_add(vertices=8, radius=0.16 * s_, depth=0.10 * s_,
                                            location=(x, y, 0.05 * s_))
        o = bpy.context.object
        o.name = n
        o.data.materials.append(c)
        return o
    if kind in ("ball", "yarn", "toy"):
        bpy.ops.mesh.primitive_ico_sphere_add(subdivisions=1, radius=0.13 * s_,
                                              location=(x, y, 0.13 * s_))
        o = bpy.context.object
        o.name = n
        o.data.materials.append(c)
        return o
    if kind in ("table", "desk"):
        top_z = 0.62 * s_
        parts = [_box(n, (1.1 * s_, 0.7 * s_, 0.07 * s_), (x, y, top_z), c)]
        for dx in (-1, 1):
            for dy in (-1, 1):
                parts.append(_box(f"{n}_leg{dx}{dy}", (0.07 * s_, 0.07 * s_, top_z),
                                  (x + dx * 0.48 * s_, y + dy * 0.28 * s_, top_z / 2), c))
        return parts
    if kind in ("sofa", "couch", "bed"):
        parts = [_box(n, (1.6 * s_, 0.75 * s_, 0.35 * s_), (x, y, 0.175 * s_), c),
                 _box(f"{n}_back", (1.6 * s_, 0.16 * s_, 0.45 * s_),
                      (x, y + 0.30 * s_, 0.40 * s_), c)]
        return parts
    if kind in ("window", "wall"):
        # a wall panel standing behind the action, with a lighter pane in it
        parts = [_box(n, (2.4 * s_, 0.1 * s_, 1.9 * s_), (x, y, 0.95 * s_), c)]
        if kind == "window":
            parts.append(_box(f"{n}_pane", (1.0 * s_, 0.12 * s_, 0.9 * s_),
                              (x, y - 0.01, 1.15 * s_),
                              mat("pane", (0.55, 0.68, 0.80), rough=0.25)))
        return parts
    if kind in ("rug", "mat"):
        return _box(n, (1.5 * s_, 1.0 * s_, 0.03 * s_), (x, y, 0.015 * s_), c)
    if kind in ("plant", "tree"):
        parts = [_box(f"{n}_pot", (0.24 * s_, 0.24 * s_, 0.26 * s_), (x, y, 0.13 * s_),
                      mat("pot", _colour("red")))]
        for i, (dx, dz, sz) in enumerate(((0, 0.55, 0.42), (0.12, 0.75, 0.30),
                                          (-0.10, 0.70, 0.26))):
            parts.append(_cone(f"{n}_leaf{i}", sz * s_ * 0.5, 0.5 * s_,
                               (x + dx * s_, y, dz * s_ + 0.2), c, verts=4))
        return parts
    h = 0.4 * s_
    return _box(n, (0.5 * s_, 0.5 * s_, h), (x, y, h / 2), c)


def flatten(items):
    """Props may be one object or several; framing wants a flat list."""
    out = []
    for it in (items if isinstance(items, (list, tuple)) else [items]):
        out.extend(flatten(it) if isinstance(it, (list, tuple)) else [it])
    return out


# ---------------------------------------------------------------- actions


def act(cat, action, sc, target=(0.0, 0.0), start=(0.0, 0.0), facing=0.0):
    """Animate the cat through one named action for the whole shot length.

    Every action is keyframed here, once, so a shot never has to describe motion in code.
    Unknown actions fall back to sitting and breathing, which is never wrong for a cat.
    """
    action = str(action or "sit").lower()
    n = sc.frame_end
    tx, ty = float(target[0]), float(target[1])
    sx, sy = float(start[0]), float(start[1])

    if action in ("walk", "walk_to", "approach"):
        for f in range(1, n + 1):
            t = (f - 1) / max(1, n - 1)
            cat.place(sx + (tx - sx) * t, sy + (ty - sy) * t, 0, facing_deg=facing)
            cat.pose_walk((t * n / 12.0) % 1.0)
            cat.key(f)
        return
    if action in ("jump", "jump_on", "leap"):
        for f in range(1, n + 1):
            t = (f - 1) / max(1, n - 1)
            # a parabola: up and over, landing on the target
            z = max(0.0, 1.6 * t * (1 - t)) * 1.2
            cat.place(sx + (tx - sx) * t, sy + (ty - sy) * t, z, facing_deg=facing)
            cat.pose_stand()
            cat.body.rotation_euler = (math.radians(-18 * math.sin(math.pi * t)), 0, 0)
            cat.key(f)
        return
    if action in ("paw", "paw_at", "swat", "knock_over"):
        cat.place(sx, sy, 0, facing_deg=facing)
        for f in range(1, n + 1):
            t = (f - 1) / max(1, n - 1)
            cat.pose_sit()
            swing = math.sin(2 * math.pi * t * 2.0)
            cat.leg_fr.rotation_euler = (math.radians(-55 * max(0.0, swing)), 0, 0)
            cat.head.rotation_euler = (math.radians(12), 0, math.radians(6 * swing))
            cat.key(f)
        return
    if action in ("look", "look_around", "alert", "curious"):
        cat.place(sx, sy, 0, facing_deg=facing)
        for f in range(1, n + 1):
            t = (f - 1) / max(1, n - 1)
            cat.pose_sit()
            cat.look(yaw_deg=32 * math.sin(2 * math.pi * t), pitch_deg=6)
            cat.tail_sway(t * 1.5)
            cat.key(f)
        return
    if action in ("sleep", "loaf", "rest"):
        cat.place(sx, sy, 0, facing_deg=facing)
        for f in range(1, n + 1):
            t = (f - 1) / max(1, n - 1)
            cat.pose_sit()
            # slow breathing, nothing else
            cat.body.location.z = cat._body_z - 0.06 * cat.scale + 0.01 * math.sin(
                2 * math.pi * t)
            cat.head.rotation_euler = (math.radians(26), 0, 0)
            cat.key(f)
        return
    if action in ("run", "chase", "flee"):
        for f in range(1, n + 1):
            t = (f - 1) / max(1, n - 1)
            cat.place(sx + (tx - sx) * t, sy + (ty - sy) * t, 0, facing_deg=facing)
            cat.pose_walk((t * n / 6.0) % 1.0)
            cat.key(f)
        return
    # default: sit, tail moving so the frame is never dead
    cat.place(sx, sy, 0, facing_deg=facing)
    for f in range(1, n + 1):
        t = (f - 1) / max(1, n - 1)
        cat.pose_sit()
        cat.tail_sway(t, amount_deg=14)
        cat.key(f)


CAMERAS = {"wide": 0.32, "medium": 0.48, "close": 0.72}
ANGLES = {"front": (0.0, 74.0), "side": (-80.0, 76.0), "high": (-35.0, 52.0),
          "low": (-30.0, 88.0), "three_quarter": (-35.0, 70.0)}
