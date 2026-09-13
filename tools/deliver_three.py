"""Build one Sketch Short and one longform Sketch Explainer, both on Inworld's Alex.

Run as: python tools/deliver_three.py short | longform

Kept as a script rather than typed into a shell each time: these two differ only in the script
and the canvas, and getting either of those wrong wastes a full generation.
"""
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import longform_video as lv
import pipeline

DELIVERY = Path("D:/iphone 2")

SHORT_SCRIPT = """Why does your stomach growl when you are NOT hungry?

That noise is not your stomach asking for food. It is called borborygmi, and it is the sound of
your gut clearing the pipes. About every ninety minutes between meals, a wave of muscle
contractions sweeps from your stomach down through your small intestine. It pushes leftover food,
bacteria and gas ahead of it like a street sweeper. Your gut is mostly empty and full of air, so
the whole thing echoes. Eat something and the wave stops immediately - which is why the growling
goes quiet the second you start chewing."""

LONGFORM_SCRIPT = """Why do you get an earworm?

A song gets stuck in your head, and it is almost never the whole song. It is a fragment, usually
fifteen to thirty seconds long, and it repeats. Researchers call this an involuntary musical
image, and about ninety percent of people get one at least once a week.

The fragment is almost always the part you know best but have not quite finished. Your brain
treats an unfinished pattern as an open loop, and an open loop keeps asking to be closed. This is
the same effect that makes an interrupted task easier to remember than a completed one.

Simple, repetitive melodies get stuck more often than complex ones, because your brain can hold
the whole shape at once. A song with an unusual leap in the middle sticks harder still, because
that leap is the part your memory keeps checking.

Tiredness makes it worse. So does an idle mind. An earworm almost never arrives while you are
concentrating, because the loop needs somewhere quiet to run.

The way out is not to fight it. Trying to suppress a thought makes it return more often. What
works is giving the loop somewhere to close: listen to the whole song, or occupy the same part of
your brain with something else - a puzzle, a conversation, reading aloud. Chewing gum works
surprisingly well, because the jaw movement interferes with the inner speech the loop rides on."""


def build(kind):
    aspect = "9:16" if kind == "short" else "16:9"
    script = SHORT_SCRIPT if kind == "short" else LONGFORM_SCRIPT
    log = lambda m: print(str(m)[:170], flush=True)
    result = lv.run_longform_video(
        script,
        tts_model=pipeline.INWORLD_TTS_MODEL,
        voice="Alex",
        reasoning_model="anthropic/claude-opus-4.8",
        aspect=aspect,
        image_zoom=lv.IMAGE_ZOOM_SUBTLE,
        halt_after_speech=False,
        hook_intro=(kind == "short"),
        status_cb=log,
    )
    video = Path(str((result or {}).get("video") or ""))
    if not video.is_file():
        raise SystemExit(f"no video produced for {kind}")
    DELIVERY.mkdir(parents=True, exist_ok=True)
    target = DELIVERY / (f"sketch_short_stomach_growl.mp4" if kind == "short"
                         else "longform_sketch_earworm.mp4")
    shutil.copy2(video, target)
    print(f"DELIVERED|{target}", flush=True)


if __name__ == "__main__":
    build((sys.argv[1] if len(sys.argv) > 1 else "short").strip().lower())
