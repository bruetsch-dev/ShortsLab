"""One 9:16 sketch explainer short, end to end, without going through the desktop app.

Same doodle pipeline as the long explainer - same drawing rules, same cut rhythm - on a vertical
canvas with a one-minute script. Speed 1.2 and the upbeat delivery are what the shorts format
uses in the UI; they are passed explicitly here so the run matches what the app would send.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import longform_video as lv

SCRIPT = """Try to remember the exact moment you fell asleep last night. Not lying in bed. Not
putting your phone down. The instant itself, where being awake ended.

You can't. Nobody can.

You might remember staring at the ceiling. A thought that made no sense. Turning onto your side.
And then there is a cut. No transition, no memory of crossing over.

The reason is that the part of your brain that files memories switches off before the part that
keeps you awake does. For a few minutes you are still conscious, still thinking, and already
recording nothing.

So the moment exists. You were there for it.

It was simply never written down."""

UPBEAT = ("Upbeat, energetic and friendly. Bright, forward-leaning pace with clear punchy "
          "emphasis on the key word of each line, a light smile in the voice, and short "
          "confident pauses instead of drawn-out ones. Never shouty, never breathless.")

VOICE = "jess_ja_es_id_pt_en_zh"


def main():
    print("SKETCH_SHORT_SCRIPT|" + " ".join(SCRIPT.split()), flush=True)
    import pipeline
    # Seed, not Gemini: the Gemini TTS payload has no speed parameter at all, and tts_options is
    # only forwarded for Seed models - so on Gemini both the 1.2 speed and the upbeat delivery
    # would be silently dropped.
    result = lv.run_longform_video(
        SCRIPT,
        aspect="9:16",
        tts_model=pipeline.SEED_SPEECH_TTS_MODEL,
        voice=VOICE,
        reasoning_model="anthropic/claude-opus-4.8",
        halt_after_speech=False,
        tts_options={"voice_instruction": UPBEAT, "speed": 1.12},
        status_cb=lambda message: print(message, flush=True),
    )
    print("SKETCH_SHORT_RESULT|" + json.dumps(
        {k: v for k, v in result.items() if isinstance(v, (str, int, float, bool))}), flush=True)


if __name__ == "__main__":
    main()
