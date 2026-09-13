"""The reviewer must look at the slice that will be on screen, not at the post it came from.

Measured on the couples Short (2026-09-04). The strip sampled the whole source at 12/50/84% and
one verdict covered every window of it, while the edit shows a single ~2.75s slice taken at 8, 49
or 80%. On a 30-second montage those are different scenes, so the verdict described footage the
viewer never sees:

    vision said "a couple embraces by a pedestrian crosswalk"  -> the window rendered a man
                                                                  holding a photo card
    vision said "an upscale restaurant interior, premium beef" -> the window rendered a flower shop

Four of five scenes were wrong that way, which is why the run's numbers looked healthy and the
finished video did not.
"""
import inspect

import scrape_v4

SOURCE = inspect.getsource(scrape_v4._vision_source_review)


def test_the_strip_is_cut_from_the_window():
    assert "start_f = max(0, int(float(candidate.start or 0.0) * fps))" in SOURCE
    assert "span = max(1, end_f - start_f)" in SOURCE
    assert "start_f + int(span * fraction)" in SOURCE


def test_every_window_is_reviewed_not_one_per_post():
    """Two windows of one post are two different pictures and each needs its own verdict."""
    assert "reviewable = [item for item in candidates if item.status == \"available\"]" in SOURCE
    assert "for candidate in reviewable:" in SOURCE
    assert "sources.setdefault" not in SOURCE


def test_each_window_gets_its_own_strip_file():
    """Sharing a filename made the second window overwrite the first on disk."""
    assert 'f"v4_vision_{source_id}_{candidate.start:.2f}.jpg"' in SOURCE


def test_a_verdict_lands_only_on_the_window_it_judged():
    assert "candidate.vision = dict(decision)" in SOURCE
    assert "for item in source_candidates:" not in SOURCE


def test_burned_in_text_is_measured_inside_the_window():
    """A post can be clean where it starts and carry a caption bar where the edit cuts in."""
    assert "start=float(candidate.start or 0.0)" in SOURCE


def test_a_failed_review_only_costs_that_window():
    assert 'unavailable for this window' in SOURCE
    assert 'no usable verdict for this window' in SOURCE
