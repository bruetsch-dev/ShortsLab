"""Rebuild the manual school-cleaning Short with short, non-overlapping source windows.

This tool deliberately needs only FFmpeg. It keeps the project editable even
when the desktop server is not running, and refuses the long 7-second holds
that made the previous export feel static.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SLUG = "manual_school_cleaning_no_repeats"
PROJECT = ROOT / "projects" / SLUG
VIDEO_DIR = PROJECT / "seedance 2.0"
CONFIG_PATH = PROJECT / "config" / "project.json"
OUT = PROJECT / "renders" / "why_japanese_students_clean_their_own_schools_no_repeats_v3_unique_sources.mp4"
ASS = PROJECT / "captions" / "why_japanese_students_clean_their_own_schools_no_repeats_v3_unique_sources.ass"

# Every shot is intentionally about 2.3 seconds.  This export takes the
# stronger rule the user asked for: one distinct TikTok source per visible
# beat, rather than merely non-overlapping moments from four source videos.
PICKS = [
    ("7430063649370262800", 0.30, 2.60, "student mops a classroom hook"),
    ("7424448977082273057", 0.40, 2.70, "student cleaning a school wash area"),
    ("7499378535069699336", 1.20, 3.50, "students with brooms in corridor"),
    ("7542460911789935890", 0.45, 2.75, "desk-wiping proof shot"),
    ("7559405909424852245", 0.50, 2.80, "student cleaning in a Japanese school"),
    ("7602956602324339975", 0.50, 2.80, "students sweep a hallway together"),
    ("7615989659339951380", 1.00, 3.30, "students hold classroom brooms"),
    ("7625922745804672263", 0.40, 2.70, "student cleans the classroom board"),
    ("7627439820012391700", 0.50, 2.80, "students wipe the wooden school floor"),
    ("7643803333920132360", 0.50, 2.80, "school hallway cleaning moment"),
    ("7656737464266001685", 0.50, 2.80, "students take part in school clean-up"),
    ("7672281615581613333", 0.50, 2.80, "floor-wiping teamwork payoff"),
]


def ffmpeg_path() -> str:
    import pipeline
    found = pipeline.find_ffmpeg()
    if not found:
        raise FileNotFoundError("FFmpeg was not found.")
    return str(found)


def duration(ffprobe: str, media: Path) -> float:
    if not Path(ffprobe).is_file():
        import cv2
        cap = cv2.VideoCapture(str(media))
        try:
            fps = float(cap.get(cv2.CAP_PROP_FPS) or 0)
            frames = float(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
            return frames / fps if fps else 0.0
        finally:
            cap.release()
    result = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(media)],
        check=True, capture_output=True, text=True,
    )
    return float(result.stdout.strip())


def ass_time(seconds: float) -> str:
    centis = max(0, round(seconds * 100))
    hours, rest = divmod(centis, 360000)
    minutes, rest = divmod(rest, 6000)
    secs, hundredths = divmod(rest, 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{hundredths:02d}"


def ass_escape(text: str) -> str:
    return str(text).upper().replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")


def write_plain_word_captions(words: list[dict]) -> None:
    ASS.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "[Script Info]", "ScriptType: v4.00+", "PlayResX: 1080", "PlayResY: 1920", "",
        "[V4+ Styles]",
        "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding",
        "Style: Plain,Arial,94,&H00FFFFFF,&H00FFFFFF,&H00111111,&H00000000,1,0,0,0,100,100,0,0,1,3,0,2,70,70,555,1",
        "", "[Events]",
        "Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text",
    ]
    for item in words:
        start = float(item["start"])
        end = max(start + 0.08, float(item["end"]))
        lines.append(f"Dialogue: 0,{ass_time(start)},{ass_time(end)},Plain,,0,0,0,,{ass_escape(item['word'])}")
    ASS.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")


def flatten_words(config: dict) -> list[dict]:
    words: list[dict] = []
    for row in config.get("timeline_caption_track") or []:
        row_start = float(row.get("start") or 0)
        for word in row.get("word_timings") or []:
            words.append({"word": str(word.get("word") or "").strip(),
                          "start": row_start + float(word.get("start") or 0),
                          "end": row_start + float(word.get("end") or 0)})
    return [word for word in words if word["word"]]


def scene_data(words: list[dict], voice_seconds: float) -> list[dict]:
    scenes = []
    for index, (source_id, _source_start, _source_end, label) in enumerate(PICKS, 1):
        start = round((index - 1) * 2.3, 3)
        end = round(min(voice_seconds, index * 2.3), 3)
        local = [word for word in words if float(word["end"]) > start and float(word["start"]) < end]
        local_words = [{"word": word["word"], "start": round(max(0.0, float(word["start"]) - start), 3),
                        "end": round(max(0.0, min(end, float(word["end"])) - start), 3)} for word in local]
        text = " ".join(word["word"] for word in local)
        scenes.append({
            "id": f"clean-{index}", "name": label, "start": start, "end": end,
            "script": text, "exact_voice_text": text, "word_timings": local_words,
            "clip": f"clean_unique_{index:02d}_{source_id}.mp4", "asset": f"clean_unique_{index:02d}_{source_id}.mp4",
            "seedance": True, "assignment_type": "manual_gemini37_verified", "scrape_source": "tiktok",
            "scrape_clip_id": source_id, "native_9_16": True, "source_has_captions": False,
            "blur_captions": False, "render_caption": True, "timeline_speed": 1.0,
            "motion": {"zoom_start": 1.0, "zoom_end": 1.0, "pan_x": 0, "pan_y": 0},
        })
    return [scene for scene in scenes if scene["start"] < voice_seconds]


def validate_windows() -> None:
    for index, (source, start, end, _label) in enumerate(PICKS):
        if end - start > 2.75:
            raise ValueError(f"Shot {index + 1} is too long: {end - start:.2f}s")
        for other_source, other_start, other_end, _ in PICKS[:index]:
            if source == other_source and not (end <= other_start or start >= other_end):
                raise ValueError(f"Source {source} has overlapping reused moments")


def main() -> None:
    validate_windows()
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    ffmpeg = ffmpeg_path()
    ffprobe = str(Path(ffmpeg).with_name("ffprobe.exe"))
    voice = Path(config["audio_path"])
    if not voice.is_file():
        raise FileNotFoundError(voice)
    voice_seconds = duration(ffprobe, voice)
    words = flatten_words(config)
    if not words:
        raise RuntimeError("No existing aligned voice words found for captions.")
    write_plain_word_captions(words)
    inputs, filters = [], []
    for index, (source_id, start, end, _label) in enumerate(PICKS):
        source = VIDEO_DIR / "_fresh_sources" / f"tiktok_{source_id}.mp4"
        if not source.is_file():
            raise FileNotFoundError(source)
        inputs.extend(["-ss", f"{start:.3f}", "-t", f"{end - start:.3f}", "-i", str(source)])
        filters.append(f"[{index}:v]fps=30,scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1,format=yuv420p[v{index}]")
    joined = "".join(f"[v{index}]" for index in range(len(PICKS)))
    filters.append(f"{joined}concat=n={len(PICKS)}:v=1:a=0[cut]")
    ass_path = str(ASS.resolve()).replace("\\", "/").replace(":", "\\:")
    filters.append(f"[cut]ass='{ass_path}'[outv]")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    command = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", *inputs, "-i", str(voice),
               "-filter_complex", ";".join(filters), "-map", "[outv]", "-map", f"{len(PICKS)}:a:0",
               "-t", f"{voice_seconds:.3f}", "-c:v", "libx264", "-preset", "medium", "-crf", "18",
               "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(OUT)]
    print("Rendering 12 short non-overlapping visual windows …", flush=True)
    result = subprocess.run(command, capture_output=True, text=True, timeout=900)
    if result.returncode or not OUT.is_file() or OUT.stat().st_size < 500_000:
        raise RuntimeError(f"FFmpeg render failed: {result.stderr[-1200:]}")
    scenes = scene_data(words, voice_seconds)
    config["duration"] = round(voice_seconds, 3)
    config["audio_path"] = str(voice)
    config["scenes"] = scenes
    config["timeline_caption_track"] = [{"text": scene["script"], "start": scene["start"], "end": scene["end"], "word_timings": scene["word_timings"]} for scene in scenes]
    config["output_basename"] = OUT.stem
    CONFIG_PATH.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    audit = [{"source_id": source, "source_start": start, "source_end": end, "duration": round(end - start, 3), "non_overlapping_reuse": True, "label": label} for source, start, end, label in PICKS]
    (PROJECT / "review" / "source_window_audit_v2.json").write_text(json.dumps(audit, indent=2), encoding="utf-8")
    print(OUT, flush=True)


if __name__ == "__main__":
    main()
