"""Load a part-named OBJ and rig it into a posable character, inside Blender.

The hand-coded `creature()` in lowpoly_kit builds a figure out of tapered boxes. It is
cheap, it is consistent, and it looks it - a pile of blocks with a face. A design tool
produces a far better low-poly animal in a minute, but it hands back an OBJ: named meshes,
no armature, no pivots, every object's origin at the world origin. This module is what
turns that file into something a shot can pose.

The one decision everything here follows: THE PART NAME IS THE CONTRACT, geometry only
supplies numbers. Who parents to whom comes from a static table keyed by canonical name -
deterministic, reviewable, and the thing you hand an artist as authoring instructions.
Geometry is measured only for what a name genuinely cannot say: WHERE each pivot sits,
because the file carries none.

Three traps are baked into the code below, all of them paid for once already:

  * `wm.obj_import` puts the Y-up -> Z-up conversion in `rotation_euler`, not in the mesh.
    Any later write to a part's rotation - which is the entire point of a rig - throws it
    across the scene. So the import transform is baked into the mesh data FIRST, and after
    that the rest pose of every joint is the zero rotation.
  * `object.origin_set` and `object.parent_set` are operators: they need selection state,
    they silently skip hidden objects, and parent_set cannot build a chain at all. Both are
    done through the data API here, with an explicit `matrix_parent_inverse` so nothing
    moves when it is parented.
  * `kit.mat()` early-returns an existing material without touching its colour, and the
    importer has already created materials called `fur`, `cream`, `pink`. Colours are
    written into the imported materials directly; nothing here creates one.

Missing parts are not an error. The rig is the intersection of the table and the file: a
part that is absent has its children re-parented to the nearest present ancestor, and
`rot()` on a joint that does not exist does nothing. That single no-op is the whole
graceful-degradation mechanism, so an action never needs to ask what an animal has.
"""

from __future__ import annotations

import colorsys
import hashlib
import json
import math
import re
from pathlib import Path

import bpy
from mathutils import Matrix, Vector
from mathutils.bvhtree import BVHTree

# ---------------------------------------------------------------- the contract
# Pure data and pure python: no bpy call belongs above the loader section, so this half can
# be lifted into a standalone module the app and the tests can import.

ROOT = "root"                    # the root empty, at the model's feet
OWN_CENTRE = "own_centre"        # bbox centre of the part itself
LIMB = "limb"                    # top-centre of the part - a leg swings from its shoulder
SOCKET = "socket"                # centroid of the verts buried inside the parent
FACE = "face"                    # bbox face nearest the parent - SOCKET's fallback
PARENT_CENTRE = "parent_centre"  # the parent's bbox centre
BALL_SOCKET = "ball_socket"      # parent's centre if the parent is a ball, else SOCKET

CHAIN_BASES = ("tail", "neck", "spine")
# Body panels that overlap the torso almost entirely. They are rigged so they follow it,
# but an action must not ROTATE them: their surfaces sit inside the torso's, and turning
# one pushes a slab of it through the skin. Scaling them (breathing) is fine.
SHELLS = ("chest", "hips", "belly")


def _sided(table, name, parent, rule, parent_sided=False):
    for side in ("left", "right"):
        table[f"{name}_{side}"] = (f"{parent}_{side}" if parent_sided else parent, rule)


# name -> (parent, pivot rule). `torso` is the only required part; everything else is
# optional and simply absent when the model has no such thing.
PARTS: dict[str, tuple[str | None, str]] = {
    "torso": (None, ROOT),
    "chest": ("torso", OWN_CENTRE),
    "hips": ("torso", OWN_CENTRE),
    "belly": ("torso", OWN_CENTRE),
    "spine": ("torso", SOCKET),
    "neck": ("chest", SOCKET),
    "head": ("neck", SOCKET),
    "muzzle": ("head", SOCKET),
    # jaw hangs off the HEAD, not the muzzle: the mouth opens correctly either way, and
    # this keeps the jaw alive on a model that has no muzzle.
    "jaw": ("head", SOCKET),
    "tongue": ("jaw", LIMB),
    "tooth": ("jaw", SOCKET),
    "nose": ("muzzle", SOCKET),
    "tail": ("hips", BALL_SOCKET),
    "tail_tip": ("tail", BALL_SOCKET),
}
_sided(PARTS, "eyeball", "head", OWN_CENTRE)
_sided(PARTS, "pupil", "eyeball", PARENT_CENTRE, parent_sided=True)
_sided(PARTS, "eyelid", "head", SOCKET)
_sided(PARTS, "ear", "head", SOCKET)
_sided(PARTS, "ear_inner", "ear", SOCKET, parent_sided=True)
_sided(PARTS, "horn", "head", SOCKET)
_sided(PARTS, "leg_front", "chest", LIMB)
_sided(PARTS, "leg_hind", "hips", LIMB)
_sided(PARTS, "paw_front", "leg_front", SOCKET, parent_sided=True)
_sided(PARTS, "paw_hind", "leg_hind", SOCKET, parent_sided=True)
_sided(PARTS, "wing", "chest", SOCKET)          # own slot: a wing flaps about Y, not X
for _side in "lr":
    for _i in range(6):
        # whiskers are SIBLINGS on the muzzle. The numeric suffix here does not mean a
        # chain: parenting whisker_l1 to whisker_l2 (11mm apart and parallel) is exactly
        # the bug the name-blind version had.
        PARTS[f"whisker_{_side}{_i}"] = ("muzzle", SOCKET)

PARENTS = {k: v[0] for k, v in PARTS.items()}
PIVOTS = {k: v[1] for k, v in PARTS.items()}

# Applied after lowercasing and stripping any .001 suffix. Mapping an ARM onto the front
# limb slot is deliberate: a human's arms then hang off the chest, pivot at the shoulder
# and swing in the quadruped diagonal - which is a correct arm swing - with no new code.
ALIASES = {
    "body": "torso", "snout": "muzzle", "beak": "muzzle", "bill": "muzzle",
    "leg_left": "leg_hind_left", "leg_right": "leg_hind_right",
    "foot_left": "paw_hind_left", "foot_right": "paw_hind_right",
    "arm_left": "leg_front_left", "arm_right": "leg_front_right",
    "hand_left": "paw_front_left", "hand_right": "paw_front_right",
    "eye_left": "eyeball_left", "eye_right": "eyeball_right",
}

# Degrees, per local axis, measured from tear sweeps on the reference cat: past these the
# joint opens and you can see through the model. An action asks for what it wants and the
# rig gives it what is safe.
LIMITS = {
    "torso": (25.0, 8.0, 25.0), "neck": (15.0, 15.0, 15.0),
    "head": (18.0, 15.0, 25.0), "muzzle": (10.0, 6.0, 10.0),
    "jaw": (22.0, 6.0, 8.0), "tongue": (30.0, 10.0, 20.0), "tooth": (8.0, 8.0, 8.0),
    "nose": (8.0, 8.0, 8.0), "ear": (20.0, 20.0, 20.0), "eyelid": (85.0, 10.0, 10.0),
    "eye": (12.0, 12.0, 12.0), "pupil": (12.0, 12.0, 12.0), "horn": (5.0, 5.0, 5.0),
    "leg": (45.0, 20.0, 20.0), "paw": (35.0, 15.0, 15.0), "wing": (30.0, 60.0, 30.0),
    "tail": (40.0, 40.0, 40.0), "whisker": (20.0, 20.0, 20.0), "spine": (15.0, 10.0, 15.0),
}
_DEFAULT_LIMIT = (20.0, 20.0, 20.0)

_CHAIN = re.compile(r"^(tail|neck|spine)_(\d+)$")


def normalise(name: str) -> str:
    """Authored name -> canonical name. Never fails; unknown names come back cleaned."""
    n = re.sub(r"\.\d{3,}$", "", str(name).strip().lower())
    n = re.sub(r"[\s\-.]+", "_", n).strip("_")
    n = re.sub(r"^([lr])_(.+)$",
               lambda m: f"{m.group(2)}_{'left' if m.group(1) == 'l' else 'right'}", n)
    n = re.sub(r"_l$", "_left", n)
    n = re.sub(r"_r$", "_right", n)
    return ALIASES.get(n, n)


def is_known(name: str) -> bool:
    return name in PARTS or bool(_CHAIN.match(name))


def pivot_rule(name: str) -> str:
    m = _CHAIN.match(name)
    if m:
        return PIVOTS[m.group(1)]
    return PIVOTS.get(name, SOCKET)


def _table_parent(name: str, present) -> str | None:
    """The parent the TABLE gives, before checking whether it is in the file."""
    if name == "tail_tip":
        segs = [int(m.group(2)) for m in (_CHAIN.match(p) for p in present)
                if m and m.group(1) == "tail"]
        return f"tail_{max(segs)}" if segs else "tail"
    m = _CHAIN.match(name)
    if m:
        base, i = m.group(1), int(m.group(2))
        return base if i <= 1 else f"{base}_{i - 1}"
    return PARENTS.get(name)


def resolve_parent(name: str, present) -> str | None:
    """Nearest ancestor that is actually in the file. None means "parent to the root".

    A bird with no chest and no hips still rigs: its neck and wings walk up to the torso,
    its legs and tail do the same, and nothing warns because nothing is wrong.
    """
    p = _table_parent(name, present)
    while p is not None and p not in present:
        p = _table_parent(p, present)
    return p


def _family(name: str) -> str:
    """Which LIMITS row a joint obeys."""
    n = re.sub(r"_(left|right)$", "", name)
    n = re.sub(r"_\d+$", "", n)
    if n.startswith("leg"):
        return "leg"
    if n.startswith("paw"):
        return "paw"
    if n.startswith("ear"):
        return "ear"
    if n.startswith("whisker"):
        return "whisker"
    if n.startswith("eyeball"):
        return "eye"
    return {"tail_tip": "tail"}.get(n, n)


# ---------------------------------------------------------------- colour

DEFAULT_PALETTE = {
    "fur": (0.62, 0.45, 0.30), "cream": (0.93, 0.89, 0.80),
    "pink": (0.90, 0.55, 0.58), "charcoal": (0.09, 0.09, 0.11),
    "sclera": (0.96, 0.96, 0.95), "skin": (0.80, 0.62, 0.50),
    "metal": (0.55, 0.57, 0.62), "cloth": (0.35, 0.42, 0.58),
    "wood": (0.42, 0.30, 0.20), "eye": (0.20, 0.35, 0.28),
    "black": (0.08, 0.08, 0.10), "white": (0.90, 0.90, 0.88),
}

# The spec's fur colour, by name, so a shot can ask for a black cat.
COATS = {"ginger": (0.72, 0.42, 0.18), "grey": (0.52, 0.52, 0.56),
         "white": (0.88, 0.86, 0.82), "black": (0.16, 0.15, 0.17),
         "brown": (0.42, 0.30, 0.20), "cream": (0.86, 0.78, 0.62)}


def _auto_colour(name: str) -> tuple[float, float, float]:
    """A stable muted colour for a material nobody named in a palette.

    hashlib, not hash(): python randomises string hashing per process, so hash() would give
    the same model a different coat on every render.
    """
    h = int(hashlib.md5(name.encode("utf-8")).hexdigest()[:8], 16)
    return colorsys.hsv_to_rgb((h % 360) / 360.0, 0.32, 0.62)


def _srgb_to_linear(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def paint(material, rgb, rough=0.75):
    """Write a colour into an EXISTING material. Nothing here ever creates one.

    By node TYPE, not by the node's name: the OBJ importer names its shader after the
    material when there is an .mtl, and "Principled BSDF" only when there is not.
    """
    material.diffuse_color = (*rgb, 1.0)         # WORKBENCH previews read this, not the node
    bsdf = next((n for n in material.node_tree.nodes if n.type == "BSDF_PRINCIPLED"), None)
    if bsdf is None:
        return material
    bsdf.inputs["Base Color"].default_value = (*[_srgb_to_linear(c) for c in rgb], 1.0)
    bsdf.inputs["Roughness"].default_value = rough
    if "Specular IOR Level" in bsdf.inputs:      # the 5.x name; it was "Specular" before
        bsdf.inputs["Specular IOR Level"].default_value = 0.15
    return material


# ---------------------------------------------------------------- geometry helpers


def bake(objects):
    """Push every object's transform into its mesh, so local == world and rest == zero.

    This is the first thing that happens to an import and the reason the rig works at all:
    the importer's Y-up->Z-up conversion lives in `rotation_euler`, and a rig exists to
    write `rotation_euler`.
    """
    for ob in objects:
        ob.data.transform(ob.matrix_world)
        ob.matrix_world = Matrix.Identity(4)
    bpy.context.view_layer.update()


def transform_data(objects, matrix):
    """Apply one world-space matrix to the MESHES, leaving every object at the identity."""
    for ob in objects:
        ob.data.transform(matrix)
        ob.data.update()
    bpy.context.view_layer.update()


def set_origin(obj, point):
    """Move an object's origin to a world point without moving its geometry.

    `object.origin_set(type='ORIGIN_CURSOR')` returns CANCELLED on a hidden object and
    leaves the origin at zero, and it drives every SELECTED object to the same point. This
    does not care about selection, visibility, or what else is in the scene.
    """
    p = Vector(point)
    local = obj.matrix_world.inverted() @ p
    obj.data.transform(Matrix.Translation(-local))
    obj.matrix_world.translation = p
    obj.data.update()


def parent_keep(child, parent):
    """Parent without moving the child. The inverse matrix is not optional."""
    bpy.context.view_layer.update()
    child.parent = parent
    child.matrix_parent_inverse = parent.matrix_world.inverted()
    bpy.context.view_layer.update()


def _world_verts(obj):
    m = obj.matrix_world
    return [m @ v.co for v in obj.data.vertices]


def _bbox(points):
    xs = [p.x for p in points]
    ys = [p.y for p in points]
    zs = [p.z for p in points]
    return Vector((min(xs), min(ys), min(zs))), Vector((max(xs), max(ys), max(zs)))


def _sphericity(points, centre):
    """1 - std(|v-c|)/mean(|v-c|): 1.0 is a perfect ball, a capsule is ~0.8.

    Aspect ratio alone is not enough - it calls the cat's chest and hips (1.11) and its ear
    (1.17) balls, which hands the legs the chest's pivot and lifts the ear off the skull.
    """
    r = [(p - centre).length for p in points]
    mean = sum(r) / len(r)
    if mean <= 1e-9:
        return 1.0
    var = sum((x - mean) ** 2 for x in r) / len(r)
    return 1.0 - math.sqrt(var) / mean


def _bvh(obj):
    verts = _world_verts(obj)
    return BVHTree.FromPolygons(verts, [tuple(p.vertices) for p in obj.data.polygons])


def _inside(bvh, point):
    """Point-in-mesh by nearest surface normal. Every part here is a closed primitive."""
    loc, normal, _idx, _dist = bvh.find_nearest(point)
    if loc is None:
        return False
    return (point - loc).dot(normal) < 0.0


def _face_centre(lo, hi, towards):
    """Centre of the own-bbox face nearest `towards` - SOCKET's answer when nothing buried."""
    centre = (lo + hi) / 2.0
    best, best_d = centre, None
    for axis in range(3):
        for value in (lo[axis], hi[axis]):
            c = centre.copy()
            c[axis] = value
            d = (c - towards).length
            if best_d is None or d < best_d:
                best, best_d = c, d
    return best


# ---------------------------------------------------------------- the rig


class Rig:
    """A loaded character. `root` is an empty at its feet - place and yaw THAT.

    Joints are canonical names, so an action asks for `head` or `leg_front_left` and gets
    a no-op if this animal has neither.
    """

    def __init__(self, root, joints, meshes, height, model_id, report):
        self.root = root
        self.joints = joints
        self.meshes = meshes
        self.height = height
        self.model_id = model_id
        self.report = report

    # -- posing

    def has(self, name) -> bool:
        return name in self.joints

    def rot(self, name, x=0.0, y=0.0, z=0.0):
        """Set a joint's LOCAL rotation in degrees, clamped. Absent joint: nothing happens.

        Every action degrades through this one line. There is no per-joint `if` anywhere in
        the action library because there does not need to be.
        """
        ob = self.joints.get(name)
        if ob is None:
            return self
        lim = LIMITS.get(_family(name), _DEFAULT_LIMIT)
        ob.rotation_euler = tuple(
            math.radians(max(-lim[i], min(lim[i], float(v))))
            for i, v in enumerate((x, y, z)))
        return self

    def rot_add(self, name, x=0.0, y=0.0, z=0.0):
        ob = self.joints.get(name)
        if ob is None:
            return self
        cur = [math.degrees(a) for a in ob.rotation_euler]
        return self.rot(name, cur[0] + x, cur[1] + y, cur[2] + z)

    def scale_joint(self, name, x=1.0, y=1.0, z=1.0):
        """Scale one joint. Breathing is a scale of the chest, not a rotation of it."""
        ob = self.joints.get(name)
        if ob is not None:
            ob.scale = (x, y, z)
        return self

    def rest(self):
        """Back to the imported pose: every joint at zero, which is what the bake bought."""
        for ob in self.joints.values():
            ob.rotation_euler = (0.0, 0.0, 0.0)
            ob.scale = (1.0, 1.0, 1.0)
        return self

    def place(self, x=0.0, y=0.0, z=0.0, facing_deg=0.0):
        """Put the feet at (x, y, z) and turn the model. facing 0 = the model faces -Y."""
        self.root.location = (x, y, z)
        self.root.rotation_euler = (0.0, 0.0, math.radians(facing_deg))
        return self

    def settle(self, floor=0.0):
        """Drop the posed model onto the floor.

        The loader stands the REST pose on z=0, but a pose moves the feet: fold the hind
        legs and the paws end up above the ground, tilt the body and they end up under it.
        Every hand-picked root offset tried here either floated the cat or buried its legs
        to the knee, because the right number depends on the pose. So measure it. Actions
        that are meant to leave the ground - a jump - simply do not call this.
        """
        lo, _hi = self.bounds()
        self.root.location.z += floor - lo.z
        return self

    def key(self, frame):
        for ob in [self.root, *self.joints.values()]:
            ob.keyframe_insert("location", frame=frame)
            ob.keyframe_insert("rotation_euler", frame=frame)
            ob.keyframe_insert("scale", frame=frame)
        return self

    # -- what the rest of the kit consumes

    def objects(self):
        """Root plus every mesh - `kit.frame()` takes this and solves the camera from it."""
        return [self.root, *self.meshes]

    def group(self, kind):
        """Canonical names present in this model, by role. Actions loop over these."""
        sides = ("left", "right")
        table = {
            "front": [f"leg_front_{s}" for s in sides],
            "hind": [f"leg_hind_{s}" for s in sides],
            "legs": [f"leg_{p}_{s}" for p in ("front", "hind") for s in sides],
            "paws_front": [f"paw_front_{s}" for s in sides],
            "paws_hind": [f"paw_hind_{s}" for s in sides],
            "ears": [f"ear_{s}" for s in sides],
            "wings": [f"wing_{s}" for s in sides],
            "eyes": [f"eyeball_{s}" for s in sides],
            "pupils": [f"pupil_{s}" for s in sides],
            "whiskers": [f"whisker_{s}{i}" for s in "lr" for i in range(6)],
            "tail_chain": ["tail"] + [f"tail_{i}" for i in range(1, 6)] + ["tail_tip"],
            "shells": list(SHELLS),
        }
        return [n for n in table.get(kind, []) if n in self.joints]

    def bounds(self):
        """(lo, hi) world corners of the posed model AT THE CURRENT FRAME.

        Framing runs after keyframing, so the transforms have to be forced to catch up -
        without the depsgraph update the camera is solved on the pose the model had when it
        was built, and a walking subject is framed where it never stands.
        """
        bpy.context.view_layer.update()
        deps = bpy.context.evaluated_depsgraph_get()
        deps.update()
        pts = []
        for ob in self.meshes:
            e = ob.evaluated_get(deps)
            pts.extend(e.matrix_world @ Vector(c) for c in e.bound_box)
        return _bbox(pts)

    def centre_span(self):
        """The camera solve's two numbers: where to look, and how big it is."""
        lo, hi = self.bounds()
        centre = (lo + hi) / 2.0
        return (centre.x, centre.y, centre.z), max(hi.x - lo.x, hi.y - lo.y, hi.z - lo.z)


# ---------------------------------------------------------------- loading


def find_model(model_id, models_dir) -> Path:
    """models/<id>/<id>.obj. The stem must match the folder, so a stray export beside it
    cannot become a second, half-broken character."""
    path = Path(models_dir) / str(model_id) / f"{model_id}.obj"
    if not path.is_file():
        raise FileNotFoundError(f"no model at {path}")
    return path


def describe(model_id, models_dir) -> dict:
    meta = Path(models_dir) / str(model_id) / "model.json"
    return json.loads(meta.read_text(encoding="utf-8")) if meta.is_file() else {}


def _import(obj_path, status):
    before = set(bpy.data.objects)
    bpy.ops.wm.obj_import(filepath=str(obj_path))
    raw = [o for o in bpy.data.objects if o not in before and o.type == "MESH"]
    if not raw:
        raise RuntimeError(f"{obj_path.name} contains no mesh objects")
    bake(raw)                                    # FIRST. Always. See the module docstring.
    for ob in raw:
        ob.data.shade_flat()                     # faceted IS the look, at every poly count
    status(f"RIG import {obj_path.parent.name}: {len(raw)} objects, "
           f"{sum(len(o.data.polygons) for o in raw)} polys")
    return raw


def _canonical_names(raw, status):
    """authored name -> canonical, with collisions resolved by vertex count.

    A part whose name is not in the contract is NOT dropped: it is kept, rendered, parented
    by geometry and never animated. A design tool will emit `collar`, `saddle`, `hat`, and
    deleting visible geometry because we have no row for it is worse than not animating it.
    """
    parts, unknown = {}, {}
    for ob in raw:
        name = normalise(ob.name)
        if not is_known(name):
            unknown[name if name not in unknown else f"{name}__dup{len(unknown)}"] = ob
            continue
        sitting = parts.get(name)
        if sitting is None:
            parts[name] = ob
            continue
        # Two objects normalised onto the same slot. The denser mesh takes the joint, the
        # other is kept as an unanimated extra: deterministic, and no geometry is lost.
        winner, loser = ((ob, sitting) if len(ob.data.vertices) > len(sitting.data.vertices)
                         else (sitting, ob))
        parts[name] = winner
        unknown[f"{name}__dup{len(unknown)}"] = loser
        status(f"RIG_WARN duplicate {name}: kept the denser mesh ({ob.name} vs "
               f"{sitting.name})")
    return parts, unknown


def _promote_torso(parts, unknown, status):
    """A single-mesh OBJ with a name nobody agreed on still has to render.

    It becomes one rigid part on a root empty: actions degrade to translation and yaw, the
    camera still frames it, and the shot is a video rather than a crash.
    """
    if "torso" in parts:
        return

    def volume(ob):
        lo, hi = _bbox(_world_verts(ob))
        return (hi.x - lo.x) * (hi.y - lo.y) * (hi.z - lo.z)

    pool = {**parts, **unknown}
    if not pool:
        raise RuntimeError("nothing to rig")
    name = max(pool, key=lambda n: volume(pool[n]))
    parts["torso"] = pool[name]
    parts.pop(name, None)
    unknown.pop(name, None)
    status(f"RIG_WARN promoted {name} -> torso")


_FACING = {"-y": (0.0, -1.0), "+y": (0.0, 1.0), "-x": (-1.0, 0.0), "+x": (1.0, 0.0)}


def _normalise_pose(meshes, faces, height, scale, status):
    """Turn it to face -Y, size it, and stand it on the floor at the world origin.

    Metres, one OBJ unit = one metre, and NO auto-normalisation of size: a mouse and a bear
    must not come out the same height. The clamp only catches a file exported in
    millimetres or inches, which would otherwise fill or vanish from the frame.
    """
    fx, fy = _FACING[str(faces or "-y").lower()]
    yaw = math.atan2(-1.0, 0.0) - math.atan2(fy, fx)
    if abs(yaw) > 1e-6:
        transform_data(meshes, Matrix.Rotation(yaw, 4, "Z"))
        status(f"RIG faces {faces} -> rotated {math.degrees(yaw):.0f}deg to -Y")

    lo, hi = _bbox([v for ob in meshes for v in _world_verts(ob)])
    native = hi.z - lo.z
    factor = float(scale)
    if height:
        factor *= float(height) / native
    elif native > 6.0 or native < 0.05:
        factor *= 1.0 / native
        status(f"RIG_WARN scale: {native:.3f}m is not a plausible height; normalised to 1m")
    if abs(factor - 1.0) > 1e-6:
        transform_data(meshes, Matrix.Scale(factor, 4))
        lo, hi = _bbox([v for ob in meshes for v in _world_verts(ob)])

    centre = (lo + hi) / 2.0
    transform_data(meshes, Matrix.Translation(Vector((-centre.x, -centre.y, -lo.z))))
    return hi.z - lo.z


def _measure(parts, unknown):
    """Everything the pivot rules need, computed once, on the geometry as it will render."""
    info = {}
    for name, ob in {**parts, **unknown}.items():
        verts = _world_verts(ob)
        lo, hi = _bbox(verts)
        centroid = sum(verts, Vector()) / len(verts)
        info[name] = {"obj": ob, "verts": verts, "lo": lo, "hi": hi,
                      "centre": (lo + hi) / 2.0, "centroid": centroid,
                      "sphericity": _sphericity(verts, centroid),
                      "polys": len(ob.data.polygons)}
    return info


def _buried(child, parent_info, bvh_cache):
    """The child's vertices that lie inside the parent mesh, in world space."""
    key = id(parent_info["obj"])
    if key not in bvh_cache:
        bvh_cache[key] = _bvh(parent_info["obj"])
    bvh = bvh_cache[key]
    return [v for v in child["verts"] if _inside(bvh, v)]


def _pivot(name, rule, info, parent_name, bvh_cache, status):
    """Where this part turns. The one thing a name cannot tell you, so it is measured."""
    me = info[name]
    parent = info.get(parent_name) if parent_name else None
    if rule == ROOT:
        return Vector((0.0, 0.0, 0.0))           # the root empty, on the floor
    if rule == OWN_CENTRE or parent is None:     # nothing to socket into: turn in place
        return me["centre"]
    if rule == LIMB:
        # Top-centre of the part, i.e. the shoulder or hip, not the middle of the bone.
        return Vector((me["centre"].x, me["centre"].y, me["hi"].z))
    if rule == PARENT_CENTRE:
        return parent["centre"]
    if rule == BALL_SOCKET and parent["sphericity"] > 0.90:
        # Rotating about a ball's centre keeps every buried vertex at the same distance
        # from it, so the socket cannot open however far the part swings. A tail rigged on
        # its own buried centroid swings its flat root cap clear out of the rump.
        return parent["centre"]
    inside = _buried(me, parent, bvh_cache)
    if len(inside) < 20:
        # Nothing meaningful is buried - the parts merely touch. Hinge on the face that
        # looks at the parent; it is the least wrong answer and it is stable.
        status(f"RIG_WARN {name}: only {len(inside)} verts inside {parent_name}, "
               "hinging on the nearest face")
        return _face_centre(me["lo"], me["hi"], parent["centre"])
    return sum(inside, Vector()) / len(inside)


def _geometric_parent(name, info, known_names, bvh_cache):
    """For a part with no row in the table: whoever contains most of it, else who is nearest.

    This is the only place geometry decides parenting, and it is the right place - there is
    no name to consult.
    """
    me = info[name]
    sample = me["verts"][::max(1, len(me["verts"]) // 200)]
    best, best_frac = None, 0.0
    for other in known_names:
        if other == name:
            continue
        key = id(info[other]["obj"])
        if key not in bvh_cache:
            bvh_cache[key] = _bvh(info[other]["obj"])
        frac = sum(1 for v in sample if _inside(bvh_cache[key], v)) / len(sample)
        if frac > best_frac:
            best, best_frac = other, frac
    if best is not None and best_frac >= 0.05:
        return best, best_frac
    near = min(known_names, key=lambda o: (info[o]["centre"] - me["centre"]).length,
               default=None)
    return near, 0.0


def _recolour(meshes, palette, coat_rgb, coat_material, status):
    """Colour by MATERIAL NAME, into the materials the importer already made.

    Materials are shared across parts, so five writes recolour the whole character - and a
    missing .mtl is harmless, because the importer still creates the named materials and
    only their colour is wrong.
    """
    mats, polys = [], {}
    for ob in meshes:
        slots = [s.material for s in ob.material_slots]
        for m in slots:
            if m is not None and m not in mats:
                mats.append(m)
        for p in ob.data.polygons:
            m = slots[p.material_index] if p.material_index < len(slots) else None
            if m is not None:
                polys[m.name] = polys.get(m.name, 0) + 1
    if not mats:
        # No usemtl anywhere in the file: there is nothing to recolour and this module does
        # not invent materials. Say so, because the model will render at default grey.
        status("RIG_WARN no materials in the model; it will render Blender-grey")
        return None
    palette = {str(k).lower(): v for k, v in (palette or {}).items()}
    coat = str(coat_material or max(polys, key=polys.get, default=mats[0].name)).lower()
    for m in mats:
        key = re.sub(r"\.\d{3,}$", "", m.name.lower())
        if coat_rgb is not None and key == coat:
            rgb = coat_rgb
        else:
            rgb = palette.get(key) or DEFAULT_PALETTE.get(key) or _auto_colour(key)
        paint(m, rgb)
    status(f"RIG colour: {', '.join(m.name for m in mats)} (coat={coat})")
    return coat


PROTECT_POLYS = 300
# A capsule measures ~0.90, so the gate has to sit above that or it protects the whole
# animal and decimation does nothing: on the reference cat 0.88 spared the torso, head,
# chest, hips and all four legs, i.e. every shape the faceting is supposed to show on.
PROTECT_SPHERICITY = 0.94


def _decimate(info, ratio, status):
    """Chunkier facets on the big shapes, small parts left alone.

    This is art direction, not performance - render time is flat from ratio 1.0 down to
    0.15. A uniform 0.15 deletes the whiskers, tears the ears open and splits the muzzle
    seam; protecting by MEASUREMENT rather than by a list of names generalises to a model
    nobody has seen. Over-protecting costs nothing but the look.
    """
    kept = 0
    for name, m in info.items():
        ob = m["obj"]
        if m["polys"] <= PROTECT_POLYS or m["sphericity"] > PROTECT_SPHERICITY:
            kept += m["polys"]
            continue
        mod = ob.modifiers.new("dec", "DECIMATE")
        mod.decimate_type = "COLLAPSE"
        mod.ratio = max(ratio, 200.0 / m["polys"])
        mod.use_collapse_triangulate = True
        kept += int(m["polys"] * mod.ratio)
    status(f"RIG decimate {ratio:.2f}: ~{kept} of "
           f"{sum(m['polys'] for m in info.values())} tris")


DECIMATE = 0.15


def _depth(name, parent_of, seen=None):
    """How deep in the hierarchy, so parents can be wired before their children."""
    seen = seen or set()
    p = parent_of.get(name)
    if p is None or p in seen or p not in parent_of:
        return 0
    return 1 + _depth(p, parent_of, seen | {name})


def load(model_id, models_dir, *, scale=1.0, height=None, coat=None, palette=None,
         decimate=None, faces=None, at=(0.0, 0.0), facing_deg=0.0, status=print) -> Rig:
    """Import models/<id>/<id>.obj and return a posed, parented, coloured Rig.

    `coat` is a fur name ("ginger") or an rgb triple; it repaints whichever material covers
    most of the model, which is what a prompt means by "a black cat". `palette` overrides
    any material by name. Everything a model.json declares is a default here, not a rule:
    the caller wins.
    """
    obj_path = find_model(model_id, models_dir)
    meta = describe(model_id, models_dir)
    raw = _import(obj_path, status)

    parts, unknown = _canonical_names(raw, status)
    _promote_torso(parts, unknown, status)
    meshes = [*parts.values(), *unknown.values()]
    real_height = _normalise_pose(meshes, faces if faces else meta.get("faces", "-Y"),
                                  height or meta.get("height_m"), scale, status)

    info = _measure(parts, unknown)
    present = set(parts)
    bvh_cache = {}

    parent_of = {}
    for name in parts:
        parent_of[name] = resolve_parent(name, present)
    for name in unknown:
        chosen, frac = _geometric_parent(name, info, list(present), bvh_cache)
        parent_of[name] = chosen
        status(f"RIG_UNKNOWN {name} -> {chosen} ({frac * 100:.0f}% inside)")

    pivots = {name: _pivot(name, pivot_rule(name) if name in parts else SOCKET,
                           info, parent_of[name], bvh_cache, status)
              for name in {**parts, **unknown}}

    bpy.ops.object.empty_add(type="PLAIN_AXES", location=(0, 0, 0))
    root = bpy.context.object
    root.name = f"{model_id}_root"
    root.empty_display_size = 0.15

    for name, point in pivots.items():
        set_origin(info[name]["obj"], point)
    # Top down, so a parent's origin is already final when its children hang off it.
    ordered = sorted(parent_of, key=lambda n: _depth(n, parent_of))
    for name in ordered:
        target = parent_of[name]
        parent_keep(info[name]["obj"],
                    info[target]["obj"] if target in info else root)

    coat_rgb = COATS.get(str(coat).lower()) if isinstance(coat, str) else coat
    _recolour(meshes, palette or meta.get("palette") or {}, coat_rgb,
              meta.get("coat_material"), status)
    _decimate(info, float(decimate if decimate is not None
                          else meta.get("decimate", DECIMATE)), status)

    rig = Rig(root, parts, meshes, real_height, str(model_id),
              {"model": str(model_id), "height": round(real_height, 4),
               "parts": sorted(parts), "unknown": sorted(unknown),
               "parents": {k: (v or "root") for k, v in parent_of.items()},
               "pivot_rules": {k: pivot_rule(k) for k in parts},
               "pivots": {k: [round(c, 4) for c in v] for k, v in pivots.items()}})
    rig.place(float(at[0]), float(at[1]), 0.0, facing_deg=facing_deg)
    status(f"RIG ready {model_id}: {len(parts)} joints, {len(unknown)} extra, "
           f"{real_height:.3f}m")
    return rig

