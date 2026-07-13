import unittest
from pathlib import Path

import visual_agent


class VisualMemeReactionTests(unittest.TestCase):
    def test_every_sticker_has_reaction_and_existing_specific_sound(self):
        memes = visual_agent.available_memes()
        self.assertEqual(len(memes), 23)
        for meme_id, rec in memes.items():
            self.assertTrue(rec.get("reaction"), meme_id)
            self.assertTrue(rec.get("cues"), meme_id)
            self.assertTrue(Path(rec["image_path"]).exists(), meme_id)
            self.assertTrue(Path(rec["sound_path"]).exists(), meme_id)

    def test_high_density_keeps_a_smaller_gap_than_medium(self):
        high = visual_agent.vfx_amount_profile("high")
        medium = visual_agent.vfx_amount_profile("medium")
        self.assertLess(high["min_gap"], medium["min_gap"])
        self.assertGreater(high["cap"], medium["cap"])


if __name__ == "__main__":
    unittest.main()
