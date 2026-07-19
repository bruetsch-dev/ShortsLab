"""Gemini 3.1 Pro VIDEO analysis via the WaveSpeed LLM API.

Every piece of video material the pipeline needs to understand - reference clips the user
supplies, and our OWN rendered Shorts during review/correction - goes through here. The
pattern mirrors agent_core's Gemini audio analysis: upload to WaveSpeed media storage,
chat/completions with a video part, inline base64 as the fallback.
"""
import base64
import json
import mimetypes
import os
from pathlib import Path

import pipeline

WAVESPEED_LLM_API = "https://llm.wavespeed.ai/v1/chat/completions"
# "Gemini 3.1 Pro" on WaveSpeed is served under the -preview id (the bare id 404s)
GEMINI_VIDEO_MODEL = "google/gemini-3.1-pro-preview"


def _log(cb, msg):
    if cb:
        cb(msg)
    else:
        print(msg)


def _post(payload, timeout):
    import agent_core
    return agent_core.post_json_url(WAVESPEED_LLM_API, payload, timeout=timeout)


def _messages_with_url(prompt, video_url):
    return [{
        "role": "user",
        "content": [
            {"type": "text", "text": prompt},
            {"type": "video_url", "video_url": {"url": video_url}},
        ],
    }]


def _messages_with_base64(prompt, video_path):
    mime = mimetypes.guess_type(str(video_path))[0] or "video/mp4"
    data = base64.b64encode(Path(video_path).read_bytes()).decode("ascii")
    return [{
        "role": "user",
        "content": [
            {"type": "text", "text": prompt},
            {"type": "video_url", "video_url": {"url": f"data:{mime};base64,{data}"}},
        ],
    }]


def analyze_video(video_path, prompt, status_cb=None, model=GEMINI_VIDEO_MODEL,
                  max_tokens=3600, temperature=0.1):
    """Video file + prompt -> parsed JSON object from Gemini 3.1 Pro."""
    import agent_core
    key = os.environ.get("WAVESPEED_API_KEY", "")
    if not key:
        raise RuntimeError("Video analysis needs WAVESPEED_API_KEY (Gemini 3.1 Pro).")
    video_path = Path(video_path)
    if not video_path.is_file():
        raise RuntimeError(f"Video not found: {video_path}")
    _log(status_cb, f"Uploading video to WaveSpeed media storage ({video_path.name})...")
    video_url, _ = pipeline.upload_media(video_path, key)
    payload = {
        "model": model,
        "messages": _messages_with_url(prompt, video_url),
        "temperature": temperature,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
    }

    def _attempt(timeout):
        data = _post(payload, timeout)
        content = ((data.get("choices") or [{}])[0].get("message", {}).get("content") or "").strip()
        if not content:
            raise RuntimeError("Gemini 3.1 Pro returned empty content.")
        analysis = agent_core.extract_json_object(content)
        if not isinstance(analysis, dict):
            raise RuntimeError("Gemini 3.1 Pro did not return a JSON object.")
        return analysis

    _log(status_cb, f"Analyzing video with {model}...")
    try:
        analysis = _attempt(300)
    except Exception as url_exc:
        # small files only: inline base64 fallback (data URL)
        if video_path.stat().st_size > 18_000_000:
            raise
        _log(status_cb, f"Gemini video URL mode failed ({url_exc}); trying inline video...")
        payload["messages"] = _messages_with_base64(prompt, video_path)
        analysis = _attempt(420)
    analysis["uploaded_video_url"] = video_url
    return analysis


# ------------------------------------------------------------------ reference analysis

REFERENCE_PROMPT = """You are a short-form video director analyzing a REFERENCE Short frame by frame.
Watch the whole video (with audio). Return ONE JSON object, nothing else:

{
  "summary": "2-3 sentences: what literally happens, start to end",
  "topic": "the subject matter in a few words",
  "story": {
    "hook": "what the FIRST 1-2 seconds show/say and why it stops the scroll",
    "beats": [{"t": "0.0-3.2", "visual": "what is on screen", "narration": "spoken words if any",
               "on_screen_text": "burned-in text if any"}],
    "payoff": "how the video resolves / loops"
  },
  "filming": {
    "style": "e.g. handheld phone POV / static / walking follow",
    "camera": "framing, movement, cuts - be concrete",
    "setting": "where it takes place, lighting, time of day",
    "subjects": "who/what is on screen",
    "edit_rhythm": "cut pace, any speed ramps / zooms"
  },
  "audio": {"narration_style": "voice character + delivery, or 'none'",
            "music_sfx": "music/ambient/sfx character"},
  "captions": "caption style: font vibe, placement, word-by-word or block, or 'none'",
  "why_it_works": ["3-5 bullets on the viral mechanics"],
  "language": "language spoken/written"
}"""


def analyze_reference(video_path, status_cb=None):
    """Reference clip -> structured breakdown used to brief the Mini Story pipeline."""
    return analyze_video(video_path, REFERENCE_PROMPT, status_cb=status_cb)


def reference_to_brief(analysis):
    """Analysis JSON -> compact text brief injected into the Mini Story script prompt."""
    story = analysis.get("story") or {}
    filming = analysis.get("filming") or {}
    audio = analysis.get("audio") or {}
    beats = story.get("beats") or []
    beat_lines = "\n".join(
        f"  - [{b.get('t', '?')}] {b.get('visual', '')}"
        + (f" | says: {b.get('narration')}" if b.get("narration") else "")
        + (f" | text: {b.get('on_screen_text')}" if b.get("on_screen_text") else "")
        for b in beats[:10])
    why = "\n".join(f"  - {w}" for w in (analysis.get("why_it_works") or [])[:5])
    return (
        f"REFERENCE VIDEO BRIEF (match this style, NOT the literal content):\n"
        f"Topic: {analysis.get('topic', '?')}\n"
        f"Summary: {analysis.get('summary', '')}\n"
        f"Hook: {story.get('hook', '')}\n"
        f"Beats:\n{beat_lines}\n"
        f"Payoff: {story.get('payoff', '')}\n"
        f"Filming: {filming.get('style', '')}; {filming.get('camera', '')}; "
        f"setting: {filming.get('setting', '')}; rhythm: {filming.get('edit_rhythm', '')}\n"
        f"Narration: {audio.get('narration_style', 'none')}\n"
        f"Why it works:\n{why}")


# ------------------------------------------------------------------ own-render review

REVIEW_PROMPT_TEMPLATE = """You are reviewing OUR generated 9:16 Short against its intended script.
Watch the whole video with audio. Judge it like a harsh short-form editor.

INTENDED SCRIPT:
{script}

{reference_note}

Return ONE JSON object:
{{
  "verdict": "pass" | "fix",
  "score": 0-10,
  "issues": [{{"t": "seconds or range", "severity": "high|medium|low",
              "problem": "what is wrong (visual/audio/caption/sync/story)",
              "fix": "concrete actionable correction"}}],
  "scene_mismatches": [{{"t": "range", "expected": "what the script wants here",
                         "seen": "what the video actually shows"}}],
  "strengths": ["what already works"],
  "summary": "2 sentences overall"
}}
Only report REAL defects you can see/hear; do not invent issues."""


def review_render(video_path, script, reference_analysis=None, status_cb=None):
    """Our rendered Short -> Gemini 3.1 Pro review verdict (pass/fix + concrete issues)."""
    note = ""
    if reference_analysis:
        note = ("STYLE REFERENCE it should resemble:\n"
                + reference_to_brief(reference_analysis))
    prompt = REVIEW_PROMPT_TEMPLATE.format(script=str(script or "")[:4000], reference_note=note)
    return analyze_video(video_path, prompt, status_cb=status_cb)
