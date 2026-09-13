"""A run may point vision at its own model.

Until this existed a run had ONE model and every vision call silently inherited it. That is the
wrong default for scraping: `grade_sources_v3` sends one frame strip per downloaded source - the
most numerous model call in a Clip Short - and it is a "does this footage show the subject"
judgement, not a planning problem.

Empty must keep meaning "follow the reasoning model", because every saved project and preset
predates the field.
"""

import re
import unittest

import app
import chat_ui
import reasoning_modes


class OverrideTests(unittest.TestCase):
    def tearDown(self):
        reasoning_modes.set_vision_model_override("")

    def test_empty_follows_the_reasoning_model(self):
        reasoning_modes.set_vision_model_override("")
        self.assertEqual(reasoning_modes.vision_model_for("openai/gpt-5.6-luna"),
                         "openai/gpt-5.6-luna")

    def test_an_explicit_model_wins(self):
        reasoning_modes.set_vision_model_override("google/gemini-3.1-flash-lite")
        self.assertEqual(reasoning_modes.vision_model_for("openai/gpt-5.6-luna"),
                         "google/gemini-3.1-flash-lite")

    def test_a_text_only_override_is_refused(self):
        """Naming a blind model for vision would break every strip; fall back instead."""
        reasoning_modes.set_vision_model_override("deepseek/deepseek-v4-pro")
        self.assertIsNone(reasoning_modes.vision_model_override())

    def test_the_text_only_fallback_still_applies(self):
        reasoning_modes.set_vision_model_override("")
        self.assertEqual(reasoning_modes.vision_model_for("deepseek/deepseek-v4-pro"),
                         reasoning_modes.VISION_FALLBACK_MODEL)


class WiringTests(unittest.TestCase):
    def test_the_form_offers_the_field(self):
        html = app.form_page(clear=False).decode("utf-8", "replace")
        block = re.search(r'<select name="vision_model">(.*?)</select>', html, re.S)
        self.assertIsNotNone(block, "the vision model select is missing from the form")
        values = re.findall(r'value="([^"]*)"', block.group(1))
        self.assertIn("", values, "there must be a 'same as reasoning model' choice")
        self.assertIn("google/gemini-3.1-flash-lite", values)

    def test_the_chat_shell_posts_it(self):
        """The chat UI is the default shell; a field it never sends does not exist."""
        self.assertIn("vision_model", chat_ui.RUN_MANIFEST["text"])

    def test_it_survives_a_restart(self):
        """WHICH model is the default is a product choice (it was "" - follow the reasoning
        model - and is now a named one). What must hold either way: the default is a model a run
        can actually use, never a retired or text-only one."""
        self.assertIn("vision_model", app.UI_TEXT_DEFAULTS)
        default = app.UI_TEXT_DEFAULTS["vision_model"]
        if default:
            self.assertIn(default, app.SELECTABLE_REASONING_MODELS, default)
            self.assertNotIn(default, app.RETIRED_REASONING_MODELS, default)
            self.assertTrue(reasoning_modes.is_vision_capable(default), default)

    def test_starting_a_run_applies_it(self):
        source = open(app.__file__, encoding="utf-8").read()
        body = source[source.index("def start_job(fields, files):"):]
        body = body[:body.index("\ndef ", 10)]
        self.assertIn("set_vision_model_override", body)


if __name__ == "__main__":
    unittest.main()
