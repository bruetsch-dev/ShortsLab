"""Re-cut a sketch explainer SHORT into more beats and keep the drawings that still fit.

The complaint this exists for: "scheiss cuts, zu wenig auch. cuts passten nicht immer zum
voiceover". Measured on the sleep short - 10 beats over 33.2s, one picture every 3.3 seconds, and
lines like "Not lying in bed. Not putting your phone down." sharing a single drawing. The
short-form rule (a finished sentence ends a beat) turns the same script into 17 beats, one every
2.0 seconds.

`tools/retime_sketch_lines.py` deliberately refuses this case, and it is right to: it re-anchors
by INDEX, so a changed beat count would point every rendered frame at the wrong narration. This
tool re-maps by TEXT instead. An old beat that split into two keeps its drawing on the FIRST of
the two; the second is reported as needing a new one. Nothing is generated without --generate.

    python tools/resplit_sketch_short.py <project-folder> [--apply] [--generate]

Without --apply it only measures and prints.
"""
import json
import re
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import longform_video as lv


def norm(text):
    """Compare narration by its words only - punctuation moves when a beat is re-split."""
    return re.sub(r"[^a-z0-9 ]+", "", str(text or "").lower()).strip()


def map_images(old_lines, new_lines):
    """new index -> old index whose drawing still shows that narration.

    An old beat covers one or more new beats; its drawing was made for the whole thing, so it
    belongs to the FIRST new beat it covers. The rest are genuinely new pictures. Matching on
    the leading words, not on position, is what makes a changed beat count safe.
    """
    mapping, cursor = {}, 0
    old_norm = [norm(row.get("text")) for row in old_lines]
    for new_index, row in enumerate(new_lines):
        text = norm(row.get("text"))
        if not text:
            continue
        for offset in range(cursor, len(old_norm)):
            haystack = old_norm[offset]
            if not haystack:
                continue
            # The new beat is a slice of this old one - the first slice inherits the drawing.
            if haystack.startswith(text) and offset not in mapping.values():
                mapping[new_index] = offset
                cursor = offset
                break
            if haystack == text and offset not in mapping.values():
                mapping[new_index] = offset
                cursor = offset + 1
                break
    return mapping


def main(name, apply_changes, generate):
    out_dir = lv.OUT_ROOT / name
    if not out_dir.is_dir():
        raise SystemExit(f"No such longform project: {out_dir}")
    lv.adopt_project_aspect(out_dir)
    state = json.loads((out_dir / lv.STATE_FILE).read_text(encoding="utf-8"))
    script = str(state.get("script") or "")
    old_lines = list(state.get("lines") or [])
    if not script or not old_lines:
        raise SystemExit("That project has no script/lines to re-cut.")
    voice = lv.voiceover_path_from_state(out_dir, state)
    if not lv._audio_done(voice):
        raise SystemExit("That project's voiceover is missing; there is nothing to align to.")

    duration = float(state.get("audio_duration") or 0.0)
    print(f"{name}: {len(old_lines)} beats over {duration:.1f}s "
          f"({duration / max(1, len(old_lines)):.2f}s per picture)")

    print("  re-splitting with the short-form rule and aligning onto the existing voice...")
    new_lines = lv.transcribe_lines(script, voice, status_cb=lambda m: print("   ", m, flush=True))
    print(f"  -> {len(new_lines)} beats "
          f"({duration / max(1, len(new_lines)):.2f}s per picture)")

    images_dir = out_dir / "images"
    old_images = {}
    for index, row in enumerate(old_lines):
        found = lv.resolve_image_for(images_dir, index, row,
                                     float(row.get("end", 0)) - float(row.get("start", 0)),
                                     lv.IMAGE_ASPECT)
        if found:
            old_images[index] = Path(found)
    print(f"  {len(old_images)} of {len(old_lines)} old beats have a drawing on disk")

    mapping = map_images(old_lines, new_lines)
    keep = {new: old for new, old in mapping.items() if old in old_images}
    missing = [i for i in range(len(new_lines)) if i not in keep]
    print(f"  {len(keep)} drawing(s) carry over, {len(missing)} new one(s) needed")
    for index in missing:
        print(f"    NEW  {index:2d}  {new_lines[index].get('text', '')[:64]}")

    if not apply_changes:
        print("  (dry run - pass --apply to write it)")
        return

    stamp = time.strftime("%Y%m%d_%H%M%S")
    backup = out_dir / "backups" / f"resplit_{stamp}"
    backup.mkdir(parents=True, exist_ok=True)
    shutil.copy2(out_dir / lv.STATE_FILE, backup / lv.STATE_FILE)
    if images_dir.is_dir():
        shutil.copytree(images_dir, backup / "images", dirs_exist_ok=True)
    print(f"  previous state + images copied to {backup}")

    # Rename the surviving drawings onto their NEW index and timestamp. Done via a temp name
    # first: an old index and a new index can collide, and a straight rename would overwrite a
    # drawing that has not been moved yet.
    staging = images_dir / "_resplit_tmp"
    staging.mkdir(parents=True, exist_ok=True)
    staged = {}
    for new_index, old_index in keep.items():
        source = old_images[old_index]
        temp = staging / f"{new_index:03d}{source.suffix}"
        shutil.copy2(source, temp)
        staged[new_index] = temp
    for old_path in set(old_images.values()):
        old_path.unlink(missing_ok=True)
    for new_index, temp in staged.items():
        row = new_lines[new_index]
        seconds = float(row.get("end", 0)) - float(row.get("start", 0))
        target = images_dir / (lv.image_key(new_index, row, seconds) + temp.suffix)
        shutil.move(str(temp), str(target))
    shutil.rmtree(staging, ignore_errors=True)
    print(f"  {len(staged)} drawing(s) renamed onto their new beats")

    lv.save_state(out_dir, lines=new_lines)
    lv.write_transcript(new_lines, out_dir / "transcript.txt")
    print("  state written")

    if not generate:
        print(f"  {len(missing)} beat(s) still have no drawing - rerun with --generate to draw "
              f"them (this one costs API calls).")
        return

    print(f"  generating {len(missing)} missing drawing(s)...")
    # The second positional argument is reasoning_model, NOT the script - passing the script
    # there would have sent the whole narration as a model name.
    reasoning_model = str(state.get("reasoning_model") or "") or None
    prompts = lv.generate_image_prompts(
        new_lines, reasoning_model=reasoning_model, aspect=lv.IMAGE_ASPECT,
        checkpoint_path=out_dir / "image_prompts_checkpoint.json",
        status_cb=lambda m: print("   ", m, flush=True))
    durations = lv.line_durations(new_lines, duration)
    # generate_images skips a beat whose drawing is already on disk under the new name, so only
    # the genuinely new ones are drawn - the carried-over eight are not paid for twice.
    lv.generate_images(prompts, new_lines, durations, images_dir, aspect=lv.IMAGE_ASPECT,
                       status_cb=lambda m: print("   ", m, flush=True))
    try:
        lv.rebuild_timeline_from_speech_clock(out_dir, status_cb=lambda m: print("   ", m))
    except lv.LongformError as exc:
        print(f"  timeline manifest not rebuilt ({exc}) - the beats themselves are saved.")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    main(args[0] if args else "try_to_remember_the_exact_moment_speed120_134252",
         "--apply" in sys.argv, "--generate" in sys.argv)
