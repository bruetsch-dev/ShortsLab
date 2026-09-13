import os
import tempfile
import time
import unittest
from pathlib import Path

import app


class MediaScanTests(unittest.TestCase):
    """count_media and latest_media walked with rglob('*') and then called is_file() on every
    entry, which costs one extra stat apiece: a single project list was 46,000 stat calls and 1.5
    of its 4.9 seconds. They use os.walk now, which hands back files and directories already
    separated. These tests pin the behaviour that must not change with it."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)

    def _touch(self, rel, mtime=None):
        path = self.tmp / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    def test_it_counts_recursively_and_only_the_asked_for_suffixes(self):
        self._touch("a.mp4")
        self._touch("deep/b.MP4")            # case-insensitive
        self._touch("deep/deeper/c.mov")
        self._touch("deep/notes.txt")
        (self.tmp / "empty_dir").mkdir()
        self.assertEqual(app.count_media(self.tmp, {".mp4"}), 2)
        self.assertEqual(app.count_media(self.tmp, {".mp4", ".mov"}), 3)
        self.assertEqual(app.count_media(self.tmp, {".png"}), 0)

    def test_a_directory_named_like_a_media_file_is_not_counted(self):
        (self.tmp / "renders.mp4").mkdir()
        self._touch("real.mp4")
        self.assertEqual(app.count_media(self.tmp, {".mp4"}), 1)

    def test_a_missing_folder_counts_zero_and_has_no_latest(self):
        missing = self.tmp / "nope"
        self.assertEqual(app.count_media(missing, {".mp4"}), 0)
        self.assertIsNone(app.latest_media(missing, {".mp4"}))

    def test_latest_media_returns_the_newest_matching_file(self):
        now = time.time()
        self._touch("old.mp4", mtime=now - 5000)
        newest = self._touch("deep/new.mp4", mtime=now - 10)
        self._touch("newer_but_wrong_type.mov", mtime=now)
        found = app.latest_media(self.tmp, {".mp4"})
        self.assertIsNotNone(found)
        self.assertEqual(Path(found).resolve(), newest.resolve())

    def test_latest_media_is_none_when_nothing_matches(self):
        self._touch("a.txt")
        self.assertIsNone(app.latest_media(self.tmp, {".mp4"}))

class WebManifestTests(unittest.TestCase):
    """The manifest is what an installed app pins to the taskbar, so its icon entry has to be
    honest: Chromium picks icons by the DECLARED size, and a declaration that does not match the
    file is one of the ways a custom icon quietly becomes a generic one."""

    def test_the_declared_icon_size_matches_the_file(self):
        from PIL import Image
        entry = next(i for i in app.web_manifest()["icons"] if i["src"].endswith(".png"))
        width, height = Image.open("static/" + entry["src"].split("/static/")[1]).size
        self.assertEqual(entry["sizes"], f"{width}x{height}")

    def test_the_icon_is_not_declared_maskable_without_a_safe_zone(self):
        for entry in app.web_manifest()["icons"]:
            self.assertNotIn("maskable", entry.get("purpose", ""),
                             "a maskable icon needs ~20% padding or the platform crops it")

    def test_the_installed_app_carries_the_current_name(self):
        manifest = app.web_manifest()
        self.assertEqual(manifest["name"], "Shortslab")
        self.assertEqual(manifest["short_name"], "Shortslab")

    def test_the_route_serves_exactly_this_manifest(self):
        import inspect
        self.assertIn("manifest = web_manifest()", inspect.getsource(app))


if __name__ == "__main__":
    unittest.main()
