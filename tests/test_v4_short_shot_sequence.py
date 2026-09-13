import scrape_v4 as v4


def make(source, start, end, *, action=True, result=True):
    return v4.Candidate(source, "replica craft", "unused.mp4", start, end, 8, "", "available",
        vision={"reviewed_line": "They make plastic food", "relevance": 8,
                "action_visible": action, "result_visible": result,
                "observed_action": "molding plastic food" if action else "finished food replica",
                "action_start": 0, "action_end": 1})


def scene(role="action"):
    return {"id": "beat", "text": "They make plastic food", "start": 2, "end": 5,
            "editorial_role": role, "required_action": "molding food replica",
            "required_result": "finished replica" if role != "context" else ""}


def test_two_short_sources_cover_one_longer_beat():
    rows = [make("a", 0, 1.5), make("b", 0, 1.5)]
    assert not any(v4._window_covers_scene(scene(), c) for c in rows)
    parts = v4._combine_short_windows(scene(), rows, {}, [], [])
    assert len(parts) == 2
    assert parts[0][0]["start"] == 2
    assert parts[0][0]["end"] == parts[1][0]["start"]
    assert parts[1][0]["end"] == 5


def test_action_and_result_can_be_in_separate_shots():
    parts = v4._combine_short_windows(scene(),
        [make("a", 0, 1.5, result=False), make("b", 0, 1.5, action=False)], {}, [], [])
    assert len(parts) == 2
    assert parts[0][0]["required_result"] == ""
    assert parts[1][0]["parent_editorial_contract"]["required_result"] == "finished replica"


def test_context_does_not_need_visible_action():
    parts = v4._combine_short_windows(scene("context"),
        [make("a", 0, 1.5, action=False, result=False),
         make("b", 0, 1.5, action=False, result=False)], {}, [], [])
    assert len(parts) == 2


def test_unrelated_line_and_overlapping_windows_are_not_a_sequence():
    a, b = make("a", 0, 1.5), make("a", .1, 1.6)
    assert not v4._combine_short_windows(scene(), [a, b], {}, [], [])
    b.source_id = "b"
    b.vision["reviewed_line"] = "Unrelated narration"
    assert not v4._combine_short_windows(scene(), [a, b], {}, [], [])
