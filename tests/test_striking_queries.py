"""Searching the category returns the category.

Reported 2026-08-30: "die meisten sind low quality clips und uninteressant. ich sagte doch mal
es sollen eher weird clips sein."

Measured on the delivered Tokyo-train run: the chapters searched 朝 通勤 電車 and
通勤ラッシュ 電車 - "morning commute train" - and the pool came back as eleven near-identical
shots of people boarding. Re-scored with the watchability prompt, ten sat between 4 and 7 and
exactly ONE reached 9: a station attendant physically pushing commuters into a carriage.

Ranking alone cannot fix that: it can only order what the search brought home. So each chapter
now also searches for the extreme version of its own subject.
"""

import unittest

import scrape_v3 as v3


class StrikingQueryTests(unittest.TestCase):
    def test_the_subject_term_is_kept_verbatim(self):
        """The topic anchor must still match, or _query_drifted would discard these."""
        got = v3._striking_queries(None, ["通勤ラッシュ 電車"])
        self.assertTrue(all(q.startswith("通勤ラッシュ 電車") for q in got), got)

    def test_a_japanese_subject_gets_japanese_intensifiers(self):
        """That is the language the footage is captioned in."""
        got = v3._striking_queries(None, ["通勤ラッシュ 電車"])
        self.assertTrue(any(w in got[0] for w in v3._STRIKING_JA), got)

    def test_an_english_subject_gets_english_intensifiers(self):
        got = v3._striking_queries(None, ["espresso bar counter"])
        self.assertTrue(any(w in got[0] for w in v3._STRIKING_EN), got)

    def test_the_intensifier_rotates(self):
        """Three searches ending in the same word are one search repeated."""
        got = v3._striking_queries(None, ["a train", "a platform", "a station"])
        tails = [q.rsplit(" ", 1)[-1] for q in got]
        self.assertEqual(len(set(tails)), len(tails), got)

    def test_it_is_bounded(self):
        got = v3._striking_queries(None, ["a", "b", "c", "d", "e"], limit=3)
        self.assertLessEqual(len(got), 3)

    def test_empty_input_is_not_a_crash(self):
        self.assertEqual(v3._striking_queries(None, []), [])
        self.assertEqual(v3._striking_queries(None, None), [])


class WiringTests(unittest.TestCase):
    CHAPTER = dict(chapter_id=2, title="Quiet-carriage manners",
                   subject="Tokyo commuter train passengers",
                   action="ride a packed morning train",
                   evidence="people on a crowded Tokyo train",
                   queries=["通勤ラッシュ 電車", "朝 通勤 電車"])

    def test_they_reach_the_executed_list(self):
        got = v3.chapter_search_queries(v3.Chapter(**self.CHAPTER))
        self.assertTrue(any(any(w in q for w in v3._STRIKING_JA) for q in got), got)

    def test_they_are_not_appended_where_the_budget_cuts_them_off(self):
        """Appended last they would never run. They must sit inside the per-chapter budget."""
        got = v3.chapter_search_queries(v3.Chapter(**self.CHAPTER))
        first_striking = next(i for i, q in enumerate(got)
                              if any(w in q for w in v3._STRIKING_JA))
        self.assertLess(first_striking, v3._queries_per_chapter())

    def test_the_proven_queries_are_still_there(self):
        """Every one of these produced an accepted source on the measured run."""
        got = v3.chapter_search_queries(v3.Chapter(**self.CHAPTER))
        self.assertIn("通勤ラッシュ 電車", got)

    def test_the_retention_hook_is_left_alone(self):
        """Its dance queries are deliberately authored; 'ヤバい' is not wanted there."""
        hook = v3.Chapter(chapter_id=0, title="Cute dance retention hook",
                          subject="a young adult Japanese woman",
                          queries=["日本 女性 ダンス", "可愛い ダンス"], is_hook=True)
        got = v3.chapter_search_queries(hook)
        self.assertFalse(any(any(w in q for w in v3._STRIKING_JA) for q in got), got)


if __name__ == "__main__":
    unittest.main()


class PlatformOrderTests(unittest.TestCase):
    """Adding a second query per chapter must not let Instagram ads jump the queue.

    Measured 2026-08-30: the "tiktok before instagram" guarantee was applied inside each search
    call, so it only survived while a chapter had ONE query. With two, the round-robin over the
    query buckets interleaved them and the pool came back
    ['tiktok','instagram','tiktok','instagram'] where a single query had given four TikToks
    first. The preference is now applied to the whole pool, stably.
    """

    def test_the_preference_is_applied_to_the_whole_pool(self):
        source = open(v3.__file__, encoding="utf-8").read()
        body = source[source.index("def gather_chapter_sources("):]
        body = body[:body.index("\ndef ", 10)]
        after = body[body.index("candidates.append(bucket[offset])"):]
        self.assertIn("candidates.sort(", after,
                      "the pool order is never corrected after the round-robin")
        self.assertIn("_platform_rank", after)

    def test_the_sort_is_stable_so_the_query_rotation_survives(self):
        """An unstable sort would undo the interleaving it is applied on top of."""
        source = open(v3.__file__, encoding="utf-8").read()
        body = source[source.index("def gather_chapter_sources("):]
        body = body[:body.index("\ndef ", 10)]
        self.assertIn("candidates.sort(key=", body)
        self.assertNotIn("candidates = sorted(candidates", body)
