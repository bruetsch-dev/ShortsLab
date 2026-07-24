"""Remove a source video's OWN burned-in overlays before it becomes a Short.

Discovery recuts a real TikTok, so whatever the uploader burned into the frame comes
along: the original subtitle track and, on reposts, a player watermark. Two frames of
text on top of each other reads as stolen footage, which is exactly what the finished
Short must not look like.

Two passes, both local (ffmpeg + RapidOCR, no API):

* `strip_top_watermark` CROPS a fixed watermark away and re-frames to 9:16. Blurring a
  logo leaves a smudge in every single frame; cutting it off leaves nothing.
* `blur_burned_captions` blurs the subtitles through a mask that CHANGES OVER TIME.
  clip_scraper.blur_caption_regions builds one static mask from 8 samples, which is
  right for a 2s scrape clip but wrong here: a recut runs for seconds and swaps its
  subtitle two or three times, so a static mask covers one line and smears the frames
  where the text has already moved.
"""

import os
import subprocess
import sys
from pathlib import Path

try:
    import cv2
    import numpy as np
except Exception:  # pragma: no cover - the caller degrades to "no cleaning"
    cv2 = None
    np = None


def _log(status_cb, msg):
    if status_cb:
        status_cb(msg)
    else:
        print(msg)


def _probe(path):
    """(width, height, duration, fps) of a video."""
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                        "-show_entries", "stream=width,height,r_frame_rate",
                        "-show_entries", "format=duration", "-of", "default=nw=1",
                        str(path)], capture_output=True, text=True, timeout=60)
    vals = {}
    for line in (r.stdout or "").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            vals[k.strip()] = v.strip()
    try:
        num, den = vals.get("r_frame_rate", "30/1").split("/")
        fps = float(num) / float(den or 1)
    except Exception:
        fps = 30.0
    return (int(vals.get("width", 0)), int(vals.get("height", 0)),
            float(vals.get("duration", 0) or 0), fps)


def strip_top_watermark(path, ffmpeg, drop_px, status_cb=None):
    """Cut `drop_px` off the top, re-crop to 9:16 and scale back to the original size.

    Used for repost watermarks ("PLAY >") that sit in a fixed corner. Returns True when
    the file was rewritten.
    """
    path = Path(path)
    w, h, _, _ = _probe(path)
    if not (w and h) or drop_px <= 0 or drop_px >= h * 0.30:
        return False
    ch = h - drop_px
    cw = int(round(ch * 9 / 16))
    if cw > w:                       # cannot keep 9:16 without adding bars - crop height instead
        cw = w
        ch = int(round(cw * 16 / 9))
    cx = max(0, (w - cw) // 2)
    tmp = path.with_name(path.stem + "_nowm" + path.suffix)
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(path),
           "-vf", f"crop={cw}:{ch}:{cx}:{drop_px},scale={w}:{h}:flags=lanczos",
           "-c:v", "libx264", "-crf", "18", "-preset", "veryfast",
           "-c:a", "copy", "-movflags", "+faststart", str(tmp)]
    subprocess.run(cmd, capture_output=True, timeout=900)
    if tmp.exists() and tmp.stat().st_size > 4096:
        os.replace(str(tmp), str(path))
        _log(status_cb, f"Cleaned {path.name}: cropped {drop_px}px off the top (watermark).")
        return True
    tmp.unlink(missing_ok=True)
    return False


def _boxes_in_band(bgr, band, min_conf=0.30):
    """OCR text boxes inside the caption band, in FULL-frame pixel coordinates."""
    import clip_scraper as cs
    h = bgr.shape[0]
    lo, hi = band[0] * h, band[1] * h
    out = []
    for (x, y, w, bh) in cs._ocr_text_rows(bgr, min_conf=min_conf):
        if lo <= y + bh / 2.0 <= hi:
            out.append((x, y, w, bh))
    return out


def blur_burned_captions(path, ffmpeg, status_cb=None, band=(0.26, 0.97),
                         step=0.25, sigma=90, pad=26, reach=2):
    """Blur the source's own subtitles through a time-varying mask.

    Samples the clip every `step` seconds, OCRs each sample, and paints a feathered
    patch over every text box found. Each mask is the union of its own boxes and its
    two neighbours', so a subtitle that swaps between two samples stays covered for the
    whole handover instead of flashing readable for a few frames.

    Returns the number of samples that carried text (0 = nothing done).
    """
    if cv2 is None or np is None:
        return 0
    path = Path(path)
    w, h, dur, fps = _probe(path)
    if not (w and h and dur > 0.2):
        return 0
    work = path.with_name(path.stem + "_mask_work")
    work.mkdir(exist_ok=True)
    try:
        # 1) sample frames (small, for OCR speed)
        subprocess.run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(path),
                        "-vf", f"fps=1/{step},scale=720:-2", str(work / "s_%04d.png")],
                       capture_output=True, timeout=900)
        samples = sorted(work.glob("s_*.png"))
        if not samples:
            return 0
        sx = w / 720.0                     # OCR ran on a 720-wide copy

        per_sample = []
        for f in samples:
            bgr = cv2.imread(str(f))
            per_sample.append(_boxes_in_band(bgr, band) if bgr is not None else [])
        hits = sum(1 for b in per_sample if b)
        if not hits:
            return 0

        # 2) delogo per detected line, switched on for the window that line is visible.
        # Two approaches lost out here: a gaussian blur through a mask leaves the letter
        # shapes readable inside an obvious grey rectangle, and ONE tall delogo box over
        # both subtitle lines smears so badly that the recognizer reads the artefacts as
        # fresh text. A moderate box per line interpolates cleanly and leaves nothing.
        parts = []
        for i, boxes in enumerate(per_sample):
            t0 = max(0.0, i * step - 0.20)
            t1 = i * step + step + 0.20
            for (x, y, bw, bh) in boxes:
                bx, by = int(x * sx), int(y * sx)
                bw2, bh2 = int(bw * sx), int(bh * sx)
                # The recognizer usually reads only the FIRST of a two-line subtitle, so
                # cover the line below it as a second, equally sized box.
                for row in (0, 1):
                    X = max(2, bx - pad)
                    Y = max(2, by - pad // 2 + row * int(bh2 * 1.15))
                    W = min(w - X - 2, bw2 + 2 * pad)
                    H = min(h - Y - 2, bh2 + pad)
                    if W > 8 and H > 8 and Y + H < h - 2:
                        parts.append(f"delogo=x={X}:y={Y}:w={W}:h={H}:"
                                     f"enable='between(t,{t0:.2f},{t1:.2f})'")
        if not parts:
            return 0

        out = path.with_name(path.stem + "_capclean" + path.suffix)
        cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(path),
               "-vf", ",".join(parts),
               "-c:v", "libx264", "-crf", "18", "-preset", "veryfast",
               "-c:a", "copy", "-movflags", "+faststart", str(out)]
        subprocess.run(cmd, capture_output=True, timeout=1800)
        if out.exists() and out.stat().st_size > 4096:
            os.replace(str(out), str(path))
            _log(status_cb, f"Cleaned {path.name}: blurred subtitles in {hits}/{len(samples)} samples.")
            return hits
        out.unlink(missing_ok=True)
        return 0
    finally:
        for f in work.glob("*.png"):
            f.unlink(missing_ok=True)
        try:
            work.rmdir()
        except OSError:
            pass
