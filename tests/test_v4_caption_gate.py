"""V4 must measure burned-in text at the source gate, not trust the vision model's label.

Measured 2026-09-02 on the finished Short `japanese_convenience_stores_look_effortless_but_the`:
four of thirteen scenes carried Japanese creator text over 14-20% of the frame. The removal pass
correctly refused them ("this is a slide, not a captioned clip") - but by then they were already
in the edit, so the Short shipped with someone else's captions on screen.

caption_remover's own docstring makes the point: asking a vision model whether a caption is
removable "produced captions on solid plates and full-height vertical text in finished Shorts,
because that judgement is a guess. This is not."
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scrape_v4


class CaptionGate(unittest.TestCase):

    def setUp(self):
        with open(scrape_v4.__file__, encoding="utf-8") as handle:
            self.source = handle.read()

    def test_a_candidate_carries_its_measured_text_share(self):
        self.assertIn("caption_share: float = -1.0", self.source)
        field = scrape_v4.Candidate(source_id="x", query="q", path="p", start=0.0, end=1.0,
                                    score=0.0, reason="", status="available")
        self.assertEqual(field.caption_share, -1.0)

    def test_a_source_over_the_ceiling_is_rejected_before_it_can_be_assigned(self):
        self.assertIn("if share > share_ceiling:", self.source)
        self.assertIn("more than the removal pass can rebuild", self.source)

    def test_cleaning_is_decided_by_the_measurement_not_the_label(self):
        self.assertIn('"blur_captions": chosen.caption_share > 0.015,', self.source)
        self.assertIn('"blur_captions": pick.caption_share > 0.015,', self.source)
        self.assertNotIn('"blur_captions": str((chosen.vision or {}).get("caption_severity")',
                         self.source)

    def test_a_failed_measurement_does_not_reject_the_clip(self):
        """-1.0 means "could not measure", which is not evidence against a source."""
        self.assertIn("share, share_ceiling = -1.0, 1.0", self.source)


if __name__ == "__main__":
    unittest.main()
