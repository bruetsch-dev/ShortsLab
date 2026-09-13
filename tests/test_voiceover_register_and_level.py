"""Three fixes to how a longform sketch explainer sounds.

1. SEED GOT THE WRONG REGISTER. Seed ignores the Gemini `style` field by design - it takes
   direction through `voice_instruction` - and the longform path set neither, so it fell back to
   SEED_DEFAULT_VOICE_INSTRUCTION: "Upbeat, energetic, confident SHORT-FORM narrator... strong
   emphasis on the hook and key words." Twenty minutes read that way never settles. Measured on
   the same sentence: the longform instruction reads at 13.8 chars/s against 14.6, with 29.2 dB
   of dynamic range against 26.9 and 12.2% pause against 10.1%.

2. THE VOICEOVER RAN THE WHOLE SCRIPT. Judging how a voice sounds does not need twenty minutes
   of it, and a full run pays for the TTS, the alignment and one image per beat.

3. THE REVIEW SCREEN COMPARED TAKES AT DIFFERENT VOLUMES. Each part is its own TTS call: two
   parts of one finished video measured -20.6 and -22.8 LUFS. Levelled before review, the spread
   goes 2.2 dB -> 0.1 dB, and the noise floor rises only by the gain applied (+2.4 dB for +2.3 dB)
   - no dynamics processing, because lifting a TTS part with a compressor raises its hiss.
"""

import unittest
from pathlib import Path

import longform_video as lv
import pipeline

ROOT = Path(__file__).resolve().parent.parent
SRC = (ROOT / "longform_video.py").read_text(encoding="utf-8")


class SeedRegisterTests(unittest.TestCase):
    def test_seed_has_its_own_longform_direction(self):
        self.assertTrue(hasattr(pipeline, "SEED_LONGFORM_VOICE_INSTRUCTION"))
        text = pipeline.SEED_LONGFORM_VOICE_INSTRUCTION.lower()
        for word in ("calm", "unhurried", "pauses"):
            self.assertIn(word, text)

    def test_it_explicitly_rejects_the_shorts_register(self):
        """The failure was a fallback, so the replacement has to name what it is not."""
        self.assertIn("no hook energy", pipeline.SEED_LONGFORM_VOICE_INSTRUCTION.lower())

    def test_the_opening_gets_one_notch_of_lift_not_a_hook(self):
        opening = pipeline.SEED_LONGFORM_OPENING_INSTRUCTION.lower()
        self.assertIn("opening", opening)
        self.assertIn("no short-form hook attack", opening)

    def test_the_longform_path_actually_sends_it(self):
        block = SRC[SRC.index("def _part_tts_kwargs"):]
        block = block[:block.index("elif index == 0:")]
        self.assertIn("SEED_LONGFORM_VOICE_INSTRUCTION", block)
        self.assertIn("SEED_LONGFORM_OPENING_INSTRUCTION", block)

    def test_a_hand_typed_direction_still_wins(self):
        block = SRC[SRC.index("def _part_tts_kwargs"):][:2600]
        # One condition now covers all three providers: a typed direction wins, and Gemini is
        # left alone because it takes its direction through `style`, not this field.
        self.assertIn('if supplied or provider == "gemini":', block)
        self.assertIn("typed by hand", block)


class LengthTests(unittest.TestCase):
    SCRIPT = ("Your body runs a slow rhythm you never agreed to. One nostril opens while the "
              "other narrows. A few hours later they quietly trade places. Nobody notices. ") * 20

    def test_the_script_is_never_truncated_by_default(self):
        """This limit cuts the SCRIPT. Switched on by default it turned a 20-minute video into a
        2.5-minute one with a single voiceover part - "2-3 minutes" was always about how long
        each TTS PART is, never about how much of the script gets narrated."""
        self.assertFalse(lv.LONGFORM_VOICEOVER_LIMIT_S,
                         "a pasted script is being cut short again")

    def test_a_full_length_script_keeps_all_of_its_parts(self):
        script = ("Your body runs a slow rhythm you never agreed to. One nostril opens while "
                  "the other narrows. ") * 200
        parts = lv.split_script_for_tts(script, limit=lv.tts_chunk_limit("pro"))
        self.assertGreater(len(parts), 5, f"{len(parts)} parts for a 20-minute script")
        self.assertGreater(sum(len(p) for p in parts) / lv.NARRATION_CHARS_PER_SECOND / 60, 15)

    def test_each_part_is_the_two_to_three_minutes_that_was_asked_for(self):
        script = ("Your body runs a slow rhythm you never agreed to. One nostril opens while "
                  "the other narrows. ") * 200
        for model, ceiling in (("pro", 190), ("inworld", 140)):
            longest = max(len(p) for p in
                          lv.split_script_for_tts(script, limit=lv.tts_chunk_limit(model)))
            seconds = longest / lv.NARRATION_CHARS_PER_SECOND
            self.assertLess(seconds, ceiling, f"{model}: {seconds:.0f}s per part")

    def test_the_option_still_exists_for_a_deliberate_preview(self):
        trimmed = lv.trim_script_to_seconds("One. Two. Three. " * 200, 150)
        self.assertLess(len(trimmed), 2400)

    def test_the_trim_lands_on_a_sentence(self):
        trimmed = lv.trim_script_to_seconds(self.SCRIPT, 150)
        self.assertTrue(trimmed.rstrip().endswith((".", "!", "?")), trimmed[-70:])

    def test_it_is_close_to_the_asked_for_length(self):
        asked = 150.0
        seconds = len(lv.trim_script_to_seconds(self.SCRIPT, asked)) / lv.NARRATION_CHARS_PER_SECOND
        self.assertGreater(seconds, asked * 0.75, f"{seconds:.0f}s for {asked:.0f}s asked")
        self.assertLess(seconds, asked * 1.1, f"{seconds:.0f}s for {asked:.0f}s asked")

    def test_zero_means_the_whole_script(self):
        self.assertEqual(lv.trim_script_to_seconds(self.SCRIPT, 0), self.SCRIPT.strip())

    def test_a_script_shorter_than_the_limit_is_untouched(self):
        short = "One sentence. And a second one."
        self.assertEqual(lv.trim_script_to_seconds(short, 150), short)

    def test_a_single_enormous_sentence_still_returns_something(self):
        """Never return an empty script because the first sentence blew the budget."""
        wall = "word " * 4000 + "."
        self.assertTrue(lv.trim_script_to_seconds(wall, 150).strip())

    def test_the_limit_is_reversible_from_the_environment(self):
        self.assertIn('os.environ.get("LONGFORM_VOICEOVER_LIMIT_S"', SRC)


class LevellingTests(unittest.TestCase):
    def test_it_runs_before_the_review_gate(self):
        gate = SRC.index("if speech_gate is not None:")
        level = SRC.index("match_part_loudness(part_files")
        self.assertLess(level, gate, "the takes are reviewed before they are levelled")

    def test_it_is_gain_only_never_a_compressor(self):
        """Lifting a TTS part with dynamics raises its noise floor - that hiss cost a day once."""
        block = SRC[SRC.index("def match_part_loudness"):SRC.index("def measure_integrated_loudness")]
        # Only what is actually SENT to ffmpeg - the docstring names loudnorm to say why it is
        # not used, and searching the whole function finds that explanation.
        call = block[block.index('"-filter:a"'):block.index("capture_output=True")]
        self.assertIn("volume=", call)
        for banned in ("acompressor", "loudnorm", "alimiter", "speechnorm"):
            self.assertNotIn(banned, call)

    def test_a_part_already_in_range_is_not_re_encoded(self):
        block = SRC[SRC.index("def match_part_loudness"):SRC.index("def measure_integrated_loudness")]
        self.assertIn("if abs(gain) < 0.5:", block)
        self.assertIn("if spread < 0.5:", block)

    def test_a_failed_level_keeps_the_original_take(self):
        block = SRC[SRC.index("def match_part_loudness"):SRC.index("def measure_integrated_loudness")]
        self.assertIn("not worth losing the take over", block)

    def test_one_part_needs_no_levelling(self):
        self.assertEqual(lv.match_part_loudness([], None), [])
        self.assertEqual(lv.match_part_loudness(["only.mp3"], None), ["only.mp3"])

    def test_an_unmeasurable_file_does_not_crash_the_run(self):
        self.assertIsNone(lv.measure_integrated_loudness("does_not_exist.wav",
                                                         pipeline.find_ffmpeg()))


if __name__ == "__main__":
    unittest.main()


class NarratorValidationTests(unittest.TestCase):
    """A narrator that does not belong to the selected model was silently replaced with "".

    The run then used the provider's default, so you pick a voice, hear a different one, and
    nothing anywhere says why. It happens whenever the two dropdowns drift apart - an Inworld
    narrator left selected while the model still says Gemini - which is exactly the case that
    produced "es hat überhaupt nicht den narrator genommen den ich wollte".
    """

    def setUp(self):
        self.app = (ROOT / "app.py").read_text(encoding="utf-8")

    def test_a_rejected_narrator_is_named_not_swallowed(self):
        self.assertIn("does not exist on", self.app)
        self.assertIn("voice_warning", self.app)

    def test_the_warning_reaches_the_job_log(self):
        self.assertIn("if voice_warning:\n        status_cb(voice_warning)", self.app)

    def test_every_provider_has_an_explicit_default(self):
        """Falling through with "" left the choice to whichever provider branch ran."""
        block = self.app[self.app.index("if not tts_voice:"):][:400]
        for provider in ('"seed"', '"inworld"', '"gemini"'):
            self.assertIn(provider, block)

    def test_the_warning_says_how_to_fix_it(self):
        self.assertIn("Pick a", self.app)


class TruncationTests(unittest.TestCase):
    """The script must reach the narrator whole.

    This limit cuts the SCRIPT, and switched on by default it turned a 20-minute video into a
    2.5-minute one with a single voiceover part. "2-3 minutes" was always about how long each
    TTS PART is, never about how much of the script gets narrated.
    """

    def test_the_run_path_never_trims(self):
        src = (ROOT / "longform_video.py").read_text(encoding="utf-8")
        run = src[src.index("def run_longform_video("):]
        # run_longform_video is the last function in the file, so nothing follows it
        nxt = run.find(chr(10) + "def ", 100)
        self.assertNotIn("trim_script_to_seconds", run if nxt < 0 else run[:nxt])

    def test_the_switch_is_off(self):
        self.assertFalse(lv.LONGFORM_VOICEOVER_LIMIT_S)

    def test_it_stays_reachable_for_a_deliberate_preview(self):
        src = (ROOT / "longform_video.py").read_text(encoding="utf-8")
        self.assertIn('os.environ.get("LONGFORM_VOICEOVER_LIMIT_S"', src)

    def test_a_full_script_keeps_all_of_its_parts(self):
        script = ("Your body runs a slow rhythm you never agreed to. One nostril opens while "
                  "the other narrows. ") * 200
        parts = lv.split_script_for_tts(script, limit=lv.tts_chunk_limit("pro"))
        self.assertGreater(len(parts), 5, f"{len(parts)} parts for a 20-minute script")


