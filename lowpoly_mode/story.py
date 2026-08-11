"""Prompt -> narrated story broken into shots.

One model call produces the narration AND the shot list together, because splitting them
means the shots get written against a summary of the script rather than the script itself,
and the pictures stop matching the words.

Shot durations are what the writer INTENDS. The real timing comes from the voiceover after
it is generated - `fit_shots_to_audio` stretches the plan onto the actual speech length so
the cuts land on sentences instead of on a guess.
"""

from __future__ import annotations

import agent_core

WRITER_MODEL = "anthropic/claude-opus-4.8"

SYSTEM = """You write short narrated stories told in crude, low-poly 3D - the deliberately
cheap PS1-era look, where blocky figures move stiffly through rooms built out of boxes.
The style is the joke. Nothing needs to look good.

Write for a vertical short of roughly {seconds} seconds of narration.

The narration:
  * Opens on a hook in the first sentence - a situation the viewer wants resolved.
  * Is spoken plainly, present tense, short sentences. It is read aloud by a synthetic
    voice, so no parentheses, no stage directions, no emoji, no formatting.
  * Tells ONE story with a turn in it. Setup, escalation, payoff.
  * Never mentions the animation, the style, or itself.

The shots:
  * One shot per beat of the narration, 8 to 14 of them.
  * Each shot names its own slice of the narration text, in order, covering the whole
    narration exactly once with no gaps and no repeats.
  * `scene` describes what is physically on screen, buildable from boxes, spheres,
    cylinders and flat planes: the room or place, the objects, where the figures are and
    what they do. Figures are blocky and stiff - say what they DO, not how they emote.
  * Mostly static camera. Say when a shot pushes in or tracks something.

Return JSON:
{{"title": "<short title>",
  "narration": "<the full narration as one string>",
  "shots": [{{"text": "<this shot's slice of the narration, verbatim>",
              "seconds": <intended length>,
              "scene": "<what is on screen>"}}]}}"""


def write_story(prompt: str, seconds: float = 30.0, status_cb=None,
                model: str = WRITER_MODEL) -> dict:
    """Turn the user's idea into {title, narration, shots}."""
    log = status_cb or print
    log("Writing the story and shot list...")
    agent_core.assert_wavespeed_balance(status_cb=status_cb)
    out = agent_core._post_llm_json(
        model,
        [{"role": "system", "content": SYSTEM.format(seconds=int(seconds))},
         {"role": "user", "content": str(prompt or "").strip()}],
        max_tokens=4000, temperature=0.6, timeout=420) or {}
    shots = [s for s in (out.get("shots") or []) if isinstance(s, dict) and s.get("scene")]
    if not shots:
        raise RuntimeError("The writer returned no shots.")
    narration = str(out.get("narration") or "").strip()
    if not narration:
        narration = " ".join(str(s.get("text") or "").strip() for s in shots).strip()
    for s in shots:
        try:
            s["seconds"] = max(1.0, float(s.get("seconds") or 2.5))
        except (TypeError, ValueError):
            s["seconds"] = 2.5
    log(f"Story: {out.get('title') or 'untitled'} - {len(shots)} shots, "
        f"{len(narration.split())} words")
    return {"title": str(out.get("title") or "Low poly short"),
            "narration": narration, "shots": shots}


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
    # put any rounding drift on the last shot so the video and the voice end together
    drift = float(audio_seconds) - sum(i["seconds"] for i in out)
    if out:
        out[-1]["seconds"] = max(0.8, round(out[-1]["seconds"] + drift, 2))
    return out
