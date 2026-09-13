"""A TikTok post that will not deliver must be dropped in seconds, not in a minute.

Measured on eight real post URLs 2026-09-03, before the change:

    successful downloads   2.9s, 3.8s, 3.8s, 4.8s
    failed downloads       26.5s, 48.1s, 48.4s, 53.5s

Nothing has ever succeeded slowly, so the long waits protected nothing - they only made failure
expensive. The mirrors Fact Short inspected 285 candidates and 112 of them were failed downloads:
roughly two hours of wall clock spent on posts this session cannot fetch. After the change the
same eight URLs failed in 12.5-12.7s and still succeeded in 3.2-5.8s.
"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tiktok_login


def download_body():
    with open(tiktok_login.__file__, encoding="utf-8") as handle:
        source = handle.read()
    start = source.index("    def download_video(self, url, dest")
    return source[start:source.index("\n    def ", start + 10)]


class FailureIsCheap(unittest.TestCase):

    def setUp(self):
        self.body = download_body()

    def test_the_video_element_is_not_waited_on_for_fifteen_seconds(self):
        self.assertIn('page.wait_for_selector("video", timeout=6000)', self.body)
        self.assertNotIn('page.wait_for_selector("video", timeout=15000)', self.body)

    def test_the_media_wait_is_a_fraction_of_the_old_half_timeout(self):
        self.assertIn("deadline = time.monotonic() + max(8.0, float(timeout_s) * 0.12)", self.body)
        self.assertNotIn("max(10.0, float(timeout_s) * 0.5)", self.body)

    def test_the_worst_case_wait_is_bounded(self):
        """With the shipped default the two waits must stay well under twenty seconds."""
        selector_ms = int(re.search(r'wait_for_selector\("video", timeout=(\d+)\)',
                                    self.body).group(1))
        floor, factor = re.search(r"max\(([\d.]+), float\(timeout_s\) \* ([\d.]+)\)",
                                  self.body).groups()
        worst = selector_ms / 1000.0 + max(float(floor), 90 * float(factor))
        self.assertLess(worst, 20.0, f"a dead post still costs {worst:.1f}s")

    def test_a_post_that_does_deliver_still_gets_its_trailing_body(self):
        """The 1.5s grace only runs once bytes are in hand, never while waiting for the first."""
        wait_loop = self.body.index("while time.monotonic() < deadline")
        grace = self.body.index("page.wait_for_timeout(1500)")
        self.assertLess(wait_loop, grace, "the grace wait still runs before anything arrived")
        self.assertIn("if chunks:", self.body[wait_loop:grace])

    def test_giving_up_needs_both_routes_to_have_failed(self):
        """A post is only undeliverable once the harvest AND the re-fetch have come up empty."""
        block = self.body[self.body.index("while time.monotonic() < deadline"):]
        self.assertLess(block.index("self._ctx.request.get("),
                        block.index("would not serve this session"))


if __name__ == "__main__":
    unittest.main()
