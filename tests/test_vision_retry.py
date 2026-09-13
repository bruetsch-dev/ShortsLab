"""A slow HTTP call must not cost a source.

Measured across saved runs: 6 sources were rejected as "V4 vision review unavailable for this
source: TimeoutError" and 2 more with IndexError. Every one had already been searched for,
downloaded and passed every technical gate, and was then discarded because a single provider call
was slow. The download path has had a retry for exactly this reason since 2026-09-03; the vision
call did not.
"""
import agent_core
import scrape_v4

JOB = ("tiktok__1", None, "strip.jpg", "prompt text")


def _transport(monkeypatch, answers):
    """Replay `answers` in order: an Exception is raised, a string is returned as content."""
    calls = []

    def fake_post(_url, payload, timeout=None):
        calls.append(payload)
        outcome = answers[len(calls) - 1]
        if isinstance(outcome, Exception):
            raise outcome
        return {"choices": [{"message": {"content": outcome}}]}

    monkeypatch.setattr(agent_core, "post_json_url", fake_post)
    monkeypatch.setattr(agent_core, "image_data_url", lambda _p: "data:image/jpeg;base64,x")
    monkeypatch.setattr(scrape_v4.time, "sleep", lambda _s: None)
    return calls


def test_a_timeout_is_retried_and_can_succeed(monkeypatch):
    calls = _transport(monkeypatch, [TimeoutError("slow"), '{"accept":true}'])
    source_id, answer, error = scrape_v4.vision_verdict(JOB, "m")
    assert (source_id, error) == ("tiktok__1", "")
    assert answer == '{"accept":true}'
    assert len(calls) == 2


def test_it_gives_up_after_three_attempts_and_names_the_failure(monkeypatch):
    calls = _transport(monkeypatch, [TimeoutError(), TimeoutError(), TimeoutError()])
    _sid, answer, error = scrape_v4.vision_verdict(JOB, "m")
    assert (answer, error) == ("", "TimeoutError")
    assert len(calls) == 3, "three attempts, not an unbounded retry storm"


def test_a_first_time_success_costs_exactly_one_call(monkeypatch):
    calls = _transport(monkeypatch, ['{"accept":false}'])
    _sid, answer, error = scrape_v4.vision_verdict(JOB, "m")
    assert (answer, error) == ('{"accept":false}', "")
    assert len(calls) == 1


def test_a_reply_with_no_choices_is_also_retried(monkeypatch):
    """IndexError came from an answer carrying no choices - a blip, not a verdict."""
    calls = _transport(monkeypatch, [IndexError("no choices"), '{"accept":true,"relevance":8}'])
    _sid, answer, error = scrape_v4.vision_verdict(JOB, "m")
    assert error == "" and "relevance" in answer
    assert len(calls) == 2


def test_the_strip_and_the_model_reach_the_request(monkeypatch):
    calls = _transport(monkeypatch, ['{"accept":true}'])
    scrape_v4.vision_verdict(JOB, "google/gemini-3.7-flash")
    payload = calls[0]
    assert payload["model"] == "google/gemini-3.7-flash"
    assert payload["messages"][1]["content"][0]["text"] == "prompt text"
    assert payload["messages"][1]["content"][1]["image_url"]["url"].startswith("data:image/jpeg")


def test_the_retry_count_is_configurable_and_at_least_one(monkeypatch):
    calls = _transport(monkeypatch, [TimeoutError(), TimeoutError()])
    _sid, _answer, error = scrape_v4.vision_verdict(JOB, "m", attempts=1)
    assert error == "TimeoutError" and len(calls) == 1
