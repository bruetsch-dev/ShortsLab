"""Timeline-first editing engine.

The old path stitched clips end to end and let the video decide the length. Here the
AUDIO decides: word-level TTS timestamps drive every cut, and clips are trimmed, split
and punched in to fit that spine.

    timeline    cut plan: dead-frame trim, jump cuts, keyword-synced hard cuts
    audio       silence trimming (with a time map), BGM bed, SFX event triggers
    captions    1-3 word cards, heavy type, NLP colour coding, pop-in animation
    overlays    add_visual_pointer() - transparent PNGs at a timestamp, with sound

Order matters: trim silence FIRST, remap the word timings through the returned TimeMap,
and only then build the timeline and captions. Cutting against untrimmed timings puts
every later edit on the wrong frame.
"""

from .audio import (            # noqa: F401
    MAX_SILENCE,
    SfxTimeline,
    TimeMap,
    build_bgm_bed,
    trim_silence,
)
from .captions import (         # noqa: F401
    CaptionCard,
    CaptionStyle,
    build_caption_track,
    classify,
    group_words,
)
from .overlays import (         # noqa: F401
    Overlay,
    OverlayPlan,
    pointers_from_cuts,
)
from .timeline import (         # noqa: F401
    ClipSource,
    Cut,
    TimelinePlan,
    build_timeline,
    load_word_timings,
    plan_to_scenes,
)

__all__ = [
    "Cut", "ClipSource", "TimelinePlan", "build_timeline", "load_word_timings",
    "plan_to_scenes",
    "TimeMap", "trim_silence", "build_bgm_bed", "SfxTimeline", "MAX_SILENCE",
    "CaptionStyle", "CaptionCard", "group_words", "classify", "build_caption_track",
    "Overlay", "OverlayPlan", "pointers_from_cuts",
]
