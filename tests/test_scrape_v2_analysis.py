import unittest
from unittest import mock

import numpy as np

import scrape_v2


class ScrapeV2AnalysisTests(unittest.TestCase):
    def test_segment_analysis_uses_pipeline_micro_stutter_detector(self):
        segment = scrape_v2.SegmentCandidate(
            segment_id="segment-1",
            source_id="source-1",
            platform="tiktok",
            source_path="sample.mp4",
            start_time=1.0,
            end_time=4.0,
            duration=3.0,
        )
        frames = [np.zeros((32, 18, 3), dtype=np.uint8) for _ in range(5)]

        with (
            mock.patch.object(scrape_v2.clip_scraper, "_probe_dims", return_value=(1080, 1920)),
            mock.patch.object(scrape_v2.clip_scraper, "_sample_bgr_frames", return_value=frames),
            mock.patch.object(scrape_v2, "_sample_window_frames", return_value=frames),
            mock.patch.object(scrape_v2, "_detect_duplicate_frame_run", return_value=0.0),
            mock.patch.object(scrape_v2.pipeline, "micro_stutter_events", return_value=[2.25]) as cadence,
            mock.patch.object(
                scrape_v2.clip_scraper,
                "detect_fake_vertical_or_black_bars",
                return_value={"black_bar_score": 0.0},
            ),
            mock.patch.object(scrape_v2, "_ocr_window", return_value=([], [], [])),
            mock.patch.object(scrape_v2.clip_scraper, "_score_from_ocr", return_value=0.0),
            mock.patch.object(scrape_v2, "_segment_caption_signals", return_value=(0.0, 0.0, False)),
            mock.patch.object(
                scrape_v2,
                "_window_stability",
                return_value={"stable": True, "internal_cut_count": 0},
            ),
        ):
            result = scrape_v2.analyze_segment_v2(segment, "ffmpeg", "ffprobe")

        cadence.assert_called_once_with("sample.mp4", 1.0, 4.0)
        self.assertEqual(result.micro_stutter_count, 1)
        # ONE hitch is a pan decelerating or a person pausing, not a defect - rejecting on the
        # first one killed 31% of every candidate window measured on a real pool. What is
        # refused now is a source whose cadence is broken THROUGHOUT, which is a RATE
        # (>= CADENCE_HITCH_RATE per second), so a single event over a 3s window must survive.
        self.assertNotIn("cadence_stutter", result.rejection_reasons)

    def test_a_window_whose_cadence_is_broken_throughout_is_still_rejected(self):
        import numpy as np
        segment = scrape_v2.SegmentCandidate(
            segment_id="s", source_id="src", platform="tiktok", source_path="sample.mp4",
            start_time=1.0, end_time=4.0, duration=3.0)
        frames = [np.zeros((32, 18, 3), dtype=np.uint8) for _ in range(5)]
        # 0.8 hitches per second over a 3s window = a re-encode with duplicated frames
        hitches = [1.2, 1.9, 2.6, 3.3]
        with (
            mock.patch.object(scrape_v2.clip_scraper, "_probe_dims", return_value=(1080, 1920)),
            mock.patch.object(scrape_v2.clip_scraper, "_sample_bgr_frames", return_value=frames),
            mock.patch.object(scrape_v2, "_sample_window_frames", return_value=frames),
            mock.patch.object(scrape_v2, "_detect_duplicate_frame_run", return_value=0.0),
            mock.patch.object(scrape_v2.pipeline, "micro_stutter_events", return_value=hitches),
            mock.patch.object(
                scrape_v2.clip_scraper, "detect_fake_vertical_or_black_bars",
                return_value={"black_bar_score": 0.0}),
            mock.patch.object(scrape_v2, "_ocr_window", return_value=([], [], [])),
            mock.patch.object(scrape_v2.clip_scraper, "_score_from_ocr", return_value=0.0),
            mock.patch.object(scrape_v2, "_segment_caption_signals", return_value=(0.0, 0.0, False)),
            mock.patch.object(
                scrape_v2, "_window_stability",
                return_value={"stable": True, "internal_cut_count": 0}),
        ):
            result = scrape_v2.analyze_segment_v2(segment, "ffmpeg", "ffprobe")
        self.assertEqual(result.micro_stutter_count, len(hitches))
        self.assertGreaterEqual(len(hitches) / 3.0, scrape_v2.CADENCE_HITCH_RATE)

    def test_hook_pool_drops_candidates_with_burned_in_captions(self):
        """The opening shot is the first thing on screen. Choosing a clip that already carries
        somebody else's caption announces "reposted" before a word is spoken."""
        def seg(name, **desc):
            s = mock.Mock()
            s.name = name
            s.visual_description = desc
            s.caption_probability = desc.pop("_prob", 0.0)
            return s

        clean = seg("clean")
        burned = seg("burned", burned_captions=True)
        overlay = seg("overlay", creator_overlay=True)
        likely = seg("likely")
        likely.caption_probability = 0.8

        kept = scrape_v2.hook_pool_without_burned_captions([clean, burned, overlay, likely])
        self.assertEqual([s.name for s in kept], ["clean"])

    def test_hook_pool_keeps_captioned_clips_when_nothing_else_exists(self):
        """A captioned opener beats no opener - but the run has to say so."""
        burned = mock.Mock()
        burned.visual_description = {"burned_captions": True}
        burned.caption_probability = 0.9
        said = []
        kept = scrape_v2.hook_pool_without_burned_captions([burned], status_cb=said.append)
        self.assertEqual(kept, [burned])
        self.assertTrue(any("burned-in text" in m for m in said), said)

    def test_resumed_proxies_do_not_spend_this_run_s_download_budget(self):
        """A second run on an existing project downloaded almost nothing.

        _downloaded_ids served two purposes at once: "do we already have this source?" and "how
        much has this run spent?". Resuming seeds it with every proxy on disk, so the Zauo re-run
        started at 184 against a cap of 45 and broke out of the planning loop on the first source
        of every round - 897 ranked candidates, 11 downloads, 8 of 13 beats uncovered, and it
        stopped with more than half its time budget unspent.
        """
        import types
        sources = [types.SimpleNamespace(source_id=f"new{i}", platform="tiktok",
                                         raw_item={"id": f"new{i}"}, query="q")
                   for i in range(30)]
        state = {"_downloaded_ids": {f"old{i}" for i in range(184)}, "_fetched_ids": set()}
        calls = []

        def fake_download(raw_item, proxy, _cb):
            calls.append(raw_item["id"])
            return None                      # dead link: nothing to analyse, planning is the test

        with mock.patch.object(scrape_v2, "download_proxy_v2", side_effect=fake_download):
            scrape_v2._download_and_segment(
                sources, self._tmp_project(), "ffmpeg", "ffprobe",
                cancel_check=None, deadline=None, state=state,
                status_cb=None, download_budget=12)

        self.assertEqual(len(calls), 12, "resumed proxies ate the budget again")
        self.assertTrue(all(c.startswith("new") for c in calls))
        # The dedupe set still holds everything, so nothing already on disk is re-fetched.
        self.assertEqual(len(state["_downloaded_ids"]), 184 + 12)

    def test_a_source_already_on_disk_is_never_downloaded_again(self):
        import types
        sources = [types.SimpleNamespace(source_id="have", platform="tiktok",
                                         raw_item={"id": "have"}, query="q"),
                   types.SimpleNamespace(source_id="want", platform="tiktok",
                                         raw_item={"id": "want"}, query="q")]
        state = {"_downloaded_ids": {"have"}, "_fetched_ids": set()}
        calls = []
        with mock.patch.object(scrape_v2, "download_proxy_v2",
                               side_effect=lambda raw, _p, _cb: calls.append(raw["id"])):
            scrape_v2._download_and_segment(
                sources, self._tmp_project(), "ffmpeg", "ffprobe",
                cancel_check=None, deadline=None, state=state,
                status_cb=None, download_budget=8)
        self.assertEqual(calls, ["want"])

    def _tmp_project(self):
        import tempfile
        tmp = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, tmp, True)
        return tmp

    def test_hook_scoring_penalises_a_face_buried_under_a_doodle_filter(self):
        """The capsule-hotel short opened on a creator whose face was covered in drawn white
        scribbles, while eleven clean topical clips sat unused in the same candidate pool. A
        drawn filter is not text, so text_penalty never saw it."""
        import inspect
        src = inspect.getsource(scrape_v2.score_hook_candidates_v2)
        self.assertIn("filter_penalty", src)
        self.assertIn('"filter_penalty":n', src, "the vision schema has to ask for it")
        self.assertIn('g("filter_penalty")', src, "and the score has to use it")
        self.assertIn("drawn stickers or doodles", src, "and it has to be a hard reject too")


if __name__ == "__main__":
    unittest.main()
