from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(r"D:\data\AutoShortsClaude")
DESKTOP = Path(r"C:\Users\USCSt\Desktop\Japan_V3_Videos")
SFX = ROOT / "soundeffects"
SPEED = 1.1

PROJECTS = [
    "extreme_takoyaki_speed_v3",
    "wagashi_art_v3",
    "japan_vending_machines_v3",
    "japan_tattoo_ban_v3",
    "japan_rent_a_family_v3",
    "japan_jouhatsu_v3",
    "strictest_chef_japan_v3",
]

TRANSITIONS = [
    (SFX / "shorts_ready" / "analog_transitions" / "soft_slide_whoosh_01.wav", 0.16),
    (SFX / "bubble-poping.mp3", 0.14),
    (SFX / "mouse-click-sound.mp3", 0.13),
    (SFX / "whoosh-sfx.mp3", 0.16),
    (SFX / "fast-swish.mp3", 0.16),
]


def probe_duration(path: Path) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True,
        text=True,
        check=True,
    )
    return float(result.stdout.strip())


def parse_timestamp(value: str) -> float:
    parts = value.split(":")
    if len(parts) == 2:
        return float(parts[0]) * 60.0 + float(parts[1])
    return float(value)


def hook_end(plan: dict, project_dir: Path) -> float:
    source_project = ROOT / "projects" / str(plan.get("source_project") or "")
    timed = source_project / "input" / "audio_timed_script.txt"
    if timed.exists():
        first = next((line.strip() for line in timed.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip()), "")
        match = re.match(r"\S+-(\S+)", first)
        if match:
            return max(1.2, parse_timestamp(match.group(1)) / SPEED)
    shots = plan.get("shots") or []
    if len(shots) > 1:
        return max(1.2, float(shots[1]["end"]) / SPEED)
    return 3.2


def find_render(project_dir: Path, slug: str) -> Path:
    preferred = project_dir / "renders" / f"{slug}_auto_short.mp4"
    if preferred.exists():
        return preferred
    candidates = sorted(
        (p for p in (project_dir / "renders").glob("*.mp4") if "1.1x" not in p.stem),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        raise FileNotFoundError(f"No base render for {slug}")
    return candidates[0]


def render_project(slug: str) -> Path:
    project_dir = ROOT / "projects" / slug
    plan = json.loads((project_dir / "v3_plan.json").read_text(encoding="utf-8"))
    source = find_render(project_dir, slug)
    out = project_dir / "renders" / f"{slug}_auto_short_1.1x_sfx.mp4"
    tmp = out.with_name(out.stem + "_tmp.mp4")

    cut_times = sorted({round(float(shot["start"]) / SPEED, 3) for shot in (plan.get("shots") or []) if float(shot["start"]) > 0})
    target_hook = hook_end(plan, project_dir)
    risers = [SFX / f"hook_riser{i}.MP3" for i in range(1, 5)]
    riser = min(risers, key=lambda p: abs(probe_duration(p) - target_hook))
    tempo = probe_duration(riser) / target_hook
    tempo = min(2.0, max(0.5, tempo))

    cmd = ["ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "warning", "-i", str(source), "-i", str(riser)]
    chosen = []
    for index, cut in enumerate(cut_times):
        effect, volume = TRANSITIONS[index % len(TRANSITIONS)]
        cmd.extend(["-i", str(effect)])
        chosen.append((index + 2, cut, volume))

    filters = [
        f"[0:v]setpts=PTS/{SPEED},fps=30,format=yuv420p[v]",
        f"[0:a]atempo={SPEED},aformat=sample_rates=48000:channel_layouts=stereo,volume=1.0[voice]",
        (
            "[1:a]aformat=sample_rates=48000:channel_layouts=stereo,"
            f"atempo={tempo:.6f},atrim=0:{target_hook:.3f},volume=0.19,"
            f"afade=t=out:st={max(0.0, target_hook - 0.35):.3f}:d=0.35[riser]"
        ),
    ]
    mix_labels = ["[voice]", "[riser]"]
    for number, (input_index, cut, volume) in enumerate(chosen, start=1):
        delay = round(cut * 1000)
        label = f"t{number}"
        filters.append(
            f"[{input_index}:a]aformat=sample_rates=48000:channel_layouts=stereo,"
            f"volume={volume:.2f},adelay={delay}:all=1[{label}]"
        )
        mix_labels.append(f"[{label}]")
    filters.append(
        "".join(mix_labels)
        + f"amix=inputs={len(mix_labels)}:normalize=0:duration=first,"
          "alimiter=limit=0.95:attack=5:release=80[aout]"
    )
    cmd.extend([
        "-filter_complex", ";".join(filters),
        "-map", "[v]", "-map", "[aout]",
        "-r", "30", "-fps_mode", "cfr", "-g", "60", "-keyint_min", "30",
        "-c:v", "libx264", "-preset", "fast", "-crf", "19", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "256k", "-ar", "48000", "-movflags", "+faststart", str(tmp),
    ])
    subprocess.run(cmd, check=True)
    tmp.replace(out)
    DESKTOP.mkdir(parents=True, exist_ok=True)
    shutil.copy2(out, DESKTOP / out.name)
    print(f"DONE {slug}: {probe_duration(out):.3f}s, {len(cut_times)} transition SFX, {riser.name} -> {target_hook:.3f}s")
    return out


def main() -> None:
    requested = sys.argv[1:]
    selected = requested if requested else PROJECTS
    unknown = [slug for slug in selected if slug not in PROJECTS]
    if unknown:
        raise SystemExit(f"Unknown V3 project(s): {', '.join(unknown)}")
    for slug in selected:
        render_project(slug)


if __name__ == "__main__":
    main()
