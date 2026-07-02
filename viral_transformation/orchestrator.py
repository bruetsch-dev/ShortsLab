"""Orchestrator: topic in -> finished viral transformation Short out. Fully autonomous."""

import json
import re
import time
from pathlib import Path

from . import agents, builder, media as media_mod

ROOT = Path(__file__).resolve().parent.parent
PROJECTS_DIR = ROOT / "projects"


def log(cb, msg):
    if cb:
        cb(msg)


def _slug(text):
    s = re.sub(r"[^a-z0-9]+", "_", str(text or "transformation").lower()).strip("_")[:40]
    return s or "transformation"


def run_transformation_job(topic, status_cb=None, cancel_event=None, reasoning_model=None):
    """The full autonomous pipeline for the 'Viral Transformation Short' mode."""
    topic = str(topic or "").strip()
    if not topic:
        raise RuntimeError("Choose a topic first.")

    log(status_cb, "Planning concept...")
    strategy = agents.topic_strategist(topic, reasoning_model=reasoning_model, status_cb=status_cb)
    if not strategy.get("safe", True):
        raise RuntimeError("This topic failed the safety check - pick a gentler transformation topic.")
    log(status_cb, f"Concept: {strategy['concept_title']}")

    slug = f"vt_{_slug(strategy.get('narrowed_topic') or topic)}_{time.strftime('%H%M%S')}"
    project_dir = PROJECTS_DIR / slug
    (project_dir / "config").mkdir(parents=True, exist_ok=True)
    (project_dir / "renders").mkdir(parents=True, exist_ok=True)

    def save_config(extra):
        cfg_path = project_dir / "config" / "project.json"
        cfg = {}
        if cfg_path.exists():
            try:
                cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            except Exception:
                cfg = {}
        cfg.update({"type": "auto_viral_transformation", "project_slug": slug,
                    "selected_topic": topic})
        cfg.update(extra)
        cfg_path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")

    save_config({"generated_concept": strategy, "title": strategy["concept_title"]})

    log(status_cb, "Creating scenes... (DECLARE -> ASSESS -> ISOLATE -> PROCESS -> BUILD -> REVEAL)")
    plan = agents.plan_scenes(strategy, reasoning_model=reasoning_model, status_cb=status_cb)
    log(status_cb, f"Scene plan: {len(plan['scenes'])} scene(s), ~{plan['total_duration']}s.")
    save_config({"phase_plan": [s["phase"] for s in plan["scenes"]], "scene_plan": plan,
                 "captions": [s["caption"] for s in plan["scenes"]]})

    media = media_mod.generate_scene_media(plan, strategy, project_dir,
                                           status_cb=status_cb, cancel_event=cancel_event)
    media = media_mod.visual_qa(plan, media, project_dir, reasoning_model=reasoning_model,
                                status_cb=status_cb, cancel_event=cancel_event)
    save_config({"image_paths": {k: v.get("image") for k, v in media.items()},
                 "generated_video_paths": {k: v.get("clip") for k, v in media.items()}})

    log(status_cb, "Editing final short...")
    out_path = project_dir / "renders" / f"{slug}_{time.strftime('%Y%m%d_%H%M%S')}.mp4"
    built = builder.build_short(plan, media, project_dir, out_path, status_cb=status_cb)

    metadata = agents.make_metadata(strategy, reasoning_model=reasoning_model)
    save_config({"final_video_path": built["video"], "metadata": metadata,
                 "audio_plan": {"music": "background music folder", "sfx": "local sfx_library",
                                "voiceover": "none (captions + music/SFX format)"}})
    log(status_cb, "Export complete.")
    return {"video": built["video"], "title": metadata.get("youtube_title"),
            "tiktok_title": metadata.get("tiktok_title"),
            "description": metadata.get("description"),
            "hashtags": " ".join(metadata.get("hashtags") or []),
            "scenes_used": built["scenes_used"], "duration": built["duration"],
            "project": str(project_dir)}
