"""Timeline-first editing engine.

The old path stitched clips end to end and let the video decide the length. Here the
AUDIO decides: word-level TTS timestamps drive every cut, and clips are trimmed, split
and punched in to fit that spine.

Modules:
    timeline        - the cut plan: dead-frame trim, jump cuts, keyword-synced hard cuts
"""

from .timeline import (           # noqa: F401
    Cut,
    ClipSource,
    TimelinePlan,
    build_timeline,
    load_word_timings,
    plan_to_scenes,
)

__all__ = ["Cut", "ClipSource", "TimelinePlan", "build_timeline",
           "load_word_timings", "plan_to_scenes"]
