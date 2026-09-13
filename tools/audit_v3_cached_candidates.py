"""Audit V3's saved test candidates without opening another TikTok browser session."""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pipeline
import scrape_v3


PROJECT = Path(r"D:\data\AutoShortsClaude\projects\why_japanese_convenience_stores_feel_like_cheat_codes_v3_test")


def main():
    lines = json.loads((PROJECT / "voice" / "line_timestamps.json").read_text(encoding="utf-8"))["lines"]
    scenes = [{"id": str(index), "start": row["start"], "end": row["end"],
               "exact_voice_text": row["exact_voice_text"]}
              for index, row in enumerate(lines)]
    chapters = scrape_v3.plan_chapters_v3(scenes, "", reasoning_model="openai/gpt-5.6-luna", status_cb=print)
    files = sorted((PROJECT / "seedance 2.0" / "_v3_proxies").glob("v3_*.mp4"),
                   key=lambda item: item.stat().st_mtime, reverse=True)[:14]
    downloaded = []
    for item in files:
        match = re.match(r"^v3_([^_]+)_(.+)$", item.stem)
        if match:
            downloaded.append({"platform": match.group(1), "source_id": match.group(2),
                               "path": str(item), "query": "saved test candidate", "item": {}})
    print(f"CACHED_AUDIT|chapters={len(chapters)} candidates={len(downloaded)}")
    ffmpeg = pipeline.find_ffmpeg()
    for chapter in chapters:
        graded = scrape_v3.grade_sources_v3(chapter, downloaded, PROJECT, ffmpeg,
                                             reasoning_model="openai/gpt-5.6-luna", status_cb=print)
        selected = scrape_v3.select_chapter_windows(chapter, graded, status_cb=print)
        print("CACHED_AUDIT_CHAPTER|%s|selected=%d|satisfied=%s" %
              (chapter.title, len(selected), scrape_v3.chapter_search_satisfied(chapter, selected)))


if __name__ == "__main__":
    main()
