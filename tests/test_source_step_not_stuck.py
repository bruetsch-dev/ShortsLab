"""The Clip Short source step must finish rendering and offer Continue.

Reported with a screenshot 2026-09-02: the step drew its note and the "CLIP ENGINE" caption and
then simply stopped - no discovery selector, no Continue button, no way forward.

Two faults in one call:

1. `selectField` reads `o.label` / `o.value`, and this card passed `["scrapedo", "Scrape.do
   rendered search"]` PAIRS. Every option came out as `value="undefined"`, so the step also
   stored "undefined" as the chosen engine.
2. `selectField` calls its handler once while the card is still being BUILT, to sync the initial
   value - and this handler called `renderAll()`. So the card re-entered the renderer from inside
   itself and everything after that line was never appended, including the Continue button.
"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SHELL = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "static", "chat-shell.js")


def shell():
    with open(SHELL, encoding="utf-8") as handle:
        return handle.read()


def source_card():
    text = shell()
    start = text.index("function renderSourceCard")
    return text[start:text.index("\nfunction ", start + 10)]


class TheSourceCardFinishes(unittest.TestCase):

    def test_no_handler_re_renders_while_the_card_is_being_built(self):
        """`renderAll()` may only run on a real user change, never on the initial sync."""
        for match in re.finditer(r"renderAll\(\)", source_card()):
            around = source_card()[max(0, match.start() - 120):match.start()]
            self.assertIn("if (!initial)", around,
                          "a source-card handler still re-renders unconditionally")

    def test_the_card_still_ends_with_a_continue_button(self):
        card = source_card()
        self.assertIn("card-foot", card)
        self.assertIn("completeStep(\"source\", \"reasoning\")", card)


class SelectFieldAcceptsBothOptionShapes(unittest.TestCase):

    def test_a_pair_is_read_as_value_and_label(self):
        text = shell()
        self.assertIn("const pair = Array.isArray(o);", text)
        self.assertIn("new Option(pair ? o[1] : o.label, pair ? o[0] : o.value)", text)

    def test_the_handler_is_told_whether_this_is_the_initial_sync(self):
        text = shell()
        self.assertIn("onChange(s.value, true);", text)
        self.assertIn("onChange(s.value, false);", text)

    def test_the_clip_short_selects_now_pass_objects(self):
        card = source_card()
        self.assertIn('{ value: "scrapedo", label: "Scrape.do rendered search" }', card)
        self.assertNotIn('["scrapedo", "Scrape.do rendered search"]', card)


class TheCopyMatchesTheDefault(unittest.TestCase):

    def test_the_note_no_longer_claims_bright_only(self):
        """Scrape.do is the discovery provider; the note shown to the user said Bright-only."""
        self.assertNotIn('"Clip Short uses Bright-only V4', shell())
        self.assertIn('"Clip Short uses V4:', shell())


if __name__ == "__main__":
    unittest.main()
