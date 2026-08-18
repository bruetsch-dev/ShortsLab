"""The model registry: what characters are installed, and what each one can do.

This module runs OUTSIDE Blender. It never imports bpy, never renders, and never touches
the network, because it is imported by `story.py` (which must know the catalogue before
the writer is called) and transitively by `app.py` at start-up - and `app.py` disables the
whole low-poly mode if that import raises.

It answers three questions:

  1. WHAT IS INSTALLED.  Scan `lowpoly_mode/models/*/` for `<id>/<id>.obj`.
  2. WHAT IS IN IT.      Read the part names straight out of the OBJ text - the `o ` lines -
                         and measure each part's bounding box from its `v ` lines. No
                         Blender, no mesh library, one streaming pass over the file.
  3. WHAT IT CAN DO.     Map those part names through the rig contract to the joints the
                         rig will build, and from the joints to the actions a shot can ask
                         for. A bird with no front legs cannot `paw_at`; that is a fact
                         about the file, and it is knowable before a single frame renders.

The part NAME is the contract. Geometry is measured only for the checks a name cannot make
- whether a part actually touches the thing it hangs off, which way the model faces, how
tall it is. Everything else is a table lookup.

Nothing here is a hard failure except an empty library. A model with odd parts still loads,
still renders and simply animates less; the report says so in words instead of throwing.


================================================================================
HOW TO ADD A NEW MODEL   (this section is for you, not for the code)
================================================================================

1. FOLDER LAYOUT

       lowpoly_mode/models/<id>/<id>.obj      <- required, and the file name must match
       lowpoly_mode/models/<id>/model.json    <- optional
       lowpoly_mode/models/<id>/preview.png   <- optional

   `<id>` is lower case letters, digits and underscores, 2-24 characters: `cat`, `dog`,
   `red_fox`. The `.obj` must be named after its folder - `dog/dog.obj`. A folder holding
   `dog/lowpoly-dog.obj` is IGNORED, on purpose: it is the difference between adding a
   model and adding a half-broken second one.

   That is all that is required. No `.mtl` file is needed. Nothing has to be registered
   anywhere else - drop the folder in and the story writer can use it on the next run.

2. AXES, FACING AND UNITS

       facing  -Y      the character looks toward -Y. Its nose is at negative y, its tail
                       at positive y. This is checked and reported; a model built facing
                       +Y renders with its back to the camera in every single shot.
       up      +Z
       +X      the character's own left
       units   METRES, one OBJ unit = one metre. A house cat is about 0.65 m to the ear
               tips, a mouse 0.08 m, a bear 1.4 m. Nothing is auto-normalised: a mouse and
               a bear must not come out the same size. Stand the model on the ground
               (lowest point near z=0); the loader drops it to the floor anyway.

3. PART NAMES

   Export one named mesh object per body part (`o <name>` in the OBJ). Use these names
   where the part exists, and simply omit every part the animal does not have:

       torso chest hips belly neck head muzzle jaw tongue tooth nose
       eyeball_left eyeball_right pupil_left pupil_right eyelid_left eyelid_right
       ear_left ear_right ear_inner_left ear_inner_right
       whisker_l0..l5 whisker_r0..r5
       leg_front_left leg_front_right leg_hind_left leg_hind_right
       paw_front_left paw_front_right paw_hind_left paw_hind_right
       wing_left wing_right tail tail_1..tail_5 tail_tip horn_left horn_right

   `torso` is the only one that is genuinely required (`body` is accepted for it).
   Accepted alternatives: body=torso, snout/beak/bill=muzzle, arm_*=front leg,
   hand_*=front paw, leg_left/leg_right=hind legs, foot_*=hind paws, eye_*=eyeball,
   `_l`/`_r`=`_left`/`_right`, `l_<part>`/`r_<part>`=`<part>_left`/`<part>_right`.

   Extra parts under any other name (`collar`, `hat`, `saddle`) are kept and rendered -
   they are parented to whichever known part contains them - but they are never animated.

4. THE ONE MODELLING RULE THAT MATTERS

   EVERY JOINT MUST OVERLAP THE PART IT ATTACHES TO. Sink each limb, ear, jaw and tail
   root well inside its parent's volume. Parts that merely touch tear open a visible hole
   the moment they rotate, and this module reports them as `detached` before you ever
   render: `scan()` prints the gap in millimetres.

5. MATERIALS

   Assign a material per surface (`usemtl`). `fur`, `cream`, `pink`, `charcoal`, `sclera`,
   `skin`, `metal`, `cloth`, `wood`, `eye`, `black`, `white` have built-in colours; any
   other name gets a stable muted colour derived from the name. The story's fur colour
   repaints whichever material covers the most polygons, so keep the coat on one material.

6. OPTIONAL model.json  (every key optional)

       {"label": "a low-poly house cat",
        "aliases": ["kitten", "housecat"],
        "stands_in_for": ["dog", "fox", "mouse", "rabbit", "bear", "tiger"],
        "height_m": 0.65,
        "decimate": 0.15,
        "coat_material": "fur",
        "palette": {"fur": [0.92, 0.62, 0.26], "cream": [0.98, 0.93, 0.84]}}

   `label` is what the story writer is told the model is - write it as a noun phrase, it
   goes straight into the prompt. `stands_in_for` is the honest-substitution list: with it,
   a writer that asks for a dog gets this model and the run log says so.

7. CHECK IT

       python -m lowpoly_mode.models_library

   prints the catalogue: every part, the joints it gives you, the actions it can play, and
   every contract violation found. Run it after adding a model, before rendering anything.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

MODELS_DIR = Path(__file__).resolve().parent / "models"
MODEL_ID = re.compile(r"^[a-z0-9_]{2,24}$")

# ---------------------------------------------------------------- the contract
# Mirrors the contract half of blender_kit/model_rig.py. It is repeated rather than
# imported because that module imports bpy at line 44 and this one must run in the app
# process. `models_library_selftest.py` diffs the two tables and fails on any drift, so
# the copy cannot rot silently.

ROOT = "root"
OWN_CENTRE = "own_centre"
LIMB = "limb"
SOCKET = "socket"
FACE = "face"
PARENT_CENTRE = "parent_centre"
BALL_SOCKET = "ball_socket"

CHAIN_BASES = ("tail", "neck", "spine")
SHELLS = ("chest", "hips", "belly")


def _sided(table, name, parent, rule, parent_sided=False):
    for side in ("left", "right"):
        table[f"{name}_{side}"] = (f"{parent}_{side}" if parent_sided else parent, rule)


PARTS: dict[str, tuple[str | None, str]] = {
    "torso": (None, ROOT),
    "chest": ("torso", OWN_CENTRE),
    "hips": ("torso", OWN_CENTRE),
    "belly": ("torso", OWN_CENTRE),
    "spine": ("torso", SOCKET),
    "neck": ("chest", SOCKET),
    "head": ("neck", SOCKET),
    "muzzle": ("head", SOCKET),
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
_sided(PARTS, "wing", "chest", SOCKET)
for _side in "lr":
    for _i in range(6):
        PARTS[f"whisker_{_side}{_i}"] = ("muzzle", SOCKET)

PARENTS = {k: v[0] for k, v in PARTS.items()}
PIVOTS = {k: v[1] for k, v in PARTS.items()}

ALIASES = {
    "body": "torso", "snout": "muzzle", "beak": "muzzle", "bill": "muzzle",
    "leg_left": "leg_hind_left", "leg_right": "leg_hind_right",
    "foot_left": "paw_hind_left", "foot_right": "paw_hind_right",
    "arm_left": "leg_front_left", "arm_right": "leg_front_right",
    "hand_left": "paw_front_left", "hand_right": "paw_front_right",
    "eye_left": "eyeball_left", "eye_right": "eyeball_right",
}

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
    """Nearest ancestor actually present in the file. None means "parent to the root"."""
    p = _table_parent(name, present)
    while p is not None and p not in present:
        p = _table_parent(p, present)
    return p


# ---------------------------------------------------------------- actions

ACTIONS = ("sit", "walk_to", "run", "jump_on", "paw_at", "look_around", "sleep")

_LEGS = ("leg_front_left", "leg_front_right", "leg_hind_left", "leg_hind_right")
_FRONT = ("leg_front_left", "leg_front_right")
_HIND = ("leg_hind_left", "leg_hind_right")
_PAWS_HIND = ("paw_hind_left", "paw_hind_right")
_EARS = ("ear_left", "ear_right")
_WINGS = ("wing_left", "wing_right")


@dataclass(frozen=True)
class ActionSpec:
    """What an action touches, and what it cannot do without.

    `drives` is every joint the action writes a key on; a missing one costs detail, not the
    shot. `needs_any` is the honest floor: each entry is (what it is, the names that would
    satisfy it), and if none of them is present the action degrades to `falls_back_to` or
    to a visibly worse read, which is named in `without`.
    """
    drives: tuple[str, ...]
    needs_any: tuple[tuple[str, tuple[str, ...]], ...] = ()
    falls_back_to: str = ""
    without: str = ""
    softly: tuple[tuple[str, str], ...] = ()   # (joint, what is lost when it is absent)


ACTION_SPECS: dict[str, ActionSpec] = {
    "sit": ActionSpec(
        drives=("torso", *_HIND, *_PAWS_HIND, *_FRONT, "head", "tail", "tail_tip",
                "ear_left"),
        needs_any=(("no hind legs", _HIND),),
        without="the body tilts back but nothing folds under it - it reads as a lean",
    ),
    "walk_to": ActionSpec(
        drives=("torso", *_LEGS, "paw_front_left", "paw_front_right", *_PAWS_HIND,
                "head", "neck", "tail", "tail_tip", *_EARS),
        needs_any=(("no legs", _LEGS),),
        without="it slides to the target with no stride - it reads as gliding",
    ),
    "run": ActionSpec(
        drives=("torso", *_LEGS, "paw_front_left", "paw_front_right", *_PAWS_HIND,
                "head", "tail", *_EARS),
        needs_any=(("no legs", _LEGS),),
        without="it slides to the target with no stride - it reads as gliding",
    ),
    "jump_on": ActionSpec(
        # the arc is root translation, so this survives a model with no legs at all
        drives=("torso", *_LEGS, "tail"),
        softly=(("leg_front_left", "no reach on the way up"),
                ("leg_hind_left", "no crouch and no landing absorb")),
    ),
    "paw_at": ActionSpec(
        drives=("torso", *_FRONT, "paw_front_left", "paw_front_right", "head", *_EARS,
                "tail"),
        needs_any=(("no front limb and no wing", _FRONT + _WINGS),),
        falls_back_to="look_around",
        without="there is nothing to swipe with",
    ),
    "look_around": ActionSpec(
        drives=("head", "neck", "pupil_left", "pupil_right", *_EARS, "tail"),
        needs_any=(("no head", ("head",)),),
        without="only the tail moves - the frame is nearly dead",
    ),
    "sleep": ActionSpec(
        drives=("torso", "chest", "head", *_EARS, "tail", "tail_tip",
                "eyelid_left", "eyelid_right"),
        softly=(("eyelid_left", "the eyes stay open; the head pitch has to carry it"),),
    ),
}


@dataclass(frozen=True)
class ActionVerdict:
    action: str
    grade: str                  # "full" | "partial" | "degraded"
    missing: tuple[str, ...]
    note: str

    @property
    def possible(self) -> bool:
        """Every action always renders. This says whether it renders as intended."""
        return self.grade != "degraded"

    def __str__(self) -> str:
        tail = f" - {self.note}" if self.note else ""
        return f"{self.action}: {self.grade}{tail}"


def actions_for(joints) -> dict[str, ActionVerdict]:
    """Grade all seven actions against the joints a model actually has."""
    have = set(joints)
    out: dict[str, ActionVerdict] = {}
    for name in ACTIONS:
        spec = ACTION_SPECS[name]
        blocked = [what for what, names in spec.needs_any if not have.intersection(names)]
        missing = tuple(j for j in spec.drives if j not in have)
        if blocked:
            note = ", ".join(blocked)
            if spec.without:
                note += f"; {spec.without}"
            if spec.falls_back_to:
                note += f"; falls back to {spec.falls_back_to}"
            out[name] = ActionVerdict(name, "degraded", missing, note)
            continue
        soft = [why for joint, why in spec.softly if joint not in have]
        if not missing:
            out[name] = ActionVerdict(name, "full", (), "")
        else:
            short = list(missing[:4]) + ([f"+{len(missing) - 4} more"]
                                         if len(missing) > 4 else [])
            note = "; ".join(soft) if soft else f"without {', '.join(short)}"
            out[name] = ActionVerdict(name, "partial", missing, note)
    return out


# ---------------------------------------------------------------- OBJ reading

@dataclass
class ObjPart:
    """One `o <name>` block, measured from its own `v ` lines."""
    name: str                          # exactly as authored
    canonical: str
    index: int
    verts: int = 0
    tris: int = 0
    materials: tuple[str, ...] = ()
    lo: tuple[float, float, float] = (0.0, 0.0, 0.0)
    hi: tuple[float, float, float] = (0.0, 0.0, 0.0)

    @property
    def centre(self) -> tuple[float, float, float]:
        return tuple((a + b) / 2.0 for a, b in zip(self.lo, self.hi))  # type: ignore

    @property
    def span(self) -> tuple[float, float, float]:
        return tuple(b - a for a, b in zip(self.lo, self.hi))  # type: ignore

    @property
    def volume(self) -> float:
        x, y, z = self.span
        return x * y * z


@dataclass
class ObjFile:
    path: Path
    parts: list[ObjPart] = field(default_factory=list)
    materials: tuple[str, ...] = ()
    mtllib: str = ""
    named: bool = True                 # False when the exporter wrote no `o `/`g ` lines
    grouped_by: str = "o"

    @property
    def verts(self) -> int:
        return sum(p.verts for p in self.parts)

    @property
    def tris(self) -> int:
        return sum(p.tris for p in self.parts)

    def bounds(self):
        if not self.parts:
            return (0.0, 0.0, 0.0), (0.0, 0.0, 0.0)
        lo = tuple(min(p.lo[i] for p in self.parts) for i in range(3))
        hi = tuple(max(p.hi[i] for p in self.parts) for i in range(3))
        return lo, hi


def to_blender(x: float, y: float, z: float) -> tuple[float, float, float]:
    """OBJ coordinates -> the coordinates the rig will actually see.

    `bpy.ops.wm.obj_import` defaults to up_axis='Y', forward_axis='NEGATIVE_Z' and applies
    that conversion to EVERY file, whatever the file was authored in. So the numbers this
    module reports - height, facing, which part is above which - have to go through the
    same rotation, or they describe a model nobody will ever render. Measured on the
    reference cat: raw OBJ y-span 0.653 is the height, raw z=+0.48 is the nose, and after
    this conversion that is Blender z=0.653 tall with the nose at y=-0.48, which is exactly
    what the rig reports after import.

    The upshot for an author: model Y-up in your design tool and it lands upright here. A
    model authored Z-up arrives lying on its face - and the height and facing checks below
    will say so instead of letting you find out in a render.
    """
    return x, -z, y


def _read_obj(path: Path, marker: str = "o") -> ObjFile:
    """One streaming pass. Vertices are global in OBJ but exporters emit them per object,
    so a part's bbox is the bbox of the `v ` lines that fall inside its block - which is
    true for every exporter that writes `o` at all, and only ever used for reporting."""
    out = ObjFile(path=path, grouped_by=marker)
    parts: list[ObjPart] = []
    cur: ObjPart | None = None
    mats: list[str] = []
    cur_mat = ""
    open_marker = marker + " "
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if line.startswith("v "):
                if cur is None:
                    cur = ObjPart(name="", canonical="", index=0,
                                  lo=(1e30,) * 3, hi=(-1e30,) * 3)
                    parts.append(cur)
                    out.named = False
                try:
                    a, b, c = (float(v) for v in line[2:].split()[:3])
                except ValueError:
                    continue
                x, y, z = to_blender(a, b, c)
                cur.verts += 1
                cur.lo = (min(cur.lo[0], x), min(cur.lo[1], y), min(cur.lo[2], z))
                cur.hi = (max(cur.hi[0], x), max(cur.hi[1], y), max(cur.hi[2], z))
            elif line.startswith("f "):
                if cur is None:
                    continue
                cur.tris += max(1, len(line.split()) - 3)
                if cur_mat and cur_mat not in cur.materials:
                    cur.materials = cur.materials + (cur_mat,)
            elif line.startswith(open_marker):
                name = line[len(open_marker):].strip()
                cur = ObjPart(name=name, canonical=normalise(name), index=len(parts),
                              lo=(1e30,) * 3, hi=(-1e30,) * 3)
                parts.append(cur)
            elif line.startswith("usemtl "):
                cur_mat = line[7:].strip()
                if cur_mat and cur_mat not in mats:
                    mats.append(cur_mat)
                if cur is not None and cur_mat and cur_mat not in cur.materials:
                    cur.materials = cur.materials + (cur_mat,)
            elif line.startswith("mtllib "):
                out.mtllib = line[7:].strip()
    for p in parts:
        if p.verts == 0:
            p.lo = p.hi = (0.0, 0.0, 0.0)
    out.parts = [p for p in parts if p.verts]
    out.materials = tuple(mats)
    return out


def _count_buried(path: Path, marker: str, boxes: dict[str, tuple], parent_of: dict) -> dict:
    """Second pass: how many of each part's verts fall inside its parent's bounding box.

    A separate pass rather than keeping every vertex from the first one: parents are not
    known until the names have been resolved, and a million-vertex model would otherwise be
    held in memory to answer a yes/no question. Counting stops at the threshold.
    """
    counts = {n: 0 for n in parent_of}
    open_marker = marker + " "
    cur = box = None
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if line.startswith("v "):
                if box is None or counts[cur] > SOCKET_MIN_VERTS:
                    continue
                try:
                    a, b, c = (float(v) for v in line[2:].split()[:3])
                except ValueError:
                    continue
                v = to_blender(a, b, c)
                if all(box[0][i] <= v[i] <= box[1][i] for i in range(3)):
                    counts[cur] += 1
            elif line.startswith(open_marker):
                cur = normalise(line[len(open_marker):].strip())
                parent = parent_of.get(cur)
                box = boxes.get(parent) if parent else None
                if cur not in counts:
                    box = None
    return counts


def read_obj(path: Path) -> ObjFile:
    """Parse an OBJ's structure. Falls back to `g ` groups when it has no `o ` objects."""
    obj = _read_obj(path, "o")
    if not obj.named or len(obj.parts) <= 1:
        alt = _read_obj(path, "g")
        if len(alt.parts) > len(obj.parts) and alt.named:
            return alt
    return obj


# ---------------------------------------------------------------- the report

@dataclass(frozen=True)
class Issue:
    level: str        # "error" | "warn" | "info"
    code: str
    message: str

    def __str__(self) -> str:
        return f"[{self.level}] {self.code}: {self.message}"


@dataclass
class ModelReport:
    """Everything knowable about one installed model without opening Blender."""
    id: str
    path: Path
    meta: dict
    parts: list[ObjPart] = field(default_factory=list)
    joints: dict[str, str] = field(default_factory=dict)     # canonical -> resolved parent
    unknown: dict[str, str] = field(default_factory=dict)    # authored -> best-guess parent
    promoted: str = ""                                       # part promoted to torso, if any
    actions: dict[str, ActionVerdict] = field(default_factory=dict)
    issues: list[Issue] = field(default_factory=list)
    size: tuple[float, float, float] = (0.0, 0.0, 0.0)
    facing: str = "?"
    materials: tuple[str, ...] = ()
    verts: int = 0
    tris: int = 0

    @property
    def label(self) -> str:
        return self.meta.get("label") or f"a low-poly {self.id.replace('_', ' ')}"

    @property
    def height(self) -> float:
        return self.size[2]

    @property
    def ok(self) -> bool:
        return not [i for i in self.issues if i.level == "error"]

    def errors(self):
        return [i for i in self.issues if i.level == "error"]

    def warnings(self):
        return [i for i in self.issues if i.level == "warn"]

    def playable(self):
        return tuple(a for a, v in self.actions.items() if v.possible)


def _gap(a: ObjPart, b: ObjPart) -> float:
    """Largest axis separation between two bounding boxes. 0 means they overlap."""
    return max(max(a.lo[i] - b.hi[i], b.lo[i] - a.hi[i], 0.0) for i in range(3))


def _contains(outer: ObjPart, inner: ObjPart) -> float:
    """Fraction of `inner`'s bbox volume that falls inside `outer`'s. Cheap stand-in for
    the BVH containment test the rig does - enough to guess a parent for an unknown part."""
    overlap = 1.0
    for i in range(3):
        lo = max(outer.lo[i], inner.lo[i])
        hi = min(outer.hi[i], inner.hi[i])
        overlap *= max(0.0, hi - lo)
    return overlap / inner.volume if inner.volume > 1e-12 else 0.0


def _build_report(model_id: str, folder: Path, obj_path: Path) -> ModelReport:
    meta = _read_meta(folder)
    rep = ModelReport(id=model_id, path=obj_path, meta=meta)
    if not meta:
        rep.issues.append(Issue("info", "no_model_json",
                                "no model.json - using defaults; the writer will be told "
                                f'this model is "{rep.label}"'))
    try:
        obj = read_obj(obj_path)
    except OSError as exc:
        rep.issues.append(Issue("error", "unreadable", f"cannot read the OBJ: {exc}"))
        return rep
    rep.parts = obj.parts
    rep.materials = obj.materials
    rep.verts, rep.tris = obj.verts, obj.tris
    if not obj.parts:
        rep.issues.append(Issue("error", "empty", "the OBJ contains no geometry"))
        return rep

    lo, hi = obj.bounds()
    rep.size = tuple(round(hi[i] - lo[i], 4) for i in range(3))  # type: ignore

    # ---- names -> canonical, keeping the collision rule: more vertices wins
    by_canon: dict[str, ObjPart] = {}
    for p in sorted(obj.parts, key=lambda p: (-p.verts, p.index)):
        if not obj.named:
            continue
        if not is_known(p.canonical):
            continue
        if p.canonical in by_canon:
            rep.issues.append(Issue(
                "warn", "duplicate",
                f'"{p.name}" and "{by_canon[p.canonical].name}" are both '
                f'"{p.canonical}"; the bigger one is rigged, the other becomes an '
                f"unanimated extra"))
            continue
        by_canon[p.canonical] = p

    # ---- torso, or the promotion rule
    if "torso" not in by_canon:
        biggest = max(obj.parts, key=lambda p: (p.volume, p.verts))
        rep.promoted = biggest.name or "(unnamed mesh)"
        by_canon.pop(biggest.canonical, None)
        by_canon["torso"] = biggest
        level = "warn" if obj.named else "info"
        rep.issues.append(Issue(
            level, "no_torso",
            f'no "torso" (or "body") part; "{rep.promoted}" is the largest mesh and will '
            "be promoted to the root. Everything under it stays rigid."))

    present = set(by_canon)
    rep.joints = {name: (resolve_parent(name, present) or ROOT) for name in sorted(present)}

    # ---- unknown parts: kept, rendered, never animated, parented by containment
    known_parts = {c: p for c, p in by_canon.items()}
    for p in obj.parts:
        if p in known_parts.values():
            continue
        best, frac = "", 0.0
        for canon, kp in known_parts.items():
            f = _contains(kp, p)
            if f > frac:
                best, frac = canon, f
        if frac < 0.05:
            best = min(known_parts.items(),
                       key=lambda kv: sum((a - b) ** 2 for a, b in
                                          zip(kv[1].centre, p.centre)))[0] if known_parts \
                else "torso"
        rep.unknown[p.name or "(unnamed mesh)"] = best
        rep.issues.append(Issue(
            "info", "unknown_part",
            f'"{p.name or "(unnamed mesh)"}" is not a contract part; it will render, '
            f'parented to "{best}", and never animate'))

    # ---- geometry checks a name cannot make
    parent_of = {c: p for c, p in rep.joints.items() if p != ROOT and p in by_canon}
    boxes = {c: (p.lo, p.hi) for c, p in by_canon.items()}
    buried = (_count_buried(obj.path, obj.grouped_by, boxes, parent_of)
              if obj.named and parent_of else {})

    _check_scale(rep)
    _check_facing(rep, by_canon)
    _check_joints_touch(rep, by_canon, buried)
    _check_pairs(rep, present)
    _check_materials(rep, obj)

    rep.actions = actions_for(present)
    return rep


def _check_scale(rep: ModelReport) -> None:
    h = rep.height
    if h > 6.0 or h < 0.05:
        rep.issues.append(Issue(
            "warn", "scale",
            f"the model is {h:g} units tall - that is not metres. It will be rescaled to "
            f'{rep.meta.get("height_m", 1.0)} m; set "height_m" in model.json, or export '
            "in metres."))


def _check_facing(rep: ModelReport, by_canon: dict[str, ObjPart]) -> None:
    """The one orientation fact that cannot be recovered later: which way it looks."""
    front = next((by_canon[n] for n in ("nose", "muzzle", "jaw", "head")
                  if n in by_canon), None)
    torso = by_canon.get("torso")
    if front is None or torso is None or front is torso:
        rep.facing = "?"
        rep.issues.append(Issue(
            "info", "facing_unknown",
            "no head/muzzle/nose part, so the facing axis cannot be verified - make sure "
            "the model looks toward -Y"))
        return
    dy = front.centre[1] - torso.centre[1]
    if abs(dy) < 1e-4:
        rep.facing = "?"
        return
    rep.facing = "-Y" if dy < 0 else "+Y"
    if dy > 0:
        rep.issues.append(Issue(
            "error", "facing",
            f'"{front.name}" is {dy:.3f} BEHIND the torso: this model faces +Y. Every '
            "shot will show its back. Rotate it 180 degrees about Z and re-export."))


# blender_kit/model_rig.py:643 - below this many buried verts the rig gives up on a real
# socket and hinges the part on the bbox face nearest its parent.
SOCKET_MIN_VERTS = 20


def _check_joints_touch(rep: ModelReport, by_canon: dict[str, ObjPart],
                        buried: dict[str, int]) -> None:
    """Two one-sided tests, both sound, neither able to cry wolf.

    A gap between two bounding boxes proves the meshes do not touch - boxes contain their
    meshes, so disjoint boxes mean disjoint parts. The rig then hinges the child on a bbox
    face and the joint tears open the moment it rotates.

    `buried` counts the child's verts inside the PARENT'S BOX, which is an upper bound on
    the verts inside the parent's mesh - the number the rig actually measures with a BVH.
    So when this count is already under the rig's threshold, the degradation is certain,
    not suspected. The reverse does not hold: the reference cat's detached `tooth` sits
    50% inside the jaw's box while touching nothing, which is why the rig's own BVH pass
    is still the last word. This check finds what can be found without one.
    """
    for canon, parent in rep.joints.items():
        if parent == ROOT or parent not in by_canon:
            continue
        me, par = by_canon[canon], by_canon[parent]
        gap = _gap(me, par)
        if gap > 1e-6:
            rep.issues.append(Issue(
                "warn", "detached",
                f'"{me.name}" does not reach "{par.name}" - a {gap * 1000:.1f} mm gap. '
                "Sink it inside its parent, or it hinges on thin air."))
        elif pivot_rule(canon) == SOCKET and buried.get(canon, 0) < SOCKET_MIN_VERTS:
            rep.issues.append(Issue(
                "warn", "shallow",
                f'"{me.name}" barely enters "{par.name}" - at most '
                f'{buried.get(canon, 0)} of its {me.verts} verts are inside it, under the '
                f"{SOCKET_MIN_VERTS} the rig needs. It will hinge on a face "
                f'("RIG_WARN {canon}") and open a hole when it moves. Sink it deeper.'))


def _check_pairs(rep: ModelReport, present: set) -> None:
    for name in sorted(present):
        if not name.endswith("_left"):
            continue
        other = name[:-5] + "_right"
        if other not in present:
            rep.issues.append(Issue(
                "warn", "unpaired", f'"{name}" has no "{other}" - the model is '
                "asymmetric and every walk cycle will show it"))
    for base in CHAIN_BASES:
        nums = sorted(int(m.group(2)) for m in (_CHAIN.match(n) for n in present)
                      if m and m.group(1) == base)
        gaps = [i for i in range(1, max(nums, default=0)) if i not in nums]
        if gaps:
            rep.issues.append(Issue(
                "warn", "chain_gap",
                f"{base} chain skips {', '.join(f'{base}_{i}' for i in gaps)} - number the "
                "segments consecutively from 1"))


def _check_materials(rep: ModelReport, obj: ObjFile) -> None:
    if not obj.materials:
        rep.issues.append(Issue(
            "warn", "no_materials",
            "no usemtl in the file: every part renders the same colour and the story's fur "
            "colour has nothing to paint"))
        return
    if obj.mtllib and not (rep.path.parent / obj.mtllib).is_file():
        rep.issues.append(Issue(
            "info", "no_mtl_file",
            f'"{obj.mtllib}" is referenced but not present - harmless, the colours come '
            "from model.json or the built-in palette, not from the .mtl"))


# ---------------------------------------------------------------- discovery

_META_DEFAULTS = {
    "label": "", "aliases": (), "stands_in_for": (), "height_m": 1.0,
    "decimate": 0.15, "coat_material": "", "palette": {},
}


def _read_meta(folder: Path) -> dict:
    f = folder / "model.json"
    if not f.is_file():
        return {}
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"_broken": True}
    return data if isinstance(data, dict) else {"_broken": True}


def _scan_folders(models_dir: Path):
    """Cheap: folder names only. Runs at import, so it never opens an OBJ."""
    found, skipped = [], []
    try:
        entries = sorted(p for p in models_dir.iterdir() if p.is_dir())
    except OSError:
        return (), ()
    for folder in entries:
        mid = folder.name
        if mid.startswith((".", "_")):
            continue
        objs = sorted(folder.glob("*.obj"))
        if not MODEL_ID.match(mid):
            if objs:
                skipped.append((mid, "the folder name must be 2-24 characters of "
                                     "a-z, 0-9 and _"))
            continue
        if not objs:
            continue
        if not (folder / f"{mid}.obj").is_file():
            skipped.append((mid, f'holds {objs[0].name} but no {mid}.obj - the .obj must '
                                 f"be named after its folder"))
            continue
        found.append(mid)
    return tuple(found), tuple(skipped)


try:
    MODELS, SKIPPED = _scan_folders(MODELS_DIR)
except Exception:                                   # never take app.py down with us
    MODELS, SKIPPED = (), ()
DEFAULT_MODEL = "cat" if "cat" in MODELS else (MODELS[0] if MODELS else "")

_CACHE: dict[str, ModelReport] = {}


def refresh() -> tuple[str, ...]:
    """Re-scan the folder. Nothing is cached across process restarts anyway; this is for
    the case where a model is dropped in while the app is running."""
    global MODELS, SKIPPED, DEFAULT_MODEL
    MODELS, SKIPPED = _scan_folders(MODELS_DIR)
    DEFAULT_MODEL = "cat" if "cat" in MODELS else (MODELS[0] if MODELS else "")
    _CACHE.clear()
    return MODELS


def resolve_path(model_id: str) -> Path | None:
    """The .obj for an id, or None. `models/<id>/<id>.obj` and nothing else."""
    mid = str(model_id or "").strip().lower()
    if not MODEL_ID.match(mid):
        return None
    p = MODELS_DIR / mid / f"{mid}.obj"
    return p if p.is_file() else None


def inspect(model_id: str) -> ModelReport | None:
    """Full report for one model. Parses the OBJ once, then caches."""
    mid = str(model_id or "").strip().lower()
    if mid in _CACHE:
        return _CACHE[mid]
    obj = resolve_path(mid)
    if obj is None:
        return None
    rep = _build_report(mid, obj.parent, obj)
    _CACHE[mid] = rep
    return rep


def scan() -> list[ModelReport]:
    """Every installed model, inspected."""
    return [r for r in (inspect(m) for m in MODELS) if r is not None]


def describe(model_id: str) -> dict:
    """model.json merged over the defaults, plus what the scan found. Never raises."""
    rep = inspect(model_id)
    out = dict(_META_DEFAULTS)
    if rep is None:
        out.update({"id": str(model_id or ""), "installed": False})
        return out
    meta = {k: v for k, v in rep.meta.items() if k != "_broken"}
    out.update(meta)
    out.update({"id": rep.id, "installed": True, "label": rep.label,
                "height_m": float(meta.get("height_m") or rep.height or 1.0),
                "path": str(rep.path), "joints": tuple(rep.joints),
                "actions": rep.playable(), "ok": rep.ok})
    return out


# ---------------------------------------------------------------- for story.py

CREATURES = MODELS          # the writer may only name a model that exists


@dataclass(frozen=True)
class Resolution:
    """What the writer asked for, what it gets, and whether that is a lie."""
    model: str
    requested: str
    substituted: bool
    reason: str               # exact | alias | stands_in_for | default | none
    note: str = ""

    def __bool__(self) -> bool:
        return bool(self.model)


_ARTICLES = re.compile(r"^(a|an|the)\s+")


def _clean_request(requested: str) -> str:
    r = _ARTICLES.sub("", str(requested or "").strip().lower())
    r = re.sub(r"[\s\-]+", "_", r).strip("_")
    return re.sub(r"s$", "", r) if len(r) > 3 and not r.endswith("ss") else r


def resolve(requested: str) -> Resolution:
    """Map a creature the writer named onto a model that exists.

    Honest by construction: `substituted` is True whenever the viewer would be looking at
    something other than what was asked for, and `note` is the line to put in the job log.
    """
    want = _clean_request(requested)
    if not MODELS:
        return Resolution("", want, True, "none",
                          "No character models are installed. Put one at "
                          "lowpoly_mode/models/<id>/<id>.obj")
    if want in MODELS:
        return Resolution(want, want, False, "exact")
    for mid in MODELS:
        if want and want in {_clean_request(a) for a in describe(mid).get("aliases") or ()}:
            return Resolution(mid, want, False, "alias")
    for mid in MODELS:
        stands = {_clean_request(a) for a in describe(mid).get("stands_in_for") or ()}
        if want and want in stands:
            return Resolution(mid, want, True, "stands_in_for",
                              f'MODEL_SUBSTITUTED {want} -> {mid} '
                              f'("{mid}" is declared a stand-in for "{want}")')
    if not want:
        return Resolution(DEFAULT_MODEL, "", False, "default")
    return Resolution(DEFAULT_MODEL, want, True, "default",
                      f'MODEL_SUBSTITUTED {want} -> {DEFAULT_MODEL} '
                      f'(no "{want}" model is installed)')


def substitute(requested: str) -> tuple[str, bool]:
    """The two-value form the design's story.clean_spec asks for."""
    r = resolve(requested)
    return r.model, r.substituted


def creature_choices() -> str:
    """The prompt line: `cat (a low-poly house cat)`. Empty when nothing is installed."""
    return ", ".join(f"{m} ({describe(m)['label']})" for m in MODELS)


def require_models() -> None:
    """Fail before the writer is called and before the voiceover is bought."""
    if not MODELS:
        raise RuntimeError(
            "No character models installed. Put one at "
            f"{MODELS_DIR / '<id>' / '<id>.obj'} - see lowpoly_mode/models_library.py "
            "for the part names and the facing axis.")


# ---------------------------------------------------------------- printing

def format_report(rep: ModelReport, verbose: bool = True) -> str:
    w, h, d = rep.size
    lines = [f"{rep.id}  -  {rep.label}",
             f"  file       {rep.path}",
             f"  size       {w:g} x {d:g} x {h:g} m (w x d x h), facing {rep.facing}",
             f"  geometry   {len(rep.parts)} parts, {rep.verts} verts, {rep.tris} tris",
             f"  materials  {', '.join(rep.materials) or '(none)'}"]
    if rep.promoted:
        lines.append(f"  promoted   {rep.promoted} -> torso")
    if verbose:
        lines.append(f"  parts      ({len(rep.parts)})")
        for p in rep.parts:
            mark = "  " if is_known(p.canonical) and p.canonical in rep.joints else "??"
            same = "" if p.canonical == p.name else f" -> {p.canonical}"
            lines.append(f"    {mark} {p.name}{same}"
                         f"   [{p.verts}v {p.tris}t {'/'.join(p.materials) or '-'}]")
    lines.append(f"  joints     ({len(rep.joints)})")
    for name, parent in rep.joints.items():
        lines.append(f"       {name} -> {parent}   ({pivot_rule(name)})")
    if rep.unknown:
        lines.append(f"  extras     ({len(rep.unknown)}) rendered, never animated")
        for name, parent in rep.unknown.items():
            lines.append(f"       {name} -> {parent}")
    lines.append("  actions")
    for name in ACTIONS:
        lines.append(f"       {rep.actions[name]}")
    bad = [i for i in rep.issues if i.level != "info"]
    lines.append(f"  violations ({len(bad)})" if bad else "  violations none")
    for i in rep.issues:
        if i.level != "info":
            lines.append(f"       {i}")
    notes = [i for i in rep.issues if i.level == "info"]
    if notes:
        lines.append(f"  notes      ({len(notes)})")
        for i in notes:
            lines.append(f"       {i}")
    return "\n".join(lines)


def format_catalogue(verbose: bool = True) -> str:
    out = [f"models dir : {MODELS_DIR}",
           f"installed  : {', '.join(MODELS) or '(none)'}",
           f"default    : {DEFAULT_MODEL or '(none)'}",
           f"prompt line: {creature_choices() or '(none)'}"]
    for mid, why in SKIPPED:
        out.append(f"skipped    : {mid} - {why}")
    out.append("")
    for rep in scan():
        out.append(format_report(rep, verbose=verbose))
        out.append("")
    return "\n".join(out)


if __name__ == "__main__":                       # python -m lowpoly_mode.models_library
    print(format_catalogue())
