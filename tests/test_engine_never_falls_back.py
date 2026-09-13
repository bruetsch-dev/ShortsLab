"""A project that does not record its engine runs on V4, not on the previous one.

Asked for 2026-09-02: "es soll nich zurückfallen".

`agent_core` read `form["scraping_engine"]` and defaulted a missing value to "v3". Saved presets
and older projects carry no such field, so they quietly ran the previous engine - none of the V4
gates (the measured caption-coverage check, the vision source review, the duplicate policy)
applied, while the UI still presented the run as V4.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent_core
import app


class UnsetMeansV4(unittest.TestCase):

    def setUp(self):
        with open(agent_core.__file__, encoding="utf-8") as handle:
            self.core = handle.read()

    def test_a_missing_engine_resolves_to_v4(self):
        self.assertIn('or "v4").strip().lower()', self.core)
        self.assertNotIn('or "v3").strip().lower()', self.core)

    def test_an_unrecognised_engine_resolves_to_v4(self):
        self.assertIn('if _scrape_engine not in ("v1", "v2", "v3", "v4"):\n'
                      '                _scrape_engine = "v4"', self.core)

    def test_the_legacy_form_default_is_v4(self):
        with open(app.__file__, encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn('state.get("scraping_engine", "v4") == "v4"', source)

    def test_the_legacy_helper_can_express_v4(self):
        """The old segmented control clamped to v2/v3 and would overwrite the v4 default."""
        with open(app.__file__, encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn('eng = (["v2", "v3", "v4"].indexOf(eng) >= 0) ? eng : "v4";', source)


if __name__ == "__main__":
    unittest.main()
