"""Take somebody else's burned-in caption off a clip - remove it, do not cover it.

The old pass blurred. Blur cannot work on a glyph-tight mask: a Gaussian averages the pixels
INSIDE the mask, and inside a glyph-tight mask there is only the glyph, so the letter is smoothed
into itself and the word shape survives. Measured on a real clip at sigma 80 - already extreme -
"Find your locker" stayed readable. The previous answer was to widen the mask to the whole text
line so the blur had background to average, which hid the words and destroyed the picture: on one
shot it smeared away the goshuin stamp that was the entire subject.

Inpainting is the right operation. It DISCARDS the glyph pixels and rebuilds from elsewhere, so a
tight mask is an advantage rather than a problem.

Two implementations, same interface:

- ProPainter (third_party/ProPainter, GPU): fills from OTHER FRAMES along the optical flow. Where
  the camera moves, the wall behind the caption was genuinely visible at some point, so this is
  reconstruction rather than invention. Measured on a locker-room pan: residual text signal 1.76
  against 50.26 in the original, and it rebuilt the panel edges. 54 frames took 15 seconds on an
  RTX 2080 Ti. Its S-Lab licence is non-commercial.
- OpenCV inpainting (CPU, always available): per frame, fills from the surrounding pixels of the
  same frame. Residual 2.42 on the same clip. Good, slightly softer, and it invents texture
  rather than recovering it.

The mask comes from the MEAN TOP-HAT across sampled frames. A burned-in caption sits in the same
pixels for the whole clip, so its response accumulates while moving highlights average away - it
found the whole line where the OCR pass had returned two of three words.
"""
import math
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

try:
    import cv2
    import numpy as np
except ImportError:      # pragma: no cover - the caller degrades to "no captions found"
    cv2 = None
    np = None

ROOT = Path(__file__).resolve().parent
PROPAINTER_DIR = ROOT / "third_party" / "ProPainter"

# Above this share of the frame the clip is a text slide, not captioned footage. Inpainting that
# much is neither fast nor convincing, and the clip should be replaced instead.
# If removing creator text would alter more than a small share of the picture, the source is bad
# footage for a Short and must be replaced. The previous 30% ceiling allowed hands, faces and
# architecture to be reconstructed along with the caption.
# This is glyph-mask coverage, not a rectangular subtitle box. OCR seeds only the visible bright
# glyphs now (rather than dark clothing/background inside the OCR row), so normal multi-line phone
# captions stay below this conservative ceiling while actual text slides are still rejected.
# Re-measured 2026-09-02 after the detector became OCR-gated. 0.08 was tuned against a detector
# that hallucinated: on clips with no text at all it reported 27.1% and 19.5% because a shape
# heuristic read faces and shirts as ink, so the ceiling had to sit far below those numbers. The
# detector now returns nothing when the recogniser reads nothing, so a high number means real
# letters. The value itself is drawn between measured outcomes rather than reasoned about: four
# clips were cleaned and looked at, and 4.6% and 10.8% came out perfectly clean while 12.5% and
# 19.7% came out smeared. Coverage is only a proxy - what really decides it is whether the
# background behind the caption is ever revealed, which cannot be known before filling - so the
# result is re-measured afterwards as well.
MAX_COVERAGE = 0.12
# ...and the floor under which a clip is left alone. This was 0.015 and it was too high: a small
# two-line Japanese caption measures well under that, so the clip was never even offered to the
# remover. Measured on the couples Short (2026-09-05), all at the beat's own in-point:
#
#     scraped_04  0.0131   shipped with the caption on it
#     capblur_02  0.0049   genuinely clean
#     scraped_00  0.0032   genuinely clean
#
# The gap between a real caption and a clean frame is wide; the floor belongs inside it.
MIN_COVERAGE = 0.008
# WHICH GENERATION OF THIS PASS MADE A CLEANED CLIP. The render caches its derivatives under
# `capblur_<scene>_<hash of source + span>.mp4` and reuses any that exists - a key that says
# nothing about the code that produced the file. So a clip cleaned by an older, worse version is
# reused for ever: the eating-while-walking Short kept shipping a fill with a close-up of the
# boy's face pasted into the sky, and four fresh runs of the same source and window came back
# clean. Bump this whenever the detector or the fill changes in a way that should invalidate what
# is on disk; old derivatives are then simply never looked up again (nothing is deleted).
CLEAN_VERSION = "5"   # 5: the fill's directional structure is measured and recorded per clip.
                      # 4: the fill reads the window the edit shows, and is keyed in only there.
                      #    Every derivative below 4 may carry a patch computed at second 0 and
                      #    printed over the whole file - they are retired, not reused.
# MEASURED AND REPORTED, NOT GATING - and this one was a gate for four hours, so the reason is
# worth writing down properly.
#
# Where the background behind a caption is never revealed, ProPainter invents it and returns
# content dragged along one axis: a smear. Nothing else here sees that - the residual check asks
# whether TEXT is left (a smear has none), fill_damage asks whether the area went dark (it did
# not), and patch_flatness put a wrecked night market at 0.741 and a clean plate of gyoza at
# 0.743. `fill_direction` compares the gradient energy inside the rebuilt area with the picture
# immediately around it, and on twelve fills of two Shorts it separated perfectly: smears at
# 0.640 / 0.650 / 0.753 / 0.876, clean rebuilds from 0.923 up to 1.231. So it became a gate with
# a floor at 0.90.
#
# A third project took an hour to destroy that. Every one of these was looked at:
#
#     0.829  the caption is still there and the children are doubled   SMEARED - rejected, right
#     0.840  jacket, pleats, shoes and tights all intact, caption gone  CLEAN - rejected, WRONG
#     0.901  a pale streak dragged through the hair                    smeared - kept
#     0.921  a crouching child rebuilt as a black lump                 smeared - kept
#     1.006  a yellow glyph stub and a bar across the sweater          fragments - kept
#     1.340, 1.559  a ghost caption still readable through the sleeve  residue - kept
#
# One correct rejection, one that threw away the cleanest fill of the whole set, and five defects
# waved through from 0.901 all the way up to 1.559. The classes overlap in BOTH directions, so
# there is no floor to put anywhere: the twelve-sample separation was the sample, not the world.
#
# The number is still worth recording - it is what a future calibration would be built from, and
# it is the only signal in the pass that looks at the PICTURE rather than at text or brightness.
# It decides nothing. A gate that discards the cleanest fill it has ever seen is worse than the
# artefact it hunts, which is exactly what the note above patch_flatness has said all along.
#
# If it is ever made to decide again: label a fresh set by eye FIRST, across at least three
# projects, and do not read the numbers until the labels are written down.
# ProPainter at full 1080x1920 needs more VRAM than a 11GB card has. It runs on a downscaled copy
# and only the FILLED REGION is composited back into the full-resolution original, so the rest of
# the picture never loses a pixel.
WORK_WIDTH = 540
# The width ProPainter actually RUNS at. Measured on an 11 GB card (2026-08-29): 540 and 432 both
# OOM on a 9:16 clip, 360 completes in 77s. Shortening the temporal window instead does nothing -
# 40, 24, 12 and 8 all failed at 540. Re-measured 2026-09-03: 540 OOMed on 100, 200 and 585
# frames alike, 82-149s wasted per attempt, so the 540px first pass was dropped - the mask and
# the composite still work at WORK_WIDTH, only the fill is smaller, and it is keyed back in
# through the mask so nothing outside the caption loses resolution.
FILL_WIDTH = 360
RETRY_WIDTH = FILL_WIDTH        # the old name, kept for callers and tests that import it
# How many frames go into ONE ProPainter run. Its memory grows with the length of the clip -
# the flow stage holds every frame's forward and backward flow at once, which --subvideo_length
# does not bound. Measured 2026-09-03 on the 11 GB card with ~3.3 GB already held by the desktop:
# 585 frames OOM at 540px AND at 360px, 200 frames at 360px complete in 123s, 100 in 45s.
MAX_FILL_FRAMES = 200
# How much stronger the ink inside the caption mask has to be than the ink beside it, between the
# quiet and the busy end of a clip, before the clip is split into captioned and clean stretches.
# Measured 2026-09-03: a clip whose caption stops halfway scores 13.0 against 1.0 (a factor of
# 13), one whose caption never stops scores 1.33 against 0.81 (a factor of 1.6) - and that second
# clip must be filled end to end, not half-skipped on a guess.
TEXT_RUN_CONTRAST = 3.0
# THE SCORE ABOVE WHICH A FRAME IS CARRYING TEXT, whatever the rest of the clip looks like.
# `_text_frame_scores` is a ratio on a fixed scale: an empty band sits at about 1, and the guard
# above already treats a whole clip peaking under 2.0 as having no caption at all. The run
# splitter, though, decided "clean" RELATIVELY - the geometric mean of the brightest and dimmest
# frames - so on a clip whose caption is merely dimmer in places, frames scoring 4 or 6 fell
# under the line and were passed through untouched with the text still on them. Measured across
# six split clips (2026-09-09), the tenth-percentile frame scored 4.05, 1.67, 1.41, 4.66, 6.31
# and 0.72: in five of the six, the frames the split called clean carried 2.4-8.3% text, and the
# fill was then rejected downstream for leaving a caption it had never been shown. Re-running one
# of them across the whole window produced a genuinely clean result.
TEXT_FRAME_FLOOR = 2.0


# The files ProPainter's own script loads before it touches a frame. A missing one shows up as
# "exit 1" a minute into a run, not as a startup error, which is the most expensive way to learn
# that a checkout is incomplete.
PROPAINTER_WEIGHTS = ("ProPainter.pth", "raft-things.pth", "recurrent_flow_completion.pth")


def propainter_unavailable_reason():
    """WHAT exactly stops the GPU path, or None when nothing does.

    A single boolean collapsed four different failures into one word. Measured 2026-09-03:
    ``propainter_available()`` returned True - checkout present, torch 2.6.0+cu124, CUDA up -
    while ``remove_caption_regions`` printed "ProPainter is the required backend and is
    unavailable" and left the caption on screen. The backend was fine; the CALLER had not
    enabled it. Naming the actual missing thing is the difference between a five-minute fix and
    a night of guessing.
    """
    script = PROPAINTER_DIR / "inference_propainter.py"
    if not script.is_file():
        return f"the ProPainter checkout is missing ({script})"
    missing = [name for name in PROPAINTER_WEIGHTS
               if not (PROPAINTER_DIR / "weights" / name).is_file()]
    if missing:
        return (f"ProPainter weights are missing ({', '.join(missing)} in "
                f"{PROPAINTER_DIR / 'weights'})")
    try:
        import torch
    except ImportError:
        return "PyTorch is not installed in this interpreter"
    try:
        if not torch.cuda.is_available():
            return f"torch {torch.__version__} reports no usable CUDA device"
    except Exception as exc:      # noqa: BLE001 - a broken driver is not an available GPU
        return f"the CUDA check failed ({type(exc).__name__}: {exc})"
    return None


def propainter_available():
    """Is the GPU path usable right now? Checked, never assumed."""
    return propainter_unavailable_reason() is None


def _read_frames(path, limit=400):
    cap = cv2.VideoCapture(str(path))
    frames = []
    while len(frames) < limit:
        ok, frame = cap.read()
        if not ok:
            break
        frames.append(frame)
    cap.release()
    return frames


def caption_coverage(path, ffmpeg, seconds=2.5, start=0.0):
    """What share of the frame burned-in text occupies, or -1.0 when it cannot be measured.

    The same measurement remove_caption_regions makes before deciding whether it can clean a
    clip. Exposed so the SCRAPER can ask it while alternatives still exist: asking a vision model
    whether a caption is removable produced captions on solid plates and full-height vertical
    text in finished Shorts, because that judgement is a guess. This is not.
    """
    if cv2 is None or np is None or not ffmpeg:
        return -1.0
    work = Path(tempfile.mkdtemp(prefix="capcov_"))
    try:
        small = work / "small.mp4"
        done = subprocess.run(
            [str(ffmpeg), "-nostdin", "-y", "-hide_banner", "-loglevel", "error",
             "-ss", f"{max(0.0, float(start)):.3f}", "-t", f"{max(0.4, float(seconds)):.3f}",
             "-i", str(path), "-vf", f"scale={WORK_WIDTH}:-2", "-an",
             "-c:v", "libx264", "-crf", "20", "-preset", "veryfast", str(small)],
            capture_output=True, timeout=180, stdin=subprocess.DEVNULL)
        if done.returncode or not small.is_file():
            return -1.0
        frames = _read_frames(small, limit=90)
        if len(frames) < 4:
            return -1.0
        mask = detect_caption_mask(frames)
        if mask is None:
            return 0.0
        return float((mask > 32).mean())
    except Exception:                                                   # noqa: BLE001
        return -1.0
    finally:
        shutil.rmtree(work, ignore_errors=True)


def detect_caption_mask(frames, dilate=7):
    """The pixels a burned-in caption occupies, as a glyph-tight mask.

    A caption is fixed in place for the whole clip while the picture moves under it, so its
    top-hat response is present in every frame and averaging over frames strengthens it. A moving
    specular highlight appears in a few frames and averages away. Components are then kept only
    where the mask concentrates - one or two text lines - so a bright seam elsewhere in the room
    does not drag a piece of the wall into the mask.
    """
    if cv2 is None or np is None or not frames:
        return None
    height, width = frames[0].shape[:2]
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (21, 21))
    total = np.zeros((height, width), np.float32)
    # Sampling every decoded frame is unnecessary for a screen-fixed overlay and made the
    # morphology pass dominate the entire render. Twenty-four evenly spaced frames preserve
    # temporal consensus while keeping cleanup interactive.
    temporal_indexes = sorted({int((len(frames) - 1) * i / 23) for i in range(24)}) \
        if len(frames) > 24 else list(range(len(frames)))
    temporal_frames = [frames[index] for index in temporal_indexes]

    # OCR-FIRST. The old shape heuristic can mistake a hand, white shirt or bright building for
    # caption ink. When the offline recognizer can read real words, restrict the mask to glyphs
    # inside those rows and return that precise mask. This is the same principle as the scraper's
    # caption gate: recognise letters, not merely bright shapes.
    try:
        import clip_scraper
        if clip_scraper._get_ocr() is not None:
            ocr_mask = np.zeros((height, width), dtype=np.uint8)
            ocr_rows = 0
            # Twenty samples, not twelve. A creator's caption is often a SEQUENCE - one line
            # leaves, the next arrives - and the recogniser also misses a frame here and
            # there (measured on the konbini clip: rows read in frames 0 and 39, none in 20).
            ocr_indexes = sorted({int((len(frames) - 1) * i / 19) for i in range(20)}) \
                if len(frames) > 20 else list(range(len(frames)))
            per_frame_rows = []
            for frame_index in ocr_indexes:
                per_frame_rows.append(clip_scraper._ocr_text_rows(
                    frames[frame_index], min_conf=0.30))
            clusters = clip_scraper._cluster_caption_boxes(per_frame_rows)
            # A third of the frames THAT CARRY ANY TEXT, not a third of the window. The old
            # rule asked a caption to survive a third of the whole clip; a two-line sequence
            # that swaps halfway through never could, so nothing counted as stable, the OCR
            # path returned empty and the fill went looking for bright shapes instead -
            # which is how a Short shipped with the red caption still standing in frame
            # while ProPainter had rebuilt 3.2% of it somewhere else.
            frames_with_text = sum(1 for rows in per_frame_rows if rows)
            required = max(2, int(round(max(1, frames_with_text) * 0.34)))
            stable_boxes = []
            for cluster in clusters:
                x, y, w, h = cluster["box"]
                cx = x + w / 2.0
                # OCR has already RECOGNISED letters here, and the cluster only exists because
                # the box stayed put across a third of the sampled frames. Judging it further by
                # aspect ratio was wrong twice over: a multi-line caption block is neither a wide
                # strip nor a narrow column - measured 190x335 on a 540x960 frame - and vertical
                # Japanese captions are narrow and tall. What still separates a caption from a
                # text slide is how much of the frame it claims.
                area_share = (w * h) / float(max(1, width * height))
                if (len(cluster["frames"]) >= required and area_share <= 0.42
                        and w >= width * 0.05 and h >= 6
                        and width * 0.04 <= cx <= width * 0.96
                        and height * 0.03 <= y <= height * 0.97):
                    stable_boxes.append(cluster["box"])

            if not stable_boxes:
                # The recogniser is available and read no text that stays in place. The shape
                # heuristic below cannot tell caption ink from a face: on two clips with no
                # burned-in text at all it reported 27.1% and 19.5% coverage, both above
                # MAX_COVERAGE, so both clips were thrown away as "text slides". Trust the
                # silence. A caption RapidOCR cannot read simply goes uncleaned, which is a far
                # cheaper failure than discarding good footage.
                return None

            def overlaps_stable(box):
                x, y, w, h = box
                for sx, sy, sw, sh in stable_boxes:
                    ix, iy = max(x, sx), max(y, sy)
                    iw, ih = min(x + w, sx + sw) - ix, min(y + h, sy + sh) - iy
                    if iw > 0 and ih > 0 and iw * ih >= 0.25 * max(1, w * h):
                        return True
                return False

            for frame_index, rows in zip(ocr_indexes, per_frame_rows):
                frame = frames[frame_index]
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
                for x, y, w, h in rows:
                    cx = x + w / 2.0
                    # Creator captions are readable rows in the central phone canvas. Tiny object
                    # labels and edge UI are not worth altering; large rows and news straps are.
                    row_share = (w * h) / float(max(1, width * height))
                    if (h < 4 or w < width * 0.04 or row_share > 0.42
                            or not (width * 0.04 <= cx <= width * 0.96)
                            or y < height * 0.03 or y > height * 0.97
                            or not overlaps_stable((x, y, w, h))):
                        continue
                    px, py = max(2, int(w * 0.025)), max(2, int(h * 0.18))
                    x0, y0 = max(0, x - px), max(0, y - py)
                    x1, y1 = min(width, x + w + px), min(height, y + h + py)
                    if x1 <= x0 or y1 <= y0:
                        continue
                    g = gray[y0:y1, x0:x1]
                    c = hsv[y0:y1, x0:x1]
                    k = max(3, int(round(h * 0.60)) | 1)
                    ker = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
                    sat = c[:, :, 1]
                    response = np.maximum(
                        np.maximum(cv2.morphologyEx(g, cv2.MORPH_TOPHAT, ker),
                                   cv2.morphologyEx(g, cv2.MORPH_BLACKHAT, ker)),
                        np.maximum(cv2.morphologyEx(sat, cv2.MORPH_TOPHAT, ker),
                                   cv2.morphologyEx(sat, cv2.MORPH_BLACKHAT, ker)))
                    white = (g >= 155) & (c[:, :, 1] <= 145)
                    yellow = ((c[:, :, 0] >= 10) & (c[:, :, 0] <= 48)
                              & (c[:, :, 1] >= 45) & (c[:, :, 2] >= 120))
                    # Seed the mask from the bright/yellow glyph fill only.  Including every
                    # high-contrast dark pixel inside an OCR row also selected hair, clothing and
                    # the entire black shadow/background behind captions; after dilation that
                    # became the large rectangular smear the remover was meant to avoid.  A small
                    # dilation around the coloured glyph seed already includes its dark outline.
                    # A caption is not always white or yellow. 「人生、変わった」 is RED, and red is neither
                    # bright enough for the `white` test (g >= 155) nor inside the yellow hue window, so
                    # the seed picked up only the white brackets around it: the mask ended at x=339 of a
                    # caption running to x=420, ProPainter rebuilt the left half, and the finished Short
                    # carried the right half of the sentence (measured on the konbini Short, 2026-09-05:
                    # 33% of the caption band was pure red before the fill, 24% after it).
                    # Any vividly coloured stroke inside a row the recogniser already read as text is
                    # caption ink - the hue does not matter, the saturation does.
                    vivid = (c[:, :, 1] >= 90) & (c[:, :, 2] >= 110)
                    local = (white | yellow | vivid) & (response >= 8)
                    glyph = local.astype(np.uint8) * 255
                    glyph = cv2.morphologyEx(
                        glyph, cv2.MORPH_CLOSE,
                        cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)))
                    # The dilation has to reach the glyph's dark OUTLINE, and an outline is
                    # thicker on bigger type. A fixed 5px grow covered the outline of an ordinary
                    # subtitle and left a grey ghost of every stroke on a 190px-wide title glyph -
                    # the fill removed the white body and the outline stayed. Scale it with the
                    # row instead; for normal caption rows this still evaluates to 5.
                    grow = max(5, int(round(min(w, h) * 0.06)) | 1)
                    glyph = cv2.dilate(
                        glyph, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (grow, grow)))
                    target = ocr_mask[y0:y1, x0:x1]
                    np.maximum(target, glyph, out=target)
                    ocr_rows += 1
            if ocr_rows and int(np.count_nonzero(ocr_mask)):
                # OCR also reads stable environmental labels (vending-machine prices, signs,
                # packaging). Creator captions form the strongest changing horizontal text band;
                # keep that band only. Without this focus the remover faithfully erased signs at
                # the top/edge of the frame while trying to clean the subtitle in the centre.
                # The band used to be a fixed 18% of frame height along the ROWS only, which is
                # true of a subtitle strap and false of everything else: it truncated multi-line
                # blocks, and on a vertical Japanese caption spanning 76% of the height it kept
                # one glyph out of five. Size each band from the boxes OCR actually found, then
                # keep whichever axis concentrates more of the ink.
                def strongest(profile, band):
                    band = max(20, min(len(profile), int(band)))
                    smoothed = np.convolve(profile, np.ones(band, np.float32), mode="same")
                    centre = int(np.argmax(smoothed))
                    start = max(0, min(len(profile) - band, centre - band // 2))
                    return start, start + band, float(profile[start:start + band].sum())

                total_ink = float(np.count_nonzero(ocr_mask))
                row_ink = np.count_nonzero(ocr_mask, axis=1).astype(np.float32)
                col_ink = np.count_nonzero(ocr_mask, axis=0).astype(np.float32)
                row_band = max(height * 0.18,
                               max((box[3] for box in stable_boxes), default=0) * 1.25)
                col_band = max(width * 0.18,
                               max((box[2] for box in stable_boxes), default=0) * 1.25)
                y0, y1, row_share = strongest(row_ink, row_band)
                x0, x1, col_share = strongest(col_ink, col_band)
                focused = np.zeros_like(ocr_mask)
                if col_share > row_share:
                    focused[:, x0:x1] = ocr_mask[:, x0:x1]
                else:
                    focused[y0:y1] = ocr_mask[y0:y1]
                # A band that keeps almost everything is not a filter, and the split above can
                # only ever discard ink, so fall back to the whole mask rather than a worse half.
                if float(np.count_nonzero(focused)) < 0.55 * total_ink:
                    focused = ocr_mask
                if int(np.count_nonzero(focused)):
                    return focused
    except Exception:
        # Keep the dependency-free temporal detector as a fallback.
        pass

    for frame in temporal_frames:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        total += cv2.morphologyEx(gray, cv2.MORPH_TOPHAT, kernel).astype(np.float32)
    total /= float(len(temporal_frames))

    threshold = max(6.0, float(np.percentile(total, 99.3)))
    mask = (total >= threshold).astype(np.uint8) * 255
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE,
                            cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5)))

    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    letters = []
    for index in range(1, count):
        x, y, w, h, area = stats[index]
        # A letter, not a wall panel or a hairline seam.
        if area >= 20 and h < height * 0.10 and w < width * 0.45:
            letters.append((index, y + h // 2, area))
    if not letters:
        return None

    # Keep only the rows where the ink actually concentrates: that is the caption.
    band_height = max(8, int(height * 0.025))
    weight = {}
    for _index, centre, area in letters:
        weight[centre // band_height] = weight.get(centre // band_height, 0) + area
    strongest = max(weight, key=weight.get)
    kept = np.zeros_like(mask)
    for index, centre, _area in letters:
        if abs(centre // band_height - strongest) <= 1:
            kept[labels == index] = 255
    persistent = None
    if int(np.count_nonzero(kept)):
        persistent = cv2.dilate(
            kept, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (dilate, dilate)))

    # A word-by-word TikTok caption is not temporally persistent: the line stays put, but the
    # actual glyphs change every few frames.  The temporal-mean pass above consequently found
    # only fragments (or the strongest single line), leaving readable words in real renders.
    # Use the scraper's local OCR only to locate caption-shaped rows, then build a GLYPH mask
    # inside those rows.  We deliberately do not paint the OCR rectangle; the mask remains the
    # organic shape of the letters so the inpaint does not produce the big smeared boxes the
    # user explicitly rejected.
    dynamic = np.zeros((height, width), dtype=np.uint8)
    try:
        import clip_scraper
        # Caption-shape detection is deliberately local and lightweight. Running the heavyweight
        # OCR recognizer per clip made a 30-second Short take tens of minutes to clean; the
        # recognizer is not needed here because no text has to be read, only its strokes found.
        sample_indexes = sorted({int((len(frames) - 1) * i / 7) for i in range(8)})
        for frame_index in sample_indexes:
            frame = frames[frame_index]
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
            boxes = clip_scraper._frame_caption_boxes(gray, hsv)
            for x, y, w, h in boxes:
                # Creator captions are compact screen overlays.  Very tall boxes are usually
                # signs or whole text slides and are handled by the normal rejection gate.
                if h <= 2 or h > height * 0.16 or w < width * 0.06:
                    continue
                px, py = max(2, int(w * 0.025)), max(2, int(h * 0.14))
                x0, y0 = max(0, x - px), max(0, y - py)
                x1, y1 = min(width, x + w + px), min(height, y + h + py)
                if x1 <= x0 or y1 <= y0:
                    continue
                g = gray[y0:y1, x0:x1]
                c = hsv[y0:y1, x0:x1]
                k = max(3, int(round(h * 0.55)) | 1)
                ker = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
                sat = c[:, :, 1]
                resp = np.maximum(
                    np.maximum(cv2.morphologyEx(g, cv2.MORPH_TOPHAT, ker),
                               cv2.morphologyEx(g, cv2.MORPH_BLACKHAT, ker)),
                    np.maximum(cv2.morphologyEx(sat, cv2.MORPH_TOPHAT, ker),
                               cv2.morphologyEx(sat, cv2.MORPH_BLACKHAT, ker)))
                # Include both bright outlined captions and dark lettering in white bubbles.
                local = resp >= max(20, int(np.percentile(resp, 91)))
                white = (g >= 175) & (c[:, :, 1] <= 110)
                yellow = ((c[:, :, 0] >= 13) & (c[:, :, 0] <= 43)
                          & (c[:, :, 1] >= 60) & (c[:, :, 2] >= 140))
                dark = g <= 155
                local |= (white | yellow) & (resp >= 12)
                # Dark letters in a white TikTok bubble are included only when they are strong
                # thin structures. Treating every dark pixel as ink masked hair, clothes and
                # buildings and created large smears instead of letter-shaped repairs.
                local |= dark & (resp >= max(24, int(np.percentile(resp, 88))))
                glyph = local.astype(np.uint8) * 255
                close = max(3, int(round(h * 0.12)) | 1)
                glyph = cv2.morphologyEx(
                    glyph, cv2.MORPH_CLOSE,
                    cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (close, close)))
                glyph = cv2.dilate(
                    glyph, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5)))
                target = dynamic[y0:y1, x0:x1]
                np.maximum(target, glyph, out=target)
    except Exception:
        # OCR is an enhancement.  The dependency-free temporal detector remains a valid path.
        pass

    if persistent is None and not int(np.count_nonzero(dynamic)):
        return None
    if persistent is None:
        return dynamic
    return np.maximum(persistent, dynamic)


# THROTTLES THAT COST TIME, NOT QUALITY.
#
# Measured while a render was running: the GPU sat at 100% with 10.7 of 11.3 GB of VRAM taken,
# and the same card draws the desktop - so the whole machine stuttered. The obvious knobs are the
# wrong ones: dropping the working resolution, cutting RAFT iterations or widening ref_stride all
# make the fill worse. These two do not touch the arithmetic at all.
#
# VRAM_FRACTION caps how much of the card PyTorch may allocate. The maths is unchanged and the
# output is bit-for-bit the same work; the allocator simply reuses a smaller pool and spills more
# often, which costs time and leaves the rest of the card for the compositor.
#
# Below-normal CPU priority costs nothing at all: it only decides who waits when the scheduler is
# oversubscribed, and that should be the background render rather than the desktop.
# HOW MUCH OF THE CARD THE FILL MAY ASK FOR. 0.55 was written while something else always held
# half the card; on this machine that is 6.2 of 11.3 GB, and at 540px the model wants more than
# that - so with the card almost empty every fill still died of an allowance, not of a shortage.
# The child narrows this to what is actually free (see the bootstrap), so a generous ceiling here
# costs nothing when the card is busy and is the difference between a fill and a rejection when
# it is not.
VRAM_FRACTION = float(os.environ.get("PROPAINTER_VRAM_FRACTION", "0.85"))
BELOW_NORMAL_PRIORITY_CLASS = 0x00004000


class OutOfMemory(RuntimeError):
    """ProPainter ran out of VRAM.

    A distinct type on purpose. The RuntimeError text carries only the LAST 400
    characters of the child stderr, and "CUDA out of memory" opens that message - so it
    is exactly the part that gets cut off. A caller testing the text therefore never saw
    an OOM and silently skipped its retry (measured twice: once for the subvideo ladder,
    once for the width ladder).
    """


class Cancelled(Exception):
    """The user pressed Cancel. Distinct from a failure, so the caller stops instead of falling
    back to the CPU path and carrying on with the next clip."""


def fill_width_now():
    """The width to reconstruct at. 360 unless the environment says otherwise.

    This was briefly made to climb to 540 whenever the card looked empty, on the theory that the
    smeared patches came from reconstructing at 360 and scaling the result back to 1080. The
    theory did not survive its own measurement. Scene 04 of the eating-walk Short, the one case
    known to have shipped a caption, filled three times with everything else identical:

        360px   clean, caption gone      74s
        450px   clean, caption gone      88s
        540px   OUT OF MEMORY, original restored, caption still there   104s

    360 was enough, 450 looked no different, and 540 failed on a card with 10 GB free. What
    actually separates a good fill from a bad one on this machine is what ELSE is on the card -
    the good results of 2026-08-22 were made with it to themselves, the bad renders while a local
    LLM and ComfyUI held 5-8 of its 11 GB - and that is not something a width can fix.

    So: the default is the width that is measured to work, and the ladder above it (subvideo
    length, then the attention window) absorbs pressure without touching the picture.
    PROPAINTER_FILL_WIDTH overrides it for an experiment.
    """
    try:
        return max(160, int(os.environ.get("PROPAINTER_FILL_WIDTH", "") or FILL_WIDTH))
    except (TypeError, ValueError):
        return FILL_WIDTH


def _fill_piece_with_width_ladder(source, mask_small, frames, work, ffmpeg, seconds=None,
                                  cancel_check=None):
    """The full width, and the smaller one only if the card refused the full one.

    `_run_propainter` spends the temporal levers first and raises OutOfMemory when it has none
    left. That is the only signal that justifies giving up picture, so it is the only thing that
    re-encodes the clip smaller.
    """
    first = fill_width_now()
    try:
        return _run_propainter_at_width(source, mask_small, frames, work, ffmpeg,
                                        seconds=seconds, cancel_check=cancel_check, width=first)
    except OutOfMemory:
        if first <= FILL_WIDTH:
            raise
    return _run_propainter_at_width(source, mask_small, frames, work, ffmpeg, seconds=seconds,
                                    cancel_check=cancel_check, width=FILL_WIDTH)


def _run_propainter_at_width(source, mask_small, frames, work, ffmpeg, seconds=None,
                             cancel_check=None, width=None):
    """Run the fill at the best width the card can take right now; None when even that fails.

    Resolution is the LAST thing given up. It is the only lever of the four that the viewer can
    see - it enters memory quadratically, so halving it is a quarter of the footprint, but a fill
    computed at 360 and scaled back to 1080 is the smeared slab this whole thread was about. The
    temporal levers (subvideo_length, then the attention window) are spent first, inside
    _run_propainter; only when those are exhausted does the clip come back here to be re-encoded
    smaller. There is no CPU fallback below that - a fill that cannot run rejects the clip.
    """
    RETRY_WIDTH = int(width or fill_width_now())
    try:
        small = work / "small_retry.mp4"
        args = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(source)]
        if seconds is not None:
            try:
                args += ["-t", f"{max(0.10, float(seconds)):.3f}"]
            except (TypeError, ValueError):
                pass
        done = subprocess.run(
            args + ["-vf", f"scale={RETRY_WIDTH}:-2", "-an", "-c:v", "libx264",
                    "-crf", "16", "-preset", "veryfast", str(small)],
            capture_output=True, timeout=300, stdin=subprocess.DEVNULL)
        if done.returncode or not small.is_file():
            return None
        retry_frames = _read_frames(small)
        if len(retry_frames) < 4:
            return None
        # Use BOTH masks. Re-detecting at the new scale exists because a glyph-tight mask scaled
        # down by a third stops covering its own glyph edges - but detection is also weaker at
        # 360px: on a vertical caption it read only the lower half of the text, so the fill
        # rebuilt two glyphs and left two standing. Resize-and-dilate restores the lost edges,
        # and the union of the two can only ever cover more than either alone, which is the safe
        # direction for a mask that decides what gets erased.
        retry_height, retry_width = retry_frames[0].shape[:2]
        carried = cv2.dilate(
            cv2.resize(mask_small, (retry_width, retry_height),
                       interpolation=cv2.INTER_NEAREST),
            cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)))
        retry_mask = detect_caption_mask(retry_frames)
        retry_mask = carried if retry_mask is None else np.maximum(retry_mask, carried)
        mask_dir = work / "frame_masks_retry"
        # Same frame/mask count rule as the first attempt - the retry clip is the same length.
        if _write_frame_masks(small, retry_mask, mask_dir) < 4:
            return None
        # Start at the full temporal window and let the ladder inside step down. It used to
        # start at 24 because this whole function WAS the out-of-memory retry; now it is the
        # normal path, and beginning half-throttled throws away quality nobody asked to lose.
        return _run_propainter(small, mask_dir, work / "out_retry",
                               cancel_check=cancel_check, subvideo_length=40)
    except Cancelled:
        raise
    except OutOfMemory:
        # MUST ESCAPE. The blanket `except Exception` below swallowed it, so the width ladder in
        # the caller never fired once: an out-of-memory at 540 came back as a plain None and the
        # clip was rejected without the smaller attempt ever being made. Measured on the five
        # captioned scenes of the eating-walk Short: four fills, four "ran out of VRAM", zero
        # retries at 360.
        raise
    except Exception:                                                   # noqa: BLE001
        return None


_WORKER = {"proc": None, "jobs": None, "seq": 0}


def _worker_alive():
    proc = _WORKER["proc"]
    return proc is not None and proc.poll() is None


def stop_propainter_worker():
    """Let the oven cool. Called when a render ends; the child also dies with the parent."""
    proc = _WORKER["proc"]
    _WORKER["proc"] = None
    if proc is not None and proc.poll() is None:
        try:
            proc.kill()
            proc.wait(timeout=20)
        except Exception:                                               # noqa: BLE001
            pass
    jobs = _WORKER["jobs"]
    _WORKER["jobs"] = None
    if jobs:
        shutil.rmtree(jobs, ignore_errors=True)


def _start_worker(mem_fraction, timeout=180):
    """Start the long-lived filler and wait for it to say it has the models on the card."""
    script = Path(__file__).resolve().parent / "propainter_worker.py"
    if not script.is_file():
        return False
    jobs = Path(tempfile.mkdtemp(prefix="pp_jobs_"))
    env = dict(os.environ)
    env.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")
    log = jobs / "worker_stderr.log"
    handle = open(log, "w", encoding="utf-8", errors="replace")
    proc = subprocess.Popen(
        [os.sys.executable, str(script), str(jobs), str(PROPAINTER_DIR), str(mem_fraction)],
        cwd=str(PROPAINTER_DIR), stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
        stderr=handle, text=True,
        creationflags=(BELOW_NORMAL_PRIORITY_CLASS if os.name == "nt" else 0), env=env)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if (jobs / "ready").exists():
            _WORKER.update({"proc": proc, "jobs": jobs, "seq": 0})
            return True
        if proc.poll() is not None:
            break
        time.sleep(0.25)
    try:
        proc.kill()
    except Exception:                                                   # noqa: BLE001
        pass
    shutil.rmtree(jobs, ignore_errors=True)
    return False


def _run_in_worker(args, out_dir, clip, timeout, cancel_check, mem_fraction):
    """Hand one fill to the warm process. Returns the produced file, or None to fall back.

    The handover is a file in a directory, not stdin: stdin is the lifeline the child watches to
    die with its parent, and a second reader on it deadlocked the next import on Windows once
    already (see the note in _run_propainter).
    """
    if not _worker_alive() and not _start_worker(mem_fraction):
        return None
    jobs = _WORKER["jobs"]
    _WORKER["seq"] += 1
    name = "%04d" % _WORKER["seq"]
    done = jobs / (name + ".done")
    tmp = jobs / (name + ".tmp")
    tmp.write_text(json.dumps(args), encoding="utf-8")
    os.replace(tmp, jobs / (name + ".job"))
    deadline = time.monotonic() + timeout
    while True:
        if done.exists():
            break
        if not _worker_alive():
            return None                                    # died: the caller retries cold
        if cancel_check and cancel_check():
            stop_propainter_worker()
            raise Cancelled("cancelled during caption removal")
        if time.monotonic() > deadline:
            stop_propainter_worker()
            raise RuntimeError("ProPainter timed out")
        time.sleep(0.25)
    try:
        answer = json.loads(done.read_text(encoding="utf-8"))
    except Exception:                                                   # noqa: BLE001
        answer = {"ok": False, "error": "unreadable answer"}
    done.unlink(missing_ok=True)
    produced = Path(out_dir) / Path(clip).stem / "inpaint_out.mp4"
    if answer.get("ok") and produced.is_file() and produced.stat().st_size > 4096:
        return produced
    _WORKER["last_error"] = str(answer.get("error") or "")
    return None


def _run_propainter(clip, mask_png, out_dir, timeout=1800, cancel_check=None,
                    subvideo_length=40, mem_fraction=None, neighbor_length=10):
    """Drive the reference implementation as a subprocess and hand back its result file.

    Their script is executed through runpy rather than called directly, so the VRAM cap can be set
    on the torch context BEFORE their code allocates anything - without editing their file, which
    is a gitignored third-party checkout that a re-clone would overwrite.
    """
    # DIE WITH THE PARENT. Polling turned out to be only half the story: when the app itself goes
    # away - a crash, the task manager, a plain shutdown - nobody is left to send the cancel, and
    # the child kept a whole GPU busy on a fill whose output nowhere collected. One was measured
    # grinding for 25 minutes after its parent had gone, holding 10.6 of 11.3 GB.
    #
    # No PID polling: the parent holds the write end of this pipe open for as long as it lives, so
    # the child sees EOF the instant it dies, whatever killed it. Reading in a daemon thread keeps
    # the inference itself untouched.
    #
    # The lifeline is armed BEFORE torch is imported, and that ordering is the whole point. The
    # first version imported torch in the same statement that created the thread, so the thread
    # only existed once the import had returned - and `import torch` is exactly where these
    # children hang. One sat for eight hours holding 55% of the card with 4.2 seconds of CPU to
    # its name and no output, long after its parent was gone, because the watchdog meant to kill
    # it had never started.
    # TWO watchdogs, both armed before torch. The stdin-EOF thread is kept, but measured on a
    # live orphan it never fired on Windows: the parent was gone and read(1) still blocked, so
    # the child sat holding the card. The second watchdog is deterministic on Windows - it asks
    # the kernel for the parent's process HANDLE and sleeps in WaitForSingleObject until the
    # parent dies, whatever killed it.
    # ONE watchdog, the kernel one. There used to be a second - a thread blocked in
    # sys.stdin.buffer.read(1) waiting for pipe EOF - and bisecting a live hang landed on it:
    # with that thread running, the very next import in the main thread deadlocks on Windows
    # (even a bare print never executes), which is why every ProPainter child of the night sat
    # at ~1s of CPU forever and the pipeline fell back to per-frame inpainting. It also never
    # fired: an orphan was observed with its parent long dead and read(1) still blocked.
    mem_fraction = float(mem_fraction if mem_fraction is not None else VRAM_FRACTION)
    bootstrap = (
        "import os, sys, threading;"
        "import ctypes;"
        "k = ctypes.windll.kernel32;"
        "h = k.OpenProcess(0x00100000, False, os.getppid());"
        "w = threading.Thread("
        "  target=lambda: (k.WaitForSingleObject(h, 0xFFFFFFFF), os._exit(3)), daemon=True);"
        "h and w.start();"
        "import runpy, torch;"
        # The fraction adapts to what is actually FREE. A fixed 0.55 of the card asked for
        # 6.05 GiB while two kernel-stuck zombies and the desktop held all but 5.49 - so every
        # real-size clip died OOM inside its own allowance (the stderr capture caught it:
        # "5.49 GiB free, 6.05 GiB allowed"). 92% of free keeps a margin for the allocator.
        "free, total = torch.cuda.mem_get_info();"
        f"torch.cuda.set_per_process_memory_fraction(min({mem_fraction}, free * 0.92 / total));"
        "sys.argv = sys.argv[1:];"
        "runpy.run_path('inference_propainter.py', run_name='__main__')"
    )
    args = [
        "-i", str(Path(clip).resolve()),
        "-m", str(Path(mask_png).resolve()),
        "-o", str(Path(out_dir).resolve()),
        "--fp16", "--mask_dilation", "4", "--subvideo_length", str(int(subvideo_length)),
        # The attention window. Their default is 10; it is one of the levers that
        # decides how many frames sit in memory at once, and unlike the working
        # resolution it costs the viewer nothing until it gets small.
        "--neighbor_length", str(int(neighbor_length)),
    ]
    # THE OVEN STAYS HOT. A render cleans several clips, and each one used to pay for loading
    # PyTorch, initialising CUDA and reading three sets of weights off disk - measured on the
    # eating-walk render at about 1.4 minutes per clip for 70-90 frames, of which the fill is
    # seconds. The worker keeps the models on the card between clips. If it will not start, or
    # dies, or answers badly, the one-shot path below runs exactly as before.
    try:
        produced = _run_in_worker(args, out_dir, clip, timeout, cancel_check, mem_fraction)
    except (Cancelled, RuntimeError):
        raise
    except Exception:                                                   # noqa: BLE001
        produced = None
    if produced is not None:
        return produced
    command = [os.sys.executable, "-c", bootstrap, "inference_propainter.py"] + args
    flags = BELOW_NORMAL_PRIORITY_CLASS if os.name == "nt" else 0
    # POLL, DO NOT BLOCK. subprocess.run() waits for the child no matter what, so a cancelled job
    # sat inside a minute-long fill per clip and only stopped once every clip was done - the user
    # pressed Cancel and watched it keep going. Polling lets the flag reach the child.
    # stdin stays open and unwritten on purpose: it is the lifeline the child watches.
    # ProPainter/tqdm writes continuous progress output. Leaving stdout/stderr as unread PIPEs
    # fills the Windows pipe buffer and deadlocks inference (observed hanging for 15+ minutes on
    # a 53-frame clip). We only need the exit status here, so drain neither into a bounded pipe.
    # stderr goes to a bounded file, not DEVNULL: "exit 1" with no text cost a night of
    # guessing (was it OOM? a bad argument? missing weights?). stdout stays discarded - that is
    # the tqdm stream whose unread pipe once deadlocked inference for 15+ minutes.
    stderr_path = Path(out_dir).parent / "propainter_stderr.log"
    stderr_handle = open(stderr_path, "w", encoding="utf-8", errors="replace")
    child_env = dict(os.environ)
    # Every logged OOM carried ~530 MiB "reserved by PyTorch but unallocated" - fragmentation,
    # and the CUDA message itself names the remedy. Expandable segments let the allocator grow
    # blocks instead of stranding half a gigabyte it cannot reuse.
    child_env.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")
    process = subprocess.Popen(command, cwd=str(PROPAINTER_DIR), stdin=subprocess.PIPE,
                               stdout=subprocess.DEVNULL, stderr=stderr_handle, text=True,
                               creationflags=flags, env=child_env)
    deadline = time.monotonic() + timeout
    while process.poll() is None:
        if cancel_check and cancel_check():
            process.kill()
            process.wait(timeout=30)
            raise Cancelled("cancelled during caption removal")
        if time.monotonic() > deadline:
            process.kill()
            process.wait(timeout=30)
            raise RuntimeError("ProPainter timed out")
        time.sleep(0.5)
    result = subprocess.CompletedProcess(
        command, process.returncode, *process.communicate())
    try:
        stderr_handle.close()
    except OSError:
        pass
    produced = Path(out_dir) / Path(clip).stem / "inpaint_out.mp4"
    if produced.is_file() and produced.stat().st_size > 4096:
        return produced
    try:
        stderr_handle.close()
        stderr_text = stderr_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        stderr_text = ""
    tail = stderr_text[-400:].strip()
    # A shorter subvideo halves the temporal window ProPainter holds in memory at once. On a
    # card where the free half is smaller than the model wants, that is the difference between
    # a clean rebuild and the OpenCV smudge fallback - worth one retry, never a loop.
    # Checked against the WHOLE stderr: "out of memory" opens the CUDA message, and the
    # 400-char tail cuts exactly that part off - the first version of this retry never fired.
    # The retry halves the temporal window only. Lowering the fraction as well was tried and
    # measured: 0.40 (4.40 GiB) OOMs even on a small clip - the model wants ~5 GiB regardless.
    # WHAT TO GIVE UP FIRST, WHEN THE CARD SAYS NO.
    #
    #   resolution        quadratic - half the width is a quarter of the memory, and it is the
    #                     one lever the viewer sees: a 360px fill scaled back to 1080 is the
    #                     smeared slab this whole thread was about
    #   subvideo_length   linear, invisible; their default is 80 and we already start at 40
    #   neighbor_length   the attention window; costs nothing until it gets small
    #   fp16              always on
    #
    # So the temporal levers go first and the picture goes last. Dropping the resolution is not
    # done here at all - it is raised as OutOfMemory and the caller re-encodes, because the width
    # is baked into the copy that was handed to us.
    if "out of memory" in stderr_text.lower():
        if subvideo_length > 24:
            return _run_propainter(clip, mask_png, out_dir, timeout=timeout,
                                   cancel_check=cancel_check, subvideo_length=24,
                                   mem_fraction=mem_fraction, neighbor_length=neighbor_length)
        if subvideo_length > 16:
            return _run_propainter(clip, mask_png, out_dir, timeout=timeout,
                                   cancel_check=cancel_check, subvideo_length=16,
                                   mem_fraction=mem_fraction, neighbor_length=neighbor_length)
        if neighbor_length > 6:
            return _run_propainter(clip, mask_png, out_dir, timeout=timeout,
                                   cancel_check=cancel_check, subvideo_length=subvideo_length,
                                   mem_fraction=mem_fraction, neighbor_length=6)
    message = (f"ProPainter produced no output (exit {result.returncode})"
               + (f": {tail}" if tail else ""))
    if "out of memory" in stderr_text.lower():
        raise OutOfMemory(message)
    raise RuntimeError(message)


def remove_caption_regions(path, ffmpeg, seconds=None, status_cb=None, info=None,
                           allow_gpu=None, cancel_check=None, start=0.0):
    """Remove a burned-in caption from ``path`` IN PLACE. Returns 1 when it did, 0 when it did not.

    Everything except the mask detection runs inside ffmpeg. The first version decoded every
    full-resolution frame into Python, composited there and piped the result back out - 335MB
    through a pipe for a two-second clip, which turned a 15-second fill into an 85-second job.
    ffmpeg scales the filled copy back up, keys it through the mask and lays it over the
    untouched original, so the picture outside the caption never leaves the source encoder.

    ``allow_gpu`` decides whether ProPainter may run. It is a THREE-state opt-out, not a default-
    off opt-in: ``False`` refuses (the background scrape probe and the render-only harnesses pass
    it deliberately, because the GPU belongs to an explicit render), ``True`` allows, and the
    default ``None`` means "nobody expressed a preference, so clean the clip". It used to default
    to ``False``, and four of the seven call sites never passed anything - so calling this
    function did nothing at all and said "ProPainter is unavailable" while ProPainter was sitting
    there working, which is how burned-in creator text kept reaching finished videos. The same
    mistake was already made once with CAPTION_REMOVER_PROPAINTER (see below): a permission gate
    that nobody sets is an off switch, not a policy. ProPainter is the ONLY inpainting backend:
    a failed/too-expensive removal rejects the clip instead of replacing it with a visibly worse
    CPU approximation.

    ``info`` is filled with ``refused``/``covered`` when the clip is too covered in text to be
    worth saving - "nothing to remove" and "too much to remove" are different answers, and a
    caller that cannot tell them apart uses a text slide as if it were clean footage.
    """
    if cv2 is None or np is None or not ffmpeg:
        return 0
    path = Path(path)
    work = Path(tempfile.mkdtemp(prefix="capfill_"))
    try:
        small = work / "small.mp4"
        # THE OFFSET, NOT ONLY THE LENGTH. This read `seconds` from the HEAD of the file while
        # the edit shows a window that starts at seedance_start_trim - 36.4s into the source for
        # scene 01 of the eating-walk Short. So the mask was built on second 0, ProPainter
        # rebuilt second 0, and the composite - which keys the filled copy in by timestamp - laid
        # second 0 over second 36. That is what the "smear" was: not a bad fill, a good fill of
        # the wrong moment. Measured on that clip: the pasted block is the red apartment building
        # standing at the head of the clip, printed over the sky at 36s.
        window_at = max(0.0, float(start or 0.0))
        source_args = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error"]
        if window_at > 0.001:
            source_args += ["-ss", f"{window_at:.4f}"]
        source_args += ["-i", str(path)]
        # Timeline scenes often use only 2-4 seconds from a much longer downloaded TikTok. The
        # old remover ignored ``seconds`` and sent the entire source (sometimes 60+ seconds) into
        # ProPainter even though the renderer would never show it. Process exactly the used span.
        if seconds is not None:
            try:
                source_args += ["-t", f"{max(0.10, float(seconds)):.3f}"]
            except (TypeError, ValueError):
                pass
        done = subprocess.run(
            source_args + ["-vf", f"scale={WORK_WIDTH}:-2", "-an",
             "-c:v", "libx264", "-crf", "16", "-preset", "veryfast", str(small)],
            capture_output=True, timeout=300, stdin=subprocess.DEVNULL)
        if done.returncode or not small.is_file():
            return 0

        frames = _read_frames(small)
        if len(frames) < 4:
            return 0
        mask_small = detect_caption_mask(frames)
        if mask_small is None:
            return 0

        coverage = float((mask_small > 32).mean())
        if coverage > MAX_COVERAGE:
            if info is not None:
                info["refused"] = True
                info["covered"] = coverage
            _log(status_cb, f"Caption removal: {coverage:.0%} of the frame is text - this is a "
                            "slide, not a captioned clip; leaving it alone.")
            return 0

        mask_png = work / "mask.png"
        cv2.imwrite(str(mask_png), mask_small)

        # Three separate reasons, three separate sentences. One shared "unavailable" message for
        # all of them is what hid this for months: the sentence blamed the backend for a decision
        # the caller had made.
        if allow_gpu is False:
            _log(status_cb, "Caption removal skipped: the caller disabled the GPU path "
                            "(allow_gpu=False); the caption is still on this clip.")
            if info is not None:
                info["skipped"] = "allow_gpu=False"
            return 0
        if os.environ.get("CAPTION_REMOVER_PROPAINTER", "1") == "0":
            _log(status_cb, "Caption removal skipped: CAPTION_REMOVER_PROPAINTER=0 kill switch "
                            "is set; the caption is still on this clip.")
            if info is not None:
                info["skipped"] = "kill-switch"
            return 0
        reason = propainter_unavailable_reason()
        if reason:
            _log(status_cb, f"Caption removal skipped: ProPainter is the required backend and "
                            f"cannot run because {reason}.")
            if info is not None:
                info["skipped"] = reason
            return 0
        filled = None
        # THE GPU ONLY SPINS UP WHEN THE USER PRESSES RENDER. ProPainter takes the whole card -
        # measured at 100% utilisation with 10.7 of 11.3 GB of VRAM - and the same card draws the
        # desktop, so running it during a scrape or a project open made the machine stutter for
        # work nobody was waiting on. Every other caller skips cleanup and the caller must reject
        # or replace the captioned source; there is intentionally no CPU inpainting path.
        # ProPainter accepts a folder of frame-wise masks. A single union mask made it rebuild a
        # broad horizontal band and damaged hands behind changing captions; frame-wise masks let
        # optical flow reconstruct only the glyph pixels visible at that instant.
        # The env var is a KILL SWITCH, not an opt-in. As an opt-in it was set by exactly one
        # batch script and never by the app, so `allow_gpu` decided nothing and ProPainter had
        # never run inside a real run - every Clip Short silently got the CPU fill while the
        # pipeline logged "inpainted, not blurred". Set CAPTION_REMOVER_PROPAINTER=0 to force
        # disabled. A busy GPU, driver issue or failed reconstruction must reject the source.
        # The parent's own CUDA tenants are the reason every fill of the night went OOM:
        # whisper's cached weights plus torch's arena held ~5.8 GiB while the child needed
        # ~6. Alignment reloads transparently on its next call.
        try:
            import voice_align
            voice_align.release_gpu_models()
        except Exception:                                               # noqa: BLE001
            pass
        runs = None
        filled = None
        try:
            runs = _text_frame_runs(small, mask_small)
            outcome = _fill_in_pieces(small, mask_small, work, ffmpeg, runs=runs,
                                      status_cb=status_cb, cancel_check=cancel_check)
            if outcome is not None:
                filled, filled_frames, total_frames = outcome
                _log(status_cb, f"Caption removal: ProPainter rebuilt the caption area from "
                                f"neighbouring frames ({coverage:.1%} of the frame, "
                                f"{filled_frames} of {total_frames} frames).")
        except Cancelled:
            raise
        except OutOfMemory:
            # the width that actually ran, not the constant - the message said 360 while the
            # attempt had been at 540, which sent me looking in the wrong place
            _log(status_cb, f"Caption removal: ProPainter ran out of VRAM; "
                            "rejecting clip.")
            if info is not None:
                info["failed"] = "the GPU ran out of memory"
            filled = None
        except Exception as exc:      # noqa: BLE001 - source must be rejected if ProPainter fails
            # The tail of the message, not the head: the head is "ProPainter produced no output
            # (exit 1)" plus the top of a runpy traceback, and [:200] cut the message off exactly
            # before the line that says what went wrong. An IndexError in their mask loop read
            # as "unavailable" for months because of it.
            _log(status_cb, f"Caption removal: ProPainter failed this run "
                            f"({type(exc).__name__}: {str(exc)[-300:]}); rejecting this captioned clip.")
            if info is not None:
                info["failed"] = f"{type(exc).__name__}: {str(exc)[-160:]}"
            filled = None
        if filled is None:
            # WHY it came back empty, not just that it did. Every one of these paths returned 0
            # with an empty `info`, and the caller writes "no removable text found" for an empty
            # info - so a crashed ProPainter, an out-of-memory GPU and a clip with genuinely no
            # text all reached the saved config as the same sentence. That sentence was on 11 of
            # the 15 scenes of the eating-walk Short and it is the reason nobody could tell which
            # of them had actually been looked at.
            if info is not None:
                info.setdefault("failed", "the fill produced no output")
            return 0

        # Look INSIDE the mask before keying anything in. A fill that came back as a hole is
        # worse than the caption it replaces, and nothing downstream can tell: a black blob
        # reads as less text than the words did, so the residual gate waves it through.
        _fill_frames = _read_frames(filled)
        # A pasted patch: no text left in it, not dark, and unmistakable on screen. Measured on
        # the couples Short (2026-09-05): a flat skin-pink rectangle across a pair of hands where
        # the caption had been, and a red slab over a shirt - both shipped, because every gate
        # here was looking for text or for darkness.
        # MEASURED AND REPORTED, NOT YET GATING. Trying to judge these two shipped patches from
        # the outside failed: rebuilding the mask from the source afterwards does not line up with
        # the fill (the pass cuts a window with a lead-in first), and both candidate measures came
        # back meaningless - texture 1.06 and 0.78, boundary step 0.17 and 0.03, i.e. indis-
        # tinguishable from a good fill. In here the frames, the fill and the mask ARE aligned, so
        # the number is worth recording; it decides nothing until a real run shows what a slab
        # actually reads as against a clean rebuild. A gate that throws away good clips on a
        # guessed threshold is worse than the artefact it hunts.
        texture = patch_flatness(_fill_frames, mask_small)
        _log(status_cb, f"Caption removal: rebuilt-area detail {texture:.2f} of its surroundings.")
        if info is not None:
            info["fill_texture"] = round(texture, 3)
        src_mean, fill_mean, black_share = fill_damage(frames, _fill_frames, mask_small)
        if fill_mean < 20.0 or (black_share > 0.35 and src_mean > 40.0) or fill_mean < src_mean * 0.45:
            _log(status_cb, f"Caption removal: the rebuilt area came back as a hole "
                            f"(masked brightness {fill_mean:.0f} against {src_mean:.0f} in the "
                            f"source, {black_share:.0%} of it black); rejecting this clip.")
            if info is not None:
                info["hole"] = {"source": round(src_mean, 1), "fill": round(fill_mean, 1),
                                "black": round(black_share, 3)}
            return 0

        tmp = path.with_name(path.stem + "_nocap" + path.suffix)
        # The overlay is switched off outside the stretches that carry text, so a frame with no
        # caption on it comes out of the base layer untouched instead of having a rebuilt patch
        # laid over it. Without this the mask is a single still image applied to every frame -
        # the fill's own smear gets keyed in even where there was never anything to remove.
        # AND NEVER OUTSIDE THE WINDOW THAT WAS PROCESSED. `runs` is None whenever the clip
        # cannot be split honestly, and then this was left empty - so the overlay stayed ON for
        # the WHOLE file. A 2.4s fill was therefore keyed into all 45 seconds of the source:
        # measured on scene 01 of the eating-walk Short, the rebuilt block from second 0 is
        # printed over second 18 and second 37, where there was never a caption to remove. That
        # is what the owner was seeing as smearing. The window gate is unconditional now; the
        # per-run gate refines it when the runs are known.
        window_end = window_at + max(0.10, float(seconds or 0.0)) if seconds else None
        enable = ""
        if runs:
            fps = _clip_fps(small)
            enable = ":enable='" + "+".join(
                # `runs` are frame indices INSIDE the window; the overlay is switched on in the
                # file's own clock, so both ends carry the window offset. (The loop variable
                # shadows the `start` parameter - window_at is captured above on purpose.)
                f"between(t,{max(0.0, window_at + (r0 - 0.5) / fps):.4f},"
                f"{window_at + (r1 + 0.5) / fps:.4f})"
                for r0, r1 in runs) + "'"
        elif window_end is not None:
            enable = f":enable='between(t,{max(0.0, window_at - 0.05):.4f},{window_end + 0.05:.4f})'"
        graph = ("[0:v]format=yuv444p[base];"
                 "[1:v]scale=rw:rh:flags=bicubic,format=yuva444p[fill];"
                 "[2:v]scale=rw:rh:flags=bicubic,format=gray[m];"
                 "[fill][m]alphamerge[keyed];"
                 "[base][keyed]overlay=format=yuv444:eof_action=pass:repeatlast=0" + enable + "[v]")
        probe = _dimensions(path, ffmpeg)
        if not probe:
            return 0
        graph = graph.replace("rw", str(probe[0])).replace("rh", str(probe[1]))
        done = subprocess.run(
            [ffmpeg, "-nostdin", "-y", "-hide_banner", "-loglevel", "error",
             "-i", str(path)]
            + (["-itsoffset", f"{window_at:.4f}"] if window_at > 0.001 else [])
            + ["-i", str(filled), "-i", str(mask_png),
             "-filter_complex", graph, "-map", "[v]", "-map", "0:a?",
             "-pix_fmt", "yuv420p", "-c:v", "libx264", "-crf", "18", "-preset", "veryfast",
             "-c:a", "copy", "-movflags", "+faststart", str(tmp)],
            capture_output=True, timeout=600, stdin=subprocess.DEVNULL)
        if done.returncode or not tmp.is_file() or tmp.stat().st_size < 4096:
            tmp.unlink(missing_ok=True)
            return 0
        keep = work / ("original" + path.suffix)
        shutil.copy2(str(path), str(keep))
        os.replace(str(tmp), str(path))

        # Look at what was produced instead of trusting that the fill worked. ProPainter has
        # nothing to borrow where the background behind a caption is never revealed, and it then
        # leaves the text standing inside a smear - measured at 2.1% residual on a clip whose
        # caption sat over a moving crowd, against 0.0% on two clips that came out perfectly
        # clean. A clip that still reads as captioned has not been cleaned, and saying so lets
        # the caller drop it while alternatives still exist. The check costs a few seconds
        # against a fill that costs ninety.
        # AT THE SECONDS THE SCENE SHOWS. This measured from the head of the FILE while the fill
        # ran over the window the edit uses, so a clip cleaned at second 11 was checked at second
        # 0 and its leftover text never counted. Measured on the school-rules Short (2026-09-05):
        # three of six cleaned clips still read 0.027, 0.042 and 0.020 in the finished video, all
        # of them passing this gate.
        residual = caption_coverage(str(path), ffmpeg, seconds=seconds or 2.5,
                                    start=float(start or 0.0))
        if residual > 0.015 and residual > 0.10 * coverage:
            # copy, not os.replace: the backup lives in the temp work directory, which is on
            # another drive from the project on Windows, and os.replace cannot cross drives
            # (WinError 17). Measured during a real render, where it turned "restore the
            # original" into "caption removal failed".
            shutil.copy2(str(keep), str(path))
            _log(status_cb, f"Caption removal: the fill left {residual:.1%} of the frame still "
                            f"reading as text (from {coverage:.1%}); restoring the original and "
                            f"rejecting this clip.")
            if info is not None:
                info["residual"] = {"left": round(residual, 4), "from": round(coverage, 4)}
            return 0

        # AND THE THIRD FAILURE, WHICH EVERY GATE ABOVE IS BLIND TO: the text is gone and so is
        # the picture that was behind it, dragged sideways. The residual check asks whether TEXT
        # is left and a smear has none; fill_damage asks whether the area went dark and it did
        # not; patch_flatness cannot separate them at all (0.741 on a wrecked night market
        # against 0.743 on a clean plate of gyoza).
        #
        # HERE, not up beside the other two, and that placement is the whole point. Measured on
        # ProPainter's raw output the same four clips read 0.844 / 0.740 / 0.779 / 0.991 - the
        # clean one at 0.779 lands under the floor and a smear at 0.844 above one. The composite
        # keys the fill in only where the caption runs and at full resolution, so it is a
        # different picture from ProPainter's own frames, and the threshold was calibrated on
        # twelve finished files. A number measured on one thing does not carry a threshold
        # validated on another.
        _note = {}
        direction = fill_direction(_window_frames(str(keep), ffmpeg, seconds, start),
                                   _window_frames(str(path), ffmpeg, seconds, start), note=_note)
        if info is not None:
            info["fill_direction"] = round(direction, 3)
            if _note.get("unmeasured"):
                info["fill_direction_unmeasured"] = _note["unmeasured"]
        if _note.get("unmeasured"):
            _log(status_cb, "Caption removal: could not check the rebuild for smearing "
                            f"({_note['unmeasured']}); letting it through.")
        else:
            _log(status_cb, f"Caption removal: the rebuilt area keeps {direction:.2f} of the "
                            f"surrounding picture's structure (recorded, not a verdict).")
        return 1
    except Cancelled:
        raise
    except Exception as exc:      # noqa: BLE001 - a failed clean-up must not kill the render
        _log(status_cb, f"Caption removal failed: {type(exc).__name__}: {exc}")
        if info is not None:
            info["failed"] = f"{type(exc).__name__}: {str(exc)[-160:]}"
        return 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


def fill_damage(source_frames, fill_frames, mask_small):
    """How much darker the fill made the masked area. Returns (source_mean, fill_mean, black_share).

    ProPainter reconstructs the masked pixels from what neighbouring frames reveal. Where a
    caption sits on something that is NEVER uncovered - a product held still in front of the
    camera, a locked-off shelf - there is nothing to borrow, and what comes back is not a smear
    but a hole: a solid dark silhouette in the shape of the union mask, keyed straight into the
    finished Short (measured on the konbini Short, 2026-09-05: a black blob across the middle of
    the frame where the Japanese caption had been).

    The residual check downstream cannot see this, because a black hole reads as LESS text than
    the caption did. So the fill is compared with the source it replaces, inside the mask only.
    """
    if cv2 is None or np is None or not source_frames or not fill_frames:
        return (0.0, 0.0, 0.0)
    m = mask_small > 32
    if not m.any():
        return (0.0, 0.0, 0.0)
    def grey(frame):
        g = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        if g.shape != m.shape:
            g = cv2.resize(g, (m.shape[1], m.shape[0]), interpolation=cv2.INTER_AREA)
        return g
    n = min(len(source_frames), len(fill_frames), 24)
    step = max(1, min(len(source_frames), len(fill_frames)) // n)
    src_vals, fill_vals, black = [], [], []
    for i in range(0, min(len(source_frames), len(fill_frames)), step):
        sg, fg = grey(source_frames[i]), grey(fill_frames[i])
        src_vals.append(float(sg[m].mean()))
        fill_vals.append(float(fg[m].mean()))
        black.append(float((fg[m] < 16).mean()))
    return (float(np.mean(src_vals)), float(np.mean(fill_vals)), float(np.mean(black)))


def _window_frames(path, ffmpeg, seconds, start):
    """The frames of one window of a finished file, at the pass's own working width.

    The same extraction `caption_coverage` makes, so a measurement taken here compares the two
    files the render will actually use rather than an intermediate the viewer never sees.
    """
    if not ffmpeg:
        return []
    work = Path(tempfile.mkdtemp(prefix="capwin_"))
    try:
        small = work / "win.mp4"
        args = [str(ffmpeg), "-nostdin", "-y", "-hide_banner", "-loglevel", "error"]
        if float(start or 0.0) > 0.001:
            args += ["-ss", f"{float(start):.4f}"]
        args += ["-i", str(path), "-t", f"{max(0.4, float(seconds or 2.5)):.3f}",
                 "-vf", f"scale={WORK_WIDTH}:-2", "-an", "-c:v", "libx264", "-crf", "16",
                 "-preset", "veryfast", str(small)]
        # The app runs under a hidden console, so a child inherits a handle nobody writes to and
        # ffmpeg's interactive-key read can block after the encode is done. This helper was added
        # in the same commit that fixed exactly that in action_editor, and shipped without it.
        done = subprocess.run(args, capture_output=True, timeout=300, stdin=subprocess.DEVNULL)
        return [] if done.returncode or not small.is_file() else _read_frames(small)
    except Exception:                                                   # noqa: BLE001
        return []
    finally:
        shutil.rmtree(work, ignore_errors=True)


def fill_direction(source_frames, fill_frames, note=None):
    """How the rebuilt area's structure leans, against the picture right next to it.

    ProPainter borrows from neighbouring frames. Where there is nothing to borrow - a caption
    over a crowd walking towards the lens, over a street that never opens up - it invents, and
    what it invents is content dragged along one axis. On screen that is the "Geschmiere": the
    text is gone and so is everything that was behind it.

    Returns the ratio of horizontal to vertical gradient energy INSIDE the repainted area over
    the same ratio just outside it. A faithful rebuild carries the surrounding picture's own
    balance into the hole (~1.0); a smear loses the gradients that run across the drag and comes
    back well under it. 1.0 when it cannot be measured, so an unmeasurable fill is never punished.

    The repainted area is recovered by comparing the fill with its source rather than from the
    mask: ProPainter paints a dilated region, and the glyph-tight mask would measure a different
    area than the one that was actually rebuilt.
    """
    def unmeasured(why):
        # SAY SO. Every one of these paths returned the same 1.0 as a perfectly balanced rebuild,
        # so the first run of this gate passed two known smears and looked exactly like a gate
        # that had examined them. An unmeasurable fill and a good one must not be the same value
        # with no way to tell them apart.
        if note is not None:
            note["unmeasured"] = why
        return 1.0

    if cv2 is None or np is None or not source_frames or not fill_frames:
        return unmeasured("no frames to compare")
    count = min(len(source_frames), len(fill_frames))
    if count < 4:
        return unmeasured(f"only {count} comparable frame(s)")

    def grey(frame):
        return (cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame
                ).astype(np.float32)

    # ONE SIZE FOR EVERYTHING, DECIDED ONCE. ProPainter returns its own working height (640
    # against the work clip's 960 on a 9:16 source), so the mask built from the source did not
    # fit the fill it was meant to index: the first run of this gate raised IndexError inside the
    # remover's own catch-all, which reads as "caption removal failed" and rejected all four test
    # clips - two of them for the right verdict and entirely the wrong reason.
    base = grey(source_frames[0])
    sources, fills = [], []
    for index in range(count):
        a, b = grey(source_frames[index]), grey(fill_frames[index])
        if a.shape != base.shape:
            return unmeasured("the source frames change size inside the window")
        if b.shape != base.shape:
            b = cv2.resize(b, (base.shape[1], base.shape[0]), interpolation=cv2.INTER_AREA)
        sources.append(a)
        fills.append(b)
    changed = np.zeros(base.shape, np.float32)
    for a, b in zip(sources, fills):
        changed += np.abs(a - b) > 14
    # A pixel counts as repainted when it differs across a quarter of the frames. One re-encoded
    # frame differs everywhere by a grey level or two; a rebuilt region differs throughout.
    inside = changed >= max(2, count // 4)
    if inside.sum() < 500:
        return unmeasured(f"only {int(inside.sum())} pixel(s) changed")
    outside = cv2.dilate(inside.astype(np.uint8),
                         np.ones((41, 41), np.uint8)).astype(bool) & ~inside
    if outside.sum() < 500:
        return unmeasured("the repainted area has no picture around it to compare with")

    def lean(region):
        horizontal = vertical = 0.0
        for g in fills:
            horizontal += float(np.abs(cv2.Sobel(g, cv2.CV_32F, 1, 0, ksize=3))[region].mean())
            vertical += float(np.abs(cv2.Sobel(g, cv2.CV_32F, 0, 1, ksize=3))[region].mean())
        return horizontal / max(vertical, 1e-6)

    around = lean(outside)
    return float(lean(inside) / around) if around > 1e-6 else 1.0


def patch_flatness(fill_frames, mask_small):
    """How much of the FILL's own texture survived, against the picture right next to it.

    The residual check asks whether text is still readable and `fill_damage` asks whether the
    area went dark. Neither of them sees the third failure, which is the one the owner keeps
    pointing at: a flat rectangle of averaged colour pasted over the picture - skin-pink across a
    pair of hands, a grey slab across a sky. It carries no text (so the residual check waves it
    through) and it is not dark (so the damage check waves it through), and it is far more
    conspicuous than the caption it replaced.

    A reconstruction borrows real pixels and therefore keeps roughly the detail of its
    surroundings. A smear has almost none. This returns the ratio of the standard deviation
    inside the mask to the standard deviation in a ring around it: ~1.0 for an honest fill,
    near 0 for a pasted patch.
    """
    if cv2 is None or np is None or not fill_frames:
        return 1.0
    inside = mask_small > 32
    if not inside.any():
        return 1.0
    ring = cv2.dilate(inside.astype(np.uint8),
                      cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (31, 31))).astype(bool) & ~inside
    if not ring.any():
        return 1.0
    ratios = []
    step = max(1, len(fill_frames) // 12)
    for i in range(0, len(fill_frames), step):
        g = cv2.cvtColor(fill_frames[i], cv2.COLOR_BGR2GRAY)
        if g.shape != inside.shape:
            g = cv2.resize(g, (inside.shape[1], inside.shape[0]), interpolation=cv2.INTER_AREA)
        # local detail, not global brightness: a gradient sky has a high std and no detail at all
        detail = cv2.Laplacian(g, cv2.CV_32F, ksize=3)
        outside = float(detail[ring].std())
        if outside < 1.0:                       # the neighbourhood is featureless too - no verdict
            continue
        ratios.append(float(detail[inside].std()) / outside)
    return float(np.median(ratios)) if ratios else 1.0


MIN_FILL_TEXTURE = 0.35


def _dimensions(path, ffmpeg):
    """Width and height of a clip.

    The ffprobe path is NOT ffmpeg-with-a-word-swapped: the install directory is itself called
    ffmpeg-8.1.1-full_build, so replacing every "ffmpeg" in the path mangled the directory too and
    every probe failed silently. clip_scraper already resolves both tools properly.
    """
    probe = None
    try:
        import clip_scraper
        probe = clip_scraper._ffmpeg_tools()[1]
    except Exception:      # noqa: BLE001
        probe = None
    if not probe:
        name = "ffprobe.exe" if str(ffmpeg).lower().endswith(".exe") else "ffprobe"
        probe = str(Path(ffmpeg).with_name(name))
    try:
        out = subprocess.run([probe, "-v", "error", "-select_streams", "v:0", "-show_entries",
                              "stream=width,height", "-of", "csv=p=0:s=x", str(path)],
                             capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL).stdout
        width, height = (int(v) for v in out.strip().split("x")[:2])
        return width, height
    except Exception:      # noqa: BLE001
        return None


def _per_frame_glyph_masks(frames, mask_small):
    """Return organic, frame-specific masks for changing white/yellow creator captions."""
    union = (mask_small > 32).astype(np.uint8)
    glyph_kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (21, 21))
    union_guard = cv2.dilate(
        union, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (15, 15)))
    masks = []
    for frame in frames:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
        white = (gray >= 155) & (hsv[:, :, 1] <= 145)
        yellow = ((hsv[:, :, 0] >= 10) & (hsv[:, :, 0] <= 48)
                  & (hsv[:, :, 1] >= 45) & (hsv[:, :, 2] >= 120))
        local_kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (15, 15))
        contrast = np.maximum(cv2.morphologyEx(gray, cv2.MORPH_TOPHAT, local_kernel),
                              cv2.morphologyEx(gray, cv2.MORPH_BLACKHAT, local_kernel))
        # Once the dominant caption band has excluded environmental text, it is safe to include
        # the high-contrast dark outline/shadow as well as the white/yellow letter fill.
        dark_outline = (gray <= 165) & (contrast >= 18)
        visible_ink = ((white | yellow | dark_outline).astype(np.uint8) & union_guard)
        per_frame = cv2.dilate(visible_ink, glyph_kernel)
        masks.append(cv2.bitwise_and(per_frame, union_guard) * 255)
    return masks


def _clip_fps(clip, default=30.0):
    capture = cv2.VideoCapture(str(clip))
    try:
        fps = float(capture.get(cv2.CAP_PROP_FPS) or 0.0)
    finally:
        capture.release()
    return fps if 1.0 < fps < 240.0 else float(default)


def _clip_frame_count(clip):
    """Decoded frame count - counted, not read off the container header, which lies on
    concatenated TikTok downloads."""
    capture = cv2.VideoCapture(str(clip))
    frames = 0
    try:
        while capture.grab():
            frames += 1
    finally:
        capture.release()
    return frames


def _text_frame_scores(clip, mask_small):
    """Per frame: how much stronger the ink response is INSIDE the caption mask than just
    outside it. Text present pushes it well above 1, an empty band sits at about 1.

    A ratio, not a difference, because the difference is dominated by how busy the background is:
    measured on a convenience-store shelf the difference between a captioned and an emptied band
    was 5.5 against -54, but on a plain wall it was 97 against 0.3 - no single cut-off fits both.
    """
    union = mask_small > 32
    near = cv2.dilate(union.astype(np.uint8),
                      cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (41, 41))) > 0
    guard = cv2.dilate(union.astype(np.uint8),
                       cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (11, 11))) > 0
    ring = near & ~guard
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (15, 15))
    if not union.any() or not ring.any():
        return []
    capture = cv2.VideoCapture(str(clip))
    scores = []
    try:
        while True:
            ok, frame = capture.read()
            if not ok:
                break
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            ink = np.maximum(cv2.morphologyEx(gray, cv2.MORPH_TOPHAT, kernel),
                             cv2.morphologyEx(gray, cv2.MORPH_BLACKHAT, kernel)).astype(np.float32)
            scores.append(float(ink[union].mean()) / max(1.0, float(ink[ring].mean())))
    finally:
        capture.release()
    return scores


def _text_frame_runs(clip, mask_small, pad=6, bridge=15, min_run=8):
    """Which frame RANGES carry the caption, or None when the clip cannot be split honestly.

    A caption is usually on screen for part of a clip, and inpainting a frame that has no text
    on it is pure cost - worse, it can only damage the picture. Measured 2026-09-03 on a clip
    whose caption stops at frame 20: the captioned frames score 10.7-15.5 and the clean ones
    0.83-1.23, so the split is unambiguous.

    None means "do not split". On a convenience-store clip whose pink caption sits over packed
    shelving, every frame scores 0.81-1.33 - the caption is genuinely on all of them, and the
    band is no inkier than the shelves beside it. Guessing a cut-off there would skip frames that
    DO carry text, which is the silent no-op this whole module exists to stop.
    """
    scores = _text_frame_scores(clip, mask_small)
    if len(scores) < 12:
        return None
    values = np.array(scores, dtype=np.float32)
    high = float(np.percentile(values, 90))
    low = float(np.percentile(values, 10))
    if high < 2.0 or high < TEXT_RUN_CONTRAST * max(low, 0.05):
        return None
    # Relative, but never above the point where a frame is plainly inked: whichever of the two
    # is lower decides. Without the floor this line is what let a captioned frame be called clean
    # (see TEXT_FRAME_FLOOR), and a clip whose caption is dimmer in the middle was split straight
    # through its own text.
    threshold = min(math.sqrt(high * max(low, 0.05)), TEXT_FRAME_FLOOR)
    flags = values >= threshold
    runs = []
    start = None
    for index, flag in enumerate(flags):
        if flag and start is None:
            start = index
        elif not flag and start is not None:
            runs.append([start, index])
            start = None
    if start is not None:
        runs.append([start, len(flags)])
    runs = [run for run in runs if run[1] - run[0] >= min_run]
    if not runs:
        return None
    # Pad and merge: ProPainter borrows from neighbouring frames, so a run that starts exactly on
    # the first lit frame has no clean context to borrow from, and a one-frame flicker in the
    # middle of a caption is noise, not a gap.
    padded = [[max(0, run[0] - pad), min(len(flags), run[1] + pad)] for run in runs]
    merged = [padded[0]]
    for run in padded[1:]:
        if run[0] - merged[-1][1] <= bridge:
            merged[-1][1] = run[1]
        else:
            merged.append(run)
    if merged[0][0] == 0 and merged[-1][1] == len(flags) and len(merged) == 1:
        return None                       # the caption is on the whole clip; nothing to skip
    return [(int(a), int(b)) for a, b in merged]


def _fill_plan(total, runs, max_frames):
    """Segments covering [0, total) as (start, count, fill?), each fill segment small enough
    for one ProPainter run."""
    spans = [(0, total, True)] if not runs else []
    if runs:
        cursor = 0
        for start, end in runs:
            if start > cursor:
                spans.append((cursor, start - cursor, False))
            spans.append((start, end - start, True))
            cursor = end
        if cursor < total:
            spans.append((cursor, total - cursor, False))
    plan = []
    for start, count, fill in spans:
        if not fill or count <= max_frames:
            plan.append((start, count, fill))
            continue
        pieces = max(1, math.ceil(count / float(max_frames)))
        per_piece = math.ceil(count / float(pieces))
        offset = 0
        while offset < count:
            plan.append((start + offset, min(per_piece, count - offset), True))
            offset += per_piece
    return plan


def _cut_frames(clip, start, count, fps, ffmpeg, destination):
    done = subprocess.run(
        [ffmpeg, "-nostdin", "-y", "-hide_banner", "-loglevel", "error",
         "-ss", f"{start / fps:.4f}", "-i", str(clip), "-frames:v", str(count),
         "-an", "-c:v", "libx264", "-crf", "16", "-preset", "veryfast", str(destination)],
        capture_output=True, timeout=300, stdin=subprocess.DEVNULL)
    return destination if not done.returncode and Path(destination).is_file() else None


def _fill_in_pieces(small, mask_small, work, ffmpeg, status_cb=None, cancel_check=None,
                    runs=None):
    """Fill the work clip and return (filled clip, filled frame count, total frames).

    Two separate reasons to cut the clip up. ProPainter's memory grows with the LENGTH of the
    clip - it holds the forward and backward flows for every frame at once, so --subvideo_length
    caps the inpainting window but not the flow stage. Measured 2026-09-03 on an 11 GB card with
    ~3.3 GB held by the desktop: 585 frames OOM at both 540px and 360px, 200 frames at 360px take
    123s and 100 frames 45s. And ``runs`` says which frames actually carry text: the rest are
    passed through untouched, because inpainting a clean frame costs a minute and can only make
    it worse.

    The returned clip is always full length and frame-aligned with ``small``, so the composite
    can key it in by timestamp. A short fill would be silently stretched by ffmpeg's overlay,
    which repeats the last frame - a frozen patch of stale background over the tail.
    """
    total = _clip_frame_count(small)
    if total < 4:
        return None
    plan = _fill_plan(total, runs, MAX_FILL_FRAMES)
    fps = _clip_fps(small)
    filled_frames = sum(count for _start, count, fill in plan if fill)
    if runs:
        _log(status_cb, f"Caption removal: {filled_frames} of {total} frames carry the caption; "
                        f"the other {total - filled_frames} are passed through untouched.")
    pieces = []
    for index, (start, count, fill) in enumerate(plan):
        piece_work = Path(work) / f"fill_{index:03d}"
        piece_work.mkdir(parents=True, exist_ok=True)
        if len(plan) == 1:
            piece = Path(small)
        else:
            piece = _cut_frames(small, start, count, fps, ffmpeg, piece_work / "piece.mp4")
            if piece is None:
                return None
        if not fill:
            pieces.append(Path(piece))
            continue
        _log(status_cb, f"Caption removal: filling frames {start}-{start + count} of {total}.")
        # Straight to FILL_WIDTH. There used to be a 540px attempt first, with 360px as the
        # out-of-memory fallback; measured 2026-09-03 on this 11 GB card, 540px OOMed on 100,
        # 200 and 585 frames alike and cost 82-149s each time before failing, and no 540px run
        # has ever been observed to complete on a 9:16 clip. An attempt that cannot succeed is
        # not a quality setting, it is a delay in front of a render.
        out = _fill_piece_with_width_ladder(piece, mask_small, None, piece_work, ffmpeg,
                                       cancel_check=cancel_check)
        if out is None:
            raise OutOfMemory(f"ProPainter could not fill piece {index + 1} of {len(plan)} "
                              f"at {FILL_WIDTH}px")
        pieces.append(Path(out))
    if not pieces:
        return None
    if len(pieces) == 1:
        return pieces[0], filled_frames, total
    # Pieces come back at different widths when only some of them needed the OOM retry, and
    # concat refuses a size change mid-stream - so they are all scaled to the work width here.
    listing = Path(work) / "filled_pieces.txt"
    listing.write_text("".join(f"file '{p.resolve().as_posix()}'\n" for p in pieces),
                       encoding="utf-8")
    joined = Path(work) / "filled_joined.mp4"
    done = subprocess.run(
        [ffmpeg, "-nostdin", "-y", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0",
         "-i", str(listing), "-vf", f"scale={WORK_WIDTH}:-2", "-an",
         "-c:v", "libx264", "-crf", "16", "-preset", "veryfast", str(joined)],
        capture_output=True, timeout=900, stdin=subprocess.DEVNULL)
    if done.returncode or not joined.is_file() or joined.stat().st_size < 4096:
        return None
    return joined, filled_frames, total


def _write_frame_masks(clip, mask_small, mask_dir):
    """Write ONE mask per frame of ``clip`` and return how many were written.

    Masks used to be produced from the frame list already in memory, and that list is capped at
    400 frames by ``_read_frames``. ProPainter iterates the VIDEO and indexes the mask list, so
    any clip longer than the cap died with ``IndexError: list index out of range`` inside their
    inference_propainter.py - measured on a 585-frame (19.5s) clip, which is what a call without
    an explicit ``seconds`` span produces. Truncating the clip to the cap instead is not an
    option: ffmpeg's overlay repeats the fill's last frame past its end, so the tail would carry
    a frozen patch of stale background. Streaming the decode keeps the two counts equal at any
    length without ever holding the whole clip in RAM.
    """
    mask_dir = Path(mask_dir)
    mask_dir.mkdir(parents=True, exist_ok=True)
    capture = cv2.VideoCapture(str(clip))
    written = 0
    try:
        while True:
            ok, frame = capture.read()
            if not ok:
                break
            frame_mask = _per_frame_glyph_masks([frame], mask_small)[0]
            cv2.imwrite(str(mask_dir / f"{written:06d}.png"), frame_mask)
            written += 1
    finally:
        capture.release()
    return written


def _log(status_cb, message):
    if status_cb:
        try:
            status_cb(message)
        except Exception:      # noqa: BLE001
            pass
