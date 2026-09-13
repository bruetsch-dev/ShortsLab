"""The audio mixer must change what you are HEARING, not only what will be rendered.

Asked 2026-09-02: "und der audiomixer button im player funktioniert?"

The 🔊 button opens its popover correctly and the popover sliders are wired to the master ones,
so the control was not dead - but the handler only wrote the new level into the `volumes` model
and called `markDirty()`. Nothing pushed it onto the elements that were already playing, so
dragging Voice or Music during playback produced no audible change. And `volumes.sfx` was read
by nothing at all in the editor: the SFX slider affected the render and never the preview.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app


def editor_js():
    with open(app.__file__, encoding="utf-8") as handle:
        return handle.read()


class MixerAppliesLive(unittest.TestCase):

    def setUp(self):
        self.source = editor_js()

    def test_moving_a_slider_pushes_the_level_onto_the_playing_audio(self):
        start = self.source.index("function bindVol")
        body = self.source[start:start + 1600]
        self.assertIn("audioVolumes();", body)

    def test_moving_a_slider_also_reaches_the_clip_that_is_on_screen(self):
        start = self.source.index("function bindVol")
        body = self.source[start:start + 1600]
        self.assertIn("applyClipAudio(pvid, activeSceneInfo.scene)", body)

    def test_the_sfx_slider_is_a_master_gain_over_each_sound(self):
        self.assertIn("var sfxMaster = (volumes.sfx != null) ? +volumes.sfx : 1;", self.source)
        self.assertIn("(f.volume != null ? f.volume : 0.25) * sfxMaster", self.source)

    def test_the_popover_sliders_still_drive_the_masters(self):
        """They were already wired indirectly; that link must not be lost."""
        self.assertIn("popup.addEventListener('input',function(){ master.value=this.value;",
                      self.source)


if __name__ == "__main__":
    unittest.main()
