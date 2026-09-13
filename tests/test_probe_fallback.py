"""A failing ffprobe must not turn good footage into "corrupt download".

Measured on the train-pushers run (2026-09-03): 126 of 190 downloads were rejected with
"corrupt download (no readable video stream)" and every one of the 16 re-downloaded afterwards
was a valid vertical video. ffprobe is the fast path, not the authority.
"""
import subprocess

import scrape_v4


class _Video:
    """A file that no external process can read, but the decoder can."""

    def __init__(self, tmp_path):
        self.path = tmp_path / "clip.mp4"
        self.path.write_bytes(b"\x00\x00\x00\x20ftypisom" + b"\x00" * (200 * 1024))


def test_opencv_answers_when_ffprobe_is_missing(tmp_path, monkeypatch):
    clip = _Video(tmp_path)
    monkeypatch.setattr(scrape_v4, "_opencv_probe", lambda path: (720, 1280, 12.5))
    assert scrape_v4._probe(clip.path, "C:/nowhere/ffprobe.exe") == (720, 1280, 12.5)


def test_a_timeout_does_not_condemn_the_file(tmp_path, monkeypatch):
    clip = _Video(tmp_path)

    def _timeout(*_a, **_k):
        raise subprocess.TimeoutExpired("ffprobe", 30)

    monkeypatch.setattr(scrape_v4.subprocess, "check_output", _timeout)
    monkeypatch.setattr(scrape_v4, "_opencv_probe", lambda path: (720, 1280, 9.0))
    scrape_v4._PROBE_FALLBACKS.clear()
    # The geometry now comes from the decoder, so the file is judged on whether it DECODES -
    # never on whether one subprocess answered.
    _ok, reason, w, h, duration = scrape_v4.validate_media(clip.path, "ffprobe")
    assert (w, h, duration) == (720, 1280, 9.0)
    assert "no readable video stream" not in reason
    assert scrape_v4._PROBE_FALLBACKS == ["ffprobe timed out"]
    scrape_v4._PROBE_FALLBACKS.clear()


def test_a_genuinely_unreadable_file_is_still_rejected(tmp_path, monkeypatch):
    clip = _Video(tmp_path)
    monkeypatch.setattr(scrape_v4, "_opencv_probe", lambda path: (0, 0, 0.0))
    ok, reason, _w, _h, _d = scrape_v4.validate_media(clip.path, "C:/nowhere/ffprobe.exe")
    assert not ok and "no readable video stream" in reason


def test_ffprobe_stays_the_fast_path(tmp_path, monkeypatch):
    clip = _Video(tmp_path)
    calls = []
    monkeypatch.setattr(scrape_v4, "_opencv_probe",
                        lambda path: calls.append(path) or (1, 1, 1.0))
    monkeypatch.setattr(scrape_v4.subprocess, "check_output",
                        lambda *a, **k: '{"streams":[{"width":576,"height":1024,"duration":"8.0"}]}')
    assert scrape_v4._probe(clip.path, "ffprobe") == (576, 1024, 8.0)
    assert calls == []


def test_the_child_never_inherits_our_stdin(tmp_path, monkeypatch):
    clip = _Video(tmp_path)
    seen = {}
    monkeypatch.setattr(scrape_v4.subprocess, "check_output",
                        lambda *a, **k: seen.update(k) or '{"streams":[{"width":9,"height":16,"duration":"1"}]}')
    scrape_v4._probe(clip.path, "ffprobe")
    assert seen.get("stdin") is subprocess.DEVNULL
