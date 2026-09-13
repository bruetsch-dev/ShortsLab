"""A short must not say its own hook three times.

Transcribed from a delivered file:

    "Why does only one of my nostrils work?"          <- the Google intro, spoken
    "Why does only one of my nostrils work?"          <- TTS part 0: the markdown TITLE
    "Why does only one of my nostrils work at a time?" <- the script's first real line

Stripping headings at the start of a run only helps a NEW run. A project generated before that
fix keeps a voiceover whose first part IS the title, and every rebuild reuses that audio. The
repair drops those parts, re-concatenates, and removes the matching beat.

The first version of this repair re-split the shortened script from scratch: 18 beats against 13
existing images, and the render refused five black scenes. The pictures were fine - the mapping
had moved. It now keeps the existing split and removes only the title beat.
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

import longform_video as lv


class RepairTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="lf_repair_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def project(self, **over):
        state = {
            "script": "# A Title Line\n\nFirst real sentence. Second one here.",
            "tts_part_texts": ["# A Title Line", "First real sentence. Second one here."],
            "tts_part_files": [str(self.tmp / "vo_part00.mp3"), str(self.tmp / "vo_part01.mp3")],
            "lines": [
                {"start": 0.0, "end": 2.0, "text": "# A Title Line",
                 "words": [{"w": "#", "s": 0.0}, {"w": "Title", "s": 1.0}]},
                {"start": 2.0, "end": 4.5, "text": "First real sentence.",
                 "words": [{"w": "First", "s": 2.1}]},
                {"start": 4.5, "end": 7.0, "text": "Second one here.",
                 "words": [{"w": "Second", "s": 4.6}]},
            ],
            "audio_duration": 7.0,
        }
        state.update(over)
        (self.tmp / lv.STATE_FILE).write_text(json.dumps(state), encoding="utf-8")
        return state

    def test_a_project_without_a_spoken_title_is_left_alone(self):
        self.project(tts_part_texts=["First real sentence.", "Second one here."])
        self.assertFalse(lv.repair_heading_voiceover(self.tmp))

    def test_a_project_with_no_state_does_not_raise(self):
        self.assertFalse(lv.repair_heading_voiceover(self.tmp / "nope"))

    def test_missing_part_files_refuse_rather_than_destroy_the_audio(self):
        """Without the parts the title cannot be cut out; overwriting the voiceover with a
        partial concat would be worse than leaving the extra line in."""
        self.project()
        self.assertFalse(lv.repair_heading_voiceover(self.tmp))
        self.assertIn("tts_part_texts", json.loads((self.tmp / lv.STATE_FILE).read_text()))

    def test_a_voiceover_that_is_only_a_title_is_never_emptied(self):
        self.project(tts_part_texts=["# Only A Title"],
                     tts_part_files=[str(self.tmp / "vo_part00.mp3")])
        self.assertFalse(lv.repair_heading_voiceover(self.tmp))


class RepairShapeTests(unittest.TestCase):
    """The behaviour that matters, read off the implementation - running it needs real audio."""

    def setUp(self):
        self.src = Path(lv.__file__).read_text(encoding="utf-8")
        self.block = self.src[self.src.index("def repair_heading_voiceover"):
                              self.src.index("def rebuild_from_disk")]

    def test_it_keeps_the_existing_beat_split(self):
        self.assertNotIn("transcribe_lines(", self.block,
                         "re-splitting is what produced 18 beats for 13 images")
        self.assertIn("old_lines = list(state.get(\"lines\") or [])", self.block)

    def test_every_later_beat_is_shifted_by_the_removed_audio(self):
        for piece in ('moved["start"]', 'moved["end"]', 'moved["words"]'):
            self.assertIn(piece, self.block)
        self.assertIn("- offset", self.block)

    def test_the_title_s_own_picture_is_removed_so_indices_still_line_up(self):
        self.assertIn("_removed_title", self.block)
        self.assertIn("reconcile_image_names", self.block)

    def test_the_repair_runs_before_anything_reads_the_timings(self):
        rebuild = self.src[self.src.index("def rebuild_from_disk"):]
        repair_at = rebuild.index("repair_heading_voiceover(project_dir")
        frames_at = rebuild.index("frames_from_disk(project_dir)")
        self.assertLess(repair_at, frames_at)


class CanvasTests(unittest.TestCase):
    """A 9:16 project rendered 720x1280 frames onto a 1920x1080 canvas: the assembly read a
    module-global that any concurrent job is free to change."""

    def setUp(self):
        self.src = Path(lv.__file__).read_text(encoding="utf-8")

    def test_the_assembly_takes_its_canvas_as_an_argument(self):
        import inspect
        self.assertIn("aspect", inspect.signature(lv.assemble_video).parameters)

    def test_it_does_not_read_the_global_size_any_more(self):
        block = self.src[self.src.index("def assemble_video"):]
        block = block[:block.index("\ndef ", 10)]
        self.assertNotIn("video_size()", block)
        self.assertIn("canvas_w, canvas_h", block)

    def test_the_caller_passes_the_project_s_own_format(self):
        self.assertIn('aspect=str(state.get("aspect") or IMAGE_ASPECT)', self.src)


if __name__ == "__main__":
    unittest.main()
