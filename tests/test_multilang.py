"""Extra audio tracks for the seven biggest non-English audiences on YouTube.

Off by default and on purpose: seven translations plus seven full voiceovers is the most
expensive thing this app can be asked to do, and on a twenty-minute explainer that is seven
scripts read end to end.

Nothing here is allowed to cost the video. The tracks are produced AFTER the render, and every
failure path in this file ends with "the video is unaffected".
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import longform_video as lv
import multilang


class LanguageSetTests(unittest.TestCase):
    def test_seven_languages(self):
        self.assertEqual(len(multilang.LANGUAGES), 7)

    def test_english_is_never_regenerated(self):
        """The video already carries the original."""
        codes = [l["code"] for l in multilang.languages_for("en")]
        self.assertNotIn("en", codes)
        self.assertEqual(len(codes), 7)

    def test_the_source_language_is_dropped_whatever_it_is(self):
        codes = [l["code"] for l in multilang.languages_for("de")]
        self.assertNotIn("de", codes)
        self.assertEqual(len(codes), 6)

    def test_every_language_is_read_by_seeds_own_native_speaker(self):
        """Always Seed, never the engine the original happened to use: handing an English-only
        voice a language label produces English phonetics of a foreign script."""
        import pipeline
        for language in multilang.LANGUAGES:
            voice, language_string, engine = multilang.voice_for(language)
            self.assertEqual(engine, pipeline.SEED_SPEECH_TTS_MODEL, language["name"])
            self.assertIn(voice, pipeline.SEED_SPEECH_TTS_VOICES, language["name"])
            self.assertIn(language_string, pipeline.SEED_SPEECH_LANGUAGES, language["name"])
            # the voice name encodes the language it natively speaks
            self.assertIn(language_string.split("-")[0], voice, language["name"])

    def test_hindi_and_russian_are_out_because_seed_cannot_say_them(self):
        """Bigger YouTube audiences than Korean, but sending Hindi to a Spanish voice does not
        produce Hindi - it produces Spanish phonetics reading a transliteration."""
        import pipeline
        codes = [l["code"] for l in multilang.LANGUAGES]
        self.assertNotIn("hi", codes)
        self.assertNotIn("ru", codes)
        self.assertNotIn("hi", pipeline.SEED_SPEECH_LANGUAGES)
        self.assertNotIn("ru", pipeline.SEED_SPEECH_LANGUAGES)
        self.assertIn("ko", codes)

    def test_the_filename_carries_the_language_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = multilang.track_path(tmp, "my_short", multilang.LANGUAGES[0])
        self.assertTrue(path.name.endswith(".es.mp3"), path.name)


class TranslationTests(unittest.TestCase):
    SCRIPT = "Why can't you remember falling asleep? " * 30

    def _reply(self, text):
        return {"choices": [{"message": {"content": text}}]}

    def test_it_returns_the_translated_narration(self):
        spanish = "Por que no recuerdas quedarte dormido? " * 30
        with mock.patch.object(multilang.agent_core, "post_json_url",
                               return_value=self._reply(spanish)):
            got = multilang.translate_script(self.SCRIPT, multilang.LANGUAGES[0])
        self.assertEqual(got, spanish.strip())

    def test_a_fenced_answer_is_unwrapped(self):
        body = "Por que no recuerdas quedarte dormido? " * 30
        with mock.patch.object(multilang.agent_core, "post_json_url",
                               return_value=self._reply("```\n" + body + "\n```")):
            got = multilang.translate_script(self.SCRIPT, multilang.LANGUAGES[0])
        self.assertFalse(got.startswith("`"))

    def test_a_truncated_translation_is_refused_rather_than_spoken(self):
        """A model that answers with one line would otherwise become a 4-second track."""
        with mock.patch.object(multilang.agent_core, "post_json_url",
                               return_value=self._reply("Hola.")):
            with self.assertRaises(multilang.MultiLanguageError):
                multilang.translate_script(self.SCRIPT, multilang.LANGUAGES[0])

    def test_the_prompt_forbids_inventing_facts_and_keeps_paragraphs(self):
        prompt = multilang._translation_prompt(multilang.LANGUAGES[0])
        self.assertIn("paragraph", prompt.lower())
        self.assertIn("do not add or remove any fact", prompt.lower())


class TrackBuildingTests(unittest.TestCase):
    def _speak(self, text, out_path, **kwargs):
        Path(out_path).write_bytes(b"x" * 9000)

    def test_it_writes_one_audio_and_one_script_per_language(self):
        with tempfile.TemporaryDirectory() as tmp:
            tracks = multilang.build_tracks(
                "narration " * 200, tmp, stem="short", languages=multilang.LANGUAGES[:2],
                speak=self._speak, translate=lambda s, l, **k: f"[{l['code']}] " + s)
            # Inside the temp dir: the files are the point of the test.
            self.assertEqual([t["code"] for t in tracks], ["es", "pt"])
            for track in tracks:
                self.assertTrue(Path(track["audio"]).is_file())
                self.assertTrue(Path(track["script"]).read_text(encoding="utf-8")
                                .startswith("["))

    def test_one_language_failing_does_not_stop_the_others(self):
        def flaky(script, language, **kwargs):
            if language["code"] == "es":
                raise multilang.MultiLanguageError("nope")
            return "narration " * 200

        with tempfile.TemporaryDirectory() as tmp:
            tracks = multilang.build_tracks(
                "narration " * 200, tmp, languages=multilang.LANGUAGES[:3],
                speak=self._speak, translate=flaky)
        self.assertEqual([t["code"] for t in tracks], ["pt", "ko"])

    def test_silent_audio_is_reported_as_a_failure_not_a_track(self):
        with tempfile.TemporaryDirectory() as tmp:
            tracks = multilang.build_tracks(
                "narration " * 200, tmp, languages=multilang.LANGUAGES[:1],
                speak=lambda text, out_path, **k: Path(out_path).write_bytes(b""),
                translate=lambda s, l, **k: "narration " * 200)
        self.assertEqual(tracks, [])

    def test_a_long_script_is_split_into_parts(self):
        """One request per language fails on exactly the scripts this feature exists for."""
        calls = []

        def counting(text, out_path, **kwargs):
            calls.append(len(text))
            Path(out_path).write_bytes(b"x" * 9000)

        long_script = ("This is a sentence about sleep and memory. " * 400)
        # The parts here hold fake bytes, so the real concatenator has nothing to decode; what
        # this test is about is how many requests the script is broken into.
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(lv, "concat_audio_parts",
                                   side_effect=lambda parts, out, ff: Path(out).write_bytes(b"x")):
                multilang.speak_long(long_script, Path(tmp) / "a.mp3", "felipe_es",
                                     "bytedance/seed-speech-tts-2.0", "es-mx", speak=counting)
        self.assertGreater(len(calls), 1, "a 17,000 character script must not be one request")
        self.assertTrue(all(n <= lv.TTS_PART_CHAR_LIMIT for n in calls), calls)


class WiringTests(unittest.TestCase):
    def test_it_is_off_unless_the_project_asked(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / lv.STATE_FILE).write_text('{"script": "x", "multilang": false}',
                                                   encoding="utf-8")
            self.assertEqual(lv.build_language_tracks(tmp, Path(tmp) / "v.mp4"), [])

    def test_a_failure_never_reaches_the_caller(self):
        """The render is already the deliverable; extra tracks must not be able to raise."""
        logged = []
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / lv.STATE_FILE).write_text('{"script": "x y z", "multilang": true}',
                                                   encoding="utf-8")
            with mock.patch.object(multilang, "build_tracks", side_effect=RuntimeError("boom")):
                got = lv.build_language_tracks(tmp, Path(tmp) / "v.mp4", logged.append)
        self.assertEqual(got, [])
        self.assertTrue(any("unaffected" in m for m in logged), logged)

    def test_the_render_announces_the_tracks_but_never_starts_them(self):
        """Seven voiceovers must not be spent on a cut nobody has watched yet."""
        source = open(lv.__file__, encoding="utf-8").read()
        for marker in ("def rebuild_from_disk(", "def recut_to_subtitles("):
            body = source[source.index(marker):]
            body = body[:body.index("\ndef ", 10)]
            self.assertIn("announce_language_tracks(", body, marker)
            self.assertNotIn("build_language_tracks(", body, marker)

    def test_the_announcement_is_silent_when_the_switch_is_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / lv.STATE_FILE).write_text('{"multilang": false}', encoding="utf-8")
            self.assertFalse(lv.announce_language_tracks(tmp))
            (Path(tmp) / lv.STATE_FILE).write_text('{"multilang": true}', encoding="utf-8")
            self.assertTrue(lv.announce_language_tracks(tmp))

    def test_the_button_behind_the_finished_video_starts_them(self):
        root = Path(__file__).resolve().parents[1]
        app = open(root / "app.py", encoding="utf-8").read()
        self.assertIn("def start_longform_languages(", app)
        self.assertIn('"/longform-languages"', app)
        shell = open(root / "static" / "chat-shell.js", encoding="utf-8").read()
        self.assertIn('jpost("/longform-languages"', shell)
        self.assertIn("Generate language tracks", shell)

    def test_the_button_works_even_if_the_switch_was_left_off(self):
        """Pressing it IS the consent, so force skips the saved flag."""
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / lv.STATE_FILE).write_text('{"script": "words", "multilang": false}',
                                                   encoding="utf-8")
            with mock.patch.object(multilang, "build_tracks", return_value=[]) as built:
                lv.build_language_tracks(tmp, force=True)
            self.assertTrue(built.called)

    def test_the_switch_reaches_the_run(self):
        app = open(Path(__file__).resolve().parents[1] / "app.py", encoding="utf-8").read()
        self.assertIn('form_flag(fields, "multilang"', app)
        self.assertIn("multilang=multilang", app)
        shell = open(Path(__file__).resolve().parents[1] / "static" / "chat-shell.js",
                     encoding="utf-8").read()
        self.assertIn('fd.append("multilang", "on")', shell)


if __name__ == "__main__":
    unittest.main()
