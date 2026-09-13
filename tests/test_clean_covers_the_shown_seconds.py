"""The caption clean has to cover the seconds the scene shows, not the head of the file.

Measured on the school-rules Short (2026-09-05). Six clips were cleaned; three of them still read
0.027, 0.042 and 0.020 in the finished video. Zoomed into one: a yellow "3" on the schoolgirl's
collar is present in the source AND in the "cleaned" derivative, untouched. The fill ran over the
first `end - start` seconds of the file while the renderer seeks to `seedance_start_trim` first,
so it rebuilt seconds nobody ever sees and left the shown ones alone.
"""
import inspect

import agent_core
import caption_remover


def test_the_remover_accepts_a_start():
    assert "start" in inspect.signature(caption_remover.remove_caption_regions).parameters


def test_the_residual_check_looks_at_that_start():
    body = inspect.getsource(caption_remover.remove_caption_regions)
    assert "start=float(start or 0.0)" in body
    assert "AT THE SECONDS THE SCENE SHOWS" in body


def test_the_scrape_pass_measures_the_beat_not_the_distance_to_it():
    """These two assertions used to demand `+ _trim` inside the DURATION.

    That was the first attempt at this fix and it was wrong in an expensive way: making the
    duration reach from the head of the file to the end of the window sent ProPainter the whole
    run-up. Measured in a render log: 1150, 1627 and 697 frames rebuilt for scenes of 1.9s, 1.8s
    and 2.5s - about twenty times the work, and the reason a thirty-second Short took forty
    minutes to clean. The offset belongs in `start`, and only the beat's own length in `seconds`.
    """
    body = inspect.getsource(agent_core)
    assert '_trim = _seconds_of(scene, "seedance_start_trim", "source_trim", "start_trim")' in body
    assert '_used_len = max(1.0, float(scene.get("end", 0) or 0)' in body
    assert '- float(scene.get("start", 0) or 0)) + 0.5' in body
    assert "+ _trim" not in body.split("_used_len =")[1].split("_used_span")[0]


def test_the_scrape_pass_hands_the_start_over():
    body = inspect.getsource(agent_core)
    assert "seconds=_used_len, start=_trim," in body


def test_the_trim_still_reaches_the_cache_key():
    """Extending a scene must build a longer derivative, not reuse the shorter one - so the key
    keys on the END of the window even though the duration does not."""
    body = inspect.getsource(agent_core)
    assert "_used_span = _used_len + _trim" in body


def test_a_scene_with_no_trim_is_unchanged():
    """The common case must not grow a longer, slower fill for nothing."""
    assert agent_core._seconds_of({"start": 0.0, "end": 2.4}, "seedance_start_trim") == 0.0


def test_the_unproven_damage_check_is_gone():
    """Two measurements refuted the hypothesis it rested on - detail loss marks a SUCCESSFUL
    clean, because text is detail. It was removed rather than left in on a hunch."""
    assert not hasattr(caption_remover, "_fill_flattened_the_region")
