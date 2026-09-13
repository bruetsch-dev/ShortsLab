"""ProPainter may only ever see the frames the viewer sees.

Bounding the remover with "the first n seconds" still made it rebuild everything BEFORE the beat:
a beat starting at 11s handed it 15 seconds of frames for 2.75 seconds of screen time, and one
render sat on the GPU for 37 minutes (2026-09-04). Cutting the window out first turns that into
about three seconds of work, and the scene then plays the cut file from its own start.
"""
import inspect

import agent_core

BODY = inspect.getsource(agent_core.prepare_timeline_clips)


def test_the_window_is_cut_before_it_is_cleaned():
    cut_at = BODY.index('"-ss", f"{max(0.0, trim - lead):.3f}"')
    clean_at = BODY.index("caption_remover.remove_caption_regions")
    assert cut_at < clean_at


def test_the_cut_covers_the_beat_with_slack_on_both_sides():
    assert "lead = min(trim, 0.35)" in BODY
    assert "span = wanted + lead + 0.6" in BODY
    assert '"-t", f"{span:.3f}"' in BODY


def test_the_remover_is_bounded_to_that_window():
    assert "seconds=span + 0.4" in BODY


def test_the_scene_stops_seeking_past_the_cut():
    """A cut file with the old offset would skip straight past the picture."""
    assert 'scene["clip"] = scene["asset"] = target.name' in BODY
    assert 'scene[key] = round(lead, 3)' in BODY


def test_a_failed_cut_falls_back_to_the_whole_clip():
    """Slower, but it still cleans - an export must not fail because a trim did not."""
    assert "target = cut if (cut and cut.exists() and cut.stat().st_size > 4096) else path" in BODY
    assert "if done and target is not path:" in BODY


def test_an_existing_cut_is_reused():
    assert "if not (cut.exists() and cut.stat().st_size > 4096):" in BODY


def test_the_child_never_inherits_our_stdin():
    """The same Windows trap that once deadlocked ffprobe."""
    assert "stdin=subprocess.DEVNULL" in BODY
