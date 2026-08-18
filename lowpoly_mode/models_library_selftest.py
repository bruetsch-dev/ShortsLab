"""Self-test for models_library. Plain python, no pytest, no Blender, no network.

    python lowpoly_mode/models_library_selftest.py

Checks three things and then prints the catalogue:

  1. the contract tables have not drifted from blender_kit/model_rig.py (that module cannot
     be imported here - it imports bpy - so its contract half is exec'd out of the source),
  2. the real cat in models/cat/cat.obj scans into the joints and actions we expect,
  3. the degradation paths hold: a bird-shaped part list, a single unnamed mesh, and an
     empty library all produce a usable answer instead of an exception.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lowpoly_mode import models_library as ml   # noqa: E402

FAILED: list[str] = []


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{('  ' + detail) if detail else ''}")
    if not ok:
        FAILED.append(label)


def eq(label, got, want):
    check(label, got == want, f"got {got!r}" if got != want else "")


# ------------------------------------------------------------------ 1. drift

def rig_contract():
    """Exec the pure-python half of model_rig.py, so the two copies can be diffed."""
    src = (Path(ml.__file__).parent / "blender_kit" / "model_rig.py").read_text("utf-8")
    body = re.split(r"^# -+ the contract$", src, flags=re.M)
    if len(body) != 2:
        return None
    body = re.split(r"^# -+ colour$", body[1], flags=re.M)[0]
    ns: dict = {}
    exec("from __future__ import annotations\nimport re\n" + body, ns)   # noqa: S102
    return ns


print("contract drift vs blender_kit/model_rig.py")
rig = rig_contract()
if rig is None:
    check("contract slice found", False, "markers missing - check by hand")
else:
    eq("PARTS identical", ml.PARTS, rig["PARTS"])
    eq("ALIASES identical", ml.ALIASES, rig["ALIASES"])
    eq("CHAIN_BASES identical", ml.CHAIN_BASES, rig["CHAIN_BASES"])
    eq("SHELLS identical", ml.SHELLS, rig["SHELLS"])
    names = ["Body", "leg_L", "l_ear", "tail.001", "eye_left", "BEAK", "collar", "tail_2"]
    eq("normalise() identical", [ml.normalise(n) for n in names],
       [rig["normalise"](n) for n in names])
    present = {"torso", "neck", "head", "tail", "leg_hind_left"}
    eq("resolve_parent() identical",
       [ml.resolve_parent(n, present) for n in sorted(present)],
       [rig["resolve_parent"](n, present) for n in sorted(present)])

# ------------------------------------------------------------------ 2. the cat

print("\ndiscovery")
eq("MODELS", ml.MODELS, ("cat",))
eq("DEFAULT_MODEL", ml.DEFAULT_MODEL, "cat")
eq("nothing skipped", ml.SKIPPED, ())
check("resolve_path", ml.resolve_path("cat") is not None, "")
check("resolve_path rejects a traversal", ml.resolve_path("../cat") is None)
check("unknown id is None", ml.inspect("dragon") is None)

print("\nthe real cat")
rep = ml.inspect("cat")
eq("parts parsed", len(rep.parts), 35)
eq("verts", rep.verts, 20780)
eq("joints", len(rep.joints), 35)
eq("no unknown parts", rep.unknown, {})
eq("nothing promoted", rep.promoted, "")
eq("facing", rep.facing, "-Y")
eq("height (m)", round(rep.height, 3), 0.653)
eq("materials", rep.materials, ("fur", "cream", "pink", "sclera", "charcoal"))
eq("no errors", [str(i) for i in rep.errors()], [])
eq("label comes from model.json", rep.label, "a low-poly house cat")
eq("torso is the root", rep.joints["torso"], "root")
eq("jaw hangs off head", rep.joints["jaw"], "head")
eq("whiskers are muzzle siblings", rep.joints["whisker_l1"], "muzzle")
eq("paw parents to its own leg", rep.joints["paw_hind_right"], "leg_hind_right")
eq("pupil parents to its own eye", rep.joints["pupil_left"], "eyeball_left")
eq("every action is possible", sorted(rep.playable()), sorted(ml.ACTIONS))
# The tooth's bbox overlaps the jaw's, so no bbox test can call it detached - but only 12
# of its 24 verts are even inside the jaw's BOX, which is an upper bound on the verts
# inside the jaw's MESH, so the rig hinging it on a face is a certainty, not a guess.
eq("the tooth is reported as a shallow socket",
   [i.code for i in rep.warnings() if "tooth" in i.message], ["shallow"])
eq("nothing else is flagged",
   sorted({i.code for i in rep.warnings()}), ["shallow"])

# ------------------------------------------------------------------ 3. degradation

print("\ndegradation (part lists only - no file needed)")
bird = {"torso", "neck", "head", "muzzle", "wing_left", "wing_right",
        "leg_hind_left", "leg_hind_right", "tail"}
eq("bird: no chest, neck walks up to torso", ml.resolve_parent("neck", bird), "torso")
eq("bird: no hips, tail walks up to torso", ml.resolve_parent("tail", bird), "torso")
bird_acts = ml.actions_for(bird)
eq("bird can walk", bird_acts["walk_to"].grade, "partial")
eq("bird paws with a wing", bird_acts["paw_at"].grade, "partial")
eq("bird can look around", bird_acts["look_around"].grade, "partial")

snake = {"torso"}
snake_acts = ml.actions_for(snake)
eq("snake walk degrades", snake_acts["walk_to"].grade, "degraded")
check("snake walk says why", "gliding" in snake_acts["walk_to"].note,
      snake_acts["walk_to"].note)
eq("snake paw_at falls back", snake_acts["paw_at"].grade, "degraded")
check("snake paw_at names the fallback", "look_around" in snake_acts["paw_at"].note)
eq("snake can still jump", snake_acts["jump_on"].grade, "partial")
eq("every action still has a verdict", sorted(snake_acts), sorted(ml.ACTIONS))

print("\nresolve()")
r = ml.resolve("cat")
eq("exact", (r.model, r.substituted, r.reason), ("cat", False, "exact"))
eq("model.json alias is not a substitution",
   (ml.resolve("kitten").model, ml.resolve("kitten").substituted), ("cat", False))
r = ml.resolve("dog")
eq("dog -> cat, honestly",
   (r.model, r.substituted, r.reason), ("cat", True, "stands_in_for"))
check("dog note is loggable", r.note.startswith("MODEL_SUBSTITUTED dog -> cat"), r.note)
eq("plural and article stripped", ml.resolve("the Dogs").model, "cat")
r2 = ml.resolve("dragon")
eq("an undeclared creature still gets a model",
   (r2.model, r2.substituted, r2.reason), ("cat", True, "default"))
eq("empty request is not a substitution", ml.resolve("").substituted, False)
eq("substitute() two-value form", ml.substitute("mouse"), ("cat", True))
eq("prompt line", ml.creature_choices(), "cat (a low-poly house cat)")

print("\nempty library")
real_models, real_default = ml.MODELS, ml.DEFAULT_MODEL
ml.MODELS, ml.DEFAULT_MODEL, ml.CREATURES = (), "", ()
try:
    r = ml.resolve("cat")
    eq("resolve returns no model", (r.model, r.reason), ("", "none"))
    check("resolve explains how to fix it", "models/<id>/<id>.obj" in r.note, r.note)
    try:
        ml.require_models()
        check("require_models raises", False)
    except RuntimeError as exc:
        check("require_models raises", True, str(exc)[:60] + "...")
finally:
    ml.MODELS, ml.DEFAULT_MODEL, ml.CREATURES = real_models, real_default, real_models

print("\n" + "=" * 78)
print(ml.format_catalogue())
print("=" * 78)
print(f"{'ALL PASS' if not FAILED else str(len(FAILED)) + ' FAILED: ' + ', '.join(FAILED)}")
sys.exit(1 if FAILED else 0)
