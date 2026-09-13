"""Regression cases from the walking/eating export, using decoded video fixtures."""
import json
import shutil
from types import SimpleNamespace
from unittest.mock import patch

import cv2
import numpy as np
import pytest

import editorial
import editorial_quality as quality
import scrape_v4


def test_food_is_not_evidence_of_standing_still():
    scene = {"exact_voice_text": "stop and stand completely still"}
    verdict = {"reviewed_line": scene["exact_voice_text"], "relevance": 10,
               "observed_action": "cook grills skewers", "action_visible": False}
    assert not editorial.evidence_fits(scene, verdict)
    assert editorial.evidence_fits(dict(scene, editorial_role="context"), verdict)


def test_required_result_and_line_identity_cannot_be_borrowed():
    scene = {"exact_voice_text": "hand the wrapper back", "required_result": "vendor receives wrapper"}
    verdict = {"reviewed_line": "open the wrapper", "action_visible": True,
               "result_visible": True, "observed_action": "opens wrapper"}
    assert not editorial.evidence_fits(scene, verdict)
    verdict["reviewed_line"] = scene["exact_voice_text"]
    verdict["result_visible"] = False
    assert not editorial.evidence_fits(scene, verdict)
    verdict["result_visible"] = True
    assert editorial.evidence_fits(scene, verdict)


def test_cut_keeps_the_late_action_and_its_result():
    verdict = {"action_start": .7, "action_end": .95}
    start = editorial.action_trim(10, 13, 1, verdict)
    assert start <= 12.1 and start + 1 >= 12.85
    assert editorial.action_trim(10, 13, .5, verdict) is None


def test_same_source_may_continue_only_its_declared_sequence():
    prior = [{"sequence_id": "food_stall", "scrape_clip_id": "post1"}]
    candidate = SimpleNamespace(source_id="post1")
    assert editorial.may_continue({"sequence_id": "food_stall"}, candidate, prior, 1)
    assert not editorial.may_continue({"sequence_id": "trash_cans"}, candidate, prior, 1)
    assert not editorial.may_continue({}, candidate, prior, 1)


def test_shot_candidates_include_late_source_and_never_cross_cuts():
    cuts = [1, 8, 30, 43, 48, 72]
    windows = scrape_v4._windows(90, 2.3, cuts)
    assert any(start > 72 for start, _ in windows)
    assert any(43 < start < 48 for start, _ in windows)
    assert all(not any(start < cut < end for cut in cuts) for start, end in windows)
    assert len(windows) <= 18


def test_missing_cut_measurement_never_means_clean():
    assert quality.repair_window(0, 2, None, 2)[2] == "unverified"
    assert quality.scan_cuts("missing.mp4", None) is None


def test_face_flash_at_leading_edge_is_removed_without_leaving_window():
    start, speed, status = quality.repair_window(0, 2, [.1], 2)
    assert status == "repaired"
    assert start > .1
    assert speed >= .84
    assert start + 2 * speed <= 2.000001


def test_interior_montage_requires_reselection_not_arbitrary_movement():
    start, speed, status = quality.repair_window(12, 15, [13, 14], 3)
    assert (start, speed, status) == (12, 1, "needs_replacement")


@pytest.fixture
def movie(tmp_path):
    path = tmp_path / "fixture.mp4"
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 30, (180, 320))
    assert writer.isOpened()
    # A 100ms green fragment followed by a red shot, then a blue shot at 2s.
    for i in range(120):
        color = (0, 240, 0) if i < 3 else (0, 0, 240) if i < 60 else (240, 0, 0)
        frame = np.full((320, 180, 3), color, dtype=np.uint8)
        cv2.circle(frame, (30 + i % 100, 70), 15, (255, 255, 255), -1)
        writer.write(frame)
    writer.release()
    return path


def config():
    return {"title": "fixture", "scenes": [
        {"id": "01", "start": 0, "end": 2, "exact_voice_text": "eat the last bite"},
        {"id": "02", "start": 2, "end": 4, "exact_voice_text": "return wrapper"}]}


def approved(scene, strip, config):
    assert strip.is_file()
    return {"action_visible": True, "result_visible": True, "relevance": 9,
            "observed_action": "visible action", "uncertain": False,
            "intrusive_source_text": False}


def test_real_video_scan_catches_three_frame_flash(movie):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("ffmpeg unavailable")
    cuts = quality.scan_cuts(movie, ffmpeg)
    assert cuts is not None
    assert any(abs(t - .1) < .04 for t in cuts)
    assert any(abs(t - 2) < .04 for t in cuts)
    report = quality.audit_export(config(), movie, ffmpeg, reviewer=approved)
    assert report["status"] == "needs_review"
    assert "01" in report["repair_queue"]
    assert movie.with_suffix(".editorial.json").is_file()


def test_own_cut_is_allowed_but_no_reviewer_cannot_pass(movie):
    report = quality.audit_export(config(), movie, None, reviewer=lambda *a: None,
                                  cut_scanner=lambda *a: [2])
    assert not any(i["type"] == "unplanned_cut" for i in report["issues"])
    assert report["status"] != "passed"
    assert report["unverified_scenes"] == ["01", "02"]


def test_review_failure_produces_report_and_preserves_export(movie):
    def failed(*args):
        raise TimeoutError()
    report = quality.audit_export(config(), movie, None, reviewer=failed,
                                  cut_scanner=lambda *a: None)
    assert report["status"] != "passed"
    assert movie.is_file()
    assert json.loads(movie.with_suffix(".editorial.json").read_text())["status"] != "passed"


def test_reselection_requires_fresh_action_evidence_and_avoids_occupied(movie, tmp_path):
    scene = config()["scenes"][0]
    no = lambda *a: {"action_visible": False, "observed_action": "grilling", "uncertain": False}
    assert quality.reselect_window(scene, movie, 0, 2, [.1, 2], 1, {}, tmp_path,
                                    reviewer=no) is None
    result = quality.reselect_window(scene, movie, 0, 2, [.1, 2], 1, {}, tmp_path,
                                      occupied=[(.1, 2)], reviewer=approved)
    assert result and result["start"] > 2
    assert result["end"] <= 4
    assert result["evidence"]["action_visible"] is True


def test_small_audio_tail_does_not_become_unreadable_footage(movie):
    c = config()
    c["scenes"][-1]["end"] = 4.08
    report = quality.audit_export(c, movie, None, reviewer=approved,
                                  cut_scanner=lambda *a: [2])
    assert not any(i["type"] == "unreadable_frames" for i in report["issues"])


def test_continuation_is_reviewed_for_its_new_line(movie, tmp_path):
    candidate = scrape_v4.Candidate("post1", "first action", str(movie), 2.1, 3.9, 8,
                                    "source", "available", vision={"reviewed_line": "old"})
    scene = {"id": "02", "start": 1, "end": 2, "sequence_id": "stall",
             "exact_voice_text": "hand back the wrapper"}
    prior = [{"sequence_id": "stall", "scrape_clip_id": "post1",
              "reviewed_window": {"start": .15, "end": 1.5}}]
    def reviewer(job, model):
        assert job[2].is_file()
        assert scene["exact_voice_text"] in job[3]
        return job[0], json.dumps({"accept": True, "relevance": 9, "continues_sequence": True,
                                  "action_visible": True, "observed_action": "hands wrapper back"}), ""
    results = scrape_v4._review_continuations(scene, [candidate], prior, tmp_path, "test", reviewer)
    assert len(results) == 1
    assert results[0].vision["reviewed_line"] == scene["exact_voice_text"]
    assert candidate.vision["reviewed_line"] == "old"  # original verdict is not overwritten


def test_quality_sidecar_travels_with_export(movie, tmp_path):
    import renders
    movie.with_suffix(".editorial.json").write_text('{"status":"unverified"}')
    with patch.object(renders, "RENDERS_DIR", tmp_path / "published"):
        copied = renders.publish(movie, project="fixture")
    assert copied.with_suffix(".editorial.json").is_file()


def test_plan_contract_survives_normalization_and_fallback():
    import agent_core
    raw = [{"exact_voice_text": "The customer returns the wrapper.", "start": 0, "end": 3,
            "editorial_role": "payoff", "sequence_id": "stall", "required_action": "return wrapper",
            "required_result": "vendor receives wrapper"}]
    beats = agent_core.normalize_micro_beat_plan(raw, "food", raw[0]["exact_voice_text"], 3)
    assert beats[0]["sequence_id"] == "stall"
    assert beats[0]["required_result"] == "vendor receives wrapper"
    assert agent_core.fallback_micro_beat_plan("food", raw[0]["exact_voice_text"], 3)


def test_renderer_repairs_flash_and_honors_explicit_same_source_inpoint(movie, tmp_path, monkeypatch):
    """Exercise the real decoder/encoder, not just a copy of preflight's logic."""
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg unavailable")
    import pipeline
    import renders
    monkeypatch.setattr(pipeline, "ROOT", tmp_path)
    monkeypatch.setattr(renders, "RENDERS_DIR", tmp_path / "deliveries")
    monkeypatch.delenv("WAVESPEED_API_KEY", raising=False)
    clips = tmp_path / "projects" / "editorial_fixture" / "seedance 2.0"
    clips.mkdir(parents=True)
    shutil.copy2(movie, clips / "fixture.mp4")
    c = config()
    c.update(project_slug="editorial_fixture", duration=4, fps=30, resolution=[180, 320],
             clip_source="scrape", render_captions=False, animated_captions=False,
             seedance_audio_in_final=False, sfx_enabled=False, background_music_enabled=False,
             grain=0, tint_alpha=0, dynamic_zoom=False)
    for i, scene in enumerate(c["scenes"]):
        scene.update(clip="fixture.mp4", seedance=True, seedance_start_trim=i * 2,
                     scrape_clip_id="same-source", asset="fixture.mp4")
    out = pipeline.render_video(c)
    assert out.is_file()
    assert c["editorial_preflight"][0]["status"] == "repaired"
    assert c["editorial_preflight"][1]["played_start"] == 2
    cap = cv2.VideoCapture(str(out))
    ok, first = cap.read()
    assert ok and first[:, :, 2].mean() > first[:, :, 1].mean() * 2
    cap.set(cv2.CAP_PROP_POS_MSEC, 2500)
    ok, second = cap.read()
    cap.release()
    assert ok and second[:, :, 0].mean() > second[:, :, 2].mean() * 2
    assert out.with_suffix(".editorial.json").is_file()
    assert c["editorial_quality"]["status"] != "passed"  # no semantic model was called
