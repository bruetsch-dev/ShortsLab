"""The widening that looked measured, and what the measurement was not asking.

For one day `evidence_fits` also accepted, for an action or payoff beat, a verdict earned under
another line of the same declared `sequence_id`. The case for it was a real count over the 322
windows the eating-walk run's vision pass accepted:

    rule                       beats with at least one fitting window (of 15)
    the line must match          4
    the same declared sequence   15

Eleven of the twelve action and payoff beats had ZERO, and went to the coverage-fill path, which
checks nothing at all. Eleven windows carried action_visible AND result_visible and were being
discarded.

The count never asked what those eleven verdicts ATTESTED. Measured afterwards:

    ten of the eleven had been reviewed under a CONTEXT line, one under a payoff line

A context contract's `required_action` is the setting - "the customer stands at a market" - so
`action_visible: True` on it means the customer was seen standing there. It says nothing about
"the skewer is visibly empty". Under the widened rule one window, "Food is handed over the
counter", proved four different beats.

Two further things the beats-covered number hid: at assignment a beat only sees its own queries,
so `matched` was identical under both rules and only `borrowed` widened - by two windows that had
already shipped as coverage fills; and the source concentration it was meant to relieve did not
move, because all four beats of seq_2 drew on the one source that already supplied 47% of the
Short. The net effect was the same footage with `needs_replacement` and the editor's red border
taken off it.

A neighbour's window can still earn a beat - by being re-reviewed under THAT beat's line, the way
`_review_continuations` does. That costs a vision call, which is what the evidence costs.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import editorial


def beat(role="action", line="stop and stand completely still", sequence="seq_2", result=""):
    return {"exact_voice_text": line, "editorial_role": role, "sequence_id": sequence,
            "required_action": "a person stops beside a stall", "required_result": result}


def verdict(line="stop and stand completely still", sequence="seq_2", action=True,
            seen="a man stops beside the stall and stands still", result=None, relevance=8):
    out = {"reviewed_line": line, "reviewed_sequence_id": sequence, "action_visible": action,
           "observed_action": seen, "relevance": relevance}
    if result is not None:
        out["result_visible"] = result
    return out


class OnlyThisLinesOwnVerdictProvesThisLine(unittest.TestCase):
    def test_its_own_line_fits(self):
        self.assertTrue(editorial.evidence_fits(beat(), verdict()))

    def test_a_neighbour_in_the_same_sequence_does_not(self):
        """This is the clause that was added and reverted. Ten of the eleven verdicts it would
        have admitted were earned on a CONTEXT contract, where action_visible means the setting
        was seen and nothing more."""
        self.assertFalse(editorial.evidence_fits(
            beat(), verdict(line="at a crowded night market, you must", sequence="seq_2")))

    def test_a_different_sequence_does_not_either(self):
        self.assertFalse(editorial.evidence_fits(
            beat(), verdict(line="greasy wrappers right back to", sequence="seq_4")))


class TheOtherClausesAreUnchanged(unittest.TestCase):
    def test_an_unseen_action_fits_nothing(self):
        self.assertFalse(editorial.evidence_fits(beat(), verdict(action=False)))

    def test_a_verdict_that_describes_nothing_fits_nothing(self):
        self.assertFalse(editorial.evidence_fits(beat(), verdict(seen="   ")))

    def test_a_required_result_must_have_been_seen(self):
        contract_beat = beat(result="the skewer is visibly empty")
        self.assertFalse(editorial.evidence_fits(contract_beat, verdict(result=False)))
        self.assertTrue(editorial.evidence_fits(contract_beat, verdict(result=True)))

    def test_context_still_asks_only_for_relevance(self):
        self.assertTrue(editorial.evidence_fits(beat(role="context"), verdict(relevance=7)))
        self.assertFalse(editorial.evidence_fits(beat(role="context"), verdict(relevance=6)))


class TheProvenanceIsStillRecorded(unittest.TestCase):
    """`reviewed_sequence_id` stays. It is not a licence - it is how anyone can tell later what
    contract a verdict was earned under, which had to be reconstructed by hand to find this."""

    def test_every_place_that_stamps_a_line_stamps_the_sequence(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for name in ("scrape_v4.py", "editorial_quality.py"):
            src = open(os.path.join(root, name), encoding="utf-8").read()
            self.assertEqual(src.count('["reviewed_line"] = editorial.contract('),
                             src.count('["reviewed_sequence_id"] = editorial.contract('),
                             f"{name} records a reviewed line somewhere without the contract it "
                             f"was earned under")

    def test_the_rule_itself_does_not_read_it(self):
        import inspect
        self.assertNotIn("reviewed_sequence_id", inspect.getsource(editorial.evidence_fits)
                         .split('"""')[-1])


if __name__ == "__main__":
    unittest.main()
