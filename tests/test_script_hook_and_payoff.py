"""A clip short lives or dies on its first sentence and its last one.

Measured against our own finished Shorts. These hooks carried their videos:

    "Tokyo train pushers only shove you if you board butt first"
    "Slicing this Japanese fish legally requires a heavy padlocked metal box"
    "Capsule hotels will kick you onto the street every single morning"

This one did not, and the video built on it was the weakest we shipped:

    "Japanese convenience stores look effortless, but the 3am shift is ruthless"

It names no object, no number and no action - there is nothing to picture and nothing to
disbelieve - and the script then listed shift chores and ended on "they twist every bottle so
the labels face forward", so leaving halfway cost the viewer nothing.
"""
import inspect
import re

import agent_core

RAW = inspect.getsource(agent_core.generate_viral_script)
# The prompt is one string split across source lines, so a rule can straddle a break.
# Rejoin the pieces before looking for wording, or the test only checks line layout.
SOURCE = re.sub(r'"\s*\n\s*"', "", RAW)


def test_the_hook_must_be_a_claim_not_a_setup():
    assert "THE FIRST SENTENCE PAYS FOR THE WHOLE VIDEO" in SOURCE
    assert "stated flat, as a fact" in SOURCE


def test_the_failing_hook_shapes_are_named():
    """A rule the model can apply beats an adjective it has to interpret."""
    for shape in ("'X looks Y, but Z'", "here is what nobody tells you about X",
                  "'X is not what you think'"):
        assert shape in SOURCE, shape


def test_both_hooks_are_shown_not_just_described():
    assert "board butt first" in SOURCE
    assert "padlocked metal box" in SOURCE
    assert "look effortless, but the 3am shift is ruthless" in SOURCE


def test_the_last_beat_has_to_be_a_payoff():
    assert "AND THE SCRIPT HAS TO GO SOMEWHERE" in SOURCE
    assert "never the last item on a list" in SOURCE
    # A test the model can actually run on its own draft.
    assert "could\n        # be swapped" in SOURCE or "could " in SOURCE
    assert "swapped with your third" in SOURCE


def test_the_rules_reach_the_scraped_and_the_generated_path():
    """Both footage sources need a hook; only footage REALITY is scrape-only."""
    hook_at = RAW.index("THE FIRST SENTENCE PAYS FOR THE WHOLE VIDEO")
    branch_at = RAW.index('if str(footage_source or "scrape")')
    assert hook_at < branch_at, "the hook rule must not sit inside the scrape-only branch"
