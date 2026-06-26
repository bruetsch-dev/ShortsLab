import argparse
import concurrent.futures
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import wave
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont


ROOT = Path(__file__).resolve().parent
API_BASE = "https://api.wavespeed.ai/api/v3"
DEFAULT_IMAGE_MODEL = "openai/gpt-image-2/text-to-image"
DEFAULT_VIDEO_MODEL = "bytedance/seedance-2.0/image-to-video"

# --- Content safety: keep every prompt sent to WaveSpeed strictly SFW ---
# Image/video models (especially uncensored variants) can hallucinate nudity or
# gore from ambiguous shapes. We steer them away with POSITIVE framing only.
#
# We deliberately do NOT send a negative_prompt listing words like "nudity" or
# "gore": Seedance I2V doesn't use one for safety, and -- more importantly --
# putting explicit terms anywhere in the request (even in a negative field) can
# itself trip the provider's content filter and block generation entirely. So
# we (a) scrub explicit request terms out of the incoming prompt and (b) append
# a short positive clause that steers toward clothed, tasteful, documentary
# imagery without ever naming the forbidden concepts.
SAFETY_PROMPT_SUFFIX = (
    " Wholesome, tasteful documentary realism: every figure is fully clothed in "
    "modest, period-appropriate attire, with respectful, non-graphic framing."
)
_NSFW_REQUEST_RE = re.compile(
    r"\b(nudes?|nudity|naked|topless|bottomless|undress\w*|lingerie|underwear|"
    r"sexual|erotic|nsfw|explicit|porn\w*|genitals?|nipples?|breasts?|buttocks?|"
    r"gore|gory|dismember\w*|mutilat\w*|disembowel\w*)\b",
    re.IGNORECASE,
)


def make_prompt_safe(prompt):
    """Scrub explicit NSFW/gore request terms and append a positive SFW clause.

    Uses positive framing only -- no negative-prompt word list -- so explicit
    tokens never reach the provider's content filter (which can otherwise block
    an otherwise-safe generation just for mentioning the forbidden concept).
    """
    text = _NSFW_REQUEST_RE.sub("clothed", str(prompt or "")).strip()
    if not text:
        return SAFETY_PROMPT_SUFFIX.strip()
    return text + SAFETY_PROMPT_SUFFIX


# --- Voiceover generation (Gemini 2.5 text-to-speech on WaveSpeed) ------------
# The user picks a speaker name + voice; we generate the spoken track instead of
# requiring an upload. Request shape (per WaveSpeed docs):
#   {"text": "Rose: <script>", "language": "English (United States)",
#    "speakers": [{"speaker": "Rose", "voice": "Achernar"}]}
# The speaker name MUST prefix the script text, and also appears in `speakers`.
GEMINI_TTS_MODELS = {
    "flash": "google/gemini-2.5-flash/text-to-speech",
    "pro": "google/gemini-2.5-pro/text-to-speech",
}
DEFAULT_TTS_MODEL = "flash"
DEFAULT_TTS_LANGUAGE = "English (United States)"
DEFAULT_TTS_SPEAKER = "Narrator"
DEFAULT_TTS_VOICE = "Achernar"
# Canonical Gemini TTS voice set (30 voices). Shown in the speaker/voice picker.
GEMINI_TTS_VOICES = [
    "Zephyr", "Puck", "Charon", "Kore", "Fenrir", "Leda", "Orus", "Aoede",
    "Callirrhoe", "Autonoe", "Enceladus", "Iapetus", "Umbriel", "Algieba",
    "Despina", "Erinome", "Algenib", "Rasalgethi", "Laomedeia", "Achernar",
    "Alnilam", "Schedar", "Gacrux", "Pulcherrima", "Achird", "Zubenelgenubi",
    "Vindemiatrix", "Sadachbia", "Sadaltager", "Sulafat",
]


def format_tts_script(speaker_name, text):
    """Prefix the script with the chosen speaker name, e.g. 'Rose: In 1814, ...'.

    Gemini multi-speaker TTS keys lines by the speaker label, so the same name
    must lead the text and appear in the `speakers` array.
    """
    speaker = (str(speaker_name or "").strip() or DEFAULT_TTS_SPEAKER)
    body = str(text or "").strip()
    return f"{speaker}: {body}" if body else speaker


def generate_speech_gemini(text, out_path, key=None, speaker=DEFAULT_TTS_SPEAKER,
                           voice=DEFAULT_TTS_VOICE, model=DEFAULT_TTS_MODEL,
                           language=DEFAULT_TTS_LANGUAGE, cancel_event=None,
                           status_cb=None):
    """Generate a spoken voiceover with Gemini TTS and download it to out_path.

    Returns the local Path. `model` accepts 'flash'/'pro' or a full model id.
    """
    key = key or api_key()
    model_id = GEMINI_TTS_MODELS.get(model, model)
    speaker = (str(speaker or "").strip() or DEFAULT_TTS_SPEAKER)
    payload = {
        "text": format_tts_script(speaker, text),
        "language": language or DEFAULT_TTS_LANGUAGE,
        "speakers": [{"speaker": speaker, "voice": voice or DEFAULT_TTS_VOICE}],
    }
    status_log(status_cb, f"Generating voiceover ({speaker}/{voice}) with {model_id}...")
    response = request_json("POST", f"{API_BASE}/{model_id}", key, payload, timeout=180)
    prediction_id = unwrap_id(response)
    outputs, _ = poll_wavespeed(prediction_id, key, timeout_s=420, cancel_event=cancel_event,
                                status_cb=status_cb, label="Voiceover")
    out_path = Path(out_path)
    # Respect the real output extension (wav/mp3) so downstream tools sniff it right.
    ext = output_extension(outputs[0], out_path.suffix or ".wav")
    if out_path.suffix.lower() != ext.lower():
        out_path = out_path.with_suffix(ext)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    download_file(outputs[0], out_path)
    return out_path


def concat_audio_with_pause(first_path, second_path, out_path, pause_s=0.45,
                            sample_rate=44100, ffmpeg=None):
    """Join two audio clips with a short silent gap between them.

    Used so the spoken hook and the rest of the narration don't run together:
    a small pause after the hook makes the intro->body transition feel edited
    rather than like one continuous take. Returns the output Path, or None if
    ffmpeg is unavailable.
    """
    ffmpeg = ffmpeg or find_ffmpeg()
    if not ffmpeg:
        return None
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    pause_s = max(0.0, float(pause_s))
    fmt = f"aformat=sample_rates={sample_rate}:channel_layouts=stereo"
    cmd = [
        ffmpeg, "-y",
        "-i", str(first_path),
        "-i", str(second_path),
        "-f", "lavfi", "-t", f"{pause_s:.3f}",
        "-i", f"anullsrc=r={sample_rate}:cl=stereo",
        "-filter_complex",
        f"[0:a]{fmt}[a];[2:a]{fmt}[s];[1:a]{fmt}[b];[a][s][b]concat=n=3:v=0:a=1[out]",
        "-map", "[out]",
        str(out_path),
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    return out_path


# --- Talking-head hook clip (InfiniteTalk on WaveSpeed) -----------------------
# The opening "speaker hook" is now a lip-synced talking head driven by the
# spoken hook audio + the speaker image -- not a Seedance I2V clip. Schema:
#   {"image": <url>, "audio": <url>, "prompt": <str?>, "resolution": "480p"|"720p", "seed": -1}
# Output video at data.outputs[0].
INFINITETALK_MODEL = "wavespeed-ai/infinitetalk"


def submit_infinitetalk(image_url, audio_url, key=None, prompt="", resolution="480p", seed=-1):
    key = key or api_key()
    payload = {
        "image": image_url,
        "audio": audio_url,
        "resolution": resolution,
        "seed": int(seed),
    }
    if prompt:
        payload["prompt"] = make_prompt_safe(prompt)
    response = request_json("POST", f"{API_BASE}/{INFINITETALK_MODEL}", key, payload, timeout=240)
    return unwrap_id(response), response


def generate_infinitetalk_clip(image_path, audio_path, out_path, key=None, prompt="",
                               resolution="480p", cancel_event=None, status_cb=None):
    """Render a lip-synced talking-head clip from a still image + spoken audio.

    Uploads the image and audio, submits InfiniteTalk, polls, and downloads the
    resulting video to out_path. Returns the output Path.
    """
    key = key or api_key()
    status_log(status_cb, "InfiniteTalk: uploading speaker image + hook audio...")
    image_url, _ = upload_media(Path(image_path), key)
    audio_url, _ = upload_media(Path(audio_path), key)
    prediction_id, _ = submit_infinitetalk(image_url, audio_url, key=key, prompt=prompt,
                                            resolution=resolution)
    outputs, _ = poll_wavespeed(prediction_id, key, timeout_s=900, interval_s=5,
                                cancel_event=cancel_event, status_cb=status_cb,
                                label="InfiniteTalk hook")
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    download_file(outputs[0], out_path)
    return out_path


# --- Sound-effect generation (Kling text-to-audio on WaveSpeed) ---------------
# Fallback when no fitting sound effect exists in the local library: describe the
# sound and generate it. Schema: {"prompt": <str>, "duration": <1-10 s>}.
KLING_SFX_MODEL = "kwaivgi/kling-text-to-audio"


def generate_sfx_clip(prompt, duration, out_path, key=None, cancel_event=None, status_cb=None):
    """Generate a sound effect from a text prompt via Kling text-to-audio.

    `duration` is clamped to 1-10 seconds. Returns the downloaded audio Path.
    """
    key = key or api_key()
    dur = int(max(1, min(10, round(float(duration or 2)))))
    payload = {"prompt": str(prompt or "").strip()[:500], "duration": dur}
    status_log(status_cb, f"Generating SFX ({dur}s): {payload['prompt'][:60]}...")
    response = request_json("POST", f"{API_BASE}/{KLING_SFX_MODEL}", key, payload, timeout=180)
    prediction_id = unwrap_id(response)
    outputs, _ = poll_wavespeed(prediction_id, key, timeout_s=420, cancel_event=cancel_event,
                                status_cb=status_cb, label="SFX generation")
    out_path = Path(out_path)
    ext = output_extension(outputs[0], out_path.suffix or ".wav")
    if out_path.suffix.lower() != ext.lower():
        out_path = out_path.with_suffix(ext)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    download_file(outputs[0], out_path)
    return out_path


class PipelineCancelled(RuntimeError):
    pass


def cancellation_requested(event):
    return bool(event and getattr(event, "is_set", lambda: False)())


def check_cancel(config=None, cancel_event=None):
    event = cancel_event
    if event is None and isinstance(config, dict):
        event = config.get("_cancel_event")
    if cancellation_requested(event):
        raise PipelineCancelled("Run cancelled by user.")


def status_log(status_cb, message):
    if status_cb:
        status_cb(message)


def run_subprocess_with_cancel(cmd, config=None):
    proc = subprocess.Popen(cmd)
    try:
        while proc.poll() is None:
            check_cancel(config)
            time.sleep(0.25)
    except PipelineCancelled:
        proc.kill()
        proc.wait()
        raise
    if proc.returncode:
        raise subprocess.CalledProcessError(proc.returncode, cmd)


def sanitize_video_metadata(path, config=None):
    if config is not None and not bool(config.get("metadata_cleanup_enabled", True)):
        return {"enabled": False, "cleaned": False, "reason": "disabled"}
    input_path = Path(path)
    if not input_path.exists():
        return {"enabled": True, "cleaned": False, "reason": "missing_input", "path": str(input_path)}
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return {"enabled": True, "cleaned": False, "reason": "ffmpeg_not_found", "path": str(input_path)}
    temp_path = input_path.with_name(f"{input_path.stem}_metadata_clean{input_path.suffix}")
    temp_path.unlink(missing_ok=True)
    cmd = [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-i",
        str(input_path),
        "-map",
        "0",
        "-map_metadata",
        "-1",
        "-map_chapters",
        "-1",
        "-metadata",
        "title=",
        "-metadata",
        "artist=",
        "-metadata",
        "comment=",
        "-metadata",
        "description=",
        "-metadata",
        "synopsis=",
        "-metadata",
        "software=",
        "-metadata",
        "creation_time=",
        "-metadata:s:v:0",
        "handler_name=",
        "-metadata:s:a:0",
        "handler_name=",
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        "-bitexact",
        str(temp_path),
    ]
    run_subprocess_with_cancel(cmd, config)
    if not temp_path.exists() or temp_path.stat().st_size <= 0:
        temp_path.unlink(missing_ok=True)
        return {"enabled": True, "cleaned": False, "reason": "empty_output", "path": str(input_path)}
    temp_path.replace(input_path)
    return {
        "enabled": True,
        "cleaned": True,
        "path": str(input_path),
        "method": "ffmpeg_map_metadata_minus_one_map_chapters_minus_one_stream_copy",
        "scope": "standard_container_metadata_only",
    }


def clamp(value, low, high):
    return max(low, min(high, value))


def ease_in_out(x):
    x = clamp(x, 0.0, 1.0)
    return x * x * (3 - 2 * x)


def find_font(names):
    candidates = [
        Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts",
        Path("/usr/share/fonts/truetype/dejavu"),
        Path("/Library/Fonts"),
    ]
    for directory in candidates:
        for name in names:
            path = directory / name
            if path.exists():
                return str(path)
    return None


FONT_BOLD = find_font(["impact.ttf", "arialbd.ttf", "Arial Bold.ttf", "DejaVuSans-Bold.ttf"])
FONT_REGULAR = find_font(["arial.ttf", "DejaVuSans.ttf"])
FONT_CACHE = {}


def get_font(size, bold=True):
    key = (size, bold)
    if key not in FONT_CACHE:
        path = FONT_BOLD if bold else FONT_REGULAR
        FONT_CACHE[key] = ImageFont.truetype(path, size) if path else ImageFont.load_default()
    return FONT_CACHE[key]


def image_fit_cover(img, size, zoom=1.0, offset=(0, 0)):
    img = img.convert("RGB")
    tw, th = size
    iw, ih = img.size
    scale = max(tw / iw, th / ih) * zoom
    nw, nh = max(1, int(iw * scale)), max(1, int(ih * scale))
    resized = img.resize((nw, nh), Image.Resampling.LANCZOS)
    x = clamp((nw - tw) // 2 + int(offset[0]), 0, max(0, nw - tw))
    y = clamp((nh - th) // 2 + int(offset[1]), 0, max(0, nh - th))
    return resized.crop((x, y, x + tw, y + th))


def image_fit_contain(img, size, zoom=1.0, offset=(0, 0)):
    img = img.convert("RGB")
    tw, th = size
    background = image_fit_cover(img.filter(ImageFilter.GaussianBlur(18)), size, zoom=1.08)
    background = ImageEnhance.Brightness(background).enhance(0.55)
    iw, ih = img.size
    scale = min((tw * 0.94) / iw, (th * 0.88) / ih) * zoom
    nw, nh = max(1, int(iw * scale)), max(1, int(ih * scale))
    foreground = img.resize((nw, nh), Image.Resampling.LANCZOS)
    x = (tw - nw) // 2 + int(offset[0])
    y = (th - nh) // 2 + int(offset[1])
    shadow = Image.new("RGBA", (nw + 28, nh + 28), (0, 0, 0, 0))
    shadow_draw = ImageDraw.Draw(shadow)
    shadow_draw.rounded_rectangle((14, 14, nw + 14, nh + 14), radius=4, fill=(0, 0, 0, 90))
    shadow = shadow.filter(ImageFilter.GaussianBlur(12))
    canvas = background.convert("RGBA")
    canvas.alpha_composite(shadow, (x - 14, y - 14))
    canvas.paste(foreground, (x, y))
    return canvas.convert("RGB")


def tint(img, alpha=35, color=(10, 12, 13)):
    overlay = Image.new("RGBA", img.size, color + (alpha,))
    return Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")


def make_vignette(width, height):
    yy, xx = np.mgrid[0:height, 0:width]
    cx, cy = width / 2, height / 2
    dist = np.sqrt(((xx - cx) / (width * 0.72)) ** 2 + ((yy - cy) / (height * 0.62)) ** 2)
    alpha = np.clip((dist - 0.48) / 0.55, 0, 1) * 145
    arr = np.zeros((height, width, 4), dtype=np.uint8)
    arr[:, :, 3] = alpha.astype(np.uint8)
    return Image.fromarray(arr, "RGBA")


def add_grain(img, frame_no, strength=42):
    rng = np.random.default_rng(frame_no + 123)
    arr = np.asarray(img.convert("RGB")).astype(np.int16)
    noise = rng.normal(0, strength / 6, arr.shape[:2]).astype(np.int16)
    arr[:, :, 0] = np.clip(arr[:, :, 0] + noise, 0, 255)
    arr[:, :, 1] = np.clip(arr[:, :, 1] + noise, 0, 255)
    arr[:, :, 2] = np.clip(arr[:, :, 2] + noise, 0, 255)
    return Image.fromarray(arr.astype(np.uint8), "RGB")


def dust_overlay(img, t, width, height, intensity=0.45):
    rng = np.random.default_rng(int(t * 1000) + 77)
    overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    for _ in range(int(18 * intensity)):
        x = int(rng.integers(-160, width + 160))
        y = int(rng.integers(int(height * 0.54), height + 60))
        rx = int(rng.integers(40, 150))
        ry = int(rng.integers(12, 42))
        a = int(rng.integers(12, 36))
        draw.ellipse((x - rx, y - ry, x + rx, y + ry), fill=(210, 184, 132, a))
    return Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")


def draw_wrapped(draw, text, box, font, fill, stroke_fill, stroke_width):
    x0, y0, x1, y1 = box
    words = text.split()
    lines = []
    current = ""
    for word in words:
        candidate = (current + " " + word).strip()
        if draw.textbbox((0, 0), candidate, font=font, stroke_width=stroke_width)[2] <= x1 - x0:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    line_h = int(font.size * 1.05)
    y = y0 + max(0, (y1 - y0 - line_h * len(lines)) // 2)
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font, stroke_width=stroke_width)
        x = x0 + (x1 - x0 - (bbox[2] - bbox[0])) // 2
        draw.text((x, y), line, font=font, fill=fill, stroke_width=stroke_width, stroke_fill=stroke_fill)
        y += line_h


def draw_caption(img, text, y, width, height, font_size=None):
    if not text:
        return
    font_size = font_size or max(58, min(104, int(width * 0.083)))
    draw = ImageDraw.Draw(img)
    font = get_font(font_size, True)
    draw_wrapped(
        draw,
        text.upper(),
        (int(width * 0.07), int(y), int(width * 0.93), int(y + font_size * 2.5)),
        font,
        (255, 255, 255, 255),
        (0, 0, 0, 255),
        max(4, font_size // 15),
    )


# ---------------------------------------------------------------------------
# Viral word-by-word ("karaoke") caption system
# ---------------------------------------------------------------------------

CAPTION_ACCENT = (255, 219, 26)      # punchy yellow for the currently spoken word
CAPTION_BODY = (255, 255, 255)       # already-spoken / idle words
CAPTION_UPCOMING = (216, 220, 226)   # words not reached yet (slightly dimmed)


def _caption_word_weight(word):
    """Relative on-screen time for a word, blending length and syllable count."""
    bare = re.sub(r"[^a-z]", "", word.lower())
    if not bare:
        return 0.7
    vowel_groups = re.findall(r"[aeiouy]+", bare)
    syllables = max(1, len(vowel_groups))
    weight = 0.55 + 0.45 * syllables + 0.045 * len(bare)
    # Trailing sentence punctuation earns a small extra beat (natural pause).
    if word.strip()[-1:] in ".!?:":
        weight += 0.45
    return weight


def build_caption_chunks(text, duration, max_words=3, uppercase=True, word_times=None):
    """Turn a spoken line into timed caption chunks (1-`max_words` words each).

    When `word_times` (frame-accurate [{word,start,end}] in scene-local seconds) is
    given, the words land exactly on the voice like a real edit. Otherwise each
    word's window is estimated inside the scene by length/syllable weight.
    """
    if duration <= 0:
        return []
    spans = []
    if word_times:
        for wt in word_times:
            word = (wt.get("word") or "").strip()
            if not word:
                continue
            start = max(0.0, float(wt.get("start", 0.0)))
            spans.append({
                "text": word.upper() if uppercase else word,
                "start": start,
                "end": max(start + 0.05, float(wt.get("end", start))),
            })
    else:
        text = (text or "").strip()
        raw_words = [w for w in re.split(r"\s+", text) if w.strip()] if text else []
        if raw_words:
            words = [w.upper() if uppercase else w for w in raw_words]
            weights = [_caption_word_weight(w) for w in raw_words]
            total = sum(weights) or 1.0
            lead = min(0.10, duration * 0.05)
            tail = min(0.12, duration * 0.05)
            usable = max(0.10, duration - lead - tail)
            cursor = lead
            for word, weight in zip(words, weights):
                span_d = usable * weight / total
                spans.append({"text": word, "start": cursor, "end": cursor + span_d})
                cursor += span_d
    if not spans:
        return []
    chunks = []
    for i in range(0, len(spans), max(1, max_words)):
        group = spans[i:i + max_words]
        chunks.append({"start": group[0]["start"], "end": group[-1]["end"], "words": group})
    if not chunks:
        return []
    # First chunk visible from the very start; no gaps between chunks; last lingers.
    chunks[0]["start"] = 0.0
    for i in range(len(chunks) - 1):
        chunks[i]["end"] = chunks[i + 1]["start"]
    chunks[-1]["end"] = duration
    return chunks


def _caption_color_for(word, local):
    if local >= word["end"]:
        return CAPTION_BODY        # already spoken
    if local >= word["start"]:
        return CAPTION_ACCENT      # active word
    return CAPTION_UPCOMING        # not reached yet


def _draw_caption_word(draw, text, cx, cy, font, color, alpha, stroke):
    """Draw one centered word with a heavy stroke + drop shadow for readability."""
    shadow_a = int(alpha * 0.5)
    draw.text((cx + 4, cy + 5), text, font=font, anchor="mm",
              fill=(0, 0, 0, shadow_a), stroke_width=stroke, stroke_fill=(0, 0, 0, shadow_a))
    draw.text((cx, cy), text, font=font, anchor="mm",
              fill=(color[0], color[1], color[2], alpha),
              stroke_width=stroke, stroke_fill=(0, 0, 0, alpha))


def draw_animated_caption(base, chunks, local, width, height, config, is_hook=False):
    """Render the active caption chunk with word-by-word karaoke highlighting."""
    if not chunks:
        return base
    chunk = next((c for c in chunks if c["start"] <= local < c["end"]), None)
    if chunk is None:
        if local >= chunks[-1]["end"]:
            chunk = chunks[-1]
        else:
            return base

    base_size = int(config.get("caption_size") or max(54, min(110, int(width * 0.076))))
    if is_hook:
        base_size = int(base_size * 1.12)
    font = get_font(base_size, True)
    stroke = max(5, base_size // 9)
    line_h = int(base_size * 1.16)
    max_text_width = width * 0.86
    center_rel = float(config.get("caption_center_y", 0.50 if is_hook else 0.72))
    center_y = int(height * center_rel)

    overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    space_w = draw.textlength(" ", font=font)

    # Wrap chunk words to fit the safe width.
    lines = []
    current = []
    current_w = 0.0
    for word in chunk["words"]:
        ww = draw.textlength(word["text"], font=font)
        add = ww if not current else ww + space_w
        if current and current_w + add > max_text_width:
            lines.append(current)
            current = [word]
            current_w = ww
        else:
            current.append(word)
            current_w += add
    if current:
        lines.append(current)

    # Entrance (slide-up + fade) and gentle exit fade.
    chunk_age = local - chunk["start"]
    enter = clamp(chunk_age / 0.14, 0.0, 1.0)
    exit_fade = clamp((chunk["end"] - local) / 0.10, 0.0, 1.0)
    block_alpha = enter * exit_fade
    y_slide = int((1.0 - ease_in_out(enter)) * 26)

    total_h = len(lines) * line_h
    top = center_y - total_h // 2 + y_slide

    for li, line in enumerate(lines):
        widths = [draw.textlength(w["text"], font=font) for w in line]
        line_w = sum(widths) + space_w * (len(line) - 1)
        x = (width - line_w) / 2.0
        cy = top + li * line_h + line_h // 2
        for word, ww in zip(line, widths):
            cx = x + ww / 2.0
            color = _caption_color_for(word, local)
            word_font = font
            # Active word "pop": briefly larger right after it becomes spoken.
            if color is CAPTION_ACCENT:
                pop_age = local - word["start"]
                pop = 1.0 + 0.16 * max(0.0, 1.0 - pop_age / 0.18)
                if pop > 1.01:
                    word_font = get_font(int(base_size * pop), True)
            a = int(255 * block_alpha * (1.0 if color is not CAPTION_UPCOMING else 0.82))
            _draw_caption_word(draw, word["text"], int(cx), int(cy), word_font, color, a, stroke)
            x += ww + space_w

    return Image.alpha_composite(base.convert("RGBA"), overlay).convert("RGB")


def render_caption_overlay(chunk, width, height, config, is_hook=False):
    """Render one caption chunk as a full-frame transparent RGBA overlay (neutral white)."""
    base_size = int(config.get("caption_size") or max(54, min(110, int(width * 0.076))))
    if is_hook:
        base_size = int(base_size * 1.12)
    font = get_font(base_size, True)
    stroke = max(5, base_size // 9)
    line_h = int(base_size * 1.16)
    max_text_width = width * 0.86
    center_rel = float(config.get("caption_center_y", 0.50 if is_hook else 0.72))
    center_y = int(height * center_rel)

    overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    space_w = draw.textlength(" ", font=font)
    lines, current, current_w = [], [], 0.0
    for word in chunk["words"]:
        ww = draw.textlength(word["text"], font=font)
        add = ww if not current else ww + space_w
        if current and current_w + add > max_text_width:
            lines.append(current)
            current, current_w = [word], ww
        else:
            current.append(word)
            current_w += add
    if current:
        lines.append(current)

    top = center_y - (len(lines) * line_h) // 2
    for li, line in enumerate(lines):
        widths = [draw.textlength(w["text"], font=font) for w in line]
        line_w = sum(widths) + space_w * (len(line) - 1)
        x = (width - line_w) / 2.0
        cy = top + li * line_h + line_h // 2
        for word, ww in zip(line, widths):
            _draw_caption_word(draw, word["text"], int(x + ww / 2.0), int(cy), font, CAPTION_BODY, 255, stroke)
            x += ww + space_w
    return overlay


def export_caption_pngs(config, caption_chunks_by_scene, scenes, first_scene_id, width, height, out_dir):
    """Save each on-screen caption as its own transparent PNG plus a timing manifest."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = []
    idx = 0
    for i, scene in enumerate(scenes, 1):
        sid = scene.get("id", str(i))
        s0 = float(scene["start"])
        for chunk in caption_chunks_by_scene.get(sid, []):
            text = " ".join(w["text"] for w in chunk["words"]).strip()
            if not text:
                continue
            idx += 1
            abs_start = round(s0 + float(chunk["start"]), 3)
            abs_end = round(s0 + float(chunk["end"]), 3)
            image = render_caption_overlay(chunk, width, height, config, is_hook=(sid == first_scene_id))
            fname = f"caption_{idx:03d}_{abs_start:07.2f}s.png"
            image.save(out_dir / fname)
            manifest.append({
                "file": fname, "index": idx, "start": abs_start, "end": abs_end, "text": text,
                "words": [{"word": w["text"], "start": round(s0 + float(w["start"]), 3),
                           "end": round(s0 + float(w["end"]), 3)} for w in chunk["words"]],
            })
    (out_dir / "captions.json").write_text(
        json.dumps({"width": width, "height": height, "count": len(manifest), "captions": manifest}, indent=2),
        encoding="utf-8",
    )
    return manifest


def pulse_at(p, center, width):
    if width <= 0:
        return 0.0
    d = abs(p - center) / width
    if d >= 1:
        return 0.0
    return (1 - d) * (1 - d)


def scene_punch_zoom(config, scene, p):
    if not bool(config.get("kinetic_style", False)):
        return 0.0
    amount = float(scene.get("punch_zoom", config.get("punch_zoom", 0.0)))
    if amount <= 0:
        return 0.0
    centers = scene.get("punch_points", [0.18])
    return amount * max(pulse_at(p, float(center), 0.075) for center in centers)


def opening_punch_zoom(config, t, local, is_first_scene):
    """Additive zoom that snaps in at each cut (harder on the opening hook), then settles.

    Always-on pacing energy: every scene start gets a quick punch-in; the first scene also
    gets a stronger hook hold so the opening frame stops the scroll. Returns a zoom amount >= 0
    that is added on top of the normal Ken Burns / clip zoom. Stays >= 0 so `cover` framing
    never reveals edges.
    """
    if not bool(config.get("dynamic_zoom", True)):
        return 0.0
    extra = 0.0
    settle = float(config.get("cut_punch_seconds", 0.34))
    cut_amount = float(config.get("cut_punch_amount", 0.06))
    if cut_amount > 0 and settle > 0 and local < settle:
        extra += cut_amount * (1.0 - ease_in_out(clamp(local / settle, 0.0, 1.0)))
    if is_first_scene:
        hold = float(config.get("hook_hold_seconds", 0.6))
        hook_amount = float(config.get("hook_punch_amount", 0.12))
        if hook_amount > 0 and hold > 0 and t < hold:
            extra += hook_amount * (1.0 - ease_in_out(clamp(t / hold, 0.0, 1.0)))
    return extra


def draw_arrow(draw, start, end, fill, width=8):
    sx, sy = start
    ex, ey = end
    draw.line((sx, sy, ex, ey), fill=fill, width=width)
    angle = math.atan2(ey - sy, ex - sx)
    head = max(22, width * 4)
    wing = math.pi * 0.82
    p1 = (ex - head * math.cos(angle - wing), ey - head * math.sin(angle - wing))
    p2 = (ex - head * math.cos(angle + wing), ey - head * math.sin(angle + wing))
    draw.polygon((end, p1, p2), fill=fill)


def draw_ring(draw, cx, cy, rx, ry, fill, width=8, dash=26):
    if dash <= 0:
        draw.ellipse((cx - rx, cy - ry, cx + rx, cy + ry), outline=fill, width=width)
        return
    for start in range(0, 360, dash * 2):
        draw.arc((cx - rx, cy - ry, cx + rx, cy + ry), start=start, end=start + dash, fill=fill, width=width)


def draw_speed_lines(draw, width, height, side="left", fill=(255, 244, 210, 145)):
    y_values = [0.58, 0.64, 0.70, 0.77]
    for i, y_rel in enumerate(y_values):
        y = int(height * y_rel)
        if side == "right":
            x0 = int(width * (0.82 + i * 0.015))
            x1 = int(width * (0.98 + i * 0.01))
        else:
            x0 = int(width * (0.02 - i * 0.01))
            x1 = int(width * (0.20 + i * 0.02))
        draw.line((x0, y, x1, y - 20), fill=fill, width=7)


def draw_kinetic_annotations(img, scene, p, frame_no, width, height, config):
    if not bool(config.get("kinetic_style", False)):
        return img
    annotation = scene.get("annotation")
    if not annotation:
        return img
    kind = annotation.get("type", annotation) if isinstance(annotation, dict) else annotation
    intensity = float(scene.get("annotation_intensity", config.get("annotation_intensity", 0.62)))
    fade = clamp(min(p * 4.0, 1.0) * min((1.0 - p) * 5.0, 1.0) + 0.18, 0.0, 1.0)
    pulse = 0.75 + 0.25 * math.sin(frame_no * 0.34)
    alpha = int(220 * intensity * fade * pulse)
    red = (230, 38, 34, alpha)
    cream = (255, 244, 210, int(150 * intensity * fade))
    dark = (22, 18, 14, int(90 * intensity * fade))
    overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    if kind == "chase":
        draw_ring(draw, int(width * 0.30), int(height * 0.31), int(width * 0.17), int(height * 0.10), red, 8)
        draw_arrow(draw, (int(width * 0.18), int(height * 0.78)), (int(width * 0.40), int(height * 0.62)), red, 8)
        draw_arrow(draw, (int(width * 0.78), int(height * 0.80)), (int(width * 0.63), int(height * 0.61)), red, 8)
        draw_speed_lines(draw, width, height, "left", cream)
    elif kind == "scale":
        for cx, cy, rx, ry in [(0.28, 0.62, 0.15, 0.07), (0.58, 0.58, 0.18, 0.08), (0.78, 0.69, 0.14, 0.06)]:
            draw_ring(draw, int(width * cx), int(height * cy), int(width * rx), int(height * ry), red, 6)
    elif kind == "help":
        draw_ring(draw, int(width * 0.31), int(height * 0.73), int(width * 0.23), int(height * 0.10), red, 7)
        draw_arrow(draw, (int(width * 0.66), int(height * 0.44)), (int(width * 0.40), int(height * 0.66)), red, 7)
    elif kind == "map":
        points = [(0.22, 0.70), (0.42, 0.58), (0.58, 0.52), (0.76, 0.35)]
        last = None
        for x_rel, y_rel in points:
            current = (int(width * x_rel), int(height * y_rel))
            draw.ellipse((current[0] - 10, current[1] - 10, current[0] + 10, current[1] + 10), fill=red)
            if last:
                draw_arrow(draw, last, current, red, 5)
            last = current
        draw.rectangle((int(width * 0.06), int(height * 0.13), int(width * 0.94), int(height * 0.88)), outline=dark, width=5)
    elif kind == "scatter":
        center = (int(width * 0.48), int(height * 0.60))
        for target in [(0.24, 0.48), (0.73, 0.44), (0.82, 0.66), (0.36, 0.76)]:
            draw_arrow(draw, center, (int(width * target[0]), int(height * target[1])), red, 7)
    elif kind == "truck":
        draw_speed_lines(draw, width, height, "right", cream)
        draw_ring(draw, int(width * 0.42), int(height * 0.51), int(width * 0.22), int(height * 0.12), red, 7)
        draw_arrow(draw, (int(width * 0.46), int(height * 0.72)), (int(width * 0.70), int(height * 0.60)), red, 7)
    elif kind == "aftermath":
        draw_ring(draw, int(width * 0.50), int(height * 0.62), int(width * 0.20), int(height * 0.24), red, 7)
        draw.line((int(width * 0.16), int(height * 0.80), int(width * 0.30), int(height * 0.70)), fill=cream, width=6)
        draw.line((int(width * 0.30), int(height * 0.70), int(width * 0.43), int(height * 0.82)), fill=cream, width=6)
    elif kind == "payoff":
        draw_ring(draw, int(width * 0.51), int(height * 0.40), int(width * 0.25), int(height * 0.18), red, 8)
        for angle in [-0.8, -0.35, 0.35, 0.8]:
            x0 = int(width * 0.51 + math.cos(angle) * width * 0.25)
            y0 = int(height * 0.40 + math.sin(angle) * height * 0.18)
            x1 = int(width * 0.51 + math.cos(angle) * width * 0.34)
            y1 = int(height * 0.40 + math.sin(angle) * height * 0.25)
            draw.line((x0, y0, x1, y1), fill=cream, width=7)

    return Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")


def draw_text_panel(draw, text, box, font_size, fill=(255, 246, 220, 245), ink=(25, 20, 15, 255), stroke=(120, 18, 12, 255)):
    x0, y0, x1, y1 = box
    draw.rounded_rectangle((x0, y0, x1, y1), radius=8, fill=fill, outline=stroke, width=8)
    font = get_font(font_size, True)
    draw_wrapped(draw, text, (x0 + 28, y0 + 28, x1 - 28, y1 - 28), font, ink, (255, 255, 255, 0), 0)


def draw_counter(draw, text, x, y, width, height, alpha=235):
    draw.rounded_rectangle((x, y, x + width, y + height), radius=8, fill=(18, 17, 15, alpha), outline=(235, 220, 180, alpha), width=6)
    font = get_font(int(height * 0.44), True)
    draw_wrapped(draw, text, (x + 20, y + 12, x + width - 20, y + height - 10), font, (255, 244, 220, alpha), (0, 0, 0, alpha), 4)


def draw_smart_overlays(img, scene, shot, p, frame_no, width, height, config):
    if not bool(config.get("smart_overlays", True)):
        return img
    overlay_specs = []
    if shot and shot.get("overlays"):
        overlay_specs.extend(shot["overlays"])
    if scene.get("overlays"):
        overlay_specs.extend(scene["overlays"])
    if not overlay_specs:
        return img
    overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    for spec in overlay_specs:
        kind = spec.get("type")
        start = float(spec.get("start", 0.0))
        end = float(spec.get("end", 1.0))
        if not (start <= p <= end):
            continue
        local = 0.0 if end <= start else (p - start) / (end - start)
        hit = ease_in_out(clamp(local * 4.0, 0.0, 1.0))
        jitter = math.sin(frame_no * 0.55) * float(spec.get("shake", 0.0))
        if kind == "paper":
            w = int(width * float(spec.get("w", 0.72)))
            h = int(height * float(spec.get("h", 0.16)))
            x = int(width * float(spec.get("x", 0.14)))
            target_y = int(height * float(spec.get("y", 0.20)))
            y = int(target_y - (1.0 - hit) * height * 0.16 + jitter)
            draw_text_panel(draw, spec.get("text", ""), (x, y, x + w, y + h), int(spec.get("font", 66)))
        elif kind == "stamp":
            font = get_font(int(spec.get("font", 72)), True)
            text = spec.get("text", "")
            x = int(width * float(spec.get("x", 0.12)))
            x = int(x + (1.0 - hit) * width * 0.12)
            y = int(height * float(spec.get("y", 0.44)) + jitter)
            bbox = draw.textbbox((0, 0), text, font=font, stroke_width=5)
            pad = 28
            box = (x, y, x + bbox[2] - bbox[0] + pad * 2, y + bbox[3] - bbox[1] + pad * 2)
            stamp = Image.new("RGBA", (box[2] - box[0], box[3] - box[1]), (0, 0, 0, 0))
            sd = ImageDraw.Draw(stamp)
            sd.rectangle((0, 0, stamp.width - 1, stamp.height - 1), outline=(160, 20, 15, 230), width=8)
            sd.text((pad, pad - 4), text, font=font, fill=(170, 24, 18, 235), stroke_width=3, stroke_fill=(255, 235, 210, 180))
            stamp = stamp.rotate(float(spec.get("rotate", -8)), resample=Image.Resampling.BICUBIC, expand=True)
            overlay.alpha_composite(stamp, (box[0], box[1]))
        elif kind == "counter":
            values = spec.get("values", ["10,000 ROUNDS"])
            idx = min(len(values) - 1, int(local * len(values)))
            x = int(width * float(spec.get("x", 0.10)))
            y = int(height * float(spec.get("y", 0.16)))
            draw_counter(draw, values[idx], x, y, int(width * float(spec.get("w", 0.80))), int(height * float(spec.get("h", 0.13))))
        elif kind == "newspaper":
            w = int(width * float(spec.get("w", 0.78)))
            h = int(height * float(spec.get("h", 0.18)))
            x = int(width * float(spec.get("x", 0.11)))
            y = int(height * float(spec.get("y", 0.52)) + (1.0 - hit) * height * 0.12 + jitter)
            draw_text_panel(draw, spec.get("text", "OPERATION FAILS"), (x, y, x + w, y + h), int(spec.get("font", 70)), fill=(236, 228, 205, 245), ink=(22, 18, 12, 255), stroke=(50, 45, 36, 255))
        elif kind == "highlight":
            cx = int(width * float(spec.get("cx", 0.50)))
            cy = int(height * float(spec.get("cy", 0.55)))
            rx = int(width * float(spec.get("rx", 0.18)))
            ry = int(height * float(spec.get("ry", 0.10)))
            draw_ring(draw, cx, cy, rx, ry, (225, 32, 25, int(220 * hit)), width=8, dash=0)
        elif kind == "arrows":
            for item in spec.get("items", []):
                draw_arrow(
                    draw,
                    (int(width * item[0]), int(height * item[1])),
                    (int(width * item[2]), int(height * item[3])),
                    (225, 32, 25, int(220 * hit)),
                    width=7,
                )
    return Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")


def load_config(path):
    config_path = Path(path)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["_config_path"] = str(config_path.resolve())
    return config


def project_paths(config):
    slug = config["project_slug"]
    project_dir = ROOT / "projects" / slug
    asset_dir = project_dir / "gpt images"
    clip_dir = project_dir / "seedance 2.0"
    render_dir = project_dir / "renders"
    review_dir = project_dir / "review"
    web_dir = project_dir / "web images"
    input_dir = project_dir / "input"
    config_dir = project_dir / "config"
    for directory in (asset_dir, clip_dir, web_dir, input_dir, config_dir, render_dir, review_dir):
        directory.mkdir(parents=True, exist_ok=True)
    return project_dir, asset_dir, render_dir, review_dir


def clip_dir_for(config):
    project_dir = ROOT / "projects" / config["project_slug"]
    clip_dir = project_dir / "seedance 2.0"
    clip_dir.mkdir(parents=True, exist_ok=True)
    return clip_dir


def wavespeed_media_dir_for(config):
    project_dir = ROOT / "projects" / config["project_slug"]
    media_dir = project_dir / "wavespeed_media"
    media_dir.mkdir(parents=True, exist_ok=True)
    return media_dir


def web_images_dir_for(config):
    project_dir = ROOT / "projects" / config["project_slug"]
    web_dir = project_dir / "web images"
    web_dir.mkdir(parents=True, exist_ok=True)
    return web_dir


def scene_uses_seedance(config, scene):
    if not bool(config.get("use_seedance_clips", True)):
        return False
    return bool(scene.get("seedance", config.get("seedance_default", True)))


def seedance_clip_start_trim(config, scene=None):
    try:
        value = float((scene or {}).get("seedance_start_trim", config.get("seedance_clip_start_trim", 0.5)))
    except (TypeError, ValueError, AttributeError):
        value = 0.5
    return max(0.0, min(value, 3.0))


def scene_renders_caption(config, scene):
    if not bool(config.get("render_captions", True)):
        return False
    return bool(scene.get("render_caption", True))


def build_scene_prompt(config, scene):
    global_style = config.get("visual_style", "")
    constraints = config.get("global_constraints", "")
    scene_prompt = scene.get("prompt") or scene.get("visual") or scene.get("script", "")
    return "\n".join(
        part
        for part in [
            f"Use case: {config.get('use_case', 'historical-scene')}",
            "Asset type: vertical YouTube Short scene plate",
            f"Short topic: {config.get('title', config.get('project_slug'))}",
            f"Scene: {scene.get('name', scene.get('id'))}",
            f"Primary request: {scene_prompt}",
            f"Script beat: {scene.get('script', '')}",
            f"Topic lock: visible subjects, objects, places, era, and action must clearly support the Short topic '{config.get('title', config.get('project_slug'))}' and this exact script beat.",
            f"Style/medium: {global_style}",
            "Priority: the voice/text script beat is authoritative. First judge whether visual-direction notes fit the spoken line. Use them only when they improve clarity, pacing, or cinematic impact; otherwise replace them with a better script-matched visual idea.",
            "Composition/framing: vertical 9:16, one coherent scene or subject, main subject readable on a phone, clean safe space for captions.",
            "Avoid collage, split-screen, grid, poster-board, scrapbook, comic panels, multiple unrelated mini-scenes, or many inset images unless this scene explicitly asks for a document board.",
            "Constraints: no embedded text, no captions, no labels, no watermark, no logo unless explicitly requested.",
            constraints,
        ]
        if part
    )


def build_video_prompt(config, scene):
    prompt = scene.get("video_prompt")
    if prompt:
        base = prompt
    else:
        base = " ".join(
            part
            for part in [
                scene.get("prompt", ""),
                f"Script beat: {scene.get('script', '')}",
                scene.get("motion_prompt", ""),
            ]
            if part
        )
    style = config.get("video_style") or config.get("visual_style", "")
    constraints = (
        f"Topic lock: '{config.get('title', config.get('project_slug'))}', exact script beat only. "
        "Preserve the source image while adding real physical motion and camera parallax, not just zoom. "
        "No speech/voices/dialogue/narration. No captions/text/watermark/logo/gore."
    )
    return "\n".join(part for part in [base, f"Style: {style}", constraints] if part)[:1800]


def api_key():
    key = os.environ.get("WAVESPEED_API_KEY", "").strip()
    if not key:
        raise RuntimeError("WAVESPEED_API_KEY is not set. Set it only as an environment variable.")
    return key


def request_json(method, url, key, payload=None, timeout=180):
    data = None
    headers = {"Authorization": f"Bearer {key}"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} from {url}: {raw}") from exc


def request_multipart_upload(url, key, path, timeout=240):
    boundary = f"----shorts-ai-agent-{int(time.time() * 1000)}"
    suffix = path.suffix.lower()
    content_type = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".mp3": "audio/mpeg",
        ".wav": "audio/wav",
        ".m4a": "audio/mp4",
        ".aac": "audio/aac",
        ".ogg": "audio/ogg",
        ".flac": "audio/flac",
        ".webm": "audio/webm",
    }.get(suffix, "application/octet-stream")
    file_bytes = path.read_bytes()
    header = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{path.name}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode("utf-8")
    footer = f"\r\n--{boundary}--\r\n".encode("utf-8")
    body = header + file_bytes + footer
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(body)),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} from {url}: {raw}") from exc


def unwrap_id(response):
    if response.get("id"):
        return response["id"]
    data = response.get("data")
    if isinstance(data, dict) and data.get("id"):
        return data["id"]
    raise RuntimeError(f"No prediction id in response: {response}")


def unwrap_data(response):
    data = response.get("data")
    return data if isinstance(data, dict) else response


def submit_wavespeed_image(prompt, config, key):
    wavespeed = config.get("wavespeed", {})
    model = wavespeed.get("image_model", DEFAULT_IMAGE_MODEL)
    payload = {
        "aspect_ratio": wavespeed.get("aspect_ratio", "9:16"),
        "enable_base64_output": False,
        "enable_sync_mode": False,
        "output_format": wavespeed.get("output_format", "png"),
        "prompt": make_prompt_safe(prompt),
    }
    # quality/resolution are gpt-image-2 knobs; other models (e.g. nano-banana-2)
    # can reject unknown params, so only send them for gpt-image models.
    if "gpt-image" in model:
        payload["quality"] = "low"
        payload["resolution"] = "1k"
    response = request_json("POST", f"{API_BASE}/{model}", key, payload)
    return unwrap_id(response), response


def upload_media(path, key):
    response = request_multipart_upload(f"{API_BASE}/media/upload/binary", key, path)
    data = unwrap_data(response)
    url = data.get("download_url") or data.get("url") or data.get("file_url")
    if not url:
        raise RuntimeError(f"No uploaded media URL in response: {response}")
    return url, response


def submit_wavespeed_clip(image_url, prompt, duration, config, scene, key):
    wavespeed = config.get("wavespeed", {})
    model = scene.get("video_model", wavespeed.get("video_model", DEFAULT_VIDEO_MODEL))
    if scene.get("max_duration") is not None:
        duration = min(duration, float(scene["max_duration"]))
    payload = {
        "aspect_ratio": wavespeed.get("video_aspect_ratio", wavespeed.get("aspect_ratio", "9:16")),
        "duration": int(clamp(int(math.ceil(duration)), 4, 15)),
        "image": image_url,
        "prompt": make_prompt_safe(prompt),
        "resolution": scene.get("video_resolution", wavespeed.get("video_resolution", "480p")),
        "seed": int(scene.get("seed", wavespeed.get("seed", -1))),
    }
    # enable_web_search / generate_audio are Seedance-specific; other I2V models
    # (LTX, Happy Horse, ...) can reject unknown params, so only send for Seedance.
    if "seedance" in str(model):
        payload["enable_web_search"] = bool(scene.get("video_enable_web_search", wavespeed.get("video_enable_web_search", False)))
        payload["generate_audio"] = bool(wavespeed.get("video_generate_audio", True))
    if scene.get("last_image"):
        payload["last_image"] = scene["last_image"]
    response = request_json("POST", f"{API_BASE}/{model}", key, payload, timeout=240)
    return unwrap_id(response), response, payload


def poll_wavespeed(prediction_id, key, timeout_s=420, interval_s=2, cancel_event=None, status_cb=None, label="WaveSpeed job"):
    result_url = f"{API_BASE}/predictions/{urllib.parse.quote(prediction_id)}/result"
    start = time.time()
    last = None
    last_wait_log = 0.0
    while True:
        check_cancel(cancel_event=cancel_event)
        response = request_json("GET", result_url, key)
        data = unwrap_data(response)
        status = data.get("status")
        if status != last:
            print(f"  status: {status}")
            status_log(status_cb, f"{label}: {status}")
            last = status
            last_wait_log = time.time()
        elif time.time() - last_wait_log > 20:
            status_log(status_cb, f"{label}: waiting for completion ({int(time.time() - start)}s elapsed)...")
            last_wait_log = time.time()
        if status == "completed":
            outputs = data.get("outputs") or data.get("output") or response.get("outputs")
            if isinstance(outputs, str):
                outputs = [outputs]
            if outputs:
                status_log(status_cb, f"{label}: completed.")
                return outputs, response
            raise RuntimeError(f"Completed but no outputs: {response}")
        if status == "failed":
            raise RuntimeError(f"Prediction failed: {response}")
        if time.time() - start > timeout_s:
            raise TimeoutError(f"Timed out waiting for prediction {prediction_id}")
        time.sleep(interval_s)


def download_file(url, path):
    req = urllib.request.Request(url, headers={"User-Agent": "shorts-ai-agent-app/1.0"})
    with urllib.request.urlopen(req, timeout=240) as resp:
        content = resp.read()
    path.write_bytes(content)
    return len(content)


def output_extension(url, fallback):
    parsed = urllib.parse.urlparse(url)
    suffix = Path(parsed.path).suffix.lower()
    if suffix and len(suffix) <= 8:
        return suffix
    return fallback


def download_wavespeed_outputs(outputs, primary_path, media_dir, stem, fallback_ext, cancel_event=None):
    records = []
    for output_index, url in enumerate(outputs, 1):
        check_cancel(cancel_event=cancel_event)
        ext = output_extension(url, fallback_ext)
        local_path = media_dir / f"{stem}_output_{output_index:02d}{ext}"
        size = download_file(url, local_path)
        records.append({"index": output_index, "url": url, "local_path": str(local_path), "bytes": size})
    if records:
        shutil.copyfile(records[0]["local_path"], primary_path)
        records[0]["primary_path"] = str(primary_path)
    return records


def ensure_archived_media(source_path, media_dir, stem):
    if not source_path.exists():
        return []
    media_dir.mkdir(parents=True, exist_ok=True)
    target = media_dir / f"{stem}_output_01{source_path.suffix.lower()}"
    if source_path.resolve() != target.resolve():
        shutil.copyfile(source_path, target)
    return [
        {
            "index": 1,
            "url": None,
            "local_path": str(target),
            "bytes": target.stat().st_size,
            "primary_path": str(source_path),
        }
    ]


def wavespeed_parallelism(config, key, default=2, maximum=8):
    try:
        value = int((config.get("wavespeed", {}) or {}).get(key, default))
    except (TypeError, ValueError, AttributeError):
        value = default
    return max(1, min(maximum, value))


def generate_assets(config, force=False, status_cb=None):
    check_cancel(config)
    _, asset_dir, _, _ = project_paths(config)
    media_dir = wavespeed_media_dir_for(config) / "images"
    media_dir.mkdir(parents=True, exist_ok=True)
    key = api_key()
    manifest_records = []
    pending_jobs = []
    scenes = config["scenes"]
    cancel_event = config.get("_cancel_event")
    print(f"Generating {len(scenes)} scene assets into {asset_dir}")
    for index, scene in enumerate(scenes, 1):
        check_cancel(config)
        filename = scene.get("asset") or f"scene_{index:02d}.png"
        out_path = asset_dir / filename
        if scene.get("needs_gpt_asset") is False:
            print(f"[{index}/{len(scenes)}] skip GPT image for {filename} (not needed)")
            status_log(status_cb, f"GPT image {index}/{len(scenes)} skipped; not needed.")
            downloads = ensure_archived_media(out_path, media_dir, Path(filename).stem) if out_path.exists() else []
            manifest_records.append(
                {
                    "_order": index,
                    "scene": scene.get("id", index),
                    "asset": str(out_path),
                    "skipped": True,
                    "reason": "not_needed",
                    "downloads": downloads,
                }
            )
            continue
        if out_path.exists() and not force:
            print(f"[{index}/{len(scenes)}] skip existing {filename}")
            status_log(status_cb, f"GPT image {index}/{len(scenes)} skipped; existing file is reused.")
            downloads = ensure_archived_media(out_path, media_dir, Path(filename).stem)
            manifest_records.append(
                {
                    "_order": index,
                    "scene": scene.get("id", index),
                    "asset": str(out_path),
                    "skipped": True,
                    "downloads": downloads,
                }
            )
            continue
        prompt = build_scene_prompt(config, scene)
        print(f"[{index}/{len(scenes)}] {filename}")
        pending_jobs.append((index, scene, filename, out_path, prompt))

    def run_image_job(job):
        index, scene, filename, out_path, prompt = job
        check_cancel(config)
        status_log(status_cb, f"GPT image {index}/{len(scenes)}: submitting {filename}...")
        prediction_id, submit_response = submit_wavespeed_image(prompt, config, key)
        print(f"  id: {prediction_id}")
        status_log(status_cb, f"Waiting for GPT Image {index}/{len(scenes)} from WaveSpeed...")
        outputs, result_response = poll_wavespeed(
            prediction_id,
            key,
            cancel_event=cancel_event,
            status_cb=status_cb,
            label=f"GPT image {index}/{len(scenes)}",
        )
        downloads = download_wavespeed_outputs(outputs, out_path, media_dir, Path(filename).stem, ".png", cancel_event=cancel_event)
        size = downloads[0]["bytes"]
        print(f"  wrote {out_path} ({size} bytes); archived {len(downloads)} WaveSpeed output(s)")
        status_log(status_cb, f"GPT image {index}/{len(scenes)} saved: {out_path.name}.")
        return {
            "_order": index,
            "scene": scene.get("id", index),
            "asset": str(out_path),
            "prediction_id": prediction_id,
            "output_url": outputs[0],
            "bytes": size,
            "downloads": downloads,
            "submit_response": submit_response,
            "result_response": result_response,
        }

    if pending_jobs:
        workers = wavespeed_parallelism(config, "image_concurrency", default=4, maximum=8)
        status_log(status_cb, f"Submitting {len(pending_jobs)} GPT image job(s) with concurrency {workers}...")
        if workers == 1 or len(pending_jobs) == 1:
            for job in pending_jobs:
                manifest_records.append(run_image_job(job))
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
                futures = [executor.submit(run_image_job, job) for job in pending_jobs]
                for future in concurrent.futures.as_completed(futures):
                    check_cancel(config)
                    manifest_records.append(future.result())
    manifest_path = asset_dir / "wavespeed_manifest.json"
    manifest = [
        {key: value for key, value in record.items() if key != "_order"}
        for record in sorted(manifest_records, key=lambda item: item.get("_order", 0))
    ]
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest_path


def clip_filename(scene, index):
    clip = scene.get("clip")
    if clip:
        return Path(clip).name
    asset = scene.get("asset")
    if asset:
        return f"{Path(asset).stem}.mp4"
    return f"scene_{index:02d}.mp4"


def generate_clips(config, force=False, status_cb=None):
    check_cancel(config)
    _, asset_dir, _, _ = project_paths(config)
    clip_dir = clip_dir_for(config)
    media_dir = wavespeed_media_dir_for(config) / "videos"
    media_dir.mkdir(parents=True, exist_ok=True)
    key = api_key()
    manifest_records = []
    pending_jobs = []
    scenes = config["scenes"]
    wavespeed = config.get("wavespeed", {})
    timeout_s = int(wavespeed.get("video_timeout_s", 1800))
    selected = [scene for scene in scenes if scene_uses_seedance(config, scene)]
    cancel_event = config.get("_cancel_event")
    print(f"Generating {len(selected)} selected Seedance clip(s) into {clip_dir}")
    for index, scene in enumerate(scenes, 1):
        check_cancel(config)
        if not scene_uses_seedance(config, scene):
            print(f"[{index}/{len(scenes)}] skip Seedance for scene {scene.get('id', index)}")
            status_log(status_cb, f"Seedance scene {index}/{len(scenes)} skipped; scene uses still media.")
            manifest_records.append({"_order": index, "scene": scene.get("id", index), "seedance": False, "skipped": True, "reason": "scene_not_selected"})
            continue
        image_path = resolve_media_path(config, asset_dir, scene.get("asset") or f"scene_{index:02d}.png")
        out_path = clip_dir / clip_filename(scene, index)
        if out_path.exists() and not force:
            print(f"[{index}/{len(scenes)}] skip existing {out_path.name}")
            status_log(status_cb, f"Seedance clip {index}/{len(scenes)} skipped; existing clip is reused.")
            downloads = ensure_archived_media(out_path, media_dir, Path(out_path).stem)
            manifest_records.append(
                {
                    "_order": index,
                    "scene": scene.get("id", index),
                    "seedance": True,
                    "clip": str(out_path),
                    "skipped": True,
                    "downloads": downloads,
                }
            )
            continue
        if not image_path.exists():
            raise FileNotFoundError(f"Missing scene image for Seedance: {image_path}")
        scene_duration = float(scene["end"]) - float(scene["start"])
        prompt = build_video_prompt(config, scene)
        print(f"[{index}/{len(scenes)}] upload {image_path.name}")
        pending_jobs.append((index, scene, image_path, out_path, scene_duration, prompt))

    def run_clip_job(job):
        index, scene, image_path, out_path, scene_duration, prompt = job
        check_cancel(config)
        status_log(status_cb, f"Seedance clip {index}/{len(scenes)}: uploading source image {image_path.name}...")
        media_url, upload_response = upload_media(image_path, key)
        print(f"  Seedance request for {out_path.name}")
        prediction_id, submit_response, payload = submit_wavespeed_clip(
            media_url,
            prompt,
            scene_duration,
            config,
            scene,
            key,
        )
        print(f"  id: {prediction_id}")
        status_log(status_cb, f"Waiting for Seedance clip {index}/{len(scenes)} with native audio...")
        outputs, result_response = poll_wavespeed(
            prediction_id,
            key,
            timeout_s=timeout_s,
            interval_s=5,
            cancel_event=cancel_event,
            status_cb=status_cb,
            label=f"Seedance clip {index}/{len(scenes)}",
        )
        downloads = download_wavespeed_outputs(outputs, out_path, media_dir, Path(out_path).stem, ".mp4", cancel_event=cancel_event)
        size = downloads[0]["bytes"]
        print(f"  wrote {out_path} ({size} bytes); archived {len(downloads)} WaveSpeed output(s)")
        status_log(status_cb, f"Seedance clip {index}/{len(scenes)} saved: {out_path.name}.")
        return {
            "_order": index,
            "scene": scene.get("id", index),
            "seedance": True,
            "source_image": str(image_path),
            "clip": str(out_path),
            "duration": payload["duration"],
            "resolution": payload["resolution"],
            "prediction_id": prediction_id,
            "output_url": outputs[0],
            "bytes": size,
            "prompt": prompt,
            "downloads": downloads,
            "payload": {k: v for k, v in payload.items() if k != "image"},
            "upload_response": upload_response,
            "submit_response": submit_response,
            "result_response": result_response,
        }

    if pending_jobs:
        workers = wavespeed_parallelism(config, "video_concurrency", default=3, maximum=5)
        status_log(status_cb, f"Submitting {len(pending_jobs)} Seedance job(s) with concurrency {workers}...")
        if workers == 1 or len(pending_jobs) == 1:
            for job in pending_jobs:
                manifest_records.append(run_clip_job(job))
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
                futures = [executor.submit(run_clip_job, job) for job in pending_jobs]
                for future in concurrent.futures.as_completed(futures):
                    check_cancel(config)
                    manifest_records.append(future.result())
    manifest_path = clip_dir / "seedance_manifest.json"
    manifest = [
        {key: value for key, value in record.items() if key != "_order"}
        for record in sorted(manifest_records, key=lambda item: item.get("_order", 0))
    ]
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest_path


def make_placeholder(scene, width, height):
    img = Image.new("RGB", (width, height), (32, 35, 34))
    draw = ImageDraw.Draw(img)
    draw.rectangle((40, 40, width - 40, height - 40), outline=(230, 210, 165), width=6)
    draw_caption(img, scene.get("name", "MISSING ASSET"), int(height * 0.35), width, height, font_size=70)
    return img


def scene_image(config, scene, asset_dir, width, height):
    path = resolve_media_path(config, asset_dir, scene.get("asset") or f"scene_{int(scene.get('id', 0)):02d}.png")
    if path.exists():
        return Image.open(path).convert("RGB")
    return make_placeholder(scene, width, height)


def path_under(path, folder):
    try:
        resolved = Path(path).resolve()
        root = Path(folder).resolve()
        return root == resolved or root in resolved.parents
    except Exception:
        return False


def asset_is_gpt_image(config, asset_dir, asset):
    if not asset:
        return False
    project_dir = ROOT / "projects" / config["project_slug"]
    return path_under(resolve_media_path(config, asset_dir, asset), project_dir / "gpt images")


def resolve_media_path(config, asset_dir, asset):
    if not asset:
        return asset_dir / "__missing__"
    path = Path(asset)
    if path.is_absolute():
        return path
    project_dir = ROOT / "projects" / config["project_slug"]
    candidates = [
        asset_dir / asset,
        project_dir / asset,
        ROOT / asset,
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def active_shot(scene, local, scene_duration):
    shots = scene.get("shots") or []
    if not shots:
        return None, 0.0, scene_duration
    for index, shot in enumerate(shots):
        start = float(shot.get("at", 0.0))
        if "duration" in shot:
            end = start + float(shot["duration"])
        elif "end" in shot:
            end = float(shot["end"])
        elif index + 1 < len(shots):
            end = float(shots[index + 1].get("at", scene_duration))
        else:
            end = scene_duration
        if start <= local < end or (index == len(shots) - 1 and local >= start):
            return shot, start, max(0.1, end - start)
    return shots[-1], float(shots[-1].get("at", 0.0)), max(0.1, scene_duration - float(shots[-1].get("at", 0.0)))


class SceneClip:
    def __init__(self, path):
        self.path = Path(path)
        self.cap = cv2.VideoCapture(str(self.path))
        if not self.cap.isOpened():
            raise RuntimeError(f"Could not open video clip: {self.path}")
        self.fps = float(self.cap.get(cv2.CAP_PROP_FPS) or 24.0)
        self.frame_count = int(self.cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        self.duration = self.frame_count / self.fps if self.frame_count and self.fps else 0.0

    def frame(self, t):
        if self.duration > 0:
            t = t % self.duration
        frame_index = int(clamp(round(t * self.fps), 0, max(0, self.frame_count - 1)))
        self.cap.set(cv2.CAP_PROP_POS_FRAMES, frame_index)
        ok, frame = self.cap.read()
        if not ok:
            self.cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ok, frame = self.cap.read()
        if not ok:
            raise RuntimeError(f"Could not read frame from video clip: {self.path}")
        return Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))

    def frame_trimmed(self, t, start_trim=0.0):
        if self.duration > 0 and start_trim > 0:
            start_trim = min(float(start_trim), max(0.0, self.duration - (1.0 / max(self.fps, 1.0))))
            usable = max(1.0 / max(self.fps, 1.0), self.duration - start_trim)
            t = start_trim + (float(t) % usable)
        return self.frame(t)

    def release(self):
        self.cap.release()


def find_ffmpeg():
    exe = shutil.which("ffmpeg")
    candidates = [Path(exe)] if exe else []
    local = os.environ.get("LOCALAPPDATA")
    if local:
        jd = Path(local) / "JDownloader 2" / "tools" / "Windows"
        if jd.exists():
            candidates.extend(jd.rglob("ffmpeg.exe"))
        capcut = Path(local) / "CapCut" / "Apps"
        if capcut.exists():
            candidates.extend(capcut.glob("*/ffmpeg.exe"))
    seen = []
    for candidate in candidates:
        if candidate and candidate.exists() and candidate not in seen:
            seen.append(candidate)
    for candidate in seen:
        try:
            result = subprocess.run(
                [str(candidate), "-hide_banner", "-h", "encoder=libx264"],
                text=True,
                capture_output=True,
                timeout=8,
            )
            output = (result.stdout + result.stderr).lower()
            if "libx264" in output and "crf" in output:
                return str(candidate)
        except Exception:
            pass
    return str(seen[0]) if seen else None


def find_ffprobe(ffmpeg=None):
    exe = shutil.which("ffprobe")
    if exe:
        return exe
    if ffmpeg:
        candidate = Path(ffmpeg).with_name("ffprobe.exe" if os.name == "nt" else "ffprobe")
        if candidate.exists():
            return str(candidate)
    return None


def media_has_audio(path, ffprobe=None):
    if not ffprobe:
        return False
    try:
        result = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-select_streams",
                "a:0",
                "-show_entries",
                "stream=codec_type",
                "-of",
                "csv=p=0",
                str(path),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except Exception:
        return False
    return "audio" in (result.stdout or "").lower()


def seedance_audio_segments(config, clip_paths, ffprobe=None):
    if not bool(config.get("seedance_audio_in_final", True)):
        return []
    segments = []
    for i, scene in enumerate(config.get("scenes", []), 1):
        scene_id = scene.get("id", str(i))
        path = clip_paths.get(scene_id)
        if not path or not Path(path).exists() or not media_has_audio(path, ffprobe=ffprobe):
            continue
        start = max(0.0, float(scene.get("start", 0)))
        end = max(start, float(scene.get("end", start)))
        duration = max(0.1, end - start)
        source_start = seedance_clip_start_trim(config, scene)
        segment = {"path": Path(path), "start": start, "duration": duration, "source_start": source_start}
        if scene.get("seedance_audio_volume") is not None:
            try:
                segment["volume"] = max(0.0, min(float(scene.get("seedance_audio_volume")), 0.35))
            except (TypeError, ValueError):
                pass
        segments.append(segment)
    return segments


def sfx_library_root(config):
    candidates = []
    configured = config.get("sfx_library_dir")
    if configured:
        candidates.append(Path(configured))
    candidates.extend([ROOT / "soundeffects", ROOT.parent / "soundeffects", Path.cwd() / "soundeffects", Path.cwd().parent / "soundeffects"])
    for candidate in candidates:
        if (candidate / "shorts_ready").exists():
            return candidate
    return None


SFX_EXTS = {".wav", ".ogg", ".mp3", ".flac"}
SFX_CATEGORY_ALIASES = {
    "analog_transitions": ["analog_transitions"],
    "subtle_transitions": ["subtle_transitions", "analog_transitions", "motion_whoosh_like", "cuts_clicks_ui"],
    "serious_foley": ["serious_foley", "foley_props"],
    "subtle_impacts": ["subtle_impacts", "hits_impacts"],
    "subtle_tones": ["subtle_tones", "sci_fi_zaps"],
}
SFX_TRANSITION_NAME_TOKENS = {
    "analog", "slide", "slideshow", "projector", "click", "clack", "shutter", "swoosh", "whoosh",
    "swish", "transition", "card-slide", "card-shove", "page", "flip",
}


def write_mono_wav(path, samples, sample_rate=44100):
    samples = np.asarray(samples, dtype=np.float32)
    if samples.size == 0:
        return
    samples = np.clip(samples, -0.98, 0.98)
    pcm = (samples * 32767.0).astype("<i2")
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(pcm.tobytes())


def click_track(duration, sample_rate, clicks):
    n = int(duration * sample_rate)
    t = np.arange(n, dtype=np.float32) / sample_rate
    out = np.zeros(n, dtype=np.float32)
    for at, freq, amp, decay in clicks:
        start = min(n - 1, max(0, int(at * sample_rate)))
        length = min(n - start, int(decay * sample_rate))
        if length <= 0:
            continue
        local_t = np.arange(length, dtype=np.float32) / sample_rate
        env = np.exp(-local_t * 34.0)
        burst = np.sin(2 * math.pi * freq * local_t) * env * amp
        out[start:start + length] += burst.astype(np.float32)
    # A tiny mechanical bed keeps it analog without being comedic.
    out += (np.sin(2 * math.pi * 90 * t) * np.exp(-t * 10.0) * 0.045).astype(np.float32)
    return out


def whoosh_track(duration, sample_rate, seed=1):
    n = int(duration * sample_rate)
    rng = np.random.default_rng(seed)
    noise = rng.normal(0.0, 1.0, n).astype(np.float32)
    smooth = np.convolve(noise, np.ones(64, dtype=np.float32) / 64.0, mode="same")
    t = np.linspace(0, 1, n, dtype=np.float32)
    env = np.maximum(0.0, np.sin(np.pi * t)) ** 1.8
    tone = np.sin(2 * math.pi * (180 + 320 * t) * (t * duration)).astype(np.float32) * 0.14
    return (smooth * 0.58 + tone) * env * 1.05


def ensure_builtin_transition_sfx(root):
    root = Path(root)
    folder = root / "shorts_ready" / "analog_transitions"
    sample_rate = 44100
    specs = {
        "analog_slide_click_01.wav": click_track(0.34, sample_rate, [(0.018, 1800, 0.82, 0.055), (0.092, 880, 0.46, 0.08)]),
        "analog_slide_click_02.wav": click_track(0.38, sample_rate, [(0.016, 1450, 0.76, 0.06), (0.12, 620, 0.38, 0.095)]),
        "soft_slide_whoosh_01.wav": whoosh_track(0.42, sample_rate, seed=11),
        "soft_slide_whoosh_02.wav": whoosh_track(0.46, sample_rate, seed=29),
    }
    for name, samples in specs.items():
        write_mono_wav(folder / name, samples, sample_rate=sample_rate)


def default_sfx_root():
    root = ROOT / "soundeffects"
    (root / "shorts_ready").mkdir(parents=True, exist_ok=True)
    return root


def sfx_category_files(config, category):
    root = sfx_library_root(config) or default_sfx_root()
    if category in {"analog_transitions", "subtle_transitions"}:
        ensure_builtin_transition_sfx(root)
    files = []
    for folder_name in SFX_CATEGORY_ALIASES.get(category, [category]):
        folder = root / "shorts_ready" / folder_name
        if not folder.exists():
            continue
        for path in sorted(folder.iterdir()):
            if not path.is_file() or path.suffix.lower() not in SFX_EXTS:
                continue
            if category in {"analog_transitions", "subtle_transitions"} and folder_name in {"motion_whoosh_like", "cuts_clicks_ui", "all_cc0"}:
                name = path.stem.lower()
                if not any(token in name for token in SFX_TRANSITION_NAME_TOKENS):
                    continue
            files.append(path)
    # Project-local AI-generated SFX (Kling fallback) count as library files too.
    if config.get("project_slug"):
        gen_folder = wavespeed_media_dir_for(config) / "sfx_generated" / category
        if gen_folder.exists():
            for path in sorted(gen_folder.iterdir()):
                if path.is_file() and path.suffix.lower() in SFX_EXTS:
                    files.append(path)
    return list(dict.fromkeys(files))


def choose_sfx(config, category, seed_text):
    files = sfx_category_files(config, category)
    if not files:
        return None
    if category == "analog_transitions" and bool(config.get("sfx_single_transition_sound_per_video", True)):
        seed_text = f"{config.get('project_slug', '')}|{config.get('output_basename', '')}|single-transition"
    digest = hashlib.sha1(f"{config.get('project_slug', '')}|{category}|{seed_text}".encode("utf-8", errors="ignore")).hexdigest()
    return files[int(digest[:8], 16) % len(files)]


def add_sfx_event(events, config, at, category, seed_text, duration, volume, min_spacing=0.28):
    if at < 0.05:
        return
    if any(abs(event["start"] - at) < min_spacing for event in events):
        return
    path = choose_sfx(config, category, seed_text)
    if not path:
        return
    events.append({
        "path": path,
        "start": round(float(at), 3),
        "duration": round(float(duration), 3),
        "volume": round(float(volume), 3),
        "category": category,
    })


def build_sfx_segments(config, has_speech=False):
    if not bool(config.get("sfx_enabled", True)):
        return []
    if not sfx_library_root(config) and not bool(config.get("sfx_generation_enabled", False)):
        return []
    base_volume = min(float(config.get("sfx_volume_with_speech" if has_speech else "sfx_volume", 0.075)), 0.14)
    transition_volume = min(
        float(config.get("sfx_transition_volume_with_speech" if has_speech else "sfx_transition_volume", base_volume * 1.05)),
        0.12,
    )
    scenes = config.get("scenes", [])
    if not scenes:
        return []
    events = []
    action_words = {"hit", "impact", "crash", "explode", "explosion", "shot", "fire", "attack", "slam", "collapse", "fall"}
    tech_words = {"signal", "screen", "computer", "electric", "power", "digital"}
    foley_words = {"map", "paper", "photo", "card", "coin", "door", "step", "footstep", "metal", "wood", "weapon"}
    for scene_index, scene in enumerate(scenes):
        scene_start = float(scene.get("start", 0))
        scene_duration = max(0.1, float(scene.get("end", scene_start)) - scene_start)
        text = f"{scene.get('script', '')} {scene.get('visual_direction', '')}".lower()
        if scene_index > 0:
            add_sfx_event(events, config, scene_start, "analog_transitions", f"slide-scene-{scene_index}-{text}", 0.36, transition_volume, min_spacing=0.18)
        shots = scene.get("shots") or []
        for shot_index, shot in enumerate(shots):
            at = scene_start + float(shot.get("at", 0))
            if shot_index == 0 or at <= 0.05:
                continue
            add_sfx_event(events, config, at, "analog_transitions", f"slide-shot-{scene_index}-{shot_index}-{shot.get('asset', '')}", 0.32, transition_volume * 0.92, min_spacing=0.18)
        if any(word in text for word in action_words):
            add_sfx_event(events, config, scene_start + min(0.45, scene_duration * 0.2), "subtle_impacts", f"impact-{scene_index}-{text}", 0.58, base_volume * 0.72)
        elif any(word in text for word in tech_words):
            add_sfx_event(events, config, scene_start + min(0.35, scene_duration * 0.18), "subtle_tones", f"tech-{scene_index}-{text}", 0.48, base_volume * 0.45)
        elif any(word in text for word in foley_words):
            add_sfx_event(events, config, scene_start + min(0.3, scene_duration * 0.15), "serious_foley", f"foley-{scene_index}-{text}", 0.40, base_volume * 0.55)
    max_events = max(3, int(float(config.get("duration", 60)) / 60.0 * int(config.get("sfx_max_per_minute", 24))))
    transition_events = [event for event in events if event.get("category") == "analog_transitions"]
    other_events = [event for event in events if event.get("category") != "analog_transitions"]
    allowed_other_count = max(0, max_events - len(transition_events))
    return sorted(transition_events + sorted(other_events, key=lambda event: event["start"])[:allowed_other_count], key=lambda event: event["start"])


# Content SFX categories that can be AI-generated when the library has no fit.
# (Transitions are excluded -- builtin whoosh/click clips always exist.)
SFX_GEN_CATEGORIES = {
    "subtle_impacts": {
        "words": {"hit", "impact", "crash", "explode", "explosion", "shot", "fire",
                  "attack", "slam", "collapse", "fall", "burst", "blast", "smash"},
        "prompt": "a short deep cinematic impact boom, subtle low-end hit, tight and clean, no music",
        "duration": 2,
    },
    "subtle_tones": {
        "words": {"signal", "screen", "computer", "electric", "power", "digital",
                  "data", "online", "broadcast", "radio"},
        "prompt": "a subtle digital tech tone, soft electronic pulse and clean UI beep, no music",
        "duration": 2,
    },
    "serious_foley": {
        "words": {"map", "paper", "photo", "card", "coin", "door", "step", "footstep",
                  "metal", "wood", "weapon", "letter", "book"},
        "prompt": "subtle realistic foley, soft paper and prop handling texture, documentary feel, no music",
        "duration": 2,
    },
}


def generated_sfx_dir(config, category=None):
    base = wavespeed_media_dir_for(config) / "sfx_generated"
    folder = base / category if category else base
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def ensure_generated_sfx(config, status_cb=None):
    """Pre-render step: generate fallback SFX (Kling) for content categories the
    scenes need but the local library can't cover. Generated clips land in the
    project's sfx_generated/<category> folder, where choose_sfx then finds them.
    Returns the list of newly generated file paths.
    """
    if not bool(config.get("sfx_enabled", True)):
        return []
    if not bool(config.get("sfx_generation_enabled", False)):
        return []
    key = config.get("wavespeed_api_key") or os.environ.get("WAVESPEED_API_KEY", "")
    if not key:
        return []
    scenes = config.get("scenes", [])
    if not scenes:
        return []
    words_present = set(re.findall(r"[a-z]+", " ".join(
        f"{scene.get('script', '')} {scene.get('visual_direction', '')}" for scene in scenes).lower()))
    generated = []
    for category, spec in SFX_GEN_CATEGORIES.items():
        if not (spec["words"] & words_present):
            continue
        if sfx_category_files(config, category):  # library or prior generation already covers it
            continue
        out = generated_sfx_dir(config, category) / f"{category}_generated_01.wav"
        if out.exists():
            continue
        try:
            generate_sfx_clip(spec["prompt"], spec["duration"], out, key=key,
                              cancel_event=config.get("_cancel_event"), status_cb=status_cb)
            generated.append(str(out))
        except PipelineCancelled:
            raise
        except Exception as exc:
            status_log(status_cb, f"SFX generation failed for {category}: {exc}")
    if generated:
        status_log(status_cb, f"Generated {len(generated)} fallback SFX clip(s) via Kling.")
    return generated


def background_music_root(config):
    candidates = []
    configured = config.get("background_music_dir")
    if configured:
        candidates.append(Path(configured))
    candidates.extend([
        ROOT / "background music",
        ROOT / "background_music",
        ROOT.parent / "background music",
        ROOT.parent / "background_music",
        Path.cwd() / "background music",
        Path.cwd() / "background_music",
        Path.cwd().parent / "background music",
        Path.cwd().parent / "background_music",
    ])
    for candidate in candidates:
        if candidate.exists() and any(
            path.is_file() and path.suffix.lower() in {".wav", ".mp3", ".ogg", ".flac", ".m4a", ".aac"}
            for path in candidate.rglob("*")
        ):
            return candidate
    return None


def background_music_files(config):
    root = background_music_root(config)
    if not root:
        return []
    return sorted(
        path for path in root.rglob("*")
        if path.is_file() and path.suffix.lower() in {".wav", ".mp3", ".ogg", ".flac", ".m4a", ".aac"}
    )


def background_music_text(config):
    parts = [config.get("title", ""), config.get("title", "")]
    for scene in config.get("scenes", []):
        parts.append(scene.get("script", ""))
        parts.append(scene.get("script", ""))
        parts.append(scene.get("visual_direction", ""))
    parts.append(config.get("visual_script", ""))
    return " ".join(parts).lower()


def choose_background_music(config):
    files = background_music_files(config)
    if not files:
        return None
    text = background_music_text(config)
    mood_keywords = [
        ("documentary_tension", {"war", "battle", "history", "mystery", "crime", "secret", "military", "soldier", "danger", "conflict", "dark"}),
        ("cinematic_drive", {"chase", "race", "truck", "escape", "run", "fast", "attack", "action", "moving", "rush"}),
        ("quirky_momentum", {"funny", "weird", "absurd", "pig", "emu", "meme", "strange", "fail", "failed", "comedy"}),
        ("electro_pulse", {"ai", "future", "tech", "robot", "digital", "internet", "app", "screen", "computer"}),
        ("warm_lofi", {"calm", "emotional", "soft", "nostalgia", "peace", "quiet", "ending", "story"}),
        ("upbeat_short", {"win", "payoff", "reveal", "success", "surprise", "trend", "viral", "shorts", "tiktok", "youtube"}),
    ]
    scored = []
    for path in files:
        name = path.stem.lower()
        score = 0
        for mood, words in mood_keywords:
            if mood in name:
                score += 4 * sum(1 for word in words if word in text)
        if "short" in name or "pulse" in name or "drive" in name:
            score += 1
        digest = hashlib.sha1(f"{config.get('project_slug', '')}|{path.name}|{text[:120]}".encode("utf-8", errors="ignore")).hexdigest()
        scored.append((score, int(digest[:8], 16), path))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return scored[0][2]


def build_background_music_segment(config, has_speech=False):
    if not bool(config.get("background_music_enabled", False)):
        return None
    path = choose_background_music(config)
    if not path:
        return None
    volume_key = "background_music_volume_with_speech" if has_speech else "background_music_volume"
    volume = float(config.get(volume_key, 0.05 if has_speech else 0.075))
    return {
        "path": path,
        "start": 0.0,
        "duration": float(config.get("duration", 60)),
        "volume": round(volume, 3),
        "mode": "instrumental_background_loop",
    }


def render_video(config, basename=None):
    check_cancel(config)
    status_cb = config.get("_status_cb") if isinstance(config, dict) else None
    _, asset_dir, render_dir, _ = project_paths(config)
    clip_dir = clip_dir_for(config)
    width, height = config.get("resolution", [1080, 1920])
    fps = int(config.get("fps", 30))
    duration = float(config.get("duration", max(scene["end"] for scene in config["scenes"])))
    total_frames = int(round(duration * fps))
    basename = basename or config.get("output_basename") or config["project_slug"]
    intermediate = render_dir / f"{basename}_intermediate.mp4"
    output = render_dir / f"{basename}.mp4"
    vignette = make_vignette(width, height)

    # Precompute viral word-by-word caption timelines (sourced from spoken script).
    captions_enabled = bool(config.get("animated_captions", config.get("captions_enabled", True)))
    caption_max_words = int(config.get("caption_max_words", 3))
    caption_uppercase = bool(config.get("caption_uppercase", True))
    caption_chunks_by_scene = {}
    first_scene_id = config["scenes"][0].get("id", "1") if config.get("scenes") else None
    if captions_enabled:
        for i, scene in enumerate(config["scenes"], 1):
            sid = scene.get("id", str(i))
            ctext = (scene.get("caption") or scene.get("script") or "").strip()
            sdur = max(0.1, float(scene["end"]) - float(scene["start"]))
            caption_chunks_by_scene[sid] = build_caption_chunks(
                ctext, sdur, caption_max_words, caption_uppercase,
                word_times=scene.get("word_timings"),
            )
        if bool(config.get("export_caption_pngs", True)):
            try:
                count = len(export_caption_pngs(
                    config, caption_chunks_by_scene, config["scenes"],
                    first_scene_id, width, height, render_dir.parent / "captions",
                ))
                status_log(status_cb, f"Saved {count} individual transparent caption PNG(s) to captions/.")
            except Exception as exc:
                status_log(status_cb, f"Caption PNG export skipped: {exc}")

    assets = {
        scene.get("id", str(i)): scene_image(config, scene, asset_dir, width, height)
        for i, scene in enumerate(config["scenes"], 1)
    }
    shot_assets = {}
    for i, scene in enumerate(config["scenes"], 1):
        for shot_index, shot in enumerate(scene.get("shots", []), 1):
            asset = shot.get("asset")
            if asset:
                path = resolve_media_path(config, asset_dir, asset)
                key = (scene.get("id", str(i)), shot_index)
                if path.exists():
                    with Image.open(path) as source:
                        source.seek(0)
                        shot_assets[key] = source.convert("RGB")
    use_clips = bool(config.get("use_seedance_clips", True))
    clips = {}
    if use_clips:
        for i, scene in enumerate(config["scenes"], 1):
            if not scene_uses_seedance(config, scene):
                continue
            path = clip_dir / clip_filename(scene, i)
            if path.exists():
                clips[scene.get("id", str(i))] = SceneClip(path)

    writer = cv2.VideoWriter(str(intermediate), cv2.VideoWriter_fourcc(*"mp4v"), fps, (width, height))
    if not writer.isOpened():
        raise RuntimeError("OpenCV could not open MP4 writer.")

    clip_paths = {}
    try:
        last_render_percent = -1
        for frame_no in range(total_frames):
            if frame_no % max(1, fps // 2) == 0:
                check_cancel(config)
            if total_frames > 0:
                render_percent = int(frame_no * 100 / total_frames)
                if render_percent >= last_render_percent + 10:
                    status_log(status_cb, f"Rendering frames: {render_percent}%")
                    last_render_percent = render_percent
            t = frame_no / fps
            scene = next((s for s in config["scenes"] if float(s["start"]) <= t < float(s["end"])), config["scenes"][-1])
            scene_id = scene.get("id", str(config["scenes"].index(scene) + 1))
            local = t - float(scene["start"])
            scene_duration = max(0.1, float(scene["end"]) - float(scene["start"]))
            p = local / scene_duration
            shot, shot_start, shot_duration = active_shot(scene, local, scene_duration)
            shot_p = clamp((local - shot_start) / shot_duration, 0.0, 1.0)
            shot_index = None
            if shot is not None:
                try:
                    shot_index = scene.get("shots", []).index(shot) + 1
                except ValueError:
                    shot_index = None
            extra_zoom = opening_punch_zoom(config, t, local, scene_id == first_scene_id)
            use_clip_frame = scene_id in clips and (shot is None or bool(shot.get("use_clip", True)))
            if use_clip_frame:
                img = image_fit_cover(clips[scene_id].frame_trimmed(local, seedance_clip_start_trim(config, scene)), (width, height), zoom=1.0 + extra_zoom, offset=(0, 0))
            else:
                motion = dict(scene.get("motion", {}))
                if shot and shot.get("motion"):
                    motion.update(shot["motion"])
                zoom_start = float(motion.get("zoom_start", 1.02))
                zoom_end = float(motion.get("zoom_end", 1.12))
                pan_x = float(motion.get("pan_x", 45))
                pan_y = float(motion.get("pan_y", 0))
                motion_scale = float(config.get("still_motion_scale", 1.0))
                if (shot or {}).get("fit", scene.get("fit", "cover")) == "contain":
                    motion_scale *= float(config.get("contain_motion_scale", 1.0))
                zoom_end = zoom_start + (zoom_end - zoom_start) * motion_scale
                pan_x *= motion_scale
                pan_y *= motion_scale
                zoom = zoom_start + (zoom_end - zoom_start) * ease_in_out(shot_p)
                zoom += scene_punch_zoom(config, scene, p)
                zoom += extra_zoom
                offset = (int((shot_p - 0.5) * pan_x), int((shot_p - 0.5) * pan_y))
                base_asset = assets[scene_id]
                if shot_index is not None and (scene_id, shot_index) in shot_assets:
                    base_asset = shot_assets[(scene_id, shot_index)]
                shot_asset_ref = (shot or {}).get("asset") or scene.get("asset")
                if bool(config.get("gpt_static_stills_disabled", True)) and asset_is_gpt_image(config, asset_dir, shot_asset_ref):
                    base_asset = make_placeholder(scene, width, height)
                fit_mode = (shot or {}).get("fit", scene.get("fit", "cover"))
                if fit_mode == "contain":
                    img = image_fit_contain(base_asset, (width, height), zoom=zoom, offset=offset)
                else:
                    img = image_fit_cover(base_asset, (width, height), zoom=zoom, offset=offset)
            img = tint(img, alpha=int(scene.get("tint_alpha", config.get("tint_alpha", 18))))
            if scene.get("dust", config.get("dust", False)) and scene_id not in clips:
                img = dust_overlay(img, t, width, height, intensity=float(scene.get("dust_intensity", 0.35)))
            base = Image.alpha_composite(img.convert("RGBA"), vignette).convert("RGB")
            base = draw_smart_overlays(base, scene, shot, shot_p, frame_no, width, height, config)
            base = draw_kinetic_annotations(base, scene, p, frame_no, width, height, config)
            if scene_renders_caption(config, scene):
                draw_caption(
                    base,
                    scene.get("caption", ""),
                    int(scene.get("caption_y", config.get("caption_y", 150))),
                    width,
                    height,
                    font_size=scene.get("caption_size"),
                )
            base = ImageEnhance.Contrast(base).enhance(float(scene.get("contrast", 1.06)))
            base = add_grain(base, frame_no, strength=int(scene.get("grain", config.get("grain", 18))))
            if captions_enabled:
                base = draw_animated_caption(
                    base,
                    caption_chunks_by_scene.get(scene_id, []),
                    local,
                    width,
                    height,
                    config,
                    is_hook=(scene_id == first_scene_id),
                )
            writer.write(cv2.cvtColor(np.array(base), cv2.COLOR_RGB2BGR))
    finally:
        clip_paths = {scene_id: clip.path for scene_id, clip in clips.items()}
        writer.release()
        for clip in clips.values():
            clip.release()

    ffmpeg = find_ffmpeg()
    ffprobe = find_ffprobe(ffmpeg)
    speech_audio_enabled = bool(config.get("speech_audio_in_final", False))
    audio_path = Path(config.get("audio_path", "")) if speech_audio_enabled and config.get("audio_path") else None
    if audio_path and not audio_path.exists():
        audio_path = None
    seedance_segments = []
    if (not audio_path or bool(config.get("mix_seedance_audio_with_speech", False))) and ffmpeg:
        seedance_segments = seedance_audio_segments(config, clip_paths, ffprobe=ffprobe)
    if clip_paths and ffmpeg:
        if seedance_segments:
            status_log(status_cb, f"Seedance audio detected: mixing {len(seedance_segments)} clip audio segment(s).")
        else:
            status_log(status_cb, "Seedance audio: no audio stream found in generated clips, using app SFX/background instead.")
    sfx_segments = build_sfx_segments(config, has_speech=bool(audio_path)) if ffmpeg else []
    background_music = build_background_music_segment(config, has_speech=bool(audio_path)) if ffmpeg else None
    if ffmpeg:
        if sfx_segments:
            status_log(status_cb, f"SFX audio: mixing {len(sfx_segments)} subtle transition/event sound(s).")
            transition_count = sum(1 for segment in sfx_segments if str(segment.get("category", "")).endswith("transitions"))
            if transition_count:
                status_log(status_cb, f"Transition SFX: mixing {transition_count} quiet analog slide/whoosh cue(s).")
        elif bool(config.get("sfx_enabled", True)):
            status_log(status_cb, "SFX audio: no eligible sound effects found for this render.")
    if ffmpeg:
        check_cancel(config)
        status_log(status_cb, "Encoding final MP4 and mixing audio...")
        cmd = [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-i",
            str(intermediate),
        ]
        if audio_path:
            cmd += ["-i", str(audio_path)]
        for segment in seedance_segments:
            cmd += ["-i", str(segment["path"])]
        for segment in sfx_segments:
            cmd += ["-i", str(segment["path"])]
        if background_music:
            cmd += ["-stream_loop", "-1", "-i", str(background_music["path"])]
        if not audio_path and not seedance_segments and not sfx_segments and not background_music:
            cmd += ["-an"]
        cmd += [
            "-r",
            str(fps),
            "-vf",
            f"scale={width}:{height},format=yuv420p",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            str(config.get("crf", 18)),
        ]
        if audio_path and not seedance_segments and not sfx_segments and not background_music:
            cmd += ["-map", "0:v:0", "-map", "1:a:0", "-c:a", "aac", "-b:a", "128k", "-shortest"]
        elif seedance_segments or sfx_segments or background_music:
            filters = []
            labels = []
            input_offset = 2 if audio_path else 1
            seedance_volume_key = "seedance_audio_volume_with_speech" if audio_path else "seedance_audio_volume"
            seedance_volume = min(float(config.get(seedance_volume_key, config.get("seedance_audio_volume", 0.2))), 0.35)
            seedance_limit = 0.45 if audio_path else 0.62
            master_gain = float(config.get("audio_master_gain", 1.05))
            for index, segment in enumerate(seedance_segments):
                input_index = input_offset + index
                label = f"sd{index}"
                delay_ms = int(round(segment["start"] * 1000))
                source_start = float(segment.get("source_start", 0.0))
                source_end = source_start + float(segment["duration"])
                segment_volume = min(float(segment.get("volume", seedance_volume)), 0.35)
                filters.append(
                    f"[{input_index}:a:0]atrim={source_start:.3f}:{source_end:.3f},"
                    f"asetpts=PTS-STARTPTS,adelay={delay_ms}:all=1,"
                    f"volume={segment_volume:.3f},alimiter=limit={seedance_limit:.2f}[{label}]"
                )
                labels.append(label)
            sfx_offset = input_offset + len(seedance_segments)
            for index, segment in enumerate(sfx_segments):
                input_index = sfx_offset + index
                label = f"sfx{index}"
                delay_ms = int(round(segment["start"] * 1000))
                fade_out_start = max(0.0, float(segment["duration"]) - 0.08)
                filters.append(
                    f"[{input_index}:a:0]atrim=0:{segment['duration']:.3f},"
                    f"asetpts=PTS-STARTPTS,afade=t=out:st={fade_out_start:.3f}:d=0.080,"
                    f"adelay={delay_ms}:all=1,volume={segment['volume']:.3f}[{label}]"
                )
                labels.append(label)
            if background_music:
                bg_input = input_offset + len(seedance_segments) + len(sfx_segments)
                bg_label = "bgm"
                fade_duration = min(1.4, max(0.25, duration / 8.0))
                fade_out_start = max(0.0, duration - fade_duration)
                filters.append(
                    f"[{bg_input}:a:0]atrim=0:{duration:.3f},asetpts=PTS-STARTPTS,"
                    f"afade=t=in:st=0:d={fade_duration:.3f},"
                    f"afade=t=out:st={fade_out_start:.3f}:d={fade_duration:.3f},"
                    f"volume={background_music['volume']:.3f}[{bg_label}]"
                )
                labels.append(bg_label)
            if audio_path:
                filters.append("[1:a:0]volume=1.0[speech]")
                mix_inputs = ["speech"] + labels
            else:
                mix_inputs = labels
            if len(mix_inputs) == 1:
                filters.append(f"[{mix_inputs[0]}]volume={master_gain:.3f},alimiter=limit=0.96,atrim=0:{duration:.3f}[aout]")
            else:
                filters.append(
                    "".join(f"[{label}]" for label in mix_inputs)
                    + f"amix=inputs={len(mix_inputs)}:duration=longest:dropout_transition=0:normalize=0,"
                    + f"volume={master_gain:.3f},alimiter=limit=0.94,atrim=0:{duration:.3f}[aout]"
                )
            cmd += [
                "-filter_complex",
                ";".join(filters),
                "-map",
                "0:v:0",
                "-map",
                "[aout]",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                "-shortest",
            ]
        cmd += ["-movflags", "+faststart", str(output)]
        run_subprocess_with_cancel(cmd, config)
        intermediate.unlink(missing_ok=True)
    else:
        intermediate.replace(output)

    import edit_plan
    plan = edit_plan.convert_config_to_edit_plan(config)
    plan["output"] = str(output)
    plan["audio_path"] = str(audio_path) if audio_path else None
    plan["seedance_audio_segments"] = [
        {"path": str(segment["path"]), "start": segment["start"], "duration": segment["duration"], "source_start": segment.get("source_start", 0.0), "volume": segment.get("volume")}
        for segment in seedance_segments
    ]
    plan["sfx_segments"] = [
        {
            "path": str(segment["path"]),
            "start": segment["start"],
            "duration": segment["duration"],
            "volume": segment["volume"],
            "category": segment["category"],
        }
        for segment in sfx_segments
    ]
    plan["background_music"] = (
        {
            "path": str(background_music["path"]),
            "start": background_music["start"],
            "duration": background_music["duration"],
            "volume": background_music["volume"],
            "mode": background_music["mode"],
        }
        if background_music else None
    )
    plan["seedance_clips_used"] = {scene_id: str(path) for scene_id, path in clip_paths.items()}
    plan["ffmpeg"] = ffmpeg
    plan["ffprobe"] = ffprobe
    (render_dir / f"{basename}_edit_plan.json").write_text(json.dumps(plan, indent=2), encoding="utf-8")
    return output


def create_review(config, video_path):
    _, _, _, review_dir = project_paths(config)
    clip_dir = clip_dir_for(config)
    width, height = config.get("resolution", [1080, 1920])
    cap = cv2.VideoCapture(str(video_path))
    font = get_font(22, False)
    thumbs = []
    samples = []
    for scene in config["scenes"]:
        start, end = float(scene["start"]), float(scene["end"])
        for label, t in [("start", start + 0.15), ("mid", (start + end) / 2)]:
            samples.append((scene, label, min(t, end - 0.05)))
    for scene, label, t in samples:
        cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
        ok, frame = cap.read()
        if not ok:
            continue
        img = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        img.thumbnail((150, 267))
        tile = Image.new("RGB", (180, 330), (235, 235, 235))
        tile.paste(img, ((180 - img.width) // 2, 8))
        draw = ImageDraw.Draw(tile)
        draw.text((8, 282), f"{scene.get('id')} {label} {t:04.1f}s", fill=(0, 0, 0), font=font)
        thumbs.append(tile)
    cap.release()

    cols = 4
    rows = math.ceil(len(thumbs) / cols)
    sheet = Image.new("RGB", (cols * 180, rows * 330), (220, 220, 220))
    for index, tile in enumerate(thumbs):
        sheet.paste(tile, ((index % cols) * 180, (index // cols) * 330))
    basename = Path(video_path).stem
    sheet_path = review_dir / f"{basename}_review_sheet.jpg"
    sheet.save(sheet_path, quality=92)

    checklist = {
        "video": str(video_path),
        "review_sheet": str(sheet_path),
        "review_instructions": [
            "For each scene, check if the visual matches the script beat in under one second.",
            "Flag weird overlays, unreadable captions, AI text artifacts, wrong subject, weak motion, or repeated-looking scenes.",
            "If any scene fails, regenerate only that scene asset and rerender.",
        ],
        "scenes": [
            {
                "id": scene.get("id"),
                "name": scene.get("name"),
                "timing": [scene.get("start"), scene.get("end")],
                "caption": scene.get("caption"),
                "render_caption": scene_renders_caption(config, scene),
                "script": scene.get("script"),
                "asset": scene.get("asset"),
                "seedance": scene_uses_seedance(config, scene),
                "clip": str(clip_dir / clip_filename(scene, index)),
                "clip_exists": (clip_dir / clip_filename(scene, index)).exists(),
                "status": "needs-human-or-agent-visual-check",
            }
            for index, scene in enumerate(config["scenes"], 1)
        ],
    }
    checklist_path = review_dir / f"{basename}_review_checklist.json"
    checklist_path.write_text(json.dumps(checklist, indent=2), encoding="utf-8")
    return sheet_path, checklist_path


def create_shot_review(config, video_path):
    _, _, _, review_dir = project_paths(config)
    cap = cv2.VideoCapture(str(video_path))
    font = get_font(20, False)
    thumbs = []
    samples = []
    for scene in config["scenes"]:
        scene_start = float(scene["start"])
        scene_end = float(scene["end"])
        scene_duration = max(0.1, scene_end - scene_start)
        shots = scene.get("shots") or [{"at": 0.0, "asset": scene.get("asset"), "use_clip": scene_uses_seedance(config, scene)}]
        for index, shot in enumerate(shots, 1):
            local = float(shot.get("at", 0.0)) + 0.18
            if index + 1 < len(shots):
                next_at = float(shots[index].get("at", scene_duration))
                local = min(local, max(float(shot.get("at", 0.0)) + 0.05, next_at - 0.08))
            t = min(scene_start + local, scene_end - 0.05)
            samples.append((scene, index, shot, t))
    for scene, shot_index, shot, t in samples:
        cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
        ok, frame = cap.read()
        if not ok:
            continue
        img = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        img.thumbnail((150, 267))
        tile = Image.new("RGB", (190, 348), (235, 235, 235))
        tile.paste(img, ((190 - img.width) // 2, 8))
        draw = ImageDraw.Draw(tile)
        source = "clip" if shot.get("use_clip", False) else Path(shot.get("asset", scene.get("asset", ""))).stem[:16]
        draw.text((8, 282), f"{scene.get('id')} shot {shot_index} {t:04.1f}s", fill=(0, 0, 0), font=font)
        draw.text((8, 309), source, fill=(0, 0, 0), font=font)
        thumbs.append(tile)
    cap.release()

    cols = 4
    rows = math.ceil(len(thumbs) / cols)
    sheet = Image.new("RGB", (cols * 190, rows * 348), (220, 220, 220))
    for index, tile in enumerate(thumbs):
        sheet.paste(tile, ((index % cols) * 190, (index // cols) * 348))
    basename = Path(video_path).stem
    sheet_path = review_dir / f"{basename}_shot_review_sheet.jpg"
    sheet.save(sheet_path, quality=92)
    return sheet_path


def validate_config(config):
    required = ["project_slug", "title", "duration", "fps", "resolution", "scenes"]
    missing = [key for key in required if key not in config]
    if missing:
        raise ValueError(f"Config missing required keys: {missing}")
    last = -1
    for scene in config["scenes"]:
        for key in ("id", "name", "start", "end", "caption"):
            if key not in scene:
                raise ValueError(f"Scene missing {key}: {scene}")
        if float(scene["start"]) < last:
            raise ValueError(f"Scenes must be sorted by time: {scene}")
        if float(scene["end"]) <= float(scene["start"]):
            raise ValueError(f"Scene end must be after start: {scene}")
        last = float(scene["start"])
    return True


def main():
    parser = argparse.ArgumentParser(description="Config-driven AI Short generator using WaveSpeed image and video assets.")
    parser.add_argument("--config", required=True, help="Path to short project JSON config.")
    parser.add_argument("--generate-assets", action="store_true", help="Generate missing/forced scene assets via WaveSpeed.")
    parser.add_argument("--force-assets", action="store_true", help="Regenerate existing assets.")
    parser.add_argument("--generate-clips", action="store_true", help="Generate missing/forced Seedance image-to-video clips.")
    parser.add_argument("--force-clips", action="store_true", help="Regenerate existing Seedance clips.")
    parser.add_argument("--render", action="store_true", help="Render the 9:16 MP4.")
    parser.add_argument("--review", action="store_true", help="Create review sheet/checklist for the rendered MP4.")
    parser.add_argument("--shot-review", action="store_true", help="Create a shot-level review sheet for every configured scene beat.")
    parser.add_argument("--all", action="store_true", help="Generate assets, generate Seedance clips, render, and create review artifacts.")
    parser.add_argument("--output-basename", help="Override output basename.")
    args = parser.parse_args()

    config = load_config(args.config)
    validate_config(config)
    if args.all:
        args.generate_assets = args.generate_clips = args.render = args.review = args.shot_review = True
    if args.output_basename:
        config["output_basename"] = args.output_basename

    output = None
    if args.generate_assets:
        manifest = generate_assets(config, force=args.force_assets)
        print(f"Wrote asset manifest: {manifest}")
    if args.generate_clips:
        manifest = generate_clips(config, force=args.force_clips)
        print(f"Wrote Seedance clip manifest: {manifest}")
    if args.render:
        output = render_video(config, basename=args.output_basename)
        print(f"Wrote video: {output}")
    if args.review:
        if output is None:
            _, _, render_dir, _ = project_paths(config)
            basename = args.output_basename or config.get("output_basename") or config["project_slug"]
            output = render_dir / f"{basename}.mp4"
        sheet, checklist = create_review(config, output)
        print(f"Wrote review sheet: {sheet}")
        print(f"Wrote review checklist: {checklist}")
    if args.shot_review:
        if output is None:
            _, _, render_dir, _ = project_paths(config)
            basename = args.output_basename or config.get("output_basename") or config["project_slug"]
            output = render_dir / f"{basename}.mp4"
        sheet = create_shot_review(config, output)
        print(f"Wrote shot review sheet: {sheet}")
    if not (args.generate_assets or args.generate_clips or args.render or args.review or args.shot_review):
        print("Config is valid. Use --all or --generate-assets/--generate-clips/--render/--review/--shot-review.")


if __name__ == "__main__":
    main()
