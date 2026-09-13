import json
import re
import math
from pathlib import Path
import agent_core

def clean_text(text):
    return (
        text.replace("\u201c", '"')
        .replace("\u201d", '"')
        .replace("\u2018", "'")
        .replace("\u2019", "'")
        .replace("\u2013", "-")
        .replace("\u2014", "-")
        .strip()
    )

def estimate_speaking_duration(script, words_per_minute=155):
    count = len(re.findall(r"\b[\w'-]+\b", clean_text(script)))
    if count <= 0:
        return 50.0
    seconds = (count / float(words_per_minute)) * 60.0
    return round(max(12.0, min(90.0, seconds * 1.08)), 2)

def estimate_line_timing(script_text, target_duration=None):
    """
    Estimates timing for sentences in the script if no audio timing is available.
    Sets timing_source to 'estimated_script_timing'.
    """
    target_duration = target_duration or estimate_speaking_duration(script_text)
    
    # Check if script has manual timestamps e.g. 0:00-0:05
    try:
        scenes = agent_core.parse_timed_script(script_text, target_duration)
        for scene in scenes:
            scene["timing_source"] = "estimated_script_timing"
            scene["exact_voice_text"] = scene["script"]
            scene["duration"] = round(scene["end"] - scene["start"], 2)
        return scenes
    except Exception:
        pass

    # Fallback to evenly distributing sentences
    sentences = re.split(r"(?<=[.!?])\s+", clean_text(script_text))
    sentences = [s.strip() for s in sentences if s.strip()]
    if not sentences:
        sentences = [script_text.strip()]

    scene_count = min(8, max(1, math.ceil(len(sentences) / 2)))
    groups = [[] for _ in range(scene_count)]
    for index, sentence in enumerate(sentences):
        groups[min(scene_count - 1, int(index * scene_count / len(sentences)))].append(sentence)
    
    scene_duration = float(target_duration) / scene_count
    scenes = []
    for index, group in enumerate(groups):
        if not group:
            continue
        start = round(index * scene_duration, 2)
        end = round((index + 1) * scene_duration, 2)
        scenes.append(
            {
                "start": start,
                "end": end,
                "duration": round(end - start, 2),
                "script": " ".join(group),
                "exact_voice_text": " ".join(group),
                "timing_source": "estimated_script_timing"
            }
        )
    return scenes

def normalize_timestamps(analysis, fallback_script, fallback_duration, audio_duration=None):
    """
    Extracts timing from WaveSpeed/Gemini JSON output.
    """
    scenes, source_type = agent_core.normalize_audio_scenes(
        analysis, fallback_script, fallback_duration, audio_duration, return_source=True
    )
    
    # Map source_type to standardized timing_source.
    # THE ANALYSIS KNOWS WHERE IT CAME FROM; ASK IT FIRST. "gemini_audio_analysis" was the
    # default for every shape this mapping did not name, so a run timed by the LOCAL forced
    # aligner wrote voice_source.json saying Gemini had done it - while audio_analysis.json,
    # next to it, said forced_alignment. Two files, one run, contradicting each other about how
    # the timing was produced. That matters exactly when something has gone wrong and these
    # files are the only record of it.
    declared = str((analysis or {}).get("timing_source") or "").strip() if isinstance(analysis, dict) else ""
    timing_source = declared or "gemini_audio_analysis"
    if source_type in ("sentences", "timed_phrases"):
        timing_source = declared or "audio_line_timestamps"
    elif source_type == "fallback":
        timing_source = "estimated_script_timing"        # a guess is a guess, whoever asked for it
        
    for scene in scenes:
        scene["timing_source"] = timing_source
        scene["exact_voice_text"] = scene["script"]
        scene["duration"] = round(scene["end"] - scene["start"], 2)
        
    return scenes, timing_source

def validate_timing_contiguity(scenes, total_duration):
    """
    Ensures that scenes are contiguous, fill the audio duration, and don't overlap wildly.
    """
    cleaned = []
    last_end = 0.0
    for scene in sorted(scenes, key=lambda s: s["start"]):
        start = max(last_end, scene["start"])
        end = max(start + 0.3, scene["end"])
        
        if total_duration and end > total_duration:
            end = total_duration
            
        scene["start"] = round(start, 2)
        scene["end"] = round(end, 2)
        scene["duration"] = round(end - start, 2)
        
        if scene["duration"] > 0:
            cleaned.append(scene)
            last_end = scene["end"]
            
    # Snap last scene to total_duration if very close
    if cleaned and total_duration and total_duration - cleaned[-1]["end"] < 0.5:
        cleaned[-1]["end"] = round(total_duration, 2)
        cleaned[-1]["duration"] = round(cleaned[-1]["end"] - cleaned[-1]["start"], 2)
        
    return cleaned

def write_voice_timing_files(project_dir, scenes, timing_source, is_estimated, audio_duration, loudness_report=None):
    """
    Writes the voice timing output to the voice directory.
    """
    voice_dir = Path(project_dir) / "voice"
    voice_dir.mkdir(parents=True, exist_ok=True)
    
    # Line timestamps
    (voice_dir / "line_timestamps.json").write_text(
        json.dumps({
            "timing_source": timing_source,
            "is_estimated": is_estimated,
            "duration": audio_duration,
            "lines": scenes
        }, indent=2), 
        encoding="utf-8"
    )
    
    # Word timestamps (stub for future if we get word-level precision)
    (voice_dir / "word_timestamps.json").write_text(json.dumps([], indent=2), encoding="utf-8")
    
    # Voice source metadata
    (voice_dir / "voice_source.json").write_text(
        json.dumps({
            "source_type": timing_source,
            "is_estimated": is_estimated,
            "duration": audio_duration
        }, indent=2), 
        encoding="utf-8"
    )
    
    if loudness_report:
        (voice_dir / "loudness_report.json").write_text(json.dumps(loudness_report, indent=2), encoding="utf-8")

def load_or_create_voice_timing(project_dir, script_text, uploaded_audio_path=None, wavespeed_analysis=None):
    """
    The main entrypoint for finalizing the voice timeline.
    """
    audio_duration = None
    if uploaded_audio_path:
        audio_duration = agent_core.probe_audio_duration(uploaded_audio_path)
    
    target_duration = audio_duration or estimate_speaking_duration(script_text)
    
    if wavespeed_analysis:
        scenes, timing_source = normalize_timestamps(
            wavespeed_analysis, script_text, target_duration, audio_duration
        )
        is_estimated = (timing_source == "estimated_script_timing")
    else:
        scenes = estimate_line_timing(script_text, target_duration)
        timing_source = "estimated_script_timing"
        is_estimated = True

    scenes = validate_timing_contiguity(scenes, audio_duration)
    
    write_voice_timing_files(
        project_dir, 
        scenes, 
        timing_source=timing_source,
        is_estimated=is_estimated,
        audio_duration=audio_duration
    )
    
    return {
        "duration": target_duration,
        "is_estimated": is_estimated,
        "timing_source": timing_source,
        "scenes": scenes
    }
