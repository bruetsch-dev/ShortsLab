from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import scrape_v4 as v4


def test_large_pool_is_bounded_and_shared_across_queries():
    rows = [v4.Candidate(str(source), f"query {query}", "unused.mp4", window, window + 3,
                         10, "", "available")
            for query in range(16) for source in range(20) for window in range(15)]
    selected = v4._shortlist_vision_candidates(rows)
    assert len(selected) == 96
    assert len({row.query for row in selected}) == 16
    assert all(sum(r.query == row.query and r.source_id == row.source_id for r in selected) <= 2
               for row in selected)
    assert all(row.status == "deferred" for row in rows if row not in selected)


def test_request_limit_is_persistent_and_thread_safe(tmp_path, monkeypatch):
    monkeypatch.setattr(v4, "VISION_REQUEST_LIMIT", 7)
    def reserve(index):
        try:
            v4._reserve_vision_request(tmp_path / "strip.jpg", "flash", str(index))
            return True
        except v4.VisionBudgetExceeded:
            return False
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(reserve, range(40))) == 7
    assert len((tmp_path / "v4_vision_requests.jsonl").read_text().splitlines()) == 7
    with pytest.raises(v4.VisionBudgetExceeded):
        v4._reserve_vision_request(tmp_path / "other.jpg", "flash", "recovery")


def test_each_window_gets_one_paid_vision_attempt_by_default():
    assert v4.VISION_ATTEMPTS == 1


def test_budget_fallback_stays_reviewed_and_is_marked_for_replacement():
    scene = {"text": "A hand locks the clear door", "editorial_role": "action",
             "required_action": "hand turns the lock", "required_result": "door locks"}
    candidate = v4.Candidate(
        "source", "clear toilet door", "clip.mp4", 0, 3, 9, "", "available",
        vision={"accept": True, "real_footage": True, "relevance": 8,
                "reviewed_line": "A different line", "action_visible": True,
                "observed_action": "person enters a clear toilet"})
    strict, _ = v4._pick_coverage_fill([candidate], {"clear toilet door"}, {}, [], [],
                                        need=2, scene=scene)
    fallback, marked = v4._pick_coverage_fill([candidate], {"clear toilet door"}, {}, [], [],
                                               need=2, scene=scene, allow_reviewed_fallback=True)
    assert strict is None
    assert fallback is candidate
    assert marked is True


def test_budget_drains_active_results_without_exceeding_limit(tmp_path, monkeypatch):
    import threading
    monkeypatch.setattr(v4, "VISION_REQUEST_LIMIT", 2)
    stopped = threading.Event()
    def verdict(job, model):
        key, _, path, _ = job
        try:
            v4._reserve_vision_request(path, model, key)
        except v4.VisionBudgetExceeded:
            stopped.set()
            raise
        assert stopped.wait(5)
        return key, '{"accept": true}', ""
    monkeypatch.setattr(v4, "vision_verdict", verdict)
    jobs = [(str(i), None, tmp_path / "review" / f"{i}.jpg", "") for i in range(30)]
    results = v4._collect_vision_jobs(jobs, "offline")
    assert len(results) == 30
    assert sum(not error for _, _, error in results.values()) == 2
    assert sum(error == "VisionBudgetExceeded" for _, _, error in results.values()) == 28
    assert v4._vision_budget_spent(tmp_path)
    assert len((tmp_path / "review/v4_vision_requests.jsonl").read_text().splitlines()) == 2


def test_exhausted_budget_preserves_reviewed_candidates(tmp_path, monkeypatch):
    monkeypatch.setattr(v4, "VISION_REQUEST_LIMIT", 0)
    evidence = {"accept": True, "reviewed_line": "An intersection"}
    reviewed = v4.Candidate("a", "intersection", "unused.mp4", 0, 3, 8, "", "available", vision=evidence)
    pending = v4.Candidate("b", "intersection", "unused.mp4", 0, 3, 8, "", "available")
    v4._vision_source_review([reviewed, pending], tmp_path)
    assert reviewed.status == "available"
    assert reviewed.vision == evidence
    assert pending.status == "deferred"
    assert v4._review_for_missing_line({}, [reviewed], tmp_path, "offline") == []
    assert v4._review_continuations({}, [reviewed], [], tmp_path, "offline") == []


@pytest.mark.parametrize("complete", [True, False])
def test_assignment_checkpoint_keeps_timeline_and_missing_beats(tmp_path, monkeypatch, complete):
    import json
    monkeypatch.setattr(v4, "VISION_REQUEST_LIMIT", 0)
    scenes = [{"id": 1, "text": "A", "start": 0, "end": 2},
              {"id": 2, "text": "B", "start": 2, "end": 4}]
    clips = ["saved-a.mp4", "saved-b.mp4" if complete else None]
    report = {}
    v4._save_assignment_checkpoint(tmp_path, scenes, clips, report)
    saved = json.loads((tmp_path / "review/scrape_v4_checkpoint.json").read_text())
    assert saved["scenes"] == scenes
    assert saved["scene_clips"] == clips
    assert saved["vision_budget_exhausted"] is True
    assert saved["status"] == ("ready" if complete else "needs_footage")
    assert [row["id"] for row in saved["missing_beats"]] == ([] if complete else [2])


def test_review_applies_success_even_when_another_job_hits_budget(tmp_path, monkeypatch):
    import cv2
    import numpy as np
    import json
    class Capture:
        def __init__(self, path):
            self.frame = 0
        def get(self, prop):
            return 90 if prop == cv2.CAP_PROP_FRAME_COUNT else 30
        def set(self, prop, value):
            self.frame = value
        def read(self):
            return True, np.full((40, 40, 3), 60 + self.frame, dtype=np.uint8)
        def release(self):
            pass
    monkeypatch.setattr(cv2, "VideoCapture", Capture)
    monkeypatch.setattr(v4, "synthetic_watermark", lambda frames: "")
    monkeypatch.setattr(v4.clip_scraper, "_ffmpeg_tools", lambda: (None, None))
    def verdict(job, model):
        key = job[0]
        if key.startswith("b@"):
            raise v4.VisionBudgetExceeded()
        return key, json.dumps({"accept": True, "real_footage": True, "relevance": 9,
                                 "action_visible": True, "observed_action": "traffic moving"}), ""
    monkeypatch.setattr(v4, "vision_verdict", verdict)
    rows = [v4.Candidate(key, "traffic", "unused.mp4", 0, 3, 8, "", "available") for key in ("a", "b")]
    v4._vision_source_review(rows, tmp_path, briefs={"traffic": {"text": "Traffic", "editorial_role": "context"}})
    assert rows[0].status == "available"
    assert rows[0].vision["reviewed_line"] == "Traffic"
    assert rows[1].status == "deferred"
    assert (tmp_path / "review/v4_vision_source_review_response.json").exists()


def test_final_vision_transport_keeps_budget_and_logs_usage(tmp_path, monkeypatch):
    import json
    import agent_core
    monkeypatch.setattr(agent_core, "assert_paid_api_allowed", lambda url: None)
    monkeypatch.setattr(agent_core, "image_data_url", lambda p: "data:image/jpeg;base64,offline")
    usage = {"prompt_tokens": 900, "completion_tokens": 300,
             "completion_tokens_details": {"reasoning_tokens": 80}}
    class Response:
        headers = {"Content-Type": "application/json"}
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self):
            return json.dumps({"model": "google/gemini-3.7-flash", "usage": usage,
                               "choices": [{"message": {"content": '{"accept": true}'}}]}).encode()
    sent = []
    def transport(req, timeout):
        sent.append(json.loads(req.data))
        return Response()
    monkeypatch.setattr(agent_core.urllib.request, "urlopen", transport)
    result = v4.vision_verdict(("window", None, tmp_path / "strip.jpg", "Judge"), "google/gemini-3.7-flash")
    assert result[2] == ""
    assert len(sent) == 1
    assert sent[0]["model"] == "google/gemini-3.7-flash"
    assert sent[0]["max_tokens"] == 1024
    assert sent[0]["reasoning"] == {"effort": "minimal"}
    assert not any(k.startswith("_") for k in sent[0])
    saved = json.loads((tmp_path / "v4_vision_results.jsonl").read_text())
    assert saved["response"]["usage"] == usage


def test_a_failed_vision_transport_uses_one_reserved_attempt(tmp_path, monkeypatch):
    import io
    import urllib.error
    import agent_core
    monkeypatch.setattr(agent_core, "assert_paid_api_allowed", lambda url: None)
    monkeypatch.setattr(agent_core, "image_data_url", lambda p: "offline")
    monkeypatch.setattr(v4.time, "sleep", lambda delay: None)
    sent = []
    def transport(req, timeout):
        sent.append(req)
        raise urllib.error.HTTPError(req.full_url, 503, "offline", {}, io.BytesIO(b"unavailable"))
    monkeypatch.setattr(agent_core.urllib.request, "urlopen", transport)
    result = v4.vision_verdict(("window", None, tmp_path / "strip.jpg", "Judge"), "google/gemini-3.7-flash")
    assert result[2] == "HTTPError"
    assert len(sent) == 1
    assert len((tmp_path / "v4_vision_requests.jsonl").read_text().splitlines()) == len(sent)


def test_sse_usage_survives_final_chunk_without_usage():
    import json
    import agent_core
    chunks = [{"model": "flash", "usage": {"prompt_tokens": 123, "completion_tokens": 45},
               "choices": [{"delta": {"content": "{}"}}]},
              {"choices": [{"finish_reason": "stop", "delta": {}}]}]
    result = agent_core._parse_sse_chat_stream("\n".join("data: " + json.dumps(x) for x in chunks))
    assert result["usage"]["prompt_tokens"] == 123
    assert result["model"] == "flash"


def test_balance_preflight_uses_selected_model(monkeypatch):
    import agent_core
    monkeypatch.setenv("WAVESPEED_API_KEY", "offline")
    calls = []
    monkeypatch.setattr(agent_core, "post_json_url", lambda url, payload, **kw: calls.append(payload))
    agent_core.assert_wavespeed_balance(model="google/gemini-3.7-flash")
    assert len(calls) == 1
    assert calls[0]["model"] == "google/gemini-3.7-flash"
    assert calls[0]["_strict_token_limit"] is True


def test_collaboration_does_not_default_to_premium_models(monkeypatch):
    import agent_core
    calls = []
    def llm(model, *args, **kwargs):
        calls.append(model)
        return {"ok": True}
    monkeypatch.setattr(agent_core, "_post_llm_json", llm)
    agent_core.collaborate_json([{"role": "user", "content": "offline"}])
    assert calls == ["google/gemini-3.7-flash"] * 2
