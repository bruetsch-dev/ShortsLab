import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import agent_core


class TimelineOpenDoesNoHeavyWorkTests(unittest.TestCase):
    """Opening the timeline editor ran the caption blur's OCR inline: measured at 9.4 of the 9.7
    seconds a page load took, for two clips. A project where every scene wants a blur would have
    blocked the browser for a minute. Reusing footage that already exists is free; BUILDING it
    belongs to the render job."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.slug = "proj"
        self.project = self.tmp / self.slug
        (self.project / "config").mkdir(parents=True)
        self.clips = self.project / "seedance 2.0"
        self.clips.mkdir(parents=True)
        (self.clips / "a.mp4").write_bytes(b"\x00" * 8192)
        (self.project / "config" / "project.json").write_text(json.dumps({
            "scenes": [{"id": "01", "start": 0.0, "end": 2.0, "clip": "a.mp4",
                        "seedance": True, "blur_captions": True}],
            "duration": 2.0,
        }), encoding="utf-8")
        (self.project / "config" / "timeline_edits.json").write_text(json.dumps({
            "scenes": [{"id": "01", "duration": 2.0, "blur_captions": True}],
            "order": ["01"], "removed": [], "added": [], "replaced": [],
            "sfx": [], "transitions": [], "overlays": [],
        }), encoding="utf-8")
        self._orig = agent_core.PROJECTS_DIR
        agent_core.PROJECTS_DIR = self.tmp
        self.addCleanup(setattr, agent_core, "PROJECTS_DIR", self._orig)

    def test_opening_never_runs_the_blur(self):
        import caption_remover
        with mock.patch.object(caption_remover, "remove_caption_regions") as blur:
            agent_core.load_project_config(self.slug, create_media=False)
        blur.assert_not_called()
        self.assertFalse(list(self.clips.glob("capblur_*")),
                         "opening the editor wrote derived footage")

    def test_the_render_still_builds_it(self):
        import caption_remover
        with mock.patch.object(caption_remover, "remove_caption_regions", return_value=1) as blur:
            agent_core.load_project_config(self.slug)
        blur.assert_called()

    def test_a_blur_that_already_exists_is_still_used_when_opening(self):
        """Once the render has built it, the editor must show it - that is the whole point of
        keeping reuse free while refusing to build."""
        import caption_remover
        with mock.patch.object(caption_remover, "remove_caption_regions", return_value=1):
            agent_core.load_project_config(self.slug)
        built = list(self.clips.glob("capblur_*"))
        self.assertTrue(built, "the render path did not produce the blurred file")
        with mock.patch.object(caption_remover, "remove_caption_regions") as blur:
            config = agent_core.load_project_config(self.slug, create_media=False)
        blur.assert_not_called()
        self.assertEqual(str(config["scenes"][0].get("clip")), built[0].name)

    def test_a_missing_blur_leaves_the_original_clip_in_place(self):
        import caption_remover
        with mock.patch.object(caption_remover, "remove_caption_regions") as blur:
            config = agent_core.load_project_config(self.slug, create_media=False)
        blur.assert_not_called()
        self.assertEqual(str(config["scenes"][0].get("clip")), "a.mp4",
                         "with no blur built yet the scene must keep its own footage")


if __name__ == "__main__":
    unittest.main()


class AssetLibraryDoesNotStatOrFetchEverythingTests(unittest.TestCase):
    """The asset library took ~7s server-side and then fired ~1.7k image requests at the browser
    the moment the "All projects" tab opened, which is what "loading forever" actually was. Two
    separate causes, so two separate guards: the server must not touch the filesystem just to
    build a URL, and the markup must not ask for a poster it cannot yet show."""

    def test_link_for_does_not_touch_the_filesystem(self):
        """resolve() opens every file to chase symlinks that do not exist here. It was called
        ~19.6k times per payload purely to build URL strings."""
        import app
        from pathlib import Path as _P
        with mock.patch.object(_P, "resolve", side_effect=AssertionError("resolve() in link_for")):
            url = app.link_for(str(Path(__file__)))
        self.assertIn("/file?path=", url)

    def test_link_for_still_round_trips_through_the_path_guard(self):
        """Dropping resolve() is only safe because the receiver resolves and re-validates."""
        import app
        import urllib.parse
        here = Path(__file__)
        url = app.link_for(here)
        got = app.safe_requested_path(url.split("path=", 1)[1])
        self.assertIsNotNone(got, "the URL link_for built no longer passes safe_requested_path")
        self.assertTrue(got.samefile(here))

    def test_library_tiles_defer_the_poster(self):
        """A poster written into the markup is fetched immediately no matter what preload says,
        so with thousands of clips it must ride along deferred and be promoted on scroll."""
        import app
        src = Path(app.__file__).read_text(encoding="utf-8")
        self.assertIn("data-lazy-poster", src)
        self.assertNotIn("""var poster=it.poster?(' poster="'""", src,
                         "library tiles set poster eagerly again")

    def test_the_timeline_page_owns_its_lazy_helper(self):
        """window.LazyVideo lives on the run page only; the timeline editor referencing it was a
        silent no-op, so its tiles would have stayed blank forever."""
        import app
        src = Path(app.__file__).read_text(encoding="utf-8")
        tl = src[src.index("function libItemEl"):src.index("function renderMediaLib")]
        self.assertNotIn("window.LazyVideo", tl,
                         "the timeline editor is calling a helper that does not exist there")
        self.assertIn("TLLazy", src)
