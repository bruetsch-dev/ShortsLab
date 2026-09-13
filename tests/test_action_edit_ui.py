"""Guards for the Action Edit pre-cut timeline.

These read the shipped JavaScript rather than driving a browser, so they are structural, not
behavioural: they cannot prove the timeline works, only that the pieces the working version
depends on are still there. Each one stands for a bug that actually happened.
"""

import re
import unittest
from pathlib import Path

SHELL = Path(__file__).resolve().parent.parent / "static" / "chat-shell.js"
CSS = Path(__file__).resolve().parent.parent / "static" / "chat-shell.css"


class ActionEditTimelineTests(unittest.TestCase):
    def setUp(self):
        self.js = SHELL.read_text(encoding="utf-8")
        self.editor = self.js[self.js.index("function actionTrimEditor"):
                              self.js.index("function renderActionEditFlow")]

    def test_a_clip_can_be_taken_back_off(self):
        """The drop zone only ever replaced a clip, so one added by mistake could not be removed
        without reloading and starting the whole set again."""
        self.assertIn("action-clip-remove", self.js)
        self.assertIn("ACTION_FILES[i] = null", self.js)
        self.assertIn("ACTION_TRIMS[i] = null", self.js)

    def test_removing_a_clip_releases_the_video_before_revoking_its_url(self):
        """A <video> still reading from the blob keeps the whole file alive for the session."""
        # Sliced forward from the remove handler. "const take = (f)" is not usable as an end
        # anchor: the AI Core flow defines one first, so index() walked backwards and matched
        # an empty slice that passed nothing.
        start = self.js.index('el("button", "action-clip-remove")')
        block = self.js[start:self.js.index("const take = (f)", start)]
        self.assertLess(block.index("removeAttribute(\"src\")"), block.index("revokeObjectURL"),
                        "the URL is revoked while the video still points at it")

    def test_play_starts_at_the_first_kept_frame(self):
        """Play used to resume wherever the file happened to be, which meant previewing footage
        that the trim had already removed."""
        self.assertIn("function playFromStart", self.editor)
        self.assertRegex(self.editor, r"playFromStart[\s\S]{0,200}seek\(state\.ranges\[0\]\[0\]\)")

    def test_playback_skips_the_removed_stretches(self):
        self.assertIn("const next = state.ranges.find(r => r[0] > at)", self.editor)
        self.assertIn("else { video.pause(); seek(state.ranges[0][0]); }", self.editor)

    def test_the_ranges_are_draggable(self):
        self.assertIn("action-handle", self.editor)
        self.assertIn("action-handle", CSS.read_text(encoding="utf-8"))
        self.assertIn("pointermove", self.editor)

    def test_normalise_keeps_the_range_objects_it_was_given(self):
        """It used to rebuild every range with .map, handing back new arrays. Harmless for a
        redraw, fatal for a drag: the handle was left editing a range the state no longer held."""
        body = self.editor[self.editor.index("function normalise"):
                           self.editor.index("function timeAt")]
        self.assertNotIn(".map(r =>", body, "normalise is replacing range objects again")
        self.assertIn("state.ranges.forEach(r =>", body)

    def test_arrow_keys_step_a_single_frame(self):
        """A fixed 0.1 s step was three frames, so a cut could not be landed exactly."""
        self.assertIn("const FRAME = 1 / 30", self.editor)
        self.assertIn("ev.shiftKey ? .5 : FRAME", self.editor)


if __name__ == "__main__":
    unittest.main()
