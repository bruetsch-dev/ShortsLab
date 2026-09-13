"""A project name belongs to ONE script and must never outlive it.

`start_job` treats a supplied `title` as final - it skips the naming model completely. So any
title that survives in the saved UI state can rename an unrelated later run. Measured
2026-08-29: a script about silence on Tokyo trains was written into a folder named
`why_some_japanese_wives_wake_up_early_for_bento_20260829_201218`, because a previous run's
title was still in `ui_state.json` and was posted back.

The browser form has no title input, so it never posted one - but nothing in the code said so.
"""

import unittest

import agent_core
import app


class TitleIsNotPersistedTests(unittest.TestCase):
    def test_the_saved_state_never_carries_a_title(self):
        self.assertIn("title", app.UI_PERSIST_SKIP_FIELDS)

    def test_a_stale_title_is_dropped_from_a_submitted_form(self):
        state = app.ui_state_from_form_fields({
            "title": "Why Some Japanese Wives Wake Up Early For Bento",
            "script": "In Tokyo, friends sometimes save every word for the platform.",
        })
        self.assertNotIn("title", {k: v for k, v in state.items() if v}, )

    def test_the_fallback_still_names_this_script(self):
        """With no stored title the run must fall back to THIS script, not to nothing."""
        title = agent_core.derive_project_title_from_script(
            "In Tokyo, friends sometimes save every word for the platform.")
        self.assertTrue(title)
        self.assertIn("tokyo", title.casefold())
        self.assertNotIn("bento", title.casefold())


if __name__ == "__main__":
    unittest.main()
