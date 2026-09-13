"""Run an EXISTING project's clips through the caption remover again.

The old pass blurred, and blur cannot hide text under a glyph-tight mask - so every project
rendered before this carries captions that are still readable. Nothing was lost, though: the
blurred copy lives beside the original as capblur_<sid>_<hash>.mp4 and the scene remembers the
untouched source in ``caption_blur_src``. So the fix is to rebuild those copies from the
originals rather than from the already-processed files.

    python tools/recaption_project.py <project-slug>            # rebuild what the toggle marked
    python tools/recaption_project.py <project-slug> --all      # every scrape clip in the project
    python tools/recaption_project.py <project-slug> --dry-run  # say what it would do

The project's config is rewritten so the timeline and the next render pick the new files up.
Re-render afterwards: the existing mp4 in renders/ is not touched.
"""
import argparse
import hashlib
import json
import os
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import agent_core
import caption_remover
import pipeline


def scenes_to_process(config, every):
    """Which scenes to rebuild, and from which ORIGINAL file."""
    jobs = []
    for scene in config.get("scenes", []):
        clip = str(scene.get("clip") or "")
        if not clip:
            continue
        source = str(scene.get("caption_blur_src") or "")
        if source:
            # Already processed once: rebuild from the untouched original, never from the
            # blurred copy - running a remover over its own output smears twice.
            jobs.append((scene, source))
        elif every and not clip.startswith(("capblur_", "speed_")):
            jobs.append((scene, clip))
    return jobs


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("slug")
    parser.add_argument("--all", action="store_true",
                        help="process every scrape clip, not only the ones already marked")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    project = agent_core.PROJECTS_DIR / args.slug
    config_path = project / "config" / "project.json"
    if not config_path.is_file():
        raise SystemExit(f"no config for project '{args.slug}'")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    clip_dir = project / "seedance 2.0"
    ffmpeg = pipeline.find_ffmpeg()
    if not ffmpeg:
        raise SystemExit("ffmpeg not found")

    jobs = scenes_to_process(config, args.all)
    print(f"{args.slug}: {len(jobs)} clip(s) to re-process "
          f"({'ProPainter' if caption_remover.propainter_available() else 'CPU inpainting'})")
    if args.dry_run:
        for scene, source in jobs:
            print(f"  scene {scene.get('id')}: rebuild from {source}")
        return

    changed = removed = refused = 0
    for scene, source in jobs:
        origin = clip_dir / source
        if not origin.is_file():
            print(f"  scene {scene.get('id')}: source {source} is gone - skipped")
            continue
        # EXACTLY the name the render would build. Python's hash() is salted per process, so
        # the first version produced a different filename on every run: junk files accumulated
        # and the render never recognised the work as already done, so it redid it.
        key = hashlib.sha1(source.encode("utf-8", "ignore")).hexdigest()[:10]
        dest = clip_dir / f"capblur_{scene.get('id')}_{key}.mp4"
        shutil.copy2(origin, dest)
        info = {}
        started = time.time()
        found = caption_remover.remove_caption_regions(
            dest, ffmpeg, status_cb=lambda m: print(f"    {m}"), info=info)
        if found:
            scene["clip"] = dest.name
            scene["asset"] = dest.name
            scene["caption_blur_src"] = source
            scene["blur_captions"] = True
            changed += 1
            removed += 1
            print(f"  scene {scene.get('id')}: cleaned in {time.time() - started:.0f}s "
                  f"-> {dest.name}")
        else:
            dest.unlink(missing_ok=True)
            if info.get("refused"):
                refused += 1
                print(f"  scene {scene.get('id')}: {info['covered']:.0%} of the frame is text - "
                      "left alone, replace the clip instead")
            else:
                # Nothing found is a fine answer: point the scene back at the clean original.
                if scene.get("caption_blur_src"):
                    scene["clip"] = source
                    scene["asset"] = source
                    scene.pop("caption_blur_src", None)
                    changed += 1
                print(f"  scene {scene.get('id')}: no burned-in caption found")

    if changed:
        backup = config_path.with_suffix(".json.pre_recaption")
        if not backup.exists():
            shutil.copy2(config_path, backup)
        tmp = config_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(config, indent=2), encoding="utf-8")
        os.replace(tmp, config_path)
        print(f"config updated ({changed} scene(s)); previous config kept as {backup.name}")
    print(f"done: {removed} cleaned, {refused} refused as text slides")


if __name__ == "__main__":
    main()
