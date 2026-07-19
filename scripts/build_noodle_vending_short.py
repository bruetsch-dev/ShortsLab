from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(r"D:\data\AutoShortsClaude")
sys.path.insert(0, str(ROOT))

import agent_core  # noqa: E402
import voice_align  # noqa: E402


PROJECT = ROOT / "projects" / "japanese_noodle_vending_machine"
INPUT = PROJECT / "input"
WORK = PROJECT / "work"
RENDERS = PROJECT / "renders"
REVIEW = PROJECT / "review"
SOURCE = (
    ROOT / "projects" / "japanese_vending_machines_have_three_secret_features"
    / "seedance 2.0" / "_v2_proxies" / "proxy_tiktok_6949868800447663362.mp4"
)
SFX = ROOT / "soundeffects"
SCRIPT = (
    "This Japanese vending machine looks abandoned—but it still cooks lunch. "
    "Drop in a coin, choose soba, and the rusted machine heats an entire bowl behind that tiny "
    "metal door. Seconds later, it slides out steaming noodles, broth and tempura. No kitchen. "
    "No cashier. Just one ancient machine still serving hot food."
)
HOOK = "This Japanese vending machine looks abandoned—but it still cooks lunch."
FPS = 30


def run(cmd: list[str], cwd: Path | None = None) -> None:
    print(" ".join(str(part) for part in cmd), flush=True)
    subprocess.run(cmd, cwd=str(cwd) if cwd else None, check=True)


def probe_duration(path: Path) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    )
    return float(result.stdout.strip())


def ass_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    return f"{int(seconds // 3600)}:{int(seconds % 3600 // 60):02d}:{seconds % 60:05.2f}"


def clean_word(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9']", "", value).upper()


def estimated_words(script: str, duration: float, hook_end: float) -> list[dict]:
    tokens = re.findall(r"[A-Za-z0-9']+", script)
    hook_count = len(re.findall(r"[A-Za-z0-9']+", HOOK))

    def spread(words: list[str], start: float, end: float) -> list[dict]:
        weights = [max(1, len(word)) for word in words]
        total = sum(weights) or 1
        cursor, rows = start, []
        for word, weight in zip(words, weights):
            length = (end - start) * weight / total
            rows.append({"word": word, "start": round(cursor, 3), "end": round(cursor + length, 3)})
            cursor += length
        return rows

    return spread(tokens[:hook_count], 0.0, hook_end) + spread(tokens[hook_count:], hook_end + 0.5, duration)


def generate_voice() -> tuple[Path, list[dict], float, float]:
    INPUT.mkdir(parents=True, exist_ok=True)
    (INPUT / "script.txt").write_text(SCRIPT, encoding="utf-8")
    form = {
        "clip_source": "scrape",
        "hook_text": HOOK,
        "speaker_name": "Male narrator",
        "tts_voice": "Puck",
        "tts_model": "pro",
        "voice_speed": 1.25,
    }
    voice = Path(agent_core.generate_project_voiceover(
        SCRIPT, PROJECT, form, status_cb=lambda message: print(message, flush=True)
    ))
    duration = probe_duration(voice)
    hook_audio = Path(form.get("_hook_audio_path") or INPUT / "hook.wav")
    body_audio = Path(form.get("_body_audio_path") or INPUT / "body.wav")
    hook_end = probe_duration(hook_audio)
    # Keep the format's promised 20-25 second range without making another API request. Slow the
    # two already-generated speech pieces equally, then rejoin them with a fresh exact 0.500s gap.
    if duration < 20.0 and body_audio.exists():
        target_duration = 20.5
        tempo = (probe_duration(hook_audio) + probe_duration(body_audio)) / (target_duration - 0.5)
        slow_hook = INPUT / "hook_mini_story.wav"
        slow_body = INPUT / "body_mini_story.wav"
        run(["ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "error", "-i", str(hook_audio),
             "-filter:a", f"atempo={tempo:.6f}", str(slow_hook)])
        run(["ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "error", "-i", str(body_audio),
             "-filter:a", f"atempo={tempo:.6f}", str(slow_body)])
        joined = agent_core.pipeline.concat_audio_with_pause(
            slow_hook, slow_body, INPUT / "voiceover_mini_story.wav", pause_s=0.5, ffmpeg="ffmpeg")
        if joined:
            voice = Path(joined)
            hook_audio = slow_hook
            duration = probe_duration(voice)
            hook_end = probe_duration(hook_audio)
    words = []
    if voice_align.available():
        words = voice_align.word_timeline(str(voice), script_text=SCRIPT, speed=1.0,
                                          status_cb=lambda message: print(message, flush=True))
    if not words:
        words = estimated_words(SCRIPT, duration, hook_end)
    (INPUT / "word_timings.json").write_text(json.dumps(words, indent=2), encoding="utf-8")
    return voice, words, duration, hook_end


def make_shots(duration: float, hook_end: float) -> list[tuple[str, float, float, float]]:
    body = hook_end + 0.5
    remaining = max(1.0, duration - body)
    # The hook reveals the payoff immediately. After the mandatory pause, the edit returns to
    # the beginning and follows the same original phone video in chronological order.
    beats = [
        ("hook_bowl_emerges", 24.15, 0.00, hook_end * 0.54),
        ("hook_finished_bowl", 27.10, hook_end * 0.54, hook_end),
        ("machine_reveal", 0.00, body, body + remaining * 0.14),
        ("coin_and_menu", 3.10, body + remaining * 0.14, body + remaining * 0.29),
        ("choose_soba", 6.15, body + remaining * 0.29, body + remaining * 0.43),
        ("machine_wait", 9.35, body + remaining * 0.43, body + remaining * 0.58),
        ("counter_ticks", 13.20, body + remaining * 0.58, body + remaining * 0.71),
        ("door_opens", 20.80, body + remaining * 0.71, body + remaining * 0.84),
        ("bowl_dispenses", 24.25, body + remaining * 0.84, body + remaining * 0.93),
        ("hot_food_payoff", 27.10, body + remaining * 0.93, duration),
    ]
    return [(name, source_at, round(start, 3), round(end, 3)) for name, source_at, start, end in beats]


def build_picture(shots: list[tuple[str, float, float, float]]) -> Path:
    clips = []
    for index, (name, source_at, start, end) in enumerate(shots):
        frames = max(1, round(end * FPS) - round(start * FPS))
        clip = WORK / "clips" / f"{index:02d}_{name}.mp4"
        run([
            "ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "warning",
            "-ss", f"{source_at:.3f}", "-i", str(SOURCE), "-an",
            "-vf", "scale=720:1280,eq=contrast=1.035:saturation=1.05,fps=30,setsar=1,format=yuv420p",
            "-frames:v", str(frames), "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
            "-pix_fmt", "yuv420p", str(clip),
        ])
        clips.append(clip)
    picture = WORK / "picture_lock.mp4"
    cmd = ["ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "warning"]
    for clip in clips:
        cmd += ["-i", str(clip)]
    labels = "".join(f"[{index}:v]setpts=PTS-STARTPTS[v{index}];" for index in range(len(clips)))
    concat = "".join(f"[v{index}]" for index in range(len(clips)))
    cmd += ["-filter_complex", labels + concat + f"concat=n={len(clips)}:v=1:a=0[v]",
            "-map", "[v]", "-an", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "15",
            "-pix_fmt", "yuv420p", str(picture)]
    run(cmd)
    return picture


def build_captions(words: list[dict], hook_end: float) -> Path:
    lines = [
        "[Script Info]", "ScriptType: v4.00+", "PlayResX: 720", "PlayResY: 1280",
        "ScaledBorderAndShadow: yes", "WrapStyle: 2", "", "[V4+ Styles]",
        "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding",
        "Style: Word,Arial Black,67,&H00FFFFFF,&H00FFFFFF,&H00101010,&H70000000,-1,0,0,0,100,100,0,0,1,7,2,5,35,35,0,1",
        "Style: Arrow,Segoe UI Symbol,110,&H002D39F2,&H002D39F2,&H00FFFFFF,&H60000000,-1,0,0,0,100,100,0,0,1,6,2,5,0,0,0,1",
        "", "[Events]", "Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text",
    ]
    emphasis = {"ABANDONED", "COOKS", "SOBA", "SECONDS", "STEAMING", "NO", "ANCIENT", "HOT"}
    for index, item in enumerate(words):
        token = clean_word(str(item.get("word") or ""))
        if not token:
            continue
        start = float(item.get("start") or 0)
        end = float(words[index + 1].get("start") or start + 0.15) if index + 1 < len(words) else float(item.get("end") or start + 0.2)
        color = r"{\c&H002D39F2&}" if token in emphasis else ""
        text = rf"{{\fscx88\fscy88\t(0,85,\fscx100\fscy100)}}{color}{token}"
        lines.append(f"Dialogue: 3,{ass_time(start)},{ass_time(end)},Word,,0,0,0,,{text}")
    arrow_start = max(hook_end + 0.5, float(words[-1]["end"]) - 2.2)
    lines.append(
        f"Dialogue: 4,{ass_time(arrow_start)},{ass_time(arrow_start + 0.85)},Arrow,,0,0,0,,"
        r"{\pos(510,720)\frz140\fscx45\fscy45\t(0,110,\fscx100\fscy100)}➜"
    )
    out = WORK / "captions.ass"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
    return out


def render(picture: Path, voice: Path, captions: Path, duration: float, hook_end: float) -> Path:
    final = RENDERS / "this_abandoned_machine_still_cooks_lunch.mp4"
    cut_ms = int(round((hook_end + 0.5) * 1000))
    reveal_ms = int(round(max(hook_end + 0.5, duration - 3.0) * 1000))
    fc = (
        "[0:v]subtitles=captions.ass[v];"
        "[1:a]aresample=48000,volume=1.0[vo];"
        f"[2:a]atrim=0:{hook_end:.3f},afade=t=out:st={max(0.0, hook_end - .28):.3f}:d=0.28,volume=0.075[riser];"
        f"[3:a]atrim=0:0.28,volume=0.030,adelay={cut_ms}|{cut_ms}[cut];"
        f"[4:a]atrim=0:0.38,afade=t=out:st=0.22:d=0.16,volume=0.036,adelay={reveal_ms}|{reveal_ms}[hit];"
        f"[vo][riser][cut][hit]amix=inputs=4:duration=longest:normalize=0,alimiter=limit=.95,atrim=0:{duration:.3f}[a]"
    )
    run([
        "ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "warning",
        "-i", str(picture), "-i", str(voice), "-i", str(SFX / "hook_riser2.MP3"),
        "-i", str(SFX / "fast-swish.mp3"), "-i", str(SFX / "sudden-impact-1.mp3"),
        "-filter_complex", fc, "-map", "[v]", "-map", "[a]", "-t", f"{duration:.3f}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-movflags", "+faststart", str(final),
    ], cwd=WORK)
    return final


def main() -> None:
    for directory in (INPUT, WORK / "clips", RENDERS, REVIEW):
        directory.mkdir(parents=True, exist_ok=True)
    if not SOURCE.exists():
        raise FileNotFoundError(SOURCE)
    voice, words, duration, hook_end = generate_voice()
    shots = make_shots(duration, hook_end)
    picture = build_picture(shots)
    captions = build_captions(words, hook_end)
    final = render(picture, voice, captions, duration, hook_end)
    manifest = {
        "title": "This Abandoned Machine Still Cooks Lunch",
        "script": SCRIPT,
        "clip_short_format": "mini_story",
        "script_token_limit": 130,
        "estimated_script_tokens": len(SCRIPT.split()) * 4 // 3,
        "duration_seconds": round(duration, 3),
        "background_music": False,
        "source_policy": "One original native-vertical TikTok, one machine and one continuous action; no filler.",
        "source": {"platform": "tiktok", "id": "6949868800447663362", "width": 720, "height": 1280,
                   "native_9_16": True, "path": str(SOURCE)},
        "shots": [{"name": name, "source_second": source_at, "start": start, "end": end}
                  for name, source_at, start, end in shots],
    }
    (PROJECT / "source_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    run(["ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "error", "-i", str(final),
         "-vf", "fps=8/24,scale=180:320,tile=4x2:padding=6:margin=6:color=0x080B09",
         "-frames:v", "1", str(REVIEW / "final_contact_sheet.jpg")])
    print(f"FINAL={final}")


if __name__ == "__main__":
    main()
