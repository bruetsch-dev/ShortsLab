"""Three frame-linked Seedance 2.5 AI Motion chapters via Higgsfield Unlimited."""
from __future__ import annotations

import json
import hashlib
import random
import re
import shutil
import subprocess
from pathlib import Path

import agent_core
import higgsfield_login
import pipeline


CHAPTERS = 3
CHAPTER_SECONDS = 10
PROMPT_DIRECTOR_MODEL = "google/gemini-3.1-flash-lite"

POV_RIGS = {
    "mountain_bike": "helmet-mounted mountain-bike POV; burgundy-red gloved hands, a silver handlebar, front tire edge and bicycle frame stay stable and recognizable in the lower third",
    "e_scooter": "e-scooter rider POV; two natural hands on a matte-black scooter handlebar, compact dashboard and front stem stay stable and recognizable in the lower third",
    "car": "driver POV from inside a modern car; two natural hands on the steering wheel, dashboard and windshield edge stay stable and recognizable in the lower third",
    "cabriolet": "open-top cabriolet driver POV; natural hands on a leather steering wheel, low windshield edge and dashboard stay stable and recognizable in the lower third",
    "motorcycle": "helmet-mounted motorcycle POV; black leather gloves, motorcycle handlebar, fuel-tank edge and small gauges stay stable and recognizable in the lower third",
}

_WORLD_SEEDS = [
    "an immense Alpine mountain pass whose switchback road folds upward into the sky, with inverted valleys and clouds flowing below the rider",
    "a minimal abstract 3D world of ivory arches, soft chrome spheres and impossible gravity, with the route continuing across vertical walls",
    "a glacial canyon made of translucent blue ice where entire mountains float overhead and waterfalls travel upward",
    "a warm sandstone desert shaped like a continuous Moebius strip, with monumental shadows and levitating black monoliths",
    "a brutalist city above the clouds whose buildings bend into a planetary ring around the horizon",
    "an enormous botanical cathedral where tree trunks become vaulted architecture and petals behave like moving stained glass",
]

# Concrete, visible signifiers let a viewer recognise a reference world without relying on a
# generic colour palette or an on-screen label. They are intentionally scenery, not logos/text.
_REFERENCE_LANDMARKS = {
    "hobbiton": "round earth-sheltered homes with circular doors, garden fences, a party-tree silhouette, sheep-dotted hills and a winding country lane",
    "edoras": "a solitary hilltop hall/fortress, golden grass, a braided river below and sharp Southern Alps behind it",
    "arrakis": "rust-red sandstone mesas, rippling monumental dunes, a narrow sand track and tiny twin suns over the horizon",
    "mordor": "black volcanic ash, steaming fissures, jagged lava rock, a distant dark fortress silhouette and a crimson volcanic glow",
    "hyrule": "floating grassy sky islands, ancient stone bridges, waterfalls dropping into clouds and a distant castle-like skyline",
    "limgrave": "a ruined stone church, a colossal radiant golden tree on the horizon, rolling grassland and distant castle walls",
    "skyrim": "snowy Nordic peaks, dark pine forest, a cold stream and massive weathered Dwemer-like stone ruins",
    "night city": "rain-wet elevated streets, stacked neon facades, a monorail silhouette, saturated reflections and dense blue-magenta haze",
    "elwynn": "sunlit oak forest, a broad cobblestone road, red-roofed human village buildings, a stone bridge over a clear stream and distant cathedral-like spires",
    "world of warcraft": "a stylised-but-photoreal fantasy road, oversized ancient trees, a fortified village silhouette, stone bridges and heroic high-fantasy landscape scale",
}


def random_motion_concept():
    return f"First-person continuous journey through {random.choice(_WORLD_SEEDS)} with a stable, natural vehicle POV."


def _log(cb, message):
    if cb:
        cb(message)


def _title(concept: str) -> str:
    words = re.findall(r"[A-Za-z0-9'-]+", concept)
    return " ".join(words[:7]).strip().title() or "AI Motion"


def _landmark_cues(concept: str) -> str:
    text = str(concept or "").lower()
    cues = [cue for key, cue in _REFERENCE_LANDMARKS.items() if key in text]
    return "; ".join(dict.fromkeys(cues))


def _write_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _last_frame(source: Path, output: Path):
    ffmpeg = pipeline.find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("ffmpeg is required to link AI Motion chapters.")
    ffprobe = pipeline.find_ffprobe(ffmpeg)
    duration = 0.0
    if ffprobe:
        try:
            result = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration",
                                     "-of", "default=noprint_wrappers=1:nokey=1", str(source)],
                                    check=True, capture_output=True, text=True, timeout=20)
            duration = float(result.stdout.strip() or 0.0)
        except (OSError, ValueError, subprocess.SubprocessError):
            duration = 0.0
    # FFmpeg 8.1's -sseof produces zero frames for some Twitter-vork MP4s. A normal input seek
    # just before the measured end is reliable and works with the native png encoder.
    seek = max(0.0, duration - 0.25) if duration > 0.25 else max(0.0, CHAPTER_SECONDS - 0.25)
    subprocess.run([ffmpeg, "-y", "-ss", f"{seek:.3f}", "-i", str(source), "-frames:v", "1", "-update", "1", str(output)],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if not output.exists() or output.stat().st_size < 1000:
        raise RuntimeError(f"Could not extract the final frame from {source.name}.")


def _file_digest(path: Path) -> str:
    """Stable content identity for rejecting a stale Higgsfield result card."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _assemble(clips: list[Path], output: Path):
    ffmpeg = pipeline.find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("ffmpeg is required to assemble AI Motion.")
    listing = output.with_suffix(".concat.txt")
    listing.write_text("".join("file '" + str(p.resolve()).replace("\\", "/").replace("'", "'\\''") + "'\n" for p in clips), encoding="utf-8")
    try:
        subprocess.run([ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", str(listing), "-vf",
                        "scale=720:1280:force_original_aspect_ratio=increase:flags=lanczos,crop=720:1280,fps=30",
                        "-c:v", "libx264", "-preset", "slow", "-crf", "16", "-pix_fmt", "yuv420p",
                        # Seedance often delivers its native ambience around -30 dBFS. Bring
                        # that source audio up to a consistent audible ASMR level without
                        # adding music or fabricated SFX.
                        "-af", "loudnorm=I=-18:TP=-1.5:LRA=7",
                        "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(output)],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    finally:
        listing.unlink(missing_ok=True)


def _surrealness_rule(level: int) -> str:
    return {
        1: "Keep the setting entirely realistic; use only natural scale, weather, light and camera parallax.",
        2: "Keep the setting believable, with one restrained magical-realist detail such as subtly unusual scale, gravity or light.",
        3: "Make the environment clearly surreal but coherent: at least two large, readable impossible-world elements evolve gradually while the route remains legible.",
        4: "Make the environment intensely surreal and visually obvious: monumental terrain, architecture, horizon and gravity continuously reshape in smooth, physically convincing morphs around the stable POV rig.",
        5: "Make the environment radically surreal and mind-bending: the whole visible world performs continuous large-scale transformations—folding mountains, rotating streets, flowing gravity, impossible architecture and living landscapes—without cuts or visual glitches. The hands, vehicle, handlebar and camera rig must remain perfectly stable.",
    }[max(1, min(5, int(level or 3)))]


def _transformation_timeline(level: int) -> str:
    """Put non-optional visual beats early in the Seedance prompt, not as a soft suffix."""
    level = max(1, min(5, int(level or 3)))
    if level <= 2:
        return ""
    base = (
        "MANDATORY VISIBLE ENVIRONMENT TRANSFORMATIONS — this must NOT be a normal scenic ride. "
        "Between seconds 2–3, perform a first slow, clearly visible environmental transformation; "
        "between seconds 4–6, perform a second different transformation; between seconds 7–9, perform a third transformation. "
        "Each change must develop continuously on screen, with smooth optical flow and physical parallax—never a cut, flash or camera trick. "
        "Only the surrounding world transforms. The rider's hands, handlebar, vehicle, lower-third composition and forward camera motion remain locked and realistic. "
    )
    if level == 3:
        return base + "Use readable magical-realist changes such as a hillside slowly lifting, an arch growing from a cliff, or water beginning to run uphill. "
    if level == 4:
        return base + "Use unmistakable large-scale morphs: mountains fold into bridges, roads curl through the air, valleys rotate, buildings grow into organic forms, and gravity visibly redirects. "
    return base + (
        "Use radical, unmistakable reality-bending morphs: the mountain range slowly bends into an overhead archway, the road twists into a vertical ribbon, "
        "the horizon rolls around the rider, waterfalls flow upward into floating islands, and whole districts continuously reconfigure into impossible but detailed architecture. "
        "A plain real-world trail, ordinary city ride or merely pretty landscape is an invalid result. "
    )


def _chapter_prompt(concept: str, profile: str, pov: str, speed: int, chapter: int, has_first_frame: bool, loop: bool, surrealness: int = 3):
    camera = {
        "lateral": "A smooth lateral move with natural parallax and forward momentum.",
        "tunnel": "A hypnotic forward tunnel pull with a clear vanishing point.",
        "orbit": "A broad graceful orbit with a physically coherent route.",
    }[profile]
    input_instruction = (
        "Use the supplied first frame as the exact visual continuation. Preserve its foreground, perspective, lighting and motion direction before evolving naturally."
        if has_first_frame else
        "Create the opening from this prompt alone: do not expect or require an input image."
    )
    speed_direction = {
        1: "Move very slowly and calmly, like a relaxed scenic glide.",
        2: "Move at an easy, relaxed touring pace.",
        3: "Move at a confident cruising pace with readable surroundings.",
        4: "Move quickly with controlled, stable forward energy.",
        5: "Move fast and exhilaratingly, but keep the camera stabilized and the surroundings readable.",
    }[speed]
    loop_hint = " In the final second, curve the world subtly toward the opening composition without a cut or crossfade." if loop and chapter == CHAPTERS else ""
    landmark_rule = (" Make these recognisable scenery landmarks visibly part of the route and parallax, never just a generic theme or a label: "
                     + _landmark_cues(concept) + "." if _landmark_cues(concept) else "")
    return (
        f"Vertical 9:16, one continuous native {CHAPTER_SECONDS}-second cinematic {POV_RIGS[pov]}, chapter {chapter} of {CHAPTERS}, through {concept}. {_transformation_timeline(surrealness)} {camera} {speed_direction} "
        f"{input_instruction} {_surrealness_rule(surrealness)} "
        "This must be authentic first-person POV, never a third-person rider shot, drone shot, empty landscape shot or detached camera. "
        "No cuts, wipes, dissolves, flashes, teleports, freeze frames, black frames, zoom bursts, text, captions, logos, watermark, music or dialogue. "
        "Generate a continuous, synchronized native ASMR environmental soundtrack for the entire shot; never leave the video silent. "
        "Match the sound to the exact world and movement: birds, light wind, leaves and gravel crunch under a bicycle in a rural medieval landscape; "
        "tire or road texture, subtle vehicle mechanics, water, distant wildlife, ice creaks or city air whenever those elements are visible. "
        "Keep it immersive, detailed and relaxed at a natural level, with no music, beat, voice, narration, artificial whooshes or loud impact effects."
        f"{landmark_rule}"
        f"{loop_hint}"
    )


def _infer_pov(raw_pov, concept: str) -> str:
    """Respect an explicit POV control, while making dragged POV tags safe for older UI state."""
    selected = str(raw_pov or "").strip().lower()
    text = str(concept or "").lower().replace("‑", "-")
    # Older saved AI Motion forms only put the clicked tag in the instruction field.
    # An explicit vehicle word must never silently turn into the mountain-bike default.
    tagged = next((key for key, tokens in (
        ("e_scooter", ("e-scooter", "escooter", "electric scooter", "e scooter")),
        ("motorcycle", ("motorcycle", "motorbike")),
        ("cabriolet", ("cabriolet", "convertible", "open-top car")),
        ("car", ("driver pov", "car pov", "inside a car")),
        ("mountain_bike", ("mountain bike", "mountain-bike", "bicycle pov", "bike pov")),
    ) if any(token in text for token in tokens)), None)
    if tagged and (selected not in POV_RIGS or selected == "mountain_bike"):
        return tagged
    return selected if selected in POV_RIGS else "mountain_bike"


def _locked_constraints(pov: str, speed: int, chapter: int, has_first_frame: bool, loop: bool, surrealness: int = 3) -> str:
    speed_direction = {
        1: "Move very slowly and calmly, like a relaxed scenic glide.",
        2: "Move at an easy, relaxed touring pace.",
        3: "Move at a confident cruising pace with readable surroundings.",
        4: "Move quickly with controlled, stable forward energy.",
        5: "Move fast and exhilaratingly, but keep the camera stabilized and the surroundings readable.",
    }[speed]
    input_rule = (
        "Use the supplied first frame as the exact visual continuation; preserve its foreground, perspective, lighting and motion direction."
        if has_first_frame else "Create the opening from the prompt alone; no input image is required."
    )
    vehicle_audio = {
        "e_scooter": "a subtle electric scooter motor hum, tire texture and wind",
        "mountain_bike": "freewheel ticks, tire texture, gravel and wind",
        "motorcycle": "a restrained real motorcycle engine, road texture and wind",
        "car": "subtle cabin, road and wind sound",
        "cabriolet": "open-air road, wind and restrained engine sound",
    }[pov]
    loop_rule = " In the final second curve subtly toward the opening composition, with no cut or crossfade." if loop and chapter == CHAPTERS else ""
    return (
        "\n\nNON-NEGOTIABLE PRODUCTION CONSTRAINTS: "
        f"Vertical 9:16. {POV_RIGS[pov]}. This is authentic first-person POV: never a bicycle, third-person rider, drone, empty landscape, or detached camera. "
        f"{speed_direction} {input_rule} "
        "One continuous native 10-second take: no cuts, wipes, dissolves, flashes, teleports, freeze frames, black frames, zoom bursts, text, captions, logos, watermark, music, dialogue, artificial whooshes or impact sounds. "
        f"Generate a synchronized native ASMR environmental soundtrack using only source ambience appropriate to what is visible: {vehicle_audio}, plus realistic water, wildlife, leaves, gravel, ice or city air where visible. "
        "Keep every vehicle component stable, recognizable and present in the lower third for the whole shot."
        f" {_surrealness_rule(surrealness)} {_transformation_timeline(surrealness)}"
        " Treat this as a coherent guided city-discovery ride: travel along a legible street or path, reveal multiple distinct blocks or landmark zones through forward motion and parallax, "
        "and give each landmark enough screen time to be recognised. Do not make it a stationary landscape montage."
        f"{loop_rule}"
    )


def _gemini_prompt_plan(concept: str, profile: str, pov: str, speed: int, seamless: bool, surrealness: int = 3, status_cb=None) -> list[str]:
    """Use Flash Lite once to direct all three chapters; local constraints prevent model drift."""
    profile_direction = {
        "lateral": "a smooth lateral move with natural parallax and forward momentum",
        "tunnel": "a hypnotic forward pull with a clear vanishing point",
        "orbit": "a broad graceful orbit while the route stays physically coherent",
    }[profile]
    _log(status_cb, "AI Motion: Gemini 3.1 Flash Lite is directing the three linked Seedance prompts.")
    messages = [
        {"role": "system", "content": (
            "You are a specialist prompt director for Seedance 2.5 image-to-video. Return strict JSON only: "
            '{"chapters":[{"prompt":"..."},{"prompt":"..."},{"prompt":"..."}]}. '
            "Write three distinct, cinematic 10-second chapters of one continuous vertical first-person city-discovery journey. "
            "Chapter 1 opens the world; chapters 2 and 3 continue naturally from the previous final frame. "
            "The rider must pass through several readable streets/landmark zones like a compelling guided city tour, not remain in a wilderness panorama or a stationary scenic montage. "
            "Describe visuals, coherent motion, continuity, and specific source-only ASMR ambience. Never propose music, narration, generic synth ambience, whooshes, or impacts. "
            "Do not contradict the locked vehicle POV supplied by the user. If the instruction names a famous film/game place, "
            "make it recognisable through at least three specific, visible in-world landmarks integrated into the route, composition and parallax. "
            "Never use a label, logo, UI, title card, or a vague generic setting in place of those landmarks."
            " Respect the supplied surrealness level. At higher levels, make environmental transformations unmistakable and continuous while locking the POV vehicle, hands and camera rig."
        )},
        {"role": "user", "content": json.dumps({
            "user_world_or_instruction": concept,
            "camera_path": profile_direction,
            "locked_pov": POV_RIGS[pov],
            "speed_level": speed,
            "seamless_final_loop": seamless,
            "surrealness_level": surrealness,
            "surrealness_direction": _surrealness_rule(surrealness),
            "required_recognisable_landmarks": _landmark_cues(concept) or None,
            "chapter_count": CHAPTERS,
            "chapter_duration_seconds": CHAPTER_SECONDS,
        }, ensure_ascii=False)},
    ]
    try:
        data = agent_core._post_llm_json(PROMPT_DIRECTOR_MODEL, messages, 1400, 0.35, timeout=120) or {}
        raw = data.get("chapters") if isinstance(data, dict) else None
        if isinstance(raw, list) and len(raw) == CHAPTERS:
            directed = [str(item.get("prompt") or "").strip() if isinstance(item, dict) else "" for item in raw]
            if all(len(item) >= 80 for item in directed):
                landmark_lock = ("\nREFERENCE-LOCATION RECOGNISABILITY: Make these actual scenery elements clearly visible, "
                                 "not merely implied or named: " + _landmark_cues(concept) + "."
                                 if _landmark_cues(concept) else "")
                return [_transformation_timeline(surrealness) + item + landmark_lock + _locked_constraints(pov, speed, index, index > 1, seamless, surrealness)
                        for index, item in enumerate(directed, 1)]
        _log(status_cb, "AI Motion: Gemini returned an incomplete prompt plan; using the safe local director plan.")
    except Exception as exc:  # Prompt planning must never block a paid video run.
        _log(status_cb, f"AI Motion: Gemini Flash Lite prompt planning failed ({exc.__class__.__name__}); using the safe local director plan.")
    return [_chapter_prompt(concept, profile, pov, speed, index, index > 1, seamless, surrealness)
            for index in range(1, CHAPTERS + 1)]


def run_motion_loop(form, status_cb=None):
    """Create 3×10-second Seedance 2.5 chapters, each frame-linked to the prior one."""
    concept = str(form.get("motion_loop_concept") or "").strip() or random_motion_concept()
    profile = str(form.get("motion_loop_profile") or "lateral").strip().lower()
    if profile not in {"lateral", "tunnel", "orbit"}:
        profile = "lateral"
    pov = _infer_pov(form.get("motion_loop_pov"), concept)
    seamless = str(form.get("motion_loop_seamless") or "").strip().lower() in {"on", "true", "1", "yes"}
    try:
        speed = max(1, min(5, int(float(form.get("motion_loop_speed") or 3))))
    except (TypeError, ValueError):
        speed = 3
    try:
        surrealness = max(1, min(5, int(float(form.get("motion_loop_surrealness") or 3))))
    except (TypeError, ValueError):
        surrealness = 3
    if str(form.get("motion_loop_unlimited") or "").lower() not in {"on", "true", "1", "yes"}:
        raise RuntimeError("AI Motion requires Higgsfield Unlimited enabled.")
    use_unlimited = True
    uploaded_raw = str(form.get("motion_loop_first_frame") or "").strip()
    uploaded = Path(uploaded_raw).expanduser() if uploaded_raw else None
    first_frame = uploaded if uploaded and uploaded.is_file() else None
    if uploaded is not None and first_frame is None:
        raise RuntimeError("The uploaded AI Motion first-frame image could not be found.")

    title = _title(concept)
    resume_slug = str(form.get("motion_loop_resume_project") or "").strip()
    candidate = (agent_core.PROJECTS_DIR / resume_slug).resolve() if resume_slug else None
    projects_root = agent_core.PROJECTS_DIR.resolve()
    if candidate is not None and candidate.parent == projects_root and candidate.is_dir():
        project_dir, slug = candidate, candidate.name
        _log(status_cb, f"AI Motion: resuming {slug}; completed chapters will be kept.")
    else:
        slug = agent_core.unique_project_slug(agent_core.slugify(title))
        project_dir = agent_core.PROJECTS_DIR / slug
    input_dir, generated = project_dir / "input", project_dir / "generated"
    renders, config_dir = project_dir / "renders", project_dir / "config"
    for folder in (input_dir, generated, renders, config_dir):
        folder.mkdir(parents=True, exist_ok=True)
    _write_json(input_dir / "run_form.json", {k: v for k, v in form.items() if not str(k).startswith("_")})
    _log(status_cb, f"PROJECT_DIR|{project_dir}")
    _log(status_cb, "AI Motion: three native 10-second Seedance 2.5 chapters through Higgsfield Unlimited.")
    cancel = form.get("_cancel_event")
    cancelled = lambda: bool(cancel and cancel.is_set())
    stored = {}
    try:
        data = json.loads((config_dir / "generation_manifest.json").read_text(encoding="utf-8"))
        stored = {int(item.get("index")): item for item in data.get("chapters", []) if isinstance(item, dict)}
    except (OSError, ValueError, TypeError):
        pass
    clips, chapters = [], []
    prior_frame = first_frame
    stored_digests = set()
    for index in range(1, CHAPTERS + 1):
        item = stored.get(index, {})
        clip_path, frame_path = Path(str(item.get("clip") or "")), Path(str(item.get("last_frame") or ""))
        if clip_path.is_file() and clip_path.stat().st_size > 1000 and frame_path.is_file() and frame_path.stat().st_size > 100:
            digest = _file_digest(clip_path)
            if digest in stored_digests:
                _log(status_cb, f"AI Motion: chapter {index} is a duplicate of an earlier chapter; regenerating it instead of treating it as complete.")
                break
            stored_digests.add(digest)
            clips.append(clip_path); chapters.append(item); prior_frame = frame_path
            _log(status_cb, f"AI Motion: keeping completed chapter {index}/{CHAPTERS}.")
        else:
            break
    clip_digests = {_file_digest(path) for path in clips if path.is_file()}
    start_index = len(chapters) + 1
    manual_chapter_gate = form.get("_motion_manual_chapter_gate")
    if start_index <= CHAPTERS and manual_chapter_gate:
        # The provider rejects video submits from a Playwright-controlled page even after a
        # human click. Every Higgsfield action stays in normal Chrome; Shortslab only prepares
        # prompts/continuity frames and imports the completed MP4s.
        higgsfield_login.open_manual_seedance_browser(status_cb=status_cb)
        prompts = _gemini_prompt_plan(concept, profile, pov, speed, seamless, surrealness, status_cb=status_cb)
    elif start_index <= CHAPTERS:
        # begin_seedance_25_video_session waits for the user's chosen preflight state instead
        # of submitting underneath a human verification or a still-loading control bar.
        # A user-owned human verification is not a failure condition. Keep the project alive
        # until its explicit Cancel action rather than converting a slow CAPTCHA/preflight into
        # a failed run before the first Seedance chapter can start.
        if not higgsfield_login.begin_seedance_25_video_session(status_cb=status_cb, cancel_check=cancelled,
                                                                 timeout_s=None, unlimited_required=use_unlimited,
                                                                 ready_gate=form.get("_motion_preflight_gate")):
            raise RuntimeError("Higgsfield AI Motion preflight was cancelled before Seedance generation started.")
        prompts = _gemini_prompt_plan(concept, profile, pov, speed, seamless, surrealness, status_cb=status_cb)
    else:
        prompts = [str(stored.get(i, {}).get("prompt") or "") for i in range(1, CHAPTERS + 1)]
    for index in range(start_index, CHAPTERS + 1):
        if cancelled():
            raise pipeline.PipelineCancelled("Cancelled.")
        prompt = prompts[index - 1]
        clip = generated / f"ai_motion_chapter_{index:02d}.mp4"
        _log(status_cb, f"AI Motion chapter {index}/{CHAPTERS}: {'using uploaded first frame' if index == 1 and prior_frame else 'using the previous chapter final frame' if prior_frame else 'prompt-only opening'}.")
        created = None
        frame_linked = bool(prior_frame)
        if manual_chapter_gate:
            imported = Path(manual_chapter_gate(index, prompt, prior_frame))
            if not imported.is_file():
                raise RuntimeError(f"AI Motion chapter {index} was not imported.")
            shutil.copy2(imported, clip)
            digest = _file_digest(clip)
            if digest in clip_digests:
                clip.unlink(missing_ok=True)
                raise RuntimeError(f"AI Motion chapter {index} is identical to an earlier chapter. Generate a new chapter and import that instead.")
            clip_digests.add(digest)
            created = str(clip)
        else:
            for attempt in range(1, 4):
                created = higgsfield_login.generate_seedance_25_video_sync(prompt, clip, first_frame=prior_frame, status_cb=status_cb, timeout_s=900)
                if created in {"UNLIMITED_OFF", "CAPTCHA", "PREFLIGHT_REQUIRED"}:
                    _log(status_cb, "Higgsfield needs its manual preflight checked again; waiting before retrying this same chapter.")
                    if not higgsfield_login.wait_for_seedance_25_unlimited_sync(status_cb=status_cb, cancel_check=cancelled, timeout_s=None):
                        raise RuntimeError("Higgsfield AI Motion preflight was cancelled before the chapter could continue.")
                    continue
                if created and clip.exists():
                    digest = _file_digest(clip)
                    if digest not in clip_digests:
                        clip_digests.add(digest)
                        break
                    _log(status_cb, f"AI Motion chapter {index}: Higgsfield returned an already-used video; waiting for the new chapter instead of duplicating it.")
                    created = None
                    clip.unlink(missing_ok=True)
                if attempt < 3:
                    _log(status_cb, f"AI Motion chapter {index} did not return a video yet; retrying its frame-link ({attempt + 1}/3).")
            # Seedance may reject a newly-extracted PNG reference without exposing a useful UI error.
            # First keep the intended frame-link three times, then preserve the project with one
            # explicit prompt-continuity submission rather than discarding the completed chapter.
            if (not created or not clip.exists()) and prior_frame:
                _log(status_cb, f"AI Motion chapter {index}: Higgsfield rejected the frame-link three times; submitting one prompt-continuity fallback instead of stopping the project.")
                continuation_prompt = (
                    "CONTINUITY FALLBACK: Continue the exact same vehicle, camera direction, lighting, world and foreground composition from the immediately previous shot; "
                    "do not reset, cut, change vehicle or introduce a new location.\n" + prompt
                )
                created = higgsfield_login.generate_seedance_25_video_sync(continuation_prompt, clip, first_frame=None,
                                                                             status_cb=status_cb, timeout_s=900)
                frame_linked = False
                if created and clip.exists():
                    digest = _file_digest(clip)
                    if digest in clip_digests:
                        _log(status_cb, f"AI Motion chapter {index}: continuity fallback also returned a duplicate; keeping the project resumable instead of repeating footage.")
                        created = None
                        clip.unlink(missing_ok=True)
                    else:
                        clip_digests.add(digest)
        if not created or not clip.exists():
            raise RuntimeError(f"Seedance 2.5 did not return AI Motion chapter {index}. Completed chapters remain in the project.")
        next_frame = generated / f"ai_motion_chapter_{index:02d}_last_frame.png"
        _last_frame(clip, next_frame)
        clips.append(clip)
        chapters.append({"index": index, "duration": CHAPTER_SECONDS, "prompt": prompt, "clip": str(clip),
                         "first_frame": str(prior_frame) if prior_frame and frame_linked else None,
                         "frame_linked": frame_linked, "last_frame": str(next_frame)})
        _write_json(config_dir / "generation_manifest.json", {"provider": "Higgsfield", "model": "Seedance 2.5", "chapters": chapters})
        prior_frame = next_frame

    final = renders / f"{slug}_ai_motion.mp4"
    _log(status_cb, "AI Motion: joining three frame-linked chapters into a 30-second master.")
    _assemble(clips, final)
    poster = renders / "thumbnail.jpg"
    pipeline.extract_poster_frame(final, poster, at=2)
    project = {"project_slug": slug, "title": title, "kind": "motion_loop", "width": 720, "height": 1280, "fps": 30,
               "duration": CHAPTERS * CHAPTER_SECONDS,
               "motion_loop": {"concept": concept, "profile": profile, "pov": pov, "speed": speed, "seamless": seamless, "model": "Seedance 2.5",
                               "provider": "Higgsfield", "unlimited_required": use_unlimited, "surrealness": surrealness, "chapters": CHAPTERS,
                               "chapter_seconds": CHAPTER_SECONDS, "initial_first_frame": str(first_frame) if first_frame else None,
                               "native_audio": "required contextual ASMR ambience; no music or dialogue"},
               "scenes": [{"id": f"ai_motion_{i:02d}", "name": f"AI Motion chapter {i}", "start": (i - 1) * CHAPTER_SECONDS,
                           "duration": CHAPTER_SECONDS, "clip": str(path)} for i, path in enumerate(clips, 1)], "final_output": str(final)}
    _write_json(config_dir / "project.json", project)
    _log(status_cb, f"AI Motion complete: {final.name}")
    return {"title": title, "project_dir": str(project_dir), "video": str(final), "thumbnail": str(poster), "kind": "motion_loop"}
