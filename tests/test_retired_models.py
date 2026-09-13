"""Sonnet 5 and both DeepSeek models are retired and must be unreachable.

Retired 2026-08-30 on measured grounds:
  - Claude Sonnet 5 returned "Script creator returned no usable script" for every topic tried,
    while Luna and Gemini 3.1 Pro both produced a usable script from the same prompt.
  - Both DeepSeek models are in TEXT_ONLY_MODELS, so they cannot look at a frame strip; every
    vision call already fell back to another model behind the user's back.

Removing the <option> is not enough on its own: every project, preset and ui_state.json on disk
still names whatever was chosen when it was saved, and that value is posted back on the next run.
"""

import re
import unittest

import app


class SelectionTests(unittest.TestCase):
    def test_no_form_offers_a_retired_model(self):
        html = app.form_page(clear=False).decode("utf-8", "replace")
        for model in app.RETIRED_REASONING_MODELS:
            self.assertNotIn(f'<option value="{model}"', html, model)

    def test_the_retired_and_selectable_lists_do_not_overlap(self):
        self.assertFalse(app.RETIRED_REASONING_MODELS & app.SELECTABLE_REASONING_MODELS)

    def test_the_default_is_selectable(self):
        self.assertIn(app.DEFAULT_REASONING_MODEL, app.SELECTABLE_REASONING_MODELS)

    def test_every_selectable_model_is_still_offered(self):
        """The retirement must not have taken a working model with it."""
        html = app.form_page(clear=False).decode("utf-8", "replace")
        offered = set(re.findall(r'<option value="([^"]+)"', html))
        for model in app.SELECTABLE_REASONING_MODELS:
            self.assertIn(model, offered, model)


class MigrationTests(unittest.TestCase):
    def test_a_saved_retired_model_is_swapped_out(self):
        for model in app.RETIRED_REASONING_MODELS:
            self.assertEqual(app.retire_model(model), app.DEFAULT_REASONING_MODEL, model)

    def test_a_working_model_is_untouched(self):
        self.assertEqual(app.retire_model("openai/gpt-5.6-luna"), "openai/gpt-5.6-luna")

    def test_the_vision_field_falls_back_to_empty_not_to_a_model(self):
        """Empty means 'follow the reasoning model'; forcing a model there would be a new
        choice the user never made."""
        self.assertEqual(app.retire_model("deepseek/deepseek-v4-pro", default=""), "")

    def test_the_run_applies_it(self):
        source = open(app.__file__, encoding="utf-8").read()
        body = source[source.index("def start_job(fields, files):"):]
        body = body[:body.index("\ndef ", 10)]
        self.assertIn('retire_model(fields.get("reasoning_model"))', body)
        self.assertIn('retire_model(fields.get("vision_model")', body)

    def test_the_allowlist_is_shared_not_copied(self):
        """A second hand-written list is how the form and the runner drift apart."""
        source = open(app.__file__, encoding="utf-8").read()
        self.assertIn("if reasoning_model not in SELECTABLE_REASONING_MODELS:", source)


if __name__ == "__main__":
    unittest.main()
