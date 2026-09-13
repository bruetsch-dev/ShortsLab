"""Action Edit, checked against real ffmpeg output rather than mocks.

The clips are synthesised here (two shots each, a known motion ordering) so the tests own their
inputs, and the one expensive thing - a full build - is done once for the whole module and then
asserted against from several angles.
"""

import hashlib
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import action_editor
import pipeline
from action_editor import ActionEditError

FF = str(pipeline.find_ffmpeg() or "")
LAB = None
BUILD = None


def _clip(dst, secs=3.0):
    """One clip, two shots: dark blue and nearly still, then orange and very busy. The hard cut
    sits exactly in the middle, which is what the boundary test looks for.

    The audio is a tone UNDER broadband noise rather than a bare tone, because a pure sine is
    far peakier than any real diegetic track: the loudness pass then has to choose between the
    -14 LUFS target and the -1 dBTP ceiling, and the test would be measuring that conflict
    instead of measuring the loudness stage.
    """
    half = secs / 2.0
    graph = (f"color=c=0x1a3d5c:s=360x640:d={half}:r=30,noise=alls=4:allf=t[A];"
             f"color=c=0xd94f1e:s=360x640:d={half}:r=30,noise=alls=70:allf=t[B];"
             f"[A][B]concat=n=2:v=1:a=0[v];"
             f"sine=frequency=220:d={secs}:r=48000,volume=0.35[s1];"
             f"anoisesrc=d={secs}:c=pink:r=48000:a=0.25[s2];"
             f"[s1][s2]amix=inputs=2:normalize=0[a]")
    subprocess.run([FF, "-y", "-filter_complex", graph, "-map", "[v]", "-map", "[a]",
                    "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-ar", "48000", "-ac", "2", str(dst)],
                   capture_output=True, timeout=600)


def setUpModule():
    global LAB, BUILD
    if not FF:
        raise unittest.SkipTest("ffmpeg is required for the Action Edit tests")
    LAB = Path(tempfile.mkdtemp(prefix="action_edit_tests_"))
    short = LAB / "shorts" / "01-test"
    short.mkdir(parents=True)
    for i in (1, 2, 3):
        _clip(short / f"clip{i}.mp4")
    (short / "short.toml").write_text('title = "Test Short"\ntext = "he sent it"\n',
                                      encoding="utf-8")
    BUILD = action_editor.build_short(short, LAB / "out" / "01.mp4", root=LAB)


def tearDownModule():
    if LAB:
        shutil.rmtree(LAB, ignore_errors=True)


class ProbeTests(unittest.TestCase):
    """A malformed Short has to stop its own build with a message, not produce a wrong video."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="action_probe_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_landscape_input_is_rejected(self):
        d = self.tmp / "s"
        d.mkdir()
        subprocess.run([FF, "-y", "-f", "lavfi", "-i", "testsrc2=s=640x360:d=1:r=30",
                        "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo:d=1", "-shortest",
                        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                        "-c:a", "aac", str(d / "clip1.mp4")], capture_output=True, timeout=300)
        for i in (2, 3):
            shutil.copy(d / "clip1.mp4", d / f"clip{i}.mp4")
        with self.assertRaises(ActionEditError) as caught:
            action_editor.probe_short(d)
        self.assertIn("9:16", str(caught.exception))

    def test_a_clip_with_no_video_stream_is_rejected(self):
        d = self.tmp / "s2"
        d.mkdir()
        subprocess.run([FF, "-y", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo:d=1",
                        "-c:a", "aac", str(d / "clip1.mp4")], capture_output=True, timeout=300)
        for i in (2, 3):
            shutil.copy(d / "clip1.mp4", d / f"clip{i}.mp4")
        with self.assertRaises(ActionEditError) as caught:
            action_editor.probe_short(d)
        self.assertIn("no video stream", str(caught.exception))

    def test_a_missing_clip_names_what_is_missing(self):
        d = self.tmp / "s3"
        d.mkdir()
        with self.assertRaises(ActionEditError) as caught:
            action_editor.probe_short(d)
        self.assertIn("clip1.mp4", str(caught.exception))


class SceneDetectionTests(unittest.TestCase):
    def test_one_boundary_in_a_two_shot_clip(self):
        clip = LAB / "shorts" / "01-test" / "clip1.mp4"
        cuts = action_editor.detect_boundaries(clip, FF, 0.4)
        self.assertEqual(len(cuts), 1, f"expected exactly one internal cut, got {cuts}")
        self.assertAlmostEqual(cuts[0], 1.5, delta=0.2)     # the clips are 3 s, cut in the middle

    def test_an_odd_boundary_count_warns_instead_of_crashing(self):
        """Detection failing must degrade to one shot, never take the Short down with it."""
        warnings = []
        shots = action_editor.shots_for(0, "x.mp4", 10.0, [1.0, 3.0, 5.0, 7.0], warnings)
        self.assertEqual(len(shots), 1)
        self.assertTrue(any("expected 1" in w for w in warnings))
        warnings = []
        shots = action_editor.shots_for(0, "x.mp4", 10.0, [], warnings)
        self.assertEqual(len(shots), 1)
        self.assertTrue(any("no internal cut" in w for w in warnings))


class AssemblyTests(unittest.TestCase):
    def test_duration_is_the_trimmed_shots_plus_the_hook(self):
        fps = 30.0
        report = BUILD
        shots = report["shots"]
        hook = report["hook"]
        expected = sum(s["end"] - s["start"] for s in shots) + (hook.get("seconds") or 0.0)
        # Ramps deliberately add time, so they are subtracted back out before comparing.
        expected += len(report["ramps"]) * action_editor.ramp_frames_added(fps) / fps
        self.assertAlmostEqual(report["planned_duration_s"], expected, delta=1.0 / fps)

    def test_the_rendered_file_matches_the_plan_within_a_frame(self):
        self.assertAlmostEqual(BUILD["duration_s"], BUILD["planned_duration_s"], delta=1.0 / 30.0)

    def test_the_hook_comes_from_the_busiest_shot(self):
        hook = BUILD["hook"]
        self.assertTrue(hook, "the cold open was not built")
        self.assertLessEqual(hook["seconds"], 1.0)
        busiest = max(range(len(BUILD["shots"])), key=lambda i: BUILD["shots"][i]["motion"])
        self.assertEqual(hook["shot_index"], busiest)

    def test_action_edit_never_uses_the_global_sfx_library(self):
        whooshes = BUILD["audio"]["whooshes"]
        self.assertEqual(whooshes, [])


class RampTests(unittest.TestCase):
    def test_a_ramp_adds_the_expected_number_of_frames(self):
        fps = 30.0
        seg = action_editor.Segment(0, 0.0, 4.0, 1.0, False, 0)
        action_editor.lay_out([seg])
        before = seg.out_duration
        after = action_editor.apply_ramp([seg], 2.0, fps)
        total = sum(s.out_duration for s in after)
        self.assertAlmostEqual(total - before, action_editor.ramp_frames_added(fps) / fps,
                               places=4)

    def test_the_ramp_shape_is_subtle_and_never_freezes_the_action(self):
        seg = action_editor.Segment(0, 0.0, 4.0, 1.0, False, 0)
        action_editor.lay_out([seg])
        speeds = [round(s.speed, 2) for s in action_editor.apply_ramp([seg], 2.0, 30.0)]
        self.assertIn(0.88, speeds, "the restrained hero hold is missing")
        self.assertEqual(speeds.count(0.94), 2, "the subtle ramp in and out pieces are missing")
        self.assertGreaterEqual(min(speeds), 0.85, "an Action Edit must never resemble a freeze")

    def test_a_ramp_is_never_placed_in_the_opening_or_the_closing_seconds(self):
        for at in BUILD["ramps"]:
            self.assertGreaterEqual(at, 1.5)
            self.assertLessEqual(at, BUILD["planned_duration_s"] - 1.0)


class LoudnessTests(unittest.TestCase):
    def test_the_delivered_file_measures_the_platform_target(self):
        out = Path(BUILD["output"])
        res = subprocess.run([FF, "-i", str(out), "-af",
                              "loudnorm=I=-14:TP=-1:LRA=11:print_format=json",
                              "-f", "null", "-"], capture_output=True, text=True, timeout=900)
        text = res.stderr or ""
        stats = json.loads(text[text.rindex("{"):text.rindex("}") + 1])
        self.assertAlmostEqual(float(stats["input_i"]), -14.0, delta=0.5)
        self.assertLessEqual(float(stats["input_tp"]), -0.5)


class DeterminismTests(unittest.TestCase):
    def test_two_runs_of_the_same_short_produce_the_same_bytes(self):
        """Seeded choices and a bit-exact encode; without both, re-running a Short would produce
        a different file and the 21-Short batch could never be trusted to be idempotent."""
        short = LAB / "shorts" / "01-test"
        again = LAB / "out" / "01-again.mp4"
        action_editor.build_short(short, again, root=LAB)
        first = hashlib.sha256(Path(BUILD["output"]).read_bytes()).hexdigest()
        second = hashlib.sha256(again.read_bytes()).hexdigest()
        self.assertEqual(first, second)

    def test_the_seed_does_not_change_between_processes(self):
        """The built-in hash() is salted per process; this is the reason the seed uses sha1."""
        out = subprocess.run(
            ["python", "-c",
             "import action_editor;print(action_editor._seed_for('01-downhill'))"],
            capture_output=True, text=True, timeout=300, cwd=str(Path(action_editor.__file__).parent))
        self.assertEqual(out.stdout.strip(), str(action_editor._seed_for("01-downhill")))


class TextOverlayTests(unittest.TestCase):
    def test_the_text_never_reaches_the_platform_ui_zone(self):
        info = BUILD["text"]
        self.assertTrue(info, "the overlay was not rendered")
        self.assertLess(info["y_fraction"], 0.78,
                        "text in the bottom 22% collides with the platform's own controls")

    def test_no_text_configured_means_no_overlay(self):
        plan = action_editor.EditPlan(segments=[], shots=[])
        cfg = action_editor.load_config()
        cfg["short"] = {}
        draw, info = action_editor.text_overlay(cfg, plan)
        self.assertIsNone(draw)
        self.assertIsNone(info)


class GradeTests(unittest.TestCase):
    def test_the_grade_is_the_same_for_every_short(self):
        """The series look must not depend on the footage - that is what keeps 21 Shorts
        looking like one channel."""
        cfg = action_editor.load_config()
        a, _ = action_editor.grade_chain(cfg, LAB, use_lut=False)
        b, _ = action_editor.grade_chain(cfg, LAB, use_lut=False)
        self.assertEqual(a, b)
        self.assertIn("eq=contrast=1.06:saturation=1.12", a)
        self.assertIn("colorbalance", a)
        # Checked as "the last filter is the grain" rather than by matching the exact string,
        # so adding the seed to it does not read as the ordering having broken.
        self.assertTrue(a.split(",")[-1].startswith("noise="),
                        f"grain has to be the last thing applied, chain ends with {a.split(',')[-1]}")

    def test_grain_can_be_turned_off(self):
        cfg = action_editor.load_config()
        cfg["grade"]["grain"] = 0
        chain, applied = action_editor.grade_chain(cfg, LAB, use_lut=False)
        self.assertNotIn("noise=", chain)
        self.assertNotIn("grain", applied)


class BatchTests(unittest.TestCase):
    def test_a_broken_short_fails_alone(self):
        """One malformed Short must not stop the other 20."""
        root = Path(tempfile.mkdtemp(prefix="action_batch_"))
        self.addCleanup(shutil.rmtree, root, True)
        good = root / "shorts" / "ok"
        good.mkdir(parents=True)
        for i in (1, 2, 3):
            shutil.copy(LAB / "shorts" / "01-test" / f"clip{i}.mp4", good / f"clip{i}.mp4")
        broken = root / "shorts" / "broken"
        broken.mkdir(parents=True)
        shutil.copy(good / "clip1.mp4", broken / "clip1.mp4")     # missing clip2 and clip3
        summary = action_editor.build_batch(root, root / "out", jobs=1)
        self.assertEqual([f["short"] for f in summary["failed"]], ["broken"])
        self.assertEqual(len(summary["built"]), 1)
        self.assertTrue((root / "out" / "ok.mp4").exists())

    def test_an_up_to_date_short_is_skipped_unless_forced(self):
        root = Path(tempfile.mkdtemp(prefix="action_resume_"))
        self.addCleanup(shutil.rmtree, root, True)
        short = root / "shorts" / "ok"
        short.mkdir(parents=True)
        for i in (1, 2, 3):
            shutil.copy(LAB / "shorts" / "01-test" / f"clip{i}.mp4", short / f"clip{i}.mp4")
        first = action_editor.build_batch(root, root / "out", jobs=1)
        self.assertEqual(len(first["built"]), 1)
        second = action_editor.build_batch(root, root / "out", jobs=1)
        self.assertEqual(second["skipped"], ["ok"])


if __name__ == "__main__":
    unittest.main()


class AiEditorTests(unittest.TestCase):
    """The AI editor path crashed on every single run with NameError: 'seen_shots' - the guard
    that stops the model reusing one shot twice was used but never created. Nothing covered this
    path, so it only surfaced in production. These stub the model instead of calling it, so the
    body of the function is actually executed without an API key or a network."""

    def _plan(self):
        shots = [action_editor.Shot(clip_index=i // 2, start=(i % 2) * 5.0,
                                    end=(i % 2) * 5.0 + 5.0, motion=1.0 + i)
                 for i in range(6)]
        segments = [action_editor.Segment(s.clip_index, s.start, s.end, 1.0, False, i)
                    for i, s in enumerate(shots)]
        action_editor.lay_out(segments)
        return action_editor.EditPlan(segments=segments, shots=shots)

    def _run(self, make_answer):
        """make_answer(plan) builds the stubbed model reply FROM the plan, so the segment times
        always sit inside the shots they name - guessing them produced zero-length ranges that
        the clamp silently dropped, and the test measured the wrong thing."""
        import scrape_v2
        plan = self._plan()
        answer = make_answer(plan) if callable(make_answer) else make_answer
        cfg = action_editor.load_config()
        cfg["short"] = {}
        signals = [__import__("numpy").ones(300, dtype="float32") for _ in range(3)]
        with mock.patch.object(action_editor, "_editor_contact_sheet",
                               return_value=Path("sheet.jpg")), \
             mock.patch.object(scrape_v2, "_vision_json", return_value=answer):
            return action_editor.apply_ai_editor(plan, ["a.mp4", "b.mp4", "c.mp4"], signals,
                                                 cfg, Path("."), "ffmpeg", "google/gemini-3.5-flash-lite")

    @staticmethod
    def _seg(plan, number):
        shot = plan.shots[number - 1]
        return {"shot": number, "start": shot.start, "end": shot.end}

    @staticmethod
    def _hook(plan, number=5):
        shot = plan.shots[number - 1]
        return {"shot": number, "start": shot.start + 0.2, "end": shot.start + 1.4}

    def test_a_usable_answer_is_applied(self):
        new_plan, info = self._run(lambda p: {
            "hook": self._hook(p),
            "segments": [self._seg(p, n) for n in (2, 3, 4, 6)],
            "slow_shot": 4, "why": "strongest payoff last"})
        self.assertTrue(info.get("applied"), info.get("reason"))
        self.assertEqual(info.get("why"), "strongest payoff last")
        self.assertTrue(any(s.is_hook for s in new_plan.segments))

    def test_the_same_shot_is_never_used_twice(self):
        """This is what seen_shots is for; the crash was in the line that reads it."""
        new_plan, info = self._run(lambda p: {
            "hook": self._hook(p),
            "segments": [self._seg(p, 2), self._seg(p, 2),      # the duplicate
                         self._seg(p, 3), self._seg(p, 4), self._seg(p, 6)],
            "slow_shot": 0, "why": "dupes"})
        self.assertTrue(info.get("applied"), info.get("reason"))
        body = [s.shot_index for s in new_plan.segments if not s.is_hook]
        self.assertEqual(len(body), len(set(body)), f"a shot was used twice: {body}")

    def test_a_junk_answer_falls_back_to_the_local_cut(self):
        for answer in ({}, {"segments": []}, {"segments": [{"shot": 99, "start": 0, "end": 3}]}):
            _, info = self._run(answer)
            self.assertFalse(info.get("applied"))
            self.assertIn("local cut", info.get("reason", ""))

    def test_local_needs_no_model_at_all(self):
        plan = self._plan()
        cfg = action_editor.load_config(); cfg["short"] = {}
        same, info = action_editor.apply_ai_editor(plan, [], [], cfg, Path("."), "ffmpeg", "local")
        self.assertIs(same, plan)
        self.assertFalse(info["applied"])
