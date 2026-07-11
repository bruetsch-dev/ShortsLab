"""Frame-accurate word-level timing for the spoken voiceover.

Uses faster-whisper (GPU when available) to transcribe the uploaded voice audio
with per-word timestamps, then aligns the KNOWN script text onto those timings so
the on-screen words match the script exactly while landing on the spoken beats.

The resulting word timeline drives word-by-word captions, scene/cut boundary
snapping, and sound-effect emphasis - so the Short cuts like a real edit.
"""

import difflib
import os
import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path

_MODEL = None
_MODEL_KEY = None
_MODEL_LOCK = threading.Lock()

# Optional WhisperX (wav2vec2 phoneme forced-alignment) backend. When installed it
# replaces faster-whisper's DTW word timestamps (±100ms jitter) with phoneme-level
# alignment (±20-30ms) - the definitive fix for word-by-word caption snap. Falls back
# to faster-whisper transparently when whisperx isn't importable.
_WX_MODEL = None            # (asr_model, device)
_WX_ALIGN = {}              # language_code -> (align_model, metadata)
_WX_LOCK = threading.Lock()

DEFAULT_MODEL = "small"  # multilingual; kept as the compat default / CPU fallback
# Caption-timing accuracy: word timestamps from "small" are visibly jittery on sped-up
# narration (1.15-1.3x), which made some caption words flip early/late. Prefer "medium"
# on the GPU (still ~seconds for a 60s voiceover); fall back to small, then CPU.
MODEL_PREFERENCE = (
    ("medium", "cuda", "float16"),
    ("small", "cuda", "float16"),
    ("small", "cpu", "int8"),
)


def _norm(token):
    return re.sub(r"[^0-9a-zà-ɏ]+", "", token.lower())


def available():
    try:
        import faster_whisper  # noqa: F401
        return True
    except Exception:
        return whisperx_available()


def whisperx_available():
    try:
        import whisperx  # noqa: F401
        return True
    except Exception:
        return False


def _despeed_to_temp(audio_path, speed, status_cb=None):
    """Write a natural-tempo (x1.0) copy of a sped-up voiceover to a temp 16k mono WAV.

    Whisper / wav2vec2 word boundaries degrade on time-compressed speech (1.15-1.3x),
    which is what flips caption words early/late. We align on the de-sped copy, then the
    caller rescales the timings back onto the real (sped) audio. atempo is linear, so the
    time mapping t_final = t_desped / speed is exact (the fixed hook/body pause scales with
    everything, unlike a saved pre-speed source). Returns the temp Path, or None to signal
    "align the original directly".
    """
    try:
        s = float(speed)
    except (TypeError, ValueError):
        return None
    if not s or abs(s - 1.0) < 0.02:
        return None
    tempo = 1.0 / s                                  # e.g. 1.3x speech -> atempo 0.769 (in-range)
    if not (0.5 <= tempo <= 2.0):
        return None
    try:
        import pipeline
        ffmpeg = pipeline.find_ffmpeg()
    except Exception:
        ffmpeg = None
    if not ffmpeg:
        return None
    tmp = Path(tempfile.gettempdir()) / f"valign_{os.getpid()}_{int(time.time()*1000)}.wav"
    try:
        subprocess.run(
            [ffmpeg, "-y", "-i", str(audio_path), "-filter:a", f"atempo={tempo:.6f}",
             "-ar", "16000", "-ac", "1", str(tmp)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
        )
    except Exception:
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass
        return None
    if status_cb:
        status_cb(f"Aligning on a natural-tempo copy (x{s:.2f} -> x1.0) for tighter word sync.")
    return tmp


def _load_whisperx(language=None, status_cb=None):
    global _WX_MODEL
    import whisperx
    with _WX_LOCK:
        if _WX_MODEL is None:
            device, compute = ("cuda", "float16")
            try:
                import torch
                if not torch.cuda.is_available():
                    device, compute = ("cpu", "int8")
            except Exception:
                device, compute = ("cpu", "int8")
            try:
                asr = whisperx.load_model("medium", device, compute_type=compute)
            except Exception:
                asr = whisperx.load_model("small", "cpu", compute_type="int8")
                device = "cpu"
            _WX_MODEL = (asr, device)
            if status_cb:
                status_cb(f"Voice aligner: WhisperX (phoneme) on {device}.")
        return _WX_MODEL


def _transcribe_words_whisperx(audio_path, language=None, status_cb=None):
    """Phoneme forced-alignment word timings via WhisperX. Returns [{word,start,end}]."""
    import whisperx
    asr, device = _load_whisperx(language, status_cb=status_cb)
    audio = whisperx.load_audio(str(audio_path))
    result = asr.transcribe(audio, batch_size=16, language=language)
    lang = result.get("language") or language or "en"
    with _WX_LOCK:
        if lang not in _WX_ALIGN:
            _WX_ALIGN[lang] = whisperx.load_align_model(language_code=lang, device=device)
        align_model, metadata = _WX_ALIGN[lang]
    aligned = whisperx.align(result["segments"], align_model, metadata, audio, device,
                             return_char_alignments=False)
    words = []
    for w in (aligned.get("word_segments") or []):
        text = str(w.get("word") or "").strip()
        start, end = w.get("start"), w.get("end")
        if not text or start is None or end is None:
            continue
        words.append({"word": text, "start": round(float(start), 3), "end": round(float(end), 3)})
    return words


def _load_model(model_name=None, status_cb=None):
    """Load the best available aligner model. model_name=None walks MODEL_PREFERENCE
    (medium on GPU first); an explicit name keeps the old cuda->cpu behaviour."""
    global _MODEL, _MODEL_KEY
    with _MODEL_LOCK:
        key = model_name or "auto"
        if _MODEL is not None and _MODEL_KEY == key:
            return _MODEL
        from faster_whisper import WhisperModel
        attempts = (MODEL_PREFERENCE if model_name is None
                    else ((model_name, "cuda", "float16"), (model_name, "cpu", "int8")))
        last_exc = None
        for name, device, compute in attempts:
            try:
                model = WhisperModel(name, device=device, compute_type=compute)
                if status_cb:
                    status_cb(f"Voice aligner: {name} on {device}.")
                _MODEL, _MODEL_KEY = model, key
                return model
            except Exception as exc:
                last_exc = exc
        raise RuntimeError(f"Could not load faster-whisper model: {last_exc}")


def _transcribe_words_faster_whisper(audio_path, model_name=None, language=None, status_cb=None):
    model = _load_model(model_name, status_cb=status_cb)
    segments, _info = model.transcribe(
        str(audio_path), word_timestamps=True, language=language,
        vad_filter=True, beam_size=5,
        # pad the VAD speech regions: without this, words right after a pause get their
        # onset CLIPPED (caption appears late); no previous-text conditioning = less drift
        vad_parameters={"min_silence_duration_ms": 300, "speech_pad_ms": 150},
        condition_on_previous_text=False,
    )
    words = []
    for segment in segments:
        for w in (segment.words or []):
            text = (w.word or "").strip()
            if not text:
                continue
            words.append({"word": text, "start": round(float(w.start), 3),
                          "end": round(float(w.end), 3)})
    return words


def transcribe_words(audio_path, model_name=None, language=None, status_cb=None, speed=1.0):
    """Return [{word, start, end}] with frame-accurate timestamps from ASR.

    `speed` is the tempo the voiceover was sped to (1.15-1.3x). When >1, we transcribe a
    natural-tempo copy and rescale the timings back so word boundaries are as tight as on
    un-sped speech. Prefers WhisperX phoneme alignment when installed, else faster-whisper.
    """
    src, inv, tmp = audio_path, 1.0, None
    try:
        tmp = _despeed_to_temp(audio_path, speed, status_cb=status_cb)
        if tmp is not None:
            src, inv = tmp, 1.0 / float(speed)
    except Exception:
        src, inv, tmp = audio_path, 1.0, None
    try:
        words = None
        if whisperx_available():
            try:
                words = _transcribe_words_whisperx(src, language=language, status_cb=status_cb)
            except Exception as exc:
                if status_cb:
                    status_cb(f"WhisperX alignment failed ({exc}); using faster-whisper.")
                words = None
        if not words:
            words = _transcribe_words_faster_whisper(src, model_name=model_name,
                                                     language=language, status_cb=status_cb)
    finally:
        if tmp is not None:
            try:
                Path(tmp).unlink()
            except Exception:
                pass
    if inv != 1.0:
        for w in words:
            w["start"] = round(w["start"] * inv, 3)
            w["end"] = round(w["end"] * inv, 3)
    return words


def align_script_to_words(script_text, asr_words):
    """Map the known script words onto ASR word timings.

    Where script and ASR agree, keep the script text on the ASR timing. Where they
    differ (ASR errors, e.g. Cellars->Sellers), keep the *script* text but borrow
    timing from the surrounding anchors. Returns [{word, start, end}].
    """
    script_tokens = [t for t in re.split(r"\s+", (script_text or "").strip()) if t]
    if not asr_words:
        return []
    if not script_tokens:
        return [dict(w) for w in asr_words]

    s_norm = [_norm(t) for t in script_tokens]
    a_norm = [_norm(w["word"]) for w in asr_words]
    matcher = difflib.SequenceMatcher(a=s_norm, b=a_norm, autojunk=False)

    timed = [None] * len(script_tokens)
    _anchor_chars, _anchor_secs = 0, 0.0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                aw = asr_words[j1 + k]
                timed[i1 + k] = {"word": script_tokens[i1 + k],
                                 "start": aw["start"], "end": aw["end"]}
                # accumulate the ACTUAL spoken pace so gap-fills below match this voice.
                nchars = len(_norm(script_tokens[i1 + k]))
                dur = max(0.0, float(aw["end"]) - float(aw["start"]))
                if nchars and 0.02 <= dur <= 1.2:
                    _anchor_chars += nchars
                    _anchor_secs += dur
        elif tag == "replace":
            # ASR heard different words here: distribute the ASR span across the script
            # words WEIGHTED BY WORD LENGTH (uniform steps made long words flash by and
            # short words linger - a visible caption desync).
            span_start = asr_words[j1]["start"] if j1 < len(asr_words) else (
                asr_words[-1]["end"] if asr_words else 0.0)
            span_end = asr_words[max(j1, j2 - 1)]["end"] if j2 > j1 and j2 - 1 < len(asr_words) else span_start
            _fill_span_weighted(timed, script_tokens, i1, i2, span_start, span_end)
        # tag == "insert": ASR has extra words the script lacks -> nothing to time here.
        # tag == "delete": script words the ASR never heard (i2 > i1, j1 == j2) -> they get
        # timed in the anchor-gap pass below (the old code stacked them all on one instant,
        # which then cascaded 0.08s offsets over the following words - THE timing bug).

    # Seconds-per-character of THIS voiceover (from the anchored words). Used to cap how
    # long an un-anchored word may linger, so words in a big gap (e.g. trailing silence the
    # ASR dropped) land at the real spoken pace right after the anchor instead of smearing
    # evenly across the whole gap (which made the following caption word appear early).
    sec_per_char = (_anchor_secs / _anchor_chars) if _anchor_chars >= 6 else 0.06

    # Time the still-missing script words inside the GAP between their neighbouring
    # anchors, at the measured pace (capped), packed from the previous anchor.
    idx = 0
    while idx < len(timed):
        if timed[idx] is not None:
            idx += 1
            continue
        run_start = idx
        while idx < len(timed) and timed[idx] is None:
            idx += 1
        run_end = idx                                     # [run_start, run_end) untimed
        prev_end = timed[run_start - 1]["end"] if run_start > 0 else 0.0
        next_start = (timed[run_end]["start"] if run_end < len(timed)
                      else prev_end + 0.25 * (run_end - run_start))
        _fill_span_weighted(timed, script_tokens, run_start, run_end, prev_end, next_start,
                            sec_per_char=sec_per_char)

    # Enforce monotonic, non-overlapping order + clamp runaway word durations (whisper
    # sometimes stretches the last word of a sentence across the following pause, which
    # kept the caption highlight stuck on that word).
    for idx in range(len(timed)):
        if idx and timed[idx]["start"] < timed[idx - 1]["end"]:
            timed[idx]["start"] = timed[idx - 1]["end"]
        if timed[idx]["end"] < timed[idx]["start"]:
            timed[idx]["end"] = timed[idx]["start"] + 0.08
        if timed[idx]["end"] - timed[idx]["start"] > 1.2:
            timed[idx]["end"] = round(timed[idx]["start"] + 1.2, 3)
    return timed


def _fill_span_weighted(timed, tokens, i1, i2, span_start, span_end, sec_per_char=None):
    """Distribute [span_start, span_end] over tokens[i1:i2], weighted by word length
    (min 0.06s each). Writes into `timed` in place.

    When `sec_per_char` (the voiceover's measured pace) is given AND the span is much
    looser than that pace needs, cap each word near its natural spoken duration and pack
    from span_start - so words don't stretch to fill a long dropped-audio gap.
    """
    count = i2 - i1
    if count <= 0:
        return
    span = max(0.0, float(span_end) - float(span_start))
    weights = [max(2, len(_norm(tokens[k])) or len(tokens[k])) for k in range(i1, i2)]
    total = float(sum(weights)) or 1.0
    if span <= 0.01:                        # no room at all: tight sequential fallback
        cursor = float(span_start)
        for k in range(i1, i2):
            timed[k] = {"word": tokens[k], "start": round(cursor, 3), "end": round(cursor + 0.08, 3)}
            cursor += 0.08
        return
    # Natural pace available and the span is >1.5x what the words actually need: pack at
    # the measured pace from the start rather than smearing across the whole gap.
    if sec_per_char and sec_per_char > 0:
        natural = [max(0.10, len(_norm(tokens[k])) * sec_per_char * 1.15) for k in range(i1, i2)]
        if sum(natural) * 1.5 < span:
            cursor = float(span_start)
            for pos, k in enumerate(range(i1, i2)):
                d = natural[pos]
                timed[k] = {"word": tokens[k], "start": round(cursor, 3),
                            "end": round(cursor + d, 3)}
                cursor += d
            return
    cursor = float(span_start)
    for pos, k in enumerate(range(i1, i2)):
        d = max(0.06, span * weights[pos] / total)
        timed[k] = {"word": tokens[k], "start": round(cursor, 3),
                    "end": round(min(span_end, cursor + d) if pos < count - 1 else span_end, 3)}
        cursor += d


def word_timeline(audio_path, script_text=None, model_name=None,
                  language=None, status_cb=None, speed=1.0):
    """Frame-accurate word timeline. Aligns the script when given, else raw ASR."""
    asr_words = transcribe_words(audio_path, model_name=model_name,
                                 language=language, status_cb=status_cb, speed=speed)
    if not asr_words:
        return []
    if not script_text or not script_text.strip():
        return asr_words
    aligned = align_script_to_words(script_text, asr_words)
    # If the script clearly does not match the audio, trust the ASR text instead.
    s_norm = [_norm(t) for t in re.split(r"\s+", script_text.strip()) if t]
    a_norm = [_norm(w["word"]) for w in asr_words]
    ratio = difflib.SequenceMatcher(a=s_norm, b=a_norm, autojunk=False).ratio()
    return aligned if ratio >= 0.5 else asr_words


def sentences_from_words(words, max_gap=0.7, max_words=14):
    """Group the word timeline into sentence-level [{start,end,text}] segments."""
    sentences, cur = [], []
    for w in words:
        cur.append(w)
        end_sentence = (w["word"][-1:] in ".!?"
                        or len(cur) >= max_words
                        or (cur and len(cur) > 1 and w is not cur[0]
                            and w["start"] - cur[-2]["end"] > max_gap))
        if end_sentence:
            sentences.append({"start": round(cur[0]["start"], 3), "end": round(cur[-1]["end"], 3),
                              "text": " ".join(x["word"] for x in cur)})
            cur = []
    if cur:
        sentences.append({"start": round(cur[0]["start"], 3), "end": round(cur[-1]["end"], 3),
                          "text": " ".join(x["word"] for x in cur)})
    return sentences


def analysis_from_audio(audio_path, script_text=None, duration=None, status_cb=None, speed=1.0):
    """Build a Gemini-shaped timing analysis from local forced alignment.

    Returns (analysis_dict, word_timeline). The analysis mimics the audio-analysis
    schema the rest of the pipeline already consumes (transcript, duration_seconds,
    sentence_timestamps) so faster-whisper can replace Gemini as the timing source.

    `speed` = the tempo the voiceover was sped to; the aligner de-speeds a copy to x1.0
    for tighter word boundaries, then rescales the timings back onto the real audio.
    """
    timeline = word_timeline(audio_path, script_text=script_text, status_cb=status_cb, speed=speed)
    if not timeline:
        return None, []
    sentences = sentences_from_words(timeline)
    transcript = " ".join(w["word"] for w in timeline)
    analysis = {
        "transcript": transcript,
        "duration_seconds": round(float(duration) if duration else timeline[-1]["end"], 3),
        "sentence_timestamps": sentences,
        "timing_source": "forced_alignment",
    }
    return analysis, timeline


def snap_scene_boundaries(scenes, timeline, total_duration=0.0, tolerance=0.28):
    """Nudge each scene cut onto the nearest spoken word onset (so cuts hit the beat).

    Keeps scenes contiguous; only moves a boundary when a word starts within
    `tolerance` seconds of it. Mutates and returns the scene list.
    """
    if not scenes or not timeline:
        return scenes
    starts = sorted(w["start"] for w in timeline)
    scenes = sorted(scenes, key=lambda s: float(s["start"]))

    def nearest(t):
        best = min(starts, key=lambda s: abs(s - t))
        return best if abs(best - t) <= tolerance else t

    for i in range(len(scenes) - 1):
        boundary = float(scenes[i]["end"])
        snapped = round(nearest(boundary), 3)
        if (snapped > float(scenes[i]["start"]) + 0.2
                and snapped < float(scenes[i + 1]["end"]) - 0.2):
            scenes[i]["end"] = snapped
            scenes[i + 1]["start"] = snapped
    scenes[0]["start"] = 0.0
    if total_duration:
        scenes[-1]["end"] = round(float(total_duration), 3)
    return scenes


def words_in_window(timeline, start, end):
    """Words whose ONSET falls inside [start, end), with scene-local times.

    Assigning by onset (not midpoint) keeps a word that straddles a cut with the scene
    where it starts being spoken, so the caption highlight never resets mid-word at a cut.
    The tail is clamped to the scene end so a word spilling past the cut doesn't linger.
    """
    span = max(0.0, float(end) - float(start))
    out = []
    for w in timeline:
        if start <= w["start"] < end:
            ls = max(0.0, w["start"] - start)
            le = min(span, w["end"] - start)
            le = max(ls + 0.05, le)
            out.append({"word": w["word"], "start": round(ls, 3), "end": round(le, 3)})
    return out
