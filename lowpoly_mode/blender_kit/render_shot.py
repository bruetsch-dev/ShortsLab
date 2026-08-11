"""Render ONE shot from a JSON description. No generated code anywhere.

    blender -b -noaudio -P render_shot.py -- '{"spec": {...}, "out_dir": "...", ...}'

This is the whole reason the mode works now. The previous version had a model write a
complete bpy script per shot: it produced valid scripts that rendered floating rectangles
and empty backgrounds, cost an expensive call and up to three repair rounds each, and
still failed about a third of the time.

Here the model only fills in a small spec - which props, where the cat is, what it does,
how tight the camera sits - and this file, written once and checked once, builds it. There
is no syntax to get wrong, no camera to aim, and nothing to repair.
"""

import json
import os
import sys

# The kit lives beside this file, but Blender starts with neither on sys.path, so the
# import has to come AFTER the path is set - and the path can only come from the params,
# which is why they are parsed by hand here instead of through kit.params().
_argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
P = json.loads(_argv[0]) if _argv else {}
sys.path.insert(0, P.get("kit_dir") or os.path.dirname(os.path.abspath(__file__)))

import lowpoly_kit as kit  # noqa: E402

SPEC = P.get("spec") or {}


def main():
    sc = kit.setup(P, world_color=tuple(SPEC.get("world", (0.03, 0.035, 0.05))))
    kit.ground(color=kit._colour(SPEC.get("floor", "grey"), (0.13, 0.14, 0.17)))
    kit.lights(strength=float(SPEC.get("light", 1.0)))

    subjects = []
    for item in (SPEC.get("props") or [])[:6]:
        if not isinstance(item, dict):
            continue
        at = item.get("at") or [0, 0]
        try:
            at = (float(at[0]), float(at[1]))
        except (TypeError, ValueError, IndexError):
            at = (0.0, 0.0)
        try:
            built = kit.prop(item.get("kind", "box"), at=at,
                             colour=item.get("colour", "wood"),
                             size=float(item.get("size", 1.0) or 1.0))
        except Exception:  # noqa: BLE001 - one bad prop must not lose the shot
            continue
        subjects.extend(kit.flatten(built))

    cat_spec = SPEC.get("cat") or {}
    cat = None
    if cat_spec.get("show", True):
        colour = cat_spec.get("colour", "ginger")
        rgb = {"ginger": kit.FUR, "grey": kit.FUR_GREY, "white": kit.FUR_WHITE,
               "black": kit.FUR_DARK}.get(str(colour).lower(), kit.FUR)
        cat = kit.cat(color=rgb, scale=float(cat_spec.get("scale", 1.0) or 1.0))
        start = cat_spec.get("at") or [0, 0]
        target = cat_spec.get("to") or start
        try:
            start = (float(start[0]), float(start[1]))
            target = (float(target[0]), float(target[1]))
        except (TypeError, ValueError, IndexError):
            start = target = (0.0, 0.0)
        kit.act(cat, cat_spec.get("action", "sit"), sc, target=target, start=start,
                facing=float(cat_spec.get("facing", 0) or 0))
        subjects.append(cat)

    if not subjects:                       # never frame an empty world
        subjects = [kit.prop("box", at=(0, 0))]
    # Frame on the MIDDLE of the action, not its first frame: a shot where the subject
    # walks in is composed around where it spends the shot, not where it starts.
    import bpy
    bpy.context.scene.frame_set(max(1, sc.frame_end // 2))
    # Frame on the CREATURE, not on everything in the room. Fitting the union of every
    # object made the camera retreat until a cat two metres from its bowl was a speck -
    # the shot was technically correct and useless. Props falling outside the frame is
    # ordinary film-making; a subject too small to read is not.
    focus = [cat] if cat is not None else subjects
    fill = kit.CAMERAS.get(str(SPEC.get("shot", "medium")).lower(), 0.48)
    yaw, pitch = kit.ANGLES.get(str(SPEC.get("angle", "three_quarter")).lower(),
                                (-35.0, 70.0))
    kit.frame(focus, fill=fill, yaw_deg=yaw, pitch_deg=pitch)
    kit.finish(P, sc, f"shot {SPEC.get('id', '')}")


main()
