"""V4 search phrases must retrieve footage, and round two must be a different search.

Measured, not assumed:

* On a delivered V3 Clip Short every query that produced an accepted source was a bare noun
  phrase ("Tokyo train carriage courtesy campaign", 通勤ラッシュ 電車); every query that produced
  only rejects narrated an action ("Passengers squeezing aboard during morning") or named a
  camera move ("close shot"). Six of the eight winners were Japanese.
* Both hand-built Shorts that reached a complete edit searched in Japanese only
  (`日本 高校生 掃除 教室`, `東京 終電 逃した`). V4 emitted no native phrase at all.
* Scrape.do returns ~12 URLs per query whatever the scroll depth (measured 2026-09-02), so
  coverage comes from DIFFERENT hypotheses - asking the same thing again buys the same twelve.

What V4 did before: `_queries` produced "a hand buying a hot can", and the recovery round handed
back the first round's phrases with "Japan " glued on the front.
"""

import unittest

import scrape_v4 as v4


SCENE = {
    "voice_line": "Japan has vending machines that sell hot corn soup in winter.",
    "visual_subject": "a Japanese street vending machine",
    "visual_action": "a hand buying a hot can",
    "required_visual_information": "a lit vending machine at night",
}
NATIVE = ["日本 自販機 ホット", "冬 自販機 あったかい", "自販機 缶 コーンスープ"]


class PhraseShapeTests(unittest.TestCase):
    def test_a_narrated_action_is_not_a_search_phrase(self):
        for text in ("a hand buying a hot can", "a warm can dropping into the tray",
                     "she buys a can", "man walks to the machine", "POV follows the buyer"):
            self.assertEqual(v4._tidy_phrase(text), "", text)

    def test_a_camera_word_is_not_a_search_phrase(self):
        for text in ("close shot", "wide shot", "b-roll"):
            self.assertEqual(v4._tidy_phrase(text), "", text)

    def test_every_measured_winner_survives(self):
        """Any filter that kills these makes the run strictly worse."""
        for text in ("通勤ラッシュ 電車", "日本 高校生 掃除 教室", "カプセルホテル 大浴場",
                     "Tokyo train carriage courtesy campaign", "capsule hotel pod bed TV",
                     "street vending machine"):
            self.assertEqual(v4._tidy_phrase(text), text, text)

    def test_a_leading_article_is_dropped_not_searched(self):
        self.assertEqual(v4._tidy_phrase("a Japanese street vending machine"),
                         "Japanese street vending machine")

    def test_japanese_is_never_word_trimmed_as_english(self):
        """CJK has no articles to strip and no participles to detect."""
        self.assertEqual(v4._tidy_phrase("自販機 夜 街角"), "自販機 夜 街角")

    def test_a_single_word_is_not_a_search(self):
        self.assertEqual(v4._tidy_phrase("machine"), "")

    def test_a_platform_name_never_reaches_a_search(self):
        self.assertNotIn("tiktok", v4._tidy_phrase("tiktok vending machine").casefold())


class RoundOneTests(unittest.TestCase):
    def test_native_phrases_come_first_when_the_project_has_them(self):
        got = v4._queries(SCENE, "Japanese vending machines", native_terms=NATIVE)
        self.assertTrue(got)
        self.assertEqual(got[:3], NATIVE)

    def test_it_still_works_without_native_terms(self):
        got = v4._queries(SCENE, "Japanese vending machines")
        self.assertTrue(got)
        for phrase in got:
            self.assertEqual(v4._tidy_phrase(phrase), phrase, phrase)

    def test_no_phrase_narrates_an_action(self):
        for phrase in v4._queries(SCENE, "Japanese vending machines", native_terms=NATIVE):
            self.assertNotIn("buying", phrase)
            self.assertNotIn("dropping", phrase)


class RoundTwoTests(unittest.TestCase):
    """The point of a second round is a different search, not the same one paid for twice."""

    def test_recovery_shares_nothing_with_the_first_round(self):
        first = v4._queries(SCENE, "Japanese vending machines", native_terms=NATIVE)
        second = v4._recovery_queries(SCENE, "Japanese vending machines", used=first,
                                      native_terms=["自販機 夜 街角"])
        self.assertTrue(second)
        self.assertFalse({x.casefold() for x in second} & {x.casefold() for x in first})

    def test_recovery_is_not_the_first_round_with_a_prefix(self):
        """The exact failure this replaces: "a hand buying a hot can" came back as
        "Japan a hand buying a hot can"."""
        first = v4._queries(SCENE, "Japanese vending machines", native_terms=NATIVE)
        second = v4._recovery_queries(SCENE, "Japanese vending machines", used=first,
                                      native_terms=[])
        for phrase in second:
            for earlier in first:
                self.assertNotIn(earlier.casefold(), phrase.casefold(),
                                 f"{phrase!r} is just {earlier!r} with something glued on")

    def test_unspent_native_terms_lead_the_recovery(self):
        second = v4._recovery_queries(SCENE, "x", used=[], native_terms=["自販機 夜 街角"])
        self.assertEqual(second[0], "自販機 夜 街角")

    def test_every_recovery_phrase_is_itself_well_shaped(self):
        second = v4._recovery_queries(SCENE, "x", used=[], native_terms=[])
        for phrase in second:
            self.assertEqual(v4._tidy_phrase(phrase), phrase, phrase)


class NativeAgentTests(unittest.TestCase):
    def test_a_supplied_list_skips_the_call(self):
        got = v4._native_terms_agent({"v4_native_terms": [["日本 自販機"], []]}, [{}, {}])
        self.assertEqual(got, [["日本 自販機"], []])

    def test_no_api_key_means_no_crash(self):
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {k: v for k, v in os.environ.items()
                                          if k != "WAVESPEED_API_KEY"}, clear=True):
            self.assertEqual(v4._native_terms_agent({}, [SCENE]), [])

    def test_the_prompt_asks_for_captions_not_sentences(self):
        prompt = v4.NATIVE_TERMS_PROMPT
        self.assertIn("local language", prompt)
        self.assertIn("caption, never a sentence", prompt)
        self.assertIn("通勤ラッシュ 電車", prompt)
        self.assertIn("Never write a platform name", prompt)


class WiringTests(unittest.TestCase):
    def source(self):
        from pathlib import Path
        return Path(v4.__file__).read_text(encoding="utf-8")

    def test_the_run_asks_for_native_terms_before_building_queries(self):
        body = self.source()
        self.assertIn("_native_terms_agent(config or {}, scenes", body)
        self.assertLess(body.index("_native_terms_agent(config or {}"),
                        body.index("_queries(scene, str(config.get(\"title\") or \"\"),"))

    def test_the_recovery_round_builds_new_angles(self):
        body = self.source()
        block = body[body.index("recovery_wanted = []"):]
        block = block[:block.index("if recovery_wanted:")]
        self.assertIn("_recovery_queries(", block)


if __name__ == "__main__":
    unittest.main()


class PrefixDuplicateQueries(unittest.TestCase):
    """A rendered search costs the same whatever it returns, so asking twice is paying twice.

    Measured on a live run 2026-09-02: "outsiders expect hot" and "outsiders expect hot coffee"
    were both sent, at five credits each, and both came back with zero posts.
    """

    def test_a_prefix_is_replaced_by_the_longer_wording(self):
        self.assertEqual(v4._dedupe(["outsiders expect hot",
                                            "outsiders expect hot coffee"]),
                         ["outsiders expect hot coffee"])

    def test_order_does_not_decide_which_survives(self):
        self.assertEqual(v4._dedupe(["outsiders expect hot coffee",
                                            "outsiders expect hot"]),
                         ["outsiders expect hot coffee"])

    def test_a_prefix_must_end_on_a_word_boundary(self):
        """"hot can" is a different search from "hot cans of soup", not a shorter one."""
        self.assertEqual(v4._dedupe(["hot can", "hot cans of soup"]),
                         ["hot can", "hot cans of soup"])

    def test_the_limit_still_holds(self):
        self.assertEqual(v4._dedupe(["a", "b", "c"], limit=2), ["a", "b"])
