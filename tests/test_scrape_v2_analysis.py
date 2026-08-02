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
        self.assertIn("cadence_stutter", result.rejection_reasons)


if __name__ == "__main__":
    unittest.main()
