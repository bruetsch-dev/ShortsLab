"""Longform resume must never re-buy TTS that is already on disk.

Every voiceover part is a paid Gemini TTS call, and the parts are only stitched (and deleted)
once the approval gate has passed - so a run cancelled in the gate leaves the parts behind with
no voiceover.wav. These tests pin the rule "an existing, healthy part is reused; anything that
cannot be proven to belong to this script and narrator is not".

Run: python tests/test_longform_resume.py
"""
import json
import shutil
import struct
import sys
import tempfile
import unittest
import wave
import zlib
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import longform_video as lf


class _FakeTTS:
    """Stands in for pipeline.generate_speech_gemini: counts calls, writes a real wav whose
    samples depend on the text (identical audio for different text would hide mix-ups)."""

    def __init__(self):
        self.calls = []
        self.die_after = None

    def __call__(self, text, path, model=None, cancel_event=None, status_cb=None, **kw):
        if self.die_after is not None and len(self.calls) >= self.die_after:
            raise RuntimeError("Gemini TTS 503 (simulated outage)")
        self.calls.append(Path(path).name)
        seed = zlib.crc32(text.encode("utf-8")) % 2000 + 200
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(p), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(24000)
            w.writeframes(b"".join(struct.pack("<h", (i * seed) % 3000) for i in range(24000)))
        return p


class _Killed(Exception):
    """The user closed the browser / the server restarted while the gate was open."""


def _script(word):
    """A script fat enough to split into several TTS parts."""
    return "\n\n".join(("%s sentence number %d here. " % (word, i)) * 45 for i in range(8))


class LongformResumeTest(unittest.TestCase):
    def setUp(self):
        self.tts = _FakeTTS()
        self._real_tts = lf.pipeline.generate_speech_gemini
        lf.pipeline.generate_speech_gemini = self.tts
        self.dir = Path(tempfile.mkdtemp(prefix="lfresume_"))
        self.script = _script("Alpha")
        self.parts = len(lf.split_script_for_tts(self.script))
        self.assertGreaterEqual(self.parts, 4, "need a multi-part script")
        self.seen = []

    def tearDown(self):
        lf.pipeline.generate_speech_gemini = self._real_tts
        shutil.rmtree(self.dir, ignore_errors=True)

    # -- helpers ---------------------------------------------------------------
    def gate_ok(self, parts_info, regen_part):
        # the parts are deleted right after stitching, so record them while they exist
        self.seen.append([(Path(p["path"]).name, Path(p["path"]).stat().st_size)
                          for p in parts_info])
        return True

    def gate_dies(self, parts_info, regen_part):
        self.gate_ok(parts_info, regen_part)
        raise _Killed("killed in the approval gate")

    def run_vo(self, gate=None, script=None, voice="Kore", resume=True, die_after=None):
        """Returns the number of NEW (paid) TTS calls this run made."""
        self.tts.calls.clear()
        self.tts.die_after = die_after
        try:
            lf.generate_voiceover(script or self.script, self.dir, speech_gate=gate,
                                  voice=voice, resume=resume)
        except (_Killed, RuntimeError):
            pass
        return len(self.tts.calls)

    def state(self):
        try:
            return json.loads((self.dir / lf.STATE_FILE).read_text(encoding="utf-8"))
        except OSError:
            return {}

    # -- tests -----------------------------------------------------------------
    def test_killed_in_gate_resumes_into_gate_for_free(self):
        self.assertEqual(self.run_vo(gate=self.gate_dies), self.parts)
        self.assertFalse((self.dir / "voiceover.wav").exists(),
                         "the gate never passed, so nothing may be stitched")
        self.assertEqual(self.run_vo(gate=self.gate_dies), 0, "resume must not re-buy any part")
        self.assertEqual(len(self.seen[-1]), self.parts, "the gate must see every part again")

    def test_tts_outage_only_repays_the_missing_parts(self):
        self.assertEqual(self.run_vo(gate=self.gate_ok, die_after=2), 2)
        self.assertEqual(self.run_vo(gate=self.gate_ok), self.parts - 2)

    def test_truncated_part_is_not_trusted(self):
        self.run_vo(gate=self.gate_ok, die_after=2)
        stub = sorted(self.dir.glob("vo_part*.wav"))[1]
        stub.write_bytes(b"RIFF\x00\x00\x00\x00WAVE")        # a failed/partial write
        self.assertEqual(self.run_vo(gate=self.gate_ok), self.parts - 1)
        self.assertGreater(self.seen[-1][1][1], lf.MIN_AUDIO_BYTES, "the stub must be replaced")

    def test_reused_tail_survives_a_kill_in_the_gate(self):
        self.run_vo(gate=self.gate_dies)
        sorted(self.dir.glob("vo_part*.wav"))[0].unlink()     # lose only part 0
        self.assertEqual(self.run_vo(gate=self.gate_dies), 1)
        self.assertEqual(self.run_vo(gate=self.gate_dies), 0,
                         "the parts reused (not regenerated) last run must be remembered too")

    def test_regenerated_take_is_reused_not_repaid(self):
        self.run_vo(gate=self.gate_dies)

        def decline_then_die(parts_info, regen_part):
            regen_part(0)                                     # user declines part 1
            raise _Killed("killed right after the regenerate")

        self.assertEqual(self.run_vo(gate=decline_then_die), 1, "the new take costs one call")
        self.assertIn("take1", (self.state().get("tts_part_files") or [""])[0])
        self.assertEqual(self.run_vo(gate=self.gate_dies), 0, "the take must not be re-bought")

    def test_stitching_clears_the_parts_from_the_state(self):
        self.assertEqual(self.run_vo(gate=self.gate_ok), self.parts)
        self.assertTrue((self.dir / "voiceover.wav").exists())
        self.assertEqual(list(self.dir.glob("vo_part*.wav")), [], "parts are cleaned up")
        self.assertEqual(self.state().get("tts_part_files"), [],
                         "the state must not point at deleted files")

    def test_changed_script_reuses_nothing_and_leaves_no_stale_timings(self):
        self.run_vo(gate=self.gate_ok)
        lf.save_state(self.dir, lines=[{"start": 0.0, "end": 1.0, "text": "ALPHA"}],
                      prompts=["alpha prompt"], audio_duration=42.0)
        old_vo = (self.dir / "voiceover.wav").read_bytes()

        beta = _script("Beta")
        self.assertGreater(self.run_vo(gate=self.gate_dies, script=beta), 0,
                           "a different script may not reuse the old parts")
        st = self.state()
        self.assertEqual(st.get("script"), beta)
        # the old lines/prompts must not survive next to the new script: run_longform_video reads
        # exactly these keys to decide "resume", and would pair Beta with Alpha's audio
        self.assertIsNone(st.get("lines"))
        self.assertIsNone(st.get("prompts"))
        reusable = bool(st.get("lines") and (self.dir / "voiceover.wav").exists())
        self.assertFalse(reusable, "Beta must not resume onto Alpha's voiceover")

        self.run_vo(gate=self.gate_ok, script=beta)
        self.assertNotEqual((self.dir / "voiceover.wav").read_bytes(), old_vo,
                            "Beta's audio must replace Alpha's")

    def test_unchanged_script_still_resumes(self):
        """The guard rails above must not break the feature they protect."""
        self.run_vo(gate=self.gate_ok)
        lf.save_state(self.dir, lines=[{"start": 0.0, "end": 1.0, "text": "ALPHA"}])
        st = lf.load_state(self.dir, self.script)
        self.assertTrue(bool(st and st.get("lines") and (self.dir / "voiceover.wav").exists()))

    def test_narrator_change_and_resume_off_reuse_nothing(self):
        self.run_vo(gate=self.gate_dies, voice="Kore")
        self.assertEqual(self.run_vo(gate=self.gate_dies, voice="Sulafat"), self.parts,
                         "another narrator means the parts are the wrong voice")
        self.assertEqual(self.run_vo(gate=self.gate_dies, voice="Sulafat", resume=False),
                         self.parts, "resume=False must ignore the disk")

    def test_voice_speed_never_replaces_the_file_playing_in_the_browser(self):
        source = self.dir / "voiceover.wav"
        original = b"R" * (lf.MIN_AUDIO_BYTES + 100)
        source.write_bytes(original)

        def fake_ffmpeg(command, **_kwargs):
            Path(command[-1]).write_bytes(b"S" * (lf.MIN_AUDIO_BYTES + 200))
            return SimpleNamespace(stderr="")

        with mock.patch.object(lf.subprocess, "run", side_effect=fake_ffmpeg), \
             mock.patch.object(lf.pipeline, "atempo_filter_chain", return_value="atempo=1.15"):
            sped = lf.apply_voice_speed(source, 1.15, ffmpeg="ffmpeg")

        self.assertNotEqual(sped, source)
        self.assertIn("_speed_1p15x_", sped.name)
        self.assertEqual(source.read_bytes(), original,
                         "the streamed approval file must remain immutable on Windows")
        self.assertGreater(sped.stat().st_size, lf.MIN_AUDIO_BYTES)
        lf.save_state(self.dir, voiceover_file=sped.name)
        self.assertEqual(lf.voiceover_path_from_state(self.dir, self.state()), sped)

    def test_resume_after_speed_gate_uses_and_records_the_new_immutable_file(self):
        raw = self.dir / "voiceover.wav"
        raw.write_bytes(b"R" * (lf.MIN_AUDIO_BYTES + 100))
        sped = self.dir / "voiceover_speed_1p15x_test.wav"
        sped.write_bytes(b"S" * (lf.MIN_AUDIO_BYTES + 100))
        lf.save_state(
            self.dir, script=self.script, voice="Kore", tts_style=lf.pipeline.TTS_STYLE_LONGFORM,
            voiceover_ready=True, voice_speed=None, voiceover_file=raw.name,
            tts_parts=self.parts)

        with mock.patch.object(lf.pipeline, "find_ffmpeg", return_value="ffmpeg"), \
             mock.patch.object(lf, "apply_voice_speed", return_value=sped) as apply:
            selected, count = lf.generate_voiceover(
                self.script, self.dir, voice="Kore", resume=True, mix_gate=lambda _path: 1.15)

        self.assertEqual(selected, sped)
        self.assertEqual(count, self.parts)
        apply.assert_called_once_with(raw, 1.15, "ffmpeg", status_cb=None)
        state = self.state()
        self.assertEqual(state["voice_speed"], 1.15)
        self.assertEqual(state["voiceover_file"], sped.name)

    def test_speed_change_renames_images_and_rewrites_their_timeline_positions(self):
        old_lines = [
            {"start": 0.0, "end": 4.5, "text": "First line"},
            {"start": 5.0, "end": 9.5, "text": "Second line"},
        ]
        new_lines = [
            {"start": 0.0, "end": 3.5, "text": "First line"},
            {"start": 4.0, "end": 7.5, "text": "Second line"},
        ]
        images = self.dir / "images"
        images.mkdir()
        old_durations = lf.line_durations(old_lines, 10.0)
        old_paths = []
        for idx, line in enumerate(old_lines):
            path = images / f"{lf.image_key(idx, line, old_durations[idx])}.png"
            path.write_bytes(b"P" * (lf.MIN_IMAGE_BYTES + idx + 1))
            old_paths.append(path)

        prompts = [{"timestamp": "[0:00.0]", "prompt": "first"},
                   {"timestamp": "[0:05.0]", "prompt": "second"}]
        updated = lf.retime_longform_assets(
            self.dir, old_lines, new_lines, 10.0, 8.0, prompts=prompts)
        new_durations = lf.line_durations(new_lines, 8.0)
        new_paths = [images / f"{lf.image_key(i, line, new_durations[i])}.png"
                     for i, line in enumerate(new_lines)]

        self.assertTrue(all(p.exists() for p in new_paths))
        self.assertTrue(all(not p.exists() for p in old_paths))
        self.assertEqual([p["timestamp"] for p in updated], ["[0:00.0]", "[0:04.0]"])

        manifest = lf.write_timeline_manifest(
            new_lines, new_durations, {0: str(new_paths[0]), 1: str(new_paths[1])},
            8.0, self.dir / "timeline.json", voice_speed=1.25)
        data = json.loads(manifest.read_text(encoding="utf-8"))
        self.assertEqual(data["voice_speed"], 1.25)
        self.assertEqual([(row["start"], row["end"], row["image"])
                          for row in data["scenes"]],
                         [(0.0, 4.0, new_paths[0].name),
                          (4.0, 8.0, new_paths[1].name)])

    def test_longform_resume_rejects_a_healthy_but_portrait_image(self):
        from PIL import Image

        portrait = self.dir / "portrait.png"
        landscape = self.dir / "landscape.png"
        Image.new("RGB", (384, 516), "red").save(portrait)
        Image.new("RGB", (1600, 900), "green").save(landscape)

        self.assertTrue(lf._image_done(portrait), "the file itself is healthy")
        self.assertFalse(lf._image_done(portrait, "16:9"),
                         "a 3:4 generation may never be reused in the longform timeline")
        self.assertTrue(lf._image_done(landscape, "16:9"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
