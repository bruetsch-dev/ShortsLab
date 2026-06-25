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

import json
import os
import re
import subprocess
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


def transcribe_with_timing(video_path, ffmpeg, ffprobe, duration, status_cb=None):
    """Extract audio and return timed phrases [{start,end,text}] via Gemini."""
    if not os.environ.get("WAVESPEED_API_KEY"):
        log(status_cb, "No WAVESPEED_API_KEY; skipping transcription (cuts-only mode).")
        return []
    audio_path = video_path.with_suffix(".sfx_audio.mp3")
    try:
        _run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(video_path),
              "-vn", "-ac", "1", "-ar", "44100", "-c:a", "libmp3lame", "-b:a", "64k",
              str(audio_path)], timeout=180)
        if not audio_path.exists():
            return []
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
    """Categories actually present in the local library, for the LLM prompt."""
    catalog = {}
    for name, meta in AGENT_SFX_CATEGORIES.items():
        files = pipeline.sfx_category_files(config, meta["lib"])
        if files:
            catalog[name] = {"desc": meta["desc"], "available": len(files)}
    return catalog


def plan_sfx_with_llm(duration, cuts, phrases, catalog, reasoning_model=None, status_cb=None):
    if not os.environ.get("WAVESPEED_API_KEY") or not catalog:
        return None
    category_lines = "\n".join(f"- {name}: {meta['desc']}" for name, meta in catalog.items())
    prompt = (
        "You are a meticulous sound designer for short vertical videos.\n"
        "You are given a finished video's duration, its hard visual cut timestamps, and the timed "
        "speech transcript. Place tasteful sound effects that make the video feel professionally "
        "edited WITHOUT being cheesy or constant.\n\n"
        "Use ONLY these categories:\n" + category_lines + "\n\n"
        "Guidelines:\n"
        "- Put a 'transition' or 'whoosh' on most real image/scene changes (use the cut timestamps).\n"
        "- Use 'impact', 'boom', or 'stinger' to emphasise a specific powerful word, number, reveal, "
        "or punchline - align the time to when that word is spoken in the transcript.\n"
        "- Use 'riser' sparingly before a big payoff; 'foley' only when it matches an on-screen action.\n"
        "- Keep it sparse and intentional: roughly 1 effect every 2-4 seconds, never stack two within 0.4s.\n"
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


def resolve_segments(config, events, duration, ffprobe):
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
        end = segment["duration"]
        fade_out = max(0.0, end - 0.06)
        filters.append(
            f"[{input_index}:a]atrim=0:{end:.3f},asetpts=PTS-STARTPTS,"
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


def enhance_video_with_sfx(video_path, reasoning_model=None, status_cb=None, out_dir=None):
    video_path = Path(video_path)
    if not video_path.exists():
        raise RuntimeError("Uploaded video not found.")
    if video_path.suffix.lower() not in VIDEO_EXTS:
        raise RuntimeError(f"Unsupported video type: {video_path.suffix}")

    ffmpeg = pipeline.find_ffmpeg()
    ffprobe = pipeline.find_ffprobe(ffmpeg)
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found; cannot process video.")

    config = {
        "project_slug": "sfx_enhance",
        "output_basename": video_path.stem,
        "sfx_library_dir": str(ROOT / "soundeffects"),
        "sfx_single_transition_sound_per_video": False,
    }
    out_dir = Path(out_dir) if out_dir else SFX_OUTPUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    out_path = out_dir / f"{video_path.stem}_sfx_{stamp}.mp4"

    duration = media_duration(video_path, ffprobe)
    log(status_cb, f"Loaded video: {duration:.1f}s.")

    log(status_cb, "Detecting scene changes...")
    cuts = detect_scene_cuts(video_path, ffmpeg)
    log(status_cb, f"Found {len(cuts)} visual cut(s).")

    phrases = transcribe_with_timing(video_path, ffmpeg, ffprobe, duration, status_cb=status_cb)
    if phrases:
        log(status_cb, f"Transcript: {len(phrases)} timed phrase(s).")

    catalog = library_catalog(config)
    events = plan_sfx_with_llm(duration, cuts, phrases, catalog, reasoning_model, status_cb)
    plan_source = "opus"
    if not events:
        log(status_cb, "Using automatic cut-based sound effects.")
        events = fallback_plan(cuts, phrases, duration)
        plan_source = "automatic"
    log(status_cb, f"Planned {len(events)} sound-effect event(s) [{plan_source}].")

    segments = resolve_segments(config, events, duration, ffprobe)
    log(status_cb, f"Placed {len(segments)} sound effect(s) after spacing/dedup.")
    if not segments:
        raise RuntimeError("No sound effects could be placed (empty plan or library).")

    mix_into_video(video_path, segments, out_path, ffmpeg, ffprobe, duration, status_cb=status_cb)
    log(status_cb, "SFX enhancement complete.")

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
    }
    plan_path.write_text(json.dumps(plan_payload, indent=2), encoding="utf-8")

    return {
        "video": str(out_path),
        "original_video": str(video_path),
        "sfx_plan": str(plan_path),
        "sfx_event_count": len(segments),
        "plan_source": plan_source,
    }
