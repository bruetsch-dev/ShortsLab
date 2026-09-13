import shutil
import tempfile
import unittest
import urllib.parse
from pathlib import Path

import agent_core
import pipeline


def _fake_clip(path, size=2048):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x00" * size)
    return path


class ClipRefSourcePathTests(unittest.TestCase):
    def test_a_preview_url_yields_the_whole_path_not_just_the_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            clip = _fake_clip(Path(tmp) / "other project" / "scraped_00.mp4")
            url = "/file?path=" + urllib.parse.quote(str(clip))
            self.assertEqual(pipeline.clip_ref_source_path(url), clip)
            self.assertEqual(pipeline._clip_ref_basename(url), "scraped_00.mp4")

    def test_a_bare_name_has_no_source_path(self):
        self.assertIsNone(pipeline.clip_ref_source_path("scraped_00.mp4"))
        self.assertIsNone(pipeline.clip_ref_source_path(""))

    def test_a_path_that_does_not_exist_is_not_returned(self):
        missing = "/file?path=" + urllib.parse.quote(str(Path(tempfile.gettempdir()) / "nope.mp4"))
        self.assertIsNone(pipeline.clip_ref_source_path(missing))


class DraggedClipSurvivesRestartTests(unittest.TestCase):
    """Delete a clip, drag another one on, save, restart, reopen - and the slot came back empty.

    The library sends an absolute path for most items. When it does not, all that survives is the
    editor's preview URL, and the BASENAME taken out of it was stored as a project-relative clip
    reference without the file ever being copied in. In the browser it still played, because the
    model still held the original URL. After a restart the project only had a name: if a same-named
    file happened to exist the scene silently showed the WRONG clip, and if none did it came back
    empty."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.project = self.tmp / "target"
        (self.project / "seedance 2.0").mkdir(parents=True)
        self.foreign = _fake_clip(self.tmp / "source project" / "seedance 2.0" / "scraped_00.mp4")
        self._orig_projects = agent_core.PROJECTS_DIR
        agent_core.PROJECTS_DIR = self.tmp
        self.addCleanup(setattr, agent_core, "PROJECTS_DIR", self._orig_projects)

    def _config(self):
        return {"scenes": [
            {"id": "01", "start": 0.0, "end": 2.0, "clip": "a.mp4", "seedance": True},
            {"id": "02", "start": 2.0, "end": 4.0, "clip": "b.mp4", "seedance": True},
        ], "duration": 4.0}

    def _added(self, path_value, clip_value):
        return {"id": "add-1", "kind": "clip", "path": path_value,
                "clip": clip_value, "poster": "", "dur": 2.0, "after": "add-1"}

    def _apply(self, added):
        config = self._config()
        edits = {
            "scenes": [{"id": "01", "duration": 2.0}, {"id": "add-1", "duration": 2.0}],
            "order": ["01", "add-1"],
            "removed": ["02"],
            "added": [added],
            "replaced": [], "sfx": [], "transitions": [], "overlays": [],
        }
        agent_core.apply_timeline_edits_to_config(config, edits, "target", prepare_media=False)
        return config

    def test_a_clip_dragged_in_with_only_a_preview_url_is_copied_into_the_project(self):
        url = "/file?path=" + urllib.parse.quote(str(self.foreign))
        config = self._apply(self._added("", url))
        scene = [s for s in config["scenes"] if str(s.get("id")) == "add-1"][0]
        name = str(scene.get("clip") or "")
        self.assertTrue(name, "the added scene lost its clip entirely")
        self.assertNotEqual(name, "scraped_00.mp4",
                            "a bare foreign basename cannot resolve after a restart")
        self.assertTrue((self.project / "seedance 2.0" / name).is_file(),
                        "the file was never copied into the project")

    def test_a_clip_dragged_in_with_an_absolute_path_still_works(self):
        config = self._apply(self._added(str(self.foreign), ""))
        scene = [s for s in config["scenes"] if str(s.get("id")) == "add-1"][0]
        name = str(scene.get("clip") or "")
        self.assertTrue((self.project / "seedance 2.0" / name).is_file())

    def test_a_replacement_given_as_a_preview_url_is_copied_too(self):
        """The replace path swallowed the same failure in a bare except, so the scene silently
        kept the clip the user had just replaced."""
        config = self._config()
        url = "/file?path=" + urllib.parse.quote(str(self.foreign))
        edits = {"scenes": [], "order": ["01", "02"], "removed": [], "added": [],
                 "replaced": [{"id": "01", "path": url, "type": "video"}],
                 "sfx": [], "transitions": [], "overlays": []}
        agent_core.apply_timeline_edits_to_config(config, edits, "target", prepare_media=False)
        scene = [s for s in config["scenes"] if str(s.get("id")) == "01"][0]
        name = str(scene.get("clip") or "")
        self.assertNotEqual(name, "a.mp4", "the replacement was silently dropped")
        self.assertTrue((self.project / "seedance 2.0" / name).is_file())


if __name__ == "__main__":
    unittest.main()
