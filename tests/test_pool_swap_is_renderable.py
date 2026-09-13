"""A pool swap must produce a clip the renderer can actually open, and never the same one twice.

Measured on the dating recut (2026-09-05). Six beats could not be cleaned, each one asked the pool
for a replacement, and the answer was identical every time: the basename of a candidate that lives
in `seedance 2.0/_v4_proxies/`. The renderer resolves a scene's clip as a bare name inside
`seedance 2.0/`, so none of those names resolved - three beats shipped as black "SCENE 01" title
cards, and had they resolved, one window would have covered six beats.

So the chosen window is copied into the clip folder before its name is handed back, and the caller
passes what the edit has already assigned so the same window is not offered again.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent_core


def _pool_project(tmp, candidates):
    """A project whose pool sits where V4 puts it: in a subfolder of the clip folder."""
    root = Path(tmp)
    (root / "review").mkdir(parents=True, exist_ok=True)
    (root / "seedance 2.0" / "_v4_proxies").mkdir(parents=True, exist_ok=True)
    rows = []
    for name, payload in candidates:
        path = root / "seedance 2.0" / "_v4_proxies" / name
        path.write_bytes(payload)
        rows.append({"path": str(path), "caption_share": 0.0, "start": 6.0, "end": 12.0,
                     "query": "q", "vision": {"relevance": 9}})
    (root / "review" / "scrape_v4_report.json").write_text(
        json.dumps({"candidates": rows}), encoding="utf-8")
    return root


class TheSwapLandsWhereTheRendererLooks(unittest.TestCase):
    def test_the_window_is_copied_into_the_clip_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _pool_project(tmp, [("v4_tiktok_7345755827334073601.mp4", b"\0" * 5000)])
            out = agent_core._clean_replacement_from_pool(
                root, {"clip": "dirty.mp4", "search_query": "q"}, 2.0)
            self.assertIsNotNone(out)
            self.assertTrue((root / "seedance 2.0" / out[0]).is_file(),
                            f"{out[0]} is not in the clip folder")

    def test_a_name_collision_does_not_overwrite_a_different_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _pool_project(tmp, [("v4_tiktok_7345755827334073601.mp4", b"\0" * 5000)])
            other = root / "seedance 2.0" / "v4_tiktok_7345755827334073601.mp4"
            other.write_bytes(b"\1" * 9000)                     # a different file, same name
            out = agent_core._clean_replacement_from_pool(
                root, {"clip": "dirty.mp4", "search_query": "q"}, 2.0)
            self.assertNotEqual(out[0], "v4_tiktok_7345755827334073601.mp4")
            self.assertEqual(other.read_bytes(), b"\1" * 9000)
            self.assertTrue((root / "seedance 2.0" / out[0]).is_file())


class OneWindowCoversOneBeat(unittest.TestCase):
    def test_a_window_the_edit_already_uses_is_not_offered(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _pool_project(tmp, [("v4_tiktok_7345755827334073601.mp4", b"\0" * 5000)])
            self.assertIsNone(agent_core._clean_replacement_from_pool(
                root, {"clip": "dirty.mp4", "search_query": "q"}, 2.0,
                taken={"v4_tiktok_7345755827334073601.mp4"}))

    def test_a_derived_copy_of_the_failing_clip_is_not_offered_back(self):
        """Every derivation keeps the post id - that IS the same footage."""
        with tempfile.TemporaryDirectory() as tmp:
            root = _pool_project(tmp, [("v4_tiktok_7345755827334073601.mp4", b"\0" * 5000)])
            self.assertIsNone(agent_core._clean_replacement_from_pool(
                root, {"clip": "unbox_v4_tiktok_73457558273340_ab12cd34.mp4", "search_query": "q"}, 2.0))

    def test_two_beats_get_two_different_windows(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _pool_project(tmp, [("v4_tiktok_7345755827334073601.mp4", b"\0" * 5000),
                                       ("v4_tiktok_7628560332566334727.mp4", b"\0" * 6000)])
            taken = set()
            first = agent_core._clean_replacement_from_pool(
                root, {"clip": "dirty_a.mp4", "search_query": "q"}, 2.0, taken=taken)
            taken.add(first[0])
            second = agent_core._clean_replacement_from_pool(
                root, {"clip": "dirty_b.mp4", "search_query": "q"}, 2.0, taken=taken)
            self.assertIsNotNone(second)
            self.assertNotEqual(first[0], second[0])


class TheCallerTracksWhatItGaveOut(unittest.TestCase):
    def test_prepare_seeds_the_set_from_the_scenes_and_adds_each_swap(self):
        import inspect
        body = inspect.getsource(agent_core.prepare_timeline_clips)
        self.assertIn('assigned = {str(scene.get("clip") or "")', body)
        self.assertIn("taken=assigned", body)
        self.assertIn("assigned.add(swap[0])", body)


if __name__ == "__main__":
    unittest.main()
