"""Close the browser window and the server goes with it.

The server runs with a hidden window, so closing the browser used to leave it running. That is
not merely untidy: the Stream Deck button deliberately never starts a second instance, so the
next press reopened the SAME old process - four servers from the previous evening were still
serving while a fix sat on disk unused, and the app looked like the change had done nothing.

The dangerous half is the other direction. A render outliving its window is exactly why this is
careful: nothing here may exit while a job is running, and a server nobody has opened yet (a
script, a test, a headless run) must never exit at all.
"""

import time
import unittest
from unittest import mock

import app


class TabBookkeepingTests(unittest.TestCase):
    def setUp(self):
        app._TABS.clear()
        app._BROWSER_EVER_SEEN = False

    def test_a_tab_registers_and_says_goodbye(self):
        app._tab_seen("a")
        self.assertIn("a", app._TABS)
        app._tab_gone("a")
        self.assertNotIn("a", app._TABS)

    def test_a_goodbye_for_an_unknown_tab_is_harmless(self):
        app._tab_gone("never-existed")          # a beacon can arrive twice
        self.assertEqual(app._TABS, {})

    def test_the_first_tab_arms_the_watchdog(self):
        self.assertFalse(app._BROWSER_EVER_SEEN)
        app._tab_seen("a")
        self.assertTrue(app._BROWSER_EVER_SEEN)


class WatchdogTests(unittest.TestCase):
    """The watchdog loop, driven one decision at a time instead of in real time."""

    def setUp(self):
        app._TABS.clear()
        app._BROWSER_EVER_SEEN = False
        self.exits = []
        patch_exit = mock.patch.object(app.os, "_exit", side_effect=lambda code: self.exits.append(code))
        patch_exit.start(); self.addCleanup(patch_exit.stop)
        # keep the loop from actually sleeping or shutting a real server down
        self.ticks = 0

        def fake_sleep(_seconds):
            self.ticks += 1
            if self.ticks > 40 or self.exits:
                raise _Stop()
        patch_sleep = mock.patch.object(app.time, "sleep", side_effect=fake_sleep)
        patch_sleep.start(); self.addCleanup(patch_sleep.stop)
        # The loop measures the grace period on the real clock, and this test does not sleep.
        # The length of the grace is asserted separately in test_the_grace_period_is_real.
        patch_grace = mock.patch.object(app, "_LAST_TAB_GRACE_S", 0.0)
        patch_grace.start(); self.addCleanup(patch_grace.stop)

    def run_watchdog(self):
        server = mock.Mock()
        try:
            app._browser_watchdog(server)
        except _Stop:
            pass
        return server

    def test_a_server_nobody_opened_never_exits(self):
        """Started from a script or a test: there is no window to close."""
        with mock.patch.object(app, "_jobs_running", return_value=False):
            self.run_watchdog()
        self.assertEqual(self.exits, [])

    def test_it_exits_once_the_last_tab_is_gone(self):
        app._tab_seen("a")
        app._tab_gone("a")
        with mock.patch.object(app, "_jobs_running", return_value=False):
            self.run_watchdog()
        self.assertEqual(self.exits, [0])

    def test_a_running_job_keeps_it_alive_with_no_tab_at_all(self):
        """The whole reason this is careful - a render must never die with its window."""
        app._tab_seen("a")
        app._tab_gone("a")
        with mock.patch.object(app, "_jobs_running", return_value=True):
            self.run_watchdog()
        self.assertEqual(self.exits, [])

    def test_an_open_tab_keeps_it_alive(self):
        app._tab_seen("a")
        with mock.patch.object(app, "_jobs_running", return_value=False):
            self.run_watchdog()
        self.assertEqual(self.exits, [])

    def test_a_tab_that_stopped_answering_is_forgotten(self):
        """A crashed browser sends no goodbye and its stream may linger half-open."""
        app._tab_seen("a")
        app._TABS["a"] = time.monotonic() - (app._TAB_TIMEOUT_S + 5)
        with mock.patch.object(app, "_jobs_running", return_value=False):
            self.run_watchdog()
        self.assertEqual(self.exits, [0])


class TimingTests(unittest.TestCase):
    """The two clocks, asserted outside WatchdogTests - which patches the grace to 0 to drive
    the loop without sleeping."""

    def test_the_grace_period_is_real(self):
        """Long enough that clicking through to another page re-registers first, short enough
        that closing the window feels like closing the app."""
        self.assertGreaterEqual(app._LAST_TAB_GRACE_S, 3.0)
        self.assertLessEqual(app._LAST_TAB_GRACE_S, 20.0)

    def test_the_tab_timeout_only_has_to_outlive_a_missed_ping(self):
        """The SERVER pings the stream, so a throttled background timer is not the constraint -
        which is what lets this be seconds rather than the 90s it needed before."""
        self.assertGreater(app._TAB_TIMEOUT_S, app._STREAM_PING_S * 3)
        self.assertLess(app._TAB_TIMEOUT_S, 60)


class JobsRunningTests(unittest.TestCase):
    def test_it_sees_a_running_job(self):
        with app.JOB_LOCK:
            app.JOBS["watchdog-test"] = {"status": "running"}
        try:
            self.assertTrue(app._jobs_running())
            with app.JOB_LOCK:
                app.JOBS["watchdog-test"]["status"] = "done"
            self.assertFalse(app._jobs_running())
        finally:
            with app.JOB_LOCK:
                app.JOBS.pop("watchdog-test", None)


class WiringTests(unittest.TestCase):
    def test_every_page_carries_the_heartbeat(self):
        self.assertIn("/heartbeat?tab=", app.heartbeat_script())
        page = app.page("t", "<p>x</p>").decode("utf-8")
        self.assertIn("/heartbeat?tab=", page)
        import chat_ui
        source = open(chat_ui.__file__, encoding="utf-8").read()
        self.assertIn("app.heartbeat_script()", source)

    def test_the_goodbye_works_on_both_methods(self):
        """navigator.sendBeacon always POSTs; the periodic beat is a GET."""
        source = open(app.__file__, encoding="utf-8").read()
        self.assertEqual(source.count('parsed.path == "/heartbeat"'), 2)

    def test_the_page_holds_an_open_stream(self):
        """Measured with a real browser: on window close the goodbye beacon did NOT fire, and
        the server only noticed 80s later through the timeout. A stream dies with the window."""
        self.assertIn("/alive-stream?tab=", app.heartbeat_script())
        source = open(app.__file__, encoding="utf-8").read()
        self.assertIn('parsed.path == "/alive-stream"', source)
        # the handler must drop the tab when the connection breaks, or nothing ever exits
        body = source[source.index('elif parsed.path == "/alive-stream":'):]
        body = body[:body.index("elif parsed.path ==", 40)]
        self.assertIn("_tab_gone(tab)", body)
        self.assertIn("finally:", body)

    def test_it_is_opt_in_not_the_default(self):
        """Shipped ON it shut the server down while the timeline editor was still loading: that
        page pulls hundreds of poster images from one origin, HTTP/1.1 allows about six
        connections to a host, and the keep-alive stream never got a slot. Off until the
        starvation problem is solved."""
        source = open(app.__file__, encoding="utf-8").read()
        self.assertIn('"--auto-close"', source)
        self.assertIn("if args.auto_close:", source)
        self.assertNotIn("if not args.stay_open:", source)


class _Stop(Exception):
    """Ends the watchdog loop inside a test."""


if __name__ == "__main__":
    unittest.main()
