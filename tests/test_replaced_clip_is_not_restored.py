"""Replacing a scene's clip must not be undone by the caption-blur pre-pass.

`caption_blur_src` is written when a clip is cleaned: it remembers which source the `capblur_*`
copy was made from, so a later render can reuse the cached copy instead of running ProPainter
again. It survives in the scene dict - and it survived a change of the scene's clip, which is where
it turned into a bug: the pre-pass rebuilds the blur from `caption_blur_src` and then points the
scene at that file, so the render shows the clip that was just replaced.

Measured 2026-09-05 on the couples copy: scene 0's clip was set to a new hook window
(`v4_tiktok_7581458380976409863.mp4` at 14.667s), `caption_blur_src` still read `scraped_00.mp4`,
and two consecutive renders came back byte-identical with the old station shot in the opening.

So a `caption_blur_src` that no longer matches the scene's own clip is dropped before the pre-pass
reads it. A scene already playing a derived file (`capblur_`/`speed_`) keeps its reference - that
is the cache doing its job.
"""

import inspect
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent_core

BODY = inspect.getsource(agent_core.apply_timeline_edits_to_config)


class TheStaleReferenceIsDropped(unittest.TestCase):
    def test_the_pre_pass_compares_the_blur_source_with_the_current_clip(self):
        self.assertIn('blur_src = str(scene.get("caption_blur_src") or "")', BODY)
        self.assertIn('current = str(scene.get("clip") or "")', BODY)
        self.assertIn('blur_src != current', BODY)
        self.assertIn('scene.pop("caption_blur_src", None)', BODY)

    def test_a_derived_clip_keeps_its_reference(self):
        """capblur_/speed_ IS the derived file - dropping the reference there would re-run the
        expensive pass on every render."""
        self.assertIn('not current.startswith(("capblur_", "speed_"))', BODY)

    def test_the_drop_happens_before_the_base_is_chosen(self):
        drop = BODY.index('blur_src = ""')
        use = BODY.index('base = str(blur_src or scene.get("timeline_speed_src")')
        self.assertLess(drop, use)

    def test_the_reason_is_written_down_where_the_next_reader_will_be(self):
        self.assertIn("silently RESTORES the old footage", BODY)


if __name__ == "__main__":
    unittest.main()
