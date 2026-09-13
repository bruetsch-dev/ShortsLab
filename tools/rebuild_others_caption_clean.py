"""Rebuild an existing Others edit with source-caption cleanup before format labels."""
import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import others_vs_king
import pipeline


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("slug")
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[1] / "projects" / args.slug
    config_path = project / "config" / "project.json"
    report_path = project / "review" / "others_search_report.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    report = json.loads(report_path.read_text(encoding="utf-8"))
    selected = report.get("selected") or []
    scenes = config.get("scenes") or []
    if len(selected) != len(scenes) or not scenes:
        raise RuntimeError("Selected-source report does not match the editable scene list.")

    clip_dir = project / "seedance 2.0"
    pieces = []
    action = str(config.get("others_action") or report.get("action") or "the action")
    normal_label = f"OTHERS {action}...".upper()
    payoff_action = action[6:] if action.lower().startswith("doing ") else action
    payoff_label = f"KING OF {payoff_action}".upper()

    for index, (source_row, scene) in enumerate(zip(selected, scenes), 1):
        source_id = str(source_row.get("id") or scene.get("source_id") or "")
        candidates = list((project / "downloads").glob(f"*/*{source_id}*.mp4"))
        if not candidates:
            raise RuntimeError(f"Original source {source_id} is missing.")
        row = dict(source_row)
        row.update({"path": str(candidates[0]), "platform": source_row.get("platform") or "tiktok"})
        clean = clip_dir / f"others_{index:02d}_clean_source.mp4"
        prepared = others_vs_king._caption_clean_selected_range(row, clean, status_cb=print)
        output = clip_dir / f"others_{index:02d}.mp4"
        label = payoff_label if index == len(scenes) else normal_label
        others_vs_king._render_piece(prepared, output, label)
        scene["blur_captions"] = False
        scene["source_captions_precleaned"] = True
        scene["caption_cleanup_removed"] = bool(prepared.get("captions_removed"))
        scene["caption_cleanup_refused"] = bool(prepared.get("caption_cleanup_refused"))
        pieces.append(output.resolve())

    duration = float(config.get("duration") or scenes[-1].get("end") or 0)
    payoff_start = float(scenes[-1].get("start") or 0)
    raw = project / "renders" / f"{args.slug}_picture_cut.mp4"
    output = project / "renders" / f"{args.slug}.mp4"
    others_vs_king._concat(pieces, raw.resolve())
    soundtrack = others_vs_king._add_unified_soundtrack(
        raw.resolve(), output.resolve(), duration, payoff_start=payoff_start)
    audio = project / "input" / "voiceover.wav"
    subprocess.run([str(pipeline.find_ffmpeg()), "-y", "-hide_banner", "-loglevel", "error",
                    "-i", str(output), "-vn", "-ar", "48000", "-ac", "2", str(audio)],
                   check=True, timeout=900)
    config["source_captions_precleaned"] = True
    config["background_music_choice"] = Path(soundtrack).name if soundtrack else "none"
    config_path.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"video": str(output), "cleaned": sum(
        bool(s.get("caption_cleanup_removed")) for s in scenes),
        "refused": sum(bool(s.get("caption_cleanup_refused")) for s in scenes)}, indent=2))


if __name__ == "__main__":
    main()
