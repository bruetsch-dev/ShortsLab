"""A beat is finished when it has a GOOD clip, not when it has any clip.

Measured on the pufferfish run (2026-09-03): every beat was filled, so nothing in the run saw a
problem - and two of the eight scenes were filler, a cash box on an office desk standing in for
"a padlocked metal box" and a street with a workshop standing in for nothing. Discovery had found
650 posts and the run inspected 120 of them before stopping.

Searching is now driven by per-beat quality and bounded by a time budget: keep looking while any
beat is still settling, spend the already-paid posts before buying new searches, and only take
the best-available clip once the budget is gone.
"""
import re
from pathlib import Path

SOURCE = (Path(__file__).resolve().parent.parent / "scrape_v4.py").read_text(encoding="utf-8")
LOOP = SOURCE[SOURCE.index("        already = {str(q).casefold() for q in requested}"):
              SOURCE.index("        uncovered = _weak_beats()\n        available =")]


def test_the_bar_is_on_topic_and_watchable():
    bar = SOURCE[SOURCE.index("def _good_enough("):SOURCE.index("def _weak_beats(")]
    assert 'float(verdict.get("relevance") or 0) >= 7' in bar
    assert "_interest_band(getattr(candidate, \"visual_interest\", 5.0)) >= 1" in bar


def test_the_budget_is_two_hours_by_default():
    line = re.search(r'quality_budget_s = float\(\(config or \{\}\)\.get\("v4_quality_budget_s"\) or (\d+)\)',
                     SOURCE)
    assert line and int(line.group(1)) == 7200


def test_filler_is_only_accepted_once_the_budget_is_gone():
    spent = SOURCE[SOURCE.index("def _budget_spent("):SOURCE.index("        already = {str(q)")]
    assert "if time.monotonic() < quality_deadline:" in spent
    assert "return False" in spent
    assert "take the best clip found" in spent


def test_it_stops_as_soon_as_every_beat_is_good():
    assert LOOP.index("if not uncovered:") < LOOP.index("take_wave(")
    assert "on-topic and worth watching" in LOOP


def test_paid_posts_are_spent_before_new_searches_are_bought():
    assert LOOP.index("take_wave(60)") < LOOP.index("_recovery_queries(")
    assert "discovery already paid for" in LOOP


def test_a_search_angle_is_never_tried_twice():
    assert "already.add(query.casefold())" in LOOP
    assert "if query.casefold() not in already" in LOOP


def test_the_loop_is_bounded_even_with_time_to_spare():
    rounds = int(re.search(r"for _attempt in range\((\d+)\)", LOOP).group(1))
    assert 2 <= rounds <= 10
    assert "no search angle is left to try" in LOOP


def test_only_distinct_records_are_ever_taken():
    wave = SOURCE[SOURCE.index("def take_wave("):SOURCE.index("shortlisted = take_wave(")]
    assert "if not sid[1] or sid in shortlisted_ids:" in wave
    assert "shortlisted_ids.add(sid)" in wave


def test_new_footage_is_reviewed_before_it_counts():
    assert LOOP.index("_vision_source_review(extra") < LOOP.index("candidates.extend(extra)")
