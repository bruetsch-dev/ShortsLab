"""LLM agents for the Viral Transformation mode: Topic Strategist, Viral Formula +
Scene Planner (+Prompt Engineer folded in), Caption rules, Metadata.

Every agent has a deterministic FALLBACK so the mode still produces a valid plan
without an API key (and so tests run hermetically).
"""

import math
import os
import re

import agent_core

PHASES = ["DECLARE", "ASSESS", "ISOLATE", "PROCESS", "BUILD", "REVEAL"]

# The scene-plan schema asks for a negative_prompt per scene. NOTE: it is stored in the plan
# only - the WaveSpeed submit deliberately does NOT send negative prompts (they trip the
# safety filters; see the app-wide "positive framing only" rule). Safety lives in the
# positive prompt wording instead.
NEG_PROMPT = ("text, watermark, logo, extra limbs, distorted hands, malformed animal, "
              "gore, injury, cruelty, blurry, low quality")

# clean-footage suffix appended POSITIVELY to every generation prompt
CLEAN_SUFFIX = (", realistic vertical smartphone video look, natural lighting, clean frame "
                "with no text and no watermark and no logo, gentle and humane handling")

TOPIC_PRESETS = [
    "street dog salon", "matted cat makeover", "rusty knife restoration",
    "filthy sneaker cleaning", "abandoned toy restoration", "dirty keyboard deep clean",
    "overgrown bonsai trim", "muddy puppy wash", "broken phone restoration",
    "tiny room makeover", "old coin cleaning", "disgusting fridge cleanup",
]

_ANIMAL_WORDS = ("dog", "puppy", "cat", "kitten", "animal", "pet", "bird", "rabbit", "hamster")
_UNSAFE_WORDS = ("blood", "gore", "injur", "wound", "dead", "kill", "abuse", "cruel",
                 "weapon", "gun", "nsfw", "sex", "drug")


def _is_animal(text):
    return any(w in str(text).lower() for w in _ANIMAL_WORDS)


def _safe_topic(text):
    return not any(w in str(text).lower() for w in _UNSAFE_WORDS)


# --------------------------------------------------------------------- Topic Strategist

def _fallback_strategy(topic):
    t = str(topic or "restoration").strip().lower() or "restoration"
    animal = _is_animal(t)
    if animal:
        subject = f"a scruffy {t.split()[0] if t.split() else 'dog'}" if "dog" in t or "puppy" in t \
            else "a matted stray cat" if "cat" in t else f"a neglected animal ({t})"
        action = "gently groomed and cleaned"
        metric_number, metric_unit = 5000, "tangles"
        before = "matted, dusty fur covering the whole body, sad but calm"
        after = "fluffy, clean, shiny coat, bright eyes, happy and relaxed"
    else:
        subject = f"a badly neglected {t}"
        action = "deep-cleaned and restored"
        metric_number, metric_unit = 12, "years of grime"
        before = "covered in dirt, rust and grime, barely recognizable"
        after = "spotless, restored, looking brand new"
    title = f"I Removed {metric_number:,} {metric_unit.title()} From This {t.title()} For This"
    return {
        "narrowed_topic": t,
        "concept_title": title,
        "subject": subject,
        "action": action,
        "metric_number": metric_number,
        "metric_unit": metric_unit,
        "absurd_claim": f"{metric_number:,} {metric_unit}",
        "before_state": before,
        "after_state": after,
        "is_animal": animal,
        "safe": _safe_topic(t),
        "safety_notes": "gentle, humane, no distress" if animal else "no gore, no hazards",
    }


def topic_strategist(topic, reasoning_model=None, status_cb=None):
    """Input: user topic only. Output: narrowed viral concept + title + subject + metric +
    before/after states + safety check."""
    fallback = _fallback_strategy(topic)
    if not _safe_topic(topic):
        fallback["safe"] = False
        return fallback
    if not os.environ.get("WAVESPEED_API_KEY"):
        return fallback
    prompt = (
        "You are the Topic Strategist for a viral 'satisfying transformation' Short "
        "(the 'I Removed 5,000 Tangles From This Street Dog For This' formula).\n"
        f"User topic: \"{topic}\"\n"
        "If the topic is broad, NARROW it to one concrete, filmable transformation. Invent an "
        "ABSURD but plausible MEASURABLE metric (a big count, hours, layers, years). Define the "
        "worst believable BEFORE state and a beautiful AFTER state. For animals: strictly gentle, "
        "humane, calm - never fear, injury or distress.\n"
        'Return STRICT JSON: {"narrowed_topic": "...", "concept_title": "I ... For This", '
        '"subject": "...", "action": "...", "metric_number": 5000, "metric_unit": "tangles", '
        '"absurd_claim": "5,000 tangles", "before_state": "...", "after_state": "...", '
        '"is_animal": true|false, "safe": true|false, "safety_notes": "..."}')
    try:
        data = agent_core._post_llm_json(
            reasoning_model or agent_core.GPT55_MODEL,
            [{"role": "system", "content": "You design viral transformation Short concepts. Return JSON only."},
             {"role": "user", "content": prompt}], 700, 0.5) or {}
        for key in ("narrowed_topic", "concept_title", "subject", "before_state", "after_state"):
            if not str(data.get(key) or "").strip():
                return fallback
        data.setdefault("action", fallback["action"])
        data.setdefault("metric_number", fallback["metric_number"])
        data.setdefault("metric_unit", fallback["metric_unit"])
        data.setdefault("absurd_claim", f"{data['metric_number']} {data['metric_unit']}")
        data["is_animal"] = bool(data.get("is_animal", _is_animal(topic)))
        data["safe"] = bool(data.get("safe", True)) and _safe_topic(str(data))
        data.setdefault("safety_notes", fallback["safety_notes"])
        return data
    except Exception:
        return fallback


# ------------------------------------------------- Viral Formula + Scene Planner + Prompts

# phase -> (min scenes, max scenes) for a 10-16 scene plan; PROCESS has the most
_PHASE_SCENES = [("DECLARE", 1, 1), ("ASSESS", 2, 2), ("ISOLATE", 1, 2),
                 ("PROCESS", 4, 7), ("BUILD", 2, 2), ("REVEAL", 2, 2)]

_FALLBACK_CAPTIONS = {
    "DECLARE": ["{claim}"],
    "ASSESS": ["Measuring", "Dirt level: extreme"],
    "ISOLATE": ["Look at this", "The worst part"],
    "PROCESS": ["First cut", "Layer one", "Still going", "Getting there", "So satisfying",
                "Almost there", "Deep clean"],
    "BUILD": ["Final touches", "Almost done"],
    "REVEAL": ["For this", "Before / After"],
}

_SHOT = {
    "DECLARE": "handheld phone shot", "ASSESS": "top-down close-up", "ISOLATE": "macro close-up",
    "PROCESS": "macro close-up", "BUILD": "handheld phone shot", "REVEAL": "reveal shot",
}


def _fallback_plan(strategy):
    scenes = []
    sid = 1
    total_target = 38.0
    counts = {"DECLARE": 1, "ASSESS": 2, "ISOLATE": 2, "PROCESS": 6, "BUILD": 2, "REVEAL": 2}
    n = sum(counts.values())
    base = total_target / n
    subj = strategy["subject"]
    claim = strategy.get("absurd_claim", "5,000 tangles")
    for phase, cnt in counts.items():
        for k in range(cnt):
            state = ("before" if phase in ("DECLARE", "ASSESS", "ISOLATE")
                     else "after" if phase == "REVEAL" else "mid-process")
            dur = round(min(4.5, max(1.5, base + (0.8 if phase in ("DECLARE", "REVEAL") else -0.3))), 1)
            caption = _FALLBACK_CAPTIONS[phase][k % len(_FALLBACK_CAPTIONS[phase])].replace("{claim}", claim)
            desc = {"DECLARE": f"{subj} in its worst state, {strategy['before_state']}",
                    "ASSESS": f"hands measuring/inspecting {subj} with a tool (ruler, comb, flashlight)",
                    "ISOLATE": f"extreme macro of the worst area of {subj}",
                    "PROCESS": f"gloved hands working on {subj}: cleaning, brushing, cutting step {k + 1}, visible improvement",
                    "BUILD": f"final steps on {subj}: drying, styling, polishing, preparing the reveal",
                    "REVEAL": f"{subj} fully transformed: {strategy['after_state']}"}[phase]
            scenes.append({
                "scene_id": sid, "phase": phase, "duration_seconds": dur,
                "visual_goal": desc, "shot_type": _SHOT[phase], "subject_state": state,
                "action": strategy.get("action", "cleaning"), "caption": caption,
                "needs_reference_image": True,
                "image_prompt": f"Realistic vertical smartphone photo, {_SHOT[phase]}: {desc}{CLEAN_SUFFIX}",
                "video_motion_prompt": (
                    "Use the provided image as the exact reference for the subject, setting, "
                    f"lighting and framing. Realistic vertical handheld phone video: {desc}. "
                    "Slow natural movement, slight handheld shake, satisfying motion"
                    + CLEAN_SUFFIX),
                "negative_prompt": NEG_PROMPT,
            })
            sid += 1
    return {"title": strategy["concept_title"], "scenes": scenes,
            "total_duration": round(sum(s["duration_seconds"] for s in scenes), 1)}


def _normalize_plan(plan, strategy):
    """Enforce the fixed structure: all 6 phases in order, 10-16 scenes, durations 1.5-4.5s,
    total 30-45s, PROCESS has the most scenes, captions <= 5 words."""
    scenes = plan.get("scenes") if isinstance(plan, dict) else None
    if not isinstance(scenes, list) or not scenes:
        return _fallback_plan(strategy)
    cleaned = []
    for i, sc in enumerate(scenes):
        if not isinstance(sc, dict):
            continue
        phase = str(sc.get("phase", "")).upper().strip()
        if phase not in PHASES:
            continue
        try:
            dur = max(1.5, min(4.5, float(sc.get("duration_seconds", 3.0))))
        except (TypeError, ValueError):
            dur = 3.0
        caption = " ".join(str(sc.get("caption", "")).split()[:5]) or \
            _FALLBACK_CAPTIONS[phase][0].replace("{claim}", strategy.get("absurd_claim", ""))
        img = str(sc.get("image_prompt") or "").strip()
        vid = str(sc.get("video_motion_prompt") or "").strip()
        if not img or not vid:
            continue
        cleaned.append({
            "scene_id": len(cleaned) + 1, "phase": phase, "duration_seconds": round(dur, 1),
            "visual_goal": str(sc.get("visual_goal", ""))[:220],
            "shot_type": str(sc.get("shot_type", _SHOT[phase]))[:60],
            "subject_state": str(sc.get("subject_state", "mid-process"))[:20],
            "action": str(sc.get("action", strategy.get("action", "")))[:80],
            "caption": caption, "needs_reference_image": True,
            "image_prompt": img[:600] + CLEAN_SUFFIX,
            "video_motion_prompt": vid[:700] + CLEAN_SUFFIX,
            "negative_prompt": NEG_PROMPT,
        })
    # order scenes by the fixed phase sequence, keep relative order within a phase
    order = {p: i for i, p in enumerate(PHASES)}
    cleaned.sort(key=lambda s: order[s["phase"]])
    for i, sc in enumerate(cleaned):
        sc["scene_id"] = i + 1
    have = {s["phase"] for s in cleaned}
    if len(cleaned) < 10 or len(cleaned) > 18 or have != set(PHASES):
        return _fallback_plan(strategy)
    n_process = sum(1 for s in cleaned if s["phase"] == "PROCESS")
    if n_process < max(3, max(sum(1 for s in cleaned if s["phase"] == p) for p in PHASES if p != "PROCESS")):
        return _fallback_plan(strategy)
    total = sum(s["duration_seconds"] for s in cleaned)
    if not (28.0 <= total <= 47.0):        # rescale into the 35-42s window
        factor = 38.0 / total
        for sc in cleaned:
            sc["duration_seconds"] = round(max(1.5, min(4.5, sc["duration_seconds"] * factor)), 1)
        total = sum(s["duration_seconds"] for s in cleaned)
    return {"title": str(plan.get("title") or strategy["concept_title"])[:120],
            "scenes": cleaned[:16], "total_duration": round(total, 1)}


def plan_scenes(strategy, reasoning_model=None, status_cb=None):
    """Viral Formula + Scene Planner + Prompt Engineer in one strict-JSON pass."""
    if not os.environ.get("WAVESPEED_API_KEY"):
        return _fallback_plan(strategy)
    prompt = (
        "You are the Viral Formula + Scene Planner for a satisfying transformation Short.\n"
        f"Concept: {strategy['concept_title']}\nSubject: {strategy['subject']}\n"
        f"Action: {strategy.get('action')}\nMetric: {strategy.get('absurd_claim')}\n"
        f"Before: {strategy['before_state']}\nAfter: {strategy['after_state']}\n"
        f"Safety: {strategy.get('safety_notes')}\n\n"
        "Build the FIXED 6-phase structure in this exact order: DECLARE (hook: worst state + "
        "absurd claim) -> ASSESS (measuring/inspecting with tools, specific visual metric) -> "
        "ISOLATE (macro of the worst part) -> PROCESS (the satisfying middle: repeated "
        "micro-actions, MOST scenes here, each visibly improving the subject) -> BUILD "
        "(drying/styling/final steps, 'almost done') -> REVEAL (clean final result, "
        "before/after, end on a clean final shot).\n"
        "Timeline: 12-16 scenes, each 1.5-4.5 seconds, total 35-42 seconds.\n"
        "Per scene write:\n"
        "- caption: 1-5 simple bold words that raise curiosity (e.g. '5,000 tangles', 'First "
        "cut', 'Layer one', 'Still going', 'Almost clean', 'For this'). No punctuation spam.\n"
        "- image_prompt: a realistic vertical smartphone PHOTO of that exact moment. Describe "
        "the subject identically in every scene (same colors, size, markings, setting) so it "
        "stays consistent. Concrete physical detail, no vague words.\n"
        "- video_motion_prompt: start with 'Use the provided image as the exact reference for "
        "the subject, setting, lighting and camera framing.' then describe a realistic vertical "
        "handheld phone video of the action: physical camera language, slow macro movement, "
        "slight natural shake, satisfying motion. Gentle and humane for animals. Never mention "
        "text, watermarks or logos as things to show.\n\n"
        'Return STRICT JSON: {"title": "...", "scenes": [{"scene_id": 1, "phase": "DECLARE", '
        '"duration_seconds": 3.0, "visual_goal": "...", "shot_type": "macro close-up|handheld '
        'phone shot|top-down|reveal shot", "subject_state": "before|mid-process|after", '
        '"action": "...", "caption": "...", "needs_reference_image": true, '
        '"image_prompt": "...", "video_motion_prompt": "..."}, ...]}')
    try:
        data = agent_core._post_llm_json(
            reasoning_model or agent_core.GPT55_MODEL,
            [{"role": "system", "content": "You plan viral transformation Shorts as strict JSON scene plans. Return JSON only."},
             {"role": "user", "content": prompt}], 3600, 0.4) or {}
        return _normalize_plan(data, strategy)
    except Exception:
        return _fallback_plan(strategy)


# --------------------------------------------------------------------------- Metadata

def make_metadata(strategy, reasoning_model=None):
    base_title = strategy.get("concept_title") or "I Transformed This For This"
    fallback = {
        "youtube_title": base_title[:95],
        "tiktok_title": base_title[:90],
        "description": (f"{base_title}. Watch the full transformation from "
                        f"{strategy.get('before_state', 'before')} to "
                        f"{strategy.get('after_state', 'after')}. AI-generated visuals."),
        "hashtags": ["#satisfying", "#transformation", "#asmr", "#restoration",
                     "#oddlysatisfying", "#shorts"],
        "internal_name": re.sub(r"[^a-z0-9]+", "_",
                                str(strategy.get("narrowed_topic", "transformation")).lower()).strip("_")[:40],
    }
    if not os.environ.get("WAVESPEED_API_KEY"):
        return fallback
    try:
        data = agent_core._post_llm_json(
            reasoning_model or agent_core.GPT55_MODEL,
            [{"role": "system", "content": "You write viral Shorts metadata. Return JSON only."},
             {"role": "user", "content": (
                 f"Concept: {base_title}\nSubject: {strategy.get('subject')}\n"
                 "Write metadata for a satisfying transformation Short using the 'I ... For This' "
                 "title formula (do NOT copy real channels' exact titles). "
                 'Return STRICT JSON: {"youtube_title": "...", "tiktok_title": "...", '
                 '"description": "...", "hashtags": ["#...", ...], "internal_name": "snake_case"}')}],
            500, 0.6) or {}
        for key, val in fallback.items():
            data.setdefault(key, val)
        if not isinstance(data.get("hashtags"), list):
            data["hashtags"] = fallback["hashtags"]
        return data
    except Exception:
        return fallback
