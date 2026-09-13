"""V4 has to ask whether a clip is worth watching, not only whether it fits.

V3 has scored `visual_interest` since 2026-08-30, after "die meisten sind low quality clips und
uninteressant". V4 then became the default engine without any of it, so a run could be entirely
correct and entirely inert. Calibrated on 24 real review strips from the train-pushers run: the
scores spread 1-8 (mean 4.2), the two clips of a guard shoving commuters into a carriage scored 8,
and a clip with relevance 8 - a man left behind on the platform - scored 5.
"""
import scrape_v4


def test_the_prompt_asks_for_a_picture_score():
    text = scrape_v4.footage_review_prompt("tiktok__1", "some query", "the line")
    assert "visual_interest" in text
    # The anti-middle paragraph is the part that was measured to make the scale sort at all.
    assert "MOST FOOTAGE IS A 5" in text
    assert "empty platform is a 3" in text


def test_the_prompt_carries_the_line_when_there_is_one():
    assert "THE LINE THIS SHOT HAS TO CARRY" in scrape_v4.footage_review_prompt("s", "q", "a line")
    assert "THE LINE THIS SHOT HAS TO CARRY" not in scrape_v4.footage_review_prompt("s", "q", "")


def test_bands_are_coarse_on_purpose():
    assert scrape_v4._interest_band(9) == scrape_v4._interest_band(8) == 2
    assert scrape_v4._interest_band(7) == scrape_v4._interest_band(5) == 1
    assert scrape_v4._interest_band(4) == scrape_v4._interest_band(0) == 0


def test_an_unscored_clip_ranks_as_ordinary_not_as_dead():
    """A malformed reply must not silently push good footage below every scored clip."""
    assert scrape_v4._interest_band(None) == 1
    assert scrape_v4._interest_band("nonsense") == 1
    assert scrape_v4._interest_of({}) == 5.0
    assert scrape_v4._interest_of({"visual_interest": "x"}) == 5.0
    assert scrape_v4._interest_of({"visual_interest": 99}) == 10.0
    assert scrape_v4._interest_of({"visual_interest": -4}) == 0.0


def test_a_candidate_defaults_to_ordinary():
    cand = scrape_v4.Candidate("s", "q", "p", 0, 3, 5.0, "", "available")
    assert cand.visual_interest == 5.0


def test_striking_queries_ask_for_the_extreme_version_of_the_same_subject():
    out = scrape_v4._striking_queries(["東京駅 満員電車", "通勤ラッシュ 電車"], limit=2)
    assert out == ["東京駅 満員電車 ヤバい", "通勤ラッシュ 電車 衝撃"]
    # Rotating the intensifier matters: the same word three times is one search repeated.
    assert len(set(out)) == len(out)


def test_striking_queries_follow_the_language_of_the_query():
    assert scrape_v4._striking_queries(["tokyo rush hour train"], limit=1) == \
        ["tokyo rush hour train insane"]


def test_striking_queries_never_duplicate_a_term_already_being_searched():
    base = ["ramen shop", "ramen shop insane"]
    assert "ramen shop insane" not in scrape_v4._striking_queries(base, limit=2)


def test_striking_queries_survive_empty_input():
    assert scrape_v4._striking_queries([]) == []
    assert scrape_v4._striking_queries(["", "  "]) == []


class _Cand:
    def __init__(self, interest=5.0, caption_share=-1.0):
        self.visual_interest = interest
        self.caption_share = caption_share


def test_a_striking_clip_wins_a_near_tie():
    dull = scrape_v4.assignment_rank(5.5, _Cand(interest=4))
    striking = scrape_v4.assignment_rank(5.0, _Cand(interest=9))
    assert striking > dull


def test_interest_never_beats_a_clearly_better_match():
    """Banded and bounded: two bands are worth 1.6, less than the gap below."""
    weak_but_striking = scrape_v4.assignment_rank(4.0, _Cand(interest=9))
    strong_but_dull = scrape_v4.assignment_rank(7.0, _Cand(interest=2))
    assert strong_but_dull > weak_but_striking


def test_the_hook_weighs_the_picture_far_more_heavily():
    """Beat 0 has a second to stop a thumb; a clip that merely fits is a wasted opening."""
    fits_better = scrape_v4.assignment_rank(6.5, _Cand(interest=3), is_hook=True)
    stops_the_thumb = scrape_v4.assignment_rank(5.0, _Cand(interest=9), is_hook=True)
    assert stops_the_thumb > fits_better
    # On an ordinary beat the same two are nearly level: two bands are worth 1.6, about what a
    # strong metadata match earns. The hook multiplies that gap by nearly four.
    ordinary = (scrape_v4.assignment_rank(6.5, _Cand(interest=3))
                - scrape_v4.assignment_rank(5.0, _Cand(interest=9)))
    hook = fits_better - stops_the_thumb
    assert abs(ordinary) < 0.2
    assert hook < -3.0


def test_a_dull_clip_still_beats_an_empty_beat():
    """Band 0 sorts last but is never discarded - a hole is worse than a dull shot."""
    assert scrape_v4.assignment_rank(3.0, _Cand(interest=0)) == 3.0


def test_text_and_interest_both_apply():
    clean_striking = scrape_v4.assignment_rank(5.0, _Cand(interest=9, caption_share=0.0))
    texted_striking = scrape_v4.assignment_rank(5.0, _Cand(interest=9, caption_share=0.08))
    assert clean_striking > texted_striking
