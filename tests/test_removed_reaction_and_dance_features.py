import inspect
import unittest
from pathlib import Path

import chat_ui
import visual_agent


ROOT = Path(__file__).resolve().parents[1]


class RemovedFeatureTests(unittest.TestCase):
    def test_removed_controls_are_absent_from_both_shells(self):
        forbidden = (
            "influencer_hook",
            "Cute dance hook",
            "Meme reactions",
            "Neko reactions",
            "add_meme_reactions",
            "add_neko_reactions",
        )
        for name in ("static/shell-v3.js", "static/chat-shell.js", "chat-shell.js"):
            source = (ROOT / name).read_text(encoding="utf-8")
            for token in forbidden:
                self.assertNotIn(token, source, f"{token} remains in {name}")

    def test_removed_fields_are_not_submitted(self):
        self.assertNotIn("influencer_hook", chat_ui.RUN_MANIFEST["check"])
        self.assertNotIn("add_meme_reactions", chat_ui.RUN_MANIFEST["check"])
        self.assertNotIn("add_neko_reactions", chat_ui.RUN_MANIFEST["check"])
        self.assertNotIn("add_characters", chat_ui.MASTER_MANIFESTS["visual"].get("check", []))
        signature = inspect.signature(visual_agent.enhance_video_with_arrows)
        self.assertNotIn("add_characters", signature.parameters)
        self.assertNotIn("add_memes", signature.parameters)

    def test_clip_station_uses_trash_preview(self):
        source = (ROOT / "static/shell-v3.js").read_text(encoding="utf-8")
        self.assertIn('video: "previews/clip_short_trash_20s_hd.mp4"', source)
        self.assertIn('poster: "previews/clip_short_trash_poster.jpg"', source)
        self.assertTrue((ROOT / "static/previews/clip_short_trash_20s_hd.mp4").is_file())
        self.assertTrue((ROOT / "static/previews/clip_short_trash_poster.jpg").is_file())


if __name__ == "__main__":
    unittest.main()
