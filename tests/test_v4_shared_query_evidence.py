import scrape_v4 as v4


def test_shared_query_gets_second_line_review_without_new_discovery(tmp_path, monkeypatch):
    scene = {"text": "Artist paints the rice.", "editorial_role": "action", "start": 0, "end": 2}
    prior = {"accept": True, "reviewed_line": "The replicas are expensive.",
             "action_visible": True, "observed_action": "artist painting rice"}
    candidate = v4.Candidate("source", "food replica workshop", "clip.mp4", 0, 3,
                             8, "", "available", vision=prior)
    calls = []
    def review(copies, project, **kwargs):
        calls.append(kwargs["briefs"])
        for c in copies:
            c.vision = dict(prior, reviewed_line=scene["text"])
    monkeypatch.setattr(v4, "_vision_source_review", review)
    result = v4._review_for_missing_line(scene, [candidate], tmp_path, "flash")
    assert len(result) == 1
    assert calls[0][candidate.query] == scene
    assert candidate.vision["reviewed_line"] == "The replicas are expensive."
    assert result[0].vision["reviewed_line"] == scene["text"]


def test_short_or_unreviewed_candidates_do_not_get_relabelled(tmp_path, monkeypatch):
    scene = {"text": "Artist paints rice", "start": 0, "end": 3}
    c = v4.Candidate("source", "rice", "clip.mp4", 0, .7, 8, "", "available")
    monkeypatch.setattr(v4, "_vision_source_review", lambda *a, **k: (_ for _ in ()).throw(AssertionError()))
    assert v4._review_for_missing_line(scene, [c], tmp_path, "flash") == []


def test_short_direct_match_does_not_block_other_candidates():
    scene = {"text": "Artist paints rice", "editorial_role": "action", "start": 0, "end": 3}
    verdict = {"reviewed_line": scene["text"], "action_visible": True,
               "observed_action": "paints rice", "accept": True}
    short = v4.Candidate("s", "rice", "clip.mp4", 0, .7, 8, "", "available", vision=verdict)
    assert not v4._has_usable_line_evidence(scene, [short])
    assert v4._pick_coverage_fill([short], {"rice"}, {}, [], [], need=3, scene=scene)[0] is None


def test_fill_skips_window_whose_action_cannot_be_trimmed():
    scene = {"text": "Artist paints rice", "editorial_role": "action", "start": 0, "end": 2}
    verdict = {"reviewed_line": scene["text"], "action_visible": True,
               "observed_action": "paints rice", "accept": True, "action_start": 0, "action_end": 1}
    long = v4.Candidate("s1", "rice", "a.mp4", 0, 4, 9, "", "available", vision=verdict)
    fit = v4.Candidate("s2", "rice", "b.mp4", 0, 2, 8, "", "available", vision=verdict)
    assert v4._pick_coverage_fill([long, fit], {"rice"}, {}, [], [], need=2, scene=scene)[0] is fit
