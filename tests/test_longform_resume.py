"""Longform resume must never re-buy TTS that is already on disk.

Every voiceover part is a paid Gemini TTS call, and the parts are only stitched (and deleted)
once the approval gate has passed - so a run cancelled in the gate leaves the parts behind with
no voiceover.wav. These tests pin the rule "an existing, healthy part is reused; anything that
cannot be proven to belong to this script and narrator is not".

Run: python tests/test_longform_resume.py
"""
import json
import inspect
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
    """A script fat enough to split into several TTS parts.

    Sized against the CURRENT limit rather than a fixed paragraph count: the limit is a measured
    property of the TTS endpoint and has already moved once (2200 -> 4400 chars, worth ~4.4
    minutes of speech in one call). A hardcoded script silently stopped producing enough parts,
    setUp's assertion failed, and because tearDown does not run after a failed setUp the fake TTS
    stayed patched over the real one for every later test in the process.
    """
    def paragraph(index):
        return ("%s sentence number %d here. " % (word, index)) * 45
    needed = max(8, lf.TTS_PART_CHAR_LIMIT * 5 // len(paragraph(0)) + 1)
    return (chr(10) * 2).join(paragraph(i) for i in range(needed))


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

    def test_stitching_keeps_reviewable_parts_in_the_state(self):
        self.assertEqual(self.run_vo(gate=self.gate_ok), self.parts)
        self.assertTrue((self.dir / "voiceover.wav").exists())
        kept = list(self.dir.glob("vo_part*.wav"))
        self.assertEqual(len(kept), self.parts, "approved parts remain individually reviewable")
        self.assertEqual(len(self.state().get("tts_part_files") or []), self.parts)
        self.assertEqual(self.state().get("tts_part_texts"), lf.split_script_for_tts(self.script))

    def test_reviewed_speech_invalidates_old_clock_but_keeps_retime_source(self):
        self.run_vo(gate=self.gate_ok)
        old_lines = [{"start": 0.0, "end": 1.0, "text": "First"}]
        old_prompts = [{"timestamp": "[0:00.0]", "prompt": "first"}]
        lf.save_state(self.dir, lines=old_lines, prompts=old_prompts, audio_duration=8.0,
                      voice_speed=1.0)
        manifest = lf.load_speech_review_parts(self.dir, self.script)
        self.assertIsNotNone(manifest)
        selected = lf.commit_speech_review_parts(self.dir, manifest)
        state = self.state()
        self.assertTrue(selected.exists())
        self.assertIsNone(state.get("lines"))
        self.assertIsNone(state.get("prompts"))
        self.assertEqual(state.get("retime_source_lines"), old_lines)
        self.assertEqual(state.get("retime_source_prompts"), old_prompts)
        self.assertEqual(state.get("retime_source_audio_duration"), 8.0)
        # Reviewing another part before continuing must not erase the original image clock.
        lf.commit_speech_review_parts(self.dir, manifest)
        state = self.state()
        self.assertEqual(state.get("retime_source_lines"), old_lines)
        self.assertEqual(state.get("retime_source_prompts"), old_prompts)
        self.assertEqual(state.get("retime_source_audio_duration"), 8.0)

    def test_review_parts_use_the_projects_selected_voice_speed(self):
        self.run_vo(gate=self.gate_ok)
        lf.save_state(self.dir, voice_speed=1.15)
        retained = [Path(path) for path in self.state().get("tts_part_files") or []]
        with mock.patch.object(lf, "apply_voice_speed", side_effect=lambda path, speed: Path(path)) as apply:
            manifest = lf.load_speech_review_parts(self.dir, self.script)
        self.assertEqual(apply.call_count, len(retained))
        self.assertTrue(all(call.args[1] == 1.15 for call in apply.call_args_list))
        self.assertEqual(manifest.get("voice_speed"), 1.15)

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

    def test_assembly_refuses_missing_scenes_instead_of_inserting_black(self):
        lines = [{"start": 0.0, "end": 1.0, "text": "Visible narration"}]
        with self.assertRaisesRegex(lf.LongformError, "black/missing scene"):
            lf.assemble_video(lines, [1.0], {}, self.dir / "voiceover.wav",
                              self.dir / "must_not_render.mp4")
        self.assertFalse((self.dir / "must_not_render.mp4").exists())

    def test_a_retimed_cut_does_not_lose_its_image(self):
        """image_key bakes the on-screen duration into the filename so the length is visible in
        the folder - which means the name changes whenever a cut is retimed, and assembly then
        calls a scene it can plainly see on disk missing. One project had 228 images, 227 names
        matching, and the first at dur4.60s against an expected dur4.80s."""
        from PIL import Image
        images = self.dir / "images"
        images.mkdir(exist_ok=True)
        line = {"start": 0.2, "end": 4.8, "text": "First"}
        drawn = images / (lf.image_key(0, line, 4.60) + ".png")
        Image.new("RGB", (1600, 900), "white").save(drawn)
        found = lf.resolve_image_for(images, 0, line, 4.80, "16:9")
        self.assertIsNotNone(found, "the cut was retimed, the picture did not move")
        self.assertEqual(found.name, drawn.name)

    def test_an_image_from_a_different_line_is_still_refused(self):
        """The exact key existed to stop a frame being silently mismatched to another line.
        Tolerating a changed duration must not tolerate a changed timestamp."""
        from PIL import Image
        images = self.dir / "images"
        images.mkdir(exist_ok=True)
        Image.new("RGB", (1600, 900), "white").save(
            images / (lf.image_key(0, {"start": 0.2, "end": 4.8, "text": "a"}, 4.60) + ".png"))
        moved = {"start": 91.5, "end": 95.0, "text": "a"}
        self.assertIsNone(lf.resolve_image_for(images, 0, moved, 4.60, "16:9"))

    def test_missing_frame_holds_nearest_existing_project_image(self):
        from PIL import Image

        lines = [{"start": 0.0, "end": 1.0, "text": "First"},
                 {"start": 1.0, "end": 2.0, "text": "Second"}]
        prompts = [{"timestamp": "[0:00.0]", "prompt": 'reading "FIRST"'},
                   {"timestamp": "[0:01.0]", "prompt": 'reading "SECOND"'}]
        lf.save_state(self.dir, script="First. Second.", lines=lines, prompts=prompts,
                      audio_duration=2.0)
        images = self.dir / "images"; images.mkdir()
        durations = lf.line_durations(lines, 2.0)
        first = images / f"{lf.image_key(0, lines[0], durations[0])}.png"
        Image.new("RGB", (1600, 900), "white").save(first)
        # `reassign_mismatched` no longer exists: recovery now HOLDS the nearest valid
        # neighbouring timeline image for an empty slot instead of reassigning a mismatched one,
        # so there is nothing left to stub out here.
        recovered = lf.recover_archived_images(self.dir)
        second = images / f"{lf.image_key(1, lines[1], durations[1])}.png"
        self.assertEqual(recovered, 1)
        self.assertTrue(lf._image_done(second, "16:9"))

    def test_missing_frame_restores_its_prompt_repair_image_before_holding_neighbour(self):
        """Interrupted prompt repair must not turn usable art into a repeated adjacent still."""
        from PIL import Image

        lines = [{"start": 0.0, "end": 1.0, "text": "First"},
                 {"start": 1.0, "end": 2.0, "text": "Second"}]
        prompts = [{"timestamp": "[0:00.0]", "prompt": "first"},
                   {"timestamp": "[0:01.0]", "prompt": "second"}]
        lf.save_state(self.dir, script="First. Second.", lines=lines, prompts=prompts,
                      audio_duration=2.0, aspect="16:9")
        images = self.dir / "images"; images.mkdir()
        durations = lf.line_durations(lines, 2.0)
        # A valid current first frame makes the old nearest-neighbour fallback available.
        first = images / f"{lf.image_key(0, lines[0], durations[0])}.png"
        Image.new("RGB", (1600, 900), "white").save(first)
        # The second picture was parked by _archive_images_from_index before regeneration.
        repair = images / "_prompt_repair_20260902_000000"; repair.mkdir()
        second = repair / f"{lf.image_key(1, lines[1], durations[1])}.png"
        Image.new("RGB", (1600, 900), "blue").save(second)

        recovered = lf.recover_archived_images(self.dir)
        active = images / f"{lf.image_key(1, lines[1], durations[1])}.png"
        self.assertEqual(recovered, 1)
        self.assertTrue(active.is_file())
        self.assertFalse(second.exists(), "restored art is no longer falsely listed as unused")
        with Image.open(active) as restored:
            self.assertEqual(restored.getpixel((0, 0)), (0, 0, 255))

    def test_manual_unused_art_is_not_automatically_restored(self):
        """Replacing a frame in the editor must remain a deliberate choice."""
        from PIL import Image

        lines = [{"start": 0.0, "end": 1.0, "text": "Only"}]
        prompts = [{"timestamp": "[0:00.0]", "prompt": "only"}]
        lf.save_state(self.dir, script="Only.", lines=lines, prompts=prompts,
                      audio_duration=1.0, aspect="16:9")
        images = self.dir / "images"; images.mkdir()
        manual = images / "_manual_unused_20260902_000000"; manual.mkdir()
        Image.new("RGB", (1600, 900), "blue").save(
            manual / f"{lf.image_key(0, lines[0], 1.0)}.png")

        self.assertEqual(lf.recover_archived_images(self.dir), 0)
        self.assertFalse((images / f"{lf.image_key(0, lines[0], 1.0)}.png").exists())

    def test_run_recovers_archived_prompt_repair_frames_before_timeline_verification(self):
        """A partial image batch must produce a complete editable timeline when old art exists."""
        body = inspect.getsource(lf.run_longform_video)
        self.assertIn("restored = recover_archived_images(out_dir", body)
        self.assertLess(body.index("restored = recover_archived_images(out_dir"),
                        body.index("missing = verify_images("))

    def test_thumbnail_generation_always_creates_three_titled_options(self):
        from PIL import Image

        concepts = [
            {"hook": "FIRST HOOK", "title": "First matching title", "subject": "first scene"},
            {"hook": "SECOND HOOK", "title": "Second matching title", "subject": "second scene"},
            {"hook": "THIRD HOOK", "title": "Third matching title", "subject": "third scene"},
        ]

        generated, sent_prompts = [], []
        def fake_wavespeed(prompt, path, _key, *_args, **_kwargs):
            idx = len(generated)
            Image.new("RGB", (1600, 900), (40 + idx * 30, 70, 90)).save(path)
            sent_prompts.append(prompt)
            generated.append(path)
            return path

        with mock.patch.object(lf, "build_thumbnail_concepts", return_value=concepts), \
                mock.patch.object(lf.pipeline, "api_key", return_value="test-key"), \
                mock.patch.object(lf, "_generate_wavespeed_thumbnail", side_effect=fake_wavespeed):
            selected = lf.generate_thumbnail(
                self.dir, "A sufficiently long thumbnail test script about a strange event.",
                [{"start": 0.0, "end": 1.0, "text": "A strange event happened."}], force=True)

        variants = lf.thumbnail_variants(self.dir)
        self.assertTrue(lf._image_done(selected, "16:9"))
        self.assertEqual(len(variants), 3)
        self.assertEqual(len(generated), 3)
        self.assertTrue(all("NO text" in prompt and "NO split panels" in prompt
                            for prompt in sent_prompts))
        self.assertEqual([v["title"] for v in variants], [c["title"] for c in concepts])
        self.assertEqual([v["selected"] for v in variants], [True, False, False])

    def test_thumbnail_image_request_uses_wavespeed_gpt_image_2_medium(self):
        from PIL import Image

        target = self.dir / "thumb.png"
        captured = {}

        def fake_request(method, url, key, payload, timeout=0):
            captured.update(method=method, url=url, key=key, payload=payload, timeout=timeout)
            return {"data": {"id": "prediction-1"}}

        def fake_download(_url, path):
            Image.new("RGB", (1600, 900), "white").save(path)
            return Path(path).stat().st_size

        with mock.patch.object(lf.pipeline, "request_json", side_effect=fake_request), \
                mock.patch.object(lf.pipeline, "poll_wavespeed",
                                  return_value=(["https://example.invalid/thumb.png"], {})), \
                mock.patch.object(lf.pipeline, "download_file", side_effect=fake_download):
            result = lf._generate_wavespeed_thumbnail(
                "Minimal doodle. NO text.", target, "secret-test-key")

        self.assertEqual(result, str(target))
        self.assertTrue(captured["url"].endswith("/openai/gpt-image-2/text-to-image"))
        self.assertEqual(captured["payload"]["quality"], "medium")
        self.assertEqual(captured["payload"]["resolution"], "1k")
        self.assertEqual(captured["payload"]["aspect_ratio"], "16:9")

    def test_prompt_shortfall_recovers_only_missing_slots_and_checkpoints(self):
        lines = [
            {"start": 0.0, "end": 1.0, "text": "First line"},
            {"start": 1.0, "end": 2.0, "text": "Second line"},
            {"start": 2.0, "end": 3.0, "text": "Third line"},
        ]
        first = {"choices": [{"message": {"content":
            "[0:00.0] first doodle prompt\n[0:01.0] second doodle prompt\n"
            "All image prompts are now delivered - one for every timestamp in your script."}}]}
        recovery = {"choices": [{"message": {"content":
            "[0:02.0] recovered third doodle prompt"}}]}
        checkpoint = self.dir / "prompt_checkpoint.json"
        with mock.patch.dict("os.environ", {"WAVESPEED_API_KEY": "test-key"}, clear=False), \
                mock.patch.object(lf.agent_core, "post_json_url",
                                  side_effect=[first, recovery]) as request:
            prompts = lf.generate_image_prompts(
                lines, checkpoint_path=checkpoint, reasoning_model="test/model")
        self.assertEqual(len(prompts), 3)
        self.assertEqual(request.call_count, 2)
        self.assertIn("recovered third", prompts[2]["prompt"])
        saved = json.loads(checkpoint.read_text(encoding="utf-8"))
        self.assertEqual(len(saved["prompts"]), 3)

        # A resumed process trusts the exact line-clock checkpoint and makes no paid request.
        with mock.patch.dict("os.environ", {"WAVESPEED_API_KEY": "test-key"}, clear=False), \
                mock.patch.object(lf.agent_core, "post_json_url") as no_request:
            resumed = lf.generate_image_prompts(lines, checkpoint_path=checkpoint)
        self.assertEqual(len(resumed), 3)
        no_request.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
