"""The v3 shell is opt-in, and everything about it has to stay additive.

`shell_v3.py`, `static/shell-v3.css` and `static/shell-v3.js` were written before anything
referenced them: no route served the document, no route served the two assets, and /job-status
never produced the block the wait screen reads. This file covers the wiring that was added, and
the two properties that must not rot:

1. A request WITHOUT `?ui=v3` must be answered exactly as before - same document for the chat
   shell, same document for `?legacy_ui=1`, and a /job-status payload with no extra keys. The
   owner runs production on that interface.
2. The /run payload is a contract. v3 must not be able to drift from it, so it is handed the
   SAME manifest object the chat shell gets; this checks the two boot documents agree.

Behaviour, not source text: the routes are driven through the real `app.Handler.do_GET`, and the
health counters are fed the log lines the providers actually emit (measured from
scrapedo_tiktok.search / brightdata_tiktok.search_many / scrape_v4._authenticated_tiktok_records).
"""

import json
import os
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app
import chat_ui
import shell_v3


class Fake(app.Handler):
    """The real handler with the socket taken out: do_GET runs, the reply is captured."""

    def __init__(self, path):                      # noqa: D107 - deliberately not BaseHTTPRequestHandler's
        self.path = path
        self.body = b""
        self.content_type = ""
        self.error = None

    def send_bytes(self, data, content_type="text/html; charset=utf-8"):
        self.body = data
        self.content_type = content_type

    def send_error(self, code, *args, **kwargs):
        self.error = code


def get(path):
    handler = Fake(path)
    handler.do_GET()
    return handler


def boot_of(document):
    """The JSON the v3 document hands its script, or None when this is not a v3 document."""
    text = document.decode("utf-8")
    match = re.search(r'<script id="v3-boot" type="application/json">(.*?)</script>', text, re.S)
    return json.loads(match.group(1)) if match else None


def chat_boot_of(document):
    text = document.decode("utf-8")
    match = re.search(r'<script id="chat-boot" type="application/json">(.*?)</script>', text, re.S)
    return json.loads(match.group(1)) if match else None


class RoutesOptIn(unittest.TestCase):

    def test_the_home_route_serves_v3_by_default(self):
        """Since 2026-09-05 v3 IS the interface; the older two are one query away."""
        self.assertIsNotNone(boot_of(get("/").body))
        self.assertIsNotNone(boot_of(get("/?ui=v3").body))
        self.assertIsNone(boot_of(get("/?ui=chat").body), "?ui=chat must still reach the chat shell")

    def test_legacy_ui_is_retired(self):
        """?legacy_ui=1 used to serve the classic forms. That page does not survive the studio
        restyle - it renders its markup and shows nothing but the title bar - and a blank screen
        is worse than no screen, so the parameter is now ignored and v3 answers."""
        self.assertIsNotNone(boot_of(get("/?legacy_ui=1").body))
        self.assertIsNotNone(boot_of(get("/?ui=v3&legacy_ui=1").body))

    def test_a_job_link_opens_the_wait_screen_on_that_job(self):
        boot = boot_of(get("/job?id=abc123&ui=v3").body)
        self.assertEqual(boot["initial"], {"job": "abc123"})

    def test_the_master_routes_carry_their_flow(self):
        for path, flow in (("/sfx", "sfx"), ("/visual", "visual"), ("/captions", "captions")):
            boot = boot_of(get(path + "?ui=v3").body)
            self.assertEqual(boot["initial"], {"flow": flow}, path)

    def test_a_project_deep_link_survives(self):
        boot = boot_of(get("/?ui=v3&project=some_slug").body)
        self.assertEqual(boot["initial"], {"project": "some_slug"})
        # "new=1" is the shape the app links use (/?new=1); parse_qs drops a bare "?new".
        self.assertEqual(boot_of(get("/?ui=v3&new=1").body)["initial"], {"new": True})

    def test_every_window_route_is_answered_by_v3(self):
        """Superseded on 2026-09-05. This test used to assert the opposite: that /assets,
        /longform and /actionedit fall through to the chat shell, because boot()
        had no screen for them. They have one now (tests/test_shell_v3_windows.py measures each),
        and a fall-through would land the user in the old look - the exact path the owner
        rejected. Only the named escape hatches may reach the older interfaces."""
        for path in ("/assets", "/longform", "/actionedit", "/reddit"):
            self.assertIsNotNone(shell_v3_initial_or_none(path), path)
            self.assertIsNotNone(boot_of(get(path + "?ui=v3").body), path)
            self.assertIsNone(boot_of(get(path + "?ui=chat").body), path + " must still reach the chat shell")

    def test_a_hostile_deep_link_cannot_break_out_of_the_boot_block(self):
        """The query string lands inside a <script> block, so `</script>` in it must stay data."""
        body = get("/?ui=v3&project=" + "%3C/script%3E%3Cscript%3Ealert(1)%3C/script%3E").body
        boot = boot_of(body)
        self.assertIn("alert(1)", boot["initial"]["project"])
        raw = re.search(r'<script id="v3-boot"[^>]*>(.*?)</script>', body.decode("utf-8"), re.S)
        self.assertNotIn("</script", raw.group(1), "the block can be closed from the query string")

    def test_only_the_named_old_interfaces_opt_out(self):
        """Anything that is not a request for an older shell lands on the default one."""
        self.assertIsNotNone(boot_of(get("/?ui=v4").body))
        self.assertIsNotNone(boot_of(get("/?ui=").body))
        for older in ("chat", "classic", "v2"):
            self.assertIsNone(boot_of(get("/?ui=" + older).body), older)


def shell_v3_initial_or_none(path):
    return app.shell_v3_initial(path, {})


class Assets(unittest.TestCase):

    def test_both_v3_assets_are_served(self):
        for name, kind in (("shell-v3.css", "text/css"), ("shell-v3.js", "application/javascript")):
            handler = Fake("/static/" + name)
            handler.do_GET()
            self.assertIsNone(handler.error, name)
            self.assertIn(kind, handler.content_type)
            self.assertGreater(len(handler.body), 1000, name)

    def test_the_document_asks_for_exactly_those_two(self):
        text = get("/?ui=v3").body.decode("utf-8")
        self.assertIn("/static/shell-v3.css?v=", text)
        self.assertIn("/static/shell-v3.js?v=", text)
        self.assertNotIn("chat-shell.js", text)

    def test_the_allow_list_is_still_an_allow_list(self):
        handler = Fake("/static/../app.py")
        handler.do_GET()
        self.assertEqual(handler.error, 404)


class RunPayloadContract(unittest.TestCase):
    """v3 may not drift from the /run contract, so it is handed the same manifest, not a copy."""

    def test_both_shells_boot_with_the_identical_run_manifest(self):
        v3 = boot_of(get("/?ui=v3").body)["manifest"]
        chat = chat_boot_of(get("/?ui=chat").body)
        self.assertIsNotNone(chat, "the chat shell document changed shape")
        self.assertEqual(v3["run"], chat["manifest"]["run"])
        self.assertEqual(v3["masters"], chat["manifest"]["masters"])

    def test_the_manifest_is_the_live_one(self):
        v3 = boot_of(get("/?ui=v3").body)["manifest"]
        self.assertEqual(v3["run"], chat_ui.RUN_MANIFEST)
        self.assertEqual(v3["masters"], chat_ui.MASTER_MANIFESTS)

    def test_the_boot_block_carries_what_the_page_needs_to_render(self):
        boot = boot_of(get("/?ui=v3").body)
        for key in ("strings", "options", "reasoningConfig", "manifest", "uiState", "initial", "now"):
            self.assertIn(key, boot)


class JobStatusIsAdditive(unittest.TestCase):

    def setUp(self):
        self.job_id = "test_v3_job"
        now = time.time()
        self.lines = [
            ("Generating voiceover...", 300),
            ("Auto Director: planning scenes...", 240),
            ("Seedance clip 2/3 rendering...", 30),
        ]
        with app.JOB_LOCK:
            app.JOBS[self.job_id] = {
                "status": "running",
                "logs": [text for text, _ in self.lines],
                "log_times": [now - back for _, back in self.lines],
                "created_at": now - 320,
                "job_kind": "run", "clip_source": "scrape",
                "result": None, "error": "",
            }
        self.addCleanup(lambda: app.JOBS.pop(self.job_id, None))

    def test_the_old_poll_is_unchanged(self):
        plain = json.loads(app.job_status_payload(self.job_id))
        self.assertNotIn("v3", plain)
        self.assertEqual(get("/job-status?id=" + self.job_id).body, app.job_status_payload(self.job_id))

    def test_the_v3_poll_adds_the_block_and_nothing_else(self):
        plain = json.loads(app.job_status_payload(self.job_id))
        rich = json.loads(get("/job-status?id=%s&v3=1" % self.job_id).body)
        self.assertEqual(set(rich) - set(plain), {"v3"})
        self.assertEqual({k: rich[k] for k in plain if k != "progress_html"},
                         {k: plain[k] for k in plain if k != "progress_html"})

    def test_the_block_describes_the_run(self):
        block = json.loads(get("/job-status?id=%s&v3=1" % self.job_id).body)["v3"]
        self.assertEqual(block["version"], shell_v3.SHELL_V3_VERSION)
        self.assertEqual(block["history_key"], "run:scrape")
        self.assertTrue(block["steps"])
        self.assertEqual(block["phase"], "Video clips")
        self.assertIn("Seedance clip 2/3", block["activity"])
        self.assertEqual(block["health"]["clips_done"], 2)
        self.assertGreater(block["elapsed"], 300)

    def test_a_broken_block_never_takes_the_poll_down(self):
        with mock.patch.object(shell_v3, "job_extras", side_effect=RuntimeError("boom")):
            rich = json.loads(get("/job-status?id=%s&v3=1" % self.job_id).body)
        self.assertEqual(rich["status"], "running")
        self.assertIn("boom", rich["v3"]["error"])

    def test_a_missing_job_still_answers(self):
        rich = json.loads(get("/job-status?id=nope&v3=1").body)
        self.assertFalse(rich["exists"])


class RunHealth(unittest.TestCase):
    """The counters are parsed from the lines the providers really log."""

    SCRAPEDO = [
        "Scrape.do TikTok: rendering 3 search page(s) with 2 worker(s).",
        "Scrape.do TikTok: 12 unique post URL(s) for 'capsule hotel pod' · 5 credits.",
        "Scrape.do TikTok: 8 unique post URL(s) for 'Japan capsule hotel pods'.",
        "Scrape.do TikTok: search 'inside capsule hotel pod' failed (RuntimeError).",
    ]

    def test_every_executed_query_counts_including_the_failed_one(self):
        self.assertEqual(shell_v3.run_health(self.SCRAPEDO)["queries"], 3)

    def test_the_same_query_twice_is_one_query(self):
        logs = self.SCRAPEDO + ["Scrape.do TikTok: 4 unique post URL(s) for 'capsule hotel pod'."]
        self.assertEqual(shell_v3.run_health(logs)["queries"], 3)

    def test_the_batched_bright_collector_reports_its_keyword_count(self):
        """Bright submits ONE job for many keywords - counting lines would say 1."""
        logs = ["Bright Data TikTok: one job for 18 keyword(s), up to 3 record(s) each.",
                "Bright Data TikTok: 41 record(s) across 18 keyword(s) (nothing). Budget 41/300."]
        self.assertEqual(shell_v3.run_health(logs)["queries"], 18)

    def test_the_logged_in_session_and_the_sort_sweep_are_counted_too(self):
        logs = ["V4 TikTok session: search failed for 'tokyo train sleeping' (TimeoutError).",
                "Sort MOST_LIKED: +3 new clip(s) (3 total) for 'tokyo commuters'.",
                "Sort RELEVANCE: +1 new clip(s) (4 total) for 'tokyo commuters'."]
        self.assertEqual(shell_v3.run_health(logs)["queries"], 2)

    def test_a_run_that_never_searched_shows_no_query_tile(self):
        """A generated AI Short must not display '0 queries' as if something had gone wrong."""
        health = shell_v3.run_health(["Generating voiceover...", "GPT image 3/8"])
        self.assertNotIn("queries", health)
        self.assertNotIn("checked", health)
        self.assertEqual((health["images_done"], health["images_total"]), (3, 8))

    def test_the_source_gate_counters(self):
        logs = ["V4 source gate: inspecting tiktok post 1 for 'a'.",
                "V4 source gate: rejected 1 — not native vertical.",
                "V4 source gate: inspecting tiktok post 2 for 'a'.",
                "Downloaded accepted candidate 2.",
                "V4 delivered 4/5 beats; 1 of them carries the best remaining source."]
        health = shell_v3.run_health(logs)
        self.assertEqual((health["checked"], health["rejected"], health["accepted"]), (2, 1, 1))
        self.assertEqual((health["beats_covered"], health["beats_total"]), (4, 5))

    def test_the_live_timeline_wins_over_the_log_line(self):
        logs = ["V4 delivered 4/5 beats."]
        live = {"beats": [{"assigned": True}, {"assigned": True}, {"assigned": False}]}
        health = shell_v3.run_health(logs, live)
        self.assertEqual((health["beats_covered"], health["beats_total"]), (2, 3))

    def test_the_transport_lines_are_not_mistaken_for_output(self):
        self.assertEqual(shell_v3.run_health(["PREVIEW_IMAGE|x.jpg", "PROJECT_DIR|d"]), {})


class StepHistory(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()) / "step_history.json"
        patch = mock.patch.object(shell_v3, "STEP_HISTORY_PATH", self.tmp)
        patch.start()
        self.addCleanup(patch.stop)
        shell_v3._RECORDED_JOBS.clear()

    def steps(self, *pairs):
        return [{"name": name, "state": "done", "elapsed": value} for name, value in pairs]

    def test_a_finished_run_teaches_the_next_one_what_is_typical(self):
        shell_v3.record_step_durations("job1", "run:scrape", self.steps(("Director", 100.0)))
        shell_v3.record_step_durations("job2", "run:scrape", self.steps(("Director", 200.0)))
        shell_v3.record_step_durations("job3", "run:scrape", self.steps(("Director", 300.0)))
        self.assertEqual(shell_v3.typical_durations("run:scrape"), {"Director": 200.0})

    def test_one_job_is_only_ever_recorded_once(self):
        self.assertTrue(shell_v3.record_step_durations("job1", "run:scrape", self.steps(("Director", 100.0))))
        self.assertFalse(shell_v3.record_step_durations("job1", "run:scrape", self.steps(("Director", 999.0))))
        self.assertEqual(shell_v3.typical_durations("run:scrape"), {"Director": 100.0})

    def test_unfinished_steps_are_not_learned_from(self):
        shell_v3.record_step_durations("job1", "run:scrape",
                                       [{"name": "Render", "state": "active", "elapsed": 12.0}])
        self.assertEqual(shell_v3.typical_durations("run:scrape"), {})

    def test_scrape_and_generate_runs_are_kept_apart(self):
        shell_v3.record_step_durations("a", "run:scrape", self.steps(("Director", 100.0)))
        shell_v3.record_step_durations("b", "run:generate", self.steps(("Director", 20.0)))
        self.assertEqual(shell_v3.typical_durations("run:scrape"), {"Director": 100.0})
        self.assertEqual(shell_v3.typical_durations("run:generate"), {"Director": 20.0})

    def test_an_unknown_population_simply_has_no_history(self):
        self.assertEqual(shell_v3.typical_durations("run:nothing_ever_ran"), {})

    def test_only_the_last_runs_are_remembered(self):
        for i in range(shell_v3.HISTORY_KEEP + 6):
            shell_v3.record_step_durations(f"job{i}", "run:scrape", self.steps(("Director", float(i))))
        stored = json.loads(self.tmp.read_text(encoding="utf-8"))["run:scrape"]["Director"]
        self.assertEqual(len(stored), shell_v3.HISTORY_KEEP)

    def test_the_history_key_follows_the_job(self):
        self.assertEqual(shell_v3.history_key({"job_kind": "run", "clip_source": "scrape"}), "run:scrape")
        self.assertEqual(shell_v3.history_key({"job_kind": "sfx"}), "sfx")


class Progress(unittest.TestCase):

    def test_without_history_the_legacy_marker_percentage_is_used(self):
        steps = [{"name": "A", "state": "done", "elapsed": 10}, {"name": "B", "state": "pending"}]
        self.assertAlmostEqual(shell_v3.progress_fraction(steps, 40), 0.4)

    def test_with_history_the_bar_follows_real_time(self):
        steps = [{"name": "A", "state": "done", "elapsed": 60, "typical": 60},
                 {"name": "B", "state": "active", "elapsed": 30, "typical": 60},
                 {"name": "C", "state": "pending", "typical": 60}]
        self.assertAlmostEqual(shell_v3.progress_fraction(steps, 10), 90 / 180.0)

    def test_a_step_that_overruns_cannot_push_the_bar_past_the_end(self):
        steps = [{"name": "A", "state": "active", "elapsed": 9000, "typical": 60},
                 {"name": "B", "state": "pending", "typical": 60}]
        self.assertLessEqual(shell_v3.progress_fraction(steps, 99), 0.99)


if __name__ == "__main__":
    unittest.main()
