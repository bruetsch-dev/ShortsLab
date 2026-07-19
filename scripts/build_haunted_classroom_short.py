from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path


ROOT = Path(r"D:\data\AutoShortsClaude")
PROJECT = ROOT / "projects" / "japanese_classroom_haunted_house"
INPUT = PROJECT / "input"
WORK = PROJECT / "work"
RENDERS = PROJECT / "renders"
REVIEW = PROJECT / "review"
SOURCE = (
    ROOT
    / "projects"
    / "japanese_school_festival_three_things"
    / "sources"
    / "haunted"
    / "hQWw5sYoXNM_hq.mp4"
)
WALK_SOURCE = (
    ROOT
    / "projects"
    / "japanese_school_festival_three_things"
    / "sources"
    / "haunted"
    / "i_yTOtHRNSM_hq.mp4"
)
SFX = ROOT / "soundeffects"
FPS = 30
WIDTH = 720
HEIGHT = 1280


# Every shot comes from the same class and the same school-festival production.
# `x` is the horizontal subject position in the 16:9 source (0=left, 1=right).
# `crop` fills 9:16 for true close-ups. `context` keeps a much wider 4:5 view
# inside a clean dark canvas. `fit` preserves the complete 16:9 source frame.
SHOTS = [
    ("hook_finished_wall", 240.0, 0.00, 0.82, 0.48, "crop"),
    ("hook_dark_entry", 243.0, 0.82, 1.62, 0.48, "context"),
    ("hook_ghost", 295.0, 1.62, 2.46, 0.53, "crop"),
    # The mandatory hook/body edit is exactly 0.5 seconds. The next shot begins at 2.96.
    ("class_at_work", 0.0, 2.46, 3.88, 0.48, "context"),
    ("maze_frame", 155.0, 3.88, 4.78, 0.52, "context"),
    ("maze_walls", 160.0, 4.78, 5.68, 0.52, "context"),
    ("maze_cover", 170.0, 5.68, 6.04, 0.52, "context"),
    ("newspaper_paint", 5.0, 6.04, 6.82, 0.48, "context"),
    ("fake_blood_close", 30.0, 6.82, 7.60, 0.52, "context"),
    ("black_plastic", 90.0, 7.60, 8.64, 0.55, "context"),
    ("wrap_the_wall", 110.0, 8.64, 9.40, 0.62, "context"),
    ("desk_tunnel", 145.0, 9.40, 10.36, 0.46, "context"),
    ("tunnel_finished", 175.0, 10.36, 11.18, 0.54, "context"),
    ("hide_in_dark", 198.0, 11.18, 12.35, 0.50, "context"),
    ("bloody_hands", 213.0, 12.35, 13.46, 0.52, "crop"),
    ("flashlight_room", 205.0, 13.46, 14.38, 0.48, "context"),
    ("doors_open", 80.0, 14.38, 15.30, 0.48, "context"),
    ("enter_the_maze", 96.0, 15.30, 16.38, 0.50, "context"),
    ("total_darkness", 108.0, 16.38, 17.52, 0.50, "context"),
    ("students_waiting", 132.0, 17.52, 18.32, 0.50, "context"),
    ("jump_scare", 147.4, 18.32, 19.32, 0.48, "crop"),
    ("weeks_of_work", 35.0, 19.32, 20.42, 0.52, "context"),
    ("nightmare_complete", 240.0, 20.42, 21.58, 0.50, "context"),
    ("one_weekend", 297.0, 21.58, 22.80, 0.50, "context"),
]


def run(cmd: list[str], cwd: Path | None = None) -> None:
    print(" ".join(str(x) for x in cmd), flush=True)
    subprocess.run(cmd, cwd=str(cwd) if cwd else None, check=True)


def ass_time(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    return f"{hours}:{minutes:02d}:{seconds % 60:05.2f}"


def clean_word(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9']", "", value).upper()


def build_captions(words: list[dict]) -> Path:
    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {WIDTH}",
        f"PlayResY: {HEIGHT}",
        "ScaledBorderAndShadow: yes",
        "WrapStyle: 2",
        "",
        "[V4+ Styles]",
        "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding",
        "Style: Word,Arial Black,67,&H00FFFFFF,&H00FFFFFF,&H00101010,&H70000000,-1,0,0,0,100,100,0,0,1,7,2,5,35,35,0,1",
        "Style: Arrow,Segoe UI Symbol,112,&H002D39F2,&H002D39F2,&H00FFFFFF,&H60000000,-1,0,0,0,100,100,0,0,1,6,2,5,0,0,0,1",
        "",
        "[Events]",
        "Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text",
    ]
    emphasis = {"ALONE", "HAUNTED", "BLOOD", "BLACK", "DARKNESS", "NIGHTMARE", "WEEKEND"}
    for index, word in enumerate(words):
        start = float(word["start"])
        end = float(words[index + 1]["start"]) if index + 1 < len(words) else float(word["end"]) + 0.12
        token = clean_word(str(word["word"]))
        if not token:
            continue
        color = r"{\c&H002D39F2&}" if token in emphasis else ""
        # Tiny scale-in makes each word feel punched in without becoming a visual transition.
        text = rf"{{\fscx88\fscy88\t(0,85,\fscx100\fscy100)}}{color}{token}"
        lines.append(f"Dialogue: 3,{ass_time(start)},{ass_time(end)},Word,,0,0,0,,{text}")

    # Three restrained visual callouts, matching the reference edit's arrows.
    arrows = [
        (0.10, 0.95, 500, 310, 138),
        (6.78, 7.55, 520, 710, 145),
        (18.30, 19.10, 470, 500, -135),
    ]
    for start, end, x, y, rotation in arrows:
        text = rf"{{\pos({x},{y})\frz{rotation}\fscx45\fscy45\t(0,110,\fscx100\fscy100)}}➜"
        lines.append(f"Dialogue: 4,{ass_time(start)},{ass_time(end)},Arrow,,0,0,0,,{text}")

    out = WORK / "captions.ass"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
    return out


def build_shot(index: int, shot: tuple) -> Path:
    name, source_start, start, end, x, mode = shot
    # Derive every clip from shared absolute frame boundaries. Using `-t` on each
    # short input floors fractions independently and previously lost 0.27 seconds
    # across the edit, cutting off the final spoken word.
    start_frame = round(start * FPS)
    end_frame = round(end * FPS)
    frame_count = max(1, end_frame - start_frame)
    out = WORK / "clips" / f"{index:02d}_{name}.mp4"
    dark_scene = name in {"hide_in_dark", "flashlight_room", "total_darkness", "students_waiting"}
    source = WALK_SOURCE if name in {
        "doors_open", "enter_the_maze", "total_darkness", "students_waiting", "jump_scare"
    } else SOURCE
    grade = (
        "eq=contrast=1.055:saturation=1.06:gamma=1.18:brightness=0.018"
        if dark_scene
        else "eq=contrast=1.045:saturation=1.06"
    )
    if mode == "fit":
        vf = (
            f"scale={WIDTH}:-2,"
            f"pad={WIDTH}:{HEIGHT}:0:(oh-ih)/2:color=0x090C0A,"
            "eq=contrast=1.045:saturation=1.06,"
            f"fps={FPS},setsar=1,format=yuv420p"
        )
    elif mode == "context":
        # Editorial reframe: preserve the COMPLETE source frame above a closer
        # vertical detail from the very same moving shot. This uses the entire
        # screen without blur, while keeping both context and subject readable.
        vf = (
            "split=2[wide][detail];"
            f"[wide]scale={WIDTH}:404[widev];"
            "[detail]scale=-2:984,"
            f"crop={WIDTH}:876:x='(iw-ow)*{x:.4f}':y=0[detailv];"
            "[widev][detailv]vstack=inputs=2,"
            "drawbox=x=0:y=401:w=720:h=6:color=white@0.20:t=fill,"
            f"{grade},"
            f"fps={FPS},setsar=1,format=yuv420p"
        )
    else:
        vf = (
            f"scale=-2:{HEIGHT},"
            f"crop={WIDTH}:{HEIGHT}:x='(iw-ow)*{x:.4f}':y=0,"
            f"{grade},"
            f"fps={FPS},setsar=1,format=yuv420p"
        )
    run([
        "ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "warning",
        "-ss", f"{source_start:.3f}", "-i", str(source),
        "-an", "-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
        "-pix_fmt", "yuv420p", "-r", str(FPS), "-frames:v", str(frame_count), str(out),
    ])
    return out


def build_picture_lock(clips: list[Path]) -> Path:
    out = WORK / "picture_lock.mp4"
    cmd = ["ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "warning"]
    for clip in clips:
        cmd.extend(["-i", str(clip)])
    chains = []
    labels = []
    for index in range(len(clips)):
        label = f"v{index}"
        chains.append(f"[{index}:v]setpts=PTS-STARTPTS,fps={FPS},format=yuv420p[{label}]")
        labels.append(f"[{label}]")
    chains.append("".join(labels) + f"concat=n={len(clips)}:v=1:a=0[v]")
    cmd.extend([
        "-filter_complex", ";".join(chains), "-map", "[v]", "-an",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "15",
        "-pix_fmt", "yuv420p", "-r", str(FPS), str(out),
    ])
    run(cmd)
    return out


def render(picture_lock: Path, captions: Path, duration: float) -> Path:
    voice = INPUT / "voiceover.wav"
    final = RENDERS / "you_shouldnt_enter_this_japanese_classroom_alone.mp4"
    # Riser #2 naturally fits this hook best; tempo makes it end exactly at the hook cut.
    filter_complex = (
        "[0:v]subtitles=captions.ass[v];"
        "[1:a]aresample=48000,volume=1.0[vo];"
        "[2:a]atempo=1.1575,atrim=0:2.46,afade=t=out:st=2.12:d=0.34,volume=0.095[riser];"
        "[3:a]atrim=0:0.34,volume=0.050,adelay=2960|2960[w1];"
        "[3:a]atrim=0:0.34,volume=0.043,adelay=6040|6040[w2];"
        "[3:a]atrim=0:0.34,volume=0.043,adelay=11180|11180[w3];"
        "[3:a]atrim=0:0.34,volume=0.043,adelay=14380|14380[w4];"
        "[3:a]atrim=0:0.34,volume=0.043,adelay=19320|19320[w5];"
        "[4:a]atrim=0:0.65,afade=t=out:st=0.42:d=0.23,volume=0.050,adelay=18320|18320[hit];"
        "[vo][riser][w1][w2][w3][w4][w5][hit]amix=inputs=8:duration=longest:normalize=0,"
        f"alimiter=limit=0.95,atrim=0:{duration:.3f}[a]"
    )
    run([
        "ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "info", "-stats",
        "-i", str(picture_lock), "-i", str(voice),
        "-i", str(SFX / "hook_riser2.MP3"), "-i", str(SFX / "fast-swish.mp3"),
        "-i", str(SFX / "sudden-impact-1.mp3"),
        "-filter_complex", filter_complex, "-map", "[v]", "-map", "[a]",
        "-t", f"{duration:.3f}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        "-movflags", "+faststart", str(final),
    ], cwd=WORK)
    return final


def write_manifest(duration: float) -> None:
    script = (INPUT / "script.txt").read_text(encoding="utf-8").strip()
    manifest = {
        "title": "You Shouldn't Enter This Japanese Classroom Alone",
        "script": script,
        "duration_seconds": duration,
        "voiceover": {"provider": "Gemini TTS", "model": "pro", "voice": "Puck", "speed": 1.45},
        "background_music": False,
        "source_policy": "One real class, one school, one haunted-house build; no filler footage.",
        "sources": [
            {"url": "https://www.youtube.com/watch?v=hQWw5sYoXNM", "use": "one class building the haunted maze"},
            {"url": "https://www.youtube.com/watch?v=i_yTOtHRNSM", "use": "one continuous visitor walk-through and jump scare"},
        ],
        "shots": [
            {"name": name, "source_second": source_start, "start": start, "end": end, "crop_x": x,
             "layout": mode}
            for name, source_start, start, end, x, mode in SHOTS
        ],
    }
    (PROJECT / "source_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def main() -> None:
    for directory in (WORK / "clips", RENDERS, REVIEW):
        directory.mkdir(parents=True, exist_ok=True)
    if not SOURCE.exists():
        raise FileNotFoundError(SOURCE)
    words = json.loads((INPUT / "word_timings.json").read_text(encoding="utf-8"))
    duration = max(22.80, float(words[-1]["end"]) + 0.02)
    clips = [build_shot(index, shot) for index, shot in enumerate(SHOTS)]
    picture_lock = build_picture_lock(clips)
    captions = build_captions(words)
    write_manifest(duration)
    final = render(picture_lock, captions, duration)
    run([
        "ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "error", "-i", str(final),
        "-vf", "fps=1/1.9,scale=180:320,tile=6x2:padding=6:margin=6:color=0x080B09",
        "-frames:v", "1", str(REVIEW / "final_contact_sheet.jpg"),
    ])
    print(f"FINAL={final}")


if __name__ == "__main__":
    main()
