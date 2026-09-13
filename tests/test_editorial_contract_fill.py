"""Regression tests for pipeline behavior."""
import editorial
import scrape_v4
import pipeline
import agent_core
import pytest


def candidate(line, *, status="available", relevance=9, action=True, result=True):
    return scrape_v4.Candidate(
        "tiktok__1", "toilet query", "clip.mp4", 0.0, 3.0, 8.0, "", status,
        vision={"reviewed_line": line, "relevance": relevance,
                "action_visible": action, "result_visible": result,
                "observed_action": "a person locks the door and the glass turns opaque"})


def pick(scene, rows):
    return scrape_v4._pick_coverage_fill(
        rows, {"toilet query"}, {}, [], [], need=2.0, scene=scene)[0]


def test_topic_match_reviewed_for_another_line_cannot_fill_a_context_beat():
    scene = {"exact_voice_text": "Tokyo installed public toilets", "editorial_role": "context"}
    assert not editorial.evidence_fits(scene, candidate("transparent toilets in Japan").vision)
    assert pick(scene, [candidate("transparent toilets in Japan")]) is None


def test_action_fill_requires_this_lines_visible_action_and_result():
    scene = {"exact_voice_text": "the glass becomes opaque", "editorial_role": "payoff",
             "required_action": "the glass changes", "required_result": "glass is opaque"}
    assert pick(scene, [candidate(scene["exact_voice_text"], result=False)]) is None
    approved = candidate(scene["exact_voice_text"], result=True)
    assert pick(scene, [approved]) is approved


def test_rejected_media_never_fills_even_with_a_positive_old_verdict():
    scene = {"exact_voice_text": "lock the door", "editorial_role": "action"}
    assert pick(scene, [candidate(scene["exact_voice_text"], status="rejected")]) is None


def test_live_caller_passes_the_scene_contract_and_marks_budget_fallbacks():
    source = open(scrape_v4.__file__, encoding="utf-8").read()
    call = source[source.index("budget_fallback = _vision_budget_spent(project)"):]
    assert "scene=out_scenes[index]" in call[:700]
    assert "allow_reviewed_fallback=budget_fallback" in call[:700]
    assert "v4_budget_fallback" in source


def test_a_failed_export_audit_is_not_published_as_a_finished_render():
    source = open(pipeline.__file__, encoding="utf-8").read()
    assert 'editorial_publishable = quality.get("status") == "passed"' in source
    assert "if editorial_publishable:\n        renders.publish" in source


def test_adjacent_identical_actions_are_detected_even_when_the_words_differ():
    beats = [
        {"exact_voice_text": "special glass stays clear", "required_action": "show empty room",
         "required_result": "interior is visible"},
        {"exact_voice_text": "so you can check it", "required_action": "  SHOW empty room  ",
         "required_result": "INTERIOR is visible"},
        {"exact_voice_text": "then lock it", "required_action": "hand turns the lock",
         "required_result": "door is locked"},
    ]
    assert agent_core.adjacent_duplicate_editorial_actions(beats) == [1]


def test_an_uncorrected_duplicate_plan_merges_into_one_honest_hold():
    source = open(agent_core.__file__, encoding="utf-8").read()
    assert "merge_adjacent_duplicate_editorial_actions" in source
    assert "merged them into honest continuation beat(s)" in source


def test_duplicate_plan_gets_one_correction_then_merges(monkeypatch):
    raw = {"micro_beats": [
        {"start_time": 0, "end_time": 1.8, "exact_voice_text": "A clear bathroom",
         "editorial_role": "context", "required_action": "show clear glass toilet",
         "required_result": "interior visible"},
        {"start_time": 1.8, "end_time": 3.6, "exact_voice_text": "has transparent walls",
         "editorial_role": "context", "required_action": "show clear glass toilet",
         "required_result": "interior visible"},
    ]}
    calls = []

    def reply(*args, **kwargs):
        import json
        calls.append(1)
        return {"choices": [{"message": {"content": json.dumps(raw)}}]}

    monkeypatch.setattr(agent_core, "post_json_url", reply)
    result = agent_core.llm_micro_beat_plan(
        "toilet", "A clear bathroom has transparent walls", "", 3.6,
        [{"start": 0, "end": 3.6,
          "script": "A clear bathroom has transparent walls"}])
    assert len(calls) == 2
    beats = result["micro_beats"]
    assert len(beats) == 1
    assert beats[0]["start"] == 0
    assert beats[0]["end"] == 3.6
    assert beats[0]["exact_voice_text"] == "A clear bathroom has transparent walls"
