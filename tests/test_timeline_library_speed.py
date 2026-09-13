"""The timeline editor's media library must open now, not in a minute.

Reported: "die assets und die timeline beim timeline editor laden nach 1min erst". Measured
before the fix, with a warm duration cache: 37.8s for a project with 309 candidate clips, 23.3s
for one with 213 - and about two minutes when the clip-duration cache was cold as well.

The build itself is honest work: one ffprobe per clip, plus a scan of every project for footage
that can be reused. Doing all of it again on every open was not, so the finished payload is
cached against a stamp that costs one stat per project.
"""

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import app


class StampTests(unittest.TestCase):
    """The stamp has to notice a change anywhere the payload draws from - and nowhere else."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "alpha" / "config").mkdir(parents=True)
        (self.root / "alpha" / "config" / "project.json").write_text("{}", encoding="utf-8")
        (self.root / "beta" / "seedance 2.0" / "_candidates").mkdir(parents=True)
        patcher = mock.patch.object(app.agent_core, "PROJECTS_DIR", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.project = self.root / "alpha"

    def stamp(self):
        return app._project_library_stamp(self.project)

    def test_it_is_stable_while_nothing_changes(self):
        self.assertEqual(self.stamp(), self.stamp())

    def test_another_project_does_not_disturb_this_one(self):
        """The payload is split: this project's own media is cached against its own stamp, and
        the cross-project pool has a separate one. A clip downloaded elsewhere must not throw
        away the cache for the panel the editor opens with."""
        before = self.stamp()
        (self.root / "beta" / "seedance 2.0" / "_candidates" / "new.mp4").write_bytes(b"x")
        self.assertEqual(before, self.stamp())

    def test_the_cross_project_pool_has_its_own_stamp(self):
        before = app._global_library_stamp()
        (self.root / "beta" / "seedance 2.0" / "_candidates" / "another.mp4").write_bytes(b"x")
        self.assertNotEqual(before, app._global_library_stamp())

    def test_a_new_project_invalidates_the_pool(self):
        before = app._global_library_stamp()
        (self.root / "gamma" / "seedance 2.0").mkdir(parents=True)
        self.assertNotEqual(before, app._global_library_stamp())

    def test_an_edit_to_this_project_invalidates_it(self):
        before = self.stamp()
        config = self.project / "config" / "project.json"
        os.utime(config, (time.time() + 10, time.time() + 10))
        self.assertNotEqual(before, self.stamp())

    def test_it_never_lists_a_directory_it_does_not_have_to(self):
        """Listing every project's media is what the cache exists to avoid; the stamp must not
        quietly reintroduce it."""
        source = open(app.__file__, encoding="utf-8").read()
        body = source[source.index("def _global_library_stamp("):]
        body = body[:body.index("\ndef ", 10)]
        self.assertNotIn(".rglob(", body)
        self.assertNotIn(".glob(", body)
        # os.scandir on the candidate folders IS allowed - it counts entries without stat-ing
        # them, which is what makes "a file appeared" reliable where a directory mtime is not.
        self.assertIn("os.scandir(", body)


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "alpha" / "config").mkdir(parents=True)
        (self.root / "alpha" / "config" / "project.json").write_text("{}", encoding="utf-8")
        patcher = mock.patch.object(app.agent_core, "PROJECTS_DIR", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        safe = mock.patch.object(app, "safe_project_dir", lambda slug: self.root / slug)
        safe.start()
        self.addCleanup(safe.stop)

    def test_a_second_open_does_not_rebuild(self):
        with mock.patch.object(app, "_build_project_library",
                               return_value={"media": [{"name": "a.mp4"}]}) as built:
            first = app.timeline_library_payload("alpha")
            second = app.timeline_library_payload("alpha")
        self.assertEqual(built.call_count, 1)
        self.assertEqual(first, second)

    def test_a_change_to_THIS_project_rebuilds(self):
        """Another project appearing no longer invalidates this panel - that is the cross-project
        pool's business now - but an edit here still must."""
        with mock.patch.object(app, "_build_project_library",
                               return_value={"media": []}) as built:
            app.timeline_library_payload("alpha")
            os.utime(self.root / "alpha" / "config" / "project.json",
                     (time.time() + 30, time.time() + 30))
            app.timeline_library_payload("alpha")
        self.assertEqual(built.call_count, 2)

    def test_the_cache_is_on_disk_so_a_restart_is_still_fast(self):
        with mock.patch.object(app, "_build_project_library",
                               return_value={"media": [{"name": "a.mp4"}]}):
            app.timeline_library_payload("alpha")
        stored = json.loads(app._library_cache_path("alpha").read_text(encoding="utf-8"))
        self.assertIn("stamp", stored)
        self.assertEqual(stored["payload"]["media"][0]["name"], "a.mp4")

    def test_an_unwritable_cache_does_not_fail_the_load(self):
        """A cache that cannot be written is a slow library, not a broken one."""
        with mock.patch.object(app, "_build_project_library",
                               return_value={"media": []}), \
             mock.patch.object(Path, "write_text", side_effect=OSError("read-only")):
            self.assertEqual(app.timeline_library_payload("alpha"), {"media": []})

    def test_a_corrupt_cache_file_is_rebuilt_rather_than_raised(self):
        app._library_cache_path("alpha").parent.mkdir(parents=True, exist_ok=True)
        app._library_cache_path("alpha").write_text("{not json", encoding="utf-8")
        with mock.patch.object(app, "_build_project_library",
                               return_value={"media": []}) as built:
            app.timeline_library_payload("alpha")
        self.assertTrue(built.called)


class ProbeWidthTests(unittest.TestCase):
    def test_the_duration_probe_runs_wide(self):
        """ffprobe is a subprocess per clip; the thread waits, so the useful width is past the
        core count. 16 workers were ~30s of a 309-clip cold load."""
        source = open(app.__file__, encoding="utf-8").read()
        body = source[source.index("def _prewarm_clip_durations("):]
        body = body[:body.index("\ndef ", 10)]
        self.assertIn("max_workers=min(32,", body)


if __name__ == "__main__":
    unittest.main()
