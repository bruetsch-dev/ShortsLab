"""No rule may put text on a background it cannot be read against.

This exists because I broke it twice in one commit, the same way both times. The editor's two
primary commands - RENDER and an unsaved SAVE - were light plates with dark ink. When the accent
was unified, their background became the dark primary and the ink stayed where it was:

    #101319 on #22307f   1.59:1   the Render button
    #171b24 on #22307f   1.47:1   Save, while there are unsaved changes

WCAG asks 4.5:1 for normal text. Those two were the most important buttons on the screen, and the
mistake is invisible in a diff - one hex changed, the other did not, and both still look like a
deliberate pair. Reading a screenshot is the only way to catch it by eye, and nobody screenshots
a hover state.

The check is deliberately narrow: it only reads pairs where a single rule sets BOTH a literal hex
background and a literal hex color, so it never has to guess what a variable resolves to. That is
enough to have caught all four of the pairs that were wrong.
"""

import glob
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

HEX = r"#[0-9a-fA-F]{6}\b|#[0-9a-fA-F]{3}\b"
RULE = re.compile(r"([^{}]+)\{([^{}]*)\}")
BG = re.compile(r"(?<!-)background(?:-color)?\s*:\s*(" + HEX + ")")
FG = re.compile(r"(?<![-a-z])color\s*:\s*(" + HEX + ")")

# AA for normal text. Large text may go to 3:1, but a rule does not say how big its text is, so
# the stricter number is the one that can be checked from the stylesheet alone.
MINIMUM = 4.5


def _luminance(value):
    digits = value.lstrip("#")
    if len(digits) == 3:
        digits = "".join(c * 2 for c in digits)
    channels = [int(digits[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    channels = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


def contrast(one, other):
    light, dark = sorted((_luminance(one), _luminance(other)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


class TextIsReadableOnItsBackground(unittest.TestCase):
    def test_the_ratio_maths_matches_the_published_examples(self):
        """A wrong checker passes everything, so the checker is checked first."""
        self.assertAlmostEqual(contrast("#ffffff", "#000000"), 21.0, places=2)
        self.assertAlmostEqual(contrast("#777777", "#ffffff"), 4.48, places=2)
        self.assertAlmostEqual(contrast("#fff", "#ffffff"), 1.0, places=6)

    def test_no_stylesheet_sets_text_it_cannot_read(self):
        offences = []
        for path in sorted(glob.glob(os.path.join(ROOT, "static", "*.css"))):
            source = open(path, encoding="utf-8").read()
            for rule in RULE.finditer(source):
                body = rule.group(2)
                back, fore = BG.search(body), FG.search(body)
                if not (back and fore):
                    continue
                ratio = contrast(fore.group(1), back.group(1))
                if ratio < MINIMUM:
                    line = source[:rule.start()].count("\n") + 1
                    selector = " ".join(rule.group(1).split())[-70:]
                    offences.append(
                        f"{os.path.basename(path)}:{line}  {ratio:.2f}:1  "
                        f"{fore.group(1)} on {back.group(1)}  {selector}")
        self.assertEqual([], offences, "text below %.1f:1 on its own background:\n  %s"
                         % (MINIMUM, "\n  ".join(offences)))


if __name__ == "__main__":
    unittest.main()
