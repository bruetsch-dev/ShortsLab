"""The letterbox gate has to measure the matte, not sample a fixed slice of the frame.

A clip with 96px of black on top and 46px underneath a 1280-high frame reached the train-pushers
timeline (2026-09-03): the old 10% band reached 128px into both ends, swallowed real picture, and
the means rose past the threshold. Re-checked across the 83 proxies that run downloaded, measuring
the matte catches that clip and changes no other verdict.
"""
import numpy as np

import scrape_v4


def _clip(monkeypatch, frame):
    """Feed _motion_and_black six frames that differ just enough to look like motion."""
    frames = []
    for i in range(6):
        f = frame.copy()
        mid = frame.shape[0] // 2
        f[mid - 120:mid + 120, :] = (30 + i * 35) % 255      # a moving band, not one row
        frames.append(f)
    monkeypatch.setattr(scrape_v4, "_sampled_gray_frames", None, raising=False)
    return frames


def _run(frames, monkeypatch):
    import cv2

    class _Cap:
        def __init__(self, *_a):
            self.i = 0

        def get(self, prop):
            return 60.0 if prop == cv2.CAP_PROP_FRAME_COUNT else 0.0

        def set(self, *_a):
            return True

        def read(self):
            if self.i >= len(frames):
                return False, None
            f = frames[self.i]; self.i += 1
            return True, np.dstack([f, f, f])

        def release(self):
            pass

    monkeypatch.setattr(cv2, "VideoCapture", _Cap)
    return scrape_v4._motion_and_black("clip.mp4")


def test_an_uneven_matte_is_caught(monkeypatch):
    frame = np.full((1280, 720), 120, dtype=np.uint8)
    frame[:96] = 0
    frame[-46:] = 0
    ok, why = _run(_clip(monkeypatch, frame), monkeypatch)
    assert not ok and "letterboxed" in why


def test_a_caption_printed_on_the_matte_does_not_hide_it(monkeypatch):
    """The first bright row is the text, not the picture."""
    frame = np.full((1280, 720), 130, dtype=np.uint8)
    frame[:400] = 0
    frame[120:150, 100:600] = 220      # a line of white type sitting on the black bar
    frame[-400:] = 0
    ok, why = _run(_clip(monkeypatch, frame), monkeypatch)
    assert not ok and "letterboxed" in why


def test_native_vertical_footage_still_passes(monkeypatch):
    frame = np.full((1280, 720), 110, dtype=np.uint8)
    ok, why = _run(_clip(monkeypatch, frame), monkeypatch)
    assert ok, why


def test_a_dark_sky_is_not_a_matte(monkeypatch):
    """Dark at the top only - there is no bar underneath, so nothing is letterboxed."""
    frame = np.full((1280, 720), 100, dtype=np.uint8)
    frame[:300] = 8
    ok, why = _run(_clip(monkeypatch, frame), monkeypatch)
    assert ok, why
