"""A word is not camera language. A PHRASE is.

The cleaner that keeps a written search whole refused any phrase containing one of
`_CAMERA_PARTICIPLE` - a word list that held "pushing", "cutting", "moving", "flying",
"circling". Those name what happens in the picture far more often than they name the camera, so
the cleaner threw away exactly the searches it exists to protect. Measured against it:

    'Japan train staff pushing passengers'       -> ''
    'chef cutting fish at market'                -> ''
    'cars moving through Japanese roundabout'    -> ''
    'customer returning empty skewer to vendor'  -> kept

That is the third time a filter here has been written as "reject anything containing X": the
strict `_tidy_phrase` rejects every -ing word, the first prototype of this one used a positive
list of meaningful verbs, and this one used a negative list. All three delete real searches.

Camera language is only ever camera language as a connected expression - "camera panning over",
"drone flying over", "wide shot of". `_CAMERA_CLAUSE` already matches those as spans, and taking
the span out leaves whatever was actually being filmed. No word is refused on its own.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scrape_v4


class AnActionVerbSurvives(unittest.TestCase):
    """Every one of these came back empty from the word list."""

    def test_pushing(self):
        self.assertEqual("Japan train staff pushing passengers",
                         scrape_v4._tidy_search_phrase("Japan train staff pushing passengers"))

    def test_cutting(self):
        self.assertEqual("chef cutting fish at market",
                         scrape_v4._tidy_search_phrase("chef cutting fish at market"))

    def test_moving(self):
        self.assertEqual("cars moving through Japanese roundabout",
                         scrape_v4._tidy_search_phrase("cars moving through Japanese roundabout"))

    def test_returning_still_survives(self):
        self.assertEqual("customer returning empty skewer to vendor",
                         scrape_v4._tidy_search_phrase(
                             "customer returning empty skewer to vendor"))

    def test_circling_and_flying_are_shots_too(self):
        self.assertEqual("seagulls circling the harbour wall",
                         scrape_v4._tidy_search_phrase("seagulls circling the harbour wall"))


class CameraLanguageGoesAsAPhrase(unittest.TestCase):
    def test_the_camera_clause_is_removed_not_the_query(self):
        """"camera panning over a junction" is a junction, filmed a particular way."""
        self.assertNotIn("panning", scrape_v4._tidy_search_phrase(
            "camera panning over a busy Tokyo junction").lower())
        self.assertIn("Tokyo junction", scrape_v4._tidy_search_phrase(
            "camera panning over a busy Tokyo junction"))

    def test_a_drone_shot_keeps_its_subject(self):
        self.assertIn("Osaka rooftops", scrape_v4._tidy_search_phrase(
            "drone flying over Osaka rooftops"))

    def test_what_is_left_of_a_pure_camera_phrase_is_nothing(self):
        self.assertEqual("", scrape_v4._tidy_search_phrase("wide cinematic drone pullback"))


class TheContextActuallyNarrowsTheSearch(unittest.TestCase):
    """`search_context` was being stored and never read. Separating the unfilmable qualifiers
    from the visible contract is only half of it; they still have to reach a search."""

    def test_a_context_produces_its_own_narrowed_variant(self):
        scene = {"required_action": "Road construction workers building a new roundabout layout.",
                 "search_queries": ["Japanese roundabout under construction"],
                 "search_context": "post-2011 reconstruction zone"}
        out = scrape_v4._queries(scene, "Japan")
        self.assertTrue(any("post-2011" in q for q in out),
                        f"the context never reached a search: {out}")

    def test_the_unnarrowed_query_is_still_there(self):
        scene = {"required_action": "Road construction workers building a new roundabout layout.",
                 "search_queries": ["Japanese roundabout under construction"],
                 "search_context": "post-2011 reconstruction zone"}
        out = scrape_v4._queries(scene, "Japan")
        self.assertIn("Japanese roundabout under construction", out)

    def test_no_context_changes_nothing(self):
        scene = {"required_action": "Road construction workers building a new roundabout layout.",
                 "search_queries": ["Japanese roundabout under construction"]}
        self.assertIn("Japanese roundabout under construction",
                      scrape_v4._queries(scene, "Japan"))


if __name__ == "__main__":
    unittest.main()
