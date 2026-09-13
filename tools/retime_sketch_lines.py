"""Retime a sketch explainer: re-cut the narration into beats and re-anchor them to the voice.

This is the timing-only repair. It re-splits the saved script with the current beat rule,
force-aligns those beats onto the voiceover that already exists, and rebuilds the timeline from
the real speech clock. No TTS, no image generation, no model call - faster-whisper runs locally.

It reports how many cuts landed mid-sentence before and after, because that is the defect being
repaired: on the sleep script 23% of all image changes fell between two words of one phrase.

    python tools/retime_sketch_lines.py <project-folder-name> [--apply]

Without --apply it only measures and prints; nothing on disk is touched.
"""
import json
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import longform_video as lv

ENDINGS = (".", "!", "?", '"', "”")


def mid_sentence(lines):
    return sum(1 for row in lines
               if not str(row.get("text") or "").rstrip().endswith(ENDINGS))


def main(name, apply_changes):
    out_dir = lv.OUT_ROOT / name
    if not out_dir.is_dir():
        raise SystemExit(f"No such longform project: {out_dir}")
    lv.adopt_project_aspect(out_dir)
    state = json.loads((out_dir / lv.STATE_FILE).read_text(encoding="utf-8"))
    script = str(state.get("script") or "")
    old_lines = list(state.get("lines") or [])
    if not script or not old_lines:
        raise SystemExit("That project has no script/lines to retime.")
    voice = lv.voiceover_path_from_state(out_dir, state)
    if not lv._audio_done(voice):
        raise SystemExit("That project's voiceover is missing; there is nothing to align to.")

    images = sorted((out_dir / "images").glob("*.png")) if (out_dir / "images").is_dir() else []
    print(f"{name}: {len(old_lines)} beats, {len(images)} image(s), voice {voice.name}")
    before = mid_sentence(old_lines)
    print(f"  before: {before}/{len(old_lines)} cuts land mid-sentence "
          f"({before / len(old_lines) * 100:.1f}%)")

    print("  re-aligning the new beats onto the existing voiceover (local, no API)...")
    new_lines = lv.transcribe_lines(script, voice, status_cb=lambda m: print("   ", m, flush=True))
    after = mid_sentence(new_lines)
    print(f"  after : {after}/{len(new_lines)} cuts land mid-sentence "
          f"({after / len(new_lines) * 100:.1f}%)")

    if images and len(new_lines) != len(old_lines):
        print(f"  NOTE: the beat count changed ({len(old_lines)} -> {len(new_lines)}) and this "
              f"project has {len(images)} rendered image(s). Their line mapping would shift, so "
              "this run stops here rather than silently pointing frames at the wrong narration.")
        if apply_changes:
            raise SystemExit("Refusing to apply: rendered images would end up on the wrong beats.")

    if not apply_changes:
        print("  (dry run - pass --apply to write it)")
        return

    backup = out_dir / "backups" / f"retime_{time.strftime('%Y%m%d_%H%M%S')}"
    backup.mkdir(parents=True, exist_ok=True)
    shutil.copy2(out_dir / lv.STATE_FILE, backup / lv.STATE_FILE)
    lv.save_state(out_dir, lines=new_lines)
    lv.write_transcript(new_lines, out_dir / "transcript.txt")
    print(f"  written. previous state saved to {backup}")
    try:
        lv.rebuild_timeline_from_speech_clock(out_dir, status_cb=lambda m: print("   ", m))
    except lv.LongformError as exc:
        print(f"  timeline manifest not rebuilt ({exc}) - the beats themselves are saved.")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    main(args[0] if args else "why_you_can_t_remember_falling", "--apply" in sys.argv)
