"""One folder that holds every finished render, whatever produced it.

Finished videos used to end up wherever the mode that made them happened to work: a clip short
under ``projects/<slug>/render/``, a longform under ``projects/_longform/<slug>/``, an action
edit somewhere else again. Finding last night's output meant remembering which mode wrote it.

So every deliverable is ALSO copied here, flat, under ``.renders/``. Copied, not moved - the
project folder keeps its own file, because the editors, the rebuild button and the resume logic
all read the render back out of the project.

A render that carries subtitles brings them along: the .srt is copied next to the video under
the same stem, so the pair stays together outside the project too.
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RENDERS_DIR = ROOT / ".renders"

# Sidecars that belong to a video and are copied with it when they sit next to the source.
SIDECAR_SUFFIXES = (".srt", ".editorial.json")


def _safe_name(value):
    """A project name that is legal as a folder on Windows."""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", str(value or "")).strip(" ._")
    return cleaned[:80]


def _unique(target):
    """A free name near `target`, never overwriting a different render.

    The same project rendered twice writes the same basename, and silently replacing the first
    file would throw away the version the user is comparing against.
    """
    if not target.exists():
        return target
    stem, suffix = target.stem, target.suffix
    for n in range(2, 1000):
        candidate = target.with_name(f"{stem}_{n}{suffix}")
        if not candidate.exists():
            return candidate
    return target.with_name(f"{stem}_{target.stat().st_mtime_ns}{suffix}")


def publish(video_path, project=None, status_cb=None, sidecars=True):
    """Copy a finished render (and its subtitles) into ``.renders``. Returns the copy, or None.

    Never raises: a delivered video must not be lost because a copy failed. The caller keeps
    using its own path either way - this is an extra location, not a move.
    """
    try:
        source = Path(video_path)
        if not source.is_file() or source.stat().st_size <= 0:
            return None
        # One folder per project, holding everything that project produced - the video, its
        # subtitles, and every language track and translation. Flat, the language files of a
        # dozen projects were an unreadable pile in which nothing belonged to anything.
        label = _safe_name(project or source.parent.name) or "misc"
        folder = RENDERS_DIR / label
        folder.mkdir(parents=True, exist_ok=True)
        target = _unique(folder / source.name)
        shutil.copy2(source, target)
        if sidecars:
            for suffix in SIDECAR_SUFFIXES:
                mate = source.with_suffix(suffix)
                if mate.is_file():
                    shutil.copy2(mate, target.with_suffix(suffix))
        if status_cb:
            status_cb(f"Saved to .renders/{target.parent.name}/{target.name}")
        return target
    except Exception as exc:                                            # noqa: BLE001
        if status_cb:
            status_cb(f"Could not copy the render into .renders ({type(exc).__name__}).")
        return None
