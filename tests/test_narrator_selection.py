"""A narrator you picked must be the narrator you hear - or you must be told why not.

Reported: "es hat überhaupt nicht den narrator genommen den ich wollte". The project's state
showed `voice = ''` and `tts_model = 'pro'`. The cause was one line: a narrator that does not
belong to the selected model was replaced with an empty string, and the run then used the
provider's own default. Nothing said so anywhere - not in the log, not in the state.

It happens whenever the two dropdowns drift apart, which is easy now that there are three
providers with three completely separate voice lists: an Inworld narrator left selected while
the model still says Gemini looks perfectly valid on screen.
"""

import unittest
from pathlib import Path

import pipeline

ROOT = Path(__file__).resolve().parent.parent
APP = (ROOT / "app.py").read_text(encoding="utf-8")


def resolve(model, voice):
    """The server's own resolution, mirrored so the table below is checkable."""
    allowed = set(pipeline.tts_voices_for(model))
    provider = pipeline.tts_provider(model)
    warned = bool(voice and voice not in allowed)
    chosen = "" if warned else voice
    if not chosen:
        chosen = {"seed": "stokie_en",
                  "inworld": pipeline.INWORLD_DEFAULT_VOICE,
                  "gemini": pipeline.DEFAULT_TTS_VOICE}.get(provider, "")
    return chosen, warned


class ResolutionTests(unittest.TestCase):
    def test_a_matching_narrator_is_used_as_chosen(self):
        for model, voice in (("pro", "Laomedeia"), ("inworld", "Dennis"),
                             (pipeline.SEED_SPEECH_TTS_MODEL, "stokie_en")):
            chosen, warned = resolve(model, voice)
            self.assertEqual(chosen, voice)
            self.assertFalse(warned)

    def test_a_mismatched_narrator_warns_instead_of_vanishing(self):
        for model, voice in (("pro", "Dennis"), ("inworld", "Laomedeia"),
                             (pipeline.SEED_SPEECH_TTS_MODEL, "Dennis")):
            _chosen, warned = resolve(model, voice)
            self.assertTrue(warned, f"{model} + {voice} was dropped silently")

    def test_every_provider_falls_back_to_one_of_its_own(self):
        """The bug shipped an EMPTY voice into the pipeline; each provider now names its own."""
        for model in ("pro", "inworld", pipeline.SEED_SPEECH_TTS_MODEL):
            chosen, _ = resolve(model, "")
            self.assertTrue(chosen, f"{model} still resolves to an empty narrator")
            self.assertIn(chosen, pipeline.tts_voices_for(model))


class ServerTests(unittest.TestCase):
    def test_the_warning_text_names_the_voice_and_the_model(self):
        self.assertIn("does not exist on", APP)
        self.assertIn("using its default instead", APP)

    def test_it_reaches_the_job_log(self):
        self.assertIn("if voice_warning:\n        status_cb(voice_warning)", APP)

    def test_no_provider_can_end_up_with_an_empty_narrator(self):
        block = APP[APP.index("allowed_tts_voices = set(pipeline.tts_voices_for(tts_model))"):]
        block = block[:block.index("tts_options = {")]
        self.assertIn("if not tts_voice:", block)
        for provider in ("seed", "inworld", "gemini"):
            self.assertIn(f'"{provider}"', block)

    def test_the_old_silent_drop_is_gone(self):
        """`tts_voice = ""` with nothing after it was the whole bug."""
        block = APP[APP.index("allowed_tts_voices = set(pipeline.tts_voices_for(tts_model))"):]
        block = block[:block.index("tts_options = {")]
        after_drop = block[block.index('tts_voice = ""') + len('tts_voice = ""'):]
        self.assertIn("if not tts_voice:", after_drop)


class TruncationTests(unittest.TestCase):
    """The limit that cut the script is off, and the comment says why it must stay off.

    It was on by default for one round and turned a 20-minute script into a 2.5-minute video
    with one voiceover part. It was also checked against a 2072-character test script - below
    the 2160-character budget - which cut nothing and made the limit look innocent. A threshold
    cannot be tested with an example that sits under it.
    """

    def test_it_is_off(self):
        import longform_video as lv
        self.assertFalse(lv.LONGFORM_VOICEOVER_LIMIT_S)

    def test_the_reason_is_written_next_to_it(self):
        src = (ROOT / "longform_video.py").read_text(encoding="utf-8")
        block = src[src.index("# OFF by default"):src.index("LONGFORM_VOICEOVER_LIMIT_S = float")]
        self.assertIn("truncates the SCRIPT", block)
        self.assertIn("TTS PART", block)

    def test_nothing_calls_it_during_a_run(self):
        src = (ROOT / "longform_video.py").read_text(encoding="utf-8")
        run = src[src.index("def run_longform_video("):]
        nxt = run.find(chr(10) + "def ", 100)
        self.assertNotIn("trim_script_to_seconds", run if nxt < 0 else run[:nxt])


if __name__ == "__main__":
    unittest.main()
