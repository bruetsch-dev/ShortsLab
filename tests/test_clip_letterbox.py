"""Baked-in black bars have to come off before a clip is fitted to the Short.

A very common upload is a landscape video placed inside a 9:16 canvas with black above and below.
The clip is already 1080x1920, so the normaliser's scale-to-fill was a no-op on it and the bars
travelled all the way into the finished Short - a delivered test short opened on three shots with
roughly a third of the frame black.
"""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import clip_scraper as cs
import pipeline

FF = str(pipeline.find_ffmpeg() or "")


class LetterboxTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not FF:
            raise unittest.SkipTest("ffmpeg is required")
        cls.tmp = Path(tempfile.mkdtemp(prefix="letterbox_"))
        cls.probe = cs._ffmpeg_tools()[1]
        # 16:9 content padded into a 9:16 canvas - the exact shape that produced the bars.
        cls.boxed = cls.tmp / "boxed.mp4"
        subprocess.run([FF, "-y", "-f", "lavfi", "-i", "testsrc2=s=1080x608:d=4:r=30",
                        "-vf", "pad=1080:1920:0:656:color=black", "-c:v", "libx264",
                        "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-an", str(cls.boxed)],
                       capture_output=True, timeout=300)
        cls.plain = cls.tmp / "plain.mp4"
        subprocess.run([FF, "-y", "-f", "lavfi", "-i", "testsrc2=s=1080x1920:d=3:r=30",
                        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                        "-an", str(cls.plain)], capture_output=True, timeout=300)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _band_brightness(self, video):
        import numpy as np
        res = subprocess.run([FF, "-v", "error", "-ss", "1", "-i", str(video), "-frames:v", "1",
                              "-vf", "scale=108:192,format=gray", "-f", "rawvideo",
                              "-pix_fmt", "gray", "-"], capture_output=True, timeout=120)
        frame = np.frombuffer(res.stdout[:108 * 192], dtype="uint8").reshape(192, 108)
        frame = frame.astype("float32")
        return float(frame[:40].mean()), float(frame[-40:].mean())

    def test_the_bars_are_found(self):
        self.assertEqual(cs.detect_letterbox(self.boxed, FF), "crop=1080:608:0:656")

    def test_a_normal_vertical_clip_is_left_alone(self):
        """Cropping ordinary footage because it happens to be dark would be far worse than the
        bars: the subject would be cut off."""
        self.assertEqual(cs.detect_letterbox(self.plain, FF), "")

    def test_the_normalised_clip_fills_the_frame(self):
        out = self.tmp / "fixed.mp4"
        cs.normalize_clip(self.boxed, out, FF, seconds=3.0)
        self.assertTrue(out.is_file())
        self.assertEqual(cs._probe_dims(out, self.probe), (cs.TARGET_W, cs.TARGET_H))
        before_top, before_bottom = self._band_brightness(self.boxed)
        after_top, after_bottom = self._band_brightness(out)
        self.assertLess(before_top, 8, "the fixture is not actually letterboxed")
        self.assertLess(before_bottom, 8)
        self.assertGreater(after_top, 40, "the top band is still black after normalising")
        self.assertGreater(after_bottom, 40, "the bottom band is still black after normalising")



class CaptionCoverageTests(unittest.TestCase):
    """Whether a caption can be cleaned off is a measurement, not a judgement call. Asking a
    vision model produced captions on solid plates and full-height vertical text in delivered
    Shorts; the remover's own mask does not guess."""

    @classmethod
    def setUpClass(cls):
        if not FF:
            raise unittest.SkipTest("ffmpeg is required")
        import caption_remover
        if caption_remover.cv2 is None:
            raise unittest.SkipTest("OpenCV is required")
        cls.tmp = Path(tempfile.mkdtemp(prefix="capcov_"))
        font = __import__("pipeline").FONT_BOLD
        if not font:
            raise unittest.SkipTest("no bundled font to draw a caption with")
        escaped = str(font).replace("\\", "/").replace(":", "\:")
        # A TEXT SLIDE - the thing MAX_COVERAGE exists to refuse. Two lines used to stand in for
        # this and measured 10.2%, which after the 2026-09-02 re-measurement is an ordinary large
        # caption: real clips at 4.6% and 10.8% inpaint perfectly. A slide has to actually fill
        # the frame with text, so draw six lines down it.
        cls.heavy = cls.tmp / "heavy.mp4"
        lines = ",".join(
            f"drawtext=fontfile='{escaped}':text='BURNED IN LINE {index}':"
            "fontcolor=white:fontsize=64:borderw=5:bordercolor=black:"
            f"x=(w-text_w)/2:y=h*{0.18 + index * 0.11:.2f}"
            for index in range(6))
        subprocess.run([FF, "-y", "-f", "lavfi", "-i", "testsrc2=s=540x960:d=3:r=30",
                        "-vf", lines,
                        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                        "-an", str(cls.heavy)], capture_output=True, timeout=300)
        cls.clean = cls.tmp / "clean.mp4"
        subprocess.run([FF, "-y", "-f", "lavfi", "-i", "testsrc2=s=540x960:d=3:r=30",
                        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                        "-an", str(cls.clean)], capture_output=True, timeout=300)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_clean_footage_measures_near_zero(self):
        import caption_remover
        share = caption_remover.caption_coverage(self.clean, FF, seconds=2.0)
        self.assertGreaterEqual(share, 0.0, "the measurement failed outright")
        self.assertLess(share, caption_remover.MAX_COVERAGE)

    def test_two_heavy_lines_measure_above_what_can_be_cleaned(self):
        import caption_remover
        share = caption_remover.caption_coverage(self.heavy, FF, seconds=2.0)
        self.assertGreater(share, caption_remover.MAX_COVERAGE,
                           f"heavy burned-in text measured only {share:.1%}")

    def test_an_unreadable_file_is_not_treated_as_a_problem(self):
        """A measurement that could not run is not evidence against a clip."""
        import caption_remover
        self.assertEqual(caption_remover.caption_coverage(self.tmp / "missing.mp4", FF), -1.0)


class BlackFrameWindowTests(unittest.TestCase):
    """A delivered Short went fully black for half a second. The clip began with a fade up from
    black and the chosen window started at 0.0; nothing checked a scraped window for pixels, so
    it travelled straight into the edit.

    The check also first shipped broken: subprocess was not imported in that module and the
    resulting NameError was swallowed by a bare `except Exception`, so it silently did nothing.
    """

    @classmethod
    def setUpClass(cls):
        if not FF:
            raise unittest.SkipTest("ffmpeg is required")
        cls.tmp = Path(tempfile.mkdtemp(prefix="blackwin_"))
        # A clip that fades up from black, exactly like the one that shipped.
        cls.fade = cls.tmp / "fade.mp4"
        subprocess.run([FF, "-y", "-f", "lavfi", "-i", "testsrc2=s=360x640:d=4:r=30",
                        "-vf", "fade=t=in:st=0:d=1.0", "-c:v", "libx264", "-preset", "ultrafast",
                        "-pix_fmt", "yuv420p", "-an", str(cls.fade)],
                       capture_output=True, timeout=300)
        cls.clean = cls.tmp / "clean.mp4"
        subprocess.run([FF, "-y", "-f", "lavfi", "-i", "testsrc2=s=360x640:d=4:r=30",
                        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                        "-an", str(cls.clean)], capture_output=True, timeout=300)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_a_window_that_opens_on_black_is_refused(self):
        import scrape_v3
        scrape_v3._WINDOW_TECH_CACHE.clear()
        self.assertEqual(scrape_v3.technical_window_issue(self.fade, 0.0, 3.0), "black_frames")

    def test_clean_footage_passes(self):
        import scrape_v3
        scrape_v3._WINDOW_TECH_CACHE.clear()
        self.assertEqual(scrape_v3.technical_window_issue(self.clean, 0.0, 3.0), "")

    def test_the_module_can_actually_run_the_probe(self):
        """The guard must not be able to hide a missing import again."""
        import scrape_v3
        self.assertTrue(hasattr(scrape_v3, "subprocess"),
                        "scrape_v3 cannot run its own black-frame probe")
        source = Path(scrape_v3.__file__).read_text(encoding="utf-8")
        block = source[source.index("blackdetect=d=0.12"):]
        block = block[:block.index("_WINDOW_TECH_CACHE[key] = issue")]
        # Comments are stripped first: the block explains WHY it avoids a bare except, and
        # matching that sentence made the test fail on correct code.
        code = "\n".join(line for line in block.splitlines()
                         if not line.lstrip().startswith("#"))
        self.assertNotIn("except Exception", code,
                         "a bare except here swallows programming errors as it did before")

if __name__ == "__main__":
    unittest.main()
