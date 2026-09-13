"""Continue a sketch explainer that stopped part-way, using its own saved settings.

The project this was written for stalled at 40 of 304 image prompts. Re-running the normal
pipeline with resume=True now picks the conversation back up (it used to run zero rounds after
loading a checkpoint and fall through to the emergency template), finishes the prompts, renders
the missing frames and re-cuts the video against the existing voiceover.

    python tools/continue_sketch_project.py <project-folder-name>
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import longform_video as lv


def main(name):
    out_dir = lv.OUT_ROOT / name
    if not out_dir.is_dir():
        raise SystemExit(f"No such longform project: {out_dir}")
    state = json.loads((out_dir / lv.STATE_FILE).read_text(encoding="utf-8"))
    script = str(state.get("script") or "")
    if not script:
        raise SystemExit("That project has no saved script to resume from.")
    lines = state.get("lines") or []
    prompts = state.get("prompts") or []
    images = len(list((out_dir / "images").glob("*.png"))) if (out_dir / "images").is_dir() else 0
    print(f"{name}: {len(lines)} lines, {len(prompts)} prompts, {images} images on disk",
          flush=True)

    options = state.get("tts_settings") if isinstance(state.get("tts_settings"), dict) else {}
    result = lv.run_longform_video(
        script,
        tts_model=str(state.get("tts_model") or "pro"),
        reasoning_model=str(state.get("reasoning_model") or "") or None,
        reasoning_mode=state.get("reasoning_mode"),
        voice=str(state.get("voice") or "") or None,
        mascot=bool(state.get("mascot_enabled")),
        halt_after_speech=False,          # the voiceover already exists and is approved
        tts_options=options,
        aspect=str(state.get("aspect") or "16:9"),
        resume=True,
        status_cb=lambda message: print(message, flush=True),
    )
    print("RESULT|" + json.dumps({k: v for k, v in result.items()
                                  if isinstance(v, (str, int, float, bool))}), flush=True)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "why_you_can_t_remember_falling")
