"""Ready-made polish for a generated voiceover part, applied from the approval screen.

A TTS take is rarely wrong in a way that regenerating fixes - it is a little quiet, a little
hissy between words, a little boomy, a little flat. Regenerating rolls the dice again and costs
another call; a preset fixes the take you already have, and can be undone because the original
is kept.

Every preset is a plain ffmpeg filter chain, chosen so it can be REVERSED by re-applying "none"
to the untouched original. Nothing here is a creative effect - these are the four repairs that
actually come up.

Deliberately NOT included: a compressor by itself. Lifting a quiet TTS part with dynamics raises
its noise floor with it, and that hiss cost a day to track down once already. Where a preset
does compress, it denoises FIRST and gates AFTER.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

# Same target the voiceover parts and the hook intro are levelled to, so a polished part still
# sits at the level everything else in the video was built around.
TARGET_LUFS = -20.0

PRESETS = {
    "none": {
        "label": "Original",
        "hint": "The take exactly as it was generated.",
        "chain": "",
    },
    "level": {
        "label": "Even level",
        "hint": "Matches this part to the loudness of every other part. Nothing else touched.",
        # Two-pass loudnorm would be exact, but it needs a measuring run per file. One pass
        # lands about a decibel under the target (measured: -21.0 against a -20.0 target) and
        # returns instantly, which is the right trade for a button you click while listening -
        # every part is treated the same, so they still MATCH each other.
        "chain": f"loudnorm=I={TARGET_LUFS}:TP=-1.5:LRA=11",
    },
    "clean": {
        "label": "Clean up hiss",
        "hint": "Removes the background hiss between words. Use when the take sounds "
                "'digital' in the pauses.",
        # afftdn first, THEN the gate: gating a hissy signal chops the hiss into bursts, which
        # is more noticeable than the steady hiss it replaced.
        "chain": ("afftdn=nf=-28:tn=1,"
                  "agate=threshold=0.006:ratio=2:attack=8:release=180,"
                  f"loudnorm=I={TARGET_LUFS}:TP=-1.5:LRA=11"),
    },
    "warm": {
        "label": "Warmer",
        "hint": "Softens a thin or harsh voice: less sibilance, a little more body.",
        # A shelf pair, not a "radio voice" preset - the point is to keep the same narrator.
        "chain": ("equalizer=f=250:t=q:w=1.0:g=2.0,"
                  "equalizer=f=6500:t=q:w=1.4:g=-3.0,"
                  f"loudnorm=I={TARGET_LUFS}:TP=-1.5:LRA=11"),
    },
    "close": {
        "label": "Close mic",
        "hint": "Tighter and more present, for a hook or an opening line.",
        "chain": ("afftdn=nf=-26:tn=1,"
                  "equalizer=f=180:t=q:w=1.0:g=1.5,"
                  "equalizer=f=3200:t=q:w=1.2:g=2.0,"
                  "acompressor=threshold=-18dB:ratio=2.5:attack=6:release=140,"
                  "agate=threshold=0.008:ratio=2:attack=6:release=160,"
                  f"loudnorm=I={TARGET_LUFS}:TP=-1.5:LRA=11"),
    },
    "clarity": {
        "label": "Clearer speech",
        "hint": "Cuts rumble and lifts consonants. Use when words run together.",
        "chain": ("highpass=f=85,"
                  "equalizer=f=2800:t=q:w=1.3:g=2.5,"
                  f"loudnorm=I={TARGET_LUFS}:TP=-1.5:LRA=11"),
    },
}

# The stash and the scratch file keep the REAL extension: ffmpeg infers the output format from
# it, and "part.mp3.tmp" is not a format it can guess - the first version of this failed with
# "Invalid argument" on every preset.
ORIGINAL_MARK = ".original"
SCRATCH_MARK = ".preset"


def preset_choices():
    """[{value, label, hint}] for the approval screen, in a deliberate order."""
    return [{"value": key, "label": PRESETS[key]["label"], "hint": PRESETS[key]["hint"]}
            for key in ("none", "level", "clean", "warm", "clarity", "close")]


def original_path(path):
    """Where the untouched take is kept, so every preset is applied to the SOURCE.

    Chaining presets onto an already-processed file compounds the loudnorm and the denoiser -
    two clicks would quietly destroy the take with no way back.
    """
    path = Path(path)
    return path.with_name(path.stem + ORIGINAL_MARK + path.suffix)


def apply_preset(path, preset, ffmpeg, status_cb=None):
    """Rewrite `path` with `preset` applied to the ORIGINAL take. Returns the path.

    The first call stashes the untouched file; every later call reads that stash, so switching
    between presets - and back to "none" - is lossless and repeatable.
    """
    path = Path(path)
    key = str(preset or "none").strip().lower()
    spec = PRESETS.get(key)
    if spec is None:
        raise ValueError(f"unknown voice preset {preset!r}")
    if not path.is_file():
        raise FileNotFoundError(str(path))
    source = original_path(path)
    if not source.is_file():
        shutil.copy2(path, source)
    if not spec["chain"]:
        shutil.copy2(source, path)
        if status_cb:
            status_cb(f"{path.name}: back to the original take.")
        return path
    out = path.with_name(path.stem + SCRATCH_MARK + path.suffix)
    codec = ["-c:a", "libmp3lame", "-b:a", "192k"] if path.suffix.lower() == ".mp3" \
        else ["-c:a", "pcm_s16le"]
    done = subprocess.run(
        [str(ffmpeg), "-y", "-hide_banner", "-loglevel", "error", "-i", str(source),
         "-filter:a", spec["chain"], *codec, str(out)],
        capture_output=True, text=True, timeout=900)
    if not out.is_file() or out.stat().st_size < 4096:
        out.unlink(missing_ok=True)
        raise RuntimeError(f"{spec['label']} failed: {(done.stderr or '')[-200:]}")
    out.replace(path)
    if status_cb:
        status_cb(f"{path.name}: {spec['label'].lower()} applied.")
    return path
