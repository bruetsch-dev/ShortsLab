"""The retry round must be told WHICH search wasted the slots, not just that something failed.

`adaptive_search_queries` exists so a second round changes tactics instead of rephrasing. It was
handed an aggregate tally - "unrelated_filler x10" - which says something went wrong but not
which hypothesis produced it. On the measured run, three commute queries returned eight unrelated
sources for a chapter about capsule hotels, and nothing in the retry's input identified them, so
it could propose the same shape again.

Now every rejection carries the query that fetched it, and the prompt names the spent hypotheses.
"""

import unittest
from unittest import mock

import scrape_v3 as v3


class RejectionProvenanceTests(unittest.TestCase):
    def test_every_rejection_site_records_its_query(self):
        """A rejection record is the one carrying both `reason` and `detail`; a selected
        window also has a `reason` and is not a rejection."""
        source = open(v3.__file__, encoding="utf-8").read()
        found = 0
        for index in range(len(source)):
            if not source.startswith('"reason": ', index):
                continue
            record = source[index:index + 900]
            end = record.find("}")
            record = record[:end if end > 0 else 900]
            if '"detail"' not in record:
                continue                      # not a rejection record
            found += 1
            self.assertIn('"query"', record,
                          "a rejection that cannot be attributed: " + record[:100])
        self.assertGreaterEqual(found, 4, "expected every rejection path to be covered")


class PromptTests(unittest.TestCase):
    CHAPTER = v3.Chapter(chapter_id=2, title="From missed trains to tourist stays",
                         subject="Late-night commuters entering a Japanese capsule hotel",
                         action="check in after dark",
                         evidence="travellers at a capsule hotel reception")

    def prompt_for(self, rejected, attempted=("朝 通勤 電車",)):
        seen = {}

        def fake(messages, **_kwargs):
            seen["text"] = messages[1]["content"]
            return {"queries": ["カプセルホテル 受付"]}

        with mock.patch.object(v3.scrape_v2, "_llm_json", side_effect=fake):
            v3.adaptive_search_queries(self.CHAPTER, list(attempted), rejected, round_index=1)
        return seen["text"]

    def test_the_wasteful_queries_are_named(self):
        rejected = [{"source_id": str(i), "reason": "unrelated_filler", "query": "朝 通勤 電車"}
                    for i in range(3)]
        text = self.prompt_for(rejected)
        self.assertIn("WHICH SEARCHES WASTED THE SLOTS", text)
        self.assertIn("朝 通勤 電車 -> unrelated_filler x3", text)

    def test_the_model_is_told_not_to_rephrase_them(self):
        text = self.prompt_for([{"source_id": "1", "reason": "unrelated_filler", "query": "q"}])
        self.assertIn("those exact hypotheses are", text)

    def test_unattributed_rejections_do_not_break_the_prompt(self):
        """Older state files hold rejections with no query."""
        text = self.prompt_for([{"source_id": "1", "reason": "unrelated_filler"}])
        self.assertIn("not attributed", text)

    def test_the_reason_tally_is_still_there(self):
        """The aggregate is still useful - it says HOW the footage was wrong."""
        text = self.prompt_for([{"source_id": "1", "reason": "no_visible_action", "query": "q"}])
        self.assertIn("no_visible_action x1", text)

    def test_the_worst_offender_is_listed_first(self):
        rejected = ([{"source_id": "a%d" % i, "reason": "unrelated_filler", "query": "weak"}
                     for i in range(5)]
                    + [{"source_id": "b", "reason": "unrelated_filler", "query": "mild"}])
        text = self.prompt_for(rejected)
        line = next(l for l in text.splitlines() if "WASTED" in l)
        self.assertLess(line.index("weak"), line.index("mild"))


if __name__ == "__main__":
    unittest.main()
