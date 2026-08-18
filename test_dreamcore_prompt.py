"""Offline checks for the reference-derived Dreamcore prompt planner."""

import dreamcore_mode as D


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + (f" - {detail}" if detail and not ok else ""))
    return bool(ok)


def test_system_uses_full_reference_range():
    s = D.PROMPT_SYSTEM
    required = [
        ("ordinary anchor", "immediately readable and ordinary"),
        ("impossible spatial rule", "ONE dominant spatial rule"),
        ("lighting range", "sodium night"),
        ("not cloud-suburb locked", "do NOT all use blue sky"),
        ("reference combinations cannot be copied", "NOVELTY"),
        ("fixed Higgsfield length", "EXACTLY 10.0 seconds"),
        ("strict output contract", "cut_times"),
    ]
    return all(check(f"system: {label}", needle in s, needle) for label, needle in required)


def test_reference_cut_languages():
    auto = D.edit_patterns_for("auto", 4)
    ok = check("auto varies its grammar", len({p["id"] for p in auto}) >= 3, str(auto))
    ok &= check("classic matches measured 3.7s cadence",
                D.EDIT_STYLES["classic_reveal"]["cuts"] == [3.7, 7.4])
    ok &= check("continuous has no hidden cut",
                D.EDIT_STYLES["continuous_passage"]["cuts"] == [])
    glitch = D.EDIT_STYLES["memory_glitch"]["cuts"]
    ok &= check("memory glitch contains micro-cut bursts",
                len(glitch) >= 12 and min(b-a for a, b in zip(glitch, glitch[1:])) <= .3,
                str(glitch))
    return ok


def test_blank_direction_is_a_supported_creative_mode():
    captured = {}
    old_post = D.agent_core._post_llm_json
    old_balance = D.agent_core.assert_wavespeed_balance
    try:
        D.agent_core.assert_wavespeed_balance = lambda **kwargs: None

        def fake_post(model, messages, max_tokens, temperature, timeout):
            captured["ask"] = messages[-1]["content"]
            captured["temperature"] = temperature
            return {"world": "A fresh collection", "prompts": [
                {"label": "Recursive post office", "shots": ["counter reveal"],
                 "text": "A complete original vertical 10-second prompt"},
                {"label": "Gravity laundromat", "shots": ["continuous orbit"],
                 "text": "A second complete original vertical 10-second prompt"},
            ]}

        D.agent_core._post_llm_json = fake_post
        out = D.prompts_for("", clip_count=2, edit_style="auto")
        ok = check("blank asks the agent to invent", "no direction" in captured["ask"])
        ok &= check("blank mode is more exploratory", captured["temperature"] >= .9)
        ok &= check("patterns survive model output",
                    [p["edit_style"] for p in out["prompts"]]
                    == ["classic_reveal", "continuous_passage"], str(out))
        ok &= check("cut metadata survives", out["prompts"][0]["cut_times"] == [3.7, 7.4])
        return ok
    finally:
        D.agent_core._post_llm_json = old_post
        D.agent_core.assert_wavespeed_balance = old_balance


def test_reviews_still_protect_generation_quality():
    hits = D.stock_review("Cinematic golden hour over rolling hills with lens flare")
    ok = check("stock wording is flagged", {"golden hour", "ad language", "postcard"} <= set(hits))
    ok &= check("unsafe negation is flagged",
                "negation" in D.safety_review("an empty hall with no people"))
    return ok


def test_authored_timings_survive_the_final_edit():
    grid = {"phrase": 3.85, "duration": 30.0, "offset": 0.0, "onsets": []}
    plan = D.plan_edit([
        {"clip": "continuous.mp4", "start": 0.0, "end": 10.0, "seconds": 10.0,
         "preserve_duration": True},
        {"clip": "glitch.mp4", "start": 0.0, "end": 0.3, "seconds": 0.3,
         "preserve_duration": True},
    ], grid)
    ok = check("continuous chapter is not cut back to one phrase",
               plan["parts"][0]["slot"] == 10.0, str(plan))
    ok &= check("reference micro-shot is not discarded",
                len(plan["parts"]) == 2 and plan["parts"][1]["slot"] == 0.3, str(plan))
    return ok


if __name__ == "__main__":
    results = [test_system_uses_full_reference_range(), test_reference_cut_languages(),
               test_blank_direction_is_a_supported_creative_mode(),
               test_reviews_still_protect_generation_quality(),
               test_authored_timings_survive_the_final_edit()]
    print(f"\n{sum(bool(r) for r in results)}/{len(results)} groups passed")
    raise SystemExit(0 if all(results) else 1)
