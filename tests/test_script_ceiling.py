"""The script call's output ceiling must cover the model's reasoning, not just the script.

Measured 2026-09-02 on openai/gpt-5.6-luna with the REAL prompt (footage briefing + editorial
lens + recent-script history): max_tokens 300 and 1200 both returned finish_reason="length" with
message.content EMPTY on all three attempts, which reached the user as "Script creator returned
no usable script - try again." 4000 returns a script on the first attempt.

The old ceiling was `max(280, token_limit * 2)`; a 150-token script limit produced exactly the
fatal 300. A trimmed toy prompt succeeded at 600, which is why the old value looked sufficient.
"""

import inspect
import unittest

import agent_core


class CeilingTests(unittest.TestCase):
    def body(self):
        source = inspect.getsource(agent_core.generate_viral_script)
        return source

    def test_the_ceiling_has_a_floor_above_the_measured_failure(self):
        # Check the ASSIGNMENT, not the file text: the comment above it quotes the old formula
        # on purpose, and a substring search over the whole function matches that quote.
        body = self.body()
        assignments = [line.strip() for line in body.splitlines()
                       if line.strip().startswith("model_output_tokens =")]
        self.assertEqual(len(assignments), 1, assignments)
        self.assertEqual(assignments[0], "model_output_tokens = max(4000, token_limit * 6)")

    def test_a_short_script_limit_cannot_produce_a_tiny_ceiling(self):
        """The failure mode was arithmetic: a small limit made a small ceiling."""
        for token_limit in (80, 90, 150, 180):
            self.assertGreaterEqual(max(4000, token_limit * 6), 4000)

    def test_a_long_limit_still_scales_up(self):
        self.assertEqual(max(4000, 1000 * 6), 6000)

    def test_the_reason_is_recorded_next_to_the_number(self):
        """A bare constant invites someone to shrink it again."""
        body = self.body()
        self.assertIn("finish_reason", body)
        self.assertIn("EMPTY", body)


if __name__ == "__main__":
    unittest.main()
