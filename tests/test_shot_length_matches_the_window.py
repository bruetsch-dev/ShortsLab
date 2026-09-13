"""A beat may never be longer than the clip the edit can cut for it.

V4 takes AT MOST MAX_SHOT (2.75s) out of any one source. A longer beat cannot be filled: the clip
is slowed to the stretch floor and then the picture freezes for the rest of the line. Measured
across five finished Shorts (2026-09-04):

    couples      5 beats  median 3.69s   longest 4.86s
    school       4 beats  median 4.67s   longest 7.04s
    future       5 beats  median 3.92s   longest 5.57s
    konbini      4 beats  median 5.06s   longest 7.86s
    dating       4 beats  median 4.72s   longest 6.27s

A reference Short that holds attention runs a 1.14s median and 13 shots in 20 seconds. These ran
four or five, each partly frozen. The caps came from a period when discovery was starving; the
same runs now return 86 to 117 usable windows.
"""
import inspect
import re

import agent_core
import scrape_v4

BODY = inspect.getsource(agent_core)


def test_the_chapter_cap_matches_what_a_window_can_fill():
    assert "_target_s = 2.55" in BODY
    assert "_max_s = 3.6" in BODY
    # V4 retains a shorter cadence candidate and also reviews a longer window up front.
    assert scrape_v4.MAX_SHOT == 2.75


def test_a_beat_that_needs_more_takes_a_longer_excerpt():
    """The cap is the DEFAULT window, not a limit on what a source can give.

    This used to assert that a comment existed, and it went red when the growth pass that
    comment described was removed - correctly, but for the wrong reason. The behaviour is
    what matters, and it is now decided before review rather than by extending a window
    into seconds nobody looked at: measured, a 40s source with no cuts could offer nothing
    longer than 2.30s, so a 3.02s beat had no candidate at all and two beats of the
    walking/eating Short were unfillable.
    """
    floor = scrape_v4.V4_STRETCH_FLOOR
    for beat in (1.43, 2.30, 3.02, 4.00):
        windows = scrape_v4._windows(40.0, 2.3, cuts=[], longest=beat)
        assert any(end - start >= beat * floor - .01 for start, end in windows), (
            f"a {beat}s beat has no window that can cover it")
    # and the cadence window survives, so short beats keep their choice of moments
    windows = scrape_v4._windows(40.0, 2.3, cuts=[], longest=4.0)
    assert any(abs((end - start) - 2.3) < .01 for start, end in windows)
    # and nothing extends a window after the fact: a reviewer saw exactly these seconds
    assert "chosen.end = round(" not in inspect.getsource(scrape_v4)


def test_the_old_supply_derived_cap_is_gone():
    assert "_max_s = max(3.4, _median * 2.15)" not in BODY


def test_the_hard_split_is_tighter_for_scraped_runs():
    """5.0s was raised while supply was the bottleneck; it now permits frozen shots."""
    assert 'else (3.6 if str(form.get("clip_source", "generate") or "").strip().lower()' in BODY


def _prompt_text():
    """The prompt is one string split across source lines, so rejoin before matching wording."""
    return re.sub(r'"\s*\n\s*"', "", inspect.getsource(agent_core.llm_micro_beat_plan))


def test_the_planner_is_told_the_real_limit():
    prompt = _prompt_text()
    assert "2.55 SECONDS IS THE AVERAGE" in prompt
    assert "roughly 12 to 14 beats" in prompt
    # Length is a retention question now, not a technical ceiling: a long source
    # can always give a longer excerpt.
    assert "A long source can always give a longer excerpt" in prompt


def test_the_planner_is_told_how_to_split_rather_than_merge():
    prompt = _prompt_text()
    assert "the hands instead of the room" in prompt
    assert "Merging is only for lines that have no picture of their own" in prompt


def test_generated_runs_keep_their_own_pacing():
    """Only scraped runs are limited by what somebody else filmed."""
    prompt = _prompt_text()
    assert "usually 0.8 to 2.2 seconds" in prompt
