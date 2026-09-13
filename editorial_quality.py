"""Inspect exact played windows and finished exports; never label unknown as passed."""
from __future__ import annotations

import json
import os
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw

import editorial


def enabled(config):
    return bool(config.get("editorial_quality_enabled", True) and (
        config.get("clip_source") == "scrape" or config.get("timeline_editor_render") or
        any(s.get("scrape_source") for s in config.get("scenes", []))))


def scan_cuts(path, ffmpeg):
    """Full-duration, bounded-memory scan. None is a failure, [] a continuous take."""
    if not ffmpeg:
        return None
    try:
        result = subprocess.run([
            str(ffmpeg), "-hide_banner", "-nostats", "-i", str(path), "-vf",
            "scale=180:320,select='gt(scene,0.25)',showinfo", "-an", "-f", "null", os.devnull],
            capture_output=True, text=True, timeout=90)
        if result.returncode:
            return None
        return sorted(set(float(t) for t in re.findall(r"pts_time:([0-9.]+)", result.stderr)))
    except (OSError, subprocess.TimeoutExpired):
        return None


def repair_window(start, end, cuts, shown, minimum_speed=.84):
    """Remove only short edge fragments, within the selected window.

    Never jump to an unrelated clean shot elsewhere in the source. Interior cuts
    need a new editorial selection; modest edge repair is verified after export.
    """
    if cuts is None:
        return start, (end - start) / shown, "unverified"
    inside = [c for c in cuts if start + .015 < c < end - .015]
    if not inside:
        return start, (end - start) / shown, "unchanged"
    marks = [start] + inside + [end]
    a, b = max(zip(marks, marks[1:]), key=lambda ab: ab[1] - ab[0])
    # Only a <=250ms lead/tail fragment is repairable without choosing a new action.
    if a - start > .25 or end - b > .25:
        return start, (end - start) / shown, "needs_replacement"
    left = a + (.035 if a > start else 0)
    right = b - (.035 if b < end else 0)
    speed = (right - left) / shown
    if speed < minimum_speed:
        return start, (end - start) / shown, "needs_replacement"
    return left, speed, "repaired"


def _strip(video, times, path):
    cap = cv2.VideoCapture(str(video))
    panel = Image.new("RGB", (180 * len(times), 340), "#151515")
    fingerprints = []
    try:
        fps = cap.get(cv2.CAP_PROP_FPS) or 30
        count = cap.get(cv2.CAP_PROP_FRAME_COUNT)
        last = max(0, (count - 1) / fps)
        for i, t in enumerate(times):
            # Audio/container duration may exceed video by a few encoder frames.
            # Clamp this small tail only; a genuinely truncated video remains an error.
            actual = min(t, last) if count and t <= last + .12 else t
            cap.set(cv2.CAP_PROP_POS_FRAMES, round(actual * fps))
            ok, frame = cap.read()
            if not ok:
                raise ValueError(f"No frame at {t:.3f}")
            im = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
            # Caption area is omitted from the repeat signature, not the visual review.
            arr = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            arr = arr[:int(arr.shape[0] * .48)]
            small = cv2.resize(arr, (16, 16)).astype(float)
            fingerprints.append((small > small.mean()).flatten().tolist())
            im.thumbnail((178, 314))
            panel.paste(im, (i * 180 + (180 - im.width) // 2, 0))
            ImageDraw.Draw(panel).text((i * 180 + 3, 320), f"{t:.3f}s", fill="white")
        panel.save(path, quality=88)
    finally:
        cap.release()
    return fingerprints


def reselect_window(scene, video, start, end, cuts, shown, config, folder,
                    occupied=(), reviewer=None):
    """Bounded repair from this source, with fresh evidence for every proposed move.

    No network search and no unreviewed jump to another part of the video. If the
    local source cannot prove the action, the export remains explicitly flagged.
    """
    if cuts is None or (reviewer is None and not os.environ.get("WAVESPEED_API_KEY")):
        return None
    cap = cv2.VideoCapture(str(video))
    duration = cap.get(cv2.CAP_PROP_FRAME_COUNT) / (cap.get(cv2.CAP_PROP_FPS) or 30)
    cap.release()
    marks = [0.] + [c for c in cuts if 0 < c < duration] + [duration]
    proposals = []
    for a, b in zip(marks, marks[1:]):
        available = b - a - .1
        if available < shown * .84:
            continue
        length = min(shown, available)
        left = max(a + .05, min(b - length - .05, start))
        if any(not (left + length <= x or left >= y) for x, y in occupied):
            continue
        proposals.append((left, left + length))
    proposals.sort(key=lambda pair: abs(pair[0] - start))
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    for i, (left, right) in enumerate(proposals[:3]):
        strip = folder / f"candidate_{i}.jpg"
        try:
            _strip(video, [left + (right - left) * f for f in (.04, .25, .5, .75, .96)], strip)
            verdict = (reviewer or _review)(scene, strip, config)
            if not verdict or verdict.get("uncertain") is not False:
                continue
            verdict["reviewed_line"] = editorial.contract(scene)["line"]
            verdict["reviewed_sequence_id"] = editorial.contract(scene)["sequence_id"]
            if editorial.evidence_fits(scene, verdict) and not verdict.get("intrusive_source_text"):
                return {"start": left, "end": right, "speed": (right - left) / shown,
                        "evidence": verdict}
        except (OSError, ValueError):
            continue
    return None


def _review(scene, strip, config):
    if not os.environ.get("WAVESPEED_API_KEY"):
        return None
    import agent_core
    import scrape_v4
    prompt = (
        "Review five chronological frames of the FINISHED video against the narration. "
        f"Topic: {config.get('title', '')}. "
        + editorial.evidence_prompt(scene) +
        "Own word captions are expected. Identify leftover creator captions separately. "
        "Do not assume an action between sampled frames: if ambiguous, set uncertain=true. "
        "Return JSON: action_visible, result_visible, observed_action, relevance (0..10), "
        "intrusive_source_text (boolean), uncertain (boolean), reason. No extra keys needed.")
    _, raw, error = scrape_v4.vision_verdict(
        (str(scene.get("id")), None, strip, prompt),
        config.get("editorial_review_model") or scrape_v4.DEFAULT_V4_VISION_MODEL, attempts=1)
    if error:
        return None
    verdict = agent_core.extract_json_object(raw)
    return verdict if isinstance(verdict, dict) and verdict else None


def audit_export(config, video, ffmpeg, reviewer=_review, cut_scanner=scan_cuts):
    """Always persist a review result and affected scene IDs for targeted repair."""
    video = Path(video)
    folder = video.parent / (video.stem + "_editorial")
    folder.mkdir(parents=True, exist_ok=True)
    scenes = config.get("scenes", [])
    report = {"version": 1, "video": str(video), "status": "unverified",
              "issues": [], "scenes": [], "repair_queue": []}
    cuts = cut_scanner(video, ffmpeg)
    report["detected_cut_candidates"] = cuts
    jobs, signatures = [], []
    for index, scene in enumerate(scenes):
        start, end = float(scene["start"]), float(scene["end"])
        sid = str(scene.get("id", index + 1))
        row = {"scene": sid, "start": start, "end": end, "semantic_status": "unverified"}
        report["scenes"].append(row)
        allowed = [start + float(shot.get("at", 0)) for shot in scene.get("shots", [])]
        internal = [t for t in cuts or [] if start + .06 < t < end - .06
                    and not any(abs(t - planned) < .06 for planned in allowed)]
        if internal:
            report["issues"].append({"scene": sid, "type": "unplanned_cut", "times": internal,
                                     "action": "reselect_clean_window"})
            # Explicit before/after strips make short flashes reviewable.
            times = [max(start, t - .04) for t in internal[:8]]
            times += [min(end - .02, t + .04) for t in internal[:8]]
            try:
                _strip(video, sorted(times), folder / f"scene_{index + 1}_cuts.jpg")
            except ValueError:
                pass
        if scene.get("needs_replacement") or scene.get("assignment_type") in {
                "uncovered", "uncovered_still", "coverage_fill"}:
            report["issues"].append({"scene": sid, "type": "unverified_assignment",
                                     "action": "find_action_evidence"})
        strip = folder / f"scene_{index + 1}.jpg"
        times = [start + (end - start) * f for f in (.04, .25, .5, .75, .96)]
        try:
            sig = _strip(video, times, strip)
            signatures.append((sid, sig))
            jobs.append((scene, strip, row))
        except ValueError:
            report["issues"].append({"scene": sid, "type": "unreadable_frames",
                                     "action": "replace_unreadable_clip"})
    # Require several matching frames, not a single similar image, to flag a repeat.
    for index, (sid, signature) in enumerate(signatures):
        for prior_id, prior in signatures[:index]:
            if sum(sum(a != b for a, b in zip(x, y)) <= 8
                   for x, y in zip(signature, prior)) >= 3:
                report["issues"].append({"scene": sid, "type": "possible_repeat",
                                         "earlier_scene": prior_id, "action": "review_repeated_sequence"})
                break

    def review_job(job):
        scene, strip, row = job
        try:
            return scene, row, reviewer(scene, strip, config)
        except Exception as exc:
            row["review_error"] = type(exc).__name__
            return scene, row, None

    with ThreadPoolExecutor(max_workers=4) as pool:
        for scene, row, verdict in pool.map(review_job, jobs):
            if not verdict or verdict.get("uncertain") is not False:
                continue
            verdict["reviewed_line"] = editorial.contract(scene)["line"]
            verdict["reviewed_sequence_id"] = editorial.contract(scene)["sequence_id"]
            row["verdict"] = verdict
            fit = editorial.evidence_fits(scene, verdict)
            row["semantic_status"] = "passed" if fit else "failed"
            if not fit:
                report["issues"].append({"scene": row["scene"], "type": "missing_action",
                                         "action": "find_action_evidence", "reason": verdict.get("reason")})
            if verdict.get("intrusive_source_text") is True:
                report["issues"].append({"scene": row["scene"], "type": "source_text",
                                         "action": "clean_or_replace_clip"})
    verified = cuts is not None and bool(scenes) and all(
        row["semantic_status"] == "passed" for row in report["scenes"])
    report["status"] = "needs_review" if report["issues"] else "passed" if verified else "unverified"
    report["repair_queue"] = sorted(set(issue["scene"] for issue in report["issues"]))
    report["preflight"] = config.get("editorial_preflight", [])
    # Unknown evidence stays actionable too, without being mislabeled a content defect.
    report["unverified_scenes"] = [r["scene"] for r in report["scenes"]
                                   if r["semantic_status"] == "unverified"]
    target = video.with_suffix(".editorial.json")
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    config["editorial_quality"] = {"status": report["status"], "report": str(target),
                                    "repair_queue": report["repair_queue"]}
    return report
