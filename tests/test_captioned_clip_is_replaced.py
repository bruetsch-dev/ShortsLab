"""A beat never goes out with a creator's caption on it while a clean clip is on disk.

ProPainter rebuilds a caption from what neighbouring frames reveal. Text printed over a moving
street never uncovers its own background, so the fill comes back still readable, the residual check
restores the original - and until now that was the end of it: the beat went out captioned, because
the render had nothing else to put there.

It does have something. Every window V4 inspected is still in the project, and the report holds its
measured `caption_share`, its verdict and the query it answered. So the render, in this order:

  1. cleans the clip (ProPainter, the only backend),
  2. if that fails, looks for a text-free stretch of the SAME source (same subject, same look),
  3. if there is none, takes another window from THIS run's own pool that measured clean
     (`caption_share <= 0.015`) and was reviewed at relevance 8 or better,
  4. and only if none of that exists does the original stay.

Nothing is downloaded and nothing is re-reviewed; step 3 only re-uses what the run already
inspected and paid for.
"""

import inspect
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent_core


def _project(tmp, candidates):
    root = Path(tmp)
    (root / "review").mkdir(parents=True, exist_ok=True)
    (root / "review" / "scrape_v4_report.json").write_text(
        json.dumps({"candidates": candidates}), encoding="utf-8")
    return root


def _clip(root, name):
    p = root / name
    p.write_bytes(b"\0" * 5000)
    return str(p)


class ThePoolIsAskedBeforeGivingUp(unittest.TestCase):
    def test_a_clean_high_relevance_window_replaces_the_captioned_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            good = _clip(root, "clean_one.mp4")
            cands = [
                {"path": good, "caption_share": 0.004, "start": 12.5, "end": 15.5,
                 "source_id": "tiktok__991", "query": "japanese couples matching outfits",
                 "vision": {"relevance": 9}},
            ]
            _project(tmp, cands)
            scene = {"clip": "dirty.mp4", "search_query": "japanese couples matching outfits",
                     "exact_voice_text": "outfits are surprisingly common"}
            out = agent_core._clean_replacement_from_pool(root, scene, 2.0)
            # The post id travels with the swap: the beat now plays a different VIDEO, and the
            # duplicate guard keys on scrape_clip_id.
            self.assertEqual(out, ("clean_one.mp4", 12.5, "tiktok__991"))

    def test_a_captioned_candidate_is_not_a_replacement(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cands = [{"path": _clip(root, "also_dirty.mp4"), "caption_share": 0.09,
                      "start": 4.0, "end": 8.0, "query": "q", "vision": {"relevance": 10}}]
            _project(tmp, cands)
            self.assertIsNone(agent_core._clean_replacement_from_pool(
                root, {"clip": "dirty.mp4", "search_query": "q"}, 2.0))

    def test_a_weakly_reviewed_window_is_not_a_replacement(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cands = [{"path": _clip(root, "weak.mp4"), "caption_share": 0.0,
                      "start": 1.0, "end": 5.0, "query": "q", "vision": {"relevance": 6}}]
            _project(tmp, cands)
            self.assertIsNone(agent_core._clean_replacement_from_pool(
                root, {"clip": "dirty.mp4", "search_query": "q"}, 2.0))

    def test_a_window_shorter_than_the_beat_is_not_a_replacement(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cands = [{"path": _clip(root, "short.mp4"), "caption_share": 0.0,
                      "start": 1.0, "end": 2.0, "query": "q", "vision": {"relevance": 10}}]
            _project(tmp, cands)
            self.assertIsNone(agent_core._clean_replacement_from_pool(
                root, {"clip": "dirty.mp4", "search_query": "q"}, 2.6))

    def test_the_beats_own_query_wins_over_a_stranger(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cands = [
                {"path": _clip(root, "stranger.mp4"), "caption_share": 0.0, "start": 2.0,
                 "end": 6.0, "query": "something else", "vision": {"relevance": 10}},
                {"path": _clip(root, "same_query.mp4"), "caption_share": 0.0, "start": 3.0,
                 "end": 7.0, "query": "the beat query", "vision": {"relevance": 8}},
            ]
            _project(tmp, cands)
            out = agent_core._clean_replacement_from_pool(
                root, {"clip": "dirty.mp4", "search_query": "the beat query"}, 2.0)
            self.assertEqual(out[0], "same_query.mp4")

    def test_a_missing_file_is_never_offered(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _project(tmp, [{"path": str(root / "gone.mp4"), "caption_share": 0.0,
                            "start": 1.0, "end": 6.0, "query": "q", "vision": {"relevance": 10}}])
            self.assertIsNone(agent_core._clean_replacement_from_pool(
                root, {"clip": "dirty.mp4", "search_query": "q"}, 2.0))


class TheOrderOfLastResorts(unittest.TestCase):
    def test_the_last_resorts_are_tried_in_order(self):
        """Move inside the same source before swapping it for another one.

        The two of them live in `look_elsewhere` now, because the heaviest captions skip the
        rebuild entirely and go straight there - so the order that matters is the order inside
        that helper, not the order of the whole function.
        """
        body = inspect.getsource(agent_core.prepare_timeline_clips)
        helper = body[body.index("def look_elsewhere"):]
        self.assertLess(helper.index("_text_free_window"),
                        helper.index("_clean_replacement_from_pool"))

    def test_a_rebuildable_caption_is_rebuilt_before_anything_is_swapped(self):
        body = inspect.getsource(agent_core.prepare_timeline_clips)
        self.assertLess(body.index("remove_caption_regions"),
                        body.index("elif look_elsewhere()"))

    def test_a_caption_too_big_to_rebuild_does_not_ship(self):
        """`> MAX_COVERAGE` used to `continue` - the worst captions were the only untreated ones."""
        body = inspect.getsource(agent_core.prepare_timeline_clips)
        self.assertIn("if coverage > caption_remover.MAX_COVERAGE:", body)
        branch = body[body.index("if coverage > caption_remover.MAX_COVERAGE:"):]
        self.assertLess(branch.index("look_elsewhere()"), branch.index("\n            continue"))

    def test_nothing_is_downloaded_for_the_swap(self):
        """The code, not the prose: the docstring is allowed to say the word 'downloaded'."""
        source = inspect.getsource(agent_core._clean_replacement_from_pool)
        body = source.split('"""')[-1].lower()
        for forbidden in ("download(", "requests.", "urlopen", "scrape_v4.", "http"):
            self.assertNotIn(forbidden, body)


if __name__ == "__main__":
    unittest.main()
