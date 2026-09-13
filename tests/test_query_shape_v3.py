"""Search queries must NAME a subject, not narrate a shot - measured, not assumed.

Ground truth is the delivered Tokyo-train Clip Short
(`review/scrape_v3_report.json`, 2026-08-29): 87 sources downloaded, 12 accepted, and chapter 3
accepted NOTHING from 20 downloads. Splitting every executed query by outcome:

  produced an accepted source : 通勤ラッシュ 電車 / 東京メトロ 車内広告 マナー 会話 /
                                駅ホーム 通勤 / "Japanese young adult dance" /
                                "Tokyo train carriage courtesy campaign"
  produced only rejects       : "Passengers squeezing aboard during morning" /
                                "Riders holding straps and looking" /
                                "commuters leave carriage and gesture" /
                                "close shot" / "POV follows friends from train"

Both groups contain long queries, so length is not the discriminator. Every winner is a bare
noun phrase; every loser narrates an action or names a camera move. A caption names the thing.
"""

import unittest

import scrape_v3 as v3


def clean(query):
    out = v3.sanitize_queries([query], limit=1)
    return out[0] if out else None


class WinnersSurviveTests(unittest.TestCase):
    """Any filter that kills these makes the run strictly worse."""

    WINNERS = ["通勤ラッシュ 電車", "東京メトロ 車内広告 マナー 会話", "電車 車内広告 撮影",
               "駅ホーム 通勤", "可愛い ダンス", "日本 女性 ダンス",
               "Japanese young adult dance", "Tokyo train carriage courtesy campaign"]

    def test_every_query_that_worked_still_passes(self):
        for query in self.WINNERS:
            self.assertEqual(clean(query), query, query)


class ShotDescriptionsAreRejectedTests(unittest.TestCase):
    LOSERS = ["Passengers squeezing aboard during morning",
              "Riders holding straps and looking",
              "commuters leave carriage and gesture",
              "Passengers exiting and clearing",
              "doors open those friends step",
              "friends turn toward each other",
              "train platform friends walk away"]

    def test_narrated_actions_never_reach_a_search(self):
        for query in self.LOSERS:
            self.assertIsNone(clean(query), query)

    def test_a_noun_that_merely_ends_in_ing_is_not_an_action(self):
        for query in ("Tokyo morning train", "Japanese vending machine",
                      "Shibuya crossing crowd", "office building lobby"):
            self.assertIsNotNone(clean(query), query)


class CameraWordsTests(unittest.TestCase):
    def test_a_pure_camera_direction_is_discarded(self):
        self.assertIsNone(clean("close shot"))

    def test_the_subject_is_kept_when_the_framing_is_stripped(self):
        """'filmed from inside train doorway' still names a real thing once cleaned."""
        self.assertEqual(clean("filmed from inside train doorway"), "train doorway")

    def test_a_query_never_starts_on_a_preposition(self):
        got = clean("from inside train doorway")
        self.assertIsNotNone(got)
        self.assertNotIn(got.split()[0].casefold(), {"from", "inside"})


class RepeatedWordTests(unittest.TestCase):
    def test_the_planner_repeating_its_subject_is_collapsed(self):
        self.assertEqual(clean("Friends shoulder-to-shoulder rush hour Friends"),
                         "Friends shoulder-to-shoulder rush hour")


class DriftTests(unittest.TestCase):
    """A retry may change the hypothesis; it may not change the subject."""

    CHAPTER = v3.Chapter(chapter_id=3, title="Conversation resumes on the platform",
                         subject="Friends stepping from a Tokyo train onto the station platform",
                         action="the friends exit, turn toward each other on the platform",
                         evidence="the friends exiting onto a station platform")
    ATTEMPTED = ["電車 降りて 友達 話す", "駅ホーム 友人 雑談", "Tokyo commuter train passenger vlog"]

    def anchors(self):
        return v3._topic_anchors(self.CHAPTER, self.ATTEMPTED)

    def test_the_measured_drift_is_caught(self):
        """Six downloads went to cafes and house moves for a chapter about a train."""
        for query in ("引っ越し 挨拶", "引っ越し 手土産", "カフェ デート", "カップル カフェ",
                      "Japanese cafe date"):
            self.assertTrue(v3._query_drifted(query, self.anchors()), query)

    def test_on_topic_retries_are_untouched(self):
        for query in ("電車ドア開く 降車 ジェスチャー", "満員電車 つり革", "駅ホーム 通勤",
                      "Tokyo train carriage courtesy campaign", "通勤ラッシュ 電車"):
            self.assertFalse(v3._query_drifted(query, self.anchors()), query)

    def test_without_anchors_nothing_is_blocked(self):
        """A chapter with no brief and no history must not lose every query."""
        self.assertFalse(v3._query_drifted("anything at all", {"english": set(), "cjk": set()}))

    def test_the_guard_never_empties_the_round(self):
        """Dropping every candidate would starve the chapter - a drifting search beats none."""
        source = open(v3.__file__, encoding="utf-8").read()
        body = source[source.index("def adaptive_search_queries("):]
        body = body[:body.index("\ndef ", 10)]
        self.assertIn("if kept:", body)


if __name__ == "__main__":
    unittest.main()
