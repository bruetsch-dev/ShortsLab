"""Longform doodle-video pipeline: paste a SCRIPT, get a finished 16:9 video.

Stages (fully autonomous after Start):
  1. VOICEOVER  - the script is split into sentence-safe parts and sent to Gemini TTS
                  (2.5 Flash or 2.5 Pro via WaveSpeed); parts are concatenated losslessly.
  2. TIMESTAMPS - faster-whisper transcribes the finished voiceover with word timestamps;
                  the KNOWN script is aligned onto the ASR timing (voice_align), then grouped
                  into clause lines -> transcript.txt like "[0:03.4] But somehow, ..." with
                  sub-second precision.
  3. PROMPTS    - the transcript is sent to the reasoning model (Opus 4.8 by default) with the
                  STAGE-3 doodle prompt; the app auto-replies "next" until every timestamp has
                  an image prompt, then writes image_prompts_<slug>.txt itself.
  4. IMAGES     - each prompt (timestamp stripped) goes to the user's logged-in Higgsfield
                  session: FLUX.2 Pro (unlimited), 16:9, IMAGE_CONCURRENCY generations in
                  flight (1 today - see the constant for why). Failed generations are retried;
                  a run whose generations ALL fail stops early instead of grinding out black
                  frames. A character-reference frame is generated FIRST. Files are named with
                  the timestamp AND the on-screen duration.
  5. REVIEW     - the run stops on a movable image/voiceover timeline with archived generations
                   and three optional thumbnail/title choices. Assembly begins only after approval;
                   the encoded video is checked for real black-screen spans before success.
"""

import json
import os
import re
import shutil
import subprocess
import threading
import time
import concurrent.futures
import urllib.error
from pathlib import Path

import agent_core
import pipeline

ROOT = Path(__file__).resolve().parent
OUT_ROOT = agent_core.PROJECTS_DIR / "_longform"

TTS_PART_CHAR_LIMIT = 2200          # sentence-safe chunking limit per TTS call

# ---- images: Ideogram (P-Image) over the WaveSpeed HTTP API ------------------------------
# Replaces the Higgsfield browser session. That route needed a visible window, a manual
# "Unlimited" switch and a DataDome bot-check that fails every automated click, and its
# page-recycling was the source of the late-delivery/mis-attribution bug this file spent
# hundreds of lines defending against. An ordinary POST+poll has none of that: every request
# owns its own result, so attribution is exact by construction.
P_IMAGE_MODEL = "pruna-ai/p-image/ideogram"
IMAGE_ASPECT = "16:9"
# 1k + "very low" (user 2026-08-16): the cheapest tier on this endpoint, $0.003 per image
# against $0.030 for 2k/high - a 240-frame video costs ~$0.72 instead of ~$7.20. Flat doodle
# art is the one style that survives it: no fine texture to lose, and the composition comes
# from the prompt rather than from the model thinking about it.
IMAGE_RESOLUTION = "1k"
IMAGE_THINKING = "very low"
IMAGE_OUTPUT_FORMAT = "png"
# Median generation is ~8s, so the run is now network-bound rather than session-bound.
IMAGE_CONCURRENCY = 6               # parallel generations in flight
IMAGE_TIMEOUT_S = 300               # per image: submit + poll + download
IMAGE_RETRIES = 2                   # re-generate a failed image up to N extra times
MAX_DEAD_ATTEMPTS_BEFORE_GIVING_UP = 6  # failed generations with ZERO successes = provider gone
MAX_CONSECUTIVE_FAILURES_MIDRUN = 10    # unbroken failure streak while fresh work remains = died mid-run
VIDEO_W, VIDEO_H, VIDEO_FPS = 1920, 1080, 30


STATE_FILE = "state.json"           # resume state: script + timings + prompts of the last run
MIN_IMAGE_BYTES = 1024              # smaller than this = a truncated/failed write, regenerate
MIN_AUDIO_BYTES = 8192              # 24kHz/16bit mono: <0.2s of audio, so a truncated part


class LongformError(RuntimeError):
    pass


def _image_done(path, expected_aspect=None):
    """True when an image is healthy and, when requested, has the right canvas ratio.

    Higgsfield's generator defaults to 3:4.  A UI-selector regression once produced a valid but
    portrait frame for a 16:9 longform project; checking only byte size made every resume trust
    that wrong frame forever.
    """
    try:
        p = Path(path)
        if not p.is_file() or p.stat().st_size < MIN_IMAGE_BYTES:
            return False
        if expected_aspect:
            from PIL import Image
            with Image.open(p) as image:
                width, height = image.size
            left, right = str(expected_aspect).split(":", 1)
            target = float(left) / float(right)
            actual = float(width) / float(height)
            # Allow normal rounding/cropping differences while rejecting 3:4 as 16:9.
            if abs(actual - target) > 0.06:
                return False
        return True
    except (OSError, ValueError, ZeroDivisionError):
        return False


def _audio_done(path):
    """True when a TTS part is already on disk and is not a truncated stub."""
    try:
        p = Path(path)
        return p.is_file() and p.stat().st_size >= MIN_AUDIO_BYTES
    except OSError:
        return False


def load_state(out_dir, script):
    """Resume state for THIS script, or None.

    Keyed on the exact script: if a single word changed, the line split, the timings and the
    prompts all change, so nothing from the old run may be reused (the images would land on the
    wrong lines). Returning None simply means "run every stage again".
    """
    try:
        data = json.loads((Path(out_dir) / STATE_FILE).read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict) or str(data.get("script") or "") != str(script or ""):
        return None
    return data


def save_state(out_dir, **fields):
    """Persist the resume state atomically (never leave a half-written state behind)."""
    path = Path(out_dir) / STATE_FILE
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(existing, dict):
            existing = {}
    except Exception:
        existing = {}
    existing.update(fields)
    try:
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(existing, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path)
    except Exception:
        pass
    return existing


def _log(cb, msg):
    if cb:
        cb(msg)


def slug_for(script):
    words = re.findall(r"[A-Za-z0-9]+", str(script or ""))[:6]
    slug = "_".join(w.lower() for w in words) or f"longform_{int(time.time())}"
    return slug[:60]


# ------------------------------------------------------------------ 1) VOICEOVER

def split_script_for_tts(script, limit=TTS_PART_CHAR_LIMIT):
    """Split the script into TTS-sized parts WITHOUT breaking sentences. Paragraphs first,
    then sentences when a paragraph alone exceeds the limit."""
    text = re.sub(r"\r\n?", "\n", str(script or "")).strip()
    if not text:
        return []
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    units = []
    for p in paragraphs:
        if len(p) <= limit:
            units.append(p)
        else:
            units.extend(s.strip() for s in re.findall(r"[^.!?]+[.!?]+(?:\s|$)|[^.!?]+$", p) if s.strip())
    parts, cur = [], ""
    for u in units:
        if cur and len(cur) + len(u) + 1 > limit:
            parts.append(cur.strip())
            cur = u
        else:
            cur = (cur + " " + u).strip() if cur else u
    if cur.strip():
        parts.append(cur.strip())
    return parts


def longest_silence_run(path, ffmpeg=None, floor_db=-45.0):
    """Length (s) of the single longest unbroken silent stretch in ``path`` (0.0 if none)."""
    ffmpeg = ffmpeg or pipeline.find_ffmpeg()
    try:
        r = subprocess.run([ffmpeg, "-hide_banner", "-i", str(path), "-af",
                            f"silencedetect=noise={floor_db}dB:d=2", "-f", "null", "-"],
                           capture_output=True, text=True, timeout=180)
        runs = [float(x) for x in re.findall(r"silence_duration:\s*(\d+(?:\.\d+)?)", r.stderr or "")]
        return max(runs) if runs else 0.0
    except Exception:
        return 0.0


def audio_is_silent(path, ffmpeg=None, floor_db=-45.0, max_silence_run=8.0):
    """True when a TTS part FAILED - either near-silent throughout, OR it speaks for a bit and
    then holds a long unbroken silence.

    Gemini TTS has two failure modes that a byte-size check passes straight into the voiceover as
    a dead hole:
      1. a whole chunk comes back near-digital-silence, and
      2. the read is TRUNCATED - the first third is spoken and the rest is padded with minutes of
         silence (the tickle part-5 bug: 56s of speech + 607s of silence). A whole-part MEAN check
         is fooled by the spoken head (that part averaged -36 dB and sailed past a -45 dB floor),
         so mean volume alone cannot see it.
    We therefore fail a part if its mean is below the floor OR it contains a single silent run
    longer than any legitimate narration pause (sentence gaps are well under 2s)."""
    ffmpeg = ffmpeg or pipeline.find_ffmpeg()
    try:
        r = subprocess.run([ffmpeg, "-hide_banner", "-i", str(path), "-af", "volumedetect",
                            "-f", "null", "-"], capture_output=True, text=True, timeout=120)
        m = re.search(r"mean_volume:\s*(-?\d+(?:\.\d+)?)\s*dB", r.stderr or "")
        mean = float(m.group(1)) if m else -99.0
    except Exception:
        return False                         # can't measure -> don't wrongly reject
    if mean <= floor_db:
        return True
    return longest_silence_run(path, ffmpeg, floor_db) >= max_silence_run


def concat_audio_parts(part_paths, out_path, ffmpeg):
    """Concatenate TTS parts RAW (concat demuxer, no resampling - resampling injects a noise
    floor; see pipeline.concat_audio_with_pause). Single part -> plain copy."""
    part_paths = [Path(p) for p in part_paths]
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if len(part_paths) == 1:
        # keep container/codec: transcode once to wav for a stable downstream format
        r = subprocess.run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                            "-i", str(part_paths[0]), "-ac", "2", "-ar", "44100",
                            str(out_path)], capture_output=True, text=True, timeout=300)
        if not out_path.exists():
            raise LongformError(f"Voiceover convert failed: {(r.stderr or '')[-300:]}")
        return out_path
    # normalize every part to the same PCM format first, then concat losslessly
    tmp_parts = []
    for i, p in enumerate(part_paths):
        t = out_path.with_name(f"_part{i:02d}.wav")
        r = subprocess.run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(p),
                            "-ac", "2", "-ar", "44100", "-c:a", "pcm_s16le", str(t)],
                           capture_output=True, text=True, timeout=300)
        if not t.exists():
            raise LongformError(f"Voiceover part convert failed: {(r.stderr or '')[-300:]}")
        tmp_parts.append(t)
    lst = out_path.with_name("_parts.txt")
    lst.write_text("".join(f"file '{p.name}'\n" for p in tmp_parts), encoding="utf-8")
    r = subprocess.run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-f", "concat",
                        "-safe", "0", "-i", str(lst), "-c", "copy", str(out_path)],
                       capture_output=True, text=True, timeout=300, cwd=str(out_path.parent))
    for t in tmp_parts:
        t.unlink(missing_ok=True)
    lst.unlink(missing_ok=True)
    if not out_path.exists():
        raise LongformError(f"Voiceover concat failed: {(r.stderr or '')[-300:]}")
    return out_path


def apply_voice_speed(path, speed, ffmpeg=None, status_cb=None):
    """Return a pitch-preserving re-tempoed copy of ``path``.

    Never replace ``path`` itself.  The approval player can still be streaming that file when
    the user clicks Continue and Windows then rejects ``os.replace`` with ``WinError 5``.  A
    versioned output also keeps the untouched TTS take as the source of truth, so selecting a
    different speed later cannot accidentally apply atempo twice.
    """
    try:
        speed = float(speed or 0)
    except (TypeError, ValueError):
        return Path(path)
    path = Path(path)
    if not speed:
        return path
    speed = max(0.5, min(2.0, speed))     # the picker offers 0.90-1.30; never trust it blindly
    if abs(speed - 1.0) < 0.01:
        return path
    ffmpeg = ffmpeg or pipeline.find_ffmpeg()
    if not ffmpeg:
        return path
    try:
        stat = path.stat()
        source_version = f"{stat.st_size:x}_{stat.st_mtime_ns:x}"
    except OSError:
        source_version = str(time.time_ns())
    speed_tag = f"{speed:.2f}".replace(".", "p")
    # The timestamp makes every conversion target unique.  ffmpeg therefore never has to open
    # an earlier preview/output for replacement either (that file may also still be streamed).
    out_path = path.with_name(
        f"{path.stem}_speed_{speed_tag}x_{source_version}_{time.time_ns():x}{path.suffix}")
    # atempo only accepts 0.5..2.0, so anything outside has to be chained - pipeline already
    # knows how to build that chain, and the clip pipeline uses the same one
    out = subprocess.run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(path),
                          "-af", pipeline.atempo_filter_chain(speed), str(out_path)],
                         capture_output=True, text=True, timeout=600)
    if not out_path.exists() or out_path.stat().st_size < MIN_AUDIO_BYTES:
        out_path.unlink(missing_ok=True)
        raise LongformError((out.stderr or "voice speed conversion failed")[-180:])
    _log(status_cb, f"Narration speed set to {speed:.2f}x.")
    return out_path


def voiceover_path_from_state(out_dir, state=None):
    """Resolve the immutable voiceover selected by the resume state, safely inside out_dir."""
    out_dir = Path(out_dir)
    name = Path(str((state or {}).get("voiceover_file") or "voiceover.wav")).name
    selected = out_dir / name
    if _audio_done(selected):
        return selected
    return out_dir / "voiceover.wav"


def generate_voiceover(script, out_dir, tts_model="pro", status_cb=None, cancel_event=None,
                       speech_gate=None, voice=None, speaker=None, resume=True, mix_gate=None,
                       tts_options=None):
    """Script -> voiceover.wav (parts stitched). Returns (path, parts_count).

    `voice` / `speaker` pick the Gemini TTS narrator (None = pipeline defaults), so longform uses
    the same narrator selection as the other modes instead of always the built-in default voice.

    `speech_gate(parts_info, regen_part)` (optional, "Halt after speech"): called AFTER all
    TTS parts exist and BEFORE they are stitched. `parts_info` is a list of
    {"index", "text", "path"}; `regen_part(i)` re-generates part i with a fresh TTS take and
    returns the new path. The gate blocks until the user has approved every part (declines
    trigger regen through the callback); it raises to cancel the run.

    RESUME: the parts are only stitched (and deleted) once the gate has approved them all, so a
    run that is cancelled or lost in the approval gate leaves every `vo_part*.wav` behind while
    `voiceover.wav` never appears - the outer resume check misses it and used to pay for the whole
    TTS again. The part paths are therefore recorded in state.json as they are produced (takes
    included) and reused here, which drops you straight back into the approval screen.
    """
    parts = split_script_for_tts(script)
    if not parts:
        raise LongformError("The script is empty.")
    ffmpeg = pipeline.find_ffmpeg()
    if not ffmpeg:
        raise LongformError("ffmpeg not found.")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    # only pass a narrator when one was chosen, so an empty pick keeps pipeline's own defaults
    tts_kw = {"style": pipeline.TTS_STYLE_LONGFORM}
    tts_settings = dict(tts_options or {}) if tts_model in pipeline.SEED_SPEECH_TTS_ALIASES else {}
    if voice:
        tts_kw["voice"] = voice
    if speaker:
        tts_kw["speaker"] = speaker
    if tts_model in pipeline.SEED_SPEECH_TTS_ALIASES:
        opts = dict(tts_options or {})
        tts_kw.update({
            "voice_instruction": str(opts.get("voice_instruction") or "").strip() or None,
            "auto_upbeat": False,
            "language": str(opts.get("language") or "").strip(),
            "tts_speed": opts.get("speed", 1.0),
            "volume": opts.get("volume", 1.0),
            "pitch": opts.get("pitch", 0),
            "sample_rate": opts.get("sample_rate", 24000),
            "output_format": opts.get("output_format", "mp3"),
        })

    # Reusable parts must belong to THIS script, narrator and delivery directive: the state is keyed
    # on the script (load_state) and the split it was recorded under must still produce the same part
    # count, else part 3 of the old split would be spoken over part 3 of the new one. The saved list
    # is an index-aligned PREFIX - a run that died on part 3 of 8 saved 2 paths, and those 2 are
    # still worth reusing - so it is the recorded total that is compared, not the list length.
    # The style matters as much as the voice: parts recorded under the old viral-narrator directive
    # are the wrong PERFORMANCE, and reusing them would silently undo an edit to TTS_STYLE_LONGFORM.
    saved = load_state(out_dir, script) if resume else None

    # A stitched voiceover for this exact script/narrator/directive IS the finished product of this
    # function. The parts are deleted at stitch time, so without this shortcut an interruption
    # anywhere after it - the mix gate below blocks on a human, the transcription takes minutes -
    # would find no parts and re-buy the entire TTS.
    raw_voice_path = out_dir / "voiceover.wav"
    saved_voice_path = voiceover_path_from_state(out_dir, saved)
    if (resume and saved and saved.get("voiceover_ready") and _audio_done(raw_voice_path)
            and str(saved.get("voice") or "") == str(voice or "")
            and str(saved.get("tts_model") or tts_model) == str(tts_model)
            and dict(saved.get("tts_settings") or {}) == tts_settings
            and str(saved.get("tts_style") or "") == str(tts_kw["style"] or "")):
        _log(status_cb, f"Resume: the voiceover is already stitched ({raw_voice_path.name}) - keeping it.")
        # The mix gate (hear the whole take, set the speed) runs AFTER stitching, so a run killed
        # at that screen resumes right here - with the choice never made. Re-offer it; once a
        # speed is recorded ("keep 1.0x" included) the question is settled and stays settled.
        if mix_gate is not None and saved.get("voice_speed") is None:
            speed = mix_gate(str(raw_voice_path))
            saved_voice_path = apply_voice_speed(raw_voice_path, speed, ffmpeg,
                                                 status_cb=status_cb)
            save_state(out_dir, voice_speed=float(speed or 1.0),
                       voiceover_file=saved_voice_path.name)
        elif (saved.get("voice_speed") not in (None, 1, 1.0)
              and (Path(str(saved.get("voiceover_file") or "voiceover.wav")).name == "voiceover.wav"
                   or not _audio_done(out_dir / Path(str(saved.get("voiceover_file"))).name))):
            # The selected derivative was removed, but the paid raw TTS is intact. Rebuild it
            # locally without another approval/TTS round.
            saved_voice_path = apply_voice_speed(raw_voice_path, saved.get("voice_speed"), ffmpeg,
                                                 status_cb=status_cb)
            save_state(out_dir, voiceover_file=saved_voice_path.name)
        return saved_voice_path, int(saved.get("tts_parts") or len(parts))

    saved_files = (saved or {}).get("tts_part_files") or []
    if (int((saved or {}).get("tts_part_total") or 0) != len(parts)
            or str((saved or {}).get("voice") or "") != str(voice or "")
            or str((saved or {}).get("tts_model") or tts_model) != str(tts_model)
            or dict((saved or {}).get("tts_settings") or {}) != tts_settings
            or str((saved or {}).get("tts_style") or "") != str(tts_kw["style"] or "")):
        saved_files = []

    part_files = []

    def _remember():
        """Record the parts after every TTS call, so a run killed halfway keeps what it paid for.

        The timings, prompts and duration in the state describe the PREVIOUS voiceover, so they
        are dropped in the same write: this is the point where the new script enters the state,
        and a resume that found the new script next to the old lines would happily pair the new
        audio with the old script's timings.
        """
        save_state(out_dir, script=script, voice=voice or "", tts_model=tts_model,
                   tts_style=tts_kw["style"] or "", tts_settings=tts_settings,
                   tts_part_files=[str(p) for p in part_files], tts_part_total=len(parts),
                   # parts are being (re)made, so any stitched voiceover on disk is the old one.
                   # voice_speed None means "the speed question was never answered" - a NUMBER
                   # (1.0 included) means the user chose, and only then may resume skip the gate.
                   voiceover_ready=False, voice_speed=None, voiceover_file="voiceover.wav",
                   lines=None, prompts=None, audio_duration=0.0)

    for i, part in enumerate(parts):
        if cancel_event is not None and cancel_event.is_set():
            raise pipeline.PipelineCancelled("Cancelled.")
        existing = saved_files[i] if i < len(saved_files) else ""
        if existing and _audio_done(existing) and not audio_is_silent(existing, ffmpeg):
            _log(status_cb, f"Resume: reusing voiceover part {i + 1}/{len(parts)} "
                            f"({Path(existing).name}) - no new TTS.")
            part_files.append(Path(existing))
            continue
        _tts_label = ("ByteDance Seed Speech TTS 2.0" if tts_model in pipeline.SEED_SPEECH_TTS_ALIASES
                      else f"Gemini 2.5 {'Pro' if tts_model == 'pro' else 'Flash'} TTS")
        _log(status_cb, f"Voiceover part {i + 1}/{len(parts)} ({len(part)} chars) "
                        f"with {_tts_label}"
                        f"{(' - narrator ' + str(voice)) if voice else ''}...")
        p = None
        for attempt in range(3):             # a silent part = failed TTS; retry before accepting
            p = pipeline.generate_speech_gemini(part, out_dir / f"vo_part{i:02d}.wav",
                                                model=tts_model, cancel_event=cancel_event,
                                                status_cb=status_cb, **tts_kw)
            if not (p and _audio_done(p)):
                _log(status_cb, f"Voiceover part {i + 1} produced no audio - retry {attempt + 1}/3.")
                continue
            if audio_is_silent(p, ffmpeg):
                _log(status_cb, f"Voiceover part {i + 1} came back SILENT - retry {attempt + 1}/3.")
                continue
            break
        if not (p and _audio_done(p)):
            raise LongformError(f"Voiceover part {i + 1} failed to generate audible speech.")
        if audio_is_silent(p, ffmpeg):
            raise LongformError(
                f"Voiceover part {i + 1} keeps coming back silent from the TTS - stopping instead "
                "of stitching a silent gap into the voiceover. Try again or switch narrator/model.")
        part_files.append(p)
        _remember()
    # the per-part writes only ever hold the prefix generated SO FAR; this one records the reused
    # tail as well, so a kill in the gate below does not re-buy parts that are sitting on disk
    _remember()

    if speech_gate is not None:
        parts_info = [{"index": i, "text": parts[i], "path": str(part_files[i])}
                      for i in range(len(parts))]
        take_counter = {}

        def regen_part(i):
            if cancel_event is not None and cancel_event.is_set():
                raise pipeline.PipelineCancelled("Cancelled.")
            take = take_counter.get(i, 0) + 1
            take_counter[i] = take
            _log(status_cb, f"Re-generating voiceover part {i + 1}/{len(parts)} (take {take + 1})...")
            # a NEW filename per take: browsers cache the old audio URL otherwise
            new_path = pipeline.generate_speech_gemini(
                parts[i], out_dir / f"vo_part{i:02d}_take{take}.wav",
                model=tts_model, cancel_event=cancel_event, status_cb=status_cb, **tts_kw)
            old = part_files[i]
            part_files[i] = Path(new_path)
            try:
                if str(old) != str(new_path):
                    Path(old).unlink(missing_ok=True)
            except Exception:
                pass
            _remember()     # the take replaces the part: resume must not point at the deleted one
            return str(new_path)

        _log(status_cb, f"Halt after speech: waiting for your approval of {len(parts)} "
                        "voiceover part(s)...")
        speech_gate(parts_info, regen_part)
        _log(status_cb, "All voiceover parts approved - stitching and continuing.")

    out = concat_audio_parts(part_files, out_dir / "voiceover.wav", ffmpeg)
    # Last line of defence: even with the per-part guards, a silent hole in the STITCHED voiceover
    # is the one defect the user must never ship unknowingly. Scan the whole take and refuse to
    # continue on a minutes-long dead stretch - regenerating a specific part here is impossible
    # (they are about to be deleted), so this hard-fails with a clear message instead.
    hole = longest_silence_run(out, ffmpeg)
    if hole >= 15.0:
        raise LongformError(
            f"The stitched voiceover contains a {hole:.0f}s silent gap - a TTS part came back "
            "truncated/silent. Not continuing with a dead hole in the narration; please start the "
            "voiceover again (a fresh run re-generates the failed part).")
    # Keep the approved source parts.  They are the only lossless way to review or regenerate one
    # paragraph of an existing Sketch Explainer without buying/rebuilding the other paragraphs.
    # Older builds deleted them here, which made per-part editing impossible after the first run.
    save_state(out_dir, tts_part_files=[str(Path(p)) for p in part_files], tts_model=tts_model,
               tts_settings=tts_settings, tts_part_texts=list(parts), voiceover_ready=True,
               voice_speed=None, voiceover_file=out.name)
    _log(status_cb, f"Voiceover ready: {out.name} ({len(parts)} part(s) stitched).")

    # `mix_gate(path) -> speed`: the stitched voiceover, played whole, before anything is timed
    # against it. Returns the narration speed to bake in (None/1.0 = leave it). It blocks on the
    # user, which is exactly why the resume shortcut above exists.
    if mix_gate is not None:
        speed = mix_gate(str(out))
        out = apply_voice_speed(out, speed, ffmpeg, status_cb=status_cb)
        save_state(out_dir, voice_speed=float(speed or 1.0), voiceover_file=out.name)
    return out, len(parts)


def audio_duration_seconds(path, ffprobe=None):
    """Return an audio duration without trusting a browser/file-size approximation."""
    path = Path(path)
    try:
        with __import__("wave").open(str(path), "rb") as wav:
            return wav.getnframes() / float(wav.getframerate() or 1)
    except Exception:
        pass
    ffprobe = ffprobe or pipeline.find_ffprobe(pipeline.find_ffmpeg())
    try:
        done = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nokey=1:noprint_wrappers=1", str(path)],
            capture_output=True, text=True, timeout=30, check=False)
        return float((done.stdout or "0").strip() or 0.0)
    except Exception:
        return 0.0


def _speech_review_manifest_path(out_dir):
    return Path(out_dir) / "speech_parts" / "manifest.json"


def save_speech_review_manifest(out_dir, script, parts, **extra):
    """Persist the exact paragraph-to-audio mapping used by the approval waveform."""
    out_dir = Path(out_dir)
    manifest_path = _speech_review_manifest_path(out_dir)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": 1, "script": str(script or ""), "parts": parts, **extra}
    tmp = manifest_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, manifest_path)
    return payload


def load_speech_review_parts(out_dir, script=None):
    """Load durable speech parts, including parts retained by newer TTS runs.

    The manifest is preferred because its text/audio association is explicit.  For a project
    created before the manifest existed, retained ``tts_part_files`` are upgraded in place.
    """
    out_dir = Path(out_dir)
    state = load_state(out_dir, script) or {}
    script = str(script if script is not None else state.get("script") or "")
    manifest_path = _speech_review_manifest_path(out_dir)
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        rows = list(manifest.get("parts") or [])
        if manifest.get("script") == script and rows and all(
                _audio_done(out_dir / str(row.get("file") or "")) for row in rows):
            return manifest
    except Exception:
        pass

    texts = list(state.get("tts_part_texts") or split_script_for_tts(script))
    files = [Path(p) for p in (state.get("tts_part_files") or [])]
    if len(files) != len(texts) or not all(_audio_done(p) for p in files):
        return None
    speed = float(state.get("voice_speed") or 1.0)
    # Retained TTS source parts are natural-speed audio, while the selected combined voiceover
    # may be 1.15x. Review and regeneration must use ONE clock or a replaced paragraph would be
    # fast between slower neighbours (and every downstream image switch would drift again).
    if abs(speed - 1.0) >= 0.01:
        files = [apply_voice_speed(path, speed) for path in files]
    rows = []
    for idx, (text, path) in enumerate(zip(texts, files)):
        rows.append({"index": idx, "text": text, "file": os.path.relpath(path, out_dir),
                     "duration": round(audio_duration_seconds(path), 3), "take": 0})
    return save_speech_review_manifest(
        out_dir, script, rows, voice=str(state.get("voice") or ""),
        tts_model=str(state.get("tts_model") or "pro"),
        voice_speed=speed)


def reconstruct_speech_review_parts(out_dir, script=None, status_cb=None):
    """Upgrade an old project by cutting its selected voiceover back into TTS-sized parts.

    Word-aligned line timings are used when present, so cuts land between the exact script
    sections.  The proportional fallback is only for very old projects without word timing.
    """
    out_dir = Path(out_dir)
    state = load_state(out_dir, script) or {}
    script = str(script if script is not None else state.get("script") or "")
    texts = split_script_for_tts(script)
    source = voiceover_path_from_state(out_dir, state)
    if not texts or not _audio_done(source):
        return None
    duration = audio_duration_seconds(source)
    if duration <= 0:
        return None
    word_starts = []
    for line in state.get("lines") or []:
        for word in line.get("words") or []:
            try:
                word_starts.append(float(word.get("s")))
            except (TypeError, ValueError):
                pass
    counts = [max(1, len(re.findall(r"\b[\w'-]+\b", text))) for text in texts]
    total_words = sum(counts)
    boundaries = [0.0]
    consumed = 0
    for count in counts[:-1]:
        consumed += count
        if len(word_starts) >= total_words and consumed < len(word_starts):
            before = word_starts[max(0, consumed - 1)]
            after = word_starts[consumed]
            boundary = (before + after) / 2.0
        else:
            boundary = duration * consumed / float(total_words or 1)
        boundaries.append(max(boundaries[-1] + 0.01, min(duration, boundary)))
    boundaries.append(duration)

    parts_dir = out_dir / "speech_parts"
    parts_dir.mkdir(parents=True, exist_ok=True)
    ffmpeg = pipeline.find_ffmpeg()
    rows = []
    for idx, text in enumerate(texts):
        target = parts_dir / f"part_{idx:03d}.wav"
        start, end = boundaries[idx], boundaries[idx + 1]
        done = subprocess.run(
            [ffmpeg, "-y", "-ss", f"{start:.6f}", "-to", f"{end:.6f}", "-i", str(source),
             "-vn", "-acodec", "pcm_s16le", str(target)],
            capture_output=True, text=True, timeout=max(120, int(end - start) * 2), check=False)
        if done.returncode != 0 or not _audio_done(target):
            raise LongformError(f"Could not prepare speech part {idx + 1}: "
                                f"{(done.stderr or '')[-240:]}")
        rows.append({"index": idx, "text": text,
                     "file": os.path.relpath(target, out_dir),
                     "duration": round(audio_duration_seconds(target), 3), "take": 0})
    _log(status_cb, f"Prepared {len(rows)} reviewable speech parts from the existing voiceover.")
    return save_speech_review_manifest(
        out_dir, script, rows, voice=str(state.get("voice") or ""),
        tts_model=str(state.get("tts_model") or "pro"),
        voice_speed=float(state.get("voice_speed") or 1.0), source=source.name)


def commit_speech_review_parts(out_dir, manifest, status_cb=None):
    """Stitch reviewed parts and invalidate every old image-switch timestamp.

    The images themselves remain on disk.  On the next project continuation the new voiceover is
    transcribed, :func:`retime_longform_assets` renames the matching images onto the new clock,
    and ``timeline.json`` is rewritten from those new positions.
    """
    out_dir = Path(out_dir)
    state = load_state(out_dir, manifest.get("script")) or {}
    rows = sorted(manifest.get("parts") or [], key=lambda row: int(row.get("index", 0)))
    paths = [out_dir / str(row.get("file") or "") for row in rows]
    if not rows or not all(_audio_done(path) for path in paths):
        raise LongformError("One or more reviewed speech parts are missing.")
    ffmpeg = pipeline.find_ffmpeg()
    target = out_dir / f"voiceover_review_{int(time.time() * 1000)}.wav"
    concat_audio_parts(paths, target, ffmpeg)
    # A second review can happen before the pending retime has run. Preserve the ORIGINAL clock
    # in that case; replacing it with empty `lines` would orphan every existing image.
    old_lines = list(state.get("lines") or state.get("retime_source_lines") or [])
    old_prompts = list(state.get("prompts") or state.get("retime_source_prompts") or [])
    old_duration = float(state.get("audio_duration") or
                         state.get("retime_source_audio_duration") or 0.0)
    save_state(
        out_dir, voiceover_ready=True, voiceover_file=target.name,
        tts_parts=len(rows), tts_part_total=len(rows),
        tts_part_files=[str(path) for path in paths],
        tts_part_texts=[str(row.get("text") or "") for row in rows],
        # Explicit retime source: clearing `lines` prevents resume from pairing a new narration
        # with the previous image clock, while these fields let it reuse/rename the images.
        retime_source_lines=old_lines, retime_source_prompts=old_prompts,
        retime_source_audio_duration=old_duration,
        lines=None, prompts=None, audio_duration=0.0)
    _log(status_cb, "Reviewed speech saved. Image-switch timings will be regenerated from the "
                    "new narration when the project continues.")
    return target


def finalize_speech_review_timing(out_dir, status_cb=None):
    """Immediately rebuild image filenames + timeline after speech-part regeneration."""
    out_dir = Path(out_dir)
    try:
        state = json.loads((out_dir / STATE_FILE).read_text(encoding="utf-8"))
    except Exception:
        state = {}
    script = str(state.get("script") or "")
    old_lines = list(state.get("retime_source_lines") or [])
    old_prompts = list(state.get("retime_source_prompts") or [])
    old_duration = float(state.get("retime_source_audio_duration") or 0.0)
    voice = voiceover_path_from_state(out_dir, state)
    if not script or not old_lines or not _audio_done(voice):
        raise LongformError("The previous image clock or the reviewed voiceover is missing.")
    _log(status_cb, "Re-transcribing reviewed narration to rebuild every image switch...")
    new_duration = audio_duration_seconds(voice)
    new_lines = transcribe_lines(script, voice, status_cb=status_cb)
    if len(new_lines) != len(old_lines):
        raise LongformError(
            f"Speech retiming produced {len(new_lines)} lines but the project has "
            f"{len(old_lines)} images. The old timeline was kept so no image is misplaced.")
    prompts = retime_longform_assets(
        out_dir, old_lines, new_lines, old_duration, new_duration,
        prompts=old_prompts, status_cb=status_cb)
    save_state(out_dir, lines=new_lines, prompts=prompts,
               audio_duration=round(new_duration, 3),
               retime_source_lines=None, retime_source_prompts=None,
               retime_source_audio_duration=0.0)
    write_transcript(new_lines, out_dir / "transcript.txt")
    if prompts:
        write_prompts_file(prompts, out_dir / f"image_prompts_{out_dir.name}.txt")
    # Retime can expose empty slots even though suitable pictures are parked in audit/archive
    # folders. Reuse those project-local assets immediately before declaring anything missing.
    recover_archived_images(out_dir, status_cb=status_cb)
    natural_durations = line_durations(new_lines, new_duration)
    results = {}
    for idx, line in enumerate(new_lines):
        candidate = out_dir / "images" / f"{image_key(idx, line, natural_durations[idx])}.png"
        if _image_done(candidate, "16:9"):
            results[idx] = str(candidate)
    cut_durations = caption_cut_durations(new_lines, prompts, new_duration)
    write_timeline_manifest(
        new_lines, cut_durations, results, new_duration, out_dir / "timeline.json",
        voice_speed=float(state.get("voice_speed") or 1.0))
    _log(status_cb, f"Timeline retimed: {len(new_lines)} image switches now follow the "
                    "regenerated narration.")
    return {"lines": len(new_lines), "duration": round(new_duration, 3),
            "images": len(results), "timeline": str(out_dir / "timeline.json")}


# ------------------------------------------------------------------ 2) TIMESTAMPS

def split_script_lines(script, min_words=4, max_words=14):
    """Split the script into clause-sized display lines (like the foziscribe example):
    sentences first, long sentences split at commas, tiny fragments merged forward."""
    text = re.sub(r"\s+", " ", str(script or "")).strip()
    sentences = [s.strip() for s in re.findall(r"[^.!?]+[.!?]+|\S[^.!?]*$", text) if s.strip()]
    lines = []
    for s in sentences:
        words = s.split()
        if len(words) <= max_words:
            lines.append(s)
            continue
        # split at commas into chunks <= max_words
        clauses = [c.strip() for c in re.split(r"(?<=,)\s+", s) if c.strip()]
        cur = ""
        for c in clauses:
            cand = (cur + " " + c).strip() if cur else c
            if cur and len(cand.split()) > max_words:
                lines.append(cur)
                cur = c
            else:
                cur = cand
        if cur:
            lines.append(cur)
    # merge fragments that are too short into the previous line
    merged = []
    for ln in lines:
        if merged and len(ln.split()) < min_words and len(merged[-1].split()) + len(ln.split()) <= max_words + 4:
            merged[-1] = (merged[-1] + " " + ln).strip()
        else:
            merged.append(ln)
    return merged


def fmt_ts(seconds, precise=True):
    """[m:ss.d] with sub-second precision (more exact than the foziscribe example)."""
    seconds = max(0.0, float(seconds))
    m = int(seconds // 60)
    s = seconds - m * 60
    return f"[{m}:{s:04.1f}]" if precise else f"[{m}:{int(s):02d}]"


def transcribe_lines(script, audio_path, status_cb=None):
    """Voiceover -> [{start, end, text}] lines with EXACT timings.
    The known script is aligned onto faster-whisper word timings (voice_align), then the
    script's own clause lines are mapped onto those word timings 1:1."""
    import voice_align
    if not voice_align.available():
        raise LongformError("faster-whisper is not installed (pip install faster-whisper).")
    _log(status_cb, "Transcribing the voiceover (faster-whisper, word timestamps)...")
    asr_words = voice_align.transcribe_words(audio_path, status_cb=status_cb)
    if not asr_words:
        raise LongformError("Transcription produced no words.")
    aligned = voice_align.align_script_to_words(str(script), asr_words)
    lines = split_script_lines(script)
    out = []
    wi = 0
    for ln in lines:
        n = len(ln.split())
        chunk = aligned[wi:wi + n]
        wi += n
        if not chunk:
            break
        out.append({"start": round(float(chunk[0]["start"]), 2),
                    "end": round(float(chunk[-1]["end"]), 2),
                    "text": ln,
                    # per-word timings: the caption-synced cut needs to know WHEN inside the
                    # line its key word is actually spoken
                    "words": [{"w": str(w.get("word") or w.get("w") or ""),
                               "s": round(float(w["start"]), 2)} for w in chunk]})
    # audio end = real duration (last line holds to the end during assembly)
    _log(status_cb, f"Transcript: {len(out)} timestamped line(s).")
    return out


def write_transcript(lines, out_path):
    out_path = Path(out_path)
    out_path.write_text("\n".join(f"{fmt_ts(l['start'])} {l['text']}" for l in lines) + "\n",
                        encoding="utf-8")
    return out_path


# ------------------------------------------------------------------ 3) IMAGE PROMPTS

# Bump when the STAGE-3 doodle-prompt FORMAT changes. A resumed project whose saved prompts
# predate this version regenerates ALL prompts - and the images made from them - in the new
# style instead of reusing the old-format cache.
# 4: every caption/label/on-screen word removed. The frames carry no text at all now, so a
# cached v3 prompt (which MANDATED an ALL-CAPS top caption) would bake words back in.
PROMPT_FORMAT_VERSION = 4

# The mascot is described in WORDS: the image endpoint takes text only, and the look has to
# survive hundreds of frames anyway.
# A short, rigid description keeps it recognisable; the last line is what makes it feel part of
# the drawing instead of a sticker (user 2026-07-25: "seine aktionen sollen zum bild passen").
MASCOT_ART = Path(__file__).resolve().parent / "assets" / "mascot" / "blob.png"
MASCOT_NAME = "Blob"
MASCOT_LOOK = (
    "a small mascot called Blob: one rounded blob-shaped body in warm mustard yellow with a "
    "thick dark charcoal outline, a wide flat bottom, no arms or legs, and two oversized "
    "white circular eyes of different sizes sitting high and off-centre with small dark "
    "pupils. Always this exact character, same colours, same thick outline, drawn in the same "
    "flat doodle style as the rest of the frame")
MASCOT_RULE = (
    "MASCOT: hide {name} somewhere in this frame - {look}. Keep it SMALL (roughly a tenth of "
    "the frame height) and place it off to one side, in a corner, behind or peeking around "
    "something. It must never be the subject and never overlap the main action. Give it ONE "
    "small reaction that fits what this frame shows - watching, "
    "hiding, leaning in, looking away, mimicking the subject - so it belongs to the scene.")


def mascot_clause():
    """The sentence appended to every image prompt when the mascot option is on."""
    return MASCOT_RULE.format(name=MASCOT_NAME, look=MASCOT_LOOK)


def add_mascot(prompts):
    """Append the mascot instruction to every prompt row (idempotent).

    Rows are {"timestamp": ..., "prompt": ...}; only the prompt text is touched, so the
    positional line mapping and the timestamp check downstream stay intact.
    """
    clause = mascot_clause()
    out = []
    for row in prompts:
        if not isinstance(row, dict):
            txt = str(row or "").strip()
            out.append(txt if "MASCOT:" in txt else (txt.rstrip(". ") + ". " + clause))
            continue
        txt = str(row.get("prompt") or "").strip()
        if txt and "MASCOT:" not in txt:
            row = dict(row, prompt=txt.rstrip(". ") + ". " + clause)
        out.append(row)
    return out


STAGE3_PROMPT = """## STAGE 3 - GENERATE IMAGE PROMPTS FOR EVERY TIMESTAMP

Once the user pastes their timestamped script, generate one detailed text-to-image prompt for every single timestamp line.

**IMAGE PROMPT RULES:**

1. Every prompt must begin with its timestamp COPIED EXACTLY as it appears in the script (e.g. `[0:03.4]`) - do not reformat or round timestamps
2. Every prompt must open with the style anchor: "Hand-drawn 2D doodle cartoon animation, flat colors, bold black outlines, slightly imperfect sketchy marker lines,"
3. Every prompt must end with the style lock: "no text, no words, no letters, no numbers, no captions, no labels, no signage, no gradients, no shadows, no textures, no photorealism, no 3D, no realistic faces, no anime style, 16:9 aspect ratio, educational YouTube explainer doodle style."
4. Be specific inside each prompt - describe what characters are present and what they are doing, their exact expression, what objects are in the scene, what background color is used

5. ABSOLUTELY NO TEXT IN THE IMAGE. The frame must contain no written language of any kind: no captions, no titles, no labels, no speech bubbles with words, no signs, no book covers with titles, no numbers on clocks or scoreboards, no letters on shirts. NEVER write phrases like `text at the top reading "..."`, `label reading "..."` or `sign saying "..."` into a prompt. The narration carries the words; the picture carries only the picture. If a scene seems to need a word, replace it with a DRAWN symbol instead - a question mark, an exclamation mark, an arrow, a heart, a skull, a cross, a tick, a lightbulb, a magnifying glass, a clock face without numbers.
6. Translate abstract narration into concrete visuals - if the script says "your body doesn't know the difference", show a confused stick figure looking back and forth between two identical objects with a large question mark above its head; if it says "millions of years", show a huge hourglass with almost all the sand fallen through and a tiny stick figure beside it for scale.

GOLD-STANDARD EXAMPLE (match this exact shape - style anchor, then a rich concrete wordless scene, then background, then the style lock):
`Hand-drawn 2D doodle cartoon animation, flat colors, bold black outlines, slightly imperfect sketchy marker lines, a helmeted stick figure and a crowned stick figure happily clinking two frothy beer mugs together with big warm smiles, a red heart floating above them and a small white dove flying overhead, plain white background, no text, no words, no letters, no numbers, no captions, no labels, no signage, no gradients, no shadows, no textures, no photorealism, no 3D, no realistic faces, no anime style, 16:9 aspect ratio, educational YouTube explainer doodle style.`

7. Match tone to background color:
   - Ancient / prehistoric -> tan or dark blue background
   - Danger / threat -> stark white or a red-tinted sky
   - Happy / triumph / discovery -> bright white or yellow background
   - Underwater / science -> solid blue background
   - Outdoor / nature / evolution -> flat green ground + blue sky
   - Fire / night / ancient ritual -> solid orange background
8. Hold scenes across consecutive timestamps - if 3 lines describe the same moment, keep the same scene and only adjust the character's expression or add one new element. Do not generate a brand new scene every 5 seconds.
9. Use these proven WORDLESS frame types when appropriate:
   - **Single concept object:** one large object (hourglass, clock face without numbers, skull, lightbulb, brain) centered on a plain background
   - **Evolution sequence:** left-to-right creature or human progression with a big right-pointing arrow
   - **Pointed diagram:** an animal or object with a thick yellow arrow pointing at the one part that matters
   - **Stick figure reaction:** a thought bubble above the head containing a drawn SYMBOL - a question mark, an exclamation mark, a lightbulb, a skull - never a written word
   - **Villain personified:** an abstract concept given an angry cartoon face (a sun with a knife, a brain with boxing gloves)
   - **Globe + creatures:** an Earth globe centered, surrounded by floating cartoon animals or objects
   - **Comparison split:** two halves of the frame showing the two things being compared, divided by one bold black line

**OUTPUT FORMAT - DELIVER IN BATCHES OF 20**

Deliver the image prompts in batches of 20 prompts at a time inside copyable code blocks. Do NOT create any text file. Do NOT deliver all prompts at once.

Rules for batching:

- Output the first 20 prompts (or fewer, if the script has fewer remaining) inside ONE fenced code block. Separate each prompt from the next with exactly ONE blank line. Do NOT add any text, headers, or commentary between prompts inside the code block.
- After each batch's code block, if more timestamps remain, end with exactly this line, then stop and wait:

**Reply "next" for the next 20 prompts.**

- When the user replies "next", output the next batch of up to 20 prompts in a new code block, following the same format.
- Continue until every timestamp has a prompt.

Do not skip any timestamp. One timestamp = one prompt. Every prompt stays on its own line, with one blank line between prompts. Always output prompts in chronological timestamp order, and keep them in order across batches.

Only after the FINAL batch has been delivered - when every timestamp now has a prompt - end with exactly this line:

**All image prompts are now delivered - one for every timestamp in your script.**

Always include in every prompt: no text, no words, no letters, no numbers, no captions, no labels, no photorealism, no 3D render, no gradients, no drop shadows, no textures, no realistic faces, no anime style."""

_PROMPT_LINE_RE = re.compile(r"^\s*(\[\d+:\d{2}(?:\.\d)?\])\s*(.+)$")


def parse_prompt_batch(text):
    """Extract '[ts] prompt' lines from an assistant reply (inside or outside code fences)."""
    body = str(text or "")
    blocks = re.findall(r"```[a-zA-Z]*\n(.*?)```", body, re.S)
    source = "\n".join(blocks) if blocks else body
    out = []
    for raw in source.splitlines():
        m = _PROMPT_LINE_RE.match(raw.strip())
        if m:
            out.append({"timestamp": m.group(1), "prompt": m.group(2).strip()})
    return out


def generate_image_prompts(lines, reasoning_model=None, status_cb=None, cancel_event=None,
                           checkpoint_path=None, mascot=False):
    """Transcript lines -> one doodle prompt per line, via the STAGE-3 conversation.
    The app itself replies "next" until every timestamp is covered. Mapping is POSITIONAL
    (prompt N belongs to line N) with a timestamp sanity check. Raises on shortfall."""
    if not os.environ.get("WAVESPEED_API_KEY"):
        raise LongformError("WAVESPEED_API_KEY missing - image prompts need the reasoning model.")
    model = str(reasoning_model or "anthropic/claude-opus-4.8")
    transcript = "\n".join(f"{fmt_ts(l['start'])} {l['text']}" for l in lines)
    messages = [{"role": "system", "content": STAGE3_PROMPT},
                {"role": "user", "content": transcript}]
    checkpoint = Path(checkpoint_path) if checkpoint_path else None
    prompts, resumed_checkpoint = [], False

    def save_checkpoint():
        if not checkpoint:
            return
        checkpoint.parent.mkdir(parents=True, exist_ok=True)
        payload = {"line_count": len(lines), "first_ts": fmt_ts(lines[0]["start"]),
                   "last_ts": fmt_ts(lines[-1]["start"]), "prompts": prompts[:len(lines)]}
        tmp = checkpoint.with_suffix(checkpoint.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(checkpoint)

    if checkpoint and checkpoint.is_file():
        try:
            saved = json.loads(checkpoint.read_text(encoding="utf-8"))
            if (int(saved.get("line_count") or 0) == len(lines)
                    and saved.get("first_ts") == fmt_ts(lines[0]["start"])
                    and saved.get("last_ts") == fmt_ts(lines[-1]["start"])
                    and isinstance(saved.get("prompts"), list)):
                prompts = [p for p in saved["prompts"] if isinstance(p, dict)
                           and p.get("timestamp") and p.get("prompt")][:len(lines)]
                resumed_checkpoint = bool(prompts)
                if resumed_checkpoint:
                    _log(status_cb, f"Image prompts: resumed {len(prompts)}/{len(lines)} from "
                                    "the saved batch checkpoint.")
        except Exception as exc:  # noqa: BLE001
            _log(status_cb, f"Image prompt checkpoint was unreadable; starting fresh ({exc}).")
    max_rounds = (len(lines) // 20) + 4
    for round_no in range(0 if resumed_checkpoint else max_rounds):
        if cancel_event is not None and cancel_event.is_set():
            raise pipeline.PipelineCancelled("Cancelled.")
        _log(status_cb, f"Image prompts: batch {round_no + 1} from {model} "
                        f"({len(prompts)}/{len(lines)} so far)...")
        data = agent_core.post_json_url(agent_core.WAVESPEED_LLM_API, {
            "model": model, "messages": messages, "temperature": 0.6, "max_tokens": 8000,
        }, timeout=600)
        reply = data["choices"][0]["message"]["content"]
        batch = parse_prompt_batch(reply)
        if not batch:
            _log(status_cb, f"Prompt batch {round_no + 1} contained no parseable prompts; "
                            "switching to missing-slot recovery.")
            break
        prompts.extend(batch)
        save_checkpoint()
        messages.append({"role": "assistant", "content": reply})
        if len(prompts) >= len(lines) or "all image prompts are now delivered" in reply.lower():
            break
        messages.append({"role": "user", "content": "next"})
    # A model sometimes announces "all delivered" two or three items early. Recover ONLY the
    # missing tail in small exact batches instead of throwing away a 20-30 minute conversation.
    recovery_round = 0
    while len(prompts) < len(lines) and recovery_round < 3:
        if cancel_event is not None and cancel_event.is_set():
            raise pipeline.PipelineCancelled("Cancelled.")
        missing = lines[len(prompts):min(len(lines), len(prompts) + 20)]
        exact = "\n".join(f"{fmt_ts(line['start'])} {line['text']}" for line in missing)
        _log(status_cb, f"Image prompts: recovering {len(missing)} missing slot(s) "
                        f"({len(prompts)}/{len(lines)} saved)...")
        try:
            data = agent_core.post_json_url(agent_core.WAVESPEED_LLM_API, {
                "model": model,
                "messages": [
                    {"role": "system", "content":
                        "Generate exactly one 16:9 image prompt for every supplied timestamp. "
                        "Output only lines in the form '[m:ss.s] prompt', chronological, no fence, "
                        "no commentary. Every prompt must request a concrete hand-drawn 2D doodle "
                        "scene with flat colors, bold black outlines and simple stick figures, "
                        "containing NO written language of any kind, and explicitly: no text, no "
                        "words, no letters, no numbers, no captions, no labels, no photorealism, "
                        "no 3D, no gradients, no shadows, no textures, no anime."},
                    {"role": "user", "content": exact}],
                "temperature": 0.35, "max_tokens": 5000,
            }, timeout=300)
            recovered = parse_prompt_batch(data["choices"][0]["message"]["content"])
        except Exception as exc:  # noqa: BLE001
            recovered = []
            _log(status_cb, f"Missing-slot recovery attempt failed ({exc}).")
        if recovered:
            prompts.extend(recovered[:len(missing)])
            save_checkpoint()
        recovery_round += 1
    # Last-resort deterministic prompts keep the project renderable even if the LLM repeatedly
    # omits a line. They remain tied to the exact narration rather than duplicating random art.
    while len(prompts) < len(lines):
        idx = len(prompts)
        line = lines[idx]
        prompt = ("Hand-drawn 2D doodle cartoon animation, flat colors, bold black outlines, "
                  "slightly imperfect marker lines, one concrete visual metaphor for the narration "
                  f"\"{str(line.get('text') or '')[:300]}\", simple expressive stick figures and "
                  "one clear focal object, plain background, clean 16:9 composition, " + NO_TEXT_LOCK)
        prompts.append({"timestamp": fmt_ts(line["start"]), "prompt": prompt})
        _log(status_cb, f"Image prompts: built a safe local fallback for slot {idx + 1}.")
        save_checkpoint()
    prompts = prompts[:len(lines)]
    strip_text_from_prompts(prompts, status_cb=status_cb)
    mismatch = sum(1 for l, p in zip(lines, prompts)
                   if p["timestamp"] != fmt_ts(l["start"]))
    if mismatch or len(prompts) != len(lines):
        _log(status_cb, f"Image prompt timestamps differ from the transcript "
                        f"({mismatch} mismatched, {len(prompts)}/{len(lines)} prompts); "
                        "reusing existing images without generating replacements.")
    if mascot:
        prompts = add_mascot(prompts)
        _log(status_cb, f"Mascot: {MASCOT_NAME} hidden in all {len(prompts)} image prompts.")
    _log(status_cb, f"All {len(prompts)} image prompts delivered.")
    return prompts


def write_prompts_file(prompts, out_path):
    out_path = Path(out_path)
    out_path.write_text("\n\n".join(f"{p['timestamp']} {p['prompt']}" for p in prompts) + "\n",
                        encoding="utf-8")
    return out_path


# ------------------------------------------------------------------ 4) IMAGES (Ideogram)

CHARACTER_REFERENCE_PROMPT = (
    "Hand-drawn 2D doodle cartoon animation, flat colors, bold black outlines, slightly "
    "imperfect sketchy marker lines, a single friendly stick figure character standing in a "
    "neutral relaxed pose, arms at sides, simple round head with two dot eyes and a small "
    "smile, centered on a plain white background, full body visible, no other objects, "
    "no gradients, no shadows, no textures, no photorealism, no 3D, no realistic faces, "
    "no anime style, 16:9 aspect ratio, educational YouTube explainer doodle style.")


def image_key(index, line, duration):
    """Filename stem: index + timestamp + on-screen DURATION (user rule: length visible)."""
    ts = fmt_ts(line["start"]).replace(":", "-").replace(".", "-").strip("[]")
    return f"img{index:03d}_[{ts}]_dur{duration:.2f}s"


# The tail every prompt must carry. Ideogram is a TEXT-STRONG model - it will happily render
# any word it finds in the prompt - so the ban has to be stated as explicitly as the style.
NO_TEXT_LOCK = ("no text, no words, no letters, no numbers, no captions, no labels, no signage, "
                "no gradients, no shadows, no textures, no photorealism, no 3D, no realistic "
                "faces, no anime style, 16:9 aspect ratio, educational YouTube explainer "
                "doodle style.")

# Phrasings a model reaches for when it wants words on the frame. Each is cut out of the prompt
# rather than trusted to the style lock, because a positive instruction ("text reading X") beats
# a negative one ("no text") in every image model.
_TEXT_INSTRUCTION_RES = (
    # ...text at the top reading "WORD",   ...label reading "WORD",   ...sign saying "WORD"
    re.compile(r",?\s*[^,]*\b(?:text|caption|title|label|lettering|sign|signage|word|words|"
               r"headline|banner)\b[^,]*?\b(?:reading|saying|says|that reads|spelling)\b\s*"
               r"[\"“‘'][^\"”’']{0,80}[\"”’']", re.I),
    # bare "bold ALL CAPS text at the top" with no quoted string
    re.compile(r",?\s*[^,]*\b(?:all[- ]caps|bold black)\b[^,]*\b(?:text|caption|lettering)\b"
               r"[^,]*", re.I),
    # a leftover clause that only announces text
    re.compile(r",?\s*[^,]*\b(?:on-screen|onscreen)\s+(?:text|caption|words)\b[^,]*", re.I),
)


def strip_text_from_prompts(prompts, status_cb=None):
    """Guarantee the NO-TEXT contract mechanically (user 2026-08-16: "keine texte oder captions
    mehr in den images").

    The STAGE-3 spec forbids written language, but a model that spent a hundred prompts writing
    `text at the top reading "X"` drifts back into it - and one such clause outweighs the whole
    negative style lock. So every caption phrasing is CUT from the prompt here, and the lock is
    appended if it is missing. Mutates `prompts` in place; returns how many were repaired."""
    stripped = locked = 0
    for p in prompts:
        if not isinstance(p, dict):
            continue
        text = str(p.get("prompt") or "").strip()
        if not text:
            continue
        cleaned = text
        for pattern in _TEXT_INSTRUCTION_RES:
            cleaned = pattern.sub("", cleaned)
        cleaned = re.sub(r"\s{2,}", " ", cleaned).replace(" ,", ",").strip(" ,.")
        if cleaned != text.strip(" ,."):
            stripped += 1
        if "no text" not in cleaned.lower():
            cleaned = cleaned.rstrip(" ,.") + ", " + NO_TEXT_LOCK
            locked += 1
        else:
            cleaned = cleaned.rstrip(" ,") + ("" if cleaned.endswith(".") else ".")
        p["prompt"] = cleaned
    if stripped or locked:
        _log(status_cb, f"No-text contract enforced: {stripped} prompt(s) still asked for "
                        f"on-screen words (removed), {locked} were missing the no-text lock.")
    return stripped + locked


def caption_cut_starts(lines, prompts, audio_duration, lead=0.0):
    """Cut times: frame i appears when its narration line starts.

    This used to hunt for the moment the frame's CAPTION PHRASE was spoken, because a caption
    naming a word from the middle of a sentence ("...almost nothing. WHY?") would otherwise
    appear seconds early. The frames carry no words any more, so there is no phrase to sync to
    and the sentence start is the honest cut point. Cuts are forced monotonic and the first is
    pinned to 0 so the video never opens on black."""
    cuts = []
    for line in lines:
        cut = float(line["start"]) - float(lead or 0.0)
        if cuts:
            cut = max(cut, cuts[-1] + 0.05)
        cuts.append(max(0.0, min(cut, float(audio_duration or 0.0) or cut)))
    if cuts:
        cuts[0] = 0.0
    return cuts


def caption_cut_durations(lines, prompts, audio_duration):
    """Per-frame on-screen durations derived from the caption-synced cuts."""
    cuts = caption_cut_starts(lines, prompts, audio_duration)
    durs = []
    for i, c in enumerate(cuts):
        nxt = cuts[i + 1] if i + 1 < len(cuts) else max(audio_duration, c + 0.4)
        durs.append(max(0.35, round(nxt - c, 3)))
    return durs


def ensure_line_words(project_dir, status_cb=None):
    """Retrofit per-word timings onto a state whose lines predate the words field (needed by the
    caption-synced cuts). Re-transcribes the recorded voiceover and maps the words onto the
    EXISTING lines by word count - the stored start/end stay untouched."""
    import voice_align
    project_dir = Path(project_dir)
    try:
        state = json.loads((project_dir / STATE_FILE).read_text(encoding="utf-8"))
    except Exception:
        return False
    lines = state.get("lines") or []
    if not lines or all(l.get("words") for l in lines):
        return bool(lines)
    if not voice_align.available():
        _log(status_cb, "faster-whisper not available - keeping sentence-start cuts.")
        return False
    voice = project_dir / Path(str(state.get("voiceover_file") or "voiceover.wav")).name
    if not voice.is_file():
        return False
    _log(status_cb, "Computing per-word timings for caption-synced cuts (one-time)...")
    asr = voice_align.transcribe_words(voice, status_cb=status_cb)
    aligned = voice_align.align_script_to_words(str(state.get("script") or ""), asr)
    wi = 0
    for l in lines:
        n = len(str(l.get("text") or "").split())
        chunk = aligned[wi:wi + n]
        wi += n
        l["words"] = [{"w": str(w.get("word") or ""), "s": round(float(w["start"]), 2)}
                      for w in chunk]
    state["lines"] = lines
    tmp = (project_dir / STATE_FILE).with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(project_dir / STATE_FILE)
    _log(status_cb, "Per-word timings saved.")
    return True


def _archive_old_images(images_dir, status_cb=None):
    """Move existing frames aside (not delete) so a format change regenerates them from scratch.
    Nothing is lost - the old PNGs live on under images/_old_format_<ts>/ if ever needed."""
    images_dir = Path(images_dir)
    if not images_dir.is_dir():
        return 0
    stale = [p for p in images_dir.glob("*.png")]
    if not stale:
        return 0
    dest = images_dir / f"_old_format_{time.strftime('%Y%m%d_%H%M%S')}"
    dest.mkdir(parents=True, exist_ok=True)
    moved = 0
    for p in stale:
        try:
            p.replace(dest / p.name)
            moved += 1
        except Exception:
            pass
    if moved:
        _log(status_cb, f"Moved {moved} old-format frame(s) to {dest.name}/ - they will be "
                        "regenerated in the new caption style.")
    return moved


def _archive_images_from_index(images_dir, start_index, status_cb=None):
    """Archive only frames at/after a broken prompt slot so valid earlier frames can be reused."""
    images_dir = Path(images_dir)
    stale = list(images_dir.glob("*.png"))
    selected = []
    for path in stale:
        match = re.match(r"img(\d{3})_", path.name)
        if match and int(match.group(1)) >= int(start_index):
            selected.append(path)
    if not selected:
        return 0
    dest = images_dir / f"_prompt_repair_{time.strftime('%Y%m%d_%H%M%S')}"
    dest.mkdir(parents=True, exist_ok=True)
    moved = 0
    for path in selected:
        try:
            path.replace(dest / path.name)
            moved += 1
        except OSError:
            pass
    if moved:
        _log(status_cb, f"Moved {moved} frame(s) from broken prompt slot #{start_index + 1} onward "
                        f"to {dest.name}/ for regeneration.")
    return moved


def line_durations(lines, audio_duration):
    """Per-line on-screen duration: to the next line's start; last line holds to audio end."""
    durs = []
    for i, l in enumerate(lines):
        if i + 1 < len(lines):
            durs.append(max(0.4, round(lines[i + 1]["start"] - l["start"], 2)))
        else:
            durs.append(max(0.4, round(max(audio_duration, l["end"]) - l["start"], 2)))
    return durs


def speech_cut_durations(lines, audio_duration):
    """Return a gapless edit clock anchored to the spoken start of every line.

    Image filenames contain historical timestamps and prompt captions may name a word from the
    middle of a sentence.  Neither is a reliable edit point.  The local forced-alignment result
    in ``line['start']`` is: frame 0 opens at video time zero, every following frame switches
    when its corresponding narration line actually starts, and the final frame holds exactly to
    the end of the voiceover.  This produces one monotonic timeline without overlaps or holes.
    """
    if not lines:
        return []
    audio_end = max(0.0, float(audio_duration or 0.0))
    cuts = [0.0]
    for line in lines[1:]:
        cuts.append(max(cuts[-1] + 0.04, min(audio_end, float(line.get("start") or 0.0))))
    durations = []
    for idx, cut in enumerate(cuts):
        nxt = cuts[idx + 1] if idx + 1 < len(cuts) else max(audio_end, cut + 0.04)
        durations.append(max(0.04, round(nxt - cut, 3)))
    return durations


def saved_project_cut_durations(state, lines, audio_duration):
    """Return the edit clock selected by an already-saved longform project.

    Legacy projects retain caption-triggered cuts.  A project explicitly repaired to the speech
    clock must keep that choice when reopening or rendering; otherwise a harmless rebuild would
    silently put the old, early caption cuts back.
    """
    repair = (state or {}).get("retime_range") or {}
    if isinstance(repair, dict) and repair.get("method") == "speech-clock":
        return speech_cut_durations(lines, audio_duration)
    return caption_cut_durations(lines, (state or {}).get("prompts") or [], audio_duration)


def rebuild_timeline_from_speech_clock(out_dir, status_cb=None):
    """Repair a saved longform timeline from the real narration clock, without any API work.

    Image names used to encode caption-triggered holds.  That makes both the filename duration
    and the edit point drift from the narration whenever a caption word occurs in the middle of
    a sentence.  The stored forced-alignment line starts are the authoritative clock: keep image
    *index* semantic, rename each existing frame to that clock, then rebuild a gapless manifest.
    """
    out_dir = Path(out_dir)
    try:
        state = json.loads((out_dir / STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        state = {}
    lines = list(state.get("lines") or [])
    voice = voiceover_path_from_state(out_dir, state)
    if not lines:
        raise LongformError("Cannot retime: this project has no saved narration lines.")
    if not _audio_done(voice):
        raise LongformError("Cannot retime: the saved voiceover is missing.")

    audio_duration = audio_duration_seconds(voice)
    # A semantic range-retime may have redistributed ``line.start`` while keeping the
    # forced-alignment word clock intact.  For image cuts the first spoken word is the
    # authoritative boundary; restore it before calculating durations so a picture does
    # not appear during the preceding pause or sentence.
    restored = 0
    for line in lines:
        word_times = []
        for word in line.get("words") or []:
            try:
                word_times.append(float(word.get("s")))
            except (TypeError, ValueError, AttributeError):
                continue
        if word_times:
            spoken_start = round(min(word_times), 3)
            if abs(float(line.get("start") or 0.0) - spoken_start) > 0.001:
                restored += 1
            line["start"] = spoken_start
    for idx, line in enumerate(lines):
        next_start = (float(lines[idx + 1]["start"])
                      if idx + 1 < len(lines) else audio_duration)
        line["end"] = round(max(float(line["start"]) + 0.04, next_start), 3)
    durations = speech_cut_durations(lines, audio_duration)
    images_dir = out_dir / "images"
    staged = []
    renamed = 0
    if images_dir.is_dir():
        # Locate by index rather than timestamp: the timestamp in an old filename is precisely
        # what this repair replaces.  A two-phase move prevents collisions among adjacent files.
        for idx, (line, duration) in enumerate(zip(lines, durations)):
            destination = images_dir / f"{image_key(idx, line, duration)}.png"
            candidates = sorted(
                p for p in images_dir.glob(f"img{idx:03d}_*.png")
                if _image_done(p, "16:9") and p != destination
            )
            if destination.is_file() or not candidates:
                continue
            source = candidates[0]
            temporary = images_dir / f".speech_clock_{idx:03d}_{time.time_ns():x}.png"
            os.replace(source, temporary)
            staged.append((temporary, destination))
        for temporary, destination in staged:
            os.replace(temporary, destination)
            renamed += 1

    results = {}
    for idx, (line, duration) in enumerate(zip(lines, durations)):
        candidate = images_dir / f"{image_key(idx, line, duration)}.png"
        if _image_done(candidate, "16:9"):
            results[idx] = str(candidate)
    prompts = list(state.get("prompts") or [])
    if prompts:
        for idx, row in enumerate(prompts):
            if idx < len(lines) and isinstance(row, dict):
                row["timestamp"] = fmt_ts(lines[idx]["start"])
        write_prompts_file(prompts, out_dir / f"image_prompts_{out_dir.name}.txt")
    save_state(out_dir, lines=lines, audio_duration=round(audio_duration, 3), prompts=prompts,
               retime_range={"start": 0, "end": len(lines) - 1,
                             "matched": len(results), "method": "speech-clock"})
    write_transcript(lines, out_dir / "transcript.txt")
    write_timeline_manifest(lines, durations, results, audio_duration,
                            out_dir / "timeline.json",
                            voice_speed=float(state.get("voice_speed") or 1.0))
    _log(status_cb, f"Speech-clock retime complete: {len(results)} frame(s), {renamed} renamed, "
                    f"{restored} cut(s) restored from aligned word starts.")
    return {"lines": len(lines), "images": len(results), "renamed": renamed,
            "duration": round(audio_duration, 3), "timeline": str(out_dir / "timeline.json")}


def retime_existing_range(out_dir, start_idx, end_idx, replacement_lines=None,
                          status_cb=None):
    """Locally match existing images and retime one inclusive line range.

    The range keeps its original wall-clock span, while line holds are redistributed by
    spoken-word count.  Images are assigned by token overlap between their saved prompt/
    narration and the target line (with the original slot as a stable tie-breaker).  No
    image generation, network call, or Higgsfield session is involved; lines and files
    outside the range are left byte-for-byte untouched.
    """
    out_dir = Path(out_dir)
    state_path = out_dir / STATE_FILE
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise LongformError(f"Could not read longform state: {exc}") from exc
    lines = [dict(row) for row in (state.get("lines") or []) if isinstance(row, dict)]
    if not lines:
        raise LongformError("This project has no timed voiceover lines.")
    try:
        first, last = int(start_idx), int(end_idx)
    except (TypeError, ValueError) as exc:
        raise LongformError("Scene range must use numeric indexes.") from exc
    if first > last:
        first, last = last, first
    if first < 0 or last >= len(lines):
        raise LongformError(f"Scene range must be between 1 and {len(lines)}.")
    if last - first < 1:
        raise LongformError("Select at least two scenes to retime.")
    if not os.environ.get("WAVESPEED_API_KEY", "").strip():
        raise LongformError("Gemini retime requires WAVESPEED_API_KEY; no local fallback was run.")

    # Optional replacement lines are deliberately constrained to the selected range.  This
    # lets a local transcription/analysis caller provide refreshed voiceover text without
    # accidentally changing the rest of the project.
    incoming = replacement_lines if isinstance(replacement_lines, list) else []
    if incoming and len(incoming) != last - first + 1:
        raise LongformError("Replacement line count must match the selected scene range.")
    original = [dict(row) for row in lines]
    audio_duration = float(state.get("audio_duration") or 0.0)
    old_durations = line_durations(original, audio_duration)
    span_start = float(original[first].get("start") or 0.0)
    span_end = span_start + sum(old_durations[first:last + 1])
    if last + 1 < len(original):
        # The next line is outside the selection and is the authoritative right boundary.
        span_end = float(original[last + 1].get("start") or span_end)
    span = max(0.04 * (last - first + 1), span_end - span_start)

    def words(text):
        return set(re.findall(r"[a-z0-9']+", str(text or "").lower()))

    prompts = state.get("prompts") or []
    descriptors = []
    for idx in range(first, last + 1):
        prompt = prompts[idx] if idx < len(prompts) else {}
        prompt_text = prompt.get("prompt", "") if isinstance(prompt, dict) else prompt
        descriptors.append(str(prompt_text or "") + " " + str(original[idx].get("text") or ""))

    targets = []
    for offset, idx in enumerate(range(first, last + 1)):
        row = dict(incoming[offset]) if incoming else dict(original[idx])
        text = str(row.get("text") or row.get("script") or original[idx].get("text") or "")
        row["text"] = text
        targets.append(row)
    word_clocks = [
        [{"word": str(word.get("w") or ""), "time": float(word.get("s") or 0.0)}
         for word in (row.get("words") or [])]
        for row in targets
    ]
    weights = [max(1, len(words(row.get("text")))) for row in targets]
    minimum = 0.4
    if sum(weights) * minimum > span:
        durations = [span / len(weights)] * len(weights)
    else:
        extra = span - sum(weights) * minimum
        total = float(sum(weights))
        durations = [minimum + extra * weight / total for weight in weights]
    # Resolve current image paths by stable index, then assign them to target lines using a
    # deterministic local text match.  Missing images remain missing; they are never generated.
    image_dir = out_dir / "images"
    sources = []
    for idx in range(first, last + 1):
        path = image_dir / f"{image_key(idx, original[idx], old_durations[idx])}.png"
        if not _image_done(path, "16:9"):
            candidates = [p for p in image_dir.glob(f"img{idx:03d}_*.png") if _image_done(p, "16:9")]
            path = candidates[0] if candidates else None
        sources.append(path)
        if any(path is None for path in sources):
            raise LongformError("Gemini vision retime requires an existing image for every selected scene.")
    target_tokens = [words(row.get("text")) for row in targets]
    descriptor_tokens = [words(value) for value in descriptors]
    remaining = set(range(len(sources)))
    assignment = {}
    gemini_order = [None] * len(sources)
    gemini_switches = [None] * len(sources)
    batch_size = 10
    voiceover_path = voiceover_path_from_state(out_dir, state)
    voiceover_url = None
    if _audio_done(voiceover_path):
        _log(status_cb, "Uploading voiceover for Gemini retime...")
        upload_error = None
        for attempt in range(3):
            try:
                voiceover_url, _ = pipeline.upload_media(
                    voiceover_path, os.environ["WAVESPEED_API_KEY"])
                upload_error = None
                break
            except (OSError, urllib.error.URLError) as exc:
                upload_error = exc
                if attempt < 2:
                    time.sleep(2 ** attempt)
        if upload_error is not None:
            # Word clocks were aligned against this exact immutable voiceover. The media upload is
            # supplementary audio context, never a reason to prevent the vision retime from running.
            _log(status_cb, "Voiceover upload failed after 3 attempts; Gemini will use the exact "
                            "voiceover transcript and aligned word times instead.")
    for batch_start in range(0, len(sources), batch_size):
        batch_end = min(len(sources), batch_start + batch_size)
        try:
            batch_sheet = out_dir / "review" / f".retime_batch_{batch_start:04d}.jpg"
            batch_sheet = agent_core.create_media_contact_sheet(
                sources[batch_start:batch_end], batch_sheet,
                title=f"Existing visuals {batch_start + 1}-{batch_end}")
            if batch_sheet is None:
                raise ValueError("Could not create a vision contact sheet.")
            payload = {
                "model": "google/gemini-3.1-pro-preview",
                "messages": [
                    {"role": "system", "content":
                     "You are a vision editor. Ignore every filename, index, prompt, and other "
                     "metadata. Judge only the supplied image pixels and the voiceover. Return only "
                     "JSON with exactly one match for every target index and every source index. "
                     "Match each source image to one target and choose the exact absolute spoken "
                     "time of its visible caption using the supplied voiceover word times; do not "
                     "invent times. Schema: "
                     "{\"matches\":[{\"target\":0,\"source\":0,\"spoken_at\":12.34}]}."},
                    {"role": "user", "content": [
                        {"type": "text", "text": json.dumps({
                            "targets": [row.get("text", "") for row in targets[batch_start:batch_end]],
                            "word_times": word_clocks[batch_start:batch_end],
                            "instruction": "The contact sheet follows. Images are ordered left-to-right, "
                            "top-to-bottom. Ignore all labels and metadata; use only the pixels.",
                        }, ensure_ascii=False)}
                    ]},
                ],
                "temperature": 0.0,
                "max_tokens": 1200,
                "response_format": {"type": "json_object"},
            }
            content = payload["messages"][1]["content"]
            content.append({
                "type": "image_url",
                "image_url": {"url": agent_core.image_data_url(batch_sheet)},
            })
            if voiceover_url:
                content.append({
                    "type": "text",
                    "text": "Use this voiceover audio as the timing reference:",
                })
                content.append({
                    "type": "audio_url",
                    "audio_url": {"url": voiceover_url},
                })
            _log(status_cb, f"Calling Gemini 3.1 Pro for retime batch "
                            f"{batch_start + 1}-{batch_end}/{len(sources)}...")
            request_error = None
            for attempt in range(2):
                try:
                    response = agent_core.post_json_url(
                        agent_core.WAVESPEED_LLM_API, payload, timeout=120)
                    request_error = None
                    break
                except (OSError, urllib.error.URLError) as exc:
                    request_error = exc
                    if attempt < 2:
                        time.sleep(2 ** attempt)
            if request_error is not None:
                raise request_error
            parsed = agent_core.extract_json_object(
                response["choices"][0]["message"]["content"]) or {}
            matches = parsed.get("matches")
            if not isinstance(matches, list) or len(matches) != batch_end - batch_start:
                correction = dict(payload)
                correction["messages"] = list(payload["messages"]) + [{
                    "role": "user",
                    "content": (
                        f"Your last answer contained {len(matches) if isinstance(matches, list) else 0} "
                        f"matches. Return the complete JSON now: exactly {batch_end - batch_start} "
                        "matches, with target and source each containing every index exactly once."
                    ),
                }]
                response = agent_core.post_json_url(
                    agent_core.WAVESPEED_LLM_API, correction, timeout=120)
                parsed = agent_core.extract_json_object(
                    response["choices"][0]["message"]["content"]) or {}
                matches = parsed.get("matches")
                if not isinstance(matches, list) or len(matches) != batch_end - batch_start:
                    raise ValueError(
                        "Gemini returned an incomplete retime batch after correction.")
            local_targets = [int(item["target"]) for item in matches]
            local_sources = [int(item["source"]) for item in matches]
            if (sorted(local_targets) != list(range(batch_end - batch_start))
                    or sorted(local_sources) != list(range(batch_end - batch_start))):
                raise ValueError("Gemini returned a non-bijective retime batch.")
            for item in matches:
                target = batch_start + int(item["target"])
                gemini_order[target] = batch_start + int(item["source"])
                gemini_switches[target] = float(item["spoken_at"])
            _log(status_cb, f"Gemini 3.1 Pro retime batch "
                            f"{batch_start + 1}-{batch_end}/{len(sources)} complete.")
        except (KeyError, TypeError, ValueError, OSError) as exc:
            _log(status_cb, f"Gemini 3.1 Pro batch {batch_start + 1}-{batch_end} unavailable; "
                            f"retime aborted ({exc}).")
            raise LongformError(
                f"Gemini 3.1 Pro retime batch {batch_start + 1}-{batch_end} failed: {exc}"
            ) from exc
    if not all(value is not None for value in gemini_order + gemini_switches):
        gemini_order = None
        gemini_switches = None
    if gemini_switches:
        # Keep the selected range's outer boundary fixed while using Gemini's spoken caption
        # switches for every subsequent visual. The first selected image owns the incoming hold.
        switches = [span_start] + [
            max(span_start, min(span_end, value)) for value in gemini_switches[1:]
        ]
        switches = [max(switches[i], switches[i - 1] + 0.04)
                    for i in range(len(switches))]
        durations = [
            max(0.04, (switches[i + 1] if i + 1 < len(switches) else span_end) - switches[i])
            for i in range(len(switches))
        ]
    cursor = span_start
    for row, duration in zip(targets, durations):
        row["start"] = round(cursor, 3)
        row["end"] = round(cursor + duration, 3)
        cursor += duration
    targets[-1]["end"] = round(span_end, 3)
    for idx, row in zip(range(first, last + 1), targets):
        lines[idx] = row
    for target_pos, target_set in enumerate(target_tokens):
        best = None
        best_score = -1.0
        for source_pos in remaining:
            source_set = descriptor_tokens[source_pos]
            overlap = (len(target_set & source_set) / max(1, len(target_set | source_set))
                       if target_set and source_set else 0.0)
            score = overlap + (0.001 if source_pos == target_pos else 0.0)
            if gemini_order is not None:
                score += 1.0 if source_pos == gemini_order[target_pos] else 0.0
            if score > best_score:
                best, best_score = source_pos, score
        if best is not None:
            assignment[target_pos] = best
            remaining.remove(best)

    staged = []
    try:
        for target_pos, source_pos in assignment.items():
            source = sources[source_pos]
            if source is None:
                continue
            target_idx = first + target_pos
            destination = image_dir / f"{image_key(target_idx, lines[target_idx], line_durations(lines, audio_duration)[target_idx])}.png"
            temp = image_dir / f".retime_range_{time.time_ns()}_{target_pos}.png"
            os.replace(source, temp)
            staged.append((temp, destination))
        for temp, destination in staged:
            os.replace(temp, destination)
    except OSError as exc:
        for temp, _destination in staged:
            if temp.exists():
                try:
                    temp.unlink()
                except OSError:
                    pass
        raise LongformError(f"Could not retime existing images: {exc}") from exc

    state["lines"] = lines
    state["retime_range"] = {"start": first, "end": last, "matched": len(staged),
                             "method": "local-text-overlap"}
    save_state(out_dir, **state)
    write_transcript(lines, out_dir / "transcript.txt")
    current_durations = line_durations(lines, audio_duration)
    current_results = {}
    for idx, line in enumerate(lines):
        candidate = out_dir / "images" / f"{image_key(idx, line, current_durations[idx])}.png"
        if candidate.is_file():
            current_results[idx] = str(candidate)
    write_timeline_manifest(lines, current_durations, current_results, audio_duration,
                            out_dir / "timeline.json",
                            voice_speed=state.get("voice_speed") or 1.0)
    _log(status_cb, f"Retimed scenes {first + 1}-{last + 1}: matched {len(staged)} existing image(s).")
    return {"start": first, "end": last, "matched": len(staged),
            "lines": lines, "method": "local-text-overlap"}


def retime_longform_assets(out_dir, old_lines, new_lines, old_audio_duration,
                           new_audio_duration, prompts=None, status_cb=None):
    """Move already-generated images onto a changed narration clock.

    Images are semantic and positional: image N still illustrates script line N when only the
    voice speed changes.  Their timestamp/duration filenames, however, must follow the NEW audio
    clock or resume would regenerate them and assembly could place stale durations on the cut.
    Rename in two phases so two new names can never collide with an old one mid-migration.
    """
    old_lines = list(old_lines or [])
    new_lines = list(new_lines or [])
    if not old_lines or len(old_lines) != len(new_lines):
        return list(prompts or []) if prompts is not None else None
    old_durations = line_durations(old_lines, old_audio_duration)
    new_durations = line_durations(new_lines, new_audio_duration)
    images_dir = Path(out_dir) / "images"
    staged = []
    if images_dir.exists():
        for idx in range(len(new_lines)):
            old_path = images_dir / f"{image_key(idx, old_lines[idx], old_durations[idx])}.png"
            new_path = images_dir / f"{image_key(idx, new_lines[idx], new_durations[idx])}.png"
            if old_path == new_path or _image_done(new_path):
                continue
            source = old_path if _image_done(old_path) else None
            if source is None:
                candidates = [p for p in images_dir.glob(f"img{idx:03d}_*.png")
                              if _image_done(p) and p != new_path]
                if len(candidates) == 1:
                    source = candidates[0]
            if source is None:
                continue
            temp = images_dir / f".retime_{idx:03d}_{time.time_ns():x}.png"
            try:
                os.replace(source, temp)
                staged.append((temp, new_path))
            except OSError as exc:
                _log(status_cb, f"Could not stage image #{idx + 1} for retiming ({exc}).")
        moved = 0
        for temp, destination in staged:
            try:
                os.replace(temp, destination)
                moved += 1
            except OSError as exc:
                _log(status_cb, f"Could not rename {temp.name} to its new timing ({exc}).")
        if moved:
            _log(status_cb, f"Voice speed changed: renamed and retimed {moved} existing image(s).")

    if prompts is None:
        return None
    updated = []
    for idx, prompt in enumerate(prompts):
        row = dict(prompt) if isinstance(prompt, dict) else {"prompt": str(prompt or "")}
        if idx < len(new_lines):
            row["timestamp"] = fmt_ts(new_lines[idx]["start"])
        updated.append(row)
    return updated


def write_timeline_manifest(lines, durations, results, audio_duration, out_path,
                            voice_speed=1.0):
    """Persist the exact image placement used by assembly and later timeline/resume tooling."""
    out_path = Path(out_path)
    rows = []
    cursor = 0.0
    for idx, (line, duration) in enumerate(zip(lines, durations)):
        image = results.get(idx) if isinstance(results, dict) else None
        rows.append({
            "index": idx,
            "start": round(cursor, 3),
            "end": round(cursor + float(duration), 3),
            "duration": round(float(duration), 3),
            "image": Path(image).name if image else None,
            "text": str(line.get("text") or ""),
        })
        cursor += float(duration)
    payload = {"audio_duration": round(float(audio_duration or 0.0), 3),
               "voice_speed": round(float(voice_speed or 1.0), 3),
               "scenes": rows}
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, out_path)
    return out_path


def _p_image_one(index, prompt, dest, key, cancel_event=None, status_cb=None):
    """One Ideogram generation: submit, poll, download. Returns the path or None.

    Each call owns its own prediction id, so a slow generation can never deliver onto a
    later prompt's slot - the failure mode that made the browser route need an OCR audit.
    """
    payload = {
        "prompt": str(prompt or "").strip(),
        "aspect_ratio": IMAGE_ASPECT,
        "resolution": IMAGE_RESOLUTION,
        "thinking": IMAGE_THINKING,
        "output_format": IMAGE_OUTPUT_FORMAT,
    }
    response = pipeline.request_json("POST", f"{pipeline.API_BASE}/{P_IMAGE_MODEL}",
                                     key, payload, timeout=120)
    prediction_id = pipeline.unwrap_id(response)
    outputs, _ = pipeline.poll_wavespeed(prediction_id, key, timeout_s=IMAGE_TIMEOUT_S,
                                         interval_s=2, cancel_event=cancel_event,
                                         label=f"image #{index + 1}")
    if not outputs:
        return None
    pipeline.download_file(outputs[0], Path(dest))
    return str(dest)


def generate_images(prompts, lines, durations, out_dir, status_cb=None, cancel_event=None,
                    mascot=False):
    """Ideogram (P-Image) 16:9 frames over the WaveSpeed API.

    IMAGE_CONCURRENCY generations run in parallel; a failed one is retried up to
    IMAGE_RETRIES times. Images already on disk are reused, so a resumed run only pays for
    what is missing.

    Returns {index: path|None}."""
    import concurrent.futures
    key = os.environ.get("WAVESPEED_API_KEY", "")
    if not key:
        raise LongformError("WAVESPEED_API_KEY missing - the image stage needs it.")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    # The mascot travels as WORDS only (see MASCOT_RULE, applied in generate_image_prompts).
    # The old route could pin the actual artwork as a reference image; this endpoint takes
    # text alone, so Blob is only as consistent as its description.
    if mascot:
        _log(status_cb, "Mascot: described in the prompt text (this model takes no reference "
                        "image, so expect more drift than the pinned-artwork route).")

    cancel_check = (lambda: cancel_event is not None and cancel_event.is_set())
    total = len(prompts)
    results = {}
    attempts = {}
    # RESUME: the filename encodes index + timestamp + duration, so a reused file always
    # belongs to the line it is mapped onto; if the script or its timing changed, the key
    # changes and the frame is regenerated instead of silently mismatched.
    for idx in range(total):
        existing = out_dir / f"{image_key(idx, lines[idx], durations[idx])}.png"
        if _image_done(existing, IMAGE_ASPECT):
            results[idx] = str(existing)
    if results:
        _log(status_cb, f"Resume: {len(results)}/{total} image(s) already generated - "
                        f"only the missing {total - len(results)} will be generated.")
    queue = [i for i in range(total) if i not in results]
    dead = 0                            # failed attempts so far
    fresh_ok = 0                        # successes THIS session - resume pre-fills results,
    #                                     and judging the provider by yesterday's images
    #                                     would disable the dead-provider stop exactly when
    #                                     a key has expired
    done_count = {"n": sum(1 for v in results.values() if v)}

    while queue:
        if cancel_check():
            raise pipeline.PipelineCancelled("Cancelled.")
        _log(status_cb, f"Generating {len(queue)} image(s), up to {IMAGE_CONCURRENCY} at a time...")
        round_results = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=IMAGE_CONCURRENCY) as pool:
            futures = {}
            for idx in queue:
                dest = out_dir / f"{image_key(idx, lines[idx], durations[idx])}.png"
                futures[pool.submit(_p_image_one, idx, prompts[idx]["prompt"], dest, key,
                                    cancel_event=cancel_event, status_cb=status_cb)] = idx
            for fut in concurrent.futures.as_completed(futures):
                idx = futures[fut]
                try:
                    path = fut.result()
                except pipeline.PipelineCancelled:
                    raise
                except Exception as exc:  # noqa: BLE001
                    path = None
                    _log(status_cb, f"image #{idx + 1} failed: {str(exc)[:160]}")
                if path and _image_done(path, IMAGE_ASPECT):
                    round_results[idx] = path
                    fresh_ok += 1
                    done_count["n"] += 1
                    _log(status_cb, f"image {done_count['n']}/{total} - #{idx + 1} done")
                else:
                    dead += 1
        if cancel_check():
            raise pipeline.PipelineCancelled("Cancelled.")
        # cold start: nothing has ever worked this session = the provider or the key is gone
        if not fresh_ok and dead >= MAX_DEAD_ATTEMPTS_BEFORE_GIVING_UP:
            raise LongformError(
                f"The first {dead} image generations all failed - the image API looks "
                "unreachable. Stopping instead of filling the video with black frames; "
                "check WAVESPEED_API_KEY and resume (nothing is lost).")
        next_queue = []
        for idx in queue:
            path = round_results.get(idx)
            if path:
                results[idx] = path
            else:
                attempts[idx] = attempts.get(idx, 0) + 1
                if attempts[idx] <= IMAGE_RETRIES:
                    next_queue.append(idx)
                else:
                    results[idx] = None
                    _log(status_cb, f"image #{idx + 1} failed after {IMAGE_RETRIES} retries "
                                    "- the render will stay blocked until it is restored.")
        # mid-run death: a whole round produced nothing while retriable images remain
        if not round_results and next_queue and fresh_ok == 0:
            raise LongformError(
                "A full generation round produced no images - the image API looks down. "
                "Stopping; the finished images are kept.")
        if next_queue:
            _log(status_cb, f"Retrying {len(next_queue)} image(s) that failed this round...")
        queue = next_queue
    ok = sum(1 for v in results.values() if v)
    _log(status_cb, f"Images finished: {ok}/{total} generated.")
    return results


# ------------------------------------------------------------------ 4b) THUMBNAIL

_THUMB_STYLE_HEAD = ("Hand-drawn 2D doodle cartoon, flat colors, bold black outlines, slightly "
                     "imperfect sketchy marker lines, ")
_THUMB_STYLE_TAIL = (", bright white background, high contrast, no gradients, no shadows, no "
                     "textures, no photorealism, no 3D, no realistic faces, 16:9 aspect ratio, "
                     "eye-catching YouTube thumbnail doodle style.")


def _fallback_thumb_hook(script, lines):
    """A short, punchy ALL-CAPS thumbnail hook derived from the script when no model is used."""
    first = ""
    for l in (lines or []):
        t = str(l.get("text") or "").strip()
        if t:
            first = t
            break
    first = first or " ".join(str(script or "").split()[:8])
    words = [w for w in re.sub(r"[^A-Za-z0-9?' ]", " ", first).split() if w]
    hook = " ".join(words[:4]).upper().strip(" '")
    return (hook + ("?" if first.rstrip().endswith("?") else "")) or "WATCH THIS"


def build_thumbnail_prompt(script, lines, reasoning_model=None, status_cb=None):
    """Compose a click-optimised doodle-thumbnail prompt: an expressive stick figure + one bold
    visual + a HUGE ALL-CAPS hook. Uses the reasoning model for hook+subject when a key is
    present, else a deterministic fallback."""
    hook, subject = "", ("a stick figure with a hugely exaggerated shocked, wide-eyed curious "
                         "face, both hands raised")
    if os.environ.get("WAVESPEED_API_KEY"):
        try:
            data = agent_core.post_json_url(agent_core.WAVESPEED_LLM_API, {
                "model": str(reasoning_model or "anthropic/claude-opus-4.8"),
                "messages": [
                    {"role": "system", "content":
                        "You design a viral YouTube thumbnail for a doodle explainer video. "
                        "Reply STRICT JSON: {\"hook\": \"1-4 word ALL-CAPS curiosity hook\", "
                        "\"subject\": \"one short vivid visual of an expressive stick-figure "
                        "doodle scene, no text\"}. The hook creates a curiosity gap; never spoil "
                        "the answer."},
                    {"role": "user", "content": "SCRIPT:\n" + str(script or "")[:4000]}],
                "temperature": 0.7, "max_tokens": 200,
                "response_format": {"type": "json_object"},
            }, timeout=90)
            j = agent_core.extract_json_object(data["choices"][0]["message"]["content"]) or {}
            hook = str(j.get("hook") or "").strip().upper()
            subject = str(j.get("subject") or "").strip() or subject
        except Exception as exc:            # noqa: BLE001
            _log(status_cb, f"Thumbnail concept fell back to the script ({exc}).")
    hook = hook or _fallback_thumb_hook(script, lines)
    return (f"{_THUMB_STYLE_HEAD}{subject}, a big bold red circle or arrow highlighting the key "
            f"element, HUGE bold black ALL CAPS marker text filling the top of the frame reading "
            f"\"{hook}\"{_THUMB_STYLE_TAIL}"), hook


def build_thumbnail_concepts(script, lines, reasoning_model=None, status_cb=None):
    """Return three deliberately different thumbnail concepts, each with its matching title."""
    fallback_hook = _fallback_thumb_hook(script, lines)
    clean = re.sub(r"\s+", " ", str(script or "")).strip()
    fallback_title = (clean.split(".", 1)[0][:88].strip(" -:;,.") or "The Story You Never Knew")
    concepts = [
        {"hook": fallback_hook, "title": fallback_title,
         "subject": "one shocked stick figure discovering the central contradiction, extreme reaction"},
        {"hook": "HOW IS THIS REAL?", "title": f"The Strange Truth Behind {fallback_title}"[:96],
         "subject": "two stick figures on opposite sides of the central conflict, one clear visual contrast"},
        {"hook": "NOBODY EXPECTED THIS", "title": f"What Really Happened: {fallback_title}"[:96],
         "subject": "one dramatic oversized object from the story with a tiny worried stick figure beside it"},
    ]
    if os.environ.get("WAVESPEED_API_KEY"):
        try:
            data = agent_core.post_json_url(agent_core.WAVESPEED_LLM_API, {
                "model": str(reasoning_model or "anthropic/claude-opus-4.8"),
                "messages": [
                    {"role": "system", "content":
                        "Create exactly 3 DISTINCT viral YouTube thumbnail concepts for a doodle "
                        "explainer. Each concept needs a matching honest video title. The IMAGE is "
                        "minimal: one focal scene, at most two characters, one unanswered visual "
                        "question, no labels or written words. Vary the angle: (1) shock/reaction, "
                        "(2) conflict/contrast, (3) mystery/object. Reply STRICT JSON: "
                        "{\"variants\":[{\"hook\":\"short internal concept tag\",\"title\":\"specific "
                        "compelling video title\",\"subject\":\"one concise curiosity-driven visual "
                        "scene, no written text\"}, ...]}. Never spoil the answer."},
                    {"role": "user", "content": "SCRIPT:\n" + clean[:5000]}],
                "temperature": 0.9, "max_tokens": 650,
                "response_format": {"type": "json_object"},
            }, timeout=120)
            parsed = agent_core.extract_json_object(data["choices"][0]["message"]["content"]) or {}
            got = parsed.get("variants") or []
            if isinstance(got, list) and len(got) >= 3:
                normalized = []
                for item in got[:3]:
                    if not isinstance(item, dict):
                        break
                    hook = str(item.get("hook") or "").strip().upper()[:48]
                    title = str(item.get("title") or "").strip()[:110]
                    subject = str(item.get("subject") or "").strip()[:420]
                    if not hook or not title or not subject:
                        break
                    normalized.append({"hook": hook, "title": title, "subject": subject})
                if len(normalized) == 3:
                    concepts = normalized
        except Exception as exc:  # noqa: BLE001
            _log(status_cb, f"Thumbnail concepts fell back to the script ({exc}).")
    return concepts


def thumbnail_variants(out_dir):
    """Read healthy generated thumbnail variants and their paired titles."""
    out_dir = Path(out_dir)
    try:
        meta = json.loads((out_dir / "thumbnail_variants.json").read_text(encoding="utf-8"))
    except Exception:
        meta = {}
    try:
        selected = int(meta.get("selected") or 0) if isinstance(meta, dict) else 0
    except (TypeError, ValueError):
        selected = 0
    result = []
    for i, item in enumerate((meta.get("variants") or []) if isinstance(meta, dict) else []):
        path = out_dir / str(item.get("file") or "")
        if _image_done(path, "16:9"):
            result.append({"index": i, "file": str(path.relative_to(out_dir)).replace("\\", "/"),
                           "title": str(item.get("title") or ""),
                           "hook": str(item.get("hook") or ""), "selected": i == selected})
    return result


def _generate_wavespeed_thumbnail(prompt, out_path, key, cancel_event=None,
                                  status_cb=None, label="Thumbnail"):
    """Generate one 16:9 doodle thumbnail with GPT Image 2.0 through WaveSpeed."""
    if cancel_event is not None and cancel_event.is_set():
        raise pipeline.PipelineCancelled("Cancelled.")
    model = "openai/gpt-image-2/text-to-image"
    payload = {
        "aspect_ratio": "16:9",
        "enable_base64_output": False,
        "enable_sync_mode": False,
        "output_format": "png",
        "prompt": str(prompt),
        "quality": "medium",
        "resolution": "1k",
    }
    response = pipeline.request_json(
        "POST", f"{pipeline.API_BASE}/{model}", key, payload, timeout=240)
    prediction_id = pipeline.unwrap_id(response)
    outputs, _ = pipeline.poll_wavespeed(
        prediction_id, key, timeout_s=600, interval_s=3,
        cancel_event=cancel_event, status_cb=status_cb, label=label)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    pipeline.download_file(outputs[0], out_path)
    if not _image_done(out_path, "16:9"):
        raise LongformError(f"{label} returned an invalid image.")
    return str(out_path)


def generate_thumbnail(out_dir, script, lines, reasoning_model=None, status_cb=None,
                       cancel_event=None, force=False):
    """Generate exactly three GPT Image 2.0 thumbnail/title pairs through WaveSpeed."""
    out_dir = Path(out_dir)
    thumb = out_dir / "thumbnail.png"
    existing = thumbnail_variants(out_dir)
    if not force and len(existing) == 3 and _image_done(thumb, "16:9"):
        _log(status_cb, "Three thumbnail variants already exist - reusing them.")
        return str(thumb)
    if cancel_event is not None and cancel_event.is_set():
        return None
    key = pipeline.api_key()
    concepts = build_thumbnail_concepts(script, lines, reasoning_model=reasoning_model,
                                        status_cb=status_cb)
    variants_dir = out_dir / "thumbnails"
    variants_dir.mkdir(parents=True, exist_ok=True)
    if force and any(variants_dir.glob("thumbnail_*.png")):
        archive = variants_dir / ("archive_" + time.strftime("%Y%m%d_%H%M%S"))
        archive.mkdir(parents=True, exist_ok=True)
        for old in variants_dir.glob("thumbnail_*.png"):
            try:
                old.replace(archive / old.name)
            except OSError:
                pass
    jobs = []
    metadata = []
    for i, concept in enumerate(concepts[:3]):
        path = variants_dir / f"thumbnail_{i + 1}.png"
        prompt = (f"{_THUMB_STYLE_HEAD}{concept['subject']}. One dominant focal point, at most two "
                  "characters, one expressive face, one intriguing object or physical contrast, "
                  "strong readable silhouette and generous clean negative space. Create curiosity "
                  "without explaining the answer. NO text, NO letters, NO words, NO numbers, NO "
                  "labels, NO split panels, NO infographic layout, NO repeated annotations, NO "
                  f"circles or arrows.{_THUMB_STYLE_TAIL} Family-friendly, clean professional "
                  "YouTube thumbnail composition.")
        jobs.append((i, prompt, str(path)))
        metadata.append({"file": str(path.relative_to(out_dir)).replace("\\", "/"),
                         "title": concept["title"], "hook": concept["hook"]})
    # Persist the three titles BEFORE any paid image request. A restart/cancel after 1-2 images
    # must not erase the concepts or leave completed thumbnails with unknowable matching titles.
    metadata_path = out_dir / "thumbnail_variants.json"
    metadata_path.write_text(
        json.dumps({"selected": 0, "complete": False, "variants": metadata},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    _log(status_cb, "Generating 3 thumbnail variants with matching titles...")
    try:
        pending, res = list(jobs), {}
        for attempt in range(3):
            if not pending or (cancel_event and cancel_event.is_set()):
                break
            if attempt:
                _log(status_cb, f"Retrying {len(pending)} missing thumbnail variant(s) "
                                f"(attempt {attempt + 1}/3)...")
            batch = {}
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(3, len(pending))) as pool:
                futures = {
                    pool.submit(_generate_wavespeed_thumbnail, prompt, path, key,
                                cancel_event, status_cb, f"Thumbnail {i + 1}"): (i, path)
                    for i, prompt, path in pending
                }
                for future, (i, path) in futures.items():
                    try:
                        batch[i] = future.result()
                    except pipeline.PipelineCancelled:
                        raise
                    except Exception as exc:  # noqa: BLE001
                        _log(status_cb, f"Thumbnail {i + 1} failed ({exc}).")
                        batch[i] = None
            res.update(batch)
            pending = [(i, prompt, path) for i, prompt, path in pending
                       if not (_image_done(path, "16:9") and res.get(i))]
    except Exception as exc:                # noqa: BLE001
        _log(status_cb, f"Thumbnail generation failed ({exc}).")
        return None
    healthy = [i for i, _prompt, path in jobs if _image_done(path, "16:9") and res.get(i)]
    if len(healthy) != 3:
        _log(status_cb, f"Thumbnail set incomplete ({len(healthy)}/3); generate again from the editor.")
        return None
    metadata_path.write_text(
        json.dumps({"selected": 0, "complete": True, "variants": metadata},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    shutil.copy2(variants_dir / "thumbnail_1.png", thumb)
    save_state(out_dir, thumbnail_title=metadata[0]["title"], thumbnail_selected=0)
    _log(status_cb, "Three thumbnail variants saved. Variant 1 is selected for now.")
    return str(thumb)


# ------------------------------------------------------------------ 5) VERIFY + ASSEMBLE

def verify_images(lines, results, reasoning_model=None, status_cb=None):
    """Deterministic completeness check + (when a key is present) the reasoning model confirms
    the timestamp->image mapping before assembly."""
    missing = [i for i in range(len(lines))
               if not results.get(i) or not _image_done(results.get(i), "16:9")]
    _log(status_cb, f"Coverage check: {len(lines) - len(missing)}/{len(lines)} timestamps have "
                    f"an image" + (f"; missing: {[i + 1 for i in missing]}" if missing else "."))
    if os.environ.get("WAVESPEED_API_KEY"):
        try:
            listing = "\n".join(
                f"{fmt_ts(l['start'])} -> {Path(results[i]).name if results.get(i) else 'MISSING'}"
                for i, l in enumerate(lines))
            data = agent_core.post_json_url(agent_core.WAVESPEED_LLM_API, {
                "model": str(reasoning_model or "anthropic/claude-opus-4.8"),
                "messages": [
                    {"role": "system", "content":
                        "You verify a timestamp->image mapping for a video assembly. Reply with "
                        "STRICT JSON: {\"complete\": bool, \"missing_timestamps\": [..]}"},
                    {"role": "user", "content": listing}],
                "temperature": 0.0, "max_tokens": 400,
                "response_format": {"type": "json_object"},
            }, timeout=120)
            verdict = agent_core.extract_json_object(data["choices"][0]["message"]["content"]) or {}
            _log(status_cb, f"Reasoning check: complete={verdict.get('complete')} "
                            f"missing={verdict.get('missing_timestamps') or []}")
        except Exception as exc:  # noqa: BLE001
            _log(status_cb, f"Reasoning check skipped ({exc}).")
    return missing


def detect_black_segments(video_path, min_duration=0.15):
    """Inspect the encoded video itself; timeline/file coverage alone cannot prove it has pixels."""
    ffmpeg = pipeline.find_ffmpeg()
    if not ffmpeg or not Path(video_path).is_file():
        return []
    run = subprocess.run(
        [ffmpeg, "-hide_banner", "-v", "info", "-i", str(video_path),
         "-vf", f"blackdetect=d={float(min_duration):.3f}:pix_th=0.10:pic_th=0.98",
         "-an", "-f", "null", os.devnull],
        capture_output=True, text=True, timeout=3600, check=False)
    segments = []
    for match in re.finditer(
            r"black_start:([0-9.]+)\s+black_end:([0-9.]+)\s+black_duration:([0-9.]+)",
            (run.stderr or "")):
        segments.append({"start": round(float(match.group(1)), 3),
                         "end": round(float(match.group(2)), 3),
                         "duration": round(float(match.group(3)), 3)})
    return segments


def assemble_video(lines, durations, results, audio_path, out_path, status_cb=None):
    """Cut every image to its exact duration, refusing missing or encoded-black scenes."""
    ffmpeg = pipeline.find_ffmpeg()
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    missing = [i for i in range(len(lines))
               if not results.get(i) or not _image_done(results.get(i), "16:9")]
    if missing:
        preview = ", ".join(f"#{i + 1} {fmt_ts(lines[i]['start'])}" for i in missing[:12])
        more = f" (+{len(missing) - 12} more)" if len(missing) > 12 else ""
        raise LongformError(
            f"Refusing to render a video with {len(missing)} black/missing scene(s): "
            f"{preview}{more}. Restore or regenerate these images first.")
    work = out_path.parent / "_assembly"
    work.mkdir(exist_ok=True)
    black = work / "black.png"
    if not black.exists():
        r = subprocess.run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                            "-f", "lavfi", "-i", f"color=black:s={VIDEO_W}x{VIDEO_H}",
                            "-frames:v", "1", str(black)], capture_output=True, timeout=60)
        if not black.exists():
            raise LongformError("Could not create the black fallback frame.")
    lst = work / "concat.txt"
    # The concat demuxer silently DROPS frames when image dimensions change mid-stream
    # (decoder reinit) - a replaced/uploaded image with a different size made its scene
    # (and neighbours) vanish while the previous image kept showing. Normalize any
    # odd-sized image to the majority size once, into the assembly workspace.
    from PIL import Image as _PILImage
    sizes = {}
    for i in range(len(lines)):
        p = results.get(i)
        if not p:
            continue
        try:
            with _PILImage.open(p) as im:
                sizes[i] = im.size
        except Exception:
            sizes[i] = None
    counts = {}
    for s in sizes.values():
        if s:
            counts[s] = counts.get(s, 0) + 1
    base_size = max(counts, key=counts.get) if counts else (1280, 720)
    for i, s in sizes.items():
        if not s or s == base_size:
            continue
        src = Path(results[i])
        norm = work / f"norm_{i:03d}_{src.stem[:40]}.png"
        try:
            with _PILImage.open(src) as im:
                im = im.convert("RGB")
                ratio = min(base_size[0] / im.width, base_size[1] / im.height)
                nw, nh = max(1, round(im.width * ratio)), max(1, round(im.height * ratio))
                canvas = _PILImage.new("RGB", base_size, (0, 0, 0))
                canvas.paste(im.resize((nw, nh), _PILImage.LANCZOS),
                             ((base_size[0] - nw) // 2, (base_size[1] - nh) // 2))
                canvas.save(norm)
            results[i] = str(norm)
            _log(status_cb, f"Scene {i + 1}: normalized {s[0]}x{s[1]} image to "
                            f"{base_size[0]}x{base_size[1]} for a glitch-free concat.")
        except Exception as exc:  # noqa: BLE001 - keep the original rather than fail the render
            _log(status_cb, f"Scene {i + 1}: could not normalize image size ({exc}).")
    entries = []
    for i in range(len(lines)):
        img = results.get(i) or str(black)
        entries.append(f"file '{Path(img).resolve().as_posix()}'\nduration {durations[i]:.3f}\n")
    # concat demuxer needs the last file repeated (its duration otherwise ignored)
    last_img = results.get(len(lines) - 1) or str(black)
    entries.append(f"file '{Path(last_img).resolve().as_posix()}'\n")
    lst.write_text("".join(entries), encoding="utf-8")
    _log(status_cb, f"Assembling {len(lines)} scenes -> {out_path.name} ...")
    r = subprocess.run([
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "concat", "-safe", "0", "-i", str(lst),
        "-i", str(audio_path),
        "-vf", (f"scale={VIDEO_W}:{VIDEO_H}:force_original_aspect_ratio=decrease,"
                f"pad={VIDEO_W}:{VIDEO_H}:(ow-iw)/2:(oh-ih)/2:color=black,"
                f"fps={VIDEO_FPS},format=yuv420p"),
        "-c:v", "libx264", "-preset", "medium", "-crf", "19",
        "-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart",
        str(out_path)], capture_output=True, text=True, timeout=3600)
    if not out_path.exists() or out_path.stat().st_size < 10000:
        raise LongformError(f"Assembly failed: {(r.stderr or '')[-400:]}")
    black = detect_black_segments(out_path)
    if black:
        spans = ", ".join(f"{item['start']:.2f}-{item['end']:.2f}s" for item in black[:8])
        raise LongformError(
            f"Rendered video contains {len(black)} detected black-screen segment(s): {spans}. "
            "The file was kept for diagnosis but is not marked as a successful render.")
    _log(status_cb, f"Final longform video ready: {out_path}")
    return out_path


# ---------------------------------------------------------------- word helper
# Kept for caption_cut_starts, which now always falls back to the line start: with no words on
# the frames there is no caption phrase left to sync a cut to.
def _norm_words(text):
    return [w for w in re.sub(r"[^A-Z0-9 ]", " ", str(text or "").upper()).split() if len(w) >= 2]


def frames_from_disk(project_dir):
    """The frame list the post-run editor works on: for every timed line the expected image
    filename, whether it exists (missing = black frame in the video), timestamp, duration and
    the narration text. Returns (state, frames) or (None, [])."""
    project_dir = Path(project_dir)
    try:
        state = json.loads((project_dir / STATE_FILE).read_text(encoding="utf-8"))
    except Exception:
        return None, []
    lines = state.get("lines") or []
    if not lines:
        return state, []
    audio_duration = float(state.get("audio_duration") or 0.0)
    durations = saved_project_cut_durations(state, lines, audio_duration)
    frames = []
    cursor = 0.0
    for i, line in enumerate(lines):
        name = f"{image_key(i, line, durations[i])}.png"
        path = project_dir / "images" / name
        frames.append({
            "idx": i, "start": round(cursor, 3),
            "end": round(cursor + float(durations[i]), 3),
            "ts": fmt_ts(line["start"]), "dur": durations[i],
            "text": str(line.get("text") or ""), "file": name,
            "exists": _image_done(path, "16:9"),
        })
        cursor += float(durations[i])
    return state, frames


def reconcile_image_names(project_dir, status_cb=None):
    """Rename each index's image onto the filename the CURRENT line clock expects.

    Image filenames encode the on-screen timestamp+duration (image_key), so any change to the
    timings - a re-transcription after the voiceover was edited, a speed change - shifts every
    expected name and the untouched image files suddenly look "missing" (black frames), even
    though the right picture for index i is sitting right there under its old name. The index i
    is the stable key (same script -> same line order -> same img{i:03d}_ prefix), so we glob by
    index and rename the real file onto the expected name. Idempotent; safe to call before every
    rebuild."""
    project_dir = Path(project_dir)
    try:
        state = json.loads((project_dir / STATE_FILE).read_text(encoding="utf-8"))
    except Exception:
        return 0
    lines = state.get("lines") or []
    if not lines:
        return 0
    imgdir = project_dir / "images"
    if not imgdir.is_dir():
        return 0
    durations = saved_project_cut_durations(
        state, lines, float(state.get("audio_duration") or 0.0))
    renamed = 0
    for i, line in enumerate(lines):
        expected = imgdir / (image_key(i, line, durations[i]) + ".png")
        if expected.is_file() and expected.stat().st_size > 1024:
            continue                                     # already on the right name
        real = [c for c in sorted(imgdir.glob(f"img{i:03d}_*.png"))
                if c.stat().st_size > 1024]
        if not real:
            continue                                     # genuinely never generated
        expected.unlink(missing_ok=True)                 # a stale/tiny placeholder under this name
        real[0].rename(expected)
        renamed += 1
    if renamed:
        _log(status_cb, f"Reconciled {renamed} image filename(s) to the current timings.")
    return renamed


def recover_archived_images(project_dir, status_cb=None):
    """Fill empty current-timeline slots from suitable images anywhere in this project."""
    project_dir = Path(project_dir)
    try:
        state = json.loads((project_dir / STATE_FILE).read_text(encoding="utf-8"))
    except Exception:
        return 0
    lines, prompts = state.get("lines") or [], state.get("prompts") or []
    if not lines or len(prompts) != len(lines):
        return 0
    durations = saved_project_cut_durations(
        state, lines, float(state.get("audio_duration") or 0.0))
    image_dir = project_dir / "images"
    results = {}
    for idx, line in enumerate(lines):
        path = image_dir / f"{image_key(idx, line, durations[idx])}.png"
        if _image_done(path, "16:9"):
            results[idx] = str(path)
    before = len(results)
    if before == len(lines):
        return 0
    recovered = 0
    # Some archived frames were parked precisely because their captions/content belong elsewhere;
    # never force those back merely because the old index matches. For any slot still empty, hold
    # the nearest VALID neighbouring timeline image instead. A coherent extended shot is an honest
    # edit and far better than either unrelated art or a black frame. Copy rather than move so the
    # neighbour remains intact and every duration-encoded filename stays independently editable.
    stable_sources = dict(results)
    held = 0
    if stable_sources:
        for idx in range(len(lines)):
            if results.get(idx):
                continue
            source_idx = min(stable_sources, key=lambda candidate: abs(candidate - idx))
            source = Path(stable_sources[source_idx])
            target = image_dir / f"{image_key(idx, lines[idx], durations[idx])}.png"
            try:
                shutil.copy2(source, target)
            except OSError:
                continue
            if _image_done(target, "16:9"):
                results[idx] = str(target)
                held += 1
                _log(status_cb, f"Filled frame #{idx + 1} with a hold of nearby frame "
                                f"#{source_idx + 1} (no generation, no black screen).")
    return recovered + held


def rebuild_from_disk(project_dir, status_cb=None):
    """Re-assemble the longform MP4 from whatever images are on disk right now (the post-run
    frame editor's Rebuild). The voiceover and line clock come from state.json untouched; images
    the user replaced/moved are picked up by filename; a new VERSIONED mp4 is written so the
    previous render is never overwritten."""
    project_dir = Path(project_dir)
    # a re-timed voiceover shifts every duration-encoded image name; re-anchor by index first so
    # the untouched pictures are not mistaken for missing (black) frames.
    reconcile_image_names(project_dir, status_cb=status_cb)
    recover_archived_images(project_dir, status_cb=status_cb)
    state, frames = frames_from_disk(project_dir)
    if not state or not frames:
        raise LongformError("No resumable state in this project - nothing to rebuild.")
    lines = state["lines"]
    audio_duration = float(state.get("audio_duration") or 0.0)
    durations = line_durations(lines, audio_duration)
    voice_path = project_dir / Path(str(state.get("voiceover_file") or "voiceover.wav")).name
    if not _audio_done(voice_path):
        raise LongformError("The voiceover file is missing - cannot rebuild.")
    results = {}
    for f in frames:
        if f["exists"]:
            results[f["idx"]] = str(project_dir / "images" / f["file"])
    _log(status_cb, f"Rebuilding from disk: {len(results)}/{len(lines)} frames present.")
    # Preserve an explicit speech-clock repair rather than putting caption-triggered cuts back
    # during a rebuild. Older projects retain their existing caption-sync behaviour.
    durations = saved_project_cut_durations(state, lines, audio_duration)
    slug = project_dir.name
    out = project_dir / f"{slug}.mp4"
    n = 2
    while out.exists():
        out = project_dir / f"{slug}_v{n}.mp4"
        n += 1
    write_timeline_manifest(lines, durations, results, audio_duration,
                            project_dir / "timeline.json",
                            voice_speed=state.get("voice_speed") or 1.0)
    rendered = assemble_video(lines, durations, results, voice_path, out, status_cb=status_cb)
    save_state(project_dir, render_pending=False, last_video=Path(rendered).name)
    return rendered


# ------------------------------------------------------------------ ORCHESTRATOR

def run_longform_video(script, tts_model="pro", reasoning_model=None, reasoning_mode=None,
                       status_cb=None, cancel_event=None, speech_gate=None, resume=True,
                       voice=None, speaker=None, mix_gate=None, mascot=False,
                       halt_after_speech=False, tts_options=None):
    """The whole pipeline. Returns a result dict for the job UI.

    RESUME (default on): re-running the SAME script continues the existing project instead of
    starting over - the voiceover, timings and prompts are reloaded from state.json and only the
    images that are actually missing get generated. The state is keyed on the exact script, so
    editing the script starts a clean run (old images would otherwise land on shifted lines).
    """
    script = str(script or "").strip()
    if len(script) < 40:
        raise LongformError("Please paste the full script (at least a few sentences).")

    # The image stage needs the API key, and the check lives up front rather than inside
    # generate_images: a fresh run with no key used to pay for the TTS, the transcription and
    # the prompt calls before failing on a precondition. Now it stops in ~0s having spent nothing.
    if not os.environ.get("WAVESPEED_API_KEY"):
        raise LongformError("WAVESPEED_API_KEY is missing - the voiceover and the images both "
                            "need it.")

    slug = slug_for(script)
    out_dir = OUT_ROOT / slug
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "script.txt").write_text(script, encoding="utf-8")
    # Keep the creator choices with the longform project so reopening it restores the
    # same production setup instead of falling back to fresh defaults.
    saved_options = dict(tts_options or {})
    save_state(
        out_dir,
        script=script,
        tts_model=str(tts_model or "pro"),
        voice=str(voice or ""),
        reasoning_model=str(reasoning_model or ""),
        mascot_enabled=bool(mascot),
        halt_after_speech=bool(halt_after_speech),
        reasoning_mode=str(reasoning_mode or ""),
        tts_voice_instruction=str(saved_options.get("voice_instruction") or ""),
        tts_language=str(saved_options.get("language") or ""),
        tts_native_speed=saved_options.get("speed", 1.0),
        tts_volume=saved_options.get("volume", 1.0),
        tts_pitch=saved_options.get("pitch", 0),
        tts_sample_rate=saved_options.get("sample_rate", 24000),
        tts_output_format=str(saved_options.get("output_format") or "mp3"),
    )

    state = load_state(out_dir, script) if resume else None
    raw_voice_path = out_dir / "voiceover.wav"
    voice_path = voiceover_path_from_state(out_dir, state)
    ffprobe = pipeline.find_ffprobe(pipeline.find_ffmpeg())

    def _probe_duration(path):
        try:
            out = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration",
                                  "-of", "default=nokey=1:noprint_wrappers=1", str(path)],
                                 capture_output=True, text=True, timeout=30)
            return float((out.stdout or "0").strip() or 0.0)
        except Exception:
            return 0.0

    # A different narrator means the saved voiceover is the wrong voice -> regenerate it (and with
    # it the timings), otherwise resume would silently keep the old narrator forever.
    if state and voice and str(state.get("voice") or "") != str(voice):
        _log(status_cb, "Narrator changed - regenerating the voiceover (timings will be redone).")
        state = None
    # The delivery directive is as much part of the performance as the narrator: a voiceover
    # stitched under a different directive is the wrong read, and keeping it would silently undo
    # an edit to TTS_STYLE_LONGFORM forever. (generate_voiceover guards its own reuse the same
    # way; this closes the outer path that skips generate_voiceover entirely.)
    if state and str(state.get("tts_style") or "") != str(pipeline.TTS_STYLE_LONGFORM or ""):
        _log(status_cb, "Delivery directive changed - regenerating the voiceover "
                        "(timings will be redone).")
        state = None
    if state is None:
        voice_path = raw_voice_path

    # If only the derived file vanished, recreate it from the untouched paid TTS. The existing
    # line clock remains valid because the same recorded speed is applied again.
    recorded_speed = float((state or {}).get("voice_speed") or 1.0)
    recorded_name = Path(str((state or {}).get("voiceover_file") or "voiceover.wav")).name
    recorded_path = out_dir / recorded_name
    if (state and abs(recorded_speed - 1.0) >= 0.01 and not _audio_done(recorded_path)
            and _audio_done(raw_voice_path)):
        voice_path = apply_voice_speed(raw_voice_path, recorded_speed, status_cb=status_cb)
        save_state(out_dir, voiceover_file=voice_path.name)

    # Older/interrupted states can already contain timed images while the speed decision is still
    # pending. Do not silently reuse that old clock: ask for speed, transcribe the resulting audio,
    # then rename the same semantic images onto the new clock below.
    explicit_retime = bool(state and state.get("retime_source_lines"))
    speed_retime = bool(state and state.get("lines") and mix_gate is not None
                        and state.get("voice_speed") is None and _audio_done(raw_voice_path))
    old_lines = (list((state or {}).get("retime_source_lines") or []) if explicit_retime
                 else list((state or {}).get("lines") or []) if speed_retime else [])
    old_prompts = (list((state or {}).get("retime_source_prompts") or []) if explicit_retime
                   else list((state or {}).get("prompts") or []) if speed_retime else None)
    old_audio_duration = (float((state or {}).get("retime_source_audio_duration") or 0.0)
                          if explicit_retime else float((state or {}).get("audio_duration") or 0.0))
    reusable = bool(state and state.get("lines") and _audio_done(voice_path) and not speed_retime)
    retimed_prompts = None
    if reusable:
        # Reusing the voiceover is what makes resume work at all: fresh TTS would shift every
        # timestamp, which changes every image key and would orphan the images already generated.
        tts_parts = int(state.get("tts_parts") or 0)
        audio_duration = float(state.get("audio_duration") or 0.0) or _probe_duration(voice_path)
        lines = state["lines"]
        _log(status_cb, f"Resume: reusing the existing voiceover + {len(lines)} timed line(s) "
                        "(no TTS, no transcription re-run).")
    else:
        voice_path, tts_parts = generate_voiceover(script, out_dir, tts_model=tts_model,
                                                   status_cb=status_cb, cancel_event=cancel_event,
                                                   speech_gate=speech_gate, voice=voice,
                                                   speaker=speaker, resume=resume,
                                                   mix_gate=mix_gate, tts_options=tts_options)
        audio_duration = _probe_duration(voice_path)
        lines = transcribe_lines(script, voice_path, status_cb=status_cb)
        if (speed_retime or explicit_retime) and len(old_lines) == len(lines):
            retimed_prompts = retime_longform_assets(
                out_dir, old_lines, lines, old_audio_duration, audio_duration,
                prompts=old_prompts, status_cb=status_cb)
        elif explicit_retime:
            _log(status_cb, "The regenerated speech produced a different line count; existing "
                            "images cannot be mapped safely and will be regenerated.")
        save_state(out_dir, script=script, lines=lines, tts_parts=tts_parts, voice=voice or "",
                   prompts=retimed_prompts,
                   audio_duration=round(audio_duration, 3),
                   retime_source_lines=None, retime_source_prompts=None,
                   retime_source_audio_duration=0.0)
    transcript_path = write_transcript(lines, out_dir / "transcript.txt")
    _log(status_cb, f"Transcript written: {transcript_path.name}")

    prompts = (state or {}).get("prompts") if reusable else retimed_prompts
    # Retiming rewrites timestamps but not prompt semantics/format. Preserve the saved format
    # marker or the normal stale-format guard would archive every reusable image immediately
    # after a speech-only regeneration.
    cached_fmt = ((state or {}).get("prompt_format")
                  if (reusable or retimed_prompts is not None) else None)
    prompt_alignment_bad = bool(prompts) and (
        len(prompts) != len(lines) or any(
            not isinstance(prompt, dict)
            or prompt.get("timestamp") != fmt_ts(lines[i]["start"])
            for i, prompt in enumerate(prompts[:len(lines)])
        )
    )
    stale_format = bool(prompts) and cached_fmt != PROMPT_FORMAT_VERSION
    if prompts and len(prompts) == len(lines) and not stale_format and not prompt_alignment_bad:
        _log(status_cb, f"Resume: reusing the {len(prompts)} saved image prompt(s).")
    else:
        if stale_format:
            # The saved prompts predate the current doodle-prompt format (e.g. before the
            # mandatory ALL-CAPS top caption). Regenerate ALL prompts, and move the images made
            # from the old prompts aside so they are regenerated in the new style too.
            _log(status_cb, "Image prompts are an older format - regenerating all prompts in the "
                            "new caption style (and the images made from them).")
            _archive_old_images(out_dir / "images", status_cb)
        elif prompt_alignment_bad:
            _log(status_cb, "Saved image prompts are not aligned to the current transcript - "
                            "regenerating prompts and their images.")
            first_bad = next(
                (i for i, prompt in enumerate(prompts[:len(lines)])
                 if not isinstance(prompt, dict)
                 or prompt.get("timestamp") != fmt_ts(lines[i]["start"])),
                min(len(prompts), len(lines)),
            )
            _archive_images_from_index(out_dir / "images", first_bad, status_cb)
        prompt_checkpoint = out_dir / "image_prompts_checkpoint.json"
        prompts = generate_image_prompts(lines, reasoning_model=reasoning_model,
                                         status_cb=status_cb, cancel_event=cancel_event,
                                         checkpoint_path=prompt_checkpoint, mascot=mascot)
        save_state(out_dir, script=script, lines=lines, prompts=prompts, tts_parts=tts_parts,
                   voice=voice or "", audio_duration=round(audio_duration, 3),
                   prompt_format=PROMPT_FORMAT_VERSION)
        prompt_checkpoint.unlink(missing_ok=True)
    prompts_path = write_prompts_file(prompts, out_dir / f"image_prompts_{slug}.txt")
    _log(status_cb, f"Prompt file written: {prompts_path.name}")

    durations = line_durations(lines, audio_duration)
    results = generate_images(prompts, lines, durations, out_dir / "images",
                              status_cb=status_cb, cancel_event=cancel_event, mascot=mascot)

    # Thumbnail generation is user-triggered from the pre-render editor. It deliberately is not
    # hidden inside the already long image run: the editor always exposes Generate thumbnails,
    # and one click produces three title-paired options. Reuse a prior selection on resume.
    thumbnail_path = out_dir / "thumbnail.png"
    thumbnail = str(thumbnail_path) if _image_done(thumbnail_path, "16:9") else ""

    missing = verify_images(lines, results, reasoning_model=reasoning_model, status_cb=status_cb)
    latest_state = load_state(out_dir, script) or {}
    # Assembly cuts follow the locally forced-aligned word containing each image's caption phrase,
    # keeping the visual and its baked-in caption synchronized with the narration.
    cut_durations = caption_cut_durations(lines, prompts, audio_duration)
    timeline_path = write_timeline_manifest(
        lines, cut_durations, results, audio_duration, out_dir / "timeline.json",
        voice_speed=latest_state.get("voice_speed") or 1.0)
    # The first assembly is intentionally deferred. The creator must see and approve the real
    # image/voiceover timeline first; rendering here used to lock mistakes into a slow MP4 before
    # the user had any chance to move frames or recover unused generations.
    save_state(out_dir, render_pending=True, reasoning_model=str(reasoning_model or ""))
    return {
        "project_dir": str(out_dir), "voiceover": str(voice_path),
        "transcript": str(transcript_path), "prompts_file": str(prompts_path),
        "timeline": str(timeline_path),
        "images_done": sum(1 for v in results.values() if v), "images_total": len(lines),
        "missing_images": [fmt_ts(lines[i]["start"]) for i in missing],
        "tts_parts": tts_parts, "audio_duration": round(audio_duration, 2),
        "thumbnail": thumbnail or "", "render_pending": True,
        "open_longform_editor": True,
    }
