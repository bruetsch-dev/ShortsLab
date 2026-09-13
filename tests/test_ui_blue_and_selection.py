"""Blue everywhere, and a selected button you can actually see.

Two separate defects, both reported from the running app:

1. The UI was green. Not one palette but FOUR, layered: chat-shell.css (--accent), its own
   prototype skin (--p-green / #a7ff83 / rgba(167,255,131,...)), timeline-theme.css, and
   design-v2.css - which has TWO palette blocks, the second overriding the first. Changing one
   left the others painting green.

2. Selected and unselected buttons were pixel-identical. Measured in the browser: background
   distance 0, border distance 0. The cause was not a weak colour choice - it was
   `.design-v2 body:not(.page-timeline) button { background: ... !important }`, a generic skin
   that no amount of specificity can beat. After the fix: border distance 196, text distance 422.
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SHEETS = {name: (ROOT / "static" / name).read_text(encoding="utf-8")
          for name in ("chat-shell.css", "design-v2.css", "timeline-theme.css")}

# The greens that were in use, as hex and as the rgba triples they were also written in.
GREEN_HEX = ("#39ff14", "#50ff31", "#b6ff69", "#a7ff83", "#1ea312", "#278f21",
             "#7dff5a", "#80ee67", "#8ff079", "#b8ff9b")
GREEN_RGBA = (r"57,\s*255,\s*20", r"167,\s*255,\s*131", r"36,\s*184,\s*26",
              r"128,\s*238,\s*103", r"184,\s*255,\s*155")


class PaletteTests(unittest.TestCase):
    def test_no_green_hex_remains_in_any_sheet(self):
        for name, css in SHEETS.items():
            for hexcode in GREEN_HEX:
                self.assertNotIn(hexcode, css.lower(), f"{name} still paints {hexcode}")

    def test_no_green_rgba_remains_either(self):
        """The literals were also written as rgba triples, which a hex search misses entirely -
        that is how the prototype skin survived the first pass."""
        for name, css in SHEETS.items():
            for pattern in GREEN_RGBA:
                self.assertIsNone(re.search(pattern, css), f"{name} still tints with {pattern}")

    def test_every_accent_token_is_blue(self):
        """Blue means the blue channel leads. Checked per token, not by eye."""
        for name, css in SHEETS.items():
            for match in re.finditer(r"--(?:v2-)?(?:p-green|accent)\s*:\s*#([0-9a-fA-F]{6})", css):
                r, g, b = (int(match.group(1)[i:i+2], 16) for i in (0, 2, 4))
                self.assertGreater(b, g, f"{name}: #{match.group(1)} is not blue")
                self.assertGreater(b, r, f"{name}: #{match.group(1)} is not blue")

    def test_the_second_v2_palette_block_was_switched_too(self):
        """design-v2.css declares .design-v2 twice; the later block wins and was still green."""
        blocks = re.findall(r"--v2-bg:\s*(#[0-9a-fA-F]{6})", SHEETS["design-v2.css"])
        self.assertGreaterEqual(len(blocks), 2, "expected both palette blocks")
        for value in blocks:
            r, g, b = (int(value[1:][i:i+2], 16) for i in (0, 2, 4))
            self.assertGreaterEqual(b, g, f"{value} still has a green cast")


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.v2 = SHEETS["design-v2.css"]

    def test_the_generic_button_skin_is_still_important(self):
        """If this ever stops being !important the selected rules can drop theirs - and if it
        changes shape, this test says so before the selection silently disappears again."""
        self.assertIn("background: var(--v2-panel-2) !important;", self.v2)

    def test_selected_matches_that_important(self):
        block = self.v2[self.v2.index("button.choice.sel,"):]
        self.assertIn("border-color: var(--v2-accent) !important;", block[:900])
        self.assertIn("!important", block[:900])

    def test_selection_changes_more_than_one_thing(self):
        """A single faint tint was the original bug. Border, fill, text and glow together."""
        block = self.v2[self.v2.index("button.choice.sel,"):][:900]
        for prop in ("border-color:", "background:", "color:", "box-shadow:"):
            self.assertIn(prop, block)

    def test_the_other_selected_controls_are_covered(self):
        for cls in ("button.mode-card.sel", "button.hook-sentence.on",
                    "button.aicore-option.on", "#sb-nav button.active"):
            self.assertIn(cls, self.v2, f"{cls} would still look unselected")


if __name__ == "__main__":
    unittest.main()
