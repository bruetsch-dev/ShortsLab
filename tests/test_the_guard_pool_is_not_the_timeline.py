"""The guard against repeats was repairing repeats with copies of what was already on screen.

`enforce_unique_scene_clips` swaps a duplicated clip for an unused one from the project pool. The
pool was "every .mp4 in the clip folder whose NAME is not on the timeline" - and V4 leaves a
byte-identical twin of every assignment behind: `scraped_00.mp4` and
`v4_00_tiktok__7671326811866893581.mp4` have the same md5.

Measured on the roundabouts Short (2026-09-09), on its own files:

    files not on the timeline : 17
      of those, byte-identical twins of clips that ARE : 17
      genuine alternatives                            :  0

So the shelf was a hundred percent copies. The guard could not do anything but repeat, and it
recorded each repeat as a repair. Scene 11 was handed the source already running on scenes 01 and
15 - the operating theatre that ended up under a line about roundabouts - and scene 13 the source
already on 01 and 14.

Two more things let that happen and are fixed with it:

  * `_source_of` only read a sidecar, and V4 writes none, so every candidate came back with
    `clip_id = ""`. The post id is in the file name.
  * `not c["clip_id"]` treated a missing id as a free pass through the per-source cap, which is
    most of what V4 leaves on disk. An unknown source is now the last choice, not the first.
"""

import hashlib
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent_core


def _project(files, scenes):
    """files: {name: bytes}. Sidecars are written for any name given a clip_id."""
    root = tempfile.mkdtemp()
    clips = os.path.join(root, "seedance 2.0")
    os.makedirs(clips)
    for name, (payload, clip_id) in files.items():
        with open(os.path.join(clips, name), "wb") as fh:
            fh.write(payload)
        if clip_id is not None:
            with open(os.path.join(clips, name.replace(".mp4", ".json")), "w") as fh:
                json.dump({"clip_id": clip_id, "status": "accepted"}, fh)
    return root, {"scenes": scenes}


class ACopyIsNotAReplacement(unittest.TestCase):
    def test_a_byte_identical_twin_is_not_offered(self):
        same = b"\x01" * 9000
        root, config = _project(
            {"scraped_00.mp4": (same, "SRC1"),
             "scraped_01.mp4": (same, "SRC1"),
             "v4_00_tiktok__111.mp4": (same, None)},          # the twin V4 leaves behind
            [{"clip": "scraped_00.mp4", "scrape_clip_id": "SRC1"},
             {"clip": "scraped_01.mp4", "scrape_clip_id": "SRC1"},
             {"clip": "scraped_01.mp4", "scrape_clip_id": "SRC1"}])
        swaps = agent_core.enforce_unique_scene_clips(config, root)
        self.assertEqual(0, swaps, "the guard swapped in a copy of what is already on screen")
        self.assertEqual("scraped_01.mp4", config["scenes"][2]["clip"])

    def test_a_genuinely_different_clip_is_still_offered(self):
        root, config = _project(
            {"a.mp4": (b"\x01" * 9000, "SRC1"),
             "b.mp4": (b"\x02" * 9000, "SRC1"),
             "spare.mp4": (b"\x03" * 9000, "SRC2")},
            [{"clip": "a.mp4", "scrape_clip_id": "SRC1"},
             {"clip": "b.mp4", "scrape_clip_id": "SRC1"},
             {"clip": "b.mp4", "scrape_clip_id": "SRC1"}])
        swaps = agent_core.enforce_unique_scene_clips(config, root)
        self.assertEqual(1, swaps)
        self.assertEqual("spare.mp4", config["scenes"][2]["clip"])


class TheNameCarriesThePostId(unittest.TestCase):
    def test_the_id_is_read_out_of_a_v4_file_name(self):
        root, config = _project(
            {"v4_00_tiktok__777.mp4": (b"\x01" * 9000, None),
             "v4_01_tiktok__777.mp4": (b"\x02" * 9000, None),
             "v4_02_tiktok__777.mp4": (b"\x03" * 9000, None)},
            [{"clip": "v4_00_tiktok__777.mp4"},
             {"clip": "v4_01_tiktok__777.mp4"},
             {"clip": "v4_02_tiktok__777.mp4"}])
        agent_core.enforce_unique_scene_clips(config, root)
        report = config.get("duplicate_guard") or {}
        self.assertEqual(1, report.get("unresolved_repeats"),
                         "a third window of one source went unnoticed, so the id was not read "
                         f"out of the file name: {report}")

    def test_the_cap_is_not_bypassed_by_a_missing_id(self):
        """`not c["clip_id"]` used to wave every sidecar-less file past the per-source cap."""
        source = agent_core.__dict__
        self.assertIn("_free(candidate)", open(
            os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "agent_core.py"), encoding="utf-8").read())


if __name__ == "__main__":
    unittest.main()
