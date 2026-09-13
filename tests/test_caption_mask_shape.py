"""The caption detector has to cope with text that is not a wide, short strip.

Every fault these tests pin down was measured on real clips in
projects/manual_school_cleaning_no_repeats on 2026-09-02, where 7 of 12 clips were being refused
as "text slides" - three of them with no burned-in text at all.
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import cv2
    import numpy as np
except ImportError:                                          # pragma: no cover
    cv2 = None
    np = None

import caption_remover


@unittest.skipIf(cv2 is None, "OpenCV is not installed")
class CaptionMaskShape(unittest.TestCase):
    WIDTH, HEIGHT = 540, 960

    def _frames(self, boxes, count=14):
        """Frames with a bright block drawn in each box on a mid-grey moving background."""
        frames = []
        for index in range(count):
            frame = np.full((self.HEIGHT, self.WIDTH, 3), 90, np.uint8)
            # something that moves, so a temporal detector has texture to reject
            cv2.circle(frame, (60 + index * 12, 700), 40, (200, 30, 30), -1)
            for x, y, w, h in boxes:
                cv2.rectangle(frame, (x, y), (x + w, y + h), (245, 245, 245), -1)
            frames.append(frame)
        return frames

    def _with_ocr(self, boxes):
        """Patch the recogniser to read exactly `boxes` in every sampled frame."""
        clusters = [{"box": box, "frames": list(range(12))} for box in boxes]
        return mock.patch.multiple(
            "clip_scraper",
            _get_ocr=mock.Mock(return_value=object()),
            _ocr_text_rows=mock.Mock(side_effect=lambda frame, **kw: list(boxes)),
            _cluster_caption_boxes=mock.Mock(return_value=clusters),
        )

    def test_silence_from_the_recogniser_means_no_caption(self):
        """No readable text must produce no mask, not a guess.

        The shape heuristic reported 27.1% and 19.5% coverage on two clips that carried no text
        at all - faces and shirts read as ink - and both clips were then discarded as slides.
        """
        frames = self._frames([])
        with mock.patch.multiple(
                "clip_scraper",
                _get_ocr=mock.Mock(return_value=object()),
                _ocr_text_rows=mock.Mock(return_value=[]),
                _cluster_caption_boxes=mock.Mock(return_value=[])):
            self.assertIsNone(caption_remover.detect_caption_mask(frames))

    def test_a_multi_line_block_is_not_rejected_for_its_shape(self):
        """A caption block measured at 190x335 is neither a wide strip nor a narrow column."""
        box = (182, 98, 190, 335)
        with self._with_ocr([box]):
            mask = caption_remover.detect_caption_mask(self._frames([box]))
        self.assertIsNotNone(mask, "the block was rejected before it could be masked")
        x, y, w, h = box
        inside = int(np.count_nonzero(mask[y:y + h, x:x + w] > 32))
        self.assertGreater(inside, 0, "nothing was masked inside the recognised block")
        self.assertGreater(inside, 0.8 * int(np.count_nonzero(mask > 32)),
                           "most of the mask landed outside the text")

    def test_a_vertical_caption_keeps_its_far_ends(self):
        """Two stacked boxes 765px apart: the focus step must not keep only one of them.

        This is the fault that let ProPainter rebuild one glyph of 掃除の時間 and leave four
        standing, because the mask was clipped to a single 18%-tall horizontal band.
        """
        top, bottom = (182, 98, 190, 335), (180, 536, 182, 327)
        with self._with_ocr([top, bottom]):
            mask = caption_remover.detect_caption_mask(self._frames([top, bottom]))
        self.assertIsNotNone(mask)
        ink_top = int(np.count_nonzero(mask[98:433] > 32))
        ink_bottom = int(np.count_nonzero(mask[536:863] > 32))
        self.assertGreater(ink_top, 0, "the upper half of the caption was dropped")
        self.assertGreater(ink_bottom, 0, "the lower half of the caption was dropped")

    def test_the_ceiling_reflects_what_was_measured(self):
        """4.6% and 10.8% cleaned perfectly; 12.5% and 19.7% came out smeared."""
        self.assertGreater(caption_remover.MAX_COVERAGE, 0.108)
        self.assertLess(caption_remover.MAX_COVERAGE, 0.125)


if __name__ == "__main__":
    unittest.main()
