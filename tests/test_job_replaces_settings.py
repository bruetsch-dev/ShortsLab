"""A running job replaces its own settings form.

Reported 2026-08-31: "warum wird immer noch das settings menu gezeigt wenn der sketch timeline
erscheint". `renderAll()` dispatched the configuration flow FIRST and appended the job section
under it, so every screen a run produces - the live log, the thumbnail choices, the pre-render
timeline - arrived with the whole settings form still sitting above it. Nothing on that form can
be changed once the run has started.

The prototype shell already returned early for a job; the default shell never did.
"""

import unittest
from pathlib import Path

JS = Path("static/chat-shell.js").read_text(encoding="utf-8")
BODY = JS[JS.index("function renderAll()"):]
BODY = BODY[:BODY.index("\n// Short chip labels")]
# There are two job guards in renderAll: an early one for "a job with no flow", and the one that
# stops the settings form being drawn at all. Target the second by its own comment.
GUARD_AT = BODY.index("A RUNNING JOB REPLACES ITS OWN SETTINGS")


class RenderAllTests(unittest.TestCase):
    def test_a_running_job_returns_before_the_flow_is_drawn(self):
        job_guard = GUARD_AT
        dispatch = BODY.index("longform: renderLongformFlow")
        self.assertLess(job_guard, dispatch,
                        "the settings flow is still drawn above the job")

    def test_the_guard_is_not_limited_to_the_prototype_shell(self):
        """It was `prototypeMode && S.jobId`, which is why the default shell kept the form."""
        guard = BODY[GUARD_AT:]
        guard = guard[:guard.index("}")]
        self.assertNotIn("prototypeMode", guard)

    def test_the_guard_actually_returns(self):
        guard = BODY[GUARD_AT:]
        guard = guard[:guard.index("}") + 1]
        self.assertIn("renderJobSection()", guard)
        self.assertIn("return", guard)

    def test_the_composer_is_closed_with_it(self):
        """It belonged to the step that started the run - often an upload field."""
        guard = BODY[GUARD_AT:]
        guard = guard[:guard.index("}") + 1]
        self.assertIn('setComposer("off")', guard)

    def test_a_flow_without_a_job_is_untouched(self):
        """Configuring a run must still show the steps."""
        self.assertIn("longform: renderLongformFlow", BODY)
        self.assertIn("}[S.flow] || renderModeMenu)()", BODY)


if __name__ == "__main__":
    unittest.main()
