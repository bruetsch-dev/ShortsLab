from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(r"D:\data\AutoShortsClaude")
sys.path.insert(0, str(ROOT / "scripts"))
import build_haunted_classroom_short as base  # noqa: E402


PROJECT = ROOT / "projects" / "japanese_classroom_cafe"
INPUT = PROJECT / "input"
WORK = PROJECT / "work"
RENDERS = PROJECT / "renders"
REVIEW = PROJECT / "review"
SOURCE = (
    ROOT / "projects" / "japanese_school_festival_three_things" / "sources" /
    "cafe" / "KZNA8H9-RnI.mp4"
)

# One source, one classroom, one event. Every horizontal shot uses the context+detail layout
# so the complete action remains readable instead of being blindly center-cropped to 9:16.
SHOTS = [
    ("hook_costume_cafe", 260.0, 0.00, 1.15, 0.50, "context"),
    ("hook_students_working", 264.0, 1.15, 2.23, 0.52, "context"),
    # Mandatory hook/body cut; the body image begins during the exact 0.50s speech pause.
    ("festival_room_reveal", 252.0, 2.23, 3.89, 0.50, "context"),
    ("classroom_restaurant", 304.0, 3.89, 6.29, 0.50, "context"),
    ("desks_become_counters", 300.0, 6.29, 7.57, 0.52, "context"),
    ("wall_decorations", 4.0, 7.57, 8.83, 0.54, "context"),
    ("chalkboard_menu", 236.0, 8.83, 10.03, 0.60, "context"),
    ("mixing_orders", 328.0, 10.03, 11.93, 0.50, "context"),
    ("maid_server", 232.0, 11.93, 13.07, 0.53, "context"),
    ("suited_server", 402.0, 13.07, 14.53, 0.48, "context"),
    ("princess_and_maid", 408.0, 14.53, 16.73, 0.52, "context"),
    ("student_kitchen", 332.0, 16.73, 18.45, 0.52, "context"),
    ("student_customers", 276.0, 18.45, 20.19, 0.50, "context"),
    ("whole_class_runs_cafe", 268.0, 20.19, 21.85, 0.52, "context"),
]


def build_captions(words: list[dict]) -> Path:
    lines = [
        "[Script Info]", "ScriptType: v4.00+", "PlayResX: 720", "PlayResY: 1280",
        "ScaledBorderAndShadow: yes", "WrapStyle: 2", "", "[V4+ Styles]",
        "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding",
        "Style: Word,Arial Black,67,&H00FFFFFF,&H00FFFFFF,&H00101010,&H70000000,-1,0,0,0,100,100,0,0,1,7,2,5,35,35,0,1",
        "Style: Arrow,Segoe UI Symbol,112,&H002D39F2,&H002D39F2,&H00FFFFFF,&H60000000,-1,0,0,0,100,100,0,0,1,6,2,5,0,0,0,1",
        "", "[Events]",
        "Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text",
    ]
    emphasis = {"NO", "EMPLOYEES", "RESTAURANT", "MENUS", "MAIDS", "PRINCESS", "STUDENT", "THEMSELVES"}
    for index, word in enumerate(words):
        start = float(word["start"])
        end = float(words[index + 1]["start"]) if index + 1 < len(words) else float(word["end"]) + 0.08
        token = base.clean_word(str(word["word"]))
        if not token:
            continue
        color = r"{\c&H002D39F2&}" if token in emphasis else ""
        text = rf"{{\fscx88\fscy88\t(0,85,\fscx100\fscy100)}}{color}{token}"
        lines.append(f"Dialogue: 3,{base.ass_time(start)},{base.ass_time(end)},Word,,0,0,0,,{text}")

    for start, end, x, y, rotation in [
        (0.08, 0.95, 500, 335, 140),
        (9.00, 9.85, 515, 320, 145),
        (12.00, 12.85, 500, 570, 138),
    ]:
        text = rf"{{\pos({x},{y})\frz{rotation}\fscx45\fscy45\t(0,110,\fscx100\fscy100)}}➜"
        lines.append(f"Dialogue: 4,{base.ass_time(start)},{base.ass_time(end)},Arrow,,0,0,0,,{text}")
    out = WORK / "captions.ass"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
    return out


def render(picture_lock: Path, captions: Path, duration: float) -> Path:
    final = RENDERS / "this_japanese_cafe_has_no_employees.mp4"
    fc = (
        "[0:v]subtitles=captions.ass[v];"
        "[1:a]aresample=48000,volume=1.0[vo];"
        "[2:a]atempo=1.1575,atrim=0:2.23,afade=t=out:st=1.94:d=0.29,volume=0.085[riser];"
        "[3:a]atrim=0:0.30,volume=0.038,adelay=2230|2230[w1];"
        "[3:a]atrim=0:0.30,volume=0.034,adelay=6290|6290[w2];"
        "[3:a]atrim=0:0.30,volume=0.034,adelay=10030|10030[w3];"
        "[3:a]atrim=0:0.30,volume=0.034,adelay=14530|14530[w4];"
        "[4:a]atrim=0:0.42,afade=t=out:st=0.25:d=0.17,volume=0.040,adelay=11930|11930[pop];"
        "[vo][riser][w1][w2][w3][w4][pop]amix=inputs=7:duration=longest:normalize=0,"
        f"alimiter=limit=0.95,atrim=0:{duration:.3f}[a]"
    )
    base.run([
        "ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "warning",
        "-i", str(picture_lock), "-i", str(INPUT / "voiceover.wav"),
        "-i", str(ROOT / "soundeffects" / "hook_riser2.MP3"),
        "-i", str(ROOT / "soundeffects" / "fast-swish.mp3"),
        "-i", str(ROOT / "soundeffects" / "pop-ding.mp3"),
        "-filter_complex", fc, "-map", "[v]", "-map", "[a]", "-t", f"{duration:.3f}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-movflags", "+faststart", str(final),
    ], cwd=WORK)
    return final


def main() -> None:
    for directory in (WORK / "clips", RENDERS, REVIEW):
        directory.mkdir(parents=True, exist_ok=True)
    words = json.loads((INPUT / "word_timings.json").read_text(encoding="utf-8"))
    duration = max(21.85, float(words[-1]["end"]) + 0.06)
    base.PROJECT, base.INPUT, base.WORK = PROJECT, INPUT, WORK
    base.RENDERS, base.REVIEW = RENDERS, REVIEW
    base.SOURCE = base.WALK_SOURCE = SOURCE
    clips = [base.build_shot(index, shot) for index, shot in enumerate(SHOTS)]
    picture_lock = base.build_picture_lock(clips)
    captions = build_captions(words)
    final = render(picture_lock, captions, duration)
    manifest = {
        "title": "This Japanese Cafe Has No Employees", "script": (INPUT / "script.txt").read_text(encoding="utf-8").strip(),
        "duration_seconds": duration, "clip_short_format": "mini_story", "script_token_limit": 130,
        "estimated_script_tokens": 94, "background_music": False,
        "source_policy": "One continuous school-festival cosplay cafe source; no unrelated filler.",
        "sources": [{"url": "https://www.youtube.com/watch?v=KZNA8H9-RnI", "use": "the same classroom cafe event throughout"}],
        "shots": [{"name": n, "source_second": ss, "start": s, "end": e, "crop_x": x, "layout": m}
                  for n, ss, s, e, x, m in SHOTS],
    }
    (PROJECT / "source_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    base.run(["ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "error", "-i", str(final),
              "-vf", "fps=1/1.82,scale=180:320,tile=6x2:padding=6:margin=6:color=0x080B09",
              "-frames:v", "1", str(REVIEW / "final_contact_sheet.jpg")])
    print(f"FINAL={final}")


if __name__ == "__main__":
    main()
