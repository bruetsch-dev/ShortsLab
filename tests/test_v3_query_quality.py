"""Search queries must be search phrases, not chopped-up prose.

Measured on a delivered Clip Short (`japan_has_hotels_where_you_can_stay`): 70 sources
downloaded, 61 rejected, and the largest single reason - 32 of them - was "visually unrelated to
the chapter". With only nine survivors for fourteen scenes, the selector had to reuse footage:
six distinct source videos carried nine scenes, one TikTok three of them. So the duplicates the
user saw and the "it never finds the exact clip" complaint are the same failure, one step apart.

Fourteen of the seventy-one executed queries were sentences cut at a fixed word count -
"A guest soaking in a", "Steam, sauna benches, or a", "A hand plugging a phone". Every query
that worked was a bare noun phrase ("capsule hotel pod bed TV", "カプセルホテル 大浴場").
"""

import unittest

import scrape_v3


def clean(query):
    out = scrape_v3.sanitize_queries([query], limit=1)
    return out[0] if out else None


class DanglingTailTests(unittest.TestCase):
    def test_a_query_never_ends_on_a_function_word(self):
        """Whatever survives must be a phrase, not a cut-off clause. Queries built around an
        action ("pulling down", "walks from") are now discarded outright - see
        test_query_shape_v3, measured on the Tokyo-train run - so only the rest is checked
        here."""
        for query in ("Japan has hotels where you", "Japanese capsule hotel pod to"):
            got = clean(query)
            self.assertIsNotNone(got, query)
            self.assertNotIn(got.split()[-1].casefold().strip(",."),
                             scrape_v3._DANGLING_TAIL_WORDS, f"{query!r} -> {got!r}")

    def test_an_action_fragment_is_discarded_rather_than_trimmed(self):
        """"guest soaking" and "guest pulling" were the salvaged remainders of prose. The later
        run measured that shape returning only rejects, so a wasted slot beats a bad search."""
        for query in ("A guest pulling down the", "capsule hotel guest walks from"):
            self.assertIsNone(clean(query), query)

    def test_a_leading_article_is_dropped(self):
        """It marks a description. Search for the subject instead."""
        self.assertEqual(clean("The sauna benches"), "sauna benches")
        self.assertEqual(clean("A capsule hotel corridor"), "capsule hotel corridor")

    def test_nothing_but_function_words_is_discarded(self):
        for query in ("a", "the and of", "in the"):
            self.assertIsNone(clean(query), query)

    def test_one_content_word_is_not_a_search(self):
        self.assertIsNone(clean("of the a"))


class GoodQueriesSurviveTests(unittest.TestCase):
    """The repair must not touch the queries that were already working."""

    WORKING = [
        "カプセルホテル 大浴場", "カプセルホテル 泊まる", "通勤ラッシュ 電車",
        "capsule hotel pod bed TV", "Japanese capsule hotel sauna user",
        "cheap capsule hotel pod Japan", "Japanese dance vlog",
        "missed last train Japan capsule",
    ]

    def test_they_pass_through_unchanged(self):
        for query in self.WORKING:
            self.assertEqual(clean(query), query, query)

    def test_japanese_is_never_word_trimmed_as_english(self):
        """CJK has no spaces to dangle on; the English rules must not touch it."""
        self.assertEqual(clean("カプセルホテル 館内ツアー"), "カプセルホテル 館内ツアー")


class SourceBudgetTests(unittest.TestCase):
    """One upload may not carry a third of the finished Short."""

    def test_there_is_a_whole_short_budget(self):
        self.assertIn("max_windows_per_source_total", scrape_v3.V3_CONFIG)
        self.assertLessEqual(scrape_v3.V3_CONFIG["max_windows_per_source_total"],
                             scrape_v3.V3_CONFIG["max_windows_per_source"])

    def test_the_budget_is_carried_between_chapters(self):
        source = open(scrape_v3.__file__, encoding="utf-8").read()
        body = source[source.index("def select_chapter_windows("):]
        body = body[:body.index("\ndef ", 10)]
        self.assertIn('state.setdefault("source_spend"', body)
        self.assertIn("ignore_global_budget", body)

    def test_the_budget_yields_to_an_uncovered_beat(self):
        """A repeat is bad; a hole is worse. The relaxation must exist and be last."""
        source = open(scrape_v3.__file__, encoding="utf-8").read()
        body = source[source.index("def select_chapter_windows("):]
        body = body[:body.index("\ndef ", 10)]
        ladder = body[body.index("chosen, skipped = pick(across_sources=True)"):]
        ladder = ladder[:ladder.index("captioned = sum(")]
        self.assertIn("(False, True, True)", ladder)
        self.assertLess(ladder.index("(False, False, False)"), ladder.index("(False, True, True)"))

    def spread(self, sources, chapters=3, scenes=3):
        """Run the real selector and count what the chapters would SHOW."""
        from collections import Counter
        graded = [{"source_id": "src%d" % i, "platform": "tiktok", "path": "/tmp/%d.mp4" % i,
                   "windows": [{"start": t * 6.0, "end": t * 6.0 + 4.0, "match_class": "exact",
                                "reason": "shot %d-%d" % (i, t),
                                "visual_signature": "sig%d%d" % (i, t)}
                               for t in range(3)]} for i in range(sources)]
        state, shown = {}, []
        for cid in range(1, chapters + 1):
            chapter = scrape_v3.Chapter(chapter_id=cid, title="t", scene_ids=["s"] * scenes)
            chosen = scrape_v3.select_chapter_windows(chapter, graded, state=state)
            shown += [w.source_id for w in chosen[:scenes]]
        return Counter(shown)

    def test_with_enough_material_no_source_carries_more_than_the_cap(self):
        """The reported symptom: one upload carried three of nine scenes."""
        cap = scrape_v3.V3_CONFIG["max_windows_per_source_total"]
        for sources in (6, 12):
            spread = self.spread(sources)
            self.assertLessEqual(max(spread.values()), cap,
                                 "%d sources -> %r" % (sources, dict(spread)))

    def test_a_starved_pool_still_fills_every_beat(self):
        """Three sources for nine shots: repeats are unavoidable and a hole would be worse.
        This is why the duplicates are a SYMPTOM - the cure is more surviving footage."""
        spread = self.spread(3)
        self.assertEqual(sum(spread.values()), 9)
        self.assertGreater(max(spread.values()),
                           scrape_v3.V3_CONFIG["max_windows_per_source_total"])

    def test_a_chapter_is_only_charged_for_what_it_shows(self):
        """Charging for every window it collected emptied every allowance in chapter one, and
        the cap was then useless from chapter two on (measured: 8x with twelve sources)."""
        source = open(scrape_v3.__file__, encoding="utf-8").read()
        body = source[source.index("def select_chapter_windows("):]
        body = body[:body.index("\ndef ", 10)]
        self.assertIn("for window in chosen[:max(1, needed)]:", body)


if __name__ == "__main__":
    unittest.main()
