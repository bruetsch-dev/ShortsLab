from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path


ROOT = Path(r"D:\data\AutoShortsClaude")
# All selected source windows are true 30fps and are cadence-checked below. Keeping their native
# cadence avoids inventing interpolation artefacts while still excluding duplicated-frame lag.
FPS = 30
WIDTH = 1080
HEIGHT = 1920


def run(cmd: list[str], cwd: Path | None = None) -> None:
    print(" ".join(str(part) for part in cmd), flush=True)
    subprocess.run(cmd, cwd=str(cwd) if cwd else None, check=True)


def probe(path: Path) -> tuple[int, int, float]:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height:format=duration", "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    )
    data = json.loads(result.stdout)
    stream = (data.get("streams") or [{}])[0]
    return int(stream.get("width") or 0), int(stream.get("height") or 0), float(data["format"]["duration"])


def audio_duration(path: Path) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    )
    return float(result.stdout.strip())


def assert_smooth_clip(path: Path, label: str) -> None:
    """Refuse repeated-frame stalls before they become part of the picture lock.

    A nominal CFR/30 stream can still look broken when its source contains several nearly
    identical frames. Timestamp and FPS probes miss that, so use FFmpeg's pixel-level freeze
    detector on every extracted shot and stop at five stalled frames (0.15 s).
    """
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "info", "-i", str(path),
         "-vf", "freezedetect=n=-42dB:d=0.15", "-an", "-f", "null", "NUL"],
        capture_output=True, text=True,
    )
    report = (result.stderr or "") + "\n" + (result.stdout or "")
    durations = [float(value) for value in re.findall(
        r"lavfi\.freezedetect\.freeze_duration:\s*([0-9.]+)", report)]
    if durations:
        raise RuntimeError(
            f"shot contains a repeated-frame stall ({max(durations):.3f}s): {label}")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ass_time(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    return f"{int(seconds // 3600)}:{int(seconds % 3600 // 60):02d}:{seconds % 60:05.2f}"


def make_ass(captions_json: Path, out: Path) -> None:
    data = json.loads(captions_json.read_text(encoding="utf-8"))
    lines = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {WIDTH}", f"PlayResY: {HEIGHT}",
        "ScaledBorderAndShadow: yes", "WrapStyle: 2", "", "[V4+ Styles]",
        "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding",
        "Style: Caption,Arial Black,92,&H00FFFFFF,&H00FFFFFF,&H00101010,&H58000000,-1,0,0,0,100,100,0,0,1,9,3,5,60,60,0,1",
        "", "[Events]",
        "Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text",
    ]
    for row in data.get("captions") or []:
        text = str(row.get("text") or "").strip().replace("{", "(").replace("}", ")")
        if not text:
            continue
        start, end = float(row.get("start") or 0), float(row.get("end") or 0)
        lines.append(
            f"Dialogue: 2,{ass_time(start)},{ass_time(end)},Caption,,0,0,0,,"
            rf"{{\pos(540,1110)\fscx91\fscy91\t(0,80,\fscx100\fscy100)}}{text}"
        )
    out.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")


def build(plan_path: Path) -> Path:
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    project = ROOT / "projects" / plan["project_slug"]
    work = project / "work"
    clips_dir = work / "clips"
    input_dir = project / "input"
    renders = project / "renders"
    review = project / "review"
    for folder in (clips_dir, input_dir, renders, review):
        folder.mkdir(parents=True, exist_ok=True)

    source_audio = Path(plan["source_audio"])
    if not source_audio.exists():
        raise FileNotFoundError(source_audio)
    voice = input_dir / source_audio.name
    shutil.copy2(source_audio, voice)
    if sha256(source_audio) != sha256(voice):
        raise RuntimeError("voiceover copy hash mismatch")
    duration = audio_duration(voice)

    source_cache: dict[str, tuple[int, int, float]] = {}
    clips: list[Path] = []
    shots = plan.get("shots") or []
    if not shots:
        raise RuntimeError("plan has no shots")
    for index, shot in enumerate(shots):
        source = Path(shot["source"])
        if not source.exists():
            raise FileNotFoundError(source)
        key = str(source.resolve())
        if key not in source_cache:
            source_cache[key] = probe(source)
        width, height, source_duration = source_cache[key]
        if height <= width or abs(width / float(height) - 9.0 / 16.0) > 0.04:
            raise RuntimeError(f"source is not native 9:16: {source} ({width}x{height})")
        start, end = float(shot["start"]), float(shot["end"])
        source_start = float(shot["source_start"])
        frames = max(1, round(end * FPS) - round(start * FPS))
        needed = frames / FPS
        if source_start + needed > source_duration + 0.04:
            raise RuntimeError(f"shot exceeds source duration: {shot['name']}")
        clip = clips_dir / f"{index:02d}_{shot['name']}.mp4"
        run([
            "ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "warning",
            "-ss", f"{source_start:.3f}", "-i", str(source), "-an",
            "-vf", f"scale={WIDTH}:{HEIGHT},eq=contrast=1.025:saturation=1.045,fps={FPS},setsar=1,format=yuv420p",
            "-frames:v", str(frames), "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
            "-pix_fmt", "yuv420p", str(clip),
        ])
        assert_smooth_clip(clip, str(shot.get("name") or clip.name))
        clips.append(clip)

    picture = work / "picture_lock.mp4"
    concat_cmd = ["ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "warning"]
    for clip in clips:
        concat_cmd += ["-i", str(clip)]
    chains = [f"[{i}:v]setpts=PTS-STARTPTS,fps={FPS},format=yuv420p[v{i}]" for i in range(len(clips))]
    chains.append("".join(f"[v{i}]" for i in range(len(clips))) + f"concat=n={len(clips)}:v=1:a=0[v]")
    concat_cmd += ["-filter_complex", ";".join(chains), "-map", "[v]", "-an", "-c:v", "libx264",
                   "-preset", "ultrafast", "-crf", "15", "-pix_fmt", "yuv420p", str(picture)]
    run(concat_cmd)

    captions = work / "captions.ass"
    make_ass(Path(plan["captions_json"]), captions)
    output = renders / f"{plan['project_slug']}_auto_short.mp4"
    run([
        "ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "warning", "-i", str(picture),
        "-i", str(voice), "-vf", "subtitles=captions.ass", "-map", "0:v:0", "-map", "1:a:0",
        "-t", f"{duration:.3f}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        "-movflags", "+faststart", str(output),
    ], cwd=work)

    manifest = dict(plan)
    manifest.update({
        "duration": round(duration, 3),
        "voiceover_sha256": sha256(source_audio),
        "copied_voiceover_sha256": sha256(voice),
        "background_music": False,
        "source_policy": "new, manually reviewed, native 9:16 footage only",
        "source_media": [
            {"path": key, "width": dims[0], "height": dims[1], "duration": round(dims[2], 3)}
            for key, dims in source_cache.items()
        ],
    })
    (project / "v3_manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    run([
        "ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "error", "-i", str(output),
        "-vf", "fps=12/30,scale=180:320,tile=6x2:padding=5:margin=5:color=0x070907",
        "-frames:v", "1", str(review / "final_contact_sheet.jpg"),
    ])
    print(f"FINAL={output}")
    return output


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("plan", type=Path)
    args = parser.parse_args()
    build(args.plan.resolve())


if __name__ == "__main__":
    main()
