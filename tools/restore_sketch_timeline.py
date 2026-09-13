"""Put a sketch explainer's already-rendered frames back into its timeline.

A version bump (scene_clock_version / prompt_format) makes the pipeline call _archive_old_images:
every rendered PNG is moved to images/_old_format_<ts>/ and the saved prompts are dropped. The
project then looks empty even though hundreds of finished frames are sitting right there, and
nothing puts them back.

This does the retime that fixes it, with no generation and no API call:

  * take the ORIGINAL line list and prompts out of the project's own backup
  * strip the hidden citation markers, so the texts match the cleaned voiceover word-for-word
  * force-align those lines onto the CURRENT voiceover (faster-whisper, local) - the frames were
    cut for a slightly longer read, so every start time has to move
  * move the frames back and rebuild the timeline against the new clock

    python tools/restore_sketch_timeline.py <project> [--backup NAME] [--apply]
"""
import json
import re
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import longform_video as lv


def aligned_lines(script, voice, texts, status_cb):
    """The given line texts, re-timed onto `voice`. Word counts are consumed in order, which is
    exactly how transcribe_lines maps a script's own clauses - only the split is ours, not its."""
    import voice_align
    if not voice_align.available():
        raise SystemExit("faster-whisper is not installed; the retime needs it to align locally.")
    status_cb("Transcribing the current voiceover (local, no API)...")
    asr = voice_align.transcribe_words(voice, status_cb=status_cb)
    if not asr:
        raise SystemExit("Transcription produced no words.")
    words = voice_align.align_script_to_words(str(script), asr)
    out, at = [], 0
    for text in texts:
        n = len(text.split())
        chunk = words[at:at + n]
        at += n
        if not chunk:
            break
        out.append({
            "start": round(float(chunk[0]["start"]), 2),
            "end": round(float(chunk[-1]["end"]), 2),
            "text": text,
            "words": [{"w": str(w.get("word") or w.get("w") or ""),
                       "s": round(float(w["start"]), 2)} for w in chunk],
        })
    status_cb(f"Re-timed {len(out)} of {len(texts)} beat(s) onto the current voice.")
    return out


def main(project, backup_name, apply_changes):
    out_dir = lv.OUT_ROOT / project
    if not out_dir.is_dir():
        raise SystemExit(f"No such longform project: {out_dir}")
    lv.adopt_project_aspect(out_dir)
    log = lambda m: print("   ", m, flush=True)

    archives = sorted((out_dir / "images").glob("_old_format_*"))
    if not archives:
        raise SystemExit("No archived frames found - nothing to restore.")
    archive = archives[-1]
    frames = sorted(archive.glob("*.png"))

    backups = sorted((out_dir / "backups").glob("*")) if (out_dir / "backups").is_dir() else []
    if backup_name:
        backups = [b for b in backups if b.name == backup_name]
    source = next((b for b in reversed(backups) if (b / lv.STATE_FILE).is_file()), None)
    if source is None:
        raise SystemExit("No backup with a state.json - the original line list is gone.")
    saved = json.loads((source / lv.STATE_FILE).read_text(encoding="utf-8"))
    old_lines = saved.get("lines") or []
    old_prompts = saved.get("prompts") or []

    state = json.loads((out_dir / lv.STATE_FILE).read_text(encoding="utf-8"))
    script = str(state.get("script") or "")
    voice = lv.voiceover_path_from_state(out_dir, state)
    if not lv._audio_done(voice):
        raise SystemExit("The current voiceover is missing; there is nothing to align to.")

    print(f"{project}")
    print(f"  archived frames : {len(frames)} in {archive.name}")
    print(f"  backup          : {source.name} - {len(old_lines)} lines, {len(old_prompts)} prompts")
    print(f"  current state   : {len(state.get('lines') or [])} lines, "
          f"{len(state.get('prompts') or [])} prompts")
    print(f"  current voice   : {voice.name}")

    texts = [lv.clean_narration_script(str(row.get("text") or "")) for row in old_lines]
    texts = [t for t in texts if t.strip()]
    script_words = len(re.sub(r"\s+", " ", script).strip().split())
    line_words = sum(len(t.split()) for t in texts)
    print(f"  script words {script_words} vs line words {line_words} "
          f"(difference {abs(script_words - line_words)})")
    if abs(script_words - line_words) > max(6, script_words * 0.01):
        raise SystemExit("The saved lines no longer match the script closely enough to re-time "
                         "them safely.")

    usable = min(len(texts), len(frames), len(old_prompts) or len(texts))
    print(f"  will restore {usable} beat(s)")

    new_lines = aligned_lines(script, voice, texts[:usable], log)
    if not new_lines:
        raise SystemExit("Alignment produced no lines.")
    drift = [round(float(new_lines[i]["start"]) - float(old_lines[i].get("start") or 0.0), 2)
             for i in range(min(len(new_lines), len(old_lines)))]
    print(f"  start-time shift: first {drift[0]:+.2f}s, last {drift[-1]:+.2f}s, "
          f"max {max(drift, key=abs):+.2f}s")

    if not apply_changes:
        print("  (dry run - pass --apply to move the frames back and write the timeline)")
        return

    stamp = time.strftime("%Y%m%d_%H%M%S")
    keep = out_dir / "backups" / f"restore_{stamp}"
    keep.mkdir(parents=True, exist_ok=True)
    shutil.copy2(out_dir / lv.STATE_FILE, keep / lv.STATE_FILE)

    moved = 0
    for frame in frames[:usable]:
        target = out_dir / "images" / frame.name
        if not target.exists():
            shutil.copy2(frame, target)      # copy, not move: the archive stays intact
            moved += 1
    print(f"  restored {moved} frame(s) into images/ (the archive is left untouched)")

    lv.save_state(out_dir, lines=new_lines, prompts=old_prompts[:len(new_lines)],
                  audio_duration=round(lv.audio_duration_seconds(voice), 3))
    lv.write_transcript(new_lines, out_dir / "transcript.txt")
    print(f"  state written; previous state kept in {keep.name}")
    try:
        lv.rebuild_timeline_from_speech_clock(out_dir, status_cb=log)
        print("  timeline rebuilt against the current speech clock")
    except lv.LongformError as exc:
        print(f"  timeline manifest not rebuilt ({exc})")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    named = None
    if "--backup" in sys.argv:
        named = sys.argv[sys.argv.index("--backup") + 1]
    main(args[0] if args else "why_you_can_t_remember_falling", named, "--apply" in sys.argv)
