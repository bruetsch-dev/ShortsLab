"""Remove known spoken citation intervals from a finished longform project without new AI calls."""
import argparse
import json
import shutil
import subprocess
import time
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import longform_video as lf
import pipeline


def _shift_time(value, cuts):
    value = float(value or 0.0)
    removed = 0.0
    for start, end in cuts:
        if value >= end:
            removed += end - start
        elif value > start:
            return start - removed
        else:
            break
    return value - removed


def repair(project_dir, cuts):
    project = Path(project_dir).resolve()
    state_path = project / lf.STATE_FILE
    state = json.loads(state_path.read_text(encoding="utf-8"))
    source = project / Path(str(state.get("voiceover_file") or "voiceover.wav")).name
    if not source.is_file():
        raise FileNotFoundError(source)
    cuts = sorted((float(a), float(b)) for a, b in cuts)
    if any(a < 0 or b <= a for a, b in cuts):
        raise ValueError("Every cut must be start:end with end greater than start.")

    backup = project / "backups" / f"citation_cleanup_{time.strftime('%Y%m%d_%H%M%S')}"
    backup.mkdir(parents=True, exist_ok=False)
    for name in (source.name, lf.STATE_FILE, "script.txt", "transcript.txt", "timeline.json"):
        path = project / name
        if path.is_file():
            shutil.copy2(path, backup / name)

    duration = lf.audio_duration_seconds(source)
    points = [0.0] + [x for cut in cuts for x in cut] + [duration]
    keep = [(points[i], points[i + 1]) for i in range(0, len(points) - 1, 2)]
    filters = []
    labels = []
    for idx, (start, end) in enumerate(keep):
        filters.append(f"[0:a]atrim=start={start:.3f}:end={end:.3f},asetpts=PTS-STARTPTS[a{idx}]")
        labels.append(f"[a{idx}]")
    filters.append("".join(labels) + f"concat=n={len(keep)}:v=0:a=1[out]")
    output = project / "voiceover_citation_clean.wav"
    ffmpeg = pipeline.find_ffmpeg()
    result = subprocess.run(
        [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(source),
         "-filter_complex", ";".join(filters), "-map", "[out]", "-c:a", "pcm_s16le",
         str(output)], capture_output=True, text=True,
    )
    if result.returncode or not lf._audio_done(output):
        raise RuntimeError(result.stderr or "ffmpeg did not create repaired audio")

    script = lf.clean_narration_script(state.get("script") or
                                       (project / "script.txt").read_text(encoding="utf-8"))
    lines = []
    for old in state.get("lines") or []:
        line = dict(old)
        line["text"] = lf.clean_narration_script(line.get("text") or "")
        kept_words = []
        for word in line.get("words") or []:
            spoken_word = str(word.get("w") or word.get("word") or "")
            if not lf.clean_narration_script(spoken_word):
                continue
            stamp = float(word.get("s") or 0.0)
            if any(start <= stamp < end for start, end in cuts):
                continue
            item = dict(word)
            item["s"] = round(_shift_time(stamp, cuts), 2)
            kept_words.append(item)
        line["words"] = kept_words
        line["start"] = round(kept_words[0]["s"] if kept_words else
                              _shift_time(line.get("start"), cuts), 2)
        line["end"] = round(max(line["start"], _shift_time(line.get("end"), cuts)), 2)
        if line["text"]:
            lines.append(line)

    new_duration = round(duration - sum(end - start for start, end in cuts), 3)
    prompts = list(state.get("prompts") or [])
    for idx, prompt in enumerate(prompts[:len(lines)]):
        if isinstance(prompt, dict):
            prompt["timestamp"] = lf.fmt_ts(lines[idx]["start"])

    (project / "script.txt").write_text(script, encoding="utf-8")
    lf.write_transcript(lines, project / "transcript.txt")
    if prompts:
        lf.write_prompts_file(prompts, project / f"image_prompts_{project.name}.txt")

    timeline_path = project / "timeline.json"
    timeline = json.loads(timeline_path.read_text(encoding="utf-8"))
    scenes = timeline.get("scenes") or []
    for idx, scene in enumerate(scenes[:len(lines)]):
        start = float(lines[idx]["start"])
        end = float(lines[idx + 1]["start"]) if idx + 1 < len(lines) else new_duration
        scene.update({"start": round(start, 3), "end": round(end, 3),
                      "duration": round(max(0.04, end - start), 3),
                      "text": lines[idx]["text"]})
    timeline["audio_duration"] = new_duration
    timeline["scenes"] = scenes[:len(lines)]
    timeline_path.write_text(json.dumps(timeline, ensure_ascii=False, indent=1), encoding="utf-8")

    state.update({"script": script, "lines": lines, "prompts": prompts,
                  "audio_duration": new_duration, "voiceover_file": output.name,
                  "voiceover_ready": True, "tts_part_files": [], "tts_part_texts": [],
                  "tts_part_total": 0})
    tmp = state_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(state_path)
    return output, backup, duration, new_duration


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("project")
    parser.add_argument("--cut", action="append", required=True)
    args = parser.parse_args()
    parsed = [tuple(map(float, value.split(":", 1))) for value in args.cut]
    repaired, backup_dir, old_duration, new_duration = repair(args.project, parsed)
    print(json.dumps({"voiceover": str(repaired), "backup": str(backup_dir),
                      "old_duration": old_duration, "new_duration": new_duration}, indent=2))
