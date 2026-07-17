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
  5. ASSEMBLY   - the reasoning model verifies every timestamp has an image; the video is cut
                  image-by-image to the exact per-line duration (black frame where an image is
                  missing) and muxed with the voiceover. The app's done-notification chimes.
"""

import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path

import agent_core
import pipeline

ROOT = Path(__file__).resolve().parent
OUT_ROOT = agent_core.PROJECTS_DIR / "_longform"

TTS_PART_CHAR_LIMIT = 2200          # sentence-safe chunking limit per TTS call
# 1, deliberately, until higgsfield_login runs more than one real worker: its executor is
# max_workers=1, so extra "slots" only queue - but generate_sync's wait clock starts at SUBMIT,
# so with 4 in flight the 4th call times out whenever one image takes >~97s (390s/4), gets
# counted as a failure and re-queued while its orphaned worker task generates on regardless.
# The scheduler below keeps full slot semantics; raise this once the session parallelizes.
IMAGE_CONCURRENCY = 1               # max Higgsfield generations in flight
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
                       speech_gate=None, voice=None, speaker=None, resume=True, mix_gate=None):
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
    if voice:
        tts_kw["voice"] = voice
    if speaker:
        tts_kw["speaker"] = speaker

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
        save_state(out_dir, script=script, voice=voice or "", tts_style=tts_kw["style"] or "",
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
        if existing and _audio_done(existing):
            _log(status_cb, f"Resume: reusing voiceover part {i + 1}/{len(parts)} "
                            f"({Path(existing).name}) - no new TTS.")
            part_files.append(Path(existing))
            continue
        _log(status_cb, f"Voiceover part {i + 1}/{len(parts)} ({len(part)} chars) "
                        f"with Gemini 2.5 {'Pro' if tts_model == 'pro' else 'Flash'} TTS"
                        f"{(' - narrator ' + str(voice)) if voice else ''}...")
        p = pipeline.generate_speech_gemini(part, out_dir / f"vo_part{i:02d}.wav",
                                            model=tts_model, cancel_event=cancel_event,
                                            status_cb=status_cb, **tts_kw)
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
    for p in part_files:
        try:
            Path(p).unlink(missing_ok=True)
        except Exception:
            pass
    # the parts are gone now and voiceover.wav carries the resume from here on
    save_state(out_dir, tts_part_files=[], voiceover_ready=True,
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
                    "text": ln})
    # audio end = real duration (last line holds to the end during assembly)
    _log(status_cb, f"Transcript: {len(out)} timestamped line(s).")
    return out


def write_transcript(lines, out_path):
    out_path = Path(out_path)
    out_path.write_text("\n".join(f"{fmt_ts(l['start'])} {l['text']}" for l in lines) + "\n",
                        encoding="utf-8")
    return out_path


# ------------------------------------------------------------------ 3) IMAGE PROMPTS

# Bump when the STAGE-3 doodle-prompt FORMAT changes (e.g. the mandatory ALL-CAPS top caption).
# A resumed project whose saved prompts predate this version regenerates ALL prompts - and the
# images made from them - in the new style instead of reusing the old-format cache.
PROMPT_FORMAT_VERSION = 2

STAGE3_PROMPT = """## STAGE 3 - GENERATE IMAGE PROMPTS FOR EVERY TIMESTAMP

Once the user pastes their timestamped script, generate one detailed text-to-image prompt for every single timestamp line.

**IMAGE PROMPT RULES:**

1. Every prompt must begin with its timestamp COPIED EXACTLY as it appears in the script (e.g. `[0:03.4]`) - do not reformat or round timestamps
2. Every prompt must open with the style anchor: "Hand-drawn 2D doodle cartoon animation, flat colors, bold black outlines, slightly imperfect sketchy marker lines,"
3. Every prompt must end with the style lock: "no gradients, no shadows, no textures, no photorealism, no 3D, no realistic faces, no anime style, 16:9 aspect ratio, educational YouTube explainer doodle style."
4. Be specific inside each prompt - describe what characters are present and what they are doing, their exact expression, what objects are in the scene, what background color is used
5. MANDATORY on-screen caption: every prompt MUST include a bold black ALL CAPS marker text at the top of the frame reading a short punchy 1-4 word caption that captures the essence of that line - phrase it exactly as: `bold black ALL CAPS marker text at the top reading "CHEERS"`. Pick the caption from the narration of that line (a key word, reaction, or label), like the on-screen words in a viral doodle explainer (CHEERS, WAR!, SORRY!, MOST COUNTRIES, 2 KM UNNOTICED, MILLIONS OF YEARS). Keep captions varied and specific to each line; hold the same caption only while the same beat is held across consecutive timestamps.
6. Translate abstract narration into concrete visuals - if the script says "your body doesn't know the difference", show a confused stick figure looking at two identical objects; if it says "millions of years", show a large hourglass with the top caption reading "MILLIONS OF YEARS"

GOLD-STANDARD EXAMPLE (match this exact shape - style anchor, then a rich concrete scene, then the mandatory top caption, then background, then the style lock):
`Hand-drawn 2D doodle cartoon animation, flat colors, bold black outlines, slightly imperfect sketchy marker lines, a Swiss-helmeted stick figure and a crowned stick figure happily clinking two frothy beer mugs together with big warm smiles, a red heart above them and a small white dove of neutrality flying overhead, bold black ALL CAPS marker text at the top reading "CHEERS", plain white background, no gradients, no shadows, no textures, no photorealism, no 3D, no realistic faces, no anime style, 16:9 aspect ratio, educational YouTube explainer doodle style.`
7. Match tone to background color:
   - Ancient / prehistoric -> tan or dark blue background
   - Danger / threat -> stark white with red text or red-tinted sky
   - Happy / triumph / discovery -> bright white or yellow background
   - Underwater / science -> solid blue background
   - Outdoor / nature / evolution -> flat green ground + blue sky
   - Fire / night / ancient ritual -> solid orange background
8. Hold scenes across consecutive timestamps - if 3 lines describe the same moment, keep the same scene and only adjust the character's expression or add one new element. Do not generate a brand new scene every 5 seconds.
9. Use these proven frame types when appropriate:
   - **Concept text frame:** Large object (hourglass, clock, skull) centered + bold ALL CAPS text at top
   - **Evolution sequence:** Left-to-right creature or human progression with a right-pointing arrow
   - **Labeled diagram:** Animal or object with a yellow diagonal arrow + ALL CAPS label word
   - **Stick figure reaction:** Thought bubble above head with "?", "HMMMM", "!", or "WAIT..."
   - **Villain personified:** An abstract concept given an angry cartoon face (sun with knife, brain with boxing gloves)
   - **Globe + creatures:** Earth globe centered, surrounded by floating cartoon animals or objects

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

Always include in every prompt: no photorealism, no 3D render, no gradients, no drop shadows, no textures, no realistic faces, no anime style."""

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


def generate_image_prompts(lines, reasoning_model=None, status_cb=None, cancel_event=None):
    """Transcript lines -> one doodle prompt per line, via the STAGE-3 conversation.
    The app itself replies "next" until every timestamp is covered. Mapping is POSITIONAL
    (prompt N belongs to line N) with a timestamp sanity check. Raises on shortfall."""
    if not os.environ.get("WAVESPEED_API_KEY"):
        raise LongformError("WAVESPEED_API_KEY missing - image prompts need the reasoning model.")
    model = str(reasoning_model or "anthropic/claude-opus-4.8")
    transcript = "\n".join(f"{fmt_ts(l['start'])} {l['text']}" for l in lines)
    messages = [{"role": "system", "content": STAGE3_PROMPT},
                {"role": "user", "content": transcript}]
    prompts = []
    max_rounds = (len(lines) // 20) + 4
    for round_no in range(max_rounds):
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
            raise LongformError(f"Prompt batch {round_no + 1} contained no parseable prompts.")
        prompts.extend(batch)
        messages.append({"role": "assistant", "content": reply})
        if len(prompts) >= len(lines) or "all image prompts are now delivered" in reply.lower():
            break
        messages.append({"role": "user", "content": "next"})
    if len(prompts) < len(lines):
        raise LongformError(f"Only {len(prompts)}/{len(lines)} image prompts were generated.")
    prompts = prompts[:len(lines)]
    mismatch = sum(1 for l, p in zip(lines, prompts) if p["timestamp"] != fmt_ts(l["start"]))
    if mismatch:
        _log(status_cb, f"Note: {mismatch} prompt timestamp(s) differ from the transcript - "
                        "using positional order (prompt N = line N).")
    _log(status_cb, f"All {len(prompts)} image prompts delivered.")
    return prompts


def write_prompts_file(prompts, out_path):
    out_path = Path(out_path)
    out_path.write_text("\n\n".join(f"{p['timestamp']} {p['prompt']}" for p in prompts) + "\n",
                        encoding="utf-8")
    return out_path


# ------------------------------------------------------------------ 4) IMAGES (Higgsfield)

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


def line_durations(lines, audio_duration):
    """Per-line on-screen duration: to the next line's start; last line holds to audio end."""
    durs = []
    for i, l in enumerate(lines):
        if i + 1 < len(lines):
            durs.append(max(0.4, round(lines[i + 1]["start"] - l["start"], 2)))
        else:
            durs.append(max(0.4, round(max(audio_duration, l["end"]) - l["start"], 2)))
    return durs


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
    for idx, (line, duration) in enumerate(zip(lines, durations)):
        image = results.get(idx) if isinstance(results, dict) else None
        rows.append({
            "index": idx,
            "start": round(float(line["start"]), 3),
            "end": round(float(line["start"]) + float(duration), 3),
            "duration": round(float(duration), 3),
            "image": Path(image).name if image else None,
            "text": str(line.get("text") or ""),
        })
    payload = {"audio_duration": round(float(audio_duration or 0.0), 3),
               "voice_speed": round(float(voice_speed or 1.0), 3),
               "scenes": rows}
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, out_path)
    return out_path


def generate_images(prompts, lines, durations, out_dir, status_cb=None, cancel_event=None):
    """FLUX.2 Pro (unlimited) 16:9 on the user's Higgsfield session.

    Slot scheduler: up to IMAGE_CONCURRENCY prompts in flight; a new prompt is only submitted
    once active generations drop below the limit; failed generations are re-queued up to
    IMAGE_RETRIES times. NOTE: higgsfield_login currently runs ONE Playwright session on one
    worker thread, so in-flight submissions serialize there today - the scheduler semantics
    (slots, refill, retry, naming) are exactly as specified and parallelize automatically once
    the session supports queued submissions.

    Returns {index: path|None}."""
    import concurrent.futures
    import higgsfield_login
    if not higgsfield_login.is_ready():
        raise LongformError("Higgsfield is not connected - click Connect Higgsfield first.")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # MANUAL, single-reused-page generation. Higgsfield's DataDome bot-check throws a CAPTCHA on
    # an automated Generate click, and the Unlimited switch resets on every fresh page load. Both
    # are solved by opening ONE visible page and letting the USER turn Unlimited on (and clear any
    # verification) once - every frame is then generated on that same page, so Unlimited and the
    # DataDome trust cookie persist. We never touch the CAPTCHA ourselves.
    cancel_check = (lambda: cancel_event is not None and cancel_event.is_set())
    _log(status_cb, "Opening the Higgsfield window - please turn ON the 'Unlimited' switch (and "
                    "complete any quick verification). Generation then starts automatically.")
    if not higgsfield_login.begin_manual_session(model="FLUX.2 Pro", aspect="16:9",
                                                 status_cb=status_cb, cancel_check=cancel_check,
                                                 timeout_s=1800):
        if cancel_check():
            raise pipeline.PipelineCancelled("Cancelled.")
        raise LongformError("Higgsfield: the Unlimited switch was not turned on in time. Turn it "
                            "on in the Higgsfield window, then resume (nothing is lost).")

    def _generate_one(prompt, path):
        """Generate one image on the shared page, pausing for the user if Unlimited flips off or a
        DataDome CAPTCHA appears (never spending on an unverified toggle). Returns path or None."""
        for _ in range(4):
            if cancel_check():
                raise pipeline.PipelineCancelled("Cancelled.")
            res = higgsfield_login.generate_shared_sync(prompt, path, timeout_s=300,
                                                        status_cb=status_cb)
            if res in ("UNLIMITED_OFF", "CAPTCHA"):
                _log(status_cb, "Paused - re-enable Unlimited / finish the verification in the "
                                "Higgsfield window; generation resumes automatically.")
                if not higgsfield_login.wait_for_user_unlimited_sync(
                        status_cb=status_cb, cancel_check=cancel_check, timeout_s=1800):
                    if cancel_check():
                        raise pipeline.PipelineCancelled("Cancelled.")
                    return None
                continue
            return res
        return None

    # character reference FIRST (consistency anchor; also proves the session works)
    ref_path = out_dir / "character_reference.png"
    if _image_done(ref_path, "16:9"):
        _log(status_cb, "Character reference already there - reusing it.")
    else:
        _log(status_cb, "Generating the character reference frame first...")
        ref = _generate_one(CHARACTER_REFERENCE_PROMPT, ref_path)
        if ref and _image_done(ref_path, "16:9"):
            _log(status_cb, "Character reference saved (used as the style anchor).")
        else:
            _log(status_cb, "Character reference failed - continuing without it.")

    total = len(prompts)
    results = {}
    attempts = {}
    # RESUME: every image whose file is already on disk is reused, so a re-run only generates
    # what is actually missing. The filename (index + timestamp + duration) identifies the line,
    # so a reused file always belongs to the line it is mapped onto - if the script or its timing
    # changed, the key changes and the image is regenerated instead of silently mismatched.
    for idx in range(total):
        existing = out_dir / f"{image_key(idx, lines[idx], durations[idx])}.png"
        if _image_done(existing, "16:9"):
            results[idx] = str(existing)
    if results:
        _log(status_cb, f"Resume: {len(results)}/{total} image(s) already generated - "
                        f"only the missing {total - len(results)} will be generated.")
    queue = [i for i in range(total) if i not in results]
    dead = 0                            # failed generation attempts so far
    fresh_ok = 0                        # successes THIS session - resume pre-fills results, and
    #                                     judging the provider by yesterday's images would disable
    #                                     the dead-provider stop exactly when a login has expired
    consec = 0                          # failures since the last success (mid-run death signal)

    # One reused page = sequential generation (Unlimited + the trust cookie only persist on that
    # single page). The circuit breakers below are unchanged.
    while queue:
        if cancel_check():
            raise pipeline.PipelineCancelled("Cancelled.")
        idx = queue.pop(0)
        key = image_key(idx, lines[idx], durations[idx])
        _log(status_cb, f"image {sum(1 for v in results.values() if v)}/{total} - "
                        f"generating #{idx + 1}")
        path = _generate_one(prompts[idx]["prompt"], out_dir / f"{key}.png")
        if path and _image_done(path, "16:9"):
            results[idx] = str(path)
            fresh_ok += 1
            consec = 0
            _log(status_cb, f"image {sum(1 for v in results.values() if v)}/{total} - "
                            f"#{idx + 1} done")
        else:
            if path:
                _log(status_cb, f"image #{idx + 1} was not a valid 16:9 frame - rejecting it")
            dead += 1
            # "No image has EVER worked this session" is what makes the cold-start stop safe: a run
            # that is producing images can never trip it. Resume pre-fills yesterday's images into
            # results, which say nothing about whether the login is still alive today.
            if dead >= MAX_DEAD_ATTEMPTS_BEFORE_GIVING_UP and not fresh_ok:
                raise LongformError(
                    f"The first {dead} image generations all failed - Higgsfield looks "
                    "down or logged out. Stopping instead of spending hours filling the "
                    "video with black frames; reconnect and resume (nothing is lost).")
            consec += 1
            # Mid-run death (blind once anything succeeded): retries go to the BACK of the queue,
            # so a failure streak is interleaved with fresh successes unless the provider actually
            # stopped. Only trip while UNTRIED images remain - at the tail the queue holds nothing
            # but retries of a few hopeless prompts, which should become black frames as designed.
            if (consec >= MAX_CONSECUTIVE_FAILURES_MIDRUN
                    and any(attempts.get(i, 0) == 0 for i in queue)):
                raise LongformError(
                    f"{consec} generations in a row have failed with untried images "
                    "still queued - Higgsfield looks like it died mid-run. Stopping; "
                    "reconnect and resume (the finished images are kept).")
            attempts[idx] = attempts.get(idx, 0) + 1
            if attempts[idx] <= IMAGE_RETRIES:
                _log(status_cb, f"image #{idx + 1} failed - regenerating "
                                f"(retry {attempts[idx]}/{IMAGE_RETRIES})")
                queue.append(idx)
            else:
                results[idx] = None
                _log(status_cb, f"image #{idx + 1} failed after {IMAGE_RETRIES} retries "
                                "- a black frame will be used.")
    ok = sum(1 for v in results.values() if v)
    _log(status_cb, f"Images finished: {ok}/{total} generated.")
    return results


# ------------------------------------------------------------------ 5) VERIFY + ASSEMBLE

def verify_images(lines, results, reasoning_model=None, status_cb=None):
    """Deterministic completeness check + (when a key is present) the reasoning model confirms
    the timestamp->image mapping before assembly."""
    missing = [i for i in range(len(lines)) if not results.get(i)]
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


def assemble_video(lines, durations, results, audio_path, out_path, status_cb=None):
    """Cut every image to its exact duration (black screen where missing), concat, mux voice."""
    ffmpeg = pipeline.find_ffmpeg()
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
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
    _log(status_cb, f"Final longform video ready: {out_path}")
    return out_path


# ------------------------------------------------------------------ ORCHESTRATOR

def run_longform_video(script, tts_model="pro", reasoning_model=None,
                       status_cb=None, cancel_event=None, speech_gate=None, resume=True,
                       voice=None, speaker=None, mix_gate=None):
    """The whole pipeline. Returns a result dict for the job UI.

    RESUME (default on): re-running the SAME script continues the existing project instead of
    starting over - the voiceover, timings and prompts are reloaded from state.json and only the
    images that are actually missing get generated. The state is keyed on the exact script, so
    editing the script starts a clean run (old images would otherwise land on shifted lines).
    """
    script = str(script or "").strip()
    if len(script) < 40:
        raise LongformError("Please paste the full script (at least a few sentences).")

    # Higgsfield is required for the image stage, but the only check used to live INSIDE
    # generate_images - after the paid TTS, the transcription and the paid prompt calls. A fresh
    # run with no login spent all of that and only THEN failed on a precondition. Check it up front
    # so the run stops in ~0s having spent nothing. (Resume is unaffected: it also has to reach the
    # image stage, so the same requirement holds, and the cached voiceover/prompts are untouched.)
    import higgsfield_login
    if not higgsfield_login.is_ready():
        raise LongformError("Higgsfield is not connected - click Connect Higgsfield first, "
                            "then start the run.")

    slug = slug_for(script)
    out_dir = OUT_ROOT / slug
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "script.txt").write_text(script, encoding="utf-8")

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
    speed_retime = bool(state and state.get("lines") and mix_gate is not None
                        and state.get("voice_speed") is None and _audio_done(raw_voice_path))
    old_lines = list((state or {}).get("lines") or []) if speed_retime else []
    old_prompts = list((state or {}).get("prompts") or []) if speed_retime else None
    old_audio_duration = float((state or {}).get("audio_duration") or 0.0)
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
                                                   mix_gate=mix_gate)
        audio_duration = _probe_duration(voice_path)
        lines = transcribe_lines(script, voice_path, status_cb=status_cb)
        if speed_retime and len(old_lines) == len(lines):
            retimed_prompts = retime_longform_assets(
                out_dir, old_lines, lines, old_audio_duration, audio_duration,
                prompts=old_prompts, status_cb=status_cb)
        save_state(out_dir, script=script, lines=lines, tts_parts=tts_parts, voice=voice or "",
                   prompts=retimed_prompts,
                   audio_duration=round(audio_duration, 3))
    transcript_path = write_transcript(lines, out_dir / "transcript.txt")
    _log(status_cb, f"Transcript written: {transcript_path.name}")

    prompts = (state or {}).get("prompts") if reusable else retimed_prompts
    cached_fmt = (state or {}).get("prompt_format") if reusable else None
    stale_format = bool(prompts) and cached_fmt != PROMPT_FORMAT_VERSION
    if prompts and len(prompts) == len(lines) and not stale_format:
        _log(status_cb, f"Resume: reusing the {len(prompts)} saved image prompt(s).")
    else:
        if stale_format:
            # The saved prompts predate the current doodle-prompt format (e.g. before the
            # mandatory ALL-CAPS top caption). Regenerate ALL prompts, and move the images made
            # from the old prompts aside so they are regenerated in the new style too.
            _log(status_cb, "Image prompts are an older format - regenerating all prompts in the "
                            "new caption style (and the images made from them).")
            _archive_old_images(out_dir / "images", status_cb)
        prompts = generate_image_prompts(lines, reasoning_model=reasoning_model,
                                         status_cb=status_cb, cancel_event=cancel_event)
        save_state(out_dir, script=script, lines=lines, prompts=prompts, tts_parts=tts_parts,
                   voice=voice or "", audio_duration=round(audio_duration, 3),
                   prompt_format=PROMPT_FORMAT_VERSION)
    prompts_path = write_prompts_file(prompts, out_dir / f"image_prompts_{slug}.txt")
    _log(status_cb, f"Prompt file written: {prompts_path.name}")

    durations = line_durations(lines, audio_duration)
    results = generate_images(prompts, lines, durations, out_dir / "images",
                              status_cb=status_cb, cancel_event=cancel_event)

    missing = verify_images(lines, results, reasoning_model=reasoning_model, status_cb=status_cb)
    latest_state = load_state(out_dir, script) or {}
    timeline_path = write_timeline_manifest(
        lines, durations, results, audio_duration, out_dir / "timeline.json",
        voice_speed=latest_state.get("voice_speed") or 1.0)
    final = assemble_video(lines, durations, results, voice_path,
                           out_dir / f"{slug}.mp4", status_cb=status_cb)
    return {
        "project_dir": str(out_dir), "video": str(final), "voiceover": str(voice_path),
        "transcript": str(transcript_path), "prompts_file": str(prompts_path),
        "timeline": str(timeline_path),
        "images_done": sum(1 for v in results.values() if v), "images_total": len(lines),
        "missing_images": [fmt_ts(lines[i]["start"]) for i in missing],
        "tts_parts": tts_parts, "audio_duration": round(audio_duration, 2),
    }
