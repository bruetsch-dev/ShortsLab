"""The showroom is one machine on a fixed spot, and clicking it has to work.

Everything here was broken once, in the browser, and each rule below is the fix:

1. There is ONE machine, not six. Scrolling a mode changes the screen, the backdrop and the words;
   the desk and the computer never move (measured request: "der desk und der pc bleiben an stelle").
2. The line of machines does not wrap: it stops at the first and the last one.
3. A click on the computer opens the mode. Three separate faults swallowed that click:
      - the showroom captured the pointer on pointerdown, which retargets the click to the
        showroom and away from the machine,
      - the text drawn on the glass was hit-testable, so pressing and releasing landed on
        different elements while the tube scaled under the cursor - no click event at all,
      - `zooming` was a boolean cleared only in an animation's finish event; one missed finish
        left the whole showroom permanently dead.
4. The pointer only reacts on the computer itself. The computer is its own cropped file over the
   desk, so the hover box is the machine and not the whole picture.
5. Nothing readable travels with the zoom into the screen - blown up 15x, text arrives as half
   words ("da sieht man immer so halbe wörter").
"""

import os
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = Path(__file__).resolve().parent.parent
JS = (ROOT / "static" / "shell-v3.js").read_text(encoding="utf-8")
CSS = (ROOT / "static" / "shell-v3.css").read_text(encoding="utf-8")


class OneMachine(unittest.TestCase):
    def test_every_mode_shows_the_same_machine(self):
        self.assertIn('const MACHINE = {', JS)
        self.assertNotIn('"machines/" + m.id', JS)
        # the per-mode photographs are gone from the mode table
        self.assertEqual(len(re.findall(r"\n\s+screen: \[", JS)), 1)

    def test_the_assets_it_names_exist(self):
        for name in ("clip_desk.webp", "clip_pc.webp"):
            self.assertTrue((ROOT / "static" / "machines" / name).is_file(), name)

    def test_the_machine_stands_outside_the_scrolling_track(self):
        # built onto the stage, not into a slide, so the slides move past it
        self.assertIn("stage.appendChild(buildMachine());", JS)
        self.assertIn(".rig { position: absolute;", CSS)

    def test_the_scroll_runs_vertically(self):
        self.assertIn("translateY(${-S.slide * 100}%)", JS)
        self.assertIn("flex-direction: column", CSS)

    def test_the_desk_takes_the_jolt_of_every_scroll(self):
        self.assertIn('machineEl.classList.add("jolt")', JS)
        self.assertIn(".rig.jolt { animation: jolt", CSS)

    def test_only_the_picture_on_the_wall_drifts_not_the_light(self):
        self.assertIn('bg.style.setProperty("--bg-par"', JS)
        self.assertIn("transform: translateY(var(--bg-par, 0%))", CSS)


class TheLineHasEnds(unittest.TestCase):
    def test_it_does_not_wrap_around(self):
        self.assertIn("Math.max(0, Math.min(MODES.length - 1, i))", JS)
        self.assertNotIn("((i % n) + n) % n", JS)

    def test_the_arrow_that_leads_nowhere_is_dead(self):
        self.assertIn("up.disabled = S.slide === 0", JS)
        self.assertIn("down.disabled = S.slide === MODES.length - 1", JS)


class ClickingTheMachine(unittest.TestCase):
    def test_the_pointer_is_captured_only_once_it_really_is_a_drag(self):
        self.assertIn("if (dragMoved && !captured)", JS)
        # never on pointerdown - that is what stole the click
        self.assertNotIn("dragging = true; dragMoved = false; dragDist = 0; y0 = e.clientY; dy = 0; "
                         "t0 = performance.now(); h = room.clientHeight || 1;\n    room.setPointerCapture", JS)

    def test_a_click_is_judged_by_distance_not_by_a_stale_flag(self):
        self.assertIn("if (dragDist < 12) enterMachine(MODES[S.slide])", JS)

    def test_what_is_drawn_on_the_glass_cannot_swallow_the_click(self):
        self.assertIn(".glass > *, .glass .field, .glass .field * { pointer-events: none; }", CSS)

    def test_a_flight_expires_by_itself(self):
        """A boolean that misses one finish event kills the showroom for good."""
        self.assertIn("let zoomUntil = 0;", JS)
        self.assertIn("const flying = () => performance.now() < zoomUntil;", JS)
        self.assertNotIn("zooming = true", JS)

    def test_the_hover_box_is_the_computer_and_not_the_picture(self):
        self.assertIn("pcBox:", JS)
        self.assertIn(".rig .pc.lift { transform: scale(", CSS)
        # the desk keeps its size while the computer grows
        self.assertNotIn(".rig.lift .photo", CSS)


class TheFlight(unittest.TestCase):
    def test_the_camera_pushes_into_the_tube_and_aims_at_the_middle(self):
        """A small rectangle stretched over the window looked wrong; the room is scaled instead."""
        self.assertIn("only ever arrive as half words", JS)
        self.assertIn("stage.style.transformOrigin = `${cx}px ${cy}px`", JS)
        self.assertIn("const tx = vw / 2 - cx, ty = vh / 2 - cy;", JS)
        # the point flown at is the glass, and the glass is what ends up in the middle
        self.assertIn("const r = glass.getBoundingClientRect();", JS)
        self.assertNotIn("z.innerHTML = screenField(m);", JS)
        self.assertNotIn('el("div", "zoomer")', JS)

    def test_closing_from_the_brand_plays_the_system_page_again(self):
        self.assertIn("goShowroom(true)", JS)
        self.assertIn("splash(land, { force: true, flash:", JS)

    def test_the_station_name_is_the_loud_thing_on_the_screen(self):
        self.assertIn("<u>' + esc(m.name.toUpperCase()) + '</u>", JS)
        self.assertIn("background: var(--phos); color: var(--blue-deep);", CSS)


if __name__ == "__main__":
    unittest.main()
