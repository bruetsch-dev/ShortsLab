"""The editor's preview must be allowed to decode - a re-seek on every tick freezes the picture.

`syncPreviewVideo` keeps the <video> lined up with the timeline clock. It ran on the playback
tick with a 0.10s tolerance, which is inside the normal drift of a video playing at a non-1.0
rate, so it assigned `currentTime` about ten times a second. Every assignment CANCELS the seek
already in flight, so the decoder never delivered a frame: readyState sat at HAVE_METADATA while
the clock ran on, and the last decoded picture stood still on screen.

Measured in the editor on the dating edit, six seconds of playback:

    before   31 `seeking` / 5 `seeked` on the visible element, 20 / 6 on its twin
             3 of 15 sampled pictures identical to the one before
    after    3 / 3 and 8 / 7
             0, 1 and 0 of 15 across three runs

So: while a seek is in flight, nothing is corrected unless the target really jumped (a scene
change or a scrub), and ordinary drift is only corrected past a quarter of a second.
"""

import os
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app

SOURCE = Path(app.__file__).read_text(encoding="utf-8", errors="replace")
SYNC = SOURCE[SOURCE.index("function syncPreviewVideo("):]
SYNC = SYNC[:SYNC.index("function onPvMeta")]


class TheSeekIsNotRestartedWhileItRuns(unittest.TestCase):
    def test_a_seek_in_flight_blocks_a_new_one(self):
        self.assertIn("!pvid.seeking", SYNC)

    def test_only_a_real_jump_interrupts_a_running_seek(self):
        self.assertIn("(pvid.seeking && jump)", SYNC)
        self.assertIn("var jump=drift>0.5", SYNC)

    def test_ordinary_drift_is_wider_than_a_tick(self):
        """0.10s is inside the drift of a clip playing at a changed rate; 0.25s is not."""
        self.assertNotIn("wanted)>.10", SYNC)
        self.assertIn("drift>0.25", SYNC)

    def test_the_measurement_is_written_down_where_the_next_reader_will_be(self):
        self.assertIn("31 `seeking` events against 5", SYNC)


class TheOnScreenElementIsNeverReloaded(unittest.TestCase):
    """Setting .src on the element the viewer is looking at leaves the PREVIOUS clip's last frame
    standing there while the new one loads - a still of the wrong shot."""

    BLOCK = SOURCE[SOURCE.index("      // the buffer already holds this exact clip"):]
    BLOCK = BLOCK[:BLOCK.index("applyClipPreviewTransform(s);   // pvid may have swapped")]

    def test_the_new_clip_is_loaded_into_the_hidden_twin(self):
        self.assertIn("if(pvidB.getAttribute('src')!==s.clip){ try{ pvidB.src=s.clip; }catch(e){} }",
                      self.BLOCK)
        self.assertNotIn("pvid.src=s.clip", self.BLOCK)

    def test_the_scene_s_own_poster_covers_the_wait(self):
        self.assertIn("pimg.src=s.poster; pimg.style.display='block';", self.BLOCK)
        self.assertIn("pvBehind(pvid); pvBehind(pvidB);", self.BLOCK)

    def test_the_swap_waits_for_an_actual_frame(self):
        self.assertIn("pvidB.addEventListener('loadeddata', swap)", self.BLOCK)

    def test_the_pending_load_is_not_thrown_away_by_the_next_preload(self):
        """The tail of the function preloads the FOLLOWING clip into pvidB - which is the element
        now holding the load we are waiting for."""
        self.assertIn("return;", self.BLOCK)
        swap_at = self.BLOCK.index("pvidB.addEventListener('loadeddata', swap)")
        self.assertIn("preloadClipInto(pvidB, nextScene);", self.BLOCK[:swap_at])


if __name__ == "__main__":
    unittest.main()
