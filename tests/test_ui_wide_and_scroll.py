"""Two layout defects reported from the running app, both measured before and after.

1. Fullscreen looked wrong on every screen except the home launchpad. Measured at 2560x1400:
   the flow and assets screens capped their column and left it flush against the sidebar, with
   884px of dead canvas on the right. The cap that mattered was on `.canvas`, not `.chat` - the
   chat can never exceed the column it lives in, so widening `.chat` alone changed nothing.

2. The longform frame editor could not be scrolled: 2165px of content in an 832px window with
   `.chat-scroll` at overflow-y:hidden, so "Render video" sat at y=2106 and the sketch short
   could not be finished at all. After the fix the same button measures y=777.
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
V2 = (ROOT / "static" / "design-v2.css").read_text(encoding="utf-8")
SHELL = (ROOT / "static" / "chat-shell.css").read_text(encoding="utf-8")


class WideScreenTests(unittest.TestCase):
    def test_the_canvas_is_what_gets_widened(self):
        """Widening .chat alone is the fix that does nothing - .canvas is the real cap."""
        block = V2[V2.index("@media (min-width: 1600px)"):]
        self.assertIn(".app.proto-flow .canvas", block[:1200])
        self.assertIn("margin-left: auto", block[:1200])
        self.assertIn("margin-right: auto", block[:1200])

    def test_the_column_is_centred_not_stretched(self):
        """A 2100px line of prose is unreadable; the home screen's uncap suits a tile grid only."""
        block = V2[V2.index("@media (min-width: 1600px)"):]
        # Read the PROSE columns only. The assets grid is deliberately wider - it holds tiles,
        # and lumping it in here is what made the first version of this test fail.
        prose = re.findall(r"\.app\.proto-(?:flow|job)[^{]*\{[^}]*max-width:\s*(?:min\()?(\d+)px",
                           block[:2400])
        self.assertTrue(prose, "no cap at all means stretched-to-edge text")
        self.assertTrue(all(int(c) <= 1800 for c in prose), prose)

    def test_tile_grids_are_allowed_more_width_than_prose(self):
        block = V2[V2.index("@media (min-width: 2100px)"):]
        self.assertIn(".app.proto-assets .canvas", block[:900])
        self.assertIn("2280px", block[:900])

    def test_narrow_windows_are_untouched(self):
        """Everything is inside a min-width query, so a 1440px window keeps its old layout."""
        for marker in ("@media (min-width: 1600px)", "@media (min-width: 2100px)"):
            self.assertIn(marker, V2)


class ScrollTests(unittest.TestCase):
    def test_the_frame_editor_can_always_scroll(self):
        for screen in ("proto-home", "proto-job"):
            self.assertIn(f".app.{screen} .chat-scroll:has(.lf-frame-editor)", V2,
                          f"{screen} still traps the editor")

    def test_the_deliberate_lock_is_still_there_for_the_screens_own_layout(self):
        """The lock is not a bug - those layouts are designed to fit exactly. Only the panels
        that are taller than the window get the exception."""
        self.assertIn(".app.proto-home .chat-scroll { overflow: hidden !important; }", V2)

    def test_the_shell_carries_the_same_exception(self):
        self.assertIn(".prototype-ui .app.proto-job .chat-scroll:has(.lf-frame-editor)", SHELL)

    def test_the_existing_result_exception_was_the_precedent(self):
        self.assertIn(".chat-scroll:has(.result-wrap)", SHELL)


class IconTests(unittest.TestCase):
    def test_the_icon_is_no_longer_green(self):
        from PIL import Image
        im = Image.open(ROOT / "static" / "app_icon.png").convert("RGBA")
        px = [p for p in im.getdata() if p[3] > 40]
        green = sum(1 for r, g, b, _ in px if g > r + 25 and g > b + 25)
        blue = sum(1 for r, g, b, _ in px if b > r + 25 and b > g + 25)
        self.assertLess(green, len(px) * 0.01, "the flask is still green")
        self.assertGreater(blue, len(px) * 0.02, "nothing became blue")

    def test_the_original_is_kept(self):
        """A hue rotation is not reversible by eye; keep the source to redo it differently."""
        self.assertTrue((ROOT / "backups" / "icons_green" / "app_icon.png").is_file())


if __name__ == "__main__":
    unittest.main()
