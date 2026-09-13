"""A provider now and then returns a valid PNG that is simply black.

Byte size and canvas ratio both pass it, so every resume trusted it and the finished render was
refused at the very end for containing a black screen - with nothing left to regenerate from.
Judging it at the resume check makes the generator treat it as missing and draw it again.
"""

import shutil
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

import longform_video as lv


class BlankFrameTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="lf_blank_"))
        self.addCleanup(shutil.rmtree, self.dir, True)

    def png(self, name, colour, draw_on=False):
        path = self.dir / name
        image = Image.new("RGB", (1600, 900), colour)
        if draw_on:
            pen = ImageDraw.Draw(image)
            pen.line([(100, 100), (1500, 800)], fill="black", width=9)
            pen.ellipse([(600, 300), (900, 600)], outline="black", width=9)
        image.save(path)
        return path

    def test_a_black_frame_is_treated_as_missing(self):
        self.assertTrue(lv._image_is_blank(self.png("dead.png", "black")))
        self.assertFalse(lv._image_done(self.png("dead2.png", "black"), "16:9"))

    def test_a_drawing_on_white_paper_is_kept(self):
        self.assertFalse(lv._image_is_blank(self.png("art.png", "white", draw_on=True)))

    def test_a_dark_night_drawing_is_kept(self):
        """The whole point of the measured threshold: a night doodle is dark, not dead."""
        path = self.dir / "night.png"
        image = Image.new("RGB", (1600, 900), (28, 30, 44))
        pen = ImageDraw.Draw(image)
        pen.ellipse([(700, 200), (900, 400)], fill=(240, 240, 210))
        pen.line([(0, 700), (1600, 700)], fill=(200, 200, 200), width=12)
        image.save(path)
        self.assertFalse(lv._image_is_blank(path))

    def test_a_flat_colour_frame_is_not_called_blank(self):
        """Flat-colour heuristics have misjudged drawings in this pipeline before; this check
        judges darkness only."""
        for colour in ("green", "white", "#8899aa"):
            self.assertFalse(lv._image_is_blank(self.png(f"flat_{colour[-3:]}.png", colour)),
                             colour)

    def test_an_unreadable_file_is_not_declared_blank(self):
        """A read failure must not silently delete a frame that is fine."""
        bad = self.dir / "broken.png"
        bad.write_bytes(b"not a png at all")
        self.assertFalse(lv._image_is_blank(bad))


if __name__ == "__main__":
    unittest.main()
