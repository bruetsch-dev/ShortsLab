"""Fix the take you have instead of paying for another one.

A generated voiceover is rarely wrong in a way that regenerating fixes - it is a little quiet, a
little hissy in the gaps, a little thin. Declining rolls the dice again and costs another TTS
call; a preset repairs the audio already on disk.

Measured on a real part (-20.3 LUFS, noise floor -86 dB):

    Original          -20.3 LUFS    -86.0 dB
    Even level        -21.0         -86.3
    Clean up hiss     -21.0        -180.0     <- gate closes the gaps
    Warmer            -21.0         -87.0
    Clearer speech    -21.1         -91.3
    Close mic         -20.6        -180.0

And switching to "Close mic" and back to "Original" returns a byte-identical file, because every
preset is applied to a stashed copy of the untouched take rather than to the previous result.
"""

import shutil
import tempfile
import unittest
from pathlib import Path

import voice_presets as vp


class CatalogueTests(unittest.TestCase):
    def test_every_preset_is_offered_with_a_reason_to_pick_it(self):
        for choice in vp.preset_choices():
            self.assertTrue(choice["label"])
            self.assertTrue(choice["hint"], f"{choice['value']} has no hint")

    def test_original_is_first_and_does_nothing(self):
        self.assertEqual(vp.preset_choices()[0]["value"], "none")
        self.assertEqual(vp.PRESETS["none"]["chain"], "")

    def test_they_all_land_on_the_same_loudness(self):
        """Parts are compared against each other on this screen; a preset that also changed the
        level would make the comparison meaningless."""
        for key, spec in vp.PRESETS.items():
            if not spec["chain"]:
                continue
            self.assertIn(f"loudnorm=I={vp.TARGET_LUFS}", spec["chain"], key)

    def test_the_target_matches_the_rest_of_the_pipeline(self):
        import longform_video as lv
        import sketch_hook_intro as hook
        self.assertEqual(vp.TARGET_LUFS, lv.VOICEOVER_TARGET_LUFS)
        self.assertEqual(vp.TARGET_LUFS, hook.HOOK_TARGET_LUFS)

    def test_a_gate_never_runs_before_the_denoiser(self):
        """Gating a hissy signal chops the hiss into bursts, which is worse than steady hiss."""
        for key, spec in vp.PRESETS.items():
            chain = spec["chain"]
            if "agate" in chain:
                self.assertIn("afftdn", chain, key)
                self.assertLess(chain.index("afftdn"), chain.index("agate"), key)

    def test_no_preset_compresses_without_denoising_first(self):
        """Lifting a quiet TTS part with dynamics raises its noise floor with it."""
        for key, spec in vp.PRESETS.items():
            if "acompressor" in spec["chain"]:
                self.assertIn("afftdn", spec["chain"], key)


class ApplyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vp_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.part = self.tmp / "vo_part00.mp3"
        self.part.write_bytes(b"x" * 8192)

    def test_an_unknown_preset_is_refused_by_name(self):
        with self.assertRaises(ValueError):
            vp.apply_preset(self.part, "sparkle", "ffmpeg")

    def test_a_missing_file_is_refused(self):
        with self.assertRaises(FileNotFoundError):
            vp.apply_preset(self.tmp / "nope.mp3", "level", "ffmpeg")

    def test_the_original_keeps_a_real_extension(self):
        """ffmpeg infers the format from the extension; "part.mp3.orig" is not one it can guess
        and every preset failed with "Invalid argument"."""
        self.assertTrue(vp.original_path(self.part).name.endswith(".mp3"))
        self.assertIn("original", vp.original_path(self.part).name)

    def test_none_restores_the_stashed_take(self):
        shutil.copy2(self.part, vp.original_path(self.part))
        self.part.write_bytes(b"processed")
        vp.apply_preset(self.part, "none", "ffmpeg")
        self.assertEqual(self.part.read_bytes(), b"x" * 8192)


class WiringTests(unittest.TestCase):
    ROOT = Path(__file__).resolve().parent.parent

    def test_the_screen_offers_them(self):
        chat = (self.ROOT / "chat_ui.py").read_text(encoding="utf-8")
        shell = (self.ROOT / "static" / "chat-shell.js").read_text(encoding="utf-8")
        self.assertIn("voice_presets", chat)
        self.assertIn("OPT.voice_presets", shell)

    def test_polish_is_its_own_action_next_to_approve_and_decline(self):
        app = (self.ROOT / "app.py").read_text(encoding="utf-8")
        self.assertIn('if action == "polish":', app)
        self.assertIn('"action": "polish"', app)

    def test_the_gate_applies_it_from_the_original(self):
        app = (self.ROOT / "app.py").read_text(encoding="utf-8")
        self.assertIn("voice_presets.apply_preset(target, preset", app)

    def test_a_failed_preset_does_not_end_the_run(self):
        app = (self.ROOT / "app.py").read_text(encoding="utf-8")
        block = app[app.index('elif action == "polish":'):][:1200]
        self.assertIn("except Exception", block)
        self.assertIn("Could not apply", block)

    def test_the_audio_url_changes_so_the_browser_refetches(self):
        """An <audio> element happily keeps playing the bytes it already cached."""
        app = (self.ROOT / "app.py").read_text(encoding="utf-8")
        self.assertIn('+ "&v=" + str(', app)


if __name__ == "__main__":
    unittest.main()
