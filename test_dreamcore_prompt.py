"""The dreamcore prompt rules, checked against what the references actually do.

No API calls: everything here is the system prompt's own text and the pure planning
functions. The point is that a future edit cannot quietly put back the aesthetic this
mode was written with first - a dim VHS corridor with a locked-off camera - which is a
different genre from the four reels in reference/dreamcore/ and produced unusable prompts.

Measured from those reels (see the module docstring): a pristine hyper-real render, hard
mid-day sun, a camera that moves in every shot and is the only thing that moves, one-point
perspective, mundane objects repeated to the horizon in an impossible geometry, and shots
that step further out until an aerial reveals the true scale.
"""

import dreamcore_mode as D


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + (f" - {detail}" if detail and not ok else ""))
    return bool(ok)


def test_system_prompt_states_the_measured_grammar():
    s = D.PROMPT_SYSTEM
    required = [
        ("the subject is the aesthetic", "ONE ORDINARY THING"),
        ("camera always moves", "The camera MOVES IN EVERY SHOT"),
        ("the move is a forward dolly", "dolly FORWARD"),
        ("the world is frozen", "Nothing in the world moves"),
        ("clean render, not VHS", "hyper-real 3D render"),
        ("hard sun with sharp shadows", "BRIGHT HARD SUNLIGHT"),
        ("one-point perspective", "One-point perspective"),
        ("the shots step further out", "FURTHER AND FURTHER OUT"),
        ("cuts live inside the clip", "HARD CUTS inside it"),
        ("the output contract survives", "Return JSON"),
    ]
    ok = True
    for label, needle in required:
        ok &= check(f"system prompt: {label}", needle in s, f"missing {needle!r}")
    return ok


def test_system_prompt_forbids_the_old_aesthetic():
    """The old rules are not merely absent - they are named and forbidden.

    Both of these were positive instructions once, and both are exactly wrong for these
    references: the reels have no still camera and no grain.
    """
    s = D.PROMPT_SYSTEM
    ok = True
    ok &= check("locked-off camera is forbidden by name",
                'NEVER write "locked off"' in s)
    ok &= check("grain and VHS are forbidden by name",
                "no grain" in s and "no VHS" in s)
    ok &= check("the frozen world is spelled out against the old rule",
                "unfelt draught" in s)
    return ok


def test_placeholders_survive_assembly():
    """The prompt is assembled from four blocks; the hold-time substitution runs on the
    result. A block joined in the wrong order silently drops the cut times."""
    s = D.PROMPT_SYSTEM
    ok = True
    for token in ("{cuts_per_clip}", "{hold_sequence}", "{clip_seconds}"):
        ok &= check(f"placeholder {token} present", token in s)
    return ok


def test_stock_review_catches_the_words_that_broke_it():
    """The exact vocabulary Gemini blamed for the stock-footage clip."""
    hits = D.stock_review("Cinematic golden hour over rolling hills, a serene surreal "
                          "dreamlike vista, drone shot with lens flare")
    ok = check("stock wording is flagged", {"golden hour", "ad language", "postcard",
                                            "drone shot", "surreal"} <= set(hits), str(hits))
    ok &= check("reference wording is not flagged",
                D.stock_review("high mid-day sun from the left, hard sharp shadows, a slow "
                               "steady dolly forward to the vanishing point") == [],
                str(D.stock_review("high mid-day sun from the left, hard sharp shadows")))
    return ok


def test_safety_review_still_works():
    ok = check("negation is flagged", "negation" in D.safety_review("an empty hall, no people"))
    ok &= check("clean prompt passes", D.safety_review("an empty hall, the street unoccupied") == [])
    return ok


def test_shot_plan_fits_the_clip():
    """Three full phrases do not fit in ten seconds; the third shot must be a half phrase."""
    holds = D.shot_plan(10.0, 3.85, 3)
    ok = check("three shots in a 10s clip", len(holds) == 3, str(holds))
    ok &= check("the last one is a half phrase", holds[-1] < holds[0], str(holds))
    ok &= check("a 4s clip carries one shot", len(D.shot_plan(4.0, 3.85, 3)) == 1)
    return ok


if __name__ == "__main__":
    results = [
        test_system_prompt_states_the_measured_grammar(),
        test_system_prompt_forbids_the_old_aesthetic(),
        test_placeholders_survive_assembly(),
        test_stock_review_catches_the_words_that_broke_it(),
        test_safety_review_still_works(),
        test_shot_plan_fits_the_clip(),
    ]
    print()
    print(f"{sum(1 for r in results if r)}/{len(results)} groups passed")
    raise SystemExit(0 if all(results) else 1)
