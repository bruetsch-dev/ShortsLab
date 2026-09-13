"""A library card leads with one action and loses none of the others.

The card used to print six chips of identical size, weight and colour in a 3x2 block - Timeline,
Load, Video, Files, Rename, Hide - so every project asked the reader to choose between six things
when five of them are not why anyone opens a shelf. It now leads with the single action the card
exists for, keeps at most one other visible, and files the rest in a menu.

The danger in that rearrangement is silent loss. The first cut of it spent both visible slots on
Watch and Timeline for any project that had a video AND an edit, and `Load` - opening the project
in the studio - then had no route anywhere on the screen. This file pins both halves: exactly one
promoted button per card, and every action still reachable.
"""

import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHELL = os.path.join(ROOT, "static", "shell-v3.js")


def library_card_source():
    src = open(SHELL, encoding="utf-8").read()
    start = src.index("function libraryCard(")
    end = src.index("\nfunction ", start + 10)
    return src[start:end]


class LibraryCardActions(unittest.TestCase):
    def setUp(self):
        self.card = library_card_source()

    def test_every_action_the_card_used_to_offer_is_still_reachable(self):
        for label in ("Timeline", "Files", "Rename", "Hide", "Unhide"):
            self.assertIn(f'"{label}"', self.card, f"{label} has no route on the card any more")
        # Watch replaced the chip that used to say "Video"; Open/Load depend on the project kind.
        self.assertIn('"Watch"', self.card)
        self.assertIn('p.longform ? "Open" : "Load"', self.card)

    def test_opening_the_project_survives_even_when_it_is_not_a_visible_slot(self):
        """The regression this file exists for: two slots taken, and Load fell off the card."""
        self.assertIn("shown.indexOf(openLabel) < 0", self.card,
                      "nothing puts Load into the menu when the visible slots are spent")
        self.assertIn("rest.push({ label: openLabel, run: openIt })", self.card)

    def test_exactly_one_button_is_promoted(self):
        """Promotion used to be :first-child, which caught whichever chip happened to be first."""
        self.assertEqual(1, len(re.findall(r'mkAction\(primary, "lib-go"\)', self.card)))
        self.assertNotIn('mkAction(second, "lib-go")', self.card)
        css = open(os.path.join(ROOT, "static", "shell-v3.css"), encoding="utf-8").read()
        self.assertNotIn(".lib-acts .btn:first-child", css,
                         "the blanket first-child promotion is back")
        self.assertIn(".lib-acts .lib-go", css)

    def test_the_poster_does_what_the_promoted_button_does(self):
        """It is the largest thing on the card, and it used to be inert."""
        self.assertIn('fig.addEventListener("click", primary.run)', self.card)
        self.assertIn('fig.setAttribute("tabindex", "0")', self.card)
        self.assertIn('fig.setAttribute("role", "button")', self.card)
        self.assertIn('e.key === "Enter"', self.card, "the poster is mouse-only")

    def test_the_menu_can_be_dismissed_the_two_ways_a_menu_is_dismissed(self):
        self.assertIn('document.addEventListener("click", onAway, true)', self.card)
        self.assertIn('e.key === "Escape"', self.card)
        self.assertIn('trigger.setAttribute("aria-expanded"', self.card)
        self.assertIn('sheet.setAttribute("role", "menu")', self.card)


if __name__ == "__main__":
    unittest.main()
