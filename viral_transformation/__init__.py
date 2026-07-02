"""Viral Transformation Creator (auto_viral_transformation_mode).

Fully autonomous mode: the user picks ONE topic ("street dog salon", "rusty knife
restoration", ...) and the agents do everything else - concept, 6-phase viral structure
(DECLARE -> ASSESS -> ISOLATE -> PROCESS -> BUILD -> REVEAL), scene plan, GPT-Image-2
reference images, Seedance 2.0 image-to-video clips, QA, captions, music/SFX edit,
final 9:16 MP4 and metadata. No voice script, no visual script, no manual prompts.
"""

from .agents import TOPIC_PRESETS, NEG_PROMPT, topic_strategist, plan_scenes, make_metadata
from .orchestrator import run_transformation_job

__all__ = ["TOPIC_PRESETS", "NEG_PROMPT", "topic_strategist", "plan_scenes",
           "make_metadata", "run_transformation_job"]
