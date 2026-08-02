"""Single-shot AI motion loops with a strict one-image/one-video paid budget.

The generative models establish the world and its physical motion.  A deterministic
OpenCV finishing pass supplies the repeatable camera phase, stable foreground rig,
chromatic glow, speed streaks and a short end-to-start blend.  This keeps the mode
useful without turning every effect into another API generation.
"""
from __future__ import annotations

import json
import math
import random
import re
import subprocess
import time
from pathlib import Path

import cv2
import numpy as np

import agent_core
import pipeline

MOTION_DNA_TEMPLATE = Path(__file__).resolve().parent / "static" / "motion_templates" / "psychedelic_lateral_8s.mp4"

_WORLD_SEEDS = [
    "an immense Alpine mountain pass whose switchback road folds upward into the sky, with inverted valleys and clouds flowing below the rider",
    "a minimal abstract 3D world of ivory arches, soft chrome spheres and impossible gravity, with the road continuing across vertical walls",
    "a glacial canyon made of translucent blue ice where entire mountains float overhead and waterfalls travel upward",
    "a warm sandstone desert shaped like a continuous Moebius strip, with monumental shadows and levitating black monoliths",
    "a brutalist city above the clouds whose buildings bend into a planetary ring around the horizon",
    "a vast green valley on a tiny curved planet, with distant villages visible upside down across the sky",
    "an underwater glass metropolis inhabited by luminous manta-like creatures and slow floating gardens",
    "an enormous botanical cathedral where tree trunks become vaulted architecture and petals behave like moving stained glass",
    "a black volcanic beach beneath two moons, with mirror-water channels and geometric basalt formations unfolding ahead",
    "a clean retro-futurist transport tunnel crossing between floating islands, with sculptural concrete and golden evening light",
]
_POV_SEEDS = [
    "natural dark bicycle handlebars and gloved hands",
    "the restrained edge of a futuristic hover-bike cockpit with natural hands on the controls",
    "a compact open rover dashboard with two subtle steering grips",
    "the nose and control bar of a silent gravity glider",
]

MOTION_LOOP_DURATION = 15


def random_motion_concept():
    return f"First-person continuous journey through {random.choice(_WORLD_SEEDS)}; foreground POV: {random.choice(_POV_SEEDS)}."


def motion_world_arc(opening_concept: str):
    """Build a varied three-act world journey for one uninterrupted generation."""
    # If the opening was randomly assembled from one of our seeds, do not silently
    # select the same environment again as a later destination.
    candidates = [world for world in _WORLD_SEEDS if world.lower() not in opening_concept.lower()]
    destinations = random.sample(candidates, 2)
    return {"opening": opening_concept, "middle": destinations[0], "final": destinations[1]}


def _template_duration(path: Path) -> float:
    cap = cv2.VideoCapture(str(path))
    try:
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 0)
        count = float(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        return count / fps if fps > 0 and count > 0 else 8.0
    finally:
        cap.release()


def _log(cb, message):
    if cb:
        cb(message)


def _title(concept: str) -> str:
    words = re.findall(r"[A-Za-z0-9'-]+", concept)
    return " ".join(words[:7]).strip().title() or "Motion Loop"


def _write_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _foreground_overlay(width: int, height: int) -> np.ndarray:
    """Stable coded POV rig. AI never gets a chance to melt these hard edges."""
    layer = np.zeros((height, width, 4), np.uint8)
    glow = np.zeros_like(layer)
    center = width // 2
    green = (105, 255, 126, 210)
    dark = (5, 8, 7, 232)
    # Perspective cockpit rails.
    cv2.line(glow, (0, height - 40), (center - 118, height - 315), green, 22, cv2.LINE_AA)
    cv2.line(glow, (width, height - 40), (center + 118, height - 315), green, 22, cv2.LINE_AA)
    glow[:, :, 3] = cv2.GaussianBlur(glow[:, :, 3], (0, 0), 18)
    cv2.line(layer, (0, height - 38), (center - 118, height - 315), dark, 32, cv2.LINE_AA)
    cv2.line(layer, (width, height - 38), (center + 118, height - 315), dark, 32, cv2.LINE_AA)
    cv2.line(layer, (center - 118, height - 315), (center + 118, height - 315), dark, 28, cv2.LINE_AA)
    # Hands/grips as deliberate graphic silhouettes rather than unstable fake anatomy.
    cv2.ellipse(layer, (center - 150, height - 305), (48, 29), -20, 0, 360, (18, 27, 22, 245), -1, cv2.LINE_AA)
    cv2.ellipse(layer, (center + 150, height - 305), (48, 29), 20, 0, 360, (18, 27, 22, 245), -1, cv2.LINE_AA)
    cv2.circle(layer, (center, height - 315), 17, (119, 255, 133, 235), 3, cv2.LINE_AA)
    return cv2.add(layer, glow)


def _alpha_over(frame: np.ndarray, overlay: np.ndarray) -> np.ndarray:
    alpha = overlay[:, :, 3:4].astype(np.float32) / 255.0
    return (frame.astype(np.float32) * (1.0 - alpha) + overlay[:, :, :3].astype(np.float32) * alpha).astype(np.uint8)


def coded_finish(source: Path, output: Path, duration: int, profile: str, intensity: str, status_cb=None):
    cap = cv2.VideoCapture(str(source))
    if not cap.isOpened():
        raise RuntimeError(f"Could not open Seedance result: {source}")
    src_fps = float(cap.get(cv2.CAP_PROP_FPS) or 30.0)
    src_count = max(1, int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 1))
    target_fps, width, height = 30.0, 720, 1280
    frame_count = int(duration * target_fps)
    fade_frames = min(18, max(8, frame_count // 8))
    tmp = output.with_name(output.stem + "_coded_temp.mp4")
    writer = cv2.VideoWriter(str(tmp), cv2.VideoWriter_fourcc(*"mp4v"), target_fps, (width, height))
    first_frames = []
    strength = {"clean": .45, "balanced": .72, "intense": 1.0}.get(intensity, .72)

    def read_at(i):
        src_i = min(src_count - 1, int(i * src_fps / target_fps) % src_count)
        cap.set(cv2.CAP_PROP_POS_FRAMES, src_i)
        ok, fr = cap.read()
        if not ok:
            raise RuntimeError("Seedance result ended before the coded pass completed.")
        h, w = fr.shape[:2]
        scale = max(width / w, height / h)
        fr = cv2.resize(fr, (int(w * scale + .5), int(h * scale + .5)), interpolation=cv2.INTER_LANCZOS4)
        y = max(0, (fr.shape[0] - height) // 2); x = max(0, (fr.shape[1] - width) // 2)
        return fr[y:y + height, x:x + width].copy()

    def finish_frame(fr, i):
        phase = 2.0 * math.pi * i / max(1, frame_count)
        if profile == "tunnel":
            dx, dy, zoom = 7 * math.sin(phase), 3 * math.cos(phase), 1.035 + .025 * math.sin(phase)
        elif profile == "orbit":
            dx, dy, zoom = 15 * math.sin(phase), 8 * math.cos(phase), 1.025 + .018 * math.cos(phase)
        else:
            dx, dy, zoom = 22 * math.sin(phase), 4 * math.sin(phase * 2), 1.03 + .015 * math.cos(phase)
        matrix = cv2.getRotationMatrix2D((width / 2, height / 2), .35 * strength * math.sin(phase), zoom)
        matrix[:, 2] += (dx * strength, dy * strength)
        fr = cv2.warpAffine(fr, matrix, (width, height), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT_101)
        # Small RGB split only at the edges; central content remains readable.
        shift = max(1, int(3 * strength))
        b, g, r = cv2.split(fr)
        r = np.roll(r, shift, axis=1); b = np.roll(b, -shift, axis=1)
        fr = cv2.merge((b, g, r))
        glow = cv2.GaussianBlur(fr, (0, 0), 10)
        fr = cv2.addWeighted(fr, 1.0, glow, .13 * strength, 0)
        # Periodic speed streaks are deterministic, so first and last phases match.
        if strength > .5:
            streak = fr.copy()
            rng = np.random.default_rng(9047 + i % 30)
            for _ in range(9 if intensity == "intense" else 5):
                x = int(rng.integers(30, width - 30)); y = int(rng.integers(150, height - 250))
                length = int(rng.integers(25, 85))
                cv2.line(streak, (x, y), (x + int(math.sin(phase) * 18), y + length), (180, 255, 205), 1, cv2.LINE_AA)
            fr = cv2.addWeighted(fr, 1, streak, .11, 0)
        # Do not draw a synthetic POV rig here. It used to produce conspicuous green
        # bars and made the finish look composited. The POV anchor is now part of the
        # authored keyframe, so Seedance can move it organically with the world.
        return fr

    _log(status_cb, "Coded finish: stabilizing the foreground and building the seamless loop...")
    for i in range(frame_count):
        fr = finish_frame(read_at(i), i)
        if i < fade_frames:
            first_frames.append(fr.copy())
        if i >= frame_count - fade_frames and first_frames:
            k = i - (frame_count - fade_frames)
            t = (k + 1) / fade_frames
            a = t * t * (3.0 - 2.0 * t)  # smoothstep: no visible velocity kink at the seam
            # Converge on the literal opening frame. Pairing k with first_frames[k]
            # leaves a hidden F-frame jump when playback wraps from the last frame to 0.
            fr = cv2.addWeighted(fr, 1.0 - a, first_frames[0], a, 0)
        writer.write(fr)
    writer.release(); cap.release()
    ffmpeg = pipeline.find_ffmpeg()
    subprocess.run([ffmpeg, "-y", "-i", str(tmp), "-an", "-r", "30", "-c:v", "libx264",
                    "-preset", "medium", "-crf", "17", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(output)],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    tmp.unlink(missing_ok=True)


def finish_motion_dna(source: Path, output: Path):
    """Preserve Seedance's temporal morphs; normalize presentation only."""
    ffmpeg = pipeline.find_ffmpeg()
    subprocess.run([ffmpeg, "-y", "-i", str(source), "-an", "-vf",
                    "scale=720:1280:force_original_aspect_ratio=increase:flags=lanczos,crop=720:1280,eq=saturation=1.04:contrast=1.025:brightness=-0.006,unsharp=5:5:0.28:3:3:0.08",
                    "-c:v", "libx264", "-preset", "slow", "-crf", "16", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(output)],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def run_motion_loop(form, status_cb=None):
    concept = str(form.get("motion_loop_concept") or "").strip() or random_motion_concept()
    duration = MOTION_LOOP_DURATION
    world_arc = motion_world_arc(concept)
    profile = str(form.get("motion_loop_profile") or "lateral").strip().lower()
    if profile not in {"lateral", "tunnel", "orbit"}: profile = "lateral"
    intensity = str(form.get("motion_loop_intensity") or "balanced").strip().lower()
    if intensity not in {"clean", "balanced", "intense"}: intensity = "balanced"
    seamless = str(form.get("motion_loop_seamless") or "").strip().lower() in {"on", "true", "1", "yes"}
    quality = str(form.get("motion_loop_quality") or "economy").strip().lower()
    if quality not in {"economy", "standard", "detail"}: quality = "economy"
    video_model = ("bytedance/seedance-2.0-fast/video-edit"
                   if quality == "economy" else "bytedance/seedance-2.0/video-edit")
    video_resolution = "720p" if quality == "detail" else "480p"
    title = _title(concept)
    slug = agent_core.unique_project_slug(agent_core.slugify(title))
    project_dir = agent_core.PROJECTS_DIR / slug
    input_dir = project_dir / "input"; assets = project_dir / "generated"; renders = project_dir / "renders"; config_dir = project_dir / "config"
    for d in (input_dir, assets, renders, config_dir): d.mkdir(parents=True, exist_ok=True)
    safe_form = {k: v for k, v in form.items() if not str(k).startswith("_")}
    _write_json(input_dir / "run_form.json", safe_form)
    _log(status_cb, f"PROJECT_DIR|{project_dir}")
    _log(status_cb, "Motion Loop budget locked: at most 1 GPT Image 2.0 call and 1 Seedance 2.0 call.")

    key = pipeline.api_key()
    budget_path = config_dir / "generation_budget.json"
    budget = {"gpt_image_2_submissions": 0, "seedance_2_submissions": 0, "limit_each": 1}
    _write_json(budget_path, budget)
    image_prompt = (
        f"Create the opening frame of one original cinematic vertical 9:16 continuous journey. Opening world: {world_arc['opening']}. "
        "This may be an abstract 3D world, impossible architecture, a folded natural landscape, or another cinematic surreal environment; it does not need to look psychedelic. "
        "Strong central route and vanishing point, layered foreground/midground/background for parallax, premium cinematic art direction, controlled color, crisp readable forms, no collage, no split screen, no words, captions, logos or watermark. "
        "Include one integrated, visually elegant first-person anchor in the bottom fifth — for example realistic dark bicycle handlebars with natural hands, a dashboard edge, or another concept-appropriate POV object. "
        "It must belong to the same lighting and perspective as the environment, remain secondary, and contain no neon rods or graphic overlay bars."
    )
    cfg = {"wavespeed": {"image_model": "openai/gpt-image-2/text-to-image", "aspect_ratio": "9:16", "output_format": "png",
                          "video_model": video_model, "video_resolution": video_resolution, "video_generate_audio": False,
                          "video_enable_web_search": False, "seed": -1}}
    budget["gpt_image_2_submissions"] = 1; _write_json(budget_path, budget)  # mark before network; never retry ambiguously
    _log(status_cb, "GPT Image 2.0: submitting the single keyframe...")
    image_id, image_submit = pipeline.submit_wavespeed_image(image_prompt, cfg, key)
    _write_json(config_dir / "generation_manifest.json", {"image": {"id": image_id, "submit": image_submit}, "video": None})
    image_outputs, image_result = pipeline.poll_wavespeed(image_id, key, timeout_s=600, status_cb=status_cb, label="Motion Loop keyframe")
    keyframe = assets / "motion_loop_keyframe.png"
    pipeline.download_file(image_outputs[0], keyframe)

    if not MOTION_DNA_TEMPLATE.exists():
        raise RuntimeError(f"Motion DNA template is missing: {MOTION_DNA_TEMPLATE}")
    motion_template = input_dir / "motion_dna.mp4"
    source_duration = _template_duration(MOTION_DNA_TEMPLATE)
    stretch = duration / max(0.1, source_duration)
    subprocess.run([pipeline.find_ffmpeg(), "-y", "-i", str(MOTION_DNA_TEMPLATE), "-vf", f"setpts={stretch:.8f}*PTS,fps=30", "-t", str(duration), "-an",
                    "-c:v", "libx264", "-preset", "fast", "-crf", "18", "-pix_fmt", "yuv420p", str(motion_template)],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    motion_url, motion_upload = pipeline.upload_media(motion_template, key)
    video_prompt = (
        f"Edit the input video into one uninterrupted 15-second {profile} camera journey. Use the input only as motion DNA: preserve its continuous optical flow, spatial bending rhythm and forward momentum, but replace all source imagery from frame one. "
        f"0-4 seconds: establish this opening world from the reference image: {world_arc['opening']}. "
        f"4-10 seconds: while the camera keeps moving, physical geometry from the opening world continuously bends, grows and transforms into {world_arc['middle']}. Every old element must visibly become part of the new world. "
        f"10-15 seconds: continue the same camera move and organically transform that world into {world_arc['final']}. "
        "Treat all three phases as one unbroken physical space and one shot. Never cut, wipe, dissolve, flash, teleport, fade to black, freeze, reset the camera, or briefly reveal the input footage. Avoid merely swapping backgrounds: use continuous object-to-object and landscape-to-landscape morphs at every change. "
        "Keep the integrated POV anchor natural and visually secondary. No text, logo, watermark, captions or generated audio."
    )
    if seamless:
        video_prompt += " During only the final second, let the final world's geometry naturally curve toward the opening composition so playback can loop, without reversing, dissolving, cutting or freezing."
    budget["seedance_2_submissions"] = 1; _write_json(budget_path, budget)
    _log(status_cb, "Seedance 2.0: submitting the single Motion-DNA video edit...")
    video_payload = {"prompt": pipeline.make_prompt_safe(video_prompt), "video": motion_url,
                     "reference_images": [image_outputs[0]], "duration": duration, "aspect_ratio": "9:16",
                     "resolution": video_resolution, "enable_web_search": False, "generate_audio": False}
    video_submit = pipeline.request_json("POST", f"{pipeline.API_BASE}/{video_model}", key, video_payload, timeout=240)
    video_id = pipeline.unwrap_id(video_submit)
    _write_json(config_dir / "generation_manifest.json", {"image": {"id": image_id, "submit": image_submit, "result": image_result},
                                                            "video": {"id": video_id, "payload": video_payload, "submit": video_submit}})
    video_outputs, video_result = pipeline.poll_wavespeed(video_id, key, timeout_s=900, status_cb=status_cb, label="Motion Loop Seedance")
    raw = assets / "motion_loop_seedance_raw.mp4"; pipeline.download_file(video_outputs[0], raw)
    final = renders / f"{slug}_motion_loop.mp4"
    _log(status_cb, "Local finish: preserving generated morph timing and normalizing the vertical master...")
    finish_motion_dna(raw, final)
    poster = renders / "thumbnail.jpg"
    pipeline.extract_poster_frame(final, poster, at=min(1.0, duration / 3))
    config = {"project_slug": slug, "title": title, "kind": "motion_loop", "width": 720, "height": 1280, "fps": 30,
              "duration": duration, "motion_loop": {"concept": concept, "profile": profile, "intensity": intensity, "seamless": seamless,
              "world_arc": world_arc,
              "quality": quality, "video_model": video_model, "video_resolution": video_resolution,
              "paid_generation_limit": {"gpt_image_2": 1, "seedance_2": 1}},
              "scenes": [{"id": "motion_loop", "name": title, "start": 0, "duration": duration, "clip": str(raw)}],
              "final_output": str(final)}
    _write_json(config_dir / "project.json", config)
    _write_json(config_dir / "generation_manifest.json", {"image": {"id": image_id, "submit": image_submit, "result": image_result},
                                                            "video": {"id": video_id, "payload": video_payload, "submit": video_submit, "result": video_result}})
    _log(status_cb, f"Motion Loop complete: {final.name}")
    return {"title": title, "project_dir": str(project_dir), "video": str(final), "thumbnail": str(poster), "kind": "motion_loop"}
