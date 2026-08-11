"""Ask a vision model whether a rendered frame actually shows the shot that was ordered.

Pixel statistics take you only so far. They catch a frame with nothing in it, and they
catch a third of the frame being dead black. They cannot catch the failure that keeps
happening: a shot that is full of geometry, correctly lit and well composed, but missing
the one object the shot is about - a wrecking ball parked above the frame edge while its
chain hangs into view.

One cheap call per preview can see that. It runs before anything expensive: the render
that follows costs half an hour of GPU time, so a few seconds of looking is free.
"""

from __future__ import annotations

import agent_core

MODEL = "google/gemini-3.5-flash-lite"


def judge_frame(path, wanted: str, model: str = MODEL,
                style_note: str = "") -> tuple[bool, str]:
    """Return (shows_it, reason). Errors return True - a flaky call must not fail a shot.

    `style_note` lets a caller say what is deliberately crude, so the judge does not
    reject a look it was asked for.
    """
    rules = ("You check single frames from a 3D animation before it is rendered in full. "
             "Fail a frame ONLY if it does not show what was ordered: a missing subject, "
             "the camera pointing the wrong way, the main object outside the frame, or an "
             "empty background. Simple or rough geometry is never a reason to fail. "
             'Return JSON {"shows_it": true|false, "reason": "<short>"}.')
    if style_note:
        rules += " " + style_note
    try:
        out = agent_core._post_llm_json(
            model,
            [{"role": "system", "content": rules},
             {"role": "user", "content": [
                 {"type": "text", "text": f"The shot should show: {wanted}"},
                 {"type": "image_url",
                  "image_url": {"url": agent_core.image_data_url(path)}}]}],
            max_tokens=300, temperature=0.0, timeout=180) or {}
    except Exception:  # noqa: BLE001
        return True, "vision check unavailable"
    if out.get("shows_it") is False:
        return False, str(out.get("reason") or "the frame does not show the shot")
    return True, str(out.get("reason") or "ok")
