"""WaveSpeed OmniVoice text-to-speech for Reddit Story Mode.

Uses the same WaveSpeed submit/poll/download plumbing as the rest of the app
(pipeline.request_json / poll_wavespeed / download_file), so there is no second HTTP client
and the WAVESPEED_API_KEY env var is reused.

The OmniVoice model id is configurable via WAVESPEED_TTS_MODEL because WaveSpeed model slugs
change; if OmniVoice is unavailable, it falls back to the app's proven Gemini TTS so the mode
still produces a voiceover instead of hard-failing.
"""

import os
from pathlib import Path

import pipeline

# Override with WAVESPEED_TTS_MODEL if your account exposes a different OmniVoice slug.
OMNIVOICE_MODEL = os.environ.get("WAVESPEED_TTS_MODEL", "wavespeed-ai/omnivoice/text-to-speech").strip()
DEFAULT_VOICE_DESCRIPTION = "male, young adult, natural storyteller voice, american accent"
DEFAULT_SPEED = 0.95
# Gemini fallback voice closest to a calm young-adult male narrator.
_FALLBACK_GEMINI_VOICE = os.environ.get("REDDIT_FALLBACK_TTS_VOICE", "Charon").strip() or "Charon"


def _omnivoice(text, out_path, voice_description, speed, key, cancel_event=None, status_cb=None):
    payload = {
        "text": str(text or "").strip(),
        "voice_description": voice_description or DEFAULT_VOICE_DESCRIPTION,
        "speed": float(speed),
    }
    if status_cb:
        status_cb(f"OmniVoice TTS: submitting narration ({OMNIVOICE_MODEL})...")
    response = pipeline.request_json("POST", f"{pipeline.API_BASE}/{OMNIVOICE_MODEL}", key, payload, timeout=180)
    prediction_id = pipeline.unwrap_id(response)
    outputs, _ = pipeline.poll_wavespeed(prediction_id, key, timeout_s=600, cancel_event=cancel_event,
                                         status_cb=status_cb, label="OmniVoice")
    out_path = Path(out_path)
    ext = pipeline.output_extension(outputs[0], out_path.suffix or ".mp3")
    if out_path.suffix.lower() != ext.lower():
        out_path = out_path.with_suffix(ext)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    pipeline.download_file(outputs[0], out_path)
    return out_path


def generate_voiceover(text, out_path, voice_description=DEFAULT_VOICE_DESCRIPTION,
                       speed=DEFAULT_SPEED, cancel_event=None, status_cb=None):
    """Generate a narration to out_path (mp3/wav). Prefers OmniVoice, falls back to Gemini TTS.
    Returns the actual Path written. Raises RuntimeError with a readable message if both fail."""
    if not str(text or "").strip():
        raise RuntimeError("No narration text to synthesize.")
    key = os.environ.get("WAVESPEED_API_KEY", "").strip()
    if not key:
        raise RuntimeError("WAVESPEED_API_KEY is not set - cannot generate the voiceover.")
    try:
        return _omnivoice(text, out_path, voice_description, speed, key,
                          cancel_event=cancel_event, status_cb=status_cb)
    except Exception as exc:  # noqa: BLE001
        msg = f"{exc.__class__.__name__}: {exc}"
        if status_cb:
            status_cb(f"OmniVoice unavailable ({msg}); falling back to Gemini TTS.")
        try:
            return pipeline.generate_speech_gemini(
                text, Path(out_path).with_suffix(".wav"),
                key=key, voice=_FALLBACK_GEMINI_VOICE, cancel_event=cancel_event, status_cb=status_cb)
        except Exception as exc2:  # noqa: BLE001
            raise RuntimeError(
                f"WaveSpeed voiceover failed. OmniVoice error: {msg}. "
                f"Fallback error: {exc2.__class__.__name__}: {exc2}. "
                "Check WAVESPEED_API_KEY and WAVESPEED_TTS_MODEL.")
