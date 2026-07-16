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
                  session: FLUX.2 Pro (unlimited), 16:9, up to 4 generations in flight - a new
                  prompt is only submitted when active generations drop below 4. Failed
                  generations are retried. A character-reference frame is generated FIRST.
                  Files are named with the timestamp AND the on-screen duration.
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
IMAGE_CONCURRENCY = 4               # max Higgsfield generations in flight
IMAGE_RETRIES = 2                   # re-generate a failed image up to N extra times
VIDEO_W, VIDEO_H, VIDEO_FPS = 1920, 1080, 30


STATE_FILE = "state.json"           # resume state: script + timings + prompts of the last run
MIN_IMAGE_BYTES = 1024              # smaller than this = a truncated/failed write, regenerate


class LongformError(RuntimeError):
    pass


def _image_done(path):
    """True when an image is already on disk and is not a truncated stub."""
    try:
        p = Path(path)
        return p.is_file() and p.stat().st_size >= MIN_IMAGE_BYTES
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


def generate_voiceover(script, out_dir, tts_model="pro", status_cb=None, cancel_event=None,
                       speech_gate=None):
    """Script -> voiceover.wav (parts stitched). Returns (path, parts_count).

    `speech_gate(parts_info, regen_part)` (optional, "Halt after speech"): called AFTER all
    TTS parts exist and BEFORE they are stitched. `parts_info` is a list of
    {"index", "text", "path"}; `regen_part(i)` re-generates part i with a fresh TTS take and
    returns the new path. The gate blocks until the user has approved every part (declines
    trigger regen through the callback); it raises to cancel the run.
    """
    parts = split_script_for_tts(script)
    if not parts:
        raise LongformError("The script is empty.")
    ffmpeg = pipeline.find_ffmpeg()
    if not ffmpeg:
        raise LongformError("ffmpeg not found.")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    part_files = []
    for i, part in enumerate(parts):
        if cancel_event is not None and cancel_event.is_set():
            raise pipeline.PipelineCancelled("Cancelled.")
        _log(status_cb, f"Voiceover part {i + 1}/{len(parts)} ({len(part)} chars) "
                        f"with Gemini 2.5 {'Pro' if tts_model == 'pro' else 'Flash'} TTS...")
        p = pipeline.generate_speech_gemini(part, out_dir / f"vo_part{i:02d}.wav",
                                            model=tts_model, cancel_event=cancel_event,
                                            status_cb=status_cb)
        part_files.append(p)

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
                model=tts_model, cancel_event=cancel_event, status_cb=status_cb)
            old = part_files[i]
            part_files[i] = Path(new_path)
            try:
                if str(old) != str(new_path):
                    Path(old).unlink(missing_ok=True)
            except Exception:
                pass
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
    _log(status_cb, f"Voiceover ready: {out.name} ({len(parts)} part(s) stitched).")
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

STAGE3_PROMPT = """## STAGE 3 - GENERATE IMAGE PROMPTS FOR EVERY TIMESTAMP

Once the user pastes their timestamped script, generate one detailed text-to-image prompt for every single timestamp line.

**IMAGE PROMPT RULES:**

1. Every prompt must begin with its timestamp COPIED EXACTLY as it appears in the script (e.g. `[0:03.4]`) - do not reformat or round timestamps
2. Every prompt must open with the style anchor: "Hand-drawn 2D doodle cartoon animation, flat colors, bold black outlines, slightly imperfect sketchy marker lines,"
3. Every prompt must end with the style lock: "no gradients, no shadows, no textures, no photorealism, no 3D, no realistic faces, no anime style, 16:9 aspect ratio, educational YouTube explainer doodle style."
4. Be specific inside each prompt - describe what characters are present and what they are doing, their exact expression, what objects are in the scene, what background color is used, whether any on-screen text or labels appear
5. Translate abstract narration into concrete visuals - if the script says "your body doesn't know the difference", show a confused stick figure looking at two identical objects; if it says "millions of years", show a large hourglass with bold red ALL CAPS text "MILLIONS OF YEARS" at the top of the frame
6. Match tone to background color:
   - Ancient / prehistoric -> tan or dark blue background
   - Danger / threat -> stark white with red text or red-tinted sky
   - Happy / triumph / discovery -> bright white or yellow background
   - Underwater / science -> solid blue background
   - Outdoor / nature / evolution -> flat green ground + blue sky
   - Fire / night / ancient ritual -> solid orange background
7. Hold scenes across consecutive timestamps - if 3 lines describe the same moment, keep the same scene and only adjust the character's expression or add one new element. Do not generate a brand new scene every 5 seconds.
8. Use these proven frame types when appropriate:
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


def line_durations(lines, audio_duration):
    """Per-line on-screen duration: to the next line's start; last line holds to audio end."""
    durs = []
    for i, l in enumerate(lines):
        if i + 1 < len(lines):
            durs.append(max(0.4, round(lines[i + 1]["start"] - l["start"], 2)))
        else:
            durs.append(max(0.4, round(max(audio_duration, l["end"]) - l["start"], 2)))
    return durs


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

    # character reference FIRST (consistency anchor; also proves the session works)
    ref_path = out_dir / "character_reference.png"
    if _image_done(ref_path):
        _log(status_cb, "Character reference already there - reusing it.")
    else:
        _log(status_cb, "Generating the character reference frame first...")
        ref = higgsfield_login.generate_sync(CHARACTER_REFERENCE_PROMPT, ref_path,
                                             aspect="16:9", model="FLUX.2 Pro",
                                             timeout_s=300, status_cb=status_cb)
        if ref:
            _log(status_cb, "Character reference saved (used as the style anchor).")
        else:
            _log(status_cb, "Character reference failed - continuing without it.")

    total = len(prompts)
    results = {}
    attempts = {}
    lock = threading.Lock()
    # RESUME: every image whose file is already on disk is reused, so a re-run only generates
    # what is actually missing. The filename (index + timestamp + duration) identifies the line,
    # so a reused file always belongs to the line it is mapped onto - if the script or its timing
    # changed, the key changes and the image is regenerated instead of silently mismatched.
    for idx in range(total):
        existing = out_dir / f"{image_key(idx, lines[idx], durations[idx])}.png"
        if _image_done(existing):
            results[idx] = str(existing)
    if results:
        _log(status_cb, f"Resume: {len(results)}/{total} image(s) already generated - "
                        f"only the missing {total - len(results)} will be generated.")
    queue = [i for i in range(total) if i not in results]
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=IMAGE_CONCURRENCY)
    inflight = {}

    def submit(idx):
        prompt = prompts[idx]["prompt"]
        key = image_key(idx, lines[idx], durations[idx])
        return pool.submit(higgsfield_login.generate_sync, prompt, out_dir / f"{key}.png",
                           "16:9", "FLUX.2 Pro", 300, status_cb)

    try:
        while queue or inflight:
            if cancel_event is not None and cancel_event.is_set():
                raise pipeline.PipelineCancelled("Cancelled.")
            # refill: only submit while fewer than IMAGE_CONCURRENCY are active
            while queue and len(inflight) < IMAGE_CONCURRENCY:
                idx = queue.pop(0)
                inflight[submit(idx)] = idx
                with lock:
                    done_n = sum(1 for v in results.values() if v)
                _log(status_cb, f"image {done_n}/{total} - submitted #{idx + 1} "
                                f"({len(inflight)} active)")
            done, _pending = concurrent.futures.wait(
                list(inflight.keys()), timeout=5.0,
                return_when=concurrent.futures.FIRST_COMPLETED)
            for fut in done:
                idx = inflight.pop(fut)
                path = None
                try:
                    path = fut.result()
                except Exception:
                    path = None
                if path:
                    results[idx] = str(path)
                    _log(status_cb, f"image {sum(1 for v in results.values() if v)}/{total} - "
                                    f"#{idx + 1} done")
                else:
                    attempts[idx] = attempts.get(idx, 0) + 1
                    if attempts[idx] <= IMAGE_RETRIES:
                        _log(status_cb, f"image #{idx + 1} failed - regenerating "
                                        f"(retry {attempts[idx]}/{IMAGE_RETRIES})")
                        queue.append(idx)
                    else:
                        results[idx] = None
                        _log(status_cb, f"image #{idx + 1} failed after {IMAGE_RETRIES} retries "
                                        "- a black frame will be used.")
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
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
                       status_cb=None, cancel_event=None, speech_gate=None, resume=True):
    """The whole pipeline. Returns a result dict for the job UI.

    RESUME (default on): re-running the SAME script continues the existing project instead of
    starting over - the voiceover, timings and prompts are reloaded from state.json and only the
    images that are actually missing get generated. The state is keyed on the exact script, so
    editing the script starts a clean run (old images would otherwise land on shifted lines).
    """
    script = str(script or "").strip()
    if len(script) < 40:
        raise LongformError("Please paste the full script (at least a few sentences).")
    slug = slug_for(script)
    out_dir = OUT_ROOT / slug
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "script.txt").write_text(script, encoding="utf-8")

    state = load_state(out_dir, script) if resume else None
    voice_path = out_dir / "voiceover.wav"
    ffprobe = pipeline.find_ffprobe(pipeline.find_ffmpeg())

    def _probe_duration(path):
        try:
            out = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration",
                                  "-of", "default=nokey=1:noprint_wrappers=1", str(path)],
                                 capture_output=True, text=True, timeout=30)
            return float((out.stdout or "0").strip() or 0.0)
        except Exception:
            return 0.0

    reusable = bool(state and state.get("lines") and voice_path.exists())
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
                                                   speech_gate=speech_gate)
        audio_duration = _probe_duration(voice_path)
        lines = transcribe_lines(script, voice_path, status_cb=status_cb)
        save_state(out_dir, script=script, lines=lines, tts_parts=tts_parts,
                   audio_duration=round(audio_duration, 3))
    transcript_path = write_transcript(lines, out_dir / "transcript.txt")
    _log(status_cb, f"Transcript written: {transcript_path.name}")

    prompts = (state or {}).get("prompts") if reusable else None
    if prompts and len(prompts) == len(lines):
        _log(status_cb, f"Resume: reusing the {len(prompts)} saved image prompt(s).")
    else:
        prompts = generate_image_prompts(lines, reasoning_model=reasoning_model,
                                         status_cb=status_cb, cancel_event=cancel_event)
        save_state(out_dir, script=script, lines=lines, prompts=prompts, tts_parts=tts_parts,
                   audio_duration=round(audio_duration, 3))
    prompts_path = write_prompts_file(prompts, out_dir / f"image_prompts_{slug}.txt")
    _log(status_cb, f"Prompt file written: {prompts_path.name}")

    durations = line_durations(lines, audio_duration)
    results = generate_images(prompts, lines, durations, out_dir / "images",
                              status_cb=status_cb, cancel_event=cancel_event)

    missing = verify_images(lines, results, reasoning_model=reasoning_model, status_cb=status_cb)
    final = assemble_video(lines, durations, results, voice_path,
                           out_dir / f"{slug}.mp4", status_cb=status_cb)
    return {
        "project_dir": str(out_dir), "video": str(final), "voiceover": str(voice_path),
        "transcript": str(transcript_path), "prompts_file": str(prompts_path),
        "images_done": sum(1 for v in results.values() if v), "images_total": len(lines),
        "missing_images": [fmt_ts(lines[i]["start"]) for i in missing],
        "tts_parts": tts_parts, "audio_duration": round(audio_duration, 2),
    }
