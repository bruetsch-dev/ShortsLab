"""A window that is mostly flat black filler is not footage, whatever the reviewer said.

Measured on the couples Short (2026-09-04). The hook opened on a "reply to this comment" layout:
the top half black, a Japanese caption slab across it, the couple squeezed into a strip. It is
NOT letterboxing - the top rows are bright, so measure_letterbox answered None - and the reviewer
accepted it at relevance 8 although its own prompt lists text cards and screen recordings as
disqualifiers. Measuring the frame separates it cleanly: that window scored 0.465 while every
other scene in the same edit sat at 0.064 or below.
"""
import numpy as np

import scrape_v4


def _clip(tmp_path, name, painter, frames=40, size=(720, 1280)):
    """Write a tiny real mp4 so the measurement runs on an actual decode."""
    import cv2
    path = tmp_path / name
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 30.0, size)
    for i in range(frames):
        writer.write(painter(i))
    writer.release()
    return path


def _full_bleed(i):
    frame = np.random.default_rng(i).integers(60, 200, (1280, 720, 3), dtype=np.uint8)
    return frame


def _half_black(i):
    frame = np.random.default_rng(i).integers(60, 200, (1280, 720, 3), dtype=np.uint8)
    frame[:640] = 0                      # a caption slab / pasted layout, not a matte
    return frame


def test_a_full_bleed_window_measures_as_picture(tmp_path):
    path = _clip(tmp_path, "clean.mp4", _full_bleed)
    assert scrape_v4.dead_frame_share(path) < 0.1


def test_a_layout_clip_is_measured_as_mostly_filler(tmp_path):
    path = _clip(tmp_path, "layout.mp4", _half_black)
    assert scrape_v4.dead_frame_share(path) > 0.4


def test_the_measurement_can_be_asked_about_a_window(tmp_path):
    """The filler can live only in the seconds the edit cuts to."""
    def late(i):
        return _half_black(i) if i >= 20 else _full_bleed(i)

    path = _clip(tmp_path, "late.mp4", late)
    assert scrape_v4.dead_frame_share(path, start=0.0, seconds=0.5) < 0.1
    assert scrape_v4.dead_frame_share(path, start=0.75, seconds=0.5) > 0.4


def test_an_unmeasurable_clip_is_not_punished(tmp_path):
    assert scrape_v4.dead_frame_share(tmp_path / "missing.mp4") == 0.0


def _cand(dead, score=5.0):
    candidate = scrape_v4.Candidate("s", "q", "p", 0.0, 2.75, score, "", "available")
    candidate.dead_share = dead
    candidate.visual_interest = 5.0
    candidate.caption_share = 0.0
    return candidate


def test_the_hook_refuses_filler_even_at_a_better_score():
    assert scrape_v4.assignment_rank(5.0, _cand(0.0), is_hook=True) > \
        scrape_v4.assignment_rank(6.0, _cand(0.46), is_hook=True)


def test_a_body_beat_still_tolerates_a_little():
    """A hole is worse than a shot with a small dark edge."""
    assert scrape_v4.assignment_rank(6.0, _cand(0.06)) > scrape_v4.assignment_rank(5.0, _cand(0.0))


def test_the_review_rejects_a_window_that_is_mostly_filler():
    import inspect
    body = inspect.getsource(scrape_v4._vision_source_review)
    assert 'getattr(candidate, "dead_share", 0.0) or 0.0) > 0.25' in body
    assert "flat black" in body


def test_an_unmeasured_window_defaults_to_no_filler():
    assert scrape_v4.Candidate("s", "q", "p", 0, 1, 1.0, "", "available").dead_share == 0.0
