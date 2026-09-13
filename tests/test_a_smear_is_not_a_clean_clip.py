"""The fourth way caption removal fails - and why measuring it is not the same as gating on it.

The residual check asks whether TEXT is still readable. `fill_damage` asks whether the area came
back dark. Neither sees the failure that reaches the screen: where the background behind a
caption is never revealed, ProPainter invents it and returns the picture dragged along one axis.
The words are gone and so is the night market that was behind them.

`fill_direction` measures that - gradient energy inside the rebuilt area against the picture
immediately around it. On twelve fills of two Shorts, all labelled by eye, it separated cleanly:

    smeared  0.640  0.650  0.753  0.876
    clean    0.923  0.925  0.945  0.969  1.150  1.154  1.181  1.231

So it was made a gate, with a floor at 0.90. A third project took an hour to destroy that:

    0.829  the caption is still there and the children are doubled   SMEARED - rejected, right
    0.840  jacket, pleats, shoes, tights intact, caption gone         CLEAN - rejected, WRONG
    0.901  a pale streak dragged through the hair                    smeared - kept
    0.921  a crouching child rebuilt as a black lump                 smeared - kept
    1.006  a yellow glyph stub and a bar across the sweater          fragments - kept
    1.340, 1.559  a ghost caption readable through the sleeve        residue - kept

One correct rejection, one that threw away the cleanest fill of the whole set, and five defects
waved through from 0.901 to 1.559. The classes overlap in both directions; the twelve-sample
separation was the sample, not the world.

The measurement stays, because it is the only thing in the pass that looks at the PICTURE rather
than at text or brightness, and because a future calibration has to be built from something. It
decides nothing. These tests exist to keep it honest AND to keep it out of the decision.
"""

import inspect
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import caption_remover

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def striped(height=300, width=200, frames=12):
    """Vertical stripes: strong gradient across x, almost none along y."""
    out = []
    for index in range(frames):
        column = np.arange(width) + index * 3
        frame = np.where((column // 4) % 2 == 0, 40, 200).astype(np.uint8)
        out.append(np.repeat(frame[None, :], height, axis=0))
    return out


def smeared(frames, top=100, bottom=170):
    out = []
    for frame in frames:
        copy = frame.copy()
        band = copy[top:bottom, :].astype(np.float32)
        for _ in range(6):
            band = (np.roll(band, 1, axis=1) + band + np.roll(band, -1, axis=1)) / 3.0
        copy[top:bottom, :] = band.astype(np.uint8)
        out.append(copy)
    return out


class TheMeasurementStillWorks(unittest.TestCase):
    def test_a_dragged_band_reads_lower_than_an_untouched_rebuild(self):
        source = striped()
        dragged = caption_remover.fill_direction(source, smeared(source))
        moved = [f.copy() for f in source]
        for frame in moved:                          # a real change, but not a directional one
            frame[100:170, :] = np.roll(frame[100:170, :], 7, axis=1)
        faithful = caption_remover.fill_direction(source, moved)
        self.assertLess(dragged, faithful,
                        f"smeared {dragged:.3f} did not read below faithful {faithful:.3f}")


class ItSaysWhenItCouldNotLook(unittest.TestCase):
    """1.0 meant both "perfectly balanced" and "never measured". While this was a gate, that
    silence passed two known smears while looking exactly like a gate that had examined them."""

    def test_no_frames_is_recorded_as_unmeasured(self):
        note = {}
        self.assertEqual(1.0, caption_remover.fill_direction([], [], note=note))
        self.assertIn("unmeasured", note)

    def test_a_fill_that_changed_nothing_is_recorded_as_unmeasured(self):
        source = striped()
        note = {}
        caption_remover.fill_direction(source, [f.copy() for f in source], note=note)
        self.assertIn("unmeasured", note)

    def test_propainters_own_size_does_not_break_the_comparison(self):
        """It returns 640 where the work clip is 960; the mask did not fit, and the IndexError
        landed in the remover's catch-all, which reads as "caption removal failed"."""
        import cv2
        source = striped(height=300, width=200)
        half = [cv2.resize(f, (100, 150), interpolation=cv2.INTER_AREA) for f in smeared(source)]
        note = {}
        ratio = caption_remover.fill_direction(source, half, note=note)
        self.assertNotIn("the source frames change size", str(note))
        self.assertIsInstance(ratio, float)


class AndItDecidesNothing(unittest.TestCase):
    """It was a gate for four hours. A third project showed it rejecting the cleanest fill of the
    set at 0.840 while keeping wrecked ones at 0.901, 0.921, 1.006, 1.340 and 1.559."""

    def setUp(self):
        self.src = inspect.getsource(caption_remover.remove_caption_regions)

    def test_no_threshold_constant_survives(self):
        self.assertFalse(hasattr(caption_remover, "SMEAR_MIN"),
                         "a floor is back; the labelled set says there is no place to put one")

    def test_the_measurement_never_returns_zero_on_its_own(self):
        after = self.src[self.src.index("direction = fill_direction("):]
        window = after[:after.index("return 1")]
        self.assertNotIn("return 0", window,
                         "the directional measurement is rejecting clips again")

    def test_it_is_still_recorded_on_every_fill(self):
        self.assertIn('info["fill_direction"] = round(direction, 3)', self.src)

    def test_and_so_is_the_reason_it_could_not_be_measured(self):
        self.assertIn('info["fill_direction_unmeasured"]', self.src)


class FourOutcomesFourSentences(unittest.TestCase):
    """"no removable text found" was the label for a crashed ProPainter, an out-of-memory GPU, a
    hole, leftover text and a genuinely clean clip. It was on 11 of 15 scenes."""

    def setUp(self):
        self.core = open(os.path.join(ROOT, "agent_core.py"), encoding="utf-8").read()
        self.remover = open(os.path.join(ROOT, "caption_remover.py"), encoding="utf-8").read()

    def test_the_remover_records_each_one(self):
        for key in ('info["residual"]', 'info["failed"]', 'info["hole"]'):
            self.assertIn(key, self.remover)

    def test_and_the_scene_writes_them_down_separately(self):
        for phrase in ("still read as", "came back as a hole", "failed: ",
                       "no removable text found"):
            self.assertIn(phrase, self.core)

    def test_the_scene_does_not_report_a_verdict_nobody_makes(self):
        self.assertNotIn('_blur_info.get("smeared")', self.core)


if __name__ == "__main__":
    unittest.main()
