"""The scene catalogue, and choosing one from a free-text prompt.

The scenes in `scenes/lib/` are hand-maintained source. Each declares a PARAMS dict saying
what it can vary and within what limits. This module reads those declarations, shows them
to a cheap model along with the user's prompt, and gets back one scene plus a set of
values - which it then forces back inside the declared limits before anything runs.

That split is the point. Variation comes from the parameters, which are wide: material,
size, counts, masses, camera angle. Correctness comes from the scene file, which a model
never writes. Letting a model author whole scenes produced, in one afternoon: a target too
small to read, a chain that was a single rod, half a frame of unlit floor, a wrecking ball
parked outside the frame, and a camera inside the wall.

PARAMS is read WITHOUT importing the module. A scene file imports `bpy` at the top and only
exists inside Blender; parsing the assignment is the only way to ask it what it accepts
from ordinary Python.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import agent_core

LIB = Path(__file__).resolve().parent / "scenes" / "lib"
# One short call per run, and a wrong pick throws away a whole render - twice in a row
# the cheap model answered "a weight dropped on a tall tower" with a scene that has no
# tower in it. The selection is the one place in this mode where being right matters more
# than being cheap.
SELECT_MODEL = "anthropic/claude-opus-4.8"

# Runner plumbing. A scene may document these alongside its own knobs, but they belong to
# the pipeline, not to the creative choice - showing them to the selector invites it to set
# the render resolution or the output directory.
RUNNER_KEYS = {"out_dir", "res_x", "res_y", "fps", "seconds", "samples",
               "preview_frame", "label"}


def _params_of(path: Path) -> dict | None:
    """Pull the PARAMS literal out of a scene file without executing it."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return None
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if "PARAMS" not in names:
            continue
        try:
            return ast.literal_eval(node.value)
        except (ValueError, SyntaxError):
            return None
    return None


def _literal_of(path: Path, name: str):
    """Read any module-level literal by name without importing the module."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return None
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == name for t in node.targets):
            try:
                return ast.literal_eval(node.value)
            except (ValueError, SyntaxError):
                return None
    return None


def _summary_of(path: Path) -> str:
    try:
        doc = ast.get_docstring(ast.parse(path.read_text(encoding="utf-8"))) or ""
    except (OSError, SyntaxError):
        return ""
    line = doc.strip().splitlines()[0] if doc.strip() else ""
    # Some files open with "grass_roll - a heavy cylinder rolls..."; the name is already the
    # heading everywhere this line is shown, so repeating it just eats the width.
    for sep in (" - ", " — ", ": "):
        head, _, tail = line.partition(sep)
        if tail and head.strip().casefold() in (path.stem, path.stem.replace("_", " ")):
            return tail.strip()[:1].upper() + tail.strip()[1:]
    return line


def catalogue() -> dict:
    """{scene_name: {"summary": str, "params": {...}, "path": str}} for every usable scene."""
    out = {}
    if not LIB.is_dir():
        return out
    for f in sorted(LIB.glob("*.py")):
        if f.name.startswith("_"):
            continue
        params = _params_of(f)
        if not params:
            continue
        # How long ONE take of this scene needs, as the scene itself declares it. The jelly
        # takes seven seconds to settle and a loop scene sizes a whole period, so a caller
        # that substitutes its own default renders a shot that is cut off before it happens.
        declared = params.get("seconds") if isinstance(params.get("seconds"), dict) else {}
        params = {k: v for k, v in params.items() if k not in RUNNER_KEYS}
        if not params:
            continue
        # A scene may declare SWEEP (render me once per value and join the takes, the way
        # 1kg/10kg/50kg works) and LOOP (one period is enough, repeat it). Both change how
        # the app renders the scene, so they travel with the catalogue entry.
        out[f.stem] = {"summary": _summary_of(f), "params": params, "path": str(f),
                       "sweep": _literal_of(f, "SWEEP"),
                       "loop": bool(_literal_of(f, "LOOP")),
                       "seconds": declared.get("default")}
    return out


def clamp(scene: str, values: dict, cat: dict | None = None) -> dict:
    """Force values inside what the scene declared. Unknown keys are dropped.

    No repair round exists downstream, so an invented material or a count of 10000 has to
    be corrected here or it reaches Blender.
    """
    cat = cat or catalogue()
    spec = (cat.get(scene) or {}).get("params") or {}
    clean = {}
    for key, rule in spec.items():
        if not isinstance(rule, dict):
            continue
        given = (values or {}).get(key, rule.get("default"))
        choices = rule.get("choices")
        rng = rule.get("range")
        if choices:
            clean[key] = given if given in choices else rule.get("default", choices[0])
        elif rng and len(rng) == 2:
            try:
                v = float(given)
            except (TypeError, ValueError):
                v = float(rule.get("default", rng[0]))
            v = max(float(rng[0]), min(float(rng[1]), v))
            # keep integers integral: a count of 12.4 bricks is not a thing
            if all(float(x).is_integer() for x in rng) and float(
                    rule.get("default", rng[0])).is_integer():
                v = int(round(v))
            clean[key] = v
        else:
            clean[key] = given if given is not None else rule.get("default")
    return clean


SYSTEM = """You pick and configure ONE pre-built 3D scene for a vertical "satisfying
physics" short. You do not describe geometry and you do not write code - the scenes exist
already. Your whole job is to choose the one that best carries the user's idea and to set
its parameters so the result is worth watching.

Here is everything available. Each scene lists the parameters it accepts, with the allowed
choices or numeric range:

{cat}

How to choose well:
  * Pick the scene whose ACTION matches the idea, not the one whose name shares a word
    with it. "Bowling ball through a shop window" is panes being punched through, not a
    wall being demolished.
  * Use the parameters for everything else. They are wide on purpose: material, size,
    counts, mass, camera. If the idea calls for something the parameters cannot express,
    get as close as the materials allow and say so in "compromise".
  * Bigger and heavier reads better on a phone. Prefer the upper half of a count or mass
    range unless the idea is specifically delicate.
  * Vary the camera from the obvious choice when the scene offers one.

Return JSON:
  {{"scene": "<one name from the list>",
    "params": {{...only keys that scene declared...}},
    "seconds": <length of one take, 3 to 8>,
    "title": "<short title>",
    "sweep": true|false,   // only meaningful for a scene that declares one. true renders
                           // the same setup once per swept value and joins the takes,
                           // which is the "1kg / 10kg / 50kg" format. Use it when the
                           // idea is a COMPARISON; leave it false for a single event.
    "compromise": "<what the idea asked for that the scenes cannot do, or empty>"}}"""


LISTING_BUDGET = 24000        # characters of catalogue the chooser is shown


def _listing_for(cat: dict, budget: int = LISTING_BUDGET) -> str:
    """Every scene, always. Shorten the NOTES to fit, never drop a scene.

    This used to be json.dumps(...)[:12000], which silently cut the listing off in the
    middle. With eleven scenes that removed tower_collapse and wall_smash entirely - the
    last two alphabetically - and the chooser answered "no scene stacks a tall wide tower"
    for a brief that asks for exactly that. It was right about the list it was given. The
    newest scene is always the one that disappears, so the library got worse as it grew.
    """
    def render(note_cap, with_params=True, does_cap=None):
        rows = {}
        for name, entry in cat.items():
            does = str(entry.get("summary", ""))
            row = {"does": does if does_cap is None else does[:does_cap].rstrip()}
            if with_params:
                params = {}
                for key, spec in (entry.get("params") or {}).items():
                    keep = {k: spec[k] for k in ("choices", "range", "default") if k in spec}
                    note = str(spec.get("note") or "")
                    if note and note_cap != 0:
                        keep["note"] = note if note_cap is None else note[:note_cap].rstrip()
                    params[key] = keep
                row["params"] = params
            rows[name] = row
        return json.dumps(rows, indent=1, ensure_ascii=False)

    # Degrade by SHRINKING entries, never by cutting the text: shorter notes, then no
    # notes, then no parameters, then a shorter one-liner. Every level still names every
    # scene and is still valid JSON, which slicing the dump was not.
    for kwargs in ({"note_cap": None}, {"note_cap": 220}, {"note_cap": 140},
                   {"note_cap": 90}, {"note_cap": 0},
                   {"note_cap": 0, "with_params": False},
                   {"note_cap": 0, "with_params": False, "does_cap": 90},
                   {"note_cap": 0, "with_params": False, "does_cap": 40}):
        text = render(**kwargs)
        if len(text) <= budget:
            return text
    return text          # every scene named, even if the budget is simply too small


def title_of(scene: str) -> str:
    return scene.replace("_", " ").capitalize()


def describe() -> list[dict]:
    """The catalogue in the shape the UI needs: one entry per scene, ready to render.

    The app used to show three hand-typed presets that predate this library, so nine of
    eleven scenes were unreachable from the interface and the two that were named did not
    exist as files any more. Everything the user sees now comes from here, which means a
    scene added to scenes/lib/ shows up in the app without a second edit.
    """
    out = []
    for name, entry in catalogue().items():
        params = []
        for key, spec in (entry.get("params") or {}).items():
            if not isinstance(spec, dict):
                continue
            params.append({
                "key": key,
                "label": key.replace("_", " "),
                "choices": spec.get("choices"),
                "range": spec.get("range"),
                "default": spec.get("default"),
                "note": str(spec.get("note") or ""),
            })
        sweep = entry.get("sweep") or None
        out.append({
            "scene": name,
            "title": title_of(name),
            "does": entry.get("summary", ""),
            "seconds": entry.get("seconds"),
            "params": params,
            "sweep": ({"param": sweep.get("param"), "values": sweep.get("values") or [],
                       "unit": sweep.get("unit", "")} if sweep else None),
            "loop": bool(entry.get("loop")),
        })
    return out


def resolve(scene: str, values: dict | None = None, *, sweep: bool = True,
            seconds: float | None = None, sweep_values=None) -> dict:
    """A scene the user picked themselves, in the same shape select() returns.

    No model call: the choice and the numbers are already made. Everything downstream -
    preview frame, sweep, render - reads this one shape, so a hand-picked scene and a
    chosen-by-model scene cannot drift apart.
    """
    cat = catalogue()
    if scene not in cat:
        raise RuntimeError(f"Unknown physics scene: {scene}")
    entry = cat[scene]
    params = clamp(scene, values or {}, cat)
    if "seed" in (entry.get("params") or {}) and not (values or {}).get("seed"):
        import secrets
        params["seed"] = secrets.randbelow(10000)
    try:
        secs = max(2.0, min(12.0, float(seconds or entry.get("seconds") or 4.0)))
    except (TypeError, ValueError):
        secs = float(entry.get("seconds") or 4.0)
    sw = dict(entry.get("sweep") or {}) if sweep and entry.get("sweep") else None
    if sw and sweep_values:
        # The swept values ARE the video - "1kg / 10kg / 50kg" is the whole idea - so the
        # user may set them. Each one is clamped by the same rule as any other value of
        # that parameter, because the sweep writes straight into it.
        clean = []
        for v in sweep_values:
            got = clamp(scene, {sw.get("param"): v}, cat).get(sw.get("param"))
            if isinstance(got, (int, float)) and got not in clean:
                clean.append(got)
        if clean:
            sw["values"] = clean
    return {"scene": scene, "params": params, "seconds": secs,
            "title": title_of(scene), "path": entry["path"],
            "sweep": sw,
            "loop": bool(entry.get("loop"))}


def select(prompt: str, status_cb=None, model: str = SELECT_MODEL) -> dict:
    """Free text -> {scene, params, seconds, title}. Raises if no scene fits at all."""
    log = status_cb or print
    cat = catalogue()
    if not cat:
        raise RuntimeError("No scenes in physics_mode/scenes/lib.")
    agent_core.assert_wavespeed_balance(status_cb=status_cb)
    listing = _listing_for(cat)
    log(f"Choosing a scene from {len(cat)} available...")
    out = agent_core._post_llm_json(
        model,
        [{"role": "system", "content": SYSTEM.format(cat=listing)},
         {"role": "user", "content": str(prompt or "").strip()}],
        max_tokens=1200, temperature=0.4, timeout=180) or {}
    scene = str(out.get("scene") or "")
    if scene not in cat:
        # fall back to the flagship rather than failing the run: a wrong-but-good scene
        # still produces a video, and the user can re-prompt from the approval gate.
        scene = "wall_smash" if "wall_smash" in cat else sorted(cat)[0]
        log(f"Model picked an unknown scene; falling back to {scene}.")
    params = clamp(scene, out.get("params") or {}, cat)
    # A fresh seed on every run unless the caller pinned one. Two people prompting the same
    # idea must not get the same video, and the model reliably answers 0 when left to
    # choose - the scenes are otherwise fully deterministic.
    if "seed" in ((cat[scene].get("params")) or {}) and not out.get("pin_seed"):
        import secrets
        params["seed"] = secrets.randbelow(10000)
    declared = cat[scene].get("seconds")
    try:
        seconds = max(2.0, min(12.0, float(out.get("seconds") or declared or 4.0)))
    except (TypeError, ValueError):
        seconds = float(declared or 4.0)
    if out.get("compromise"):
        log(f"Note: {out['compromise']}")
    entry = cat[scene]
    sweep = entry.get("sweep") if out.get("sweep", True) is not False else None
    if sweep:
        log(f"Scene: {scene} - sweeping {sweep.get('param')} over {sweep.get('values')}")
    else:
        log(f"Scene: {scene} - {params}")
    return {"scene": scene, "params": params, "seconds": seconds,
            "title": str(out.get("title") or scene.replace("_", " ").title()),
            "path": entry["path"], "sweep": sweep, "loop": bool(entry.get("loop"))}
