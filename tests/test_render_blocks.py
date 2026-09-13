import unittest

import agent_core
import pipeline


class RenderBlockSwitchTests(unittest.TestCase):
    """The app refused to render clips it judged defective - a repeated-frame stall, an isolated
    duplicate-frame hitch, an overlay shape, an SFX judged too long. Useful to know, but it left
    the user with an error card and no video. The checks still run and still report; they no
    longer stop the render unless the gates are switched back on."""

    def test_the_gates_are_off_by_default_and_can_be_switched_on(self):
        for module in (pipeline, agent_core):
            self.assertFalse(module.render_blocks_enabled({}))
            self.assertFalse(module.render_blocks_enabled(None))
            self.assertTrue(module.render_blocks_enabled({"enforce_render_blocks": True}))

    def _bad_config(self):
        # voice_speed outside the allowed band is the cheapest gate to trip.
        return {"voice_speed": 99.0, "scenes": [{"id": "01", "overlays": []}],
                "clip_source": "scrape", "_scrape_enforcement": {}}

    def test_a_failed_check_reports_instead_of_refusing(self):
        said = []
        agent_core.validate_scrape_render(self._bad_config(), status_cb=said.append)
        self.assertTrue(any("not blocking" in m for m in said), said)
        self.assertTrue(any("voice_speed" in m for m in said), said)

    def test_switching_the_gates_on_restores_the_refusal(self):
        config = self._bad_config()
        config["enforce_render_blocks"] = True
        with self.assertRaises(RuntimeError):
            agent_core.validate_scrape_render(config)

    def test_a_clean_config_says_nothing(self):
        config = self._bad_config()
        config["voice_speed"] = agent_core.VOICE_SPEED_MIN
        said = []
        try:
            agent_core.validate_scrape_render(config, status_cb=said.append)
        except RuntimeError:
            self.fail("a non-blocking validation must never raise")
        self.assertFalse([m for m in said if "voice_speed" in m], said)

    def test_a_clip_defect_never_refuses_the_render(self):
        """Pressing Render renders. Three gates used to throw the whole job away - a clip below
        the 0.8x floor, a repeated-frame stall, duplicate-frame hitches - so one bad shot meant
        no file at all. A short with one slow shot can be fixed in the editor; a refused render
        cannot be fixed at all. The defects are still measured and reported."""
        import inspect
        src = inspect.getsource(pipeline.render_video)
        # Check the RAISES, not the prose: the comments above the new code quote the old error
        # text on purpose, and a substring search over the function matches those quotes.
        raises = [line.strip() for line in src.splitlines()
                  if line.strip().startswith("raise RuntimeError(")]
        for line in raises:
            self.assertNotIn("Render blocked", line)
            self.assertNotIn("Timeline repair required", line)
        for signal in ("repeated-frame stall", "duplicate-frame", "below the usual 0.8x floor"):
            self.assertIn(signal, src, signal)
        # Genuine encoder failures are not quality gates and must still raise.
        self.assertIn("OpenCV could not open MP4 writer.", src)

    def test_footage_is_still_never_looped_or_frozen(self):
        """Unblocking the render must not have bought it by repeating frames."""
        import inspect
        src = inspect.getsource(pipeline.render_video)
        self.assertIn("speed_factor = max(0.05, min(1.0, required_speed))", src)


if __name__ == "__main__":
    unittest.main()
