from __future__ import annotations

import json
import math
import shutil
import subprocess
from pathlib import Path


ROOT = Path(r"D:\data\AutoShortsClaude")
PROJECT = ROOT / "projects" / "japanese_school_festival_three_things"
INPUT = PROJECT / "input"
SOURCES = PROJECT / "sources"
WORK = PROJECT / "work"
RENDERS = PROJECT / "renders"
REVIEW = PROJECT / "review"
SFX = ROOT / "soundeffects"
FPS = 30
WIDTH = 720
HEIGHT = 1280
TOTAL_DURATION = 50.877


SHOTS = [
    # The hook is a compact preview of the three claims, not an unrelated viral clip.
    {"name": "hook_ghost", "source": SOURCES / "haunted" / "i_yTOtHRNSM_hq.mp4", "src": 148.0, "start": 0.0, "end": 1.35, "mode": "crop", "x": 0.02, "force": True},
    {"name": "hook_cafe", "source": SOURCES / "cafe" / "KZNA8H9-RnI.mp4", "src": 294.0, "start": 1.35, "end": 2.68, "mode": "fit"},
    {"name": "hook_fire", "source": SOURCES / "campfire" / "BaH_ms3MHAQ_hq.mp4", "src": 16.0, "start": 2.68, "end": 4.10, "mode": "fit"},
    # A held construction shot bridges the mandatory hook/body pause without a black frame.
    {"name": "haunted_intro", "source": SOURCES / "haunted" / "hQWw5sYoXNM_hq.mp4", "src": 120.0, "start": 4.10, "end": 7.25, "mode": "fit", "crop_bottom": 0.80, "force": True},
    {"name": "haunted_cardboard", "source": SOURCES / "haunted" / "hQWw5sYoXNM_hq.mp4", "src": 151.0, "start": 7.25, "end": 10.05, "mode": "fit", "crop_bottom": 0.80, "force": True},
    {"name": "haunted_build", "source": SOURCES / "haunted" / "hQWw5sYoXNM_hq.mp4", "src": 92.0, "start": 10.05, "end": 12.95, "mode": "fit", "crop_bottom": 0.80, "force": True},
    {"name": "haunted_props", "source": SOURCES / "haunted" / "hQWw5sYoXNM_hq.mp4", "src": 8.0, "start": 12.95, "end": 14.75, "mode": "fit", "crop_bottom": 0.80, "force": True},
    {"name": "haunted_ghost", "source": SOURCES / "haunted" / "i_yTOtHRNSM_hq.mp4", "src": 147.1, "start": 14.75, "end": 16.45, "mode": "crop", "x": 0.02, "force": True},
    {"name": "haunted_hallway", "source": SOURCES / "haunted" / "i_yTOtHRNSM_hq.mp4", "src": 104.0, "start": 16.45, "end": 18.40, "mode": "crop", "x": 0.48},
    {"name": "cafe_overview", "source": SOURCES / "cafe" / "KZNA8H9-RnI.mp4", "src": 15.0, "start": 18.40, "end": 21.25, "mode": "fit"},
    {"name": "cafe_coffee", "source": SOURCES / "cafe" / "KZNA8H9-RnI.mp4", "src": 228.0, "start": 21.25, "end": 23.70, "mode": "fit"},
    {"name": "cafe_cosplay", "source": SOURCES / "cafe" / "KZNA8H9-RnI.mp4", "src": 294.0, "start": 23.70, "end": 26.85, "mode": "fit"},
    {"name": "cafe_desks", "source": SOURCES / "cafe" / "KZNA8H9-RnI.mp4", "src": 258.0, "start": 26.85, "end": 29.30, "mode": "fit"},
    {"name": "cafe_maid", "source": SOURCES / "cafe" / "KZNA8H9-RnI.mp4", "src": 298.0, "start": 29.30, "end": 32.32, "mode": "fit"},
    {"name": "cafe_pancakes", "source": SOURCES / "cafe" / "KZNA8H9-RnI.mp4", "src": 329.0, "start": 32.32, "end": 35.68, "mode": "fit"},
    {"name": "cafe_classmates", "source": SOURCES / "cafe" / "KZNA8H9-RnI.mp4", "src": 234.0, "start": 35.68, "end": 38.06, "mode": "fit"},
    {"name": "dance_reveal", "source": SOURCES / "campfire" / "rQ_186SjHic_hq.mp4", "src": 96.0, "start": 38.06, "end": 40.06, "mode": "fit"},
    {"name": "bonfire_wide", "source": SOURCES / "campfire" / "BaH_ms3MHAQ_hq.mp4", "src": 16.0, "start": 40.06, "end": 42.55, "mode": "fit"},
    {"name": "bonfire_circle", "source": SOURCES / "campfire" / "BaH_ms3MHAQ_hq.mp4", "src": 27.0, "start": 42.55, "end": 44.26, "mode": "fit"},
    {"name": "students_hands", "source": SOURCES / "campfire" / "y4X2rjIdzyM_hq.mp4", "src": 24.0, "start": 44.26, "end": 47.72, "mode": "fit"},
    {"name": "couple_dance", "source": SOURCES / "campfire" / "rQ_186SjHic_hq.mp4", "src": 104.0, "start": 47.72, "end": TOTAL_DURATION, "mode": "fit"},
]

# The generated body take contained 0.4 s of additional leading silence after the
# deliberately inserted 0.5 s hook/body pause. That silence is removed in the
# final voice file; every downstream body cut and caption follows the same shift.
BODY_SHIFT = 0.4
for shot_index, shot in enumerate(SHOTS):
    if shot_index >= 3:
        shot["end"] = float(shot["end"]) - BODY_SHIFT
        if shot_index > 3:
            shot["start"] = float(shot["start"]) - BODY_SHIFT
TOTAL_DURATION -= BODY_SHIFT


def run(cmd: list[str], cwd: Path | None = None) -> None:
    print(" ".join(str(x) for x in cmd), flush=True)
    subprocess.run(cmd, cwd=str(cwd) if cwd else None, check=True)


def ass_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = seconds % 60
    return f"{hours}:{minutes:02d}:{secs:05.2f}"


def ass_escape(text: str) -> str:
    return text.replace("\\", r"\\").replace("{", r"\{").replace("}", r"\}")


def make_caption_groups(words: list[dict]) -> list[list[dict]]:
    groups: list[list[dict]] = []
    current: list[dict] = []
    chars = 0
    for word in words:
        token = str(word["word"])
        gap = float(word["start"]) - float(current[-1]["end"]) if current else 0.0
        boundary = bool(current) and (gap > 0.52 or len(current) >= 4 or chars + len(token) + 1 > 18)
        if boundary:
            groups.append(current)
            current = []
            chars = 0
        current.append(word)
        chars += len(token) + (1 if chars else 0)
        if token.endswith((".", "?", "!")):
            groups.append(current)
            current = []
            chars = 0
    if current:
        groups.append(current)
    return groups


def build_ass() -> Path:
    words = json.loads((INPUT / "word_timings.json").read_text(encoding="utf-8"))
    for word in words:
        if float(word["start"]) >= 4.80:
            word["start"] = float(word["start"]) - BODY_SHIFT
            word["end"] = float(word["end"]) - BODY_SHIFT
    groups = make_caption_groups(words)
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
        "Style: Caption,Arial,54,&H00FFFFFF,&H00FFFFFF,&H00101010,&H50000000,-1,0,0,0,100,100,0,0,1,7,2,2,42,42,218,1",
        "Style: Section,Arial,42,&H00FFFFFF,&H00FFFFFF,&H00101010,&H50000000,-1,0,0,0,100,100,2,0,1,5,1,8,36,36,92,1",
        "Style: Hook,Arial,44,&H0068FC8D,&H0068FC8D,&H00101010,&H50000000,-1,0,0,0,100,100,2,0,1,6,2,8,36,36,90,1",
        "",
        "[Events]",
        "Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text",
    ]
    lines.append(f"Dialogue: 1,{ass_time(0.0)},{ass_time(3.98)},Hook,,0,0,0,,3 CRAZY SCHOOL FESTIVAL\\NTRADITIONS")
    sections = [
        (4.48, 6.65, "01  HAUNTED CLASSROOMS"),
        (18.00, 20.52, "02  CLASSROOM CAFES"),
        (37.66, 39.28, "03  CAMPFIRE COUPLES"),
    ]
    for start, end, label in sections:
        lines.append(f"Dialogue: 1,{ass_time(start)},{ass_time(end)},Section,,0,0,0,,{label}")

    for group in groups:
        for index, word in enumerate(group):
            start = float(word["start"])
            if index + 1 < len(group):
                end = float(group[index + 1]["start"])
            else:
                end = float(word["end"]) + 0.10
            parts = []
            for j, item in enumerate(group):
                token = ass_escape(str(item["word"]).upper())
                if j == index:
                    parts.append(r"{\c&H0068FC8D&}" + token + r"{\c&H00FFFFFF&}")
                else:
                    parts.append(token)
            text = " ".join(parts)
            lines.append(f"Dialogue: 2,{ass_time(start)},{ass_time(end)},Caption,,0,0,0,,{text}")
    path = WORK / "captions.ass"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
    return path


def build_shot(shot: dict, output: Path) -> None:
    duration = float(shot["end"]) - float(shot["start"])
    if shot["mode"] == "crop":
        pos = min(1.0, max(0.0, float(shot.get("x", 0.5))))
        vf = (
            f"scale=-2:{HEIGHT},"
            f"crop={WIDTH}:{HEIGHT}:x='(iw-ow)*{pos:.3f}':y=0,"
            "eq=contrast=1.035:saturation=1.08,"
            f"fps={FPS},setsar=1,format=yuv420p"
        )
    else:
        crop_bottom = float(shot.get("crop_bottom", 1.0))
        foreground = "[fg]"
        if crop_bottom < 0.999:
            foreground = f"[fg]crop=iw:ih*{crop_bottom:.3f}:0:0,"
        vf = (
            f"split=2[bg][fg];"
            f"[bg]scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,"
            f"crop={WIDTH}:{HEIGHT},gblur=sigma=28,eq=brightness=-0.22:saturation=1.05[bgv];"
            f"{foreground}scale={WIDTH}:-2,eq=contrast=1.035:saturation=1.08[fgv];"
            f"[bgv][fgv]overlay=(W-w)/2:(H-h)/2,"
            f"fps={FPS},setsar=1,format=yuv420p"
        )
    run([
        "ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "warning",
        "-ss", f"{float(shot['src']):.3f}", "-i", str(shot["source"]),
        "-t", f"{duration:.3f}", "-an", "-vf", vf,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "19",
        "-pix_fmt", "yuv420p", "-r", str(FPS), str(output),
    ])


def create_manifest() -> None:
    manifest = {
        "title": "Three Crazy Things Japanese Students Do at School Festivals",
        "duration_seconds": TOTAL_DURATION,
        "background_music": False,
        "voiceover": {"provider": "Gemini TTS", "voice": "Laomedeia", "only_api_used": True},
        "sources": [
            {"url": "https://www.youtube.com/watch?v=hQWw5sYoXNM", "use": "students building a real classroom haunted house and cardboard maze"},
            {"url": "https://www.youtube.com/watch?v=i_yTOtHRNSM", "use": "completed dark school-festival haunted house and costumed ghost"},
            {"url": "https://www.youtube.com/watch?v=KZNA8H9-RnI", "use": "decorated classroom cafe, costumes, desks, food service and pancake batter"},
            {"url": "https://www.youtube.com/watch?v=BaH_ms3MHAQ", "use": "large night bonfire with people dancing around it"},
            {"url": "https://www.youtube.com/watch?v=y4X2rjIdzyM", "use": "Japanese students holding hands in an outdoor folk dance"},
            {"url": "https://www.youtube.com/watch?v=rQ_186SjHic", "use": "paired Japanese folk dance and hand-holding"},
        ],
        "shots": [{k: (str(v) if isinstance(v, Path) else v) for k, v in s.items()} for s in SHOTS],
    }
    (PROJECT / "source_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    WORK.mkdir(parents=True, exist_ok=True)
    RENDERS.mkdir(parents=True, exist_ok=True)
    REVIEW.mkdir(parents=True, exist_ok=True)
    clips_dir = WORK / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)

    for index, shot in enumerate(SHOTS):
        if not Path(shot["source"]).exists():
            raise FileNotFoundError(shot["source"])
        output = clips_dir / f"{index:02d}_{shot['name']}.mp4"
        if shot.get("force") or "_hq" in Path(shot["source"]).stem or not output.exists() or output.stat().st_size < 10_000:
            build_shot(shot, output)

    concat_file = WORK / "concat.txt"
    concat_lines = []
    for index, shot in enumerate(SHOTS):
        clip_path = clips_dir / f"{index:02d}_{shot['name']}.mp4"
        concat_lines.append(f"file '{clip_path.as_posix()}'")
    concat_file.write_text("\n".join(concat_lines) + "\n", encoding="utf-8")
    base_video = WORK / "picture_lock.mp4"
    # Each downloaded source carries slightly different color metadata. The concat
    # demuxer lets that metadata reconfigure a downstream subtitle filter mid-run,
    # which can reset timestamps. Normalize each already-cut shot in its own input
    # chain, then concatenate the normalized streams into one continuous timeline.
    concat_cmd = ["ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "warning"]
    clip_paths = [clips_dir / f"{i:02d}_{s['name']}.mp4" for i, s in enumerate(SHOTS)]
    for clip_path in clip_paths:
        concat_cmd.extend(["-i", str(clip_path)])
    chains = []
    labels = []
    for index in range(len(clip_paths)):
        label = f"v{index}"
        chains.append(
            f"[{index}:v]setpts=PTS-STARTPTS,fps={FPS},format=yuv420p,"
            f"setparams=range=tv:color_primaries=bt709:color_trc=bt709:colorspace=bt709[{label}]"
        )
        labels.append(f"[{label}]")
    chains.append("".join(labels) + f"concat=n={len(clip_paths)}:v=1:a=0[v]")
    concat_cmd.extend([
        "-filter_complex", ";".join(chains), "-map", "[v]", "-an",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "16", "-pix_fmt", "yuv420p",
        "-r", str(FPS), str(base_video),
    ])
    run(concat_cmd)

    build_ass()
    create_manifest()

    final = RENDERS / "japanese_school_festival_three_crazy_things.mp4"
    cmd = [
        "ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "info", "-stats",
        "-i", str(base_video),
        "-i", str(INPUT / "voiceover_exact_pause.wav"),
        "-i", str(SFX / "hook_riser4.MP3"),
        "-i", str(SFX / "fast-swish.mp3"),
        "-i", str(SFX / "dramatic-boomer.mp3"),
        "-i", str(SFX / "fast-swish.mp3"),
        "-i", str(SFX / "magic-reveal-audio.mp3"),
        "-i", str(SFX / "fast-swish.mp3"),
        "-i", str(SFX / "love-impact.mp3"),
        "-filter_complex",
        "[0:v]subtitles=captions.ass[v];"
        "[1:a]aresample=48000,volume=1.0[vo];"
        "[2:a]atempo=0.7213,atrim=0:3.98,afade=t=out:st=3.55:d=0.43,volume=0.13[s0];"
        "[3:a]atrim=0:0.42,volume=0.12,adelay=4480|4480[s1];"
        "[4:a]atrim=0:1.15,volume=0.08,adelay=7800|7800[s2];"
        "[5:a]atrim=0:0.42,volume=0.12,adelay=18000|18000[s3];"
        "[6:a]atrim=0:0.80,volume=0.09,adelay=24580|24580[s4];"
        "[7:a]atrim=0:0.42,volume=0.11,adelay=37660|37660[s5];"
        "[8:a]atrim=0:1.20,volume=0.08,adelay=48900|48900[s6];"
        f"[vo][s0][s1][s2][s3][s4][s5][s6]amix=inputs=8:duration=longest:normalize=0,alimiter=limit=0.95,atrim=0:{TOTAL_DURATION:.3f}[a]",
        "-map", "[v]", "-map", "[a]",
        "-t", f"{TOTAL_DURATION:.3f}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        "-movflags", "+faststart", str(final),
    ]
    run(cmd, cwd=WORK)

    contact = REVIEW / "final_contact_sheet.jpg"
    run([
        "ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "error", "-i", str(final),
        "-vf", "fps=1/4.5,scale=216:384,tile=4x3:padding=8:margin=8:color=0x0A0D0B",
        "-frames:v", "1", str(contact),
    ])
    print(f"FINAL={final}")


if __name__ == "__main__":
    main()
