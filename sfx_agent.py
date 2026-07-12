"""AI sound-effect post-production for an already-finished Short.

Upload a rendered vertical video and this module:
  1. detects visual cuts / image changes with ffmpeg scene detection,
  2. transcribes the speech with timing (Gemini 3.5 Flash via WaveSpeed),
  3. asks an Opus 4.8 agent to place fitting sound effects (whooshes on cuts,
     impacts/stingers to emphasise key spoken moments, risers for tension, ...),
  4. mixes the chosen SFX from the local soundeffects/ library UNDER the original
     audio, copying the video stream untouched (lossless, fast).

The LLM is primary; a deterministic fallback places transition whooshes on the
detected cuts so the feature still works without an API key.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import agent_core
import pipeline

ROOT = Path(__file__).resolve().parent
SFX_OUTPUT_DIR = ROOT / "projects" / "_sfx_enhanced"

# Agent-facing category -> local library folder + mixing defaults.
AGENT_SFX_CATEGORIES = {
    "transition": {"lib": "subtle_transitions", "vol": 0.26, "max_dur": 0.9,
                   "desc": "soft whoosh/slide for a cut or image change"},
    "whoosh":     {"lib": "motion_whoosh_like", "vol": 0.28, "max_dur": 0.9,
                   "desc": "motion whoosh for fast movement, a swipe, or a reveal pushing in"},
    "impact":     {"lib": "subtle_impacts",     "vol": 0.30, "max_dur": 1.0,
                   "desc": "subtle impact to punctuate an important word"},
    "boom":       {"lib": "hits_impacts",       "vol": 0.40, "max_dur": 1.4,
                   "desc": "stronger hit/boom for a dramatic reveal, shock, or hard fact"},
    "stinger":    {"lib": "jingles_stingers",   "vol": 0.30, "max_dur": 1.6,
                   "desc": "short musical stinger to mark a punchline or twist"},
    "riser":      {"lib": "subtle_tones",       "vol": 0.24, "max_dur": 2.2,
                   "desc": "tension riser / tone building anticipation before a payoff"},
    "foley":      {"lib": "serious_foley",      "vol": 0.30, "max_dur": 1.4,
                   "desc": "diegetic foley matching an on-screen action or object"},
    "click":      {"lib": "cuts_clicks_ui",     "vol": 0.22, "max_dur": 0.5,
                   "desc": "tiny click/tick for a quick cut, counter, or UI accent"},
}
INTENSITY_GAIN = {"subtle": 0.78, "medium": 1.0, "strong": 1.32}
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".webm", ".mkv"}


def log(status_cb, message):
    if status_cb:
        status_cb(message)


def _run(cmd, timeout=None):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def media_duration(path, ffprobe):
    if not ffprobe:
        return 0.0
    try:
        result = _run([ffprobe, "-v", "error", "-show_entries", "format=duration",
                       "-of", "default=nokey=1:noprint_wrappers=1", str(path)], timeout=30)
        return float((result.stdout or "0").strip() or 0.0)
    except Exception:
        return 0.0


def detect_existing_sfx_onsets(video_path, ffmpeg, status_cb=None):
    """Detect sharp audio transients already in the uploaded video (existing SFX/whooshes/impacts).
    Returns their timestamps (s). Pure stdlib WAV RMS (no audioop - removed in Py 3.13): a hop whose
    loudness spikes well above the local baseline is an onset. Used so the SFX Master does NOT stack
    a NEW transition sound on a cut that already has one."""
    import wave, array, math, tempfile
    if not ffmpeg:
        return []
    tmp = Path(tempfile.gettempdir()) / f"_sfxonset_{os.getpid()}_{int(time.time()*1000)}.wav"
    try:
        # HIGH-PASS at 4.5 kHz first: speech is band-limited, so this removes almost all of it and
        # leaves broadband transients (whooshes/clicks/impacts). Detection is deliberately
        # CONSERVATIVE - only clearly LOUD existing SFX are flagged - because quiet ducked SFX look
        # just like sibilant speech in this band, and over-skipping would make the Master add
        # nothing on a voice-only clip. So: it won't stack a new whoosh on an obvious existing one,
        # but very subtle existing SFX may go undetected (adding a soft whoosh there is harmless).
        subprocess.run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(video_path),
                        "-vn", "-ac", "1", "-ar", "16000", "-af", "highpass=f=4500",
                        "-f", "wav", str(tmp)], capture_output=True, timeout=180)
        if not tmp.exists() or tmp.stat().st_size < 1024:
            return []
        with wave.open(str(tmp), "rb") as wf:
            if wf.getsampwidth() != 2:
                return []
            sr = wf.getframerate()
            samples = array.array("h"); samples.frombytes(wf.readframes(wf.getnframes()))
        hop = max(1, int(sr * 0.02))          # 20 ms windows
        rmses = []
        for i in range(0, len(samples) - hop, hop):
            chunk = samples[i:i + hop]
            rmses.append(math.sqrt(sum(s * s for s in chunk) / len(chunk)))
        if not rmses:
            return []
        # CONSERVATIVE floor: only a strong HF burst well above the clip's 90th-percentile HF level
        # counts (a real loud whoosh/impact). Tuned so a voice-only clip yields ~0 detections.
        ref = sorted(rmses)[int(len(rmses) * 0.90)]
        floor = max(1800.0, ref * 4.5)
        onsets, base = [], []
        for k, rms in enumerate(rmses):
            t = k * hop / float(sr)
            if base:
                med = sorted(base)[len(base) // 2] or 1.0
                if rms > floor and rms > med * 3.0 and (not onsets or t - onsets[-1] > 0.12):
                    onsets.append(round(t, 3))
            base.append(rms)
            if len(base) > 8:
                base.pop(0)
        log(status_cb, f"Existing-audio scan: {len(onsets)} SFX-like transient(s) already present "
                       f"(voice removed via high-pass).")
        return onsets
    except Exception as exc:  # noqa: BLE001
        log(status_cb, f"Existing-audio scan skipped ({exc}).")
        return []
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


def detect_scene_cuts(video_path, ffmpeg, threshold=0.30, min_gap=0.45):
    """Return sorted timestamps (s) where the picture changes hard enough."""
    if not ffmpeg:
        return []
    try:
        result = _run(
            [ffmpeg, "-hide_banner", "-i", str(video_path),
             "-filter_complex", f"select='gt(scene,{threshold})',showinfo",
             "-an", "-f", "null", "-"],
            timeout=240,
        )
    except Exception:
        return []
    times = []
    for match in re.finditer(r"pts_time:([0-9]+\.?[0-9]*)", result.stderr or ""):
        try:
            times.append(round(float(match.group(1)), 3))
        except ValueError:
            continue
    times.sort()
    spaced = []
    for t in times:
        if t < 0.2:
            continue
        if spaced and t - spaced[-1] < min_gap:
            continue
        spaced.append(t)
    return spaced


# word-level ASR times of the LAST transcription (same in-process job) - lets the resolver
# snap meaning-bound reactions (death gong...) onto the exact spoken word instead of trusting
# the vision director's rough timestamp (which used to land the gong seconds early, on a cut).
LAST_ASR_WORDS = []


def _phrases_from_words(words, max_gap=0.6, max_words=12):
    """Group frame-accurate ASR words into short timed phrases for the LLM prompt."""
    phrases, cur = [], []
    for w in words:
        if cur and (w["start"] - cur[-1]["end"] > max_gap
                    or len(cur) >= max_words
                    or cur[-1]["word"][-1:] in ".!?"):
            phrases.append({"start": round(cur[0]["start"], 2), "end": round(cur[-1]["end"], 2),
                            "text": " ".join(x["word"] for x in cur)})
            cur = []
        cur.append(w)
    if cur:
        phrases.append({"start": round(cur[0]["start"], 2), "end": round(cur[-1]["end"], 2),
                        "text": " ".join(x["word"] for x in cur)})
    return phrases


def transcribe_with_timing(video_path, ffmpeg, ffprobe, duration, status_cb=None):
    """Extract audio and return timed phrases [{start,end,text}].

    Prefers the local frame-accurate word aligner (faster-whisper); falls back to
    Gemini if the aligner is unavailable and an API key is set.
    """
    try:
        import voice_align
        local_ok = voice_align.available()
    except Exception:
        local_ok = False
    if not local_ok and not os.environ.get("WAVESPEED_API_KEY"):
        log(status_cb, "No local aligner and no WAVESPEED_API_KEY; cuts-only mode.")
        return []
    audio_path = video_path.with_suffix(".sfx_audio.mp3")
    try:
        _run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(video_path),
              "-vn", "-ac", "1", "-ar", "44100", "-c:a", "libmp3lame", "-b:a", "64k",
              str(audio_path)], timeout=180)
        if not audio_path.exists():
            return []
        # Prefer the local frame-accurate word aligner (no hallucination on real speech).
        try:
            import voice_align
            if voice_align.available():
                log(status_cb, "Transcribing speech with local word aligner...")
                asr_words = voice_align.transcribe_words(str(audio_path), status_cb=status_cb)
                if asr_words:
                    # keep the WORD-level times: resolve_vision_segments snaps reaction
                    # sounds (death gong etc.) onto the exact spoken word
                    global LAST_ASR_WORDS
                    LAST_ASR_WORDS = list(asr_words)
                    return _phrases_from_words(asr_words)
        except Exception as exc:
            log(status_cb, f"Local transcription failed ({exc}); trying Gemini...")
        log(status_cb, "Transcribing speech with Gemini 3.5 Flash...")
        analysis = agent_core.analyze_audio_with_gemini(
            audio_path, title="Uploaded Short", script_hint="", status_cb=status_cb,
            audio_duration=duration or None,
        )
        scenes, _ = agent_core.normalize_audio_scenes(
            analysis, fallback_script="", fallback_duration=duration or 30.0,
            audio_duration=duration or None, return_source=True,
        )
        phrases = []
        for scene in scenes:
            text = (scene.get("script") or scene.get("exact_voice_text") or "").strip()
            if not text:
                continue
            phrases.append({
                "start": round(float(scene.get("start", 0.0)), 2),
                "end": round(float(scene.get("end", 0.0)), 2),
                "text": text,
            })
        return phrases
    except Exception as exc:
        log(status_cb, f"Transcription failed ({exc}); continuing with cuts only.")
        return []
    finally:
        try:
            audio_path.unlink(missing_ok=True)
        except Exception:
            pass


def library_catalog(config):
    """Categories offered to the LLM: those present in the local library, plus --
    when SFX generation is enabled -- every category, since a missing one can be
    generated on demand via Kling."""
    gen = bool(config.get("sfx_generation_enabled", False))
    catalog = {}
    for name, meta in AGENT_SFX_CATEGORIES.items():
        files = pipeline.sfx_category_files(config, meta["lib"])
        if files or gen:
            catalog[name] = {"desc": meta["desc"], "available": len(files)}
    return catalog


# User-selectable SFX density. "low" matches the app's historical behaviour; medium/high raise the
# per-minute budget AND tell the reasoning model to place more, denser effects.
SFX_AMOUNT_PROFILES = {
    "low": {
        "max_per_minute": 42, "budget_cap": 52,
        "plan": "Keep it sparse and intentional: roughly 1 effect every 2-4 seconds, "
                "never stack two within 0.4s. When unsure, leave the moment silent.",
        "review": "Be strict: when a pick does not clearly fit the moment, DROP it - "
                  "silence beats a wrong sound.",
    },
    "medium": {
        "max_per_minute": 65, "budget_cap": 80,
        "plan": "Aim for a lively, professionally edited feel: roughly 1 effect every 1.5-2.5 "
                "seconds. Cover EVERY real scene change and every emphasised word/number/reveal; "
                "never stack two within 0.3s.",
        "review": "Balance quality and coverage: replace a wrong pick with a better file instead "
                  "of dropping it; only drop when nothing in the library fits.",
    },
    "high": {
        "max_per_minute": 95, "budget_cap": 110,
        "plan": "Make it a DENSE hyper-edited TikTok mix: roughly 1 effect every 0.8-1.5 seconds. "
                "Every cut gets a transition sound, every emphasised word/number/name gets a hit, "
                "add risers into payoffs and foley wherever an on-screen action supports it; "
                "never stack two within 0.25s.",
        "review": "Preserve density: NEVER drop an event just because it feels busy - replace a "
                  "wrong file with a better-fitting one. Only drop if the sound would actively "
                  "clash with speech.",
    },
}


def sfx_amount_profile(amount):
    return SFX_AMOUNT_PROFILES.get(str(amount or "medium").strip().lower(),
                                   SFX_AMOUNT_PROFILES["medium"])


# =====================================================================================
# VISION AUDIO DIRECTOR - multimodal SFX placement (Gemini watches + hears the video).
# The WHOLE video (visuals + existing audio) is uploaded and sent to a Gemini model,
# which returns a timed sfx_timeline JSON using the human-labeled trainer categories
# (hook_riser / impact / ui / transition / reaction:<slug>). Used by the SFX Master
# (uploaded videos) and as the post-render semantic pass in Script-to-Visuals mode.
# =====================================================================================

def _vision_model(reasoning_model):
    """Video+audio input needs a Gemini model; force one if the dropdown picked e.g. Opus."""
    m = str(reasoning_model or "").strip()
    return m if "gemini" in m.lower() else "google/gemini-3.1-pro-preview"


def labeled_sfx_data(status_cb=None):
    """The label-driven library (roles/reactions ground truth from tools/sfx_trainer)."""
    import sfx_library
    return sfx_library.build_library(status_cb=status_cb)


def vision_tag_catalog(data, transitions_present=False):
    """Exact tags the director may use, built from pools that actually have sounds."""
    tags = []
    if data.get("hook_risers") or data.get("risers"):
        tags.append("hook_riser")
    lib = data.get("library", {})
    if lib.get("impact_hit"):
        tags.append("impact")
    if lib.get("ui_click") or lib.get("caption_pop"):
        tags.append("ui")
    if not transitions_present and any(lib.get(c) for c in
                                       ("bright_whoosh", "swipe_whoosh", "whoosh_hit_combo")):
        tags.append("transition")
    for slug, files in sorted((data.get("reactions") or {}).items()):
        if files:
            tags.append(f"reaction: {slug}")
    return tags


def build_sfx_director_prompt(tags, transitions_present=False):
    """System prompt for the multimodal Audio Director call."""
    tag_lines = "\n".join(f'- "{t}"' for t in tags)
    if transitions_present:
        redundancy = (
            "CRITICAL RULE - AVOID REDUNDANCY:\n"
            "This video's editor has ALREADY placed a transition/whoosh sound on every cut. "
            "You are STRICTLY FORBIDDEN from adding any structural, transition, or cut sounds. "
            "Your job is ONLY to add \"Semantic\" and \"Reaction\" SFX based on the content."
        )
    else:
        redundancy = (
            "CRITICAL RULE - AVOID REDUNDANCY:\n"
            "First, listen to the provided audio track and the video clip. Identify timestamps "
            "where the video editor has ALREADY placed structural/transition sounds (e.g., "
            "whooshes on cuts, impacts on scene changes). You are STRICTLY FORBIDDEN from adding "
            "new structural, transition, or cut sounds if there is already a transition/cut sound "
            "where you wanted to place one. Your job is to add \"Semantic\" and \"Reaction\" SFX "
            "based on the content. If there are no transition/cut sounds present, your job is "
            "also to add \"transition\" sounds from the according library."
        )
    return f"""You are an expert Short-Form Video Audio Director. Your task is to analyze a highly edited, fast-paced video and return a precise JSON payload for Sound Effect (SFX) placement. You must analyze BOTH the visual track and the audio track simultaneously.

{redundancy}

RULES FOR SFX PLACEMENT:

1. EXACT AUDIO LIBRARY:
You MUST choose ONLY from the following exact tags. Do not invent tags:
{tag_lines}

2. THE HOOK BUILD-UP (0.00 to about 5 seconds, depending on the script):
- You MUST include exactly one "hook_riser" at the very beginning of the video.
- The "hook_riser" ALWAYS starts at timestamp 0.00.
- The `end_timestamp` of the "hook_riser" MUST perfectly match the `timestamp` of the main impact visual/audio trigger inside the hook.
- At that `end_timestamp` you MUST also add one single "impact" event (the riser's climax).
- You are STRICTLY FORBIDDEN from using "hook_riser" (or any other riser) after the hook.

3. VISUAL TRIGGERS (Priority 1):
Place SFX on clear editorial emphasis moments:
- An arrow, circle, pointer, or other emphasis overlay appears for the first time.
- A new visual example is revealed through a strong cut or noticeable change of subject.
- The main visible action reaches its clearest impact moment.
- A flash, whip, fast zoom, swipe, or other deliberate transition occurs.
- A comedic, surprising, awkward, or reaction-based visual punchline appears.
- A presenter makes a strong gesture or reaction that clearly emphasizes the narration.

VISUAL COVERAGE IS MANDATORY:
- At least 60% of NON-TRANSITION semantic/reaction events must use `trigger_source: "visual"`.
- Aim for one genuine visual semantic event every 6-9 seconds when the footage contains one.
- A generic cut/scene change is only a `transition`; it does NOT count as a visual semantic event.
- Do not let easy transcript keywords replace analysis of visible actions, objects, gestures or overlays.

Do not trigger SFX for:
- normal word-by-word captions
- overlays that are already visible
- minor body movement
- static labels
- continuous footage without a new emphasis event

4. AUDIO/KEYWORD TRIGGERS (Priority 2):
Map reaction SFX to the exact start timestamp of high-impact spoken words:
- Negative absolutes (e.g., "banned", "illegal", "never", "fail") -> "reaction: error_wrong".
- Crazy facts, rules, or core subjects (e.g., "China", "Japan", "wildest") -> "reaction: shock_reveal" or "reaction: suspense".
- Enumerations or lists (e.g., "First", "Next", "Number one") -> "reaction: notification".
- Money/Cost related words -> "reaction: money_cash".
- Match the OTHER reaction tags to lines whose meaning clearly fits them.

5. PACING & LAYER STYLE (The "Retention" Style):
This is a fast-paced retention video. You should place approximately 1 to 2 semantic SFX per sentence. The SFX should feel aggressive and perfectly synced to the exact frame a word is spoken or a graphic appears. Never place two SFX within 0.25 seconds of each other (the hook_riser bed is exempt).

OUTPUT FORMAT:
Respond STRICTLY with valid JSON matching the following schema. Do not include markdown or explanations outside the JSON.

{{
  "sfx_timeline": [
    {{
      "timestamp": 0.00,
      "end_timestamp": 2.15,
      "sfx_type": "hook_riser",
      "trigger_source": "audio",
      "trigger_detail": "Start of video tension build-up.",
      "reasoning": "Mandatory hook riser leading into the first major semantic impact."
    }},
    {{
      "timestamp": 2.15,
      "sfx_type": "impact",
      "trigger_source": "audio",
      "trigger_detail": "The word 'banned' was spoken.",
      "reasoning": "Climax of the hook_riser."
    }},
    {{
      "timestamp": 4.50,
      "sfx_type": "reaction: notification",
      "trigger_source": "visual",
      "trigger_detail": "Red arrow appeared pointing at a student's hair.",
      "reasoning": "Visual emphasis requires a pop/notification sound to draw the eye."
    }}
  ]
}}"""


def _tag_key(value):
    return re.sub(r"\s*:\s*", ":", str(value or "").strip().lower())


def is_visual_semantic_event(event):
    """A visible-content reaction, excluding generic cuts and the mandatory hook impact."""
    if str(event.get("trigger_source") or "").strip().lower() != "visual":
        return False
    kind = _tag_key(event.get("sfx_type"))
    if kind in ("hook_riser", "transition"):
        return False
    if kind == "impact" and float(event.get("timestamp") or 0.0) <= 6.0:
        return False
    return kind == "ui" or kind == "impact" or kind.startswith("reaction:")


def visual_semantic_target(duration, sfx_amount="medium"):
    spacing = {"low": 12.0, "medium": 8.0, "high": 6.0}.get(
        str(sfx_amount or "medium").strip().lower(), 8.0)
    return max(3, min(12, int(round(max(1.0, float(duration or 0.0)) / spacing))))


def build_visual_repair_prompt(tags, missing, existing_events):
    usable = [tag for tag in tags if _tag_key(tag) not in ("hook_riser", "transition")]
    prior = "\n".join(
        f"- {float(ev.get('timestamp') or 0):.2f}s: {ev.get('sfx_type')} / {ev.get('trigger_detail')}"
        for ev in existing_events if is_visual_semantic_event(ev)
    ) or "- none"
    tag_lines = "\n".join(f'- "{tag}"' for tag in usable)
    return f"""You are doing a VISUAL-ONLY correction pass on a finished short-form video.
The first Audio Director pass under-detected visible semantic moments. Add {int(missing)} missing
VISUAL reaction events. Watch the video frame-by-frame. IGNORE transcript keywords and spoken-word
meaning for this pass; `trigger_source` must always be `visual`.

Find clear visible triggers such as: a person falls or reacts; an object is used, exchanged, tapped,
opened, dropped or revealed; money/card/phone/food appears as the visual focus; an arrow/circle/sticker
appears; a strong hand gesture or facial reaction lands; a camera flash/photo occurs; a message/UI
appears; a cute, awkward, suspicious, celebratory, sad or surprising visible payoff occurs.

Do NOT return generic cuts, scene changes, ordinary walking, static shots, captions, hook_riser or
transition. Prefer precise action-peak timestamps. Do not duplicate these existing visual reactions:
{prior}

Use ONLY these exact tags:
{tag_lines}

Return strict JSON: {{"sfx_timeline":[{{"timestamp":12.34,"sfx_type":"reaction: camera_photo",
"trigger_source":"visual","trigger_detail":"A camera flash fires on screen.",
"reasoning":"Visible action peak."}}]}}"""


def merge_visual_repair_events(events, repair_events, tags):
    """Merge visual-only repairs, replacing a generic transition at the same visible beat."""
    merged = list(events or [])
    allowed = {_tag_key(tag) for tag in (tags or [])}
    added = 0
    for event in sorted(repair_events or [], key=lambda row: float(row.get("timestamp") or 0.0)):
        if not is_visual_semantic_event(event) or _tag_key(event.get("sfx_type")) not in allowed:
            continue
        timestamp = float(event.get("timestamp") or 0.0)
        if any(abs(timestamp - float(old.get("timestamp") or 0.0)) < 0.28
               for old in merged if _tag_key(old.get("sfx_type")) != "transition"):
            continue
        # A reaction explains the visible beat better than a generic whoosh/pop at the same cut.
        merged = [old for old in merged
                  if not (_tag_key(old.get("sfx_type")) == "transition"
                          and abs(timestamp - float(old.get("timestamp") or 0.0)) < 0.30)]
        event = dict(event)
        event["reasoning"] = "Visual coverage repair: " + str(event.get("reasoning") or "visible trigger")
        merged.append(event)
        added += 1
    merged.sort(key=lambda row: float(row.get("timestamp") or 0.0))
    return merged, added


def parse_sfx_timeline(payload, duration):
    """Backend parser for the director's JSON schema. Normalizes/clamps every event to
    {timestamp, end_timestamp?, sfx_type, trigger_source, trigger_detail, reasoning}."""
    if isinstance(payload, dict):
        rows = payload.get("sfx_timeline")
    else:
        rows = payload
    if not isinstance(rows, list):
        return []
    events = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            t = float(row.get("timestamp"))
        except (TypeError, ValueError):
            continue
        if t < 0 or (duration and t > duration - 0.05):
            continue
        ev = {
            "timestamp": round(t, 3),
            "sfx_type": str(row.get("sfx_type") or "").strip().lower(),
            "trigger_source": str(row.get("trigger_source") or "").strip().lower(),
            "trigger_detail": str(row.get("trigger_detail") or "").strip(),
            "reasoning": str(row.get("reasoning") or "").strip(),
        }
        end = row.get("end_timestamp")
        if end is not None:
            try:
                end = float(end)
                if end > t:
                    ev["end_timestamp"] = round(min(end, duration or end), 3)
            except (TypeError, ValueError):
                pass
        if ev["sfx_type"]:
            events.append(ev)
    events.sort(key=lambda e: e["timestamp"])
    return events


def prepare_audio_director_proxy(video_path, status_cb=None):
    """Create a compact 480px-wide MP4 while preserving the source audio.

    The director needs visual timing and the audible mix, not the original render bitrate.
    Uploading the full 100-170 MB render made transient connection resets much more likely.
    Returns ``(proxy_path, temp_dir)``; the caller owns cleanup.
    """
    ffmpeg = pipeline.find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found; cannot prepare the Audio Director upload.")
    temp_dir = Path(tempfile.mkdtemp(prefix="sfx_director_"))
    proxy = temp_dir / "audio_director_480p.mp4"
    command = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(video_path),
        "-map", "0:v:0", "-map", "0:a:0?",
        "-vf", "scale=480:-2",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "30",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "96k", "-ac", "2", "-ar", "48000",
        "-movflags", "+faststart", str(proxy),
    ]
    log(status_cb, "Preparing a compact 480p video + audio proxy for the Audio Director...")
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=900)
        if result.returncode or not proxy.exists() or proxy.stat().st_size <= 0:
            detail = (result.stderr or "ffmpeg produced no output").strip()[-500:]
            raise RuntimeError(f"Could not create Audio Director proxy: {detail}")
        source_mb = Path(video_path).stat().st_size / (1024 * 1024)
        proxy_mb = proxy.stat().st_size / (1024 * 1024)
        log(status_cb, f"Audio Director proxy ready: {source_mb:.1f} MB -> {proxy_mb:.1f} MB.")
        return proxy, temp_dir
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise


def upload_audio_director_media(path, key, status_cb=None, attempts=3):
    """Upload a director proxy with bounded retries for transient connection failures."""
    attempts = max(1, int(attempts or 1))
    last_error = None
    used_attempts = 0
    for attempt in range(1, attempts + 1):
        used_attempts = attempt
        try:
            log(status_cb, f"Uploading video for the multimodal Audio Director "
                           f"(attempt {attempt}/{attempts})...")
            return pipeline.upload_media(Path(path), key)
        except Exception as exc:  # noqa: BLE001 - urllib exposes several reset/timeout types
            last_error = exc
            message = str(exc).lower()
            permanent = any(marker in message for marker in (
                "balance is empty", "http 400", "http 401", "http 402", "http 403",
                "http 404", "http 413", "http 415", "http 422",
            ))
            if attempt >= attempts or permanent:
                break
            delay = 2 ** attempt  # 2s, then 4s
            log(status_cb, f"Audio Director upload connection failed ({exc.__class__.__name__}); "
                           f"retrying in {delay}s.")
            time.sleep(delay)
    raise RuntimeError(
        f"Audio Director video upload failed after {used_attempts} attempt(s): {last_error}"
    ) from last_error


def plan_sfx_with_vision(video_path, duration, cuts, phrases, reasoning_model=None,
                         status_cb=None, transitions_present=False, tags=None,
                         sfx_amount="medium", impact_word=None):
    """Upload the WHOLE video (visuals + audio) and ask the Gemini director for the
    sfx_timeline. Returns normalized events or None on any failure (callers fall back)."""
    key = os.environ.get("WAVESPEED_API_KEY", "")
    if not key or not tags:
        return None                       # config issue (no key / empty library) - not a call failure
    model = _vision_model(reasoning_model)
    # A real failure of the video/audio-URL call must ABORT the run (user rule: no silent fallback
    # to the deterministic engine). Only a missing key / empty library returns None above.
    proxy_path, proxy_dir = prepare_audio_director_proxy(Path(video_path), status_cb=status_cb)
    try:
        video_url, _ = upload_audio_director_media(
            proxy_path, key, status_cb=status_cb, attempts=3)
    finally:
        shutil.rmtree(proxy_dir, ignore_errors=True)
    hints = [f"Video duration: {round(duration, 2)} seconds."]
    if cuts:
        hints.append("Detected hard-cut timestamps (sync hints): "
                     + ", ".join(f"{c:.2f}" for c in cuts[:80]))
    if phrases:
        lines = [f'{p.get("start", 0):.2f}-{p.get("end", 0):.2f}: {p.get("text", "")}'
                 for p in phrases[:120]]
        hints.append("Timed transcript (sync hints):\n" + "\n".join(lines))
    if impact_word:
        hints.append(
            f'IMPACT WORD (user-marked): "{impact_word}" is the single most important beat and occurs '
            f'in the first ~0-5 seconds. Place the hook-riser CLIMAX and a strong impact hit EXACTLY on '
            f"that word's timestamp (read it off the timed transcript above). Do NOT guess a different "
            f"word for the main impact.")
    hints.append("Analyze the attached video (visuals AND audio) and return the sfx_timeline JSON.")
    payload = {
        "model": model,
        "messages": [
            {"role": "system",
             "content": build_sfx_director_prompt(tags, transitions_present=transitions_present)},
            {"role": "user", "content": [
                {"type": "video_url", "video_url": {"url": video_url}},
                {"type": "text", "text": "\n\n".join(hints)},
            ]},
        ],
        "temperature": 0.3,
        "max_tokens": 4000,
        "response_format": {"type": "json_object"},
    }
    log(status_cb, f"Audio Director ({model}) watching the video...")
    data = agent_core.post_json_url(agent_core.WAVESPEED_LLM_API, payload, timeout=600)
    parsed = agent_core.extract_json_object(data["choices"][0]["message"]["content"])
    events = parse_sfx_timeline(parsed, duration)
    if not events:
        raise RuntimeError("Audio Director returned no usable sfx_timeline events.")
    target = visual_semantic_target(duration, sfx_amount=sfx_amount)
    visual_count = sum(1 for event in events if is_visual_semantic_event(event))
    log(status_cb, f"Audio Director returned {len(events)} timed event(s); "
                   f"visual semantic coverage {visual_count}/{target}.")
    if visual_count < target:
        missing = target - visual_count
        log(status_cb, f"Audio Director visual coverage is low - scanning the video again for "
                       f"{missing} visible action/overlay/reaction trigger(s)...")
        repair_payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": build_visual_repair_prompt(tags, missing, events)},
                {"role": "user", "content": [
                    {"type": "video_url", "video_url": {"url": video_url}},
                    {"type": "text", "text": (
                        f"Video duration: {duration:.2f}s. Return up to {missing} precise "
                        "visual-only semantic reaction events now."
                    )},
                ]},
            ],
            "temperature": 0.2,
            "max_tokens": 2500,
            "response_format": {"type": "json_object"},
        }
        try:
            repair_data = agent_core.post_json_url(
                agent_core.WAVESPEED_LLM_API, repair_payload, timeout=600)
            repair_parsed = agent_core.extract_json_object(
                repair_data["choices"][0]["message"]["content"])
            repair_events = parse_sfx_timeline(repair_parsed, duration)
            events, added = merge_visual_repair_events(events, repair_events, tags)
            log(status_cb, f"Audio Director visual repair added {added} event(s) and replaced "
                           "overlapping generic transitions where appropriate.")
        except Exception as exc:  # keep the valid first plan, but make degraded coverage explicit
            log(status_cb, f"Audio Director visual repair failed ({exc.__class__.__name__}: {exc}); "
                           "using the first-pass events.")
    return events


def resolve_vision_segments(events, data, duration, existing_onsets=None, sfx_amount="medium",
                            transitions_present=False, ffprobe=None, status_cb=None):
    """Map the director's tags onto the human-labeled sound pools -> concrete mix segments.
    Enforces: exactly one hook riser (start 0, duration-matched so it ends at end_timestamp),
    a guaranteed impact at the riser climax, spacing/density caps, and the transition-redundancy
    guard against SFX already present in the source audio."""
    events = events or []                 # director may return None (no key / empty library) - never crash
    import sfx_library
    lib = data.get("library", {})
    dedup = lambda xs: list(dict.fromkeys(xs))
    transition_pool = dedup((lib.get("bright_whoosh") or []) + (lib.get("swipe_whoosh") or [])
                            + (lib.get("whoosh_hit_combo") or []))
    impact_pool = lib.get("impact_hit") or (data.get("reactions", {}).get("shock_reveal") or [])
    ui_pool = lib.get("ui_click") or lib.get("caption_pop") or []
    # the OPENING sound must be a dedicated hook_riser file - never a generic body riser
    hook_pool = data.get("hook_risers") or []
    reactions = data.get("reactions") or {}
    existing_onsets = [float(o) for o in (existing_onsets or [])]
    profile = sfx_amount_profile(sfx_amount)
    max_events = max(6, int(profile["max_per_minute"] * max(duration, 1.0) / 60.0))

    _dur_cache = {}

    def fdur(path):
        p = str(path)
        if p not in _dur_cache:
            _dur_cache[p] = media_duration(p, ffprobe) or 0.8
        return _dur_cache[p]

    _MAXDUR = {"impact": 0.7, "ui": 0.3, "transition": 0.5}
    _DB = {"impact": sfx_library.CAT_DB.get("impact_hit", -5),
           "ui": sfx_library.CAT_DB.get("ui_click", -15),
           "transition": sfx_library.CAT_DB.get("bright_whoosh", -8)}

    segments = []
    rot = {}
    placed_times = []
    hook_done = False

    def pick(pool, kind):
        if not pool:
            return None
        k = rot.get(kind, 0)
        rot[kind] = k + 1
        return pool[k % len(pool)]

    def spaced(t):
        # ±0.1s "no additional sound" window (was ±0.22/0.3): reactions can land right on the
        # spoken word even when it sits close to a cut, without doubling exactly on top of it.
        return all(abs(t - pt) >= 0.10 for pt in placed_times)

    # -- hook riser first (exactly one; extra risers dropped) --
    riser_events = [e for e in events if e["sfx_type"] == "hook_riser"]
    rest = [e for e in events if e["sfx_type"] != "hook_riser"]
    # The opening riser MUST be one of the dedicated hook_riser*.MP3 files. Catalog entries
    # are label-driven and a mislabeled sound once played under the NAME hook_riser while
    # being a completely different sound (user caught it on the timeline). Trust the scanned
    # sound library (filename-matched pool) FIRST; the passed catalog is only a fallback.
    try:
        _full = sfx_library.build_library()
        _lib_hooks = list(_full.get("hook_risers") or [])
    except Exception:
        _lib_hooks = []
    if _lib_hooks:
        hook_pool = _lib_hooks
    if not hook_pool:
        log(status_cb, "No dedicated hook_riser files found - the opening riser is skipped.")
    if hook_pool and not riser_events:
        # The director is REQUIRED to open with a hook_riser at 0.00 but LLMs sometimes omit
        # it - then the video used to open with whatever reaction landed first (e.g. an "aww"
        # at 0.0s). Synthesize the riser instead of trusting the plan blindly.
        log(status_cb, "Hook riser missing from the director plan - synthesizing the mandatory opening riser.")
        riser_events = [{"sfx_type": "hook_riser", "timestamp": 0.0, "end_timestamp": 0.0,
                         "trigger_source": "backend",
                         "trigger_detail": "synthesized (director omitted the mandatory opening riser)"}]
    if riser_events and hook_pool:
        ev = riser_events[0]
        end = float(ev.get("end_timestamp") or 0.0)
        # An explicit director climax is authoritative, whether the hook is short or long.
        # Reject only impossible/out-of-video values; do not silently replace a valid late beat.
        if not (0.35 <= end <= max(0.35, duration - 0.05)):
            # no usable climax time -> aim at the first non-riser event inside the hook window
            cand = [e["timestamp"] for e in rest if 0.6 <= e["timestamp"] <= 5.5]
            end = cand[0] if cand else min(2.5, max(1.2, duration * 0.15))
        item, rlen, playback_rate = sfx_library.choose_riser_for_target(
            hook_pool, end, duration_getter=fdur)
        if item:
            segments.append({"path": Path(item["path"]), "start": 0.0,
                         "duration": round(end, 3), "source_trim": 0.0,
                         "source_duration": round(rlen, 3),
                         "playback_rate": round(playback_rate, 6),
                         "volume": round(min(0.85, sfx_library.db_to_gain(-6)), 3),
                         "category": "hook_riser",
                         "reason": ev.get("trigger_detail") or "hook build-up"})
            hook_done = True
            log(status_cb, f"Hook riser: selected {Path(item['path']).name} ({rlen:.2f}s) "
                           f"for 0.00->{end:.2f}s at {playback_rate:.3f}x.")
        # guaranteed climax: an impact at the riser end unless the director already put one there
        if impact_pool and not any(abs(e["timestamp"] - end) <= 0.10 for e in rest
                                   if e["sfx_type"] in ("impact",) or e["sfx_type"].startswith("reaction")):
            rest.append({"timestamp": round(end, 3), "sfx_type": "impact",
                         "trigger_source": "backend", "trigger_detail": "riser climax (auto)",
                         "reasoning": "guaranteed impact at hook_riser end"})
            rest.sort(key=lambda e: e["timestamp"])

    if hook_done:
        # the riser owns the opening: no other sound may sit in the first 0.3s on top of it
        early = [e for e in rest if float(e.get("timestamp", 0) or 0) < 0.30]
        if early:
            rest = [e for e in rest if float(e.get("timestamp", 0) or 0) >= 0.30]
            log(status_cb, f"Dropped {len(early)} SFX event(s) inside 0.0-0.3s - the hook riser owns the opening.")

    # TWO-PASS ORDER (user rule): place ALL structural/cut SFX first (transitions, impacts, ui),
    # THEN the reaction SFX. This way cut sounds always claim their slot on the boundary and the
    # reaction sounds fill the remaining gaps ON THE SPOKEN WORD instead of being snapped to the
    # nearest cut. Each group stays in chronological order.
    _structural = sorted((e for e in rest if e["sfx_type"] in ("transition", "impact", "ui")),
                         key=lambda e: e["timestamp"])
    _reactions = sorted((e for e in rest if str(e["sfx_type"]).startswith("reaction")),
                        key=lambda e: e["timestamp"])
    for ev in (_structural + _reactions):
        if len(segments) >= max_events:
            break
        t = ev["timestamp"]
        kind = ev["sfx_type"]
        if kind == "hook_riser":
            continue                                   # only one riser, already handled
        if kind.startswith("reaction"):
            slug = kind.split(":", 1)[-1].strip().replace(" ", "_")
            pool = reactions.get(slug)
            if not pool:
                continue
            # WORD-ACCURATE: snap the reaction onto the END of its trigger word (the director's
            # vision timestamp put the death gong on the nearest cut, seconds before the word).
            _trigs = tuple(tr.split()[0] for tr in
                           (agent_core.REACTION_TRIGGERS.get(slug) or ()))
            for _w in LAST_ASR_WORDS or []:
                try:
                    _ws, _we = float(_w.get("start") or 0.0), float(_w.get("end") or 0.0)
                except (TypeError, ValueError):
                    continue
                _tok = str(_w.get("word") or "").lower().strip(" .,!?…\"'")
                if abs(_ws - t) <= 2.5 and any(_tok.startswith(tr) for tr in _trigs if tr):
                    t = round(_we + 0.03, 3)
                    break
            if not spaced(t):
                continue
            path = pick(pool, f"r:{slug}")
            db = agent_core.REACTION_DB.get(slug, agent_core._REACTION_DB_DEFAULT)
            if str(ev.get("trigger_source") or "").lower() == "visual":
                db = max(db, -8)  # visual hits must remain as audible as the working money trigger
            dur = min(fdur(path), 1.1)
            cat = f"reaction_{slug}"
        elif kind in ("impact", "ui", "transition"):
            if kind == "transition":
                if transitions_present:
                    continue                           # render already has cut sounds
                if any(abs(t - o) <= 0.14 for o in existing_onsets):
                    continue                           # source audio already has one here
            pool = {"impact": impact_pool, "ui": ui_pool, "transition": transition_pool}[kind]
            if not pool or not spaced(t):
                continue
            path = pick(pool, kind)
            db = _DB[kind]
            dur = min(fdur(path), _MAXDUR[kind])
            cat = "impact_hit" if kind == "impact" else ("ui_click" if kind == "ui" else "transition")
        else:
            continue                                   # unknown tag -> drop
        segments.append({"path": Path(path), "start": round(t, 3),
                         "duration": round(max(0.1, dur), 3),
                         "volume": round(min(0.85, sfx_library.db_to_gain(db)), 3),
                         "category": cat,
                         "reason": ev.get("trigger_detail") or ev.get("reasoning") or kind})
        placed_times.append(t)

    segments.sort(key=lambda s: s["start"])
    log(status_cb, f"Audio Director resolved {len(segments)} event(s) onto the labeled library"
                   + (" (incl. hook riser)" if hook_done else "") + ".")
    return segments


def apply_semantic_sfx_to_render(video_path, reasoning_model=None, status_cb=None,
                                 sfx_amount="medium", phrases=None, cuts=None, impact_word=None):
    """Script-to-Visuals post-render pass: the director watches the FINISHED render (which
    already has transition whooshes on every cut) and adds semantic/reaction SFX + the hook
    riser. ``impact_word`` (optional) is the user-marked word to anchor the hook impact on.
    Mixes in place (same file). Returns the number of added SFX (0 = unchanged)."""
    video_path = Path(video_path)
    ffmpeg = pipeline.find_ffmpeg()
    ffprobe = pipeline.find_ffprobe(ffmpeg)
    if not ffmpeg or not video_path.exists():
        return 0
    duration = media_duration(video_path, ffprobe)
    data = labeled_sfx_data(status_cb=None)
    tags = vision_tag_catalog(data, transitions_present=True)
    events = plan_sfx_with_vision(video_path, duration, cuts or [], phrases or [],
                                  reasoning_model=reasoning_model, status_cb=status_cb,
                                  transitions_present=True, tags=tags, impact_word=impact_word,
                                  sfx_amount=sfx_amount)
    if not events:
        return 0
    segments = resolve_vision_segments(events, data, duration, existing_onsets=None,
                                       sfx_amount=sfx_amount, transitions_present=True,
                                       ffprobe=ffprobe, status_cb=status_cb)
    if not segments:
        return 0
    tmp = video_path.with_name(video_path.stem + "_semantic_tmp.mp4")
    mix_into_video(video_path, segments, tmp, ffmpeg, ffprobe, duration, status_cb=status_cb)
    os.replace(tmp, video_path)
    # sidecar report so the placement is auditable
    try:
        side = video_path.with_name(video_path.stem + "_semantic_sfx.json")
        side.write_text(json.dumps({"events": [
            {"time": s["start"], "category": s["category"], "file": s["path"].name,
             "reason": s["reason"]} for s in segments]}, indent=2), encoding="utf-8")
    except Exception:
        pass
    return len(segments)


def plan_sfx_with_llm(duration, cuts, phrases, catalog, reasoning_model=None, status_cb=None,
                      sfx_amount="medium"):
    if not os.environ.get("WAVESPEED_API_KEY") or not catalog:
        return None
    profile = sfx_amount_profile(sfx_amount)
    category_lines = "\n".join(f"- {name}: {meta['desc']}" for name, meta in catalog.items())
    prompt = (
        "You are a meticulous sound designer for short vertical videos.\n"
        "You are given a finished video's duration, its hard visual cut timestamps, and the timed "
        "speech transcript. Place sound effects that make the video feel professionally "
        "edited WITHOUT being cheesy.\n\n"
        "Use ONLY these categories:\n" + category_lines + "\n\n"
        "Guidelines:\n"
        "- Put a 'transition' or 'whoosh' on most real image/scene changes (use the cut timestamps).\n"
        "- Use 'impact', 'boom', or 'stinger' to emphasise a specific powerful word, number, reveal, "
        "or punchline - align the time to when that word is spoken in the transcript.\n"
        "- Use 'riser' before a big payoff; 'foley' only when it matches an on-screen action.\n"
        f"- DENSITY ({str(sfx_amount).upper()}): {profile['plan']}\n"
        "- intensity is one of: subtle, medium, strong. Most should be subtle/medium.\n"
        "- time is in seconds (float), must be within the video duration.\n\n"
        "Return STRICT JSON only: {\"events\":[{\"time\":number,\"category\":string,"
        "\"intensity\":\"subtle|medium|strong\",\"reason\":string}]}\n\n"
        f"Video duration seconds: {round(duration, 2)}\n"
        f"Hard cut timestamps: {json.dumps(cuts)}\n"
        f"Transcript phrases: {json.dumps(phrases, ensure_ascii=False)}"
    )
    payload = {
        "model": reasoning_model or "anthropic/claude-opus-4.8",
        "messages": [
            {"role": "system", "content": "You are an expert short-form video sound designer. Return compact valid JSON only."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.4,
        "max_tokens": 2000,
        "response_format": {"type": "json_object"},
    }
    try:
        log(status_cb, "Opus 4.8 planning sound effects...")
        data = agent_core.post_json_url(agent_core.WAVESPEED_LLM_API, payload, timeout=180)
        plan = agent_core.extract_json_object(data["choices"][0]["message"]["content"])
        if isinstance(plan, dict) and isinstance(plan.get("events"), list):
            return plan["events"]
    except Exception as exc:
        log(status_cb, f"Opus planning failed ({exc}); using automatic cut-based effects.")
    return None


def fallback_plan(cuts, phrases, duration):
    """Deterministic plan: whoosh on each cut, an impact on emphatic phrases."""
    events = [{"time": t, "category": "transition", "intensity": "subtle",
               "reason": "scene change"} for t in cuts]
    emphasis = re.compile(r"[A-Z]{3,}|\b\d{2,}\b|never|first|last|died|killed|secret|but|until|finally", re.I)
    for phrase in phrases:
        if emphasis.search(phrase.get("text", "")):
            events.append({"time": float(phrase.get("start", 0.0)) + 0.05,
                           "category": "impact", "intensity": "medium",
                           "reason": "emphasis"})
    return events


def generate_agent_sfx(config, category, meta, event, status_cb=None):
    """Generate a fitting SFX via Kling when the library has no match for this event."""
    if not bool(config.get("sfx_generation_enabled", False)):
        return None
    key = config.get("wavespeed_api_key") or os.environ.get("WAVESPEED_API_KEY", "")
    if not key:
        return None
    gen_dir = Path(config.get("_gen_sfx_dir") or (ROOT / "soundeffects" / "generated"))
    gen_dir.mkdir(parents=True, exist_ok=True)
    reason = str(event.get("reason") or "").strip()
    prompt = f"{meta['desc']}. {reason}".strip().rstrip(".") + ", clean isolated sound effect, no music, no speech"
    digest = hashlib.sha1(prompt.lower().encode("utf-8", "ignore")).hexdigest()[:10]
    out = gen_dir / f"{category}_{digest}.wav"
    if out.exists():
        return out
    dur = int(max(1, min(10, round(float(meta.get("max_dur", 2)) + 0.5))))
    try:
        return pipeline.generate_sfx_clip(prompt, dur, out, key=key,
                                          cancel_event=config.get("_cancel_event"), status_cb=status_cb)
    except Exception as exc:
        log(status_cb, f"SFX generation failed ({category}): {exc}")
        return None


def resolve_segments(config, events, duration, ffprobe, status_cb=None):
    """Turn agent events into concrete mix segments with files, timing, volume."""
    segments = []
    last_at = {}
    for event in sorted(events, key=lambda e: float(e.get("time", 0.0))):
        try:
            at = float(event.get("time", 0.0))
        except (TypeError, ValueError):
            continue
        category = str(event.get("category", "")).strip().lower()
        meta = AGENT_SFX_CATEGORIES.get(category)
        if not meta or at < 0.05 or (duration and at > duration - 0.05):
            continue
        if any(abs(at - prev) < 0.40 for prev in last_at.get(category, [])):
            continue
        seed = f"{category}|{round(at, 2)}|{event.get('reason', '')}"
        path = pipeline.choose_sfx(config, meta["lib"], seed)
        if not path:
            path = generate_agent_sfx(config, category, meta, event, status_cb=status_cb)
        if not path:
            continue
        sfx_dur = media_duration(path, ffprobe) or meta["max_dur"] * 0.7
        sfx_dur = max(0.12, min(sfx_dur, meta["max_dur"]))
        gain = INTENSITY_GAIN.get(str(event.get("intensity", "medium")).lower(), 1.0)
        volume = round(min(0.6, meta["vol"] * gain), 3)
        segments.append({
            "path": Path(path), "start": round(at, 3), "duration": round(sfx_dur, 3),
            "volume": volume, "category": category, "reason": event.get("reason", ""),
        })
        last_at.setdefault(category, []).append(at)
    return segments


def mix_into_video(video_path, segments, out_path, ffmpeg, ffprobe, duration, status_cb=None):
    """Mix SFX under the original audio, copying the video stream untouched."""
    has_audio = pipeline.media_has_audio(video_path, ffprobe)
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "warning", "-i", str(video_path)]
    for segment in segments:
        cmd += ["-i", str(segment["path"])]

    filters = []
    labels = []
    for index, segment in enumerate(segments):
        input_index = index + 1
        label = f"s{index}"
        delay_ms = int(round(segment["start"] * 1000))
        end = float(segment["duration"])
        fade_out = max(0.0, end - 0.06)
        # source_trim: skip into the FILE (risers play their tail so the swell peaks on the beat)
        src = max(0.0, float(segment.get("source_trim") or 0.0))
        playback_rate = max(0.01, float(segment.get("playback_rate") or 1.0))
        source_duration = float(segment.get("source_duration") or (end * playback_rate))
        tempo = pipeline.atempo_filter_chain(playback_rate)
        tempo_part = f",{tempo}" if tempo else ""
        filters.append(
            f"[{input_index}:a]atrim={src:.3f}:{src + source_duration:.3f},"
            f"asetpts=PTS-STARTPTS{tempo_part},atrim=0:{end:.3f},asetpts=PTS-STARTPTS,"
            f"aformat=sample_rates=44100:channel_layouts=stereo,"
            f"afade=t=out:st={fade_out:.3f}:d=0.060,volume={segment['volume']:.3f},"
            f"adelay={delay_ms}:all=1[{label}]"
        )
        labels.append(label)

    if has_audio:
        filters.append("[0:a]aformat=sample_rates=44100:channel_layouts=stereo,volume=1.0[orig]")
        mix_inputs = ["orig"] + labels
    else:
        mix_inputs = labels

    if not mix_inputs:
        # Nothing to add; just copy through.
        _run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(video_path),
              "-c", "copy", "-movflags", "+faststart", str(out_path)], timeout=300)
        return out_path

    if len(mix_inputs) == 1:
        filters.append(f"[{mix_inputs[0]}]alimiter=limit=0.95,atrim=0:{duration:.3f}[aout]")
    else:
        filters.append(
            "".join(f"[{name}]" for name in mix_inputs)
            + f"amix=inputs={len(mix_inputs)}:duration=longest:dropout_transition=0:normalize=0,"
            + f"alimiter=limit=0.95,atrim=0:{duration:.3f}[aout]"
        )

    cmd += [
        "-filter_complex", ";".join(filters),
        "-map", "0:v:0", "-map", "[aout]",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart", str(out_path),
    ]
    log(status_cb, f"Mixing {len(segments)} sound effect(s) under the original audio...")
    result = _run(cmd, timeout=600)
    if not out_path.exists():
        raise RuntimeError(f"ffmpeg SFX mix failed: {(result.stderr or '')[-500:]}")
    return out_path


def refine_segments_with_llm(segments, phrases, reasoning_model=None, status_cb=None,
                             sfx_amount="medium"):
    """FILE-LEVEL intelligence pass: show the reasoning model每 placed event (time, current
    file, why) plus the actual library files (name + seconds + category) and let it veto or
    swap picks that do not FIT - e.g. a church bell as a transition or a melodic hit on a
    calm sentence. Keeps the plan unchanged when the model is unavailable."""
    if not segments or not os.environ.get("WAVESPEED_API_KEY"):
        return segments
    try:
        import sfx_library
        data = sfx_library.build_library(status_cb=None)
    except Exception:
        return segments
    usable = [r for r in data.get("records", []) if r.get("usable")]
    if not usable:
        return segments
    by_name = {r["file"]: r for r in usable}
    catalog = [{"file": r["file"], "seconds": round(float(r.get("trim_len") or r.get("duration") or 1.0), 2),
                "category": r.get("estimated_category", "")} for r in usable][:90]
    events = [{"i": i, "t": round(float(s["start"]), 2), "file": Path(str(s["path"])).name,
               "category": s.get("category", ""), "reason": str(s.get("reason", ""))[:60]}
              for i, s in enumerate(segments)]
    transcript = " | ".join(f"{round(float(p.get('start', 0)), 1)}s {p.get('text', '')}"
                            for p in (phrases or []))[:2200]
    prompt = (
        "You are reviewing the sound-effect plan for a finished short vertical video.\n"
        "For every event decide: keep, drop, or replace its FILE with a better-fitting one from "
        "the library list. Judge by the file NAME + length and the spoken context at that time.\n"
        "Hard rules:\n"
        "- Cut/transition slots need SHORT NEUTRAL sounds (whoosh/click/pop/ding <=1.2s). "
        "NEVER bells, gongs, church/choir, musical jingles or melodic hits on a plain cut.\n"
        "- Dramatic hits only where the transcript actually has a reveal/shock/punchline.\n"
        "- Never use scary/horror/scream files. Prefer variety over repeating one file.\n"
        f"- AMOUNT POLICY ({str(sfx_amount).upper()}): {sfx_amount_profile(sfx_amount)['review']}\n\n"
        "Return STRICT JSON only: {\"events\":[{\"i\":number,\"action\":\"keep|drop|replace\","
        "\"file\":\"name-when-replacing\"}]}\n\n"
        f"Planned events: {json.dumps(events, ensure_ascii=False)}\n"
        f"Library files: {json.dumps(catalog, ensure_ascii=False)}\n"
        f"Timed transcript: {transcript}"
    )
    payload = {
        "model": reasoning_model or "anthropic/claude-opus-4.8",
        "messages": [
            {"role": "system", "content": "You are an expert short-form sound designer. Return compact valid JSON only."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2, "max_tokens": 3000, "response_format": {"type": "json_object"},
    }
    try:
        log(status_cb, "Reviewing every SFX pick with the reasoning model (file-level fit)...")
        data_resp = agent_core.post_json_url(agent_core.WAVESPEED_LLM_API, payload, timeout=180)
        plan = agent_core.extract_json_object(data_resp["choices"][0]["message"]["content"])
        rows = plan.get("events") if isinstance(plan, dict) else None
        if not isinstance(rows, list):
            return segments
    except Exception as exc:
        log(status_cb, f"SFX review pass unavailable ({exc}); keeping the heuristic plan.")
        return segments
    out, dropped, swapped = [], 0, 0
    actions = {int(r.get("i", -1)): r for r in rows if isinstance(r, dict)}
    for i, seg in enumerate(segments):
        act = actions.get(i) or {}
        action = str(act.get("action", "keep")).lower()
        if action == "drop":
            dropped += 1
            continue
        if action == "replace":
            rec = by_name.get(str(act.get("file", "")).strip())
            if rec and rec.get("use_path") and Path(rec["use_path"]).exists():
                seg = dict(seg)
                seg["path"] = Path(rec["use_path"])
                seg["duration"] = round(min(float(seg["duration"]),
                                            float(rec.get("trim_len") or seg["duration"])), 3)
                seg["reason"] = (str(seg.get("reason", "")) + " (model swap)").strip()
                swapped += 1
        out.append(seg)
    log(status_cb, f"SFX review: kept {len(out)}, swapped {swapped}, dropped {dropped}.")
    return out or segments


def _write_timeline_project(video_path, segments, scenes, duration, ffmpeg, enhanced_out,
                            status_cb=None):
    """Create a REAL project for the enhanced upload so the timeline editor can open it:
    the video is split at the detected cuts into per-scene clips, the original audio becomes
    the voice track, and every placed sound lands in config['custom_sfx'] - fully editable
    (move / replace / delete / volume) and re-renderable through the normal pipeline."""
    base = re.sub(r"[^a-z0-9_]+", "_", video_path.stem.lower()).strip("_")[:36] or "upload"
    slug = f"sfxmaster_{base}_{time.strftime('%H%M%S')}"
    pdir = agent_core.PROJECTS_DIR / slug
    clip_dir = pdir / "seedance 2.0"
    for d in (clip_dir, pdir / "input", pdir / "config", pdir / "renders"):
        d.mkdir(parents=True, exist_ok=True)
    # original audio -> the voice track the renderer lays back over the (muted) segments
    _run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(video_path), "-vn",
          "-ar", "48000", "-ac", "2", str(pdir / "input" / "voiceover.wav")], timeout=300)
    log(status_cb, f"Timeline project: splitting the video into {len(scenes)} segment clip(s)...")
    cfg_scenes = []
    for k, sc in enumerate(scenes):
        start, end = float(sc.get("start", 0.0)), float(sc.get("end", 0.0))
        seg_dur = max(0.15, end - start)
        name = f"seg_{k:02d}.mp4"
        _run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{start:.3f}",
              "-i", str(video_path), "-t", f"{seg_dur:.3f}", "-an",
              "-c:v", "libx264", "-crf", "19", "-preset", "veryfast",
              str(clip_dir / name)], timeout=600)
        cfg_scenes.append({
            "id": f"{k + 1:02d}", "name": f"Segment {k + 1:02d}",
            "script": sc.get("script", ""), "exact_voice_text": sc.get("exact_voice_text", ""),
            "start": round(start, 3), "end": round(end, 3),
            "clip": name, "asset": name, "seedance": True,
            "seedance_start_trim": 0.0, "render_caption": False,
        })
    custom = []
    for i, seg in enumerate(segments):
        at = float(seg["start"])
        scene = next((s for s in cfg_scenes if s["start"] <= at < s["end"]), cfg_scenes[-1])
        custom.append({
            "id": f"sfxm-{i:03d}", "scene_id": scene["id"],
            "path": str(seg["path"]), "offset": round(max(0.0, at - scene["start"]), 3),
            "duration": round(float(seg["duration"]), 3), "volume": float(seg["volume"]),
            "source_duration": round(float(seg.get("source_duration") or 0.0), 3),
            "playback_rate": round(float(seg.get("playback_rate") or 1.0), 6),
            "enabled": True, "label": Path(str(seg["path"])).stem.replace("_", " "),
        })
    config = {
        "project_slug": slug, "title": f"SFX Master - {video_path.stem}"[:70],
        "duration": round(float(duration), 3),
        "scenes": cfg_scenes, "custom_sfx": custom,
        "sfx_enabled": False,               # re-renders mix ONLY the editable custom_sfx
        "render_captions": False, "animated_captions": False,
        "use_seedance_clips": True, "seedance_clip_start_trim": 0.0,
        "background_music_choice": "none", "audio_master_gain": 1.0,
        "sfx_master_source": str(video_path),
    }
    (pdir / "config" / "project.json").write_text(
        json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")
    try:
        shutil.copy2(enhanced_out, pdir / "renders" / Path(enhanced_out).name)
    except Exception:
        pass
    log(status_cb, f"Timeline project ready: {slug} (open it in the timeline editor to "
                   "move/replace/delete the added sounds).")
    return slug


def _scenes_from_cuts_and_phrases(cuts, phrases, duration):
    """Build the `scenes` list that `agent_core.place_editor_sfx` expects from an uploaded video's
    detected hard cuts + timed transcript. Each cut is a real edit point (a scene boundary); the
    transcript text overlapping each segment drives the same big-moment / topic-accent detection the
    normal agent run uses. Scene 0 starts at 0.0 so it gets the hook-opening impact."""
    bounds = sorted({0.0} | {round(float(c), 3) for c in (cuts or [])
                             if 0.2 < float(c) < max(0.3, duration - 0.05)})
    scenes = []
    for k, start in enumerate(bounds):
        end = bounds[k + 1] if k + 1 < len(bounds) else duration
        # Assign a phrase to the ONE scene where it STARTS (not every scene it overlaps), so a
        # dramatic line marks a single cut as a big moment and the cuts in between stay "normal" -
        # letting the variety rotation (whoosh/pop/ding/flash) cover them instead of everything
        # becoming an impact. This mirrors the normal run's per-cut word assignment.
        text = " ".join(str(p.get("text", "")) for p in (phrases or [])
                        if start <= float(p.get("start", 0.0)) < end).strip()
        scenes.append({"start": round(start, 3), "end": round(end, 3),
                       "exact_voice_text": text, "script": text, "fx": {}})
    return scenes


def enhance_video_with_sfx(video_path, reasoning_model=None, status_cb=None, out_dir=None,
                           generate_missing=True, sfx_amount="medium"):
    video_path = Path(video_path)
    if not video_path.exists():
        raise RuntimeError("Uploaded video not found.")
    if video_path.suffix.lower() not in VIDEO_EXTS:
        raise RuntimeError(f"Unsupported video type: {video_path.suffix}")

    ffmpeg = pipeline.find_ffmpeg()
    ffprobe = pipeline.find_ffprobe(ffmpeg)
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found; cannot process video.")

    out_dir = Path(out_dir) if out_dir else SFX_OUTPUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    out_path = out_dir / f"{video_path.stem}_sfx_{stamp}.mp4"

    config = {
        "project_slug": "sfx_enhance",
        "output_basename": video_path.stem,
        "sfx_library_dir": str(ROOT / "soundeffects"),
        "sfx_single_transition_sound_per_video": False,
        "sfx_generation_enabled": bool(generate_missing) and bool(os.environ.get("WAVESPEED_API_KEY", "")),
        "wavespeed_api_key": os.environ.get("WAVESPEED_API_KEY", ""),
        "_gen_sfx_dir": str(out_dir / "generated_sfx"),
    }

    duration = media_duration(video_path, ffprobe)
    log(status_cb, f"Loaded video: {duration:.1f}s.")

    log(status_cb, "Detecting scene changes...")
    cuts = detect_scene_cuts(video_path, ffmpeg)
    log(status_cb, f"Found {len(cuts)} visual cut(s).")

    phrases = transcribe_with_timing(video_path, ffmpeg, ffprobe, duration, status_cb=status_cb)
    if phrases:
        log(status_cb, f"Transcript: {len(phrases)} timed phrase(s).")

    # Detect SFX the video ALREADY has (transients in the existing audio). At any cut that already
    # has one, the SFX Master skips adding a NEW transition/cut sound - it only fills the gaps and
    # still adds non-transition SFX (impacts, topic accents) elsewhere.
    existing_onsets = detect_existing_sfx_onsets(video_path, ffmpeg, status_cb=status_cb)

    # PRIMARY: the multimodal Audio Director - the model watches AND hears the whole video,
    # then returns the sfx_timeline (hook_riser + impacts + semantic reactions + transitions
    # only where the audio has none). Falls back to the deterministic editor engine on failure.
    plan_source = "editor_pack"
    scenes = _scenes_from_cuts_and_phrases(cuts, phrases, duration)
    segments = []
    sfx_report = {}
    if os.environ.get("WAVESPEED_API_KEY"):
        # Vision Audio Director is REQUIRED here (a key is present). Any failure of the
        # video/audio-URL call ABORTS the run - NO silent fallback to the local engine (user rule).
        data_lib = labeled_sfx_data(status_cb=status_cb)
        tags = vision_tag_catalog(data_lib, transitions_present=False)
        vision_events = plan_sfx_with_vision(video_path, duration, cuts, phrases,
                                             reasoning_model=reasoning_model,
                                             status_cb=status_cb, transitions_present=False,
                                             tags=tags, sfx_amount=sfx_amount)   # raises on upload/first-pass failure
        segments = resolve_vision_segments(vision_events, data_lib, duration,
                                           existing_onsets=existing_onsets,
                                           sfx_amount=sfx_amount, transitions_present=False,
                                           ffprobe=ffprobe, status_cb=status_cb)
        if not segments:
            raise RuntimeError("Audio Director produced no placeable SFX for this video.")
        plan_source = "vision_director"
        sfx_report = {
            "vision_timeline": vision_events,
            "visual_semantic_count": sum(1 for event in vision_events
                                         if is_visual_semantic_event(event)),
            "visual_semantic_target": visual_semantic_target(duration, sfx_amount),
            "audio_semantic_count": sum(
                1 for event in vision_events
                if str(event.get("trigger_source") or "").lower() == "audio"
                and _tag_key(event.get("sfx_type")) not in ("hook_riser", "transition")
            ),
        }

    if not segments:
        # No API key -> the deterministic engine (same as a normal agent run) over cuts + transcript.
        profile = sfx_amount_profile(sfx_amount)
        sfx_config = {
            "sfx_enabled": True,
            "scenes": scenes,
            "duration": duration,
            "sfx_amount": str(sfx_amount or "medium"),
            "editor_sfx_max_per_minute": int(profile["max_per_minute"]),
            "editor_sfx_budget_cap": int(profile["budget_cap"]),
            "existing_sfx_onsets": existing_onsets,   # skip NEW cut sounds where one already exists
        }
        log(status_cb, f"Placing editor SFX over {len(scenes)} cut point(s) with the local SFX library "
                       f"(amount: {str(sfx_amount)}, same engine as a normal run)...")
        agent_core.place_editor_sfx(sfx_config, reasoning_model=reasoning_model, status_cb=status_cb)
        events = sfx_config.get("ai_content_sfx") or []
        sfx_report = sfx_config.get("sfx_report") or {}
        segments = [{
            "path": Path(e["path"]), "start": float(e["start"]),
            "duration": float(e["duration"]), "volume": float(e["volume"]),
            "source_trim": float(e.get("source_trim") or 0.0),
            "source_duration": float(e.get("source_duration") or 0.0),
            "playback_rate": float(e.get("playback_rate") or 1.0),
            "category": e.get("category") or e.get("sfx_type") or "sfx",
            "reason": e.get("sfx_type") or e.get("category") or "",
        } for e in events if e.get("path")]
        if segments:
            # FILE-LEVEL review only for the deterministic path (the director already reasoned).
            segments = refine_segments_with_llm(segments, phrases, reasoning_model=reasoning_model,
                                                status_cb=status_cb, sfx_amount=sfx_amount)
    log(status_cb, f"Placed {len(segments)} sound effect(s) [{plan_source}].")
    if not segments:
        raise RuntimeError("No sound effects placed - your soundeffects/ library has no usable clips. "
                           "Drop SFX files into the soundeffects/ folder and try again.")

    mix_into_video(video_path, segments, out_path, ffmpeg, ffprobe, duration, status_cb=status_cb)
    log(status_cb, "SFX enhancement complete.")

    # Make the result EDITABLE: build a real project (segment clips + original audio as the
    # voice track + every sound as custom_sfx) so the timeline editor can open it.
    timeline_slug = None
    try:
        timeline_slug = _write_timeline_project(video_path, segments, scenes, duration,
                                                ffmpeg, out_path, status_cb=status_cb)
    except Exception as exc:
        log(status_cb, f"Timeline project could not be created ({exc}); the enhanced video "
                       "is still fine.")

    # Write the plan + a readable report next to the output.
    plan_path = out_path.with_name(out_path.stem + "_plan.json")
    plan_payload = {
        "source_video": str(video_path),
        "duration": round(duration, 2),
        "plan_source": plan_source,
        "cuts": cuts,
        "events": [
            {"time": s["start"], "category": s["category"], "volume": s["volume"],
             "file": s["path"].name, "reason": s["reason"]}
            for s in segments
        ],
        "sfx_report": sfx_report,   # same scanner/classification/summary as a normal agent run
    }
    plan_path.write_text(json.dumps(plan_payload, indent=2), encoding="utf-8")

    return {
        "video": str(out_path),
        "original_video": str(video_path),
        "sfx_plan": str(plan_path),
        "sfx_event_count": len(segments),
        "plan_source": plan_source,
        "timeline_slug": timeline_slug,
        "project_dir": str(agent_core.PROJECTS_DIR / timeline_slug) if timeline_slug else None,
    }
