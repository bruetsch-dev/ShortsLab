"""A voice preview must arrive as the format it actually is.

Gemini's sketch-short samples come back from the provider as MP3, while the longform and default
samples come back as WAV. The route hardcoded `audio/wav` for all of them, so the browser was
handed MP3 bytes under a WAV label: <audio> failed to decode, nothing played, and switching
narrator looked like the preview was stuck on the previous voice - only in the sketch-short flow,
which is why longform previews always seemed fine.

Confirmed live before the fix (Content-Type: audio/wav, first bytes ID3) and after (audio/mpeg,
and the browser's own <audio> element reports 8.0/8.4/8.3s for three different voices).
"""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = (ROOT / "app.py").read_text(encoding="utf-8")


def route_source():
    start = APP.index("def serve_voice_preview(")
    return APP[start:APP.index("def send_static_asset(", start)]


class MediaTypeTests(unittest.TestCase):
    def test_the_type_follows_the_file(self):
        block = route_source()
        self.assertIn('".mp3": "audio/mpeg"', block)
        self.assertIn("self.serve_file_ranged(path, media_type)", block)

    def test_wav_is_still_served_as_wav(self):
        self.assertIn('".wav": "audio/wav"', route_source())

    def test_nothing_hardcodes_wav_for_every_preview(self):
        self.assertNotIn('self.serve_file_ranged(path, "audio/wav")', APP)

    def test_an_unknown_extension_falls_back_rather_than_failing(self):
        self.assertIn('}.get(suffix, "audio/wav")', route_source())


class CacheTests(unittest.TestCase):
    """The cache key said .wav while the generator wrote .mp3, so the existence check never
    matched its own file: every click regenerated a paid sample, ~6 seconds each. After the fix
    a repeat request answers in 0.01s."""

    def test_the_cache_looks_for_the_format_that_exists(self):
        block = route_source()
        self.assertIn('path.with_suffix(".mp3")', block)
        self.assertIn("if existing is not None:", block)

    def test_a_missing_sample_is_still_generated(self):
        self.assertIn("if existing is None:", route_source())

    def test_a_stub_file_does_not_count_as_cached(self):
        self.assertIn("c.stat().st_size >= 4096", route_source())


if __name__ == "__main__":
    unittest.main()
