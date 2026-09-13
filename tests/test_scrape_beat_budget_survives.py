"""The scrape beat budget must survive the pacing pass, and the language call must survive a blip.

Two faults measured on one live run 2026-09-03 ("Japanese barbers", ~30s script):

    Micro-Beat Planner: created 8 beat(s).
    Pacing: split long beats into readable Fact Short shots (8 -> 17 beats).
    Fact Short visual chapters: 17 narration beat(s) -> 12 searchable chapters
    V4 native search terms unavailable (HTTPError); continuing with the brief's own wording.

1. `enforce_reference_pacing` ran at max_s=2.45 and cut every planned beat in half. That cap came
   from a reference Short whose editor already owned the footage - and the block runs ONLY for
   clip_source == "scrape", where each beat is a separate video that has to be found. So the one
   path that cannot afford beats was the one manufacturing them.
2. `_native_terms_agent` made a single attempt. `post_json_url` retries only 429/502/503, so a
   500, a 504 or a dropped connection killed the call outright - and the run then searched TikTok
   in English, measured at 72 post URLs against 476 with native phrases.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent_core
import scrape_v4


class ThePacingPassRespectsTheBudget(unittest.TestCase):

    def test_planned_beats_survive_the_scrape_cap(self):
        """Beats inside the budget must reach the search untouched.

        The lengths moved on 2026-09-04. This used to prove that 3.4-5.0s beats survived, because
        the cap had been widened to stop the pass manufacturing clip demand a starving run could
        not meet. Those same lengths then reached the screen: five finished Shorts came back with
        medians of 3.7-5.1s, and anything past ~2.75s is a slowed clip ending in a freeze. The
        budget is now the user's 2.3s average, so the shapes worth protecting are these.
        """
        for count, length in ((10, 1.8), (9, 2.55), (8, 3.2)):
            with self.subTest(beats=count, length=length):
                beats = [{"id": str(i), "start": i * length, "end": (i + 1) * length,
                          "text": f"beat {i}"} for i in range(count)]
                kept = agent_core.enforce_reference_pacing([dict(b) for b in beats], max_s=3.6)
                self.assertEqual(len(kept), count,
                                 "the pacing pass split beats that were already short enough")

    def test_a_beat_that_would_freeze_is_split(self):
        """4s of narration over a 2.3s excerpt is 1.7s of frozen picture."""
        for length in (3.7, 4.0, 5.0):
            with self.subTest(length=length):
                beat = [{"id": "1", "start": 0.0, "end": length, "text": "a long held line"}]
                self.assertGreater(len(agent_core.enforce_reference_pacing(beat, max_s=3.6)), 1)

    def test_a_genuinely_held_shot_still_splits(self):
        long_beat = [{"id": "1", "start": 0.0, "end": 11.0, "text": "one very long held shot"}]
        self.assertGreater(len(agent_core.enforce_reference_pacing(long_beat, max_s=3.6)), 1)

    def test_the_call_site_caps_a_scraped_beat_at_the_readable_length(self):
        """Superseded 2026-09-04: 5.0s was raised while the run could not find enough clips, and
        it then permitted the frozen 6-8 second shots measured in five finished Shorts."""
        with open(agent_core.__file__, encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn("max_s=(3.2 if mini_story_mode", source)
        self.assertIn('else (3.6 if str(form.get("clip_source", "generate") or "").strip().lower()',
                      source)
        self.assertNotIn("scenes_override, max_s=(3.2 if mini_story_mode else 5.0))", source)


class TheLanguageCallIsRetried(unittest.TestCase):

    def setUp(self):
        with open(scrape_v4.__file__, encoding="utf-8") as handle:
            self.source = handle.read()

    def test_transport_failures_get_another_attempt(self):
        self.assertIn("for attempt in range(3):", self.source)
        self.assertIn("except (OSError, urllib.error.URLError) as exc:", self.source)

    def test_a_non_transport_failure_does_not_spin(self):
        """A 400 or a malformed reply will not become right by asking again."""
        self.assertIn("except Exception as exc:                                        # noqa: BLE001\n"
                      "            last_error = exc\n"
                      "            break", self.source)

    def test_the_log_says_how_many_attempts_were_made(self):
        self.assertIn("unavailable after {attempt + 1} attempt(s)", self.source)


if __name__ == "__main__":
    unittest.main()
