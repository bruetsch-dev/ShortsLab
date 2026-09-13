"""Three connected Seedance 2.5 POV worlds generated through Higgsfield Unlimited."""
from __future__ import annotations

import json
import random
import re
import subprocess
from pathlib import Path

import agent_core
import higgsfield_login
import pipeline


CHAPTERS = 3
SECONDS = 10

POVS = {
    "mountain_bike": ("helmet-mounted mountain-bike POV", "burgundy-red gloved hands, silver handlebar, bicycle frame and front tire edge"),
    "e_scooter": ("e-scooter rider POV", "two natural hands on a matte-black e-scooter handlebar, compact dashboard and front stem"),
    "car": ("driver POV", "a clean modern car dashboard, steering wheel and two natural hands"),
    "cabriolet": ("open-top cabriolet driver POV", "a low windshield edge, leather steering wheel and natural hands"),
    "motorcycle": ("helmet-mounted motorcycle POV", "black leather gloves, stable motorcycle handlebar, fuel tank edge and small analog gauges"),
}
WORLDS = [
    "a monumental living green aqueduct beside a dusty Alpine trail, its tree-covered arches opening onto a sunlit valley of geometric fields and tiny farmhouses",
    "an impossible Alpine pass where switchback roads fold into the sky and mirror lakes sit between immense mossy cliffs",
    "a golden desert canyon where sandstone arches grow into floating terraces above an ocean of clouds",
    "a glacial blue valley of translucent ice walls, upward waterfalls and distant mountains floating over the route",
    "an immense botanical cathedral where tree trunks become arches and luminous roots form a road through a warm valley",
    "a volcanic highland of black basalt, mirror streams and monumental soft-lit monoliths emerging from low cloud",
]


def _log(cb, text):
    if cb:
        cb(text)


def _write(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _last_frame(source: Path, output: Path):
    ffmpeg = pipeline.find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("ffmpeg is required to carry the last frame into the next Seedance chapter.")
    subprocess.run([ffmpeg, "-y", "-sseof", "-0.08", "-i", str(source), "-frames:v", "1", "-q:v", "2", str(output)],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if not output.exists() or output.stat().st_size < 1000:
        raise RuntimeError(f"Could not extract the continuation frame from {source.name}.")


def _assemble(clips: list[Path], output: Path):
    ffmpeg = pipeline.find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("ffmpeg is required to assemble the final POV journey.")
    listing = output.with_suffix(".concat.txt")
    # concat demuxer wants forward slash paths on Windows too; single quotes are escaped.
    listing.write_text("".join("file '" + str(p.resolve()).replace("\\", "/").replace("'", "'\\''") + "'\n" for p in clips), encoding="utf-8")
    try:
        subprocess.run([ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", str(listing),
                        "-vf", "scale=720:1280:force_original_aspect_ratio=increase:flags=lanczos,crop=720:1280,fps=30",
                        "-c:v", "libx264", "-preset", "medium", "-crf", "17", "-c:a", "aac", "-b:a", "160k",
                        "-movflags", "+faststart", str(output)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    finally:
        listing.unlink(missing_ok=True)


def _prompt(pov: str, rig: str, world: str, next_world: str | None, chapter: int,
            camera: str, transform: str, atmosphere: str, instructions: str) -> str:
    continuation = ""
    if next_world:
        continuation = (
            f" In the final three seconds, the distant landscape begins a slow, physically continuous transformation toward {next_world}. "
            "The route, forward movement, foreground vehicle and hands stay stable while only the far world evolves."
        )
    camera_direction = {
        "calm": "Use a calm, smooth stabilized forward glide with a slow lateral drift.",
        "dynamic": "Use a lively but stabilized forward ride with controlled lean, natural road vibration and strong parallax.",
        "orbit": "Use a slow continuous forward ride with a wide, graceful lateral orbit and strong natural parallax.",
    }.get(camera, "Use a calm, smooth stabilized forward glide with a slow lateral drift.")
    transform_direction = {
        "subtle": "World changes remain subtle, slow and physically plausible.",
        "bold": "The distant world can make bold dreamlike transformations while the foreground stays stable and real.",
        "balanced": "The distant world evolves in a striking but physically continuous surreal way.",
    }.get(transform, "The distant world evolves in a striking but physically continuous surreal way.")
    audio_direction = {
        "wind": "wind through the route, vehicle movement, road texture and distant natural detail",
        "quiet": "quiet environmental room tone, restrained wind and subtle natural detail",
        "nature": "wind, tires or road texture, foliage, distant birds and subtle environmental detail",
    }.get(atmosphere, "wind, tires or road texture, foliage, distant birds and subtle environmental detail")
    user_direction = f" Additional director instructions for every chapter: {instructions.strip()}" if instructions.strip() else ""
    return (
        f"Vertical 9:16, one continuous {SECONDS}-second {pov}. Keep {rig} stable, recognizable and physically consistent in the lower third throughout. "
        f"The camera travels smoothly forward through {world}. {camera_direction} Use a 24mm action-sports lens. "
        "The setting is cinematic magical realism: immense and surreal but photorealistic, physically present and detailed. Background architecture and terrain may subtly bend or reform, "
        f"never the hands, vehicle or route. {transform_direction} Golden-hour light, rich believable color, soft lens flare, atmospheric haze, moving foliage or dust. "
        f"Native audio is quiet ASMR nature and vehicle sound only: {audio_direction}. No music, score, dialogue, voice, text, logo or watermark. "
        "One unbroken take: no cuts, dissolves, teleporting, zoom bursts, black frames, freeze frames, extra limbs, changing vehicle, melting handlebars, cartoon, painting, CGI or video-game look."
        f" This is connected chapter {chapter} of {CHAPTERS}.{continuation}{user_direction}"
    )


def run_pov_journey(form, status_cb=None):
    if str(form.get("pov_journey_unlimited") or "").lower() not in {"on", "true", "1", "yes"}:
        raise RuntimeError("POV Journey only runs with Higgsfield Unlimited enabled.")
    key = str(form.get("pov_journey_pov") or "mountain_bike").lower()
    pov, rig = POVS.get(key, POVS["mountain_bike"])
    opening = str(form.get("pov_journey_direction") or "").strip() or random.choice(WORLDS)
    camera = str(form.get("pov_journey_camera") or "calm").strip().lower()
    transform = str(form.get("pov_journey_transform") or "balanced").strip().lower()
    atmosphere = str(form.get("pov_journey_atmosphere") or "nature").strip().lower()
    instructions = str(form.get("pov_journey_instructions") or "").strip()[:1600]
    choices = [world for world in WORLDS if world != opening]
    random.shuffle(choices)
    worlds = [opening, choices[0], choices[1]]
    title = " ".join(re.findall(r"[A-Za-z0-9'-]+", f"{pov} {opening}")[:6]).title() or "Endless POV Journey"
    slug = agent_core.unique_project_slug(agent_core.slugify(title))
    project_dir = agent_core.PROJECTS_DIR / slug
    generated, renders, config = project_dir / "generated", project_dir / "renders", project_dir / "config"
    for folder in (generated, renders, config, project_dir / "input"):
        folder.mkdir(parents=True, exist_ok=True)
    _write(project_dir / "input" / "run_form.json", {k: v for k, v in form.items() if not str(k).startswith("_")})
    _log(status_cb, f"PROJECT_DIR|{project_dir}")
    _log(status_cb, "Opening Higgsfield Seedance 2.5. Unlimited must be ON before every chapter; no API credit is used.")
    cancel = form.get("_cancel_event")
    cancelled = lambda: bool(cancel and cancel.is_set())
    if not higgsfield_login.begin_seedance_25_video_session(status_cb=status_cb, cancel_check=cancelled):
        raise RuntimeError("Higgsfield Seedance 2.5 is not ready with Unlimited enabled.")

    clips, manifest = [], {"provider": "Higgsfield", "model": "Seedance 2.5", "unlimited_required": True, "chapters": []}
    previous_frame = None
    for index, world in enumerate(worlds, 1):
        if cancelled():
            raise pipeline.PipelineCancelled("Cancelled.")
        prompt = _prompt(pov, rig, world, worlds[index] if index < CHAPTERS else None, index,
                         camera, transform, atmosphere, instructions)
        clip = generated / f"pov_chapter_{index:02d}.mp4"
        _log(status_cb, f"Seedance 2.5 chapter {index}/{CHAPTERS}: {'using the exact prior last frame' if previous_frame else 'creating the opening world'}.")
        created = higgsfield_login.generate_seedance_25_video_sync(prompt, clip, first_frame=previous_frame, status_cb=status_cb)
        if created in {"UNLIMITED_OFF", "CAPTCHA"}:
            _log(status_cb, "Higgsfield needs Unlimited re-enabled; waiting without submitting a paid generation.")
            if not higgsfield_login.wait_for_seedance_25_unlimited_sync(status_cb=status_cb, cancel_check=cancelled):
                raise RuntimeError("Higgsfield Unlimited was not re-enabled.")
            created = higgsfield_login.generate_seedance_25_video_sync(prompt, clip, first_frame=previous_frame, status_cb=status_cb)
        if not created or not clip.exists():
            raise RuntimeError(f"Seedance 2.5 did not return chapter {index}. Completed chapters remain in the project.")
        previous_frame = generated / f"pov_chapter_{index:02d}_last_frame.jpg"
        _last_frame(clip, previous_frame)
        clips.append(clip)
        manifest["chapters"].append({"index": index, "world": world, "prompt": prompt, "clip": str(clip), "last_frame": str(previous_frame)})
        _write(config / "generation_manifest.json", manifest)

    output = renders / f"{slug}_pov_journey.mp4"
    _log(status_cb, "Joining the three frame-linked chapters into one 30-second vertical master...")
    _assemble(clips, output)
    poster = renders / "thumbnail.jpg"
    pipeline.extract_poster_frame(output, poster, at=2)
    project = {"project_slug": slug, "title": title, "kind": "pov_journey", "width": 720, "height": 1280, "fps": 30,
               "duration": CHAPTERS * SECONDS, "final_output": str(output), "pov_journey": {"pov": key, "worlds": worlds, "camera": camera, "transform": transform, "atmosphere": atmosphere, "instructions": instructions, "model": "Seedance 2.5", "provider": "Higgsfield", "unlimited": True, "native_audio_only": True},
               "scenes": [{"id": f"pov_chapter_{i:02d}", "name": f"POV chapter {i}", "start": (i - 1) * SECONDS, "duration": SECONDS, "clip": str(path)} for i, path in enumerate(clips, 1)]}
    _write(config / "project.json", project)
    _log(status_cb, f"POV Journey complete: {output.name}")
    return {"title": title, "project_dir": str(project_dir), "video": str(output), "thumbnail": str(poster), "kind": "pov_journey"}
