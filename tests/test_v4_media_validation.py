"""A corrupt download must be named as one, never reported as a geometry problem.

`_probe` collapses every failure to (0, 0, 0.0) and the gate's next line asks
`h < w or h < 800 or w < 400` - so a truncated file, an HTML error body saved as .mp4, or a
download with no video stream all came back as "not native vertical". That reading sent every
investigation the wrong way, and "not native vertical" is one of the most common rejections in
the V4 smoke reports (7x in v4_bright_school_lunch_smoke, 6x in v4_bright_station_smoke_v4).

Measured on real files (2026-09-02): truncated -> "incomplete download (40000 bytes)",
HTML body -> "server sent a page, not a video", random bytes -> "no MP4 container header",
the real clip -> ok, 720x1280, 10.04s.
"""

import os
import shutil
import tempfile
import unittest
from pathlib import Path

import clip_scraper
import scrape_v4 as v4


REAL_CLIP = Path("assets/hook_intros/google_search.mp4")


class ValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _, cls.ffprobe = clip_scraper._ffmpeg_tools()
        cls.dir = Path(tempfile.mkdtemp(prefix="v4_mediaval_"))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def write(self, name, data):
        path = self.dir / name
        path.write_bytes(data)
        return path

    def test_a_real_video_passes_with_its_true_geometry(self):
        if not REAL_CLIP.is_file():
            self.skipTest("sample clip missing")
        ok, reason, width, height, duration = v4.validate_media(REAL_CLIP, self.ffprobe)
        self.assertTrue(ok, reason)
        self.assertEqual((width, height), (720, 1280))
        self.assertGreater(duration, 1.0)

    def test_a_truncated_download_says_incomplete(self):
        if not REAL_CLIP.is_file():
            self.skipTest("sample clip missing")
        path = self.write("trunc.mp4", REAL_CLIP.read_bytes()[:40000])
        ok, reason, *_ = v4.validate_media(path, self.ffprobe)
        self.assertFalse(ok)
        self.assertIn("incomplete", reason)
        self.assertNotIn("vertical", reason)

    def test_an_html_error_body_is_not_a_video(self):
        path = self.write("html.mp4", b"<!doctype html><html>Forbidden</html>" * 3000)
        ok, reason, *_ = v4.validate_media(path, self.ffprobe)
        self.assertFalse(ok)
        self.assertIn("page", reason)
        self.assertNotIn("vertical", reason)

    def test_random_bytes_are_not_a_video(self):
        path = self.write("junk.mp4", os.urandom(200_000))
        ok, reason, *_ = v4.validate_media(path, self.ffprobe)
        self.assertFalse(ok)
        self.assertIn("container", reason)

    def test_an_empty_file_is_incomplete_not_invisible(self):
        path = self.write("empty.mp4", b"")
        ok, reason, *_ = v4.validate_media(path, self.ffprobe)
        self.assertFalse(ok)
        self.assertIn("incomplete", reason)

    def test_a_missing_file_is_reported(self):
        ok, reason, *_ = v4.validate_media(self.dir / "nope.mp4", self.ffprobe)
        self.assertFalse(ok)
        self.assertIn("no file", reason)

    def test_no_failure_is_ever_called_a_geometry_problem(self):
        """The whole point: these must not read as 'not native vertical'."""
        cases = [self.write("a.mp4", b""),
                 self.write("b.mp4", os.urandom(200_000)),
                 self.write("c.mp4", b"<html>x</html>" * 8000)]
        for path in cases:
            _ok, reason, *_ = v4.validate_media(path, self.ffprobe)
            self.assertNotIn("vertical", reason.lower(), f"{path.name}: {reason}")


class GateWiringTests(unittest.TestCase):
    def body(self):
        source = Path(v4.__file__).read_text(encoding="utf-8")
        start = source.index("if h < w or h < 800 or w < 400:")
        return source[max(0, start - 1200):start + 400]

    def test_the_gate_validates_before_it_measures_geometry(self):
        block = self.body()
        self.assertIn("validate_media(path, ffprobe)", block)
        self.assertLess(block.index("validate_media("), block.index("if h < w or h < 800"))

    def test_an_invalid_file_is_deleted_not_left_for_a_later_stage(self):
        """A corrupt clip on disk is a clip a later reuse path can pick up."""
        block = self.body()
        self.assertIn("unlink(missing_ok=True)", block)

    def test_the_geometry_rejection_now_reports_the_size(self):
        """When it IS a geometry problem, say which one."""
        block = self.body()
        self.assertIn("not native vertical ({w}x{h})", block)

    def test_an_unreadable_probe_is_caught_however_it_degrades(self):
        """Without ffprobe the OpenCV path returns -1, not 0, for a missing file - and
        `not -1` is False, so a truthiness test would have let it through."""
        width, height, _duration = v4._probe("definitely-not-a-file.mp4", None)
        self.assertLessEqual(width, 0)
        self.assertLessEqual(height, 0)
        source = Path(v4.__file__).read_text(encoding="utf-8")
        self.assertIn("if width <= 0 or height <= 0:", source)


if __name__ == "__main__":
    unittest.main()
