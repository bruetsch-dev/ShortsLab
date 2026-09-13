"""How well a clip carries its line has to decide which clip gets the line.

Measured on the school-rules Short (2026-09-04). The payoff beat - "your final subject is
literally cleaning the floor" - was given a clip of a girl turning on a corridor tap, relevance 7,
while SEVENTEEN clips of students actually cleaning the floor sat available in the same run:

    "Japanese high school students kneel on the floor to wipe it clean."          relevance 10
    "Japanese high school students flip chairs onto desks to clear the floor."    relevance 10
    "Japanese high school students crouch and wipe the classroom floor."          relevance 10

The reviewer's relevance was used only as a gate at >= 7 and then dropped. The sort ran on
`score`, which is 5.0 plus a popularity term plus a metadata bonus - so a popular 7 beat a 10.
"""
import scrape_v4


def _cand(relevance, popularity=0.0, interest=5.0, text=0.0, dead=0.0):
    candidate = scrape_v4.Candidate("s", "q", "p.mp4", 0.0, 2.75, 5.0 + popularity, "", "available")
    candidate.vision = {"relevance": relevance}
    candidate.visual_interest = interest
    candidate.caption_share = text
    candidate.dead_share = dead
    return candidate


def _rank(candidate, is_hook=False):
    return scrape_v4.assignment_rank(candidate.score, candidate, is_hook=is_hook)


def test_the_measured_case_now_goes_the_other_way():
    tap = _cand(relevance=7, popularity=0.9)
    floor = _cand(relevance=10)
    assert _rank(floor) > _rank(tap)


def test_each_point_above_the_gate_counts():
    assert _rank(_cand(10)) > _rank(_cand(9)) > _rank(_cand(8)) > _rank(_cand(7))


def test_below_the_gate_nothing_is_subtracted():
    """Relevance under 7 never reaches the ranking, and must not push a clip below zero."""
    assert _rank(_cand(3)) == _rank(_cand(7))


def test_a_missing_verdict_does_not_crash_the_sort():
    bare = scrape_v4.Candidate("s", "q", "p.mp4", 0.0, 2.75, 5.0, "", "available")
    assert isinstance(_rank(bare), float)


def test_relevance_does_not_override_a_screen_full_of_filler():
    """A perfect match that is half black bar is still the wrong opening shot."""
    perfect_but_dead = _cand(10, dead=0.5)
    ordinary_but_clean = _cand(7)
    assert _rank(ordinary_but_clean, is_hook=True) > _rank(perfect_but_dead, is_hook=True)


def test_relevance_outweighs_burned_in_text_but_not_by_much():
    """Text costs at most 1.2; three points of relevance is worth more than that."""
    assert _rank(_cand(10, text=0.08)) > _rank(_cand(7))
    assert _rank(_cand(8)) > _rank(_cand(8, text=0.08))
