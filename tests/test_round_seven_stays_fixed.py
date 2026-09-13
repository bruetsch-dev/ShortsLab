"""Round seven's findings, pinned. The first one is a mistake I made and did not measure.

Three rounds went into unifying the product on one blue, and the blue was never checked against
what it sits on:

    the primary plate on the dock   #22307f on #151821   1.52:1
    the primary plate on the page   #22307f on #111313   1.60:1
    a SELECTED tile, filled white                        16.54:1

So the loudest object on every screen was a passive state - which scene you had already picked -
and the next action was a plate you could barely find. The lesson generalises, and that is what
this file is: the contrast checks are computed here rather than trusted to a comment.
"""

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_text_is_readable_on_its_background import contrast

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PAGE = "#111313"          # the shell's ground
DOCK = "#151821"          # the bar the primary action sits on
CARD = "#151821"          # a Library card

# 3:1 is the floor WCAG sets for a UI component against what is behind it; the plate is a
# component, and its text is text, so both numbers have to hold at once. They pull in opposite
# directions - a lighter plate weakens white on it - which is the whole difficulty.
COMPONENT_FLOOR = 3.0
TEXT_FLOOR = 4.5


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


def token(name, css=None):
    css = css if css is not None else read("static/shell-v3.css")
    found = re.findall(r"--%s\s*:\s*([^;]+);" % re.escape(name), css)
    return found[-1].strip() if found else None


class TheAccentCanActuallyBeSeen(unittest.TestCase):
    def test_the_primary_plate_clears_the_component_floor_on_both_surfaces(self):
        plate = token("studio-accent-solid")
        self.assertIsNotNone(plate, "--studio-accent-solid is gone")
        for surface, what in ((DOCK, "the dock"), (PAGE, "the page")):
            ratio = contrast(plate, surface)
            self.assertGreaterEqual(round(ratio, 2), COMPONENT_FLOOR,
                                    f"the primary plate {plate} measures {ratio:.2f}:1 against "
                                    f"{what} - below the {COMPONENT_FLOOR}:1 floor")

    def test_and_its_own_text_still_clears_the_text_floor(self):
        plate, ink = token("studio-accent-solid"), token("studio-accent-ink")
        ratio = contrast(ink, plate)
        self.assertGreaterEqual(round(ratio, 2), TEXT_FLOOR,
                                f"{ink} on {plate} is {ratio:.2f}:1 - the plate went too light")

    def test_a_selection_no_longer_outranks_an_action(self):
        """The white slab was 16.54:1 while the button that starts a render was 1.52:1."""
        css = read("static/shell-v3.css")
        self.assertIn(".phys-tile.on { background: var(--blue-raise); color: var(--phos); "
                      "border-color: var(--studio-accent-solid); }", css)
        self.assertNotIn(".phys-tile.on { background: var(--phos);", css)
        raise_ = token("blue-raise")
        self.assertLess(contrast(raise_, PAGE), contrast(token("studio-accent-solid"), PAGE),
                        "the selected state is louder than the primary action again")


class TheContainersAreVisible(unittest.TestCase):
    """.panel/.grp measured 1.07:1 fill and 1.58:1 border against the page - five of them on the
    Sketch Station, all optically absent, which is why its form read as loose floating labels.
    """

    def setUp(self):
        css = read("static/shell-v3.css")
        rule = re.search(r"\.panel,\.grp,\.recent \{background:(#[0-9a-f]{6});"
                         r"border:1px solid (#[0-9a-f]{6});", css, re.I)
        self.assertIsNotNone(rule, "the panel rule moved")
        self.fill, self.border = rule.group(1), rule.group(2)

    def test_the_fill_reads_as_a_surface(self):
        self.assertGreater(contrast(self.fill, PAGE), 1.10)

    def test_the_border_reads_as_an_edge(self):
        self.assertGreater(contrast(self.border, PAGE), 2.0)

    def test_the_olive_ramp_is_gone(self):
        css = read("static/shell-v3.css")
        for stray in ("#323a2f", "#aab2a7", "#333b2d", "#30372f", "#191d18", "#c3cbbb"):
            self.assertNotIn(stray, css, f"{stray}: the second, olive-cast neutral ramp is back")


class TheTimelineIsOperableWithoutAMouse(unittest.TestCase):
    """Twelve clips, eleven sound markers and two overlay chips, all plain divs with no tabindex,
    no role and no name, on a page with three hundred focusable elements.
    """

    def setUp(self):
        src = read("app.py")
        self.arm = src[src.index("function armTimelineKeyboard(){"):]
        self.arm = self.arm[:self.arm.index("\n  }\n") + 4]
        self.src = src

    def test_each_track_is_one_stop_and_the_arrows_move_inside_it(self):
        self.assertIn("track.setAttribute('role','toolbar')", self.arm)
        self.assertIn("ev.key==='ArrowRight'", self.arm)
        self.assertIn("ev.key==='ArrowLeft'", self.arm)
        self.assertIn("ev.key==='Home'", self.arm)
        self.assertIn("ev.key==='End'", self.arm)

    def test_every_object_is_named(self):
        self.assertIn("el.setAttribute('aria-label', spec[2](el,i))", self.arm)
        self.assertIn("el.setAttribute('role','button')", self.arm)

    def test_enter_calls_the_selector_the_pointer_calls(self):
        """el.click() never reaches a handler bound to pointerdown."""
        # the comment above that line SAYS "el.click()", so read the code without the comments
        code = "\n".join(l for l in self.arm.splitlines() if not l.strip().startswith("//"))
        self.assertNotIn("el.click()", code)
        self.assertIn("spec[3](el, at)", code)
        for real in ("selectClip(s.id, false)", "selectFx(el.dataset.fxId)",
                     "selectOverlay(el.dataset.ovId, el.dataset.ovScene)"):
            self.assertIn(real, self.src, f"the keyboard path stopped calling {real}")

    def test_the_position_survives_the_rebuild_that_selecting_causes(self):
        self.assertIn("kbdWhere", self.arm)
        self.assertIn("var back=document.querySelectorAll(spec[0]+' '+spec[1])[at]", self.arm)

    def test_and_the_mouse_and_the_keyboard_share_one_position(self):
        """A click focuses nothing here - the pointer handler calls preventDefault."""
        self.assertIn("var current=items.findIndex(function(el){ return "
                      "el.classList.contains('selected'); });", self.arm)
        self.assertIn("if(current<0 && kbdWhere && kbdWhere.track===spec[0])", self.arm)

    def test_a_sound_marker_can_be_hit(self):
        """And the pad is an ELEMENT, because both pseudo-elements are already spoken for.

        This test used to assert the presence of `.tl-fx::after { width:32px; height:32px }` in
        timeline-theme.css. That rule was there and computed to 0x0 for a whole round, because
        `#tl-sfx .tl-fx::after` is the marker's pointed tip - an id selector, unlayered, which
        outranks anything in a layer. Asserting a declaration is not asserting an effect; the
        structural fact a source test CAN hold is that the pad is not a pseudo-element.
        """
        src = read("app.py")
        self.assertIn(".tl-fx-pad { position:absolute;", src)
        self.assertIn("width:32px; height:32px;", src)
        self.assertIn("className:'tl-fx-pad'", src)
        css = read("static/timeline-theme.css")
        self.assertNotIn("body.page-timeline #timeline-root .tl-fx::after {", css,
                         "the pad is back on a pseudo-element the pointed tip already owns")

    def test_the_transition_button_does_not_shrink_its_own_target(self):
        """A transform scales an element's pseudo-elements with it."""
        src = read("app.py")
        self.assertIn(".tl-trans-add::after", src)
        self.assertNotIn("transform:scale(.55)", src)


class TheEditorSaysWhereYouAreWithoutSayingItBroke(unittest.TestCase):
    def test_the_playhead_is_not_the_failure_colour(self):
        css = read("static/timeline-theme.css")
        rule = re.search(r"\.tl-playhead[^{]*\{ background: (#[0-9a-f]{6})", css, re.I)
        self.assertIsNotNone(rule)
        self.assertNotEqual("#ff8a7a", rule.group(1).lower(),
                            "the playhead is painted in --fail again")

    def test_a_long_clip_cannot_be_read_as_a_short_one(self):
        """"2:04" sat between two 3-second clips in the same column."""
        src = read("app.py")
        self.assertIn("Math.floor(it.duration/60)+'m '+Math.round(it.duration%60)+'s'", src)


class TheLibraryDoesNotContradictItself(unittest.TestCase):
    def test_a_failed_project_does_not_call_itself_work_in_progress(self):
        shell = read("static/shell-v3.js")
        self.assertIn('const caption = p.failed ? "No video was produced"', shell)
        self.assertIn('p.running ? "Working on it"', shell)


class EveryStationLinkIsAnAddress(unittest.TestCase):
    """"#machine=<id>" is read by the boot only as a carousel POSITION, so all five cards were
    dead when opened cold, reloaded, bookmarked or middle-clicked.
    """

    def test_the_card_links_to_a_route_or_to_a_screen_the_boot_opens(self):
        shell = read("static/shell-v3.js")
        self.assertIn('a.href = route !== "/" ? route : "/#screen=" + m.id;', shell)
        self.assertNotIn('a.href = "#machine=" + m.id;', shell)


if __name__ == "__main__":
    unittest.main()
