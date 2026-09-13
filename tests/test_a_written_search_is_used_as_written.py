"""Every stage between a beat and its search was allowed to shorten it, and each one did.

Measured on the roundabouts run (2026-09-09). Sixteen beats, sixteen contracts naming a filmable
thing, and these are the phrases that were actually searched:

    never noticed almost roundabouts / while they everywhere europe / more them after /
    relies traffic lights even / one europe most normal / dedicated legal framework

Three stages produced that:

    _intent()       collected words from the brief and the narration, sentence order, cap 8
    _queries()      took terms[:3] and terms[:4] - and a contract sentence LEADS with the camera
                    ("Aerial drone shot descending over a complex multi-way Japanese
                    intersection"), so the prefix is the camera half
    _tidy_phrase()  refused any phrase carrying an -ing word or a word in _ACTION_VERBS, then cut
                    what survived to FIVE words

The third one is why fixing the first two would have changed nothing:

    'customer handing greasy wrappers back shopkeeper'  -> ''
    'blacked-out dead traffic lights power outage cars' -> 'blacked-out dead traffic lights power'

So "power outage" could not survive, and no phrase naming an action could survive at all.

What replaces it: the beat writes its own searches (`search_queries`), the contract sentence is
kept as a motif with the camera clause removed, and both go through a cleaner that shortens
nothing and deletes no verb. Qualifiers that no clip can show - a year, a cause, a legal status -
go in `search_context`, away from the visible-evidence contract.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent_core
import scrape_v4

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class AWrittenSearchIsNotShortened(unittest.TestCase):
    def setUp(self):
        self.scene = {
            "required_action": "The customer hands the empty wooden skewer back to the vendor.",
            "search_queries": ["customer returning empty skewer to vendor",
                               "Japan street food stall handing skewer back"],
            "exact_voice_text": "customers quietly hand empty wooden sticks back",
        }
        self.out = scrape_v4._queries(self.scene, "Japan")

    def test_the_phrase_arrives_word_for_word(self):
        self.assertIn("customer returning empty skewer to vendor", self.out)

    def test_a_phrase_naming_an_action_is_not_thrown_away(self):
        """The old filter returned "" for anything carrying an -ing word."""
        self.assertIn("Japan street food stall handing skewer back", self.out)

    def test_and_the_contract_is_kept_as_its_own_variant(self):
        self.assertIn("customer hands empty wooden skewer back to vendor", self.out)


class TheMotifKeepsWhatTheShotIs(unittest.TestCase):
    def test_the_camera_clause_goes_and_the_subject_stays(self):
        motif = scrape_v4.motif_phrase(
            "Aerial drone shot descending over a complex multi-way Japanese intersection "
            "with only signals.")
        self.assertNotIn("drone", motif.lower())
        self.assertIn("Japanese intersection", motif)

    def test_a_six_word_term_is_not_cut_in_half(self):
        """"power outage" sat past the five-word cut and was lost every time."""
        motif = scrape_v4.motif_phrase(
            "Blacked-out dead traffic lights during power outage with confused stopped cars.")
        cleaned = scrape_v4._tidy_search_phrase(motif)
        self.assertIn("traffic lights", cleaned)
        self.assertIn("power outage", cleaned)

    def test_removing_a_manner_word_does_not_strand_its_conjunction(self):
        """"operating smoothly and continuously without…" -> "operating and without…"."""
        motif = scrape_v4.motif_phrase(
            "Roundabout operating smoothly and continuously without needing any electricity.")
        self.assertNotIn(" and ", f" {motif} ")
        self.assertIn("without needing any electricity", motif)

    def test_camera_language_goes_and_the_query_survives(self):
        """This asserted that the whole phrase was refused. Refusing a phrase for a word is what
        deleted "Japan train staff pushing passengers"; the camera clause comes out instead and
        what was being filmed stays."""
        self.assertEqual("junction",
                         scrape_v4._tidy_search_phrase("drone descending over the junction"))

    def test_a_phrase_that_is_only_camera_leaves_nothing(self):
        self.assertEqual("", scrape_v4._tidy_search_phrase("wide cinematic drone pullback"))


class TheBeatIsAskedForItsOwnSearches(unittest.TestCase):
    def setUp(self):
        self.src = open(os.path.join(ROOT, "agent_core.py"), encoding="utf-8").read()

    def test_the_planner_is_told_to_write_them(self):
        self.assertIn("search_queries: 2-3 short phrases", self.src)
        self.assertIn("No camera words", self.src)

    def test_and_to_keep_the_unfilmable_qualifiers_apart(self):
        """A year or a cause cannot be read off a clip; asking footage to prove one fails it for
        something no footage could show."""
        self.assertIn("search_context: qualifiers that narrow the search but CANNOT be seen",
                      self.src)
        self.assertIn("Never put these ", self.src)
        self.assertIn("in required_action or required_result.", self.src)

    def test_both_fields_are_in_the_schema_line(self):
        self.assertIn("required_result,search_queries,search_context,exact_voice_text", self.src)

    def test_the_plan_carries_them_out_of_normalisation(self):
        self.assertIn('"search_queries": [str(q).strip() for q in', self.src)

    def test_and_plan_config_does_not_drop_them(self):
        """plan_config's scene dict is a whitelist - this is how seedance_start_trim went
        missing and left the caption pass reading second 0 of every source."""
        block = self.src[self.src.index('"required_result": scene.get("required_result", ""),'):][:900]
        self.assertIn('"search_queries": scene.get("search_queries") or []', block)
        self.assertIn('"search_context": scene.get("search_context", "")', block)


if __name__ == "__main__":
    unittest.main()
