"""No beat may show footage that is perceptually the same as footage already on screen.

Measured on projects/japanese_convenience_stores_look_effortless_but_the: scenes 08, 10 and 12
of the finished Short are the same snack shelf (16x16 mean-threshold signatures of the first
frame: 08 vs 10 = 0 bits apart, 08 vs 12 = 1, 10 vs 12 = 1, out of 256). Its
review/scrape_v4_report.json shows fingerprint 0e08080e1f3f3f7f assigned three times across
two different post ids - tiktok__7379470337752173841 twice at windows 1.56-4.31 and 9.56-12.31
despite MAX_PER_SOURCE = 1, and tiktok__7108113828465904897, a repost carrying the same
fingerprint. The ranking loop honours both limits, so the repeats came from the coverage fill,
which consulted neither used_fingerprints nor recorded what it took.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scrape_v4

SHELF_FINGERPRINT = "0e08080e1f3f3f7f"      # the real signature of the repeated shelf


def _cand(source_id, query="convenience store shelf", start=0.0, end=2.75,
          fingerprint="", score=5.0, window_fingerprint=None):
    """A candidate. `window_fingerprint` defaults to the source one - the static-shot case."""
    candidate = scrape_v4.Candidate(source_id, query, f"{source_id}.mp4", start, end, score,
                                    "", "available", fingerprint=fingerprint)
    candidate.window_fingerprint = (fingerprint if window_fingerprint is None
                                    else window_fingerprint)
    return candidate


def _used(source_id, fingerprint, window_fingerprint=None):
    """One entry of used_fingerprints: who it was, the file's signature, the window's."""
    return (source_id, fingerprint,
            fingerprint if window_fingerprint is None else window_fingerprint)


class TheFillNeverRepeatsWhatIsAlreadyOnScreen(unittest.TestCase):

    def test_a_second_window_that_looks_the_same_is_not_taken(self):
        """The exact defect: the same post filled two beats at two windows of one static shot.

        Refined 2026-09-04. The block used to apply to ANY second window of a used source, because
        the fingerprint is taken once per file - which also made "another window of this beat's
        own footage", the first preference here, impossible to reach. Windows now carry their own
        signature, so a genuinely different moment is allowed and a repeat of the same shot is
        not. This case is the static shelf: same window signature, still refused.
        """
        shelf = [_cand("tiktok__7379470337752173841", start=9.56, end=12.31,
                       fingerprint=SHELF_FINGERPRINT),
                 _cand("tiktok__7680411335410126100", fingerprint="3f7d3c2c67030100")]
        pick, repeats = scrape_v4._pick_coverage_fill(
            shelf, {"convenience store shelf"},
            used_sources={"tiktok__7379470337752173841": 1},
            used_ranges=[("tiktok__7379470337752173841", 1.56, 4.31)],
            used_fingerprints=[("tiktok__7379470337752173841", SHELF_FINGERPRINT,
                                SHELF_FINGERPRINT)])
        self.assertEqual(pick.source_id, "tiktok__7680411335410126100")
        self.assertFalse(repeats)

    def test_a_repost_under_a_different_post_id_is_not_taken(self):
        """Different id, same picture - the fingerprint is the only thing that catches it."""
        shelf = [_cand("tiktok__7108113828465904897", fingerprint=SHELF_FINGERPRINT),
                 _cand("tiktok__7680411335410126100", fingerprint="3f7d3c2c67030100")]
        pick, repeats = scrape_v4._pick_coverage_fill(
            shelf, {"convenience store shelf"}, used_sources={}, used_ranges=[],
            used_fingerprints=[("tiktok__7379470337752173841", SHELF_FINGERPRINT,
                                SHELF_FINGERPRINT)])
        self.assertEqual(pick.source_id, "tiktok__7680411335410126100")
        self.assertFalse(repeats)

    def test_an_almost_identical_repost_counts_as_the_same_footage(self):
        """Re-encodes shift a few bits; _fingerprints_are_reposts tolerates up to five."""
        near = "0e08080e1f3f3f7d"
        self.assertTrue(scrape_v4._fingerprints_are_reposts(near, SHELF_FINGERPRINT))
        shelf = [_cand("tiktok__repost", fingerprint=near),
                 _cand("tiktok__other", fingerprint="ffff0000ffff0000")]
        pick, _repeats = scrape_v4._pick_coverage_fill(
            shelf, set(), used_sources={}, used_ranges=[],
            used_fingerprints=[("tiktok__7379470337752173841", SHELF_FINGERPRINT,
                                SHELF_FINGERPRINT)])
        self.assertEqual(pick.source_id, "tiktok__other")

    def test_a_hole_is_still_worse_than_a_repeat(self):
        """With nothing unseen left the beat is filled anyway - and says that it repeats."""
        shelf = [_cand("tiktok__7108113828465904897", fingerprint=SHELF_FINGERPRINT)]
        pick, repeats = scrape_v4._pick_coverage_fill(
            shelf, set(), used_sources={}, used_ranges=[],
            used_fingerprints=[("tiktok__7379470337752173841", SHELF_FINGERPRINT,
                                SHELF_FINGERPRINT)])
        self.assertEqual(pick.source_id, "tiktok__7108113828465904897")
        self.assertTrue(repeats)

    def test_an_empty_shelf_still_yields_nothing(self):
        pick, repeats = scrape_v4._pick_coverage_fill([], set(), {}, [], [])
        self.assertIsNone(pick)
        self.assertFalse(repeats)

    def test_the_beats_own_footage_is_still_preferred_over_an_unused_stranger(self):
        """The existing principle must survive: this beat's own query beats a stranger."""
        own = _cand("tiktok__own", query="fresh onigiri restock", fingerprint="1111222233334444")
        stranger = _cand("tiktok__stranger", query="tokyo street", fingerprint="5555666677778888")
        pick, _repeats = scrape_v4._pick_coverage_fill(
            [stranger, own], {"fresh onigiri restock"}, used_sources={}, used_ranges=[],
            used_fingerprints=[])
        self.assertEqual(pick.source_id, "tiktok__own")

    def test_a_clip_with_no_fingerprint_is_not_treated_as_a_repeat(self):
        """An unreadable middle frame must not make every leftover clip unusable."""
        shelf = [_cand("tiktok__unknown", fingerprint="")]
        pick, repeats = scrape_v4._pick_coverage_fill(
            shelf, set(), {}, [], [_used("tiktok__x", SHELF_FINGERPRINT)])
        self.assertEqual(pick.source_id, "tiktok__unknown")
        self.assertFalse(repeats)


class TheFillRemembersWhatItUsed(unittest.TestCase):
    """Every later fill has to see the earlier ones, or the second repeat is as blind as the first."""

    def test_three_beats_in_a_row_cannot_all_take_the_same_picture(self):
        shelf = [_cand("tiktok__a", fingerprint=SHELF_FINGERPRINT),
                 _cand("tiktok__b", fingerprint="0e08080e1f3f3f7d"),   # repost of a
                 _cand("tiktok__c", fingerprint="3f7d3c2c67030100")]
        used_sources, used_ranges, used_fingerprints, taken = {}, [], [], []
        for _beat in range(3):
            pick, repeats = scrape_v4._pick_coverage_fill(
                shelf, set(), used_sources, used_ranges, used_fingerprints)
            self.assertIsNotNone(pick)
            shelf.remove(pick)
            used_sources[pick.source_id] = used_sources.get(pick.source_id, 0) + 1
            used_ranges.append((pick.source_id, pick.start, pick.end))
            if pick.fingerprint:
                used_fingerprints.append(_used(pick.source_id, pick.fingerprint,
                                               pick.window_fingerprint))
            taken.append((pick.source_id, repeats))
        self.assertEqual([t[0] for t in taken][:2], ["tiktok__a", "tiktok__c"])
        # Only the third beat runs out of unseen footage, and it is the one flagged as a repeat.
        self.assertEqual([t[1] for t in taken], [False, False, True])


if __name__ == "__main__":
    unittest.main()


class ASecondMomentOfTheSamePostIsAllowed(unittest.TestCase):
    """The first preference exists for exactly this, and it could never fire before.

    The fingerprint is taken once per FILE, so every window of a post carried the same value and
    "another window of this beat's own source" always looked like a repost of itself. Windows now
    carry their own signature: a different moment is taken, an identical shot is not.
    """

    def test_a_different_moment_of_the_same_post_is_taken(self):
        own = _cand("tiktok__own", start=9.6, end=12.35, fingerprint=SHELF_FINGERPRINT,
                    window_fingerprint="ffffffffffffffff")
        pick, repeats = scrape_v4._pick_coverage_fill(
            [own], {"convenience store shelf"},
            used_sources={"tiktok__own": 1},
            used_ranges=[("tiktok__own", 1.5, 4.25)],
            used_fingerprints=[_used("tiktok__own", SHELF_FINGERPRINT, SHELF_FINGERPRINT)])
        self.assertEqual(pick.source_id, "tiktok__own")
        self.assertFalse(repeats)

    def test_the_same_shot_at_another_timestamp_is_still_refused(self):
        same = _cand("tiktok__own", start=9.6, end=12.35, fingerprint=SHELF_FINGERPRINT)
        other = _cand("tiktok__else", fingerprint="3f7d3c2c67030100",
                      window_fingerprint="3f7d3c2c67030100")
        pick, _repeats = scrape_v4._pick_coverage_fill(
            [same, other], {"convenience store shelf"},
            used_sources={"tiktok__own": 1},
            used_ranges=[("tiktok__own", 1.5, 4.25)],
            used_fingerprints=[_used("tiktok__own", SHELF_FINGERPRINT, SHELF_FINGERPRINT)])
        self.assertEqual(pick.source_id, "tiktok__else")


class OnlyApprovedFootageIsAPreferredFill(unittest.TestCase):
    """The shelf keeps every file on disk, including gate rejects - their files are never deleted.

    The second preference asked only whether the SOURCE was unused, so once the accepted pool was
    spent a beat was handed a not-vertical or AI-watermarked clip in preference to an approved
    second window of a source already on screen.
    """

    def test_a_rejected_clip_never_beats_an_approved_one(self):
        rejected = _cand("tiktok__rejected", fingerprint="1111111111111111")
        rejected.status = "rejected"
        approved = _cand("tiktok__ok", start=9.6, end=12.35, fingerprint="2222222222222222",
                         window_fingerprint="aaaaaaaaaaaaaaaa")
        pick, _repeats = scrape_v4._pick_coverage_fill(
            [rejected, approved], set(), used_sources={"tiktok__ok": 1},
            used_ranges=[("tiktok__ok", 1.5, 4.25)],
            used_fingerprints=[_used("tiktok__ok", "2222222222222222", "2222222222222222")])
        self.assertEqual(pick.source_id, "tiktok__ok")

    def test_a_hole_is_still_worse_than_a_rejected_clip(self):
        rejected = _cand("tiktok__rejected", fingerprint="1111111111111111")
        rejected.status = "rejected"
        pick, repeats = scrape_v4._pick_coverage_fill([rejected], set(), {}, [], [])
        self.assertEqual(pick.source_id, "tiktok__rejected")
        self.assertTrue(repeats, "the caller has to mark it for replacement")
