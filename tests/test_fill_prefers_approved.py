"""The coverage fill must prefer what this run APPROVED, not what the model claimed.

Measured on the couples Short (2026-09-04). A window carrying 18% burned-in creator text had been
rejected for exactly that - the removal pass cannot rebuild it - yet it was picked to fill a beat
while two APPROVED windows of the same post, at 8% and 2% text, sat unused. The ranking asked the
model's `accept` flag, which was true, instead of the run's own conclusion after measuring.
"""
import scrape_v4


def _cand(status, relevance=7, share=0.0, interest=5.0, score=5.0, accept=True):
    candidate = scrape_v4.Candidate("tiktok__1", "q", "p.mp4", 0.0, 2.75, score, "", status)
    candidate.vision = {"accept": accept, "relevance": relevance}
    candidate.caption_share = share
    candidate.visual_interest = interest
    return candidate


def test_an_approved_window_outranks_a_rejected_one_the_model_liked():
    approved = _cand("available", relevance=7, share=0.08)
    rejected = _cand("rejected", relevance=8, share=0.18, accept=True)
    assert scrape_v4.fill_rank(approved) > scrape_v4.fill_rank(rejected)


def test_text_over_the_ceiling_sinks_below_anything_clean():
    dirty = _cand("rejected", relevance=10, share=0.30)
    clean = _cand("rejected", relevance=3, share=0.00)
    assert scrape_v4.fill_rank(clean) > scrape_v4.fill_rank(dirty)


def test_among_equals_the_cleaner_window_wins():
    assert scrape_v4.fill_rank(_cand("available", relevance=8, share=0.01)) > \
        scrape_v4.fill_rank(_cand("available", relevance=8, share=0.09))


def test_a_striking_window_wins_a_tie_on_relevance():
    assert scrape_v4.fill_rank(_cand("available", relevance=8, interest=9)) > \
        scrape_v4.fill_rank(_cand("available", relevance=8, interest=3))


def test_a_candidate_with_no_verdict_still_ranks():
    """A hole is worse than a dull shot, so an unreviewed leftover must stay orderable."""
    bare = scrape_v4.Candidate("tiktok__2", "q", "p.mp4", 0.0, 2.75, 4.0, "", "rejected")
    assert isinstance(scrape_v4.fill_rank(bare), tuple)
    assert scrape_v4.fill_rank(_cand("available")) > scrape_v4.fill_rank(bare)


def test_the_shelf_uses_this_comparator():
    import inspect
    body = inspect.getsource(scrape_v4)
    assert "key=fill_rank, reverse=True" in body
