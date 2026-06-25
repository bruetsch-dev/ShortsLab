"""Frame-accurate word-level timing for the spoken voiceover.

Uses faster-whisper (GPU when available) to transcribe the uploaded voice audio
with per-word timestamps, then aligns the KNOWN script text onto those timings so
the on-screen words match the script exactly while landing on the spoken beats.

The resulting word timeline drives word-by-word captions, scene/cut boundary
snapping, and sound-effect emphasis - so the Short cuts like a real edit.
"""

import difflib
import re
import threading

_MODEL = None
_MODEL_KEY = None
_MODEL_LOCK = threading.Lock()

DEFAULT_MODEL = "small"  # multilingual; "small.en" is a touch sharper for English-only


def _norm(token):
    return re.sub(r"[^0-9a-zà-ɏ]+", "", token.lower())


def available():
    try:
        import faster_whisper  # noqa: F401
        return True
    except Exception:
        return False


def _load_model(model_name=DEFAULT_MODEL, status_cb=None):
    global _MODEL, _MODEL_KEY
    with _MODEL_LOCK:
        if _MODEL is not None and _MODEL_KEY == model_name:
            return _MODEL
        from faster_whisper import WhisperModel
        last_exc = None
        for device, compute in (("cuda", "float16"), ("cpu", "int8")):
            try:
                model = WhisperModel(model_name, device=device, compute_type=compute)
                if status_cb:
                    status_cb(f"Voice aligner: {model_name} on {device}.")
                _MODEL, _MODEL_KEY = model, model_name
                return model
            except Exception as exc:
                last_exc = exc
        raise RuntimeError(f"Could not load faster-whisper model: {last_exc}")


def transcribe_words(audio_path, model_name=DEFAULT_MODEL, language=None, status_cb=None):
    """Return [{word, start, end}] with frame-accurate timestamps from ASR."""
    model = _load_model(model_name, status_cb=status_cb)
    segments, _info = model.transcribe(
        str(audio_path), word_timestamps=True, language=language,
        vad_filter=True, beam_size=5,
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
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                aw = asr_words[j1 + k]
                timed[i1 + k] = {"word": script_tokens[i1 + k],
                                 "start": aw["start"], "end": aw["end"]}
        elif tag in ("replace", "delete", "insert"):
            # Distribute the ASR time span of this block across the script words it covers.
            span_start = asr_words[j1]["start"] if j1 < len(asr_words) else (
                asr_words[-1]["end"] if asr_words else 0.0)
            span_end = asr_words[max(j1, j2 - 1)]["end"] if j2 > j1 and j2 - 1 < len(asr_words) else span_start
            count = max(1, i2 - i1)
            step = (span_end - span_start) / count if count else 0.0
            for k in range(i1, i2):
                a = span_start + step * (k - i1)
                b = a + step if step > 0 else a + 0.12
                timed[k] = {"word": script_tokens[k], "start": round(a, 3), "end": round(b, 3)}

    # Fill any remaining gaps by interpolating between known anchors.
    last = 0.0
    for idx in range(len(timed)):
        if timed[idx] is None:
            nxt = next((timed[j]["start"] for j in range(idx + 1, len(timed)) if timed[j]), last + 0.2)
            timed[idx] = {"word": script_tokens[idx], "start": round(last, 3), "end": round(nxt, 3)}
        last = timed[idx]["end"]

    # Enforce monotonic, non-overlapping order.
    for idx in range(1, len(timed)):
        if timed[idx]["start"] < timed[idx - 1]["end"]:
            timed[idx]["start"] = timed[idx - 1]["end"]
        if timed[idx]["end"] < timed[idx]["start"]:
            timed[idx]["end"] = timed[idx]["start"] + 0.08
    return timed


def word_timeline(audio_path, script_text=None, model_name=DEFAULT_MODEL,
                  language=None, status_cb=None):
    """Frame-accurate word timeline. Aligns the script when given, else raw ASR."""
    asr_words = transcribe_words(audio_path, model_name=model_name,
                                 language=language, status_cb=status_cb)
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
    """Words whose midpoint falls inside [start, end), with scene-local times."""
    out = []
    for w in timeline:
        mid = (w["start"] + w["end"]) / 2.0
        if start <= mid < end:
            out.append({"word": w["word"],
                        "start": round(max(0.0, w["start"] - start), 3),
                        "end": round(max(0.0, w["end"] - start), 3)})
    return out
