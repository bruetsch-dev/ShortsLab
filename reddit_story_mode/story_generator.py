"""Generate 5 original Reddit-style story candidates via the app's existing LLM helper.

Reuses agent_core._post_llm_json (WaveSpeed LLM gateway, same one the main app uses for
reasoning) so there is no second API client. Stories are ORIGINAL and rewritten - never copied
from real posts - and each is screened by story_moderator before being returned.
"""

import re
import uuid

try:
    import agent_core
except Exception:  # pragma: no cover
    agent_core = None

from . import story_moderator

STORY_COUNT = 5

_SYSTEM = (
    "You are a short-form video scriptwriter. You write ORIGINAL Reddit-style anonymous-internet "
    "stories for narrated vertical videos. You never copy real posts, real usernames, or real "
    "people. You write clean, YouTube-monetization-safe content."
)

# The user-facing generation brief (kept close to the requested prompt).
_USER = (
    "Generate exactly 5 original Reddit-style anonymous internet stories for short-form video "
    "narration.\n\n"
    "Vary the vibes across these types: relationship drama, confession, workplace drama, creepy "
    "anonymous internet story, AITA-style, revenge, and \"I found out something disturbing\".\n\n"
    "Rules for EVERY story:\n"
    "- 2-3 minutes when narrated (roughly 300-430 words in the script).\n"
    "- The FIRST sentence must be a strong hook.\n"
    "- Simple, spoken English. Emotionally engaging. High retention.\n"
    "- Each story must have a clear twist, reveal, or strong payoff.\n"
    "- Do NOT copy real Reddit posts. Do NOT mention real usernames or real people.\n"
    "- No slurs, no hate, no graphic gore, no self-harm instructions, no school threats, no "
    "terrorism, no real-person accusations, no explicit sexual content, no sexual content "
    "involving minors, no copyrighted characters, and never claim it is 100% real.\n"
    "- No captions or visual instructions inside the script - narration text only.\n\n"
    "Return VALID JSON ONLY, an object of the exact shape:\n"
    "{\"stories\": [ {\"title\": \"...\", \"hook\": \"...\", \"summary\": \"...\", "
    "\"script\": \"...\", \"estimated_duration_sec\": 150 }, ... 5 items ... ] }"
)

_WPM = 165.0  # narration words-per-minute used to estimate duration when the model omits it


def _word_count(text):
    return len([w for w in re.split(r"\s+", str(text or "").strip()) if w])


def _estimate_seconds(script, given=None):
    try:
        given = int(given)
        if 30 <= given <= 600:
            return given
    except (TypeError, ValueError):
        pass
    words = _word_count(script)
    return max(30, min(600, int(round(words / _WPM * 60.0)))) if words else 150


def _normalize(raw):
    """Coerce one raw story dict into the canonical schema."""
    story = {
        "id": "story_" + uuid.uuid4().hex[:8],
        "title": str(raw.get("title") or "").strip()[:140] or "Untitled story",
        "hook": str(raw.get("hook") or "").strip()[:300],
        "summary": str(raw.get("summary") or "").strip()[:400],
        "script": str(raw.get("script") or "").strip(),
        "estimated_duration_sec": _estimate_seconds(raw.get("script"), raw.get("estimated_duration_sec")),
        "source_type": "generated_original",
        "risk_flags": [],
    }
    return story


def generate_stories(reasoning_model=None, status_cb=None, count=STORY_COUNT):
    """Return up to `count` safe, canonical story dicts. Raises RuntimeError on hard failure."""
    if agent_core is None:
        raise RuntimeError("Story generator unavailable: agent_core could not be imported.")
    import os
    if not os.environ.get("WAVESPEED_API_KEY", "").strip():
        raise RuntimeError("WAVESPEED_API_KEY is not set - cannot generate stories.")
    model = reasoning_model or getattr(agent_core, "GPT55_MODEL", "openai/gpt-5.5")
    messages = [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": _USER}]
    if status_cb:
        status_cb("Reddit Story: asking the writer model for 5 original stories...")
    try:
        data = agent_core._post_llm_json(model, messages, max_tokens=3600, temperature=0.9, timeout=180)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"Story generation failed ({exc.__class__.__name__}: {exc}).")
    raw_list = []
    if isinstance(data, dict):
        raw_list = data.get("stories") or data.get("items") or []
    if not isinstance(raw_list, list) or not raw_list:
        raise RuntimeError("Story generation returned no usable stories.")
    out = []
    for raw in raw_list:
        if not isinstance(raw, dict):
            continue
        story = _normalize(raw)
        if _word_count(story["script"]) < 120:      # too short to be a 2-3 min narration
            continue
        is_safe, story = story_moderator.moderate_story(story)
        if not is_safe:
            if status_cb:
                status_cb(f"Reddit Story: dropped a candidate for {', '.join(story['risk_flags'])}.")
            continue
        out.append(story)
        if len(out) >= count:
            break
    if not out:
        raise RuntimeError("All generated stories were filtered out by the safety screen; try again.")
    if status_cb:
        status_cb(f"Reddit Story: {len(out)} safe stories ready.")
    return out
