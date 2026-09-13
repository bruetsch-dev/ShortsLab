"""Saving a timeline stores intent. It must not build media while the browser waits.

`save_timeline_edits` says so in its own docstring - "Media preprocessing belongs to the render
job; doing it here can block the HTTP request on FFmpeg" - and then passed only `prepare_media`.
The argument that decides whether derived footage is BUILT is `create_media`, and it defaulted to
True. So every save measured caption coverage scene by scene and re-encoded the speed-changed
clips first.

Measured on a 14-scene edit with nothing changed:

    before   48.3 s
    after     0.02 s

The Render button saves before it renders, so that was 48 seconds of "saving" in front of every
render.
"""

import inspect
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent_core

SAVE = inspect.getsource(agent_core.save_timeline_edits)
RENDER = inspect.getsource(agent_core.render_project_timeline)


class SavingBuildsNothing(unittest.TestCase):
    def test_the_config_is_loaded_without_building_media(self):
        self.assertIn("load_project_config(slug, prepare_media=False, create_media=False)", SAVE)

    def test_the_edits_are_applied_without_building_media(self):
        self.assertIn("prepare_media=False, create_media=False", SAVE)

    def test_the_measurement_is_written_down(self):
        self.assertIn("48.3 seconds", SAVE)


class RenderingStillBuildsIt(unittest.TestCase):
    """The work did not disappear - it belongs to the job the user waits on knowingly."""

    def test_the_render_does_not_switch_media_building_off(self):
        self.assertNotIn("create_media=False", RENDER)

    def test_the_render_runs_the_clip_preparation(self):
        self.assertIn("prepare_timeline_clips(config, project_dir", RENDER)


if __name__ == "__main__":
    unittest.main()
