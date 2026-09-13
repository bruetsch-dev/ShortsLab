import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import others_vs_king
import clip_scraper
import pipeline


ROOT = Path(r"D:\data\AutoShortsClaude")
PROJECT = ROOT / "projects" / "others_reference_pattern_test"

SOURCES = [
    ("setup", "7356707451825048875", 0.1, 8.45, "OTHERS HIGH JUMP..."),
    ("setup", "7633972734510370078", 0.0, 10.5, "OTHERS HIGH JUMP..."),
    ("setup", "7676382016966790422", 3.0, 11.8, "OTHERS HIGH JUMP..."),
    ("setup", "7664311822597590302", 14.5, 17.4, "OTHERS HIGH JUMP..."),
    ("payoff", "7519207846727191839", 1.0, 8.0, "KING OF HIGH JUMP"),
]


def main():
    clips = PROJECT / "seedance 2.0" / "verified"
    renders = PROJECT / "renders"
    review = PROJECT / "review" / "others_mode"
    for folder in (clips, renders, review, PROJECT / "config"):
        folder.mkdir(parents=True, exist_ok=True)

    rendered = []
    scenes = []
    cursor = 0.0
    for index, (kind, video_id, start, end, label) in enumerate(SOURCES, 1):
        source = PROJECT / "downloads" / kind / f"tiktok_{video_id}.mp4"
        destination = clips / f"v6_{index:02d}_{video_id}.mp4"
        source_cuts = clip_scraper.hard_cut_times(
            str(source), pipeline.find_ffmpeg(), scan_seconds=end + 0.2) or []
        internal_cuts = [cut for cut in source_cuts if start + 0.08 < cut < end - 0.08]
        if internal_cuts:
            raise RuntimeError(f"Source {video_id} contains an original edit at {internal_cuts}")
        row = {
            "path": str(source), "start": start, "end": end,
            "label": label, "platform": "tiktok", "id": video_id,
        }
        others_vs_king._render_piece(row, destination, label)
        duration = end - start
        rendered.append(destination)
        scenes.append({
            "index": index - 1, "clip": str(destination), "source": str(source),
            "source_id": video_id, "source_start": start, "source_end": end,
            "start": round(cursor, 3), "end": round(cursor + duration, 3),
            "duration": duration, "visual_subject": label,
            "role": "payoff" if kind == "payoff" else "setup",
        })
        cursor += duration

    payoff_start = scenes[-1]["start"]
    raw = renders / "others_reference_pattern_VERIFIED_V8_RAW.mp4"
    output = renders / "others_reference_pattern_VERIFIED_V8_FINAL.mp4"
    others_vs_king._concat(rendered, raw)
    soundtrack = others_vs_king._add_unified_soundtrack(
        raw, output, cursor, payoff_start=payoff_start)

    payload = {
        "title": "Others High Jump vs King of High Jump",
        "mode": "others_vs_king",
        "render_path": str(output),
        "duration": round(cursor, 3),
        "payoff_start": payoff_start,
        "reference_drop_source_time": others_vs_king.REFERENCE_DROP_SECONDS,
        "music_source_offset": round(others_vs_king.REFERENCE_DROP_SECONDS - payoff_start, 3),
        "soundtrack": soundtrack,
        "scenes": scenes,
    }
    (PROJECT / "config" / "project.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    (PROJECT / "review" / "others_search_report.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
