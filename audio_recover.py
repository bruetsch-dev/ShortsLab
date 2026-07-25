"""Give a silent discovery clip its original audio back.

Story mode lays the source TikTok's own audio quietly under the voiceover, but clips
cut before that feature landed (commit 17a9433, 2026-07-24 00:29) are mute, and any
clip re-cut by the old cut-verification pass lost its audio too. Re-running the whole
recut would need the LLM; this does it offline instead.

The trick is that the PICTURE is already correct. So instead of reproducing the edit,
find where each clip's frames came from: hash every frame of the source, hash the
clip's frames, and read the offset straight off the match. A clip built from several
jump cuts is split at its internal scene changes first and each piece located on its
own, so the recovered audio follows the same jumps the picture makes.

Every match reports a Hamming distance. A real match sits near 0; anything above
`max_distance` is refused rather than muxed in as plausible-sounding garbage.
"""

import os
import subprocess
import sys
from pathlib import Path

try:
    import cv2
    import numpy as np
except Exception:  # pragma: no cover
    cv2 = None
    np = None


def _log(cb, msg):
    (cb or print)(msg)


def _probe_duration(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "csv=p=0", str(path)], capture_output=True, text=True, timeout=60)
    try:
        return float((r.stdout or "").strip())
    except ValueError:
        return 0.0


def _has_audio(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
                        "stream=codec_type", "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True, timeout=60)
    return "audio" in (r.stdout or "")


def _dhash(gray):
    """64-bit difference hash of one frame, as a numpy bool array."""
    small = cv2.resize(gray, (9, 8), interpolation=cv2.INTER_AREA)
    return small[:, 1:] > small[:, :-1]


def _hash_sequence(video, ffmpeg, workdir, tag, step=None, fps=None):
    """Decode a video to small greyscale frames and return [(time, hash), ...]."""
    for f in workdir.glob(f"{tag}_*.png"):
        f.unlink(missing_ok=True)
    rate = fps if fps else 1.0 / step
    subprocess.run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(video),
                    "-vf", f"fps={rate:.4f},scale=64:114", str(workdir / f"{tag}_%05d.png")],
                   capture_output=True, timeout=1800)
    out = []
    for i, f in enumerate(sorted(workdir.glob(f"{tag}_*.png"))):
        img = cv2.imread(str(f), cv2.IMREAD_GRAYSCALE)
        if img is not None:
            out.append((i / rate, _dhash(img)))
    return out


def _scene_cuts(video, ffmpeg, threshold=0.30):
    """Times of hard cuts inside a clip (the jump cuts the recut stitched together)."""
    r = subprocess.run([ffmpeg, "-hide_banner", "-i", str(video), "-filter_complex",
                        f"select='gt(scene,{threshold})',metadata=print:file=-",
                        "-an", "-f", "null", "-"], capture_output=True, text=True, timeout=900)
    times = []
    for line in (r.stdout or "").splitlines():
        if "pts_time:" in line:
            try:
                times.append(float(line.split("pts_time:")[1].split()[0]))
            except (IndexError, ValueError):
                pass
    return [t for t in times if t > 0.25]


def recover_clip_audio(clip, source, ffmpeg, workdir, status_cb=None,
                       step=0.10, max_distance=12):
    """Locate `clip`'s segments inside `source` and mux the matching audio in.

    Returns (True, worst_distance) on success, (False, reason) when a segment could
    not be located confidently enough to trust.
    """
    clip, source = Path(clip), Path(source)
    if _has_audio(clip):
        return False, "clip already has audio"
    if not _has_audio(source):
        return False, "source has no audio"
    src_hashes = _hash_sequence(source, ffmpeg, workdir, "src", step=step)
    if not src_hashes:
        return False, "could not decode the source"

    clip_dur = _probe_duration(clip)
    cuts = [0.0] + _scene_cuts(clip, ffmpeg) + [clip_dur]
    segs = [(cuts[i], cuts[i + 1]) for i in range(len(cuts) - 1)
            if cuts[i + 1] - cuts[i] > 0.20]
    if not segs:
        segs = [(0.0, clip_dur)]

    stack = np.array([h for _, h in src_hashes])
    times = [t for t, _ in src_hashes]
    parts, worst = [], 0
    for k, (s0, s1) in enumerate(segs):
        probe = workdir / f"seg_{k:03d}.png"
        subprocess.run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                        "-ss", f"{s0 + 0.05:.3f}", "-i", str(clip), "-frames:v", "1",
                        "-vf", "scale=64:114", str(probe)], capture_output=True, timeout=120)
        img = cv2.imread(str(probe), cv2.IMREAD_GRAYSCALE)
        probe.unlink(missing_ok=True)
        if img is None:
            return False, f"segment {k} unreadable"
        dist = np.count_nonzero(stack != _dhash(img), axis=(1, 2))
        best = int(dist.argmin())
        if int(dist[best]) > max_distance:
            return False, f"segment {k} not found in the source (distance {int(dist[best])})"
        worst = max(worst, int(dist[best]))
        parts.append((times[best], s1 - s0))

    # cut the matching audio pieces and glue them in the clip's own order
    piece_files = []
    for k, (t0, dur) in enumerate(parts):
        pf = workdir / f"a_{k:03d}.m4a"
        subprocess.run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                        "-ss", f"{t0:.3f}", "-t", f"{dur:.3f}", "-i", str(source),
                        "-vn", "-c:a", "aac", "-b:a", "128k", "-ar", "48000", str(pf)],
                       capture_output=True, timeout=300)
        if pf.exists() and pf.stat().st_size > 512:
            piece_files.append(pf)
    if not piece_files:
        return False, "audio extraction produced nothing"

    listing = workdir / "concat.txt"
    listing.write_text("".join(f"file '{p.as_posix()}'\n" for p in piece_files), encoding="utf-8")
    joined = workdir / "joined.m4a"
    subprocess.run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-f", "concat",
                    "-safe", "0", "-i", str(listing), "-c", "copy", str(joined)],
                   capture_output=True, timeout=300)
    if not joined.exists():
        return False, "audio concat failed"

    out = clip.with_name(clip.stem + "_wa" + clip.suffix)
    subprocess.run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                    "-i", str(clip), "-i", str(joined), "-map", "0:v", "-map", "1:a",
                    "-c:v", "copy", "-c:a", "aac", "-b:a", "128k", "-shortest",
                    "-movflags", "+faststart", str(out)], capture_output=True, timeout=600)
    for f in piece_files:
        f.unlink(missing_ok=True)
    joined.unlink(missing_ok=True)
    listing.unlink(missing_ok=True)
    if out.exists() and out.stat().st_size > 4096:
        os.replace(str(out), str(clip))
        _log(status_cb, f"   {clip.name}: {len(segs)} segment(s) located, worst match {worst}/64")
        return True, worst
    out.unlink(missing_ok=True)
    return False, "mux failed"
