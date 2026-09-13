"""Recover an editable partial V3 timeline from its persisted scrape report.

This is intentionally offline: it never searches, downloads, or calls a model.  V3 reports keep
the reviewed source windows, while ``_v3_proxies`` keeps the downloaded files.  Older V3 builds
discarded a reviewed window when it could not fill its entire chapter; this tool joins those two
pieces back together and writes short, explicit replacement slots for only the uncovered spans.
"""
import argparse
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import scrape_v3


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def recover(project_dir):
    project_dir = Path(project_dir).resolve()
    config_path = project_dir / "config" / "project.json"
    report_path = project_dir / "review" / "scrape_v3_report.json"
    config = _read(config_path)
    report = _read(report_path)
    backup = config_path.with_name("project.before_v3_recovery.json")
    manual_path = project_dir / "review" / "manual_rescue_windows.json"
    manual_windows = _read(manual_path) if manual_path.exists() else {}
    # Re-running recovery must not use the already rebuilt V3 shots as voice truth: their
    # display `script` fields span cuts and would recursively duplicate narration fragments.
    # The pre-recovery file contains the original fine-grained timed voice beats.
    voice_config = _read(backup) if backup.exists() else config
    old_scenes = [row for row in (voice_config.get("scenes") or []) if isinstance(row, dict)]
    proxy_dir = project_dir / "seedance 2.0" / "_v3_proxies"
    output_dir = project_dir / "seedance 2.0"
    output_dir.mkdir(parents=True, exist_ok=True)

    chapters = []
    windows_by_chapter = {}
    for row in (report.get("chapters") or []):
        span = row.get("voice_span") or [0.0, 0.0]
        chapter_id = int(row.get("chapter_id") or 0)
        chapter = scrape_v3.Chapter(
            chapter_id=chapter_id,
            title=str(row.get("title") or f"Chapter {chapter_id + 1}"),
            scene_ids=[str(value) for value in (row.get("scene_ids") or [])],
            start=float(span[0] or 0.0), end=float(span[1] or 0.0),
        )
        chapters.append(chapter)
        windows = []
        reported = list(row.get("windows") or [])
        # Optional human-reviewed rescue windows use the same schema as the persisted report.
        # This keeps a manual editorial correction auditable and lets the normal recovery path
        # rebuild timings/caption-cleanup flags rather than hand-editing project.json.
        reviewed = list(manual_windows.get(str(chapter_id), []) or [])
        if manual_windows.get("replace_reported") and reviewed:
            reported = reviewed
        else:
            reported.extend(reviewed)
        for item in reported:
            source_id = str(item.get("source_id") or "")
            matches = list(proxy_dir.glob(f"*{source_id}*.mp4"))
            if not matches:
                continue
            bounds = item.get("window") or [0.0, 0.0]
            windows.append(scrape_v3.ShotWindow(
                chapter_id=chapter_id, source_id=source_id,
                platform=str(item.get("platform") or "tiktok"), path=str(matches[0]),
                start=float(bounds[0] or 0.0), end=float(bounds[1] or 0.0),
                match_class=str(item.get("match_class") or "context"),
                reason=str(item.get("why") or ""),
                source_has_captions=bool(item.get("source_has_captions")),
            ))
        windows_by_chapter[chapter_id] = windows

    duration = max((float(row.get("end") or 0.0) for row in old_scenes), default=0.0)
    new_scenes = scrape_v3.align_windows_to_voice(
        chapters, windows_by_chapter, duration, voice_scenes=old_scenes)
    for scene in new_scenes:
        name = str(scene.get("clip") or "")
        if not name:
            continue
        source = next((Path(window.path) for windows in windows_by_chapter.values()
                       for window in windows if Path(window.path).name == name), None)
        if source and source.is_file():
            shutil.copy2(source, output_dir / name)

    if not backup.exists():
        shutil.copy2(config_path, backup)
    config["scenes"] = new_scenes
    config["scraping_engine"] = "v3"
    config["_scrape_engine"] = "v3"
    config.setdefault("scrape_recovery", {})["v3_partial_timeline"] = {
        "source_report": str(report_path),
        "downloaded_pool_preserved": len(list(proxy_dir.glob("*.mp4"))),
        "assigned_windows_restored": sum(1 for row in new_scenes if row.get("clip")),
        "uncovered_replacement_slots": sum(1 for row in new_scenes if not row.get("clip")),
    }
    config_path.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")
    return config["scrape_recovery"]["v3_partial_timeline"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("project")
    args = parser.parse_args()
    print(json.dumps(recover(args.project), indent=2))
