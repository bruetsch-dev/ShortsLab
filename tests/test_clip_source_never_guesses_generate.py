"""A Clip Short must never fall back to generating clips with a video model.

Reported, furiously and correctly: a Clip Short generated its footage with a video model.
`why_japanese_wives_call_their_husbands_dad_20260828_185444` is the evidence - its saved
run_form holds `search_terms`, `script_relevancy: 82`, `scrape_rounds: 5`, a 7200s scrape budget
and `scrape_version: v3`, and its config came out as `clip_source: generate`. Every clip in it
was generated.

The cause is a default, not a decision: `form.get("clip_source", "generate")`. Whatever built
that form used field names this codebase does not read (`scrape_version`, `search_terms`,
`tts_provider`) and omitted `clip_source` entirely, so the most expensive possible behaviour was
chosen in silence. Generating is a fine default for a form that says nothing at all; it is the
wrong one for a form that is visibly a scrape run.
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AGENT_CORE = (ROOT / "agent_core.py").read_text(encoding="utf-8")


def decision_block():
    """The lines that settle clip_source in build_project_config."""
    start = AGENT_CORE.index("# ===== Clip source: generate (AI) vs scrape")
    return AGENT_CORE[start:start + 1800]


class InferenceTests(unittest.TestCase):
    """The rule, read off the source: scrape markers + no clip_source => scrape."""

    def test_scrape_markers_are_recognised(self):
        block = decision_block()
        for marker in ("search_terms", "scrape_terms", "scrape_rounds",
                       "scrape_time_budget", "script_relevancy", "scrape_version"):
            self.assertIn(marker, block, marker)

    def test_it_only_infers_when_the_field_is_actually_absent(self):
        """An explicit `clip_source: generate` is a choice and must be obeyed - an AI Short
        with a leftover scrape term in its form must still generate."""
        block = decision_block()
        self.assertIn('not str(form.get("clip_source") or "").strip()', block)

    def test_the_inference_is_announced(self):
        """Silence is what made this cost a whole short."""
        block = decision_block()
        self.assertIn("log(status_cb", block)
        self.assertIn("Clip Short", block)

    def test_it_lands_on_scrape_not_on_something_else(self):
        block = decision_block()
        self.assertIn('clip_source = "scrape"', block)


class RegressionEvidenceTests(unittest.TestCase):
    """The shipped project that proves the bug was real, if it is still on this machine."""

    def setUp(self):
        import agent_core
        self.project = (agent_core.PROJECTS_DIR /
                        "why_japanese_wives_call_their_husbands_dad_20260828_185444")
        if not (self.project / "input" / "run_form.json").is_file():
            self.skipTest("the reference project is not on this machine")

    def test_that_form_would_now_be_read_as_a_clip_short(self):
        import json
        form = json.loads((self.project / "input" / "run_form.json").read_text(encoding="utf-8"))
        self.assertEqual(str(form.get("clip_source") or ""), "",
                         "the form really did omit clip_source")
        markers = [k for k in ("search_terms", "scrape_terms", "scrape_platforms",
                               "scrape_rounds", "scrape_time_budget", "scrape_sort",
                               "script_relevancy", "scrape_version", "scraping_engine")
                   if str(form.get(k) or "").strip()]
        self.assertTrue(markers, "this form is visibly a scrape run: " + repr(markers))


class UnknownFieldNameTests(unittest.TestCase):
    """The deeper cause: the form spoke a vocabulary the app does not read."""

    def test_scrape_version_only_ever_acts_as_a_hint(self):
        """No code branches on its VALUE - it is a field name the app never adopted. It is
        listed among the scrape markers purely so a form that carries it is still recognised as
        a Clip Short; recorded here so the next reader does not hunt for a handler."""
        outside = AGENT_CORE.replace(decision_block(), "")
        self.assertNotIn("scrape_version", outside)
        others = [f.name for f in ROOT.glob("*.py") if f.name != "agent_core.py"
                  and "scrape_version" in f.read_text(encoding="utf-8")]
        self.assertEqual(others, [])

    def test_the_field_the_app_actually_reads_is_scraping_engine(self):
        import chat_ui
        self.assertIn("scraping_engine", chat_ui.RUN_MANIFEST["text"])
        self.assertIn("clip_source", chat_ui.RUN_MANIFEST["text"])


if __name__ == "__main__":
    unittest.main()
