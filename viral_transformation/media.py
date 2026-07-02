"""Media Generation + Visual QA for the Viral Transformation mode.

Per scene: GPT-Image-2 reference image (9:16) -> upload -> Seedance 2.0 image-to-video
(the reference keeps the subject consistent) -> validate with ffprobe -> retry with a
simplified prompt -> final fallback: a zoom/pan (Ken Burns) motion clip from the still.
A single batched vision-QA pass rejects broken/off-topic clips and regenerates once.
"""

import json
import math
import os
import subprocess
from pathlib import Path

import agent_core
import pipeline

WAVESPEED_CFG = {"wavespeed": {
    "image_model": pipeline.DEFAULT_IMAGE_MODEL,          # openai/gpt-image-2/text-to-image
    "video_model": "bytedance/seedance-2.0/image-to-video",
    "aspect_ratio": "9:16",
    "video_resolution": "480p",
}}


def log(cb, msg):
    if cb:
        cb(msg)


def _run(cmd, timeout=None):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def validate_clip(path, ffprobe, min_seconds=2.0):
    """ffprobe check: real video stream, sane duration, vertical or croppable to 9:16."""
    try:
        r = _run([ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
                  "stream=width,height", "-show_entries", "format=duration",
                  "-of", "json", str(path)], timeout=30)
        data = json.loads(r.stdout or "{}")
        st = (data.get("streams") or [{}])[0]
        w, h = int(st.get("width") or 0), int(st.get("height") or 0)
        dur = float((data.get("format") or {}).get("duration") or 0.0)
        if not (w and h and dur >= min_seconds):
            return False, f"invalid stream ({w}x{h}, {dur:.1f}s)"
        if h < w:                                     # landscape can't crop to 9:16 nicely
            return False, f"landscape output ({w}x{h})"
        return True, "ok"
    except Exception as exc:
        return False, f"probe failed ({exc.__class__.__name__})"


def ken_burns_clip(image_path, out_path, seconds, ffmpeg, zoom_in=True):
    """Fallback: animate a still with a slow zoom/pan into a 1080x1920 clip."""
    n = max(30, int(round(seconds * 30)))
    z = "min(zoom+0.0012,1.25)" if zoom_in else "if(lte(zoom,1.0),1.25,max(zoom-0.0012,1.0))"
    vf = (f"scale=2160:3840:force_original_aspect_ratio=increase,crop=2160:3840,"
          f"zoompan=z='{z}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={n}:s=1080x1920:fps=30,"
          f"format=yuv420p")
    r = _run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-loop", "1",
              "-i", str(image_path), "-vf", vf, "-t", f"{seconds:.2f}",
              "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", str(out_path)],
             timeout=300)
    return out_path if Path(out_path).exists() else None


def _simplify(prompt):
    """Retry prompt: drop everything after the first two sentences, keep the reference line."""
    parts = [p.strip() for p in str(prompt).split(".") if p.strip()]
    return (". ".join(parts[:2]) + ". Simple, slow, realistic motion, clean frame, "
            "no text, no watermark.")


def generate_scene_media(plan, strategy, project_dir, status_cb=None, cancel_event=None):
    """Generate reference image + video clip for every scene. Returns
    {scene_id: {"image": path|None, "clip": path|None, "source": "seedance|kenburns",
                "status": "accepted|failed"}}."""
    key = pipeline.api_key()
    ffmpeg = pipeline.find_ffmpeg()
    ffprobe = pipeline.find_ffprobe(ffmpeg)
    img_dir = Path(project_dir) / "gpt images"
    clip_dir = Path(project_dir) / "seedance 2.0"
    img_dir.mkdir(parents=True, exist_ok=True)
    clip_dir.mkdir(parents=True, exist_ok=True)
    scenes = plan["scenes"]
    out = {}

    log(status_cb, f"Generating images... ({len(scenes)} scene reference images)")
    for sc in scenes:
        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError("Run cancelled by user.")
        sid = sc["scene_id"]
        img_path = img_dir / f"scene_{sid:02d}.png"
        rec = {"image": None, "clip": None, "source": None, "status": "failed"}
        out[sid] = rec
        for attempt, prompt in enumerate((sc["image_prompt"], _simplify(sc["image_prompt"]))):
            try:
                pred, _ = pipeline.submit_wavespeed_image(prompt, WAVESPEED_CFG, key)
                outputs, _ = pipeline.poll_wavespeed(pred, key, timeout_s=300,
                                                     cancel_event=cancel_event,
                                                     status_cb=None, label=f"Image {sid}")
                ext = pipeline.output_extension(outputs[0], ".png")
                img_path = img_path.with_suffix(ext)
                pipeline.download_file(outputs[0], img_path)
                if img_path.exists() and img_path.stat().st_size > 4096:
                    rec["image"] = str(img_path)
                    log(status_cb, f"Scene {sid}: reference image ready.")
                    break
            except Exception as exc:
                log(status_cb, f"Scene {sid}: image attempt {attempt + 1} failed "
                               f"({exc.__class__.__name__}).")
        if not rec["image"]:
            log(status_cb, f"Scene {sid}: no reference image - scene will be skipped.")

    log(status_cb, "Generating WaveSpeed videos... (Seedance 2.0 image-to-video)")
    for sc in scenes:
        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError("Run cancelled by user.")
        sid = sc["scene_id"]
        rec = out[sid]
        if not rec["image"]:
            continue
        clip_path = clip_dir / f"scene_{sid:02d}.mp4"
        gen_seconds = max(4, int(math.ceil(float(sc["duration_seconds"]))))
        prompts = (sc["video_motion_prompt"], _simplify(sc["video_motion_prompt"]))
        for attempt, prompt in enumerate(prompts):
            try:
                image_url, _ = pipeline.upload_media(Path(rec["image"]), key)
                pred, _, _ = pipeline.submit_wavespeed_clip(
                    image_url, prompt, gen_seconds, WAVESPEED_CFG, {}, key)
                outputs, _ = pipeline.poll_wavespeed(pred, key, timeout_s=600,
                                                     cancel_event=cancel_event,
                                                     status_cb=None, label=f"Clip {sid}")
                pipeline.download_file(outputs[0], clip_path)
                ok, why = validate_clip(clip_path, ffprobe)
                if ok:
                    rec["clip"] = str(clip_path)
                    rec["source"] = "seedance"
                    rec["status"] = "accepted"
                    log(status_cb, f"Scene {sid}: clip accepted.")
                    break
                log(status_cb, f"Scene {sid}: clip attempt {attempt + 1} rejected ({why}).")
            except Exception as exc:
                log(status_cb, f"Scene {sid}: clip attempt {attempt + 1} failed "
                               f"({exc.__class__.__name__}).")
        if not rec["clip"]:                            # last resort: zoom/pan the still
            kb = ken_burns_clip(rec["image"], clip_path, float(sc["duration_seconds"]) + 0.6,
                                ffmpeg, zoom_in=(sid % 2 == 1))
            if kb:
                rec["clip"] = str(kb)
                rec["source"] = "kenburns"
                rec["status"] = "accepted"
                log(status_cb, f"Scene {sid}: using zoom/pan fallback clip.")
    return out


# --------------------------------------------------------------------------- Visual QA

def visual_qa(plan, media, project_dir, reasoning_model=None, status_cb=None,
              cancel_event=None):
    """One batched VISION pass over a mid-frame contact sheet: reject clips whose subject is
    distorted/off-topic/text-covered/wrong phase; rejected Seedance clips fall back to the
    zoom/pan still (identity always consistent there). Skipped without an API key."""
    if not os.environ.get("WAVESPEED_API_KEY"):
        return media
    ffmpeg = pipeline.find_ffmpeg()
    frames_dir = Path(project_dir) / "review" / "_vt_qa"
    frames_dir.mkdir(parents=True, exist_ok=True)
    ids, frames = [], []
    for sc in plan["scenes"]:
        rec = media.get(sc["scene_id"]) or {}
        if rec.get("clip") and rec.get("source") == "seedance":
            fp = frames_dir / f"qa_{sc['scene_id']:02d}.jpg"
            if pipeline.extract_poster_frame(rec["clip"], fp, ffmpeg=ffmpeg,
                                             at=max(0.3, float(sc["duration_seconds"]) / 2)):
                ids.append(sc["scene_id"]); frames.append(fp)
    if not frames:
        return media
    sheet = agent_core.create_media_contact_sheet(
        frames, frames_dir / "_qa_sheet.jpg",
        title="One mid-frame per generated scene clip (tile index below).")
    if not sheet:
        return media
    lines = "\n".join(
        f"tile {i}: scene {sid} | phase {next(s['phase'] for s in plan['scenes'] if s['scene_id'] == sid)} "
        f"| goal: {next(s['visual_goal'] for s in plan['scenes'] if s['scene_id'] == sid)[:90]}"
        for i, sid in enumerate(ids))
    prompt = (
        f"QA for a '{plan.get('title', '')}' transformation Short about: {plan['scenes'][0]['visual_goal'][:80]}.\n"
        "For each tile decide accept|reject. REJECT when: the subject looks like a different "
        "individual than the others, the animal/object is distorted or malformed, random "
        "unreadable generated text or a watermark is visible, the frame is unrelated to the "
        "topic, or the action clearly contradicts the phase. Otherwise accept.\n\n"
        f"{lines}\n\n"
        'Return STRICT JSON: {"tiles": {"<tile_index>": {"verdict": "accept|reject", '
        '"reason": "short"}}}')
    try:
        log(status_cb, "Checking clips... (vision QA)")
        data = agent_core.post_json_url(agent_core.WAVESPEED_LLM_API, {
            "model": reasoning_model or "anthropic/claude-opus-4.8",
            "messages": [
                {"role": "system", "content": "You QA AI-generated clips strictly. Return JSON only."},
                {"role": "user", "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": agent_core.image_data_url(sheet)}},
                ]}],
            "temperature": 0.2, "max_tokens": 900,
            "response_format": {"type": "json_object"}}, timeout=180)
        plan_json = agent_core.extract_json_object(data["choices"][0]["message"]["content"]) or {}
        tiles = plan_json.get("tiles") if isinstance(plan_json.get("tiles"), dict) else {}
    except Exception as exc:
        log(status_cb, f"Vision QA skipped ({exc.__class__.__name__}).")
        return media
    ffmpeg = pipeline.find_ffmpeg()
    for key_, d in tiles.items():
        try:
            i = int(key_)
        except (TypeError, ValueError):
            continue
        if not (0 <= i < len(ids)) or not isinstance(d, dict):
            continue
        if str(d.get("verdict", "accept")).lower().strip() != "reject":
            continue
        sid = ids[i]
        rec = media.get(sid) or {}
        sc = next(s for s in plan["scenes"] if s["scene_id"] == sid)
        log(status_cb, f"Scene {sid}: QA reject ({str(d.get('reason', ''))[:70]}) -> "
                       "zoom/pan fallback.")
        if rec.get("image"):
            kb = ken_burns_clip(rec["image"], Path(rec["clip"]),
                                float(sc["duration_seconds"]) + 0.6, ffmpeg)
            if kb:
                rec["source"] = "kenburns"
    return media
