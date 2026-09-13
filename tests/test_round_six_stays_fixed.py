"""The whole-product review's round-six findings, pinned so they cannot come back quietly.

Each of these was measured in a browser before it was changed, and every one of them is the kind
of defect that looks deliberate in a diff: a hex that did not move when its neighbour did, a
mode missing from a list, a title set once and never again. The numbers in the docstrings are
what was actually measured at 1440x900, not estimates.
"""

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


class TheEditorShowsItsWholeTimeline(unittest.TestCase):
    """.tl-stage had clientHeight 172 for 227px of content, overflow-y:hidden, page not scrolling.

    The music track sat at y894 in a 900px window - six of its twenty-eight pixels on screen and
    no way to reach the rest. The cause was a panel-wide `overflow:hidden` written to clip a
    decorative 1px hairline, out-specifying `.tl-stage { overflow:auto }`.
    """

    def setUp(self):
        self.src = read("app.py")

    def test_the_stage_may_scroll_whatever_the_panel_rule_says(self):
        self.assertIn("body.page-timeline #timeline-root .panel.tl-stage { overflow:auto; }", self.src)

    def test_the_tracks_row_has_a_floor_that_holds_them(self):
        row = re.search(r"grid-template-rows: auto minmax\(0, 1\.9fr\) minmax\((\d+)px", self.src)
        self.assertIsNotNone(row, "the editor's grid rows moved")
        self.assertGreaterEqual(int(row.group(1)), 200,
                                "the tracks row is back under the height four tracks need")


class AButtonThatCannotRunSaysSo(unittest.TestCase):
    """Every dockWith caller passes the reason a run cannot start, and "" when it can.

    The button stayed full-strength blue and pointer-cursored regardless - disabled false,
    aria-disabled absent - with the reason stranded in a separate span at the other end of the
    dock, related to the control by nothing at all.
    """

    def setUp(self):
        self.shell = read("static/shell-v3.js")
        self.dock = self.shell[self.shell.index("function dockWith("):]
        self.dock = self.dock[:self.dock.index("\nfunction ")]

    def test_the_note_disables_the_button(self):
        self.assertIn("const blocked = !!note;", self.dock)
        self.assertIn("go.disabled = blocked;", self.dock)

    def test_and_tells_a_screen_reader_why(self):
        self.assertIn('go.setAttribute("aria-disabled", blocked ? "true" : "false")', self.dock)
        self.assertIn('go.setAttribute("aria-describedby", n.id)', self.dock)

    def test_and_the_reason_does_not_outlive_the_block(self):
        self.assertIn('go.removeAttribute("aria-describedby")', self.dock)
        self.assertIn('go.removeAttribute("title")', self.dock)

    def test_a_blocked_primary_is_not_painted_as_an_armed_one(self):
        css = read("static/shell-v3.css")
        self.assertIn('.btn.primary:disabled, .btn.primary[aria-disabled="true"]', css)


class TheWaitScreenStopsClaimingWork(unittest.TestCase):
    """document.title was set once, at render, and never again.

    A job that had finished, failed or expired an hour ago still said "Working..." on the tab
    strip - which is where a render is actually watched.
    """

    def setUp(self):
        self.src = read("app.py")

    def test_one_place_sets_the_state_and_it_reaches_the_tab(self):
        self.assertIn("function setState(text, kind) {{", self.src)
        self.assertIn('document.title = text + " " + String.fromCharCode(183) + " Shortslab";', self.src)

    def test_every_end_state_goes_through_it(self):
        for call in ('setState("Nothing to show", "gone")',
                     'setState("Finished", "done")',
                     'setState((j.status === "cancelled") ? "Cancelled" : "Failed",'):
            self.assertIn(call, self.src)

    def test_the_three_end_states_are_not_one_colour(self):
        """What this test is for is that they DIFFER, not which three values they are.

        It first pinned the exact hexes, including amber for "gone" - and then failed honestly
        when "gone" moved to graphite, because amber already means "in edit" on the shelf and a
        job that has simply expired is information rather than a warning. The rule the file
        exists to hold is that a finished, a failed and a vanished run do not read as the same
        neutral word, which is what they all were before any of this.
        """
        found = dict(re.findall(r'\.pg-title\[data-state="(\w+)"\] \{\{ color:(#[0-9a-f]{6});',
                                self.src, re.I))
        self.assertEqual({"done", "fail", "gone"}, set(found),
                         "an end state lost its own colour")
        self.assertEqual(3, len(set(found.values())), f"two end states share a colour: {found}")
        self.assertNotIn("#f1f1f4", found.values(), "an end state is back to the body colour")

    def test_the_dead_glyph_is_gone_rather_than_hidden(self):
        """CSS hid it while the script kept writing emoji into it."""
        self.assertNotIn("pg-ico", self.src)


class OneFocusRing(unittest.TestCase):
    """Two global rules in the shell sheet, a third in the window sheet, a fourth in design-v2,
    plus separate cream rules for inputs, range thumbs and toggles. Keyboard focus was blue at
    4px offset on the Library and cream at 2px in the editor - same app, same session.
    """

    def test_no_sheet_draws_the_ring_in_the_text_colour(self):
        for sheet in ("static/shell-v3.css", "static/shell-window.css"):
            css = read(sheet)
            for line in css.splitlines():
                if ":focus-visible" not in line and "slider-thumb" not in line:
                    continue
                if "outline:" not in line and "outline " not in line:
                    continue
                self.assertNotIn("var(--phos)", line, f"{sheet}: a cream ring is back")
                self.assertNotIn("var(--w-phos)", line, f"{sheet}: a cream ring is back")

    def test_the_offset_is_the_same_everywhere(self):
        v2 = read("static/design-v2.css")
        self.assertIn(".design-v2 :focus-visible { outline: 2px solid var(--v2-accent); outline-offset: 2px; }", v2)


class OneWordmarkOnOneColumn(unittest.TestCase):
    """19/750/-0.8px at x=36, 17/700/-0.51px at x=56, 15/600/+2.1px at x=36 - three treatments,
    and four different content gutters under them.
    """

    def test_the_gutter_is_one_expression_read_from_both_sheets(self):
        shell, window = read("static/shell-v3.css"), read("static/shell-window.css")
        self.assertIn("--shell-gutter: clamp(20px, 4vw, 56px);", shell)
        self.assertIn("--win-gutter: clamp(20px, 4vw, 56px);", window)
        for user in (".v3-top,body.is-screen .v3-top {padding:0 var(--shell-gutter)",
                     ".studio-home {max-width:1640px;margin:auto;padding:42px var(--shell-gutter)",
                     ".page {padding:34px var(--shell-gutter)"):
            self.assertIn(user, shell)
        self.assertIn("--start-gutter: var(--shell-gutter);", shell)

    def test_the_windows_wear_the_shell_wordmark(self):
        window = read("static/shell-window.css")
        self.assertIn("font-size:19px!important;font-weight:750!important;"
                      "letter-spacing:-.042em!important;", window)

    def test_and_so_does_the_wait_screen(self):
        src = read("app.py")
        self.assertIn("font-size:19px; font-weight:750;", src)
        self.assertIn("padding:0 clamp(20px, 4vw, 56px);", src)


class NothingOnTheEditorIsTooSmallOrTheWrongBlue(unittest.TestCase):
    # A chevron and a square bullet are drawn, not read, so they keep whatever size draws them
    # well. Everything that carries a word has a floor.
    GLYPH_RULES = ("tl-sfx-caret", "cbar-cap")
    # the lookbehind matters: without it "12.5px" reports a 5px offender
    SIZE = re.compile(r"font(?:-size)?\s*:\s*[^;]*?(?<![\d.])(\d+(?:\.\d+)?)px")

    def test_no_text_under_ten_pixels(self):
        """Eighteen declarations sat under 10px - nine of them on the editor alone."""
        offenders = []
        for number, line in enumerate(read("app.py").splitlines(), 1):
            if any(glyph in line for glyph in self.GLYPH_RULES):
                continue
            for size in self.SIZE.findall(line):
                if 0 < float(size) < 10:
                    offenders.append("app.py:%d  %spx" % (number, size))
        self.assertEqual([], offenders,
                         "type under the 10px floor is back:\n  " + "\n  ".join(offenders))

    def test_the_editor_has_no_seventh_blue(self):
        """A cyan of its own hue, on the voiceover-end marker and the audio band."""
        src = read("app.py")
        editor = src[src.index(".tl-voice-end {"):src.index(".tl-voice-end {") + 40000]
        self.assertNotIn("6fd3ff", editor)
        self.assertNotIn("111,211,255", editor)


if __name__ == "__main__":
    unittest.main()
