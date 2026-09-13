"""Letterboxed sources get rebuilt instead of discarded.

Measured on the fish run 2026-09-03 (projects/slicing_this_japanese_fish_legally_requires_a):
49 of 300 downloaded sources were thrown away as "persistent top-and-bottom black bars".
The picture inside those bars is fine, so the gate now crops the measured matte and puts the
sharp content back on a blurred, zoomed copy of itself. Re-running the real repair over the
first 12 of those proxies: 8 rebuilt and passed the same gate afterwards, 4 stayed rejected
because under 40% of the frame was picture.
"""
import shutil
import subprocess

import numpy as np
import pytest

import scrape_v4


def _letterboxed_frame(height=1280, width=720, top=200, bottom=260, level=140):
    frame = np.full((height, width), level, dtype=np.uint8)
    frame[:top] = 0
    frame[height - bottom:] = 0
    return frame


def _feed(monkeypatch, frame, count=6):
    monkeypatch.setattr(scrape_v4, "_sampled_gray_frames",
                        lambda *_a, **_k: [frame.copy() for _ in range(count)])


def test_the_matte_the_gate_rejected_is_the_matte_that_gets_measured(monkeypatch):
    _feed(monkeypatch, _letterboxed_frame(top=200, bottom=260))
    assert scrape_v4.measure_letterbox("clip.mp4") == (200, 260, 1280, 720)


def test_native_vertical_footage_measures_no_matte(monkeypatch):
    _feed(monkeypatch, np.full((1280, 720), 120, dtype=np.uint8))
    assert scrape_v4.measure_letterbox("clip.mp4") is None


def test_a_caption_on_the_bar_does_not_shrink_the_measurement(monkeypatch):
    frame = _letterboxed_frame(top=400, bottom=400)
    frame[120:150, 100:600] = 220        # a line of white type printed on the black bar
    _feed(monkeypatch, frame)
    measured = scrape_v4.measure_letterbox("clip.mp4")
    assert measured is not None and measured[0] > 300


def test_a_mostly_black_frame_is_still_rejected(monkeypatch, tmp_path):
    """Under 40% picture there is nothing worth blowing up - keep rejecting, and say why."""
    _feed(monkeypatch, _letterboxed_frame(top=450, bottom=450))
    src = tmp_path / "v4_tiktok_1.mp4"; src.write_bytes(b"x" * 8192)
    out, note = scrape_v4.unletterbox_clip(src, ffmpeg="ffmpeg")
    assert out is None
    assert "30%" in note and "450px" in note


def test_the_rebuild_crops_the_measured_bars_and_keeps_the_original_frame(monkeypatch, tmp_path):
    _feed(monkeypatch, _letterboxed_frame(top=200, bottom=260))
    src = tmp_path / "v4_tiktok_2.mp4"; src.write_bytes(b"x" * 8192)
    seen = {}

    def _fake_run(cmd, **_kwargs):
        seen["cmd"] = cmd
        out = tmp_path / cmd[-1] if not str(cmd[-1]).startswith(str(tmp_path)) else cmd[-1]
        open(out, "wb").write(b"y" * 9000)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(scrape_v4.subprocess, "run", _fake_run)
    out, note = scrape_v4.unletterbox_clip(src, ffmpeg="ffmpeg")
    assert out is not None and out.exists()
    graph = seen["cmd"][seen["cmd"].index("-filter_complex") + 1]
    assert "crop=720:820:0:200" in graph      # exactly the measured content strip
    assert "boxblur" in graph and "overlay=(W-w)/2:(H-h)/2" in graph
    assert "scale=-2:1280" in graph           # blurred copy zoomed to cover the full frame
    assert "-map" in seen["cmd"] and "0:a?" in seen["cmd"]      # audio survives
    assert "200px/260px" in note


def test_two_different_mattes_of_one_clip_get_different_filenames(monkeypatch, tmp_path):
    """Derived clips have collided on a basename in this repo before (speed_/capblur_/replaced_)."""
    src = tmp_path / "v4_tiktok_3.mp4"; src.write_bytes(b"x" * 8192)

    def _fake_run(cmd, **_kwargs):
        open(cmd[-1], "wb").write(b"y" * 9000)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(scrape_v4.subprocess, "run", _fake_run)
    names = []
    for bars in ((200, 260), (150, 150)):
        _feed(monkeypatch, _letterboxed_frame(top=bars[0], bottom=bars[1]))
        out, _note = scrape_v4.unletterbox_clip(src, ffmpeg="ffmpeg")
        names.append(out.name)
    assert names[0] != names[1]
    assert all(name.startswith("unbox_") for name in names)


def test_a_failed_rebuild_leaves_nothing_behind(monkeypatch, tmp_path):
    _feed(monkeypatch, _letterboxed_frame())
    src = tmp_path / "v4_tiktok_4.mp4"; src.write_bytes(b"x" * 8192)

    def _fake_run(cmd, **_kwargs):
        return subprocess.CompletedProcess(cmd, 1, "", "Invalid argument")

    monkeypatch.setattr(scrape_v4.subprocess, "run", _fake_run)
    out, note = scrape_v4.unletterbox_clip(src, ffmpeg="ffmpeg")
    assert out is None and "rebuild failed" in note
    assert not list(tmp_path.glob("unbox_*"))


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
                    reason="needs a real ffmpeg to rebuild a frame")
def test_a_real_letterboxed_clip_passes_the_gate_after_the_rebuild(tmp_path):
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    src = tmp_path / "boxed.mp4"
    subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-f", "lavfi",
                    "-i", "testsrc=size=720x900:rate=24:duration=2",
                    "-vf", "pad=720:1280:0:190:black", "-pix_fmt", "yuv420p", str(src)],
                   check=True, timeout=180)
    assert scrape_v4._motion_and_black(src)[0] is False
    out, note = scrape_v4.unletterbox_clip(src, ffmpeg=ffmpeg)
    assert out is not None, note
    ok, why = scrape_v4._motion_and_black(out)
    assert ok, why

    def _dims(path):
        raw = subprocess.run([ffprobe, "-v", "error", "-select_streams", "v:0",
                              "-show_entries", "stream=width,height", "-of", "csv=p=0",
                              str(path)], capture_output=True, text=True, timeout=60).stdout
        return raw.strip()

    assert _dims(out) == _dims(src) == "720,1280"
