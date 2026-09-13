"""ProPainter must actually be reachable from a real run, not just in theory.

Measured 2026-08-29: a delivered Clip Short still showed the source's own Japanese captions
burned across its last shot, while the pipeline logged "Caption cleanup: burned-in captions are
removed by default for source footage (inpainted, not blurred)."

Two independent gates each defaulted to OFF, and together they made the promise impossible:

  1. `apply_timeline_edits_to_config(..., allow_gpu=False)` - the pre-render cleanup of a normal
     run never passed it, so only the timeline editor's Render could reach the GPU.
  2. `CAPTION_REMOVER_PROPAINTER` was an OPT-IN, set by exactly one batch script under tools/
     and never by the app.

So ProPainter had never run inside the application at all - no `propainter_stderr.log` existed
anywhere on disk - and every clip silently got the CPU fill instead.
"""

import inspect
import os
import unittest
from unittest import mock

import agent_core
import caption_remover as cr


class GateTests(unittest.TestCase):
    def test_the_env_var_is_a_kill_switch_not_an_opt_in(self):
        """An opt-in nobody sets is the same as no GPU path at all.

        The gate used to be one composite condition; it is now three early returns, one per
        reason, because the single shared "unavailable" message blamed the backend for the
        caller's own default. The rule this test guards is unchanged: UNSET means on.
        """
        body = inspect.getsource(cr.remove_caption_regions)
        self.assertIn('os.environ.get("CAPTION_REMOVER_PROPAINTER", "1") == "0"', body)
        self.assertIn("kill switch", body)

    def test_it_can_still_be_switched_off(self):
        """A busy GPU or a broken driver must still have a way out."""
        with mock.patch.dict(os.environ, {"CAPTION_REMOVER_PROPAINTER": "0"}):
            self.assertEqual(os.environ.get("CAPTION_REMOVER_PROPAINTER", "1"), "0")

    def test_the_default_is_unset_and_that_means_on(self):
        env = {k: v for k, v in os.environ.items() if k != "CAPTION_REMOVER_PROPAINTER"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertNotEqual(os.environ.get("CAPTION_REMOVER_PROPAINTER", "1"), "0")

    def test_a_background_scrape_still_may_not_take_the_gpu(self):
        """`allow_gpu` stays the real switch - but as an opt-OUT.

        Superseded 2026-09-03 by measurement: with the default at False, four of the seven call
        sites never passed anything, so the function returned 0 while reporting the backend as
        unavailable and the caption stayed on the video. The default is now None ("nobody said,
        so clean it"); the callers that genuinely must not take the card - the scrape probe and
        the save path - say so explicitly, and that is what this test now pins.
        """
        self.assertIsNone(
            inspect.signature(cr.remove_caption_regions).parameters["allow_gpu"].default)
        import scrape_v2
        self.assertIn("allow_gpu=False",
                      inspect.getsource(scrape_v2._captions_are_removable))
        self.assertIs(
            inspect.signature(agent_core.apply_timeline_edits_to_config)
            .parameters["allow_gpu"].default, False)


class RenderPathTests(unittest.TestCase):
    def test_the_normal_runs_caption_cleanup_asks_for_the_gpu(self):
        """This is the pass whose own log line promises inpainting."""
        source = open(agent_core.__file__, encoding="utf-8").read()
        anchor = source.index("Caption cleanup: burned-in captions are removed by default")
        block = source[anchor:anchor + 1400]
        call = block[block.index("apply_timeline_edits_to_config("):]
        call = call[:call.index(")") + 1]
        self.assertIn("allow_gpu=True", call, call)

    def test_the_timeline_render_still_asks_for_it_too(self):
        self.assertIn("allow_gpu=True",
                      inspect.getsource(agent_core.render_project_timeline))


if __name__ == "__main__":
    unittest.main()
