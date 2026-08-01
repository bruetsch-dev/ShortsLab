import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np

import pipeline


class VideoCadenceTests(unittest.TestCase):
    @staticmethod
    def _write_video(path, duplicate_at=None, frozen_tail=0):
        writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), 30, (160, 90))
        frames = []
        for index in range(30):
            frame = np.zeros((90, 160, 3), dtype=np.uint8)
            x = 8 + index * 3
            cv2.rectangle(frame, (x, 28), (x + 24, 62), (255, 255, 255), -1)
            frames.append(frame)
        if duplicate_at is not None:
            frames[duplicate_at] = frames[duplicate_at - 1].copy()
        if frozen_tail:
            frames.extend([frames[-1].copy() for _ in range(frozen_tail)])
        for frame in frames:
            writer.write(frame)
        writer.release()

    def test_isolated_duplicate_frame_is_detected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "hitch.avi"
            self._write_video(path, duplicate_at=12)
            events = pipeline.micro_stutter_events(path)
            self.assertTrue(any(0.30 <= stamp <= 0.50 for stamp in events), events)

    def test_smooth_motion_has_no_false_hitch(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "smooth.avi"
            self._write_video(path)
            self.assertEqual(pipeline.micro_stutter_events(path), [])

    def test_repeated_frame_tail_is_detected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "stall.avi"
            self._write_video(path, frozen_tail=8)
            self.assertGreaterEqual(pipeline.repeated_frame_stall_seconds(path), 0.20)


if __name__ == "__main__":
    unittest.main()
