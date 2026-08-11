"""Prompt -> narrated story broken into shots, each shot a SPEC the renderer can execute.

One model call produces everything: the narration, the shot list, and for each shot a small
JSON description of what is on screen. Nothing downstream calls a model again.

That is the whole design change. The first version had an expensive model write a complete
Blender script per shot - twelve calls with up to three repair rounds each, and about a
third of the shots still rendered empty background because a text contract cannot tell you
whether your camera is pointing at your subject. Here the model picks from a fixed
catalogue of creatures, props, actions and camera framings, and a hand-written renderer
builds it. There is no syntax to get wrong and no camera to aim.
"""

from __future__ import annotations

import agent_core

# Cheap on purpose. This is a form-filling job against a fixed catalogue, not a reasoning
# one, and it runs once per video.
WRITER_MODEL = "google/gemini-3.5-flash"

CREATURES = ("cat", "dog", "mouse", "bear", "cow", "bird", "human", "robot")
PROPS = ("box", "bowl", "ball", "table", "sofa", "window", "wall", "rug", "plant")
ACTIONS = ("sit", "walk_to", "run", "jump_on", "paw_at", "look_around", "sleep")
SHOTS = ("wide", "medium", "close")
ANGLES = ("front", "side", "high", "low", "three_quarter")
COLOURS = ("wood", "cream", "red", "blue", "green", "grey", "white", "black",
           "pink", "yellow")
FURS = ("ginger", "grey", "white", "black")

SYSTEM = """You write short narrated stories told in crude, low-poly 3D - the deliberately
cheap PS1-era look, where blocky figures move stiffly through rooms built out of boxes.
The style is the joke. Nothing needs to look good.

Write for a vertical short of roughly {seconds} seconds of narration.

NARRATION
  * Opens on a hook in the first sentence: a situation the viewer wants resolved.
  * Plain, present tense, short sentences, spoken by a synthetic voice - so no
    parentheses, no stage directions, no emoji, no formatting.
  * One story with a turn in it: setup, escalation, payoff.
  * Never mentions the animation or itself.

SHOTS
  * 5 to 9 of them. Each takes its own slice of the narration, in order, covering the whole
    narration exactly once with no gaps and no repeats.
  * You do NOT describe geometry or write code. You fill in a spec from these lists ONLY.
    Anything outside them is ignored and replaced by a default.

    creature : {creatures}
    fur      : {furs}
    action   : {actions}
    prop     : {props}
    colour   : {colours}
    shot     : {shots}          (how tight the framing is)
    angle    : {angles}

  * Coordinates are metres on a flat floor, roughly -3..3 in x and y. The creature starts
    at "at" and, for walk_to / run / jump_on, ends at "to". Put props where they belong in
    the story - a bowl in front of the cat, a box it jumps onto - and keep everything
    within about 2 metres of each other so one frame holds it all.
  * Keep the SAME creature and fur across every shot unless the story needs a second one.
  * Vary shot and angle between beats; do not use the same pair twice in a row.

Return JSON:
{{"title": "<short title>",
  "narration": "<the full narration as one string>",
  "shots": [{{"text": "<this shot's slice of the narration, verbatim>",
              "seconds": <intended length>,
              "spec": {{"creature": "cat", "fur": "ginger", "action": "walk_to",
                       "at": [-1.2, 0], "to": [0.3, 0], "facing": 0,
                       "props": [{{"kind": "bowl", "at": [0.6, 0], "colour": "blue"}}],
                       "shot": "medium", "angle": "three_quarter",
                       "floor": "grey"}}}}]}}"""


def write_story(prompt: str, seconds: float = 30.0, status_cb=None,
                model: str = WRITER_MODEL) -> dict:
    """Turn the user's idea into {title, narration, shots[{text, seconds, spec}]}."""
    log = status_cb or print
    log("Writing the story and shot specs...")
    agent_core.assert_wavespeed_balance(status_cb=status_cb)
    out = agent_core._post_llm_json(
        model,
        [{"role": "system", "content": SYSTEM.format(
            seconds=int(seconds), creatures=", ".join(CREATURES), furs=", ".join(FURS),
            actions=", ".join(ACTIONS), props=", ".join(PROPS),
            colours=", ".join(COLOURS), shots=", ".join(SHOTS),
            angles=", ".join(ANGLES))},
         {"role": "user", "content": str(prompt or "").strip()}],
        max_tokens=4000, temperature=0.6, timeout=420) or {}

    shots = [s for s in (out.get("shots") or []) if isinstance(s, dict)]
    if not shots:
        raise RuntimeError("The writer returned no shots.")
    narration = str(out.get("narration") or "").strip()
    if not narration:
        narration = " ".join(str(s.get("text") or "").strip() for s in shots).strip()
    for i, s in enumerate(shots):
        try:
            s["seconds"] = max(1.0, float(s.get("seconds") or 2.5))
        except (TypeError, ValueError):
            s["seconds"] = 2.5
        s["spec"] = clean_spec(s.get("spec"), index=i)
    log(f"Story: {out.get('title') or 'untitled'} - {len(shots)} shots, "
        f"{len(narration.split())} words")
    return {"title": str(out.get("title") or "Low poly short"),
            "narration": narration, "shots": shots}


def _pair(value, default=(0.0, 0.0)):
    try:
        return [max(-4.0, min(4.0, float(value[0]))), max(-4.0, min(4.0, float(value[1])))]
    except (TypeError, ValueError, IndexError):
        return list(default)


def _one_of(value, allowed, default):
    v = str(value or "").strip().lower()
    return v if v in allowed else default


def clean_spec(spec, index: int = 0) -> dict:
    """Force a spec onto the catalogue.

    Every field is validated rather than trusted: a model that invents "action": "ponder"
    or puts a prop at x=900 must still produce a renderable shot, because there is no
    repair round to fall back on any more - that is the point of dropping code generation.
    """
    spec = spec if isinstance(spec, dict) else {}
    at = _pair(spec.get("at"), (0.0, 0.0))
    to = _pair(spec.get("to"), tuple(at))
    action = _one_of(spec.get("action"), ACTIONS, "sit")
    props = []
    for item in (spec.get("props") or [])[:5]:
        if not isinstance(item, dict):
            continue
        props.append({"kind": _one_of(item.get("kind"), PROPS, "box"),
                      "at": _pair(item.get("at"), (1.0, 0.5)),
                      "colour": _one_of(item.get("colour"), COLOURS, "wood"),
                      "size": max(0.4, min(2.5, float(item.get("size", 1.0) or 1.0)))})
    # alternate the framing when the model repeats itself, so cuts still read as cuts
    shot = _one_of(spec.get("shot"), SHOTS, SHOTS[index % len(SHOTS)])
    angle = _one_of(spec.get("angle"), ANGLES, ANGLES[index % len(ANGLES)])
    try:
        facing = float(spec.get("facing", 0) or 0)
    except (TypeError, ValueError):
        facing = 0.0
    return {
        "id": index + 1,
        "cat": {"kind": _one_of(spec.get("creature"), CREATURES, "cat"),
                "colour": _one_of(spec.get("fur"), FURS, "ginger"),
                "action": action, "at": at, "to": to, "facing": facing,
                "scale": max(0.4, min(2.5, float(spec.get("scale", 1.0) or 1.0)))},
        "props": props,
        "shot": shot,
        "angle": angle,
        "floor": _one_of(spec.get("floor"), COLOURS, "grey"),
    }


def fit_shots_to_audio(shots, audio_seconds: float) -> list:
    """Scale the planned shot lengths onto the real voiceover length.

    The writer's per-shot seconds are an estimate; the synthesised voice is whatever it is.
    Scaling proportionally keeps each shot covering its own sentences, which is what makes
    the cut feel written rather than chopped to a metronome.
    """
    planned = sum(float(s.get("seconds") or 0) for s in shots) or 1.0
    scale = float(audio_seconds) / planned
    out = []
    for s in shots:
        item = dict(s)
        item["seconds"] = max(0.8, round(float(s.get("seconds") or 2.5) * scale, 2))
        out.append(item)
    drift = float(audio_seconds) - sum(i["seconds"] for i in out)
    if out:
        out[-1]["seconds"] = max(0.8, round(out[-1]["seconds"] + drift, 2))
    return out
