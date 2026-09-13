"""A scraped Clip Short must not ask for more unique clips than a run can find.

Measured on a live V4 run 2026-09-02: a 24s voiceover became 15 chapters of 1.6s, so the edit
needed 15 unique verified clips. Discovery returned 22 accepted vision verdicts covering roughly
eight distinct sources; five beats were filled and the run correctly refused to render anything.
Seventeen accepted clips went unused.

The cap decides whether two adjacent narration beats can merge at all, so a FIXED cap behaves as
a cliff: at 3.4s, 1.60s beats pair into 8 chapters and 1.71s beats cannot pair and stay at 14.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent_core


def scrape_bounds(scenes):
    """The bounds agent_core derives for a scraped run - kept in step with the caller by test."""
    return 2.55, 3.6


def chapters(count, duration):
    step = duration / count
    beats = [{"id": f"b{i}", "start": i * step, "end": (i + 1) * step,
              "text": f"beat {i} with some spoken words in it"} for i in range(count)]
    target_s, max_s = scrape_bounds(beats)
    built = agent_core.coalesce_scrape_visual_chapters(
        beats, min_s=1.55, target_s=target_s, max_s=max_s)
    built = agent_core.enforce_fact_short_max_hold(built, max_s=max_s)
    return built


class ScrapeChapterSupply(unittest.TestCase):

    def test_the_demand_tracks_the_trash_reference_cadence(self):
        """Aim near one shot per 2.55 seconds without inventing rapid extra cuts."""
        for count, duration in [(15, 24.0), (14, 24.0), (12, 26.0), (10, 22.0), (18, 28.0)]:
            with self.subTest(beats=count, duration=duration):
                built = chapters(count, duration)
                expected = duration / 2.55
                self.assertLessEqual(abs(len(built) - expected), 2.5,
                                     f"{len(built)} clips misses the Trash cadence")
                self.assertGreaterEqual(len(built), 4, "a Short still needs several cuts")

    def test_the_1_71s_cliff_is_gone(self):
        """A flat 3.4s cap merged 1.60s beats and left 1.71s beats untouched."""
        self.assertLess(len(chapters(14, 24.0)), 14)

    def test_no_chapter_becomes_a_slideshow_hold(self):
        for count, duration in [(15, 24.0), (12, 26.0), (10, 22.0)]:
            with self.subTest(beats=count):
                longest = max(c["end"] - c["start"] for c in chapters(count, duration))
                self.assertLessEqual(longest, 4.5, "a single shot held too long to watch")

    def test_the_chapters_still_cover_the_whole_voiceover(self):
        built = chapters(15, 24.0)
        self.assertAlmostEqual(built[0]["start"], 0.0, places=2)
        self.assertAlmostEqual(built[-1]["end"], 24.0, places=2)
        for earlier, later in zip(built, built[1:]):
            self.assertAlmostEqual(earlier["end"], later["start"], places=2,
                                   msg="a gap opened between chapters")


class BoundsStayInStepWithTheCaller(unittest.TestCase):
    """`scrape_bounds` above is a copy of logic that lives inline in a very large function.

    A copy can drift silently, so pin the two numbers that define it to the real source.
    """

    def test_the_caller_caps_a_chapter_at_what_a_window_fills(self):
        """Superseded 2026-09-04. Deriving the cap from the median beat let any two beats merge,
        which is what a starving run needed - it halved the clip demand. It also produced chapters
        of 3.4-5s that no 2.3s excerpt could fill, so the picture froze. The cap is now the window
        itself, and a beat that genuinely needs longer takes a longer excerpt instead."""
        with open(agent_core.__file__, encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn("_target_s = 2.55", source)
        self.assertIn("_max_s = 3.6", source)
        self.assertNotIn("_max_s = max(3.4, _median * 2.15)", source)
        self.assertIn("_hold_cap = _max_s if (mini_story_mode or _scraping) else 2.75", source)


if __name__ == "__main__":
    unittest.main()
