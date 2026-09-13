import inspect
from pathlib import Path

import challenge_mode
import app


def test_generate_topics_returns_exactly_twenty_distinct_choices(monkeypatch):
    captured = {}

    def fake_llm(model, messages, max_tokens, temperature, timeout):
        captured["messages"] = messages
        return {"topics": [
            {"title": f"I tested history idea {i}", "hook": f"Visible joke {i}",
             "format": "challenge"}
            for i in range(20)
        ]}

    monkeypatch.setattr(challenge_mode.agent_core, "_post_llm_json", fake_llm)
    topics = challenge_mode.generate_topics("ancient Japan")

    assert len(topics) == 20
    assert len({item["title"] for item in topics}) == 20
    assert "ancient Japan" in captured["messages"][1]["content"]
    assert "I survived one day as a Roman slave" in captured["messages"][1]["content"]


def test_preview_stops_at_voice_approval_without_video_requests(
        monkeypatch, tmp_path):
    sheet = tmp_path / "orange-character.webp"
    sheet.write_bytes(b"fake image bytes")

    def fake_llm(model, messages, max_tokens, temperature, timeout):
        return {
            "title": "I Took a Samurai to IKEA",
            "script": "What if a samurai had to build flat-pack furniture? No retainers. "
                      "No servants. Only one tiny hex key. Here's what happened. "
                      + "The task gets worse every hour. " * 20,
            "voice_direction": "Fast, dry, increasingly frustrated.",
            "continuity": "Same blue robe and orange cap.",
            "clips": [
                {"label": f"Beat {i}", "narration": f"Line {i}",
                 "prompt": f"Photorealistic live action. @image1 is the exact hero. "
                           f"Shot 1 (5s): action {i}. Shot 2 (5s): reaction {i}. Natural sound."}
                for i in range(1, 9)
            ],
        }

    monkeypatch.setattr(challenge_mode.agent_core, "_post_llm_json", fake_llm)
    result = challenge_mode.build_preview(
        {"title": "I Took a Samurai to IKEA", "format": "visitor"}, "dry humor", sheet)

    assert result["dry_run"] is False
    assert result["video_api_called"] is False
    assert result["generation_state"] == "voice_pending_approval"
    assert result["model"] == "bytedance/seedance-2.5/text-to-video-turbo"
    assert result["requests"] == []
    assert [row["label"] for row in result["clips"]] == [
        "Hook", "Day 1", "Day 2", "Day 3", "Day 4", "Day 5", "Day 6", "Ending"]
    assert result["character_sheet"]["path"] == str(sheet.resolve())
    assert "cut_timeline" not in result


def test_preview_injects_character_reference_when_writer_omits_it(monkeypatch, tmp_path):
    sheet = tmp_path / "character.png"
    sheet.write_bytes(b"png")

    monkeypatch.setattr(challenge_mode.agent_core, "_post_llm_json", lambda *a, **k: {
        "script": "Here's what happened. " + "Visible action. " * 50,
        "clips": [{"label": str(i), "prompt": "Shot 1 (10s): a visible action."}
                  for i in range(8)],
    })
    result = challenge_mode.build_preview({"title": "A challenge"}, "", sheet)

    assert all(clip["prompt"].startswith("Use @image1") for clip in result["clips"])


def test_preview_rejects_day_seven(monkeypatch, tmp_path):
    sheet = tmp_path / "character.png"
    sheet.write_bytes(b"png")
    monkeypatch.setattr(challenge_mode.agent_core, "_post_llm_json", lambda *a, **k: {
        "script": "Day: 7, this should never happen.",
        "clips": [{"label": str(i), "prompt": "@image1. Shot 1 (10s): action."}
                  for i in range(8)],
    })

    try:
        challenge_mode.build_preview({"title": "A challenge"}, "", sheet)
    except RuntimeError as exc:
        assert "Day 7" in str(exc)
    else:
        raise AssertionError("Day 7 was accepted")


def test_editor_requires_exactly_eight_sources(tmp_path):
    clips = []
    for index in range(7):
        path = tmp_path / f"{index}.mp4"
        path.write_bytes(b"video")
        clips.append(path)
    try:
        challenge_mode.edit_generated_clips(clips, {"script": "hello"}, tmp_path / "project")
    except RuntimeError as exc:
        assert "exactly 8" in str(exc)
    else:
        raise AssertionError("Seven sources reached the editor")


def test_voiceover_timeline_uses_narration_weight_and_never_exceeds_source_cap():
    preview = {"clips": [
        {"label": "Hook", "narration": "short hook"},
        *[{"label": f"Day {i}", "narration": "a medium narration section with visible action"}
          for i in range(1, 7)],
        {"label": "Ending", "narration": "a much longer final narration section with a callback and a dry modern comparison at the end"},
    ]}
    timeline = challenge_mode._narration_timeline(preview, 61.0)

    assert len(timeline) == 8
    assert timeline[0]["seconds"] < timeline[-1]["seconds"]
    assert max(row["seconds"] for row in timeline) <= 10.0
    assert timeline[-1]["end"] == 61.0
    assert timeline[1]["start"] == timeline[0]["end"]


def test_preview_voiceover_uses_seed_tts_but_waits_to_timestamp(monkeypatch, tmp_path):
    captured = {}
    preview = {"script": "A complete challenge voiceover.", "voice_direction": "Dry.",
               "clips": [{"label": str(i), "narration": f"section {i}"} for i in range(8)],
               "edit_plan": {}}

    def fake_tts(text, path, **kwargs):
        captured.update(kwargs)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"voice")
        return path

    monkeypatch.setattr(challenge_mode.pipeline, "generate_speech_gemini", fake_tts)
    monkeypatch.setattr(challenge_mode, "_duration", lambda _path: 56.0)
    result = challenge_mode.generate_preview_voiceover(preview, tmp_path)

    assert captured["model"] == "bytedance/seed-speech-tts-2.0"
    assert captured["voice"] == "tim_en"
    assert captured["tts_speed"] == 1.25
    assert captured["volume"] == 1.05
    assert result["voiceover"]["duration"] == 56.0
    assert result["voiceover"]["speed"] == 1.25
    assert result["voiceover"]["volume"] == 1.05
    assert "cut_timeline" not in result
    assert "Pending approval" in result["edit_plan"]["timing"]


def test_challenge_route_opens_the_new_v3_station():
    assert app.shell_v3_initial("/challenge", {}) == {"flow": "challenge"}
    page = app.shell_v3.page({"flow": "challenge"}).decode("utf-8")
    assert "/static/shell-v3.js" in page


def test_own_topic_is_handed_to_the_writer_word_for_word(monkeypatch, tmp_path):
    sheet = tmp_path / "character.png"
    sheet.write_bytes(b"png")
    captured = {}

    def fake_llm(model, messages, max_tokens, temperature, timeout):
        captured["ask"] = messages[1]["content"]
        return {"title": "The Writer Renamed It",
                "script": "Here's what happened. " + "Visible action. " * 40,
                "clips": [{"label": str(i), "prompt": "@image1. Shot 1 (10s): action."}
                          for i in range(8)]}

    monkeypatch.setattr(challenge_mode.agent_core, "_post_llm_json", fake_llm)
    topic = challenge_mode.custom_topic("  I raced a Mongol horse archer to a pit stop  ")
    result = challenge_mode.build_preview(topic, "", sheet)

    assert topic["title"] == "I raced a Mongol horse archer to a pit stop"
    assert topic["custom"] is True
    assert "I raced a Mongol horse archer to a pit stop" in captured["ask"]
    assert "do not rename it" in captured["ask"]
    assert result["topic"] == topic
    assert result["title"] == topic["title"]


def test_a_generated_topic_is_not_marked_custom(monkeypatch, tmp_path):
    sheet = tmp_path / "character.png"
    sheet.write_bytes(b"png")
    captured = {}

    def fake_llm(model, messages, max_tokens, temperature, timeout):
        captured["ask"] = messages[1]["content"]
        return {"script": "Here's what happened. " + "Visible action. " * 40,
                "clips": [{"label": str(i), "prompt": "@image1. Shot 1 (10s): action."}
                          for i in range(8)]}

    monkeypatch.setattr(challenge_mode.agent_core, "_post_llm_json", fake_llm)
    result = challenge_mode.build_preview({"title": "I took a Viking to IKEA", "format": "visitor"},
                                          "", sheet)

    assert "do not rename it" not in captured["ask"]
    assert result["title"] == "I took a Viking to IKEA"


def test_custom_topic_needs_a_title():
    for empty in ("", "   ", None):
        try:
            challenge_mode.custom_topic(empty)
        except RuntimeError as exc:
            assert "topic" in str(exc).lower()
        else:
            raise AssertionError(f"An empty own topic was accepted: {empty!r}")


def test_resolve_topic_prefers_the_words_the_user_typed():
    own = challenge_mode.resolve_topic("I taught a samurai to parallel park", '{"title": "ignored"}')
    assert own["title"] == "I taught a samurai to parallel park"
    assert own["custom"] is True

    picked = challenge_mode.resolve_topic("", '{"title": "I took a Viking to IKEA"}')
    assert picked["title"] == "I took a Viking to IKEA"
    assert not picked.get("custom")

    for own_value, raw in (("", "not json"), ("", "{}"), ("", "")):
        try:
            challenge_mode.resolve_topic(own_value, raw)
        except RuntimeError:
            pass
        else:
            raise AssertionError(f"An unusable topic was accepted: {raw!r}")


def test_generate_topics_drops_filler_and_repairs_to_twenty(monkeypatch):
    calls = []

    def fake_llm(model, messages, max_tokens, temperature, timeout):
        calls.append(messages[1]["content"])
        if len(calls) == 1:
            return {"topics": (
                [{"title": f"I took a Spartan to place {i}", "hook": f"Visible beat {i}",
                  "format": "visitor"} for i in range(18)]
                + [{"title": "A Day in the Life of a Roman", "hook": "Nothing happens",
                    "format": "survival"},
                   {"title": "I took a Spartan to place 1", "hook": "Repeated card",
                    "format": "visitor"}])}
        return {"topics": [{"title": f"I survived the job of scribe {i}", "hook": f"Beat {i}",
                            "format": "survival"} for i in range(2)]}

    monkeypatch.setattr(challenge_mode.agent_core, "_post_llm_json", fake_llm)
    topics = challenge_mode.generate_topics("")

    assert len(topics) == 20
    assert len({row["title"] for row in topics}) == 20
    assert all("day in the life" not in row["title"].casefold() for row in topics)
    assert len(calls) == 2
    assert "do not repeat" in calls[1]
    assert "I took a Spartan to place 1" in calls[1]


def test_generate_topics_still_refuses_a_short_set(monkeypatch):
    def fake_llm(model, messages, max_tokens, temperature, timeout):
        return {"topics": [{"title": f"I took a Spartan to place {i}", "hook": f"Beat {i}",
                            "format": "visitor"} for i in range(19)]}

    monkeypatch.setattr(challenge_mode.agent_core, "_post_llm_json", fake_llm)
    try:
        challenge_mode.generate_topics("")
    except RuntimeError as exc:
        assert "19 usable ideas" in str(exc)
    else:
        raise AssertionError("A 19-idea set was accepted as 20")


def test_the_challenge_screen_sends_the_own_topic_field_the_route_reads():
    js = (Path(app.__file__).resolve().parent / "static" / "shell-v3.js").read_text(encoding="utf-8")
    assert 'fd.append("own_topic"' in js
    assert "challenge-own-row" in js
    assert "own_topic" in inspect.getsource(app.Handler.do_POST)
    # Step 3 is gated on the character sheet and a chosen topic, so the screen must say which one
    # is missing instead of leaving a greyed-out button unexplained.
    assert "previewBtn.disabled = !selected || !FILES.challenge_character" in js
    assert "Locked — step 1 still needs the character sheet" in js
    assert "Locked — pick one topic card above to continue." in js
    assert 'drop.classList.add("stale")' in js


def _multipart_form(fields, files):
    """Minimal multipart body, so the real Handler can be driven without a browser."""
    boundary = "----challenge-test-boundary"
    chunks = []
    for name, value in fields.items():
        chunks.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
                       f"{value}\r\n").encode("utf-8"))
    for name, (filename, data) in files.items():
        chunks.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; '
                       f'filename="{filename}"\r\nContent-Type: application/octet-stream\r\n\r\n'
                       ).encode("utf-8") + data + b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(chunks), boundary


def _post_challenge_preview(fields, files):
    """Drive the real Handler over loopback and return its decoded JSON reply."""
    import json as jsonlib
    import threading
    import urllib.request
    from http.server import ThreadingHTTPServer

    body, boundary = _multipart_form(fields, files)
    server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_address[1]}/challenge-preview", data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        with urllib.request.urlopen(request, timeout=30) as response:
            return jsonlib.loads(response.read().decode("utf-8"))
    finally:
        server.shutdown()
        server.server_close()


def _post_json(path, payload):
    import json as jsonlib
    import threading
    import urllib.request
    from http.server import ThreadingHTTPServer

    server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_address[1]}{path}",
            data=jsonlib.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return jsonlib.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return jsonlib.loads(exc.read().decode("utf-8"))
    finally:
        server.shutdown()
        server.server_close()


def test_preview_route_keeps_the_topic_the_user_typed(monkeypatch, tmp_path):
    """The exact request the screen sends: own_topic in, the user's own title out."""
    captured = {}

    def fake_llm(model, messages, max_tokens, temperature, timeout):
        captured["ask"] = messages[1]["content"]
        return {"title": "Renamed By The Writer",
                "script": "Here's what happened. " + "Visible action. " * 40,
                "clips": [{"label": str(i), "prompt": "@image1. Shot 1 (10s): action."}
                          for i in range(8)]}

    def fake_tts(text, path, **kwargs):
        captured["tts"] = kwargs
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(b"voice")
        return path

    monkeypatch.setattr(challenge_mode.agent_core, "_post_llm_json", fake_llm)
    monkeypatch.setattr(challenge_mode.pipeline, "generate_speech_gemini", fake_tts)
    monkeypatch.setattr(challenge_mode, "_duration", lambda _path: 56.0)
    monkeypatch.setattr(app, "ROOT", tmp_path)

    out = _post_challenge_preview(
        {"direction": "dry humour", "own_topic": "I taught a samurai to parallel park"},
        {"character_sheet": ("hero.png", b"png-bytes")})

    assert out["ok"] is True, out
    assert out["topic"]["custom"] is True
    assert out["title"] == "I taught a samurai to parallel park"
    assert out["script"].startswith("Here's what happened.")
    assert "I taught a samurai to parallel park" in captured["ask"]
    assert out["voiceover"]["duration"] == 56.0
    assert out["voiceover"]["voice"] == "tim_en"
    assert out["voiceover"]["speed"] == 1.25
    assert out["voiceover"]["volume"] == 1.05
    assert captured["tts"]["tts_speed"] == 1.25
    assert captured["tts"]["volume"] == 1.05
    assert out["requests"] == []
    assert "cut_timeline" not in out
    assert (tmp_path / "outputs" / "challenge_previews").is_dir()


def test_approved_voice_is_word_aligned_before_timed_requests(monkeypatch, tmp_path):
    voice = tmp_path / "voice.mp3"
    voice.write_bytes(b"voice")
    clips = [{"index": i + 1, "label": ("Hook" if i == 0 else "Ending" if i == 7 else f"Day {i}"),
              "narration": f"word{i * 2} word{i * 2 + 1}",
              "prompt": f"Photorealistic. @image1 performs action {i + 1}."}
             for i in range(8)]
    script_words = [f"word{i}" for i in range(16)]
    preview = {"script": " ".join(script_words), "clips": clips,
               "voiceover": {"path": str(voice), "duration": 64.0}}
    asr = [{"word": word, "start": i * 4.0, "end": i * 4.0 + 1.0}
           for i, word in enumerate(script_words)]
    monkeypatch.setattr(challenge_mode.voice_align, "transcribe_words", lambda *a, **k: asr)

    timeline = challenge_mode.timestamp_approved_voice(preview, tmp_path / "timing")
    requests = challenge_mode.build_timed_requests(preview, "https://assets.example/hero.png")

    assert len(timeline) == len(requests) == 8
    assert timeline[0]["start"] == 0.0
    assert timeline[-1]["end"] == 64.0
    assert all(row["seconds"] == 8.0 for row in timeline)
    assert (tmp_path / "timing" / "approved_voice_timestamps.json").is_file()
    for request, timing in zip(requests, timeline):
        body = request["body"]
        assert body["reference_images"] == ["https://assets.example/hero.png"]
        assert body["aspect_ratio"] == "9:16"
        assert body["resolution"] == "720p"
        assert body["duration"] == 10
        assert body["generate_audio"] is True
        assert f"cut at {timing['seconds']:.2f} seconds" in body["prompt"]
        assert "@image1" in body["prompt"]


def test_live_generation_submits_exactly_eight_then_edits(monkeypatch, tmp_path):
    voice = tmp_path / "voice.mp3"; voice.write_bytes(b"voice")
    character = tmp_path / "hero.png"; character.write_bytes(b"hero")
    clips = [{"index": i + 1, "label": ("Hook" if i == 0 else "Ending" if i == 7 else f"Day {i}"),
              "narration": f"section {i}", "prompt": f"@image1 action {i}"}
             for i in range(8)]
    preview = {"title": "Test", "script": "one two three four five six seven eight",
               "clips": clips, "voiceover": {"path": str(voice), "duration": 56.0},
               "character_sheet": {"path": str(character)}}
    calls = []
    monkeypatch.setattr(challenge_mode, "timestamp_approved_voice", lambda p, *a, **k: p.update({
        "cut_timeline": [{"index": i + 1, "label": clips[i]["label"], "start": i * 7.0,
                          "end": (i + 1) * 7.0, "seconds": 7.0,
                          "spoken_text": f"section {i}"} for i in range(8)]}) or p["cut_timeline"])
    monkeypatch.setattr(challenge_mode, "retime_prompts_for_approved_voice",
                        lambda p, *a, **k: p["clips"])
    monkeypatch.setattr(challenge_mode.pipeline, "api_key", lambda: "key")
    monkeypatch.setattr(challenge_mode.pipeline, "upload_media",
                        lambda *a: ("https://assets.example/hero.png", {"ok": True}))
    def submit(method, url, key, body, timeout=0):
        calls.append(body)
        return {"data": {"id": f"job-{len(calls)}"}}
    monkeypatch.setattr(challenge_mode.pipeline, "request_json", submit)
    monkeypatch.setattr(challenge_mode.pipeline, "poll_wavespeed",
                        lambda prediction_id, *a, **k: ([f"https://x/{prediction_id}.mp4"], {"status": "completed"}))
    def download(url, path):
        Path(path).write_bytes(b"video")
    monkeypatch.setattr(challenge_mode.pipeline, "download_file", download)
    monkeypatch.setattr(challenge_mode, "edit_generated_clips",
                        lambda paths, p, project, **k: {"video": "final.mp4", "clips": list(map(str, paths))})

    report = challenge_mode.generate_and_edit(preview, tmp_path / "project")

    assert len(calls) == 8
    assert len(report["generation_reports"]) == 8
    assert report["voice_approved"] is True
    assert [row["body"]["duration"] for row in report["requests"]] == [10] * 8


def test_prompt_retimer_receives_the_approved_windows(monkeypatch):
    captured = {}
    clips = [{"index": i + 1, "label": ("Hook" if i == 0 else "Ending" if i == 7 else f"Day {i}"),
              "narration": f"spoken section {i}", "prompt": f"@image1 old clock {i}"}
             for i in range(8)]
    preview = {"clips": clips, "cut_timeline": [
        {"start": i * 7.0, "end": (i + 1) * 7.0, "seconds": 7.0,
         "spoken_text": f"aligned section {i}"} for i in range(8)]}
    def fake_llm(model, messages, **kwargs):
        captured["material"] = messages[1]["content"]
        return {"clips": [{"index": i + 1,
                            "prompt": f"@image1. Shot 1 (7s): payoff {i}. Shot 2 (3s): hold."}
                           for i in range(8)]}
    monkeypatch.setattr(challenge_mode.agent_core, "_post_llm_json", fake_llm)

    result = challenge_mode.retime_prompts_for_approved_voice(preview)

    assert len(result) == 8
    assert '"cut_after_seconds": 7.0' in captured["material"]
    assert '"spoken_text": "aligned section 0"' in captured["material"]
    assert result[0]["draft_prompt"] == "@image1 old clock 0"
    assert "Shot 1 (7s)" in result[0]["prompt"]


def test_challenge_ui_uses_approval_gate_and_no_manual_clip_slots():
    js = (Path(app.__file__).resolve().parent / "static" / "shell-v3.js").read_text(encoding="utf-8")
    assert "Approve voice & generate short" in js
    assert 'fetch("/challenge-generate"' in js
    assert "VOICE APPROVAL REQUIRED" in js
    assert "FILES.challenge_clips" not in js


def test_preview_route_refuses_a_blank_own_topic_without_a_card(monkeypatch, tmp_path):
    """An empty box must come back as an error, not as a random script."""
    monkeypatch.setattr(app, "ROOT", tmp_path)
    monkeypatch.setattr(challenge_mode.agent_core, "_post_llm_json",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("the writer was called")))

    out = _post_challenge_preview({"own_topic": "   ", "topic": ""},
                                  {"character_sheet": ("hero.png", b"png-bytes")})

    assert out["ok"] is False
    assert "topic" in out["error"].lower()


def test_generate_route_requires_approval_and_rebuilds_browser_timing(monkeypatch, tmp_path):
    stage = tmp_path / "outputs" / "challenge_previews" / "take"
    stage.mkdir(parents=True)
    voice = stage / "seed_voiceover.mp3"; voice.write_bytes(b"voice")
    character = stage / "hero.png"; character.write_bytes(b"hero")
    preview = {
        "title": "Test", "voiceover": {"path": str(voice)},
        "character_sheet": {"path": str(character),
                            "sha256": challenge_mode.hashlib.sha256(b"hero").hexdigest()},
        "cut_timeline": [{"fake": True}], "word_timestamps": [{"fake": True}],
        "requests": [{"fake": True}], "video_api_called": True,
    }
    captured = {}
    monkeypatch.setattr(app, "ROOT", tmp_path)
    monkeypatch.setattr(app, "start_challenge_generation_job",
                        lambda clean: captured.update(clean) or "job-1")

    denied = _post_json("/challenge-generate", {"approved": False, "preview": preview})
    accepted = _post_json("/challenge-generate", {"approved": True, "preview": preview})

    assert denied["ok"] is False
    assert "approve" in denied["error"].lower()
    assert accepted == {"ok": True, "job_id": "job-1"}
    assert "cut_timeline" not in captured
    assert "word_timestamps" not in captured
    assert captured["requests"] == []
    assert captured["video_api_called"] is False
