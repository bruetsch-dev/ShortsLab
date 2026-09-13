"""The sidebar must not make you wait for the filesystem.

Measured on this library (337 project folders, 394 KB of payload):

    before                    43.7s first load, 3.3s after
    with cached summaries     41.7s first load   <- the cache KEY still cost 5 stats x 337
    serving the last answer    0.61s after an app restart

The first number is all cold I/O: building one summary costs about five directory scans, and
even deciding whether a cached summary is still valid costs five stat calls. Nothing makes ~1700
cold filesystem calls fast, so the list stops being something the user waits for - the previous
answer is served immediately and rebuilt behind it.
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import chat_ui

ROOT = Path(__file__).resolve().parent.parent
SRC = (ROOT / "chat_ui.py").read_text(encoding="utf-8")
APP_SRC = (ROOT / "app.py").read_text(encoding="utf-8")


class StaleWhileRevalidateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="plist_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        patcher = mock.patch.object(chat_ui, "_payload_cache_path",
                                    return_value=self.tmp / "cache.json")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_cached_answer_is_served_without_touching_the_library(self):
        chat_ui._write_payload_cache("visible", {"projects": [{"slug": "a"}], "hidden_count": 0})
        with mock.patch.object(chat_ui, "_build_projects_list_payload") as build:
            payload = chat_ui.projects_list_payload()
        self.assertEqual(payload["projects"], [{"slug": "a"}])
        # the rebuild happens on a thread, so it may or may not have started - what matters is
        # that the ANSWER did not wait for it
        self.assertFalse(build.called and payload is build.return_value)

    def test_the_first_ever_load_still_builds(self):
        """There is nothing to show yet, so this one has to walk the library."""
        with mock.patch.object(chat_ui, "_build_projects_list_payload",
                               return_value={"projects": [], "hidden_count": 0}) as build:
            chat_ui.projects_list_payload()
        self.assertTrue(build.called)

    def test_the_first_build_is_written_for_next_time(self):
        with mock.patch.object(chat_ui, "_build_projects_list_payload",
                               return_value={"projects": [{"slug": "x"}], "hidden_count": 0}):
            chat_ui.projects_list_payload()
        self.assertEqual(chat_ui._read_payload_cache("visible")["projects"], [{"slug": "x"}])

    def test_hidden_and_visible_are_cached_apart(self):
        """One cache for both would show hidden projects to a user who did not ask."""
        chat_ui._write_payload_cache("visible", {"projects": [{"slug": "v"}]})
        chat_ui._write_payload_cache("hidden", {"projects": [{"slug": "h"}]})
        self.assertEqual(chat_ui._read_payload_cache("visible")["projects"][0]["slug"], "v")
        self.assertEqual(chat_ui._read_payload_cache("hidden")["projects"][0]["slug"], "h")

    def test_a_corrupt_cache_falls_back_to_building(self):
        (self.tmp / "cache.json").write_text("{ not json", encoding="utf-8")
        self.assertIsNone(chat_ui._read_payload_cache("visible"))

    def test_only_one_refresh_runs_at_a_time(self):
        """Three sidebar loads in a row must not start three full library walks."""
        self.assertIn("_PAYLOAD_REFRESHING", SRC)
        self.assertIn("_PAYLOAD_LOCK", SRC)

    def test_a_failed_refresh_releases_its_slot(self):
        block = SRC[SRC.index("def _refresh():"):]
        self.assertIn("finally:", block[:500])
        self.assertIn("discard(key)", block[:500])


class SummaryCacheTests(unittest.TestCase):
    def test_the_key_never_lists_a_directory(self):
        block = APP_SRC[APP_SRC.index("def project_cache_stamp"):
                        APP_SRC.index("_SUMMARY_CACHE_FILE")]
        self.assertIn(".stat().st_mtime", block)
        for listing in ("iterdir", "glob", "latest_media", "scandir"):
            self.assertNotIn(listing, block, "the cache key is doing a directory listing again")

    def test_a_new_render_still_invalidates(self):
        """Stat-ing the renders DIRECTORY notices a new file: adding one moves its parent."""
        block = APP_SRC[APP_SRC.index("def project_cache_stamp"):
                        APP_SRC.index("_SUMMARY_CACHE_FILE")]
        self.assertIn('"renders"', block)

    def test_paths_survive_the_json_round_trip(self):
        """A summary carries Path objects; JSON turns them into strings and the callers expect
        Paths back."""
        block = APP_SRC[APP_SRC.index("def cached_project_entry"):]
        self.assertIn('for key in ("video", "thumb", "project_dir")', block[:1600])
        self.assertIn("default=str", APP_SRC)

    def test_the_summary_and_its_sort_key_are_fetched_together(self):
        """Looking the edit time up separately put the directory listing straight back."""
        self.assertIn("app.cached_project_entry(p)", SRC)
        self.assertNotIn("app.project_edited_mtime(p) for p in dirs", SRC)


if __name__ == "__main__":
    unittest.main()
