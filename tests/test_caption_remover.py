import math
import os
import inspect
import unittest
from pathlib import Path

import caption_remover as cr

try:
    import cv2
    import numpy as np
except ImportError:      # pragma: no cover
    cv2 = None
    np = None


def _panning_clip_with_caption(frames=30, width=320, height=180, text="FIND YOUR LOCKER"):
    """A moving background with a caption nailed to the same pixels - the real situation.

    The caption is what stays still while everything else slides, which is exactly the signal the
    detector is supposed to key on.
    """
    out = []
    base = np.zeros((height, width * 3, 3), np.uint8)
    for x in range(width * 3):                       # vertical stripes so panning is visible
        base[:, x] = (60 + (x // 7 % 5) * 25, 70 + (x // 11 % 4) * 20, 80)
    for index in range(frames):
        shift = int(index * (width * 2) / max(1, frames - 1))
        frame = base[:, shift:shift + width].copy()
        cv2.putText(frame, text, (18, height // 2), cv2.FONT_HERSHEY_SIMPLEX,
                    0.6, (250, 250, 250), 2, cv2.LINE_AA)
        out.append(frame)
    return out


class MaskDetectionTests(unittest.TestCase):
    """The OCR pass returned two of three words on a real clip. A burned-in caption sits in the
    same pixels for the whole clip, so averaging the top-hat over frames strengthens it while a
    moving highlight averages away."""

    def setUp(self):
        if cv2 is None:
            self.skipTest("OpenCV unavailable")

    def test_it_finds_the_caption_and_not_the_moving_background(self):
        frames = _panning_clip_with_caption()
        mask = cr.detect_caption_mask(frames)
        self.assertIsNotNone(mask, "the caption was not found at all")
        height, width = frames[0].shape[:2]
        coverage = float((mask > 32).mean())
        self.assertGreater(coverage, 0.01, "almost nothing was masked")
        self.assertLess(coverage, 0.30, "the mask swallowed the background")
        ys, _xs = np.where(mask > 32)
        # It has to sit on the text line, not somewhere else in the picture.
        self.assertLess(abs(int(ys.mean()) - height // 2), height * 0.18)

    def test_a_clip_with_no_caption_yields_no_mask(self):
        frames = _panning_clip_with_caption(text="")
        mask = cr.detect_caption_mask(frames)
        if mask is not None:
            self.assertLess(float((mask > 32).mean()), 0.01,
                            "clean footage must not be masked")

    def test_it_survives_being_given_nothing(self):
        self.assertIsNone(cr.detect_caption_mask([]))
        self.assertIsNone(cr.detect_caption_mask(None))


class RemovalContractTests(unittest.TestCase):
    def test_the_gpu_path_is_checked_never_assumed(self):
        """A missing checkout or a broken driver is not an available GPU."""
        self.assertIn(cr.propainter_available(), (True, False))

    def test_too_much_text_is_refused_with_a_reason(self):
        """"Nothing to remove" and "too much to remove" are different answers; a caller that
        cannot tell them apart uses a text slide as if it were clean footage."""
        import inspect
        src = inspect.getsource(cr.remove_caption_regions)
        self.assertIn('info["refused"] = True', src)
        self.assertIn("MAX_COVERAGE", src)

    def test_the_full_resolution_picture_is_not_downscaled_by_the_fill(self):
        """ProPainter runs on a downscaled copy to fit the card. Only the FILLED REGION goes back
        into the original, or the whole clip would lose its resolution for one caption line."""
        import inspect
        src = inspect.getsource(cr.remove_caption_regions)
        self.assertIn("alphamerge", src)
        self.assertIn("overlay", src)
        self.assertIn("[0:v]", src, "the untouched original has to be the base layer")

    def test_ffprobe_is_resolved_not_guessed_from_the_ffmpeg_path(self):
        """The install directory is itself called ffmpeg-8.1.1-full_build, so replacing every
        "ffmpeg" in the path mangled the directory and every probe failed silently."""
        import inspect
        src = inspect.getsource(cr._dimensions)
        self.assertNotIn('replace("ffmpeg", "ffprobe")', src)
        self.assertIn("_ffmpeg_tools", src)

    def test_the_throttles_cost_time_and_not_quality(self):
        """The obvious knobs are the wrong ones: working resolution, RAFT iterations and
        ref_stride all make the fill worse. A VRAM cap and a lower CPU priority change no
        arithmetic at all - the same work runs, just with a smaller allocator pool and a later
        slice of the scheduler."""
        import inspect
        src = inspect.getsource(cr._run_propainter)
        self.assertIn("set_per_process_memory_fraction", src)
        self.assertIn("BELOW_NORMAL_PRIORITY_CLASS", src)
        # Quality-relevant arguments must stay at their defaults.
        self.assertNotIn("--raft_iter", src, "fewer flow iterations is a quality cut")
        self.assertNotIn("--ref_stride", src, "fewer reference frames is a quality cut")
        self.assertNotIn("--resize_ratio", src, "downscaling further is a quality cut")
        self.assertEqual(cr.WORK_WIDTH, 540, "the working resolution is not a throttle")

    def test_the_vram_cap_is_a_knob_not_a_constant(self):
        self.assertGreater(cr.VRAM_FRACTION, 0.0)
        self.assertLessEqual(cr.VRAM_FRACTION, 1.0)

    def test_the_gpu_only_runs_on_an_explicit_render(self):
        """ProPainter takes the whole card - 100% utilisation and 10.7 of 11.3 GB of VRAM - and
        the same card draws the desktop. Running it during a scrape or a project open made the
        machine stutter for work nobody was waiting on."""
        import inspect
        import agent_core
        # allow_gpu is an opt-OUT: the scrape probe and the save path pass False deliberately,
        # while a caller that says nothing gets its clip cleaned. Defaulting it to False meant
        # four of the seven call sites did nothing at all and blamed the backend for it.
        self.assertIsNone(
            inspect.signature(cr.remove_caption_regions).parameters["allow_gpu"].default)
        self.assertIs(
            inspect.signature(agent_core.apply_timeline_edits_to_config)
            .parameters["allow_gpu"].default, False)
        import scrape_v2
        self.assertIn("allow_gpu=False", inspect.getsource(scrape_v2._captions_are_removable),
                      "the scrape probe must keep the GPU policy in writing")
        # Both RENDER paths turn it on: the timeline editor's Render, and the pre-render
        # caption cleanup of a normal run. The second was missing until 2026-08-29, so a
        # delivered Clip Short shipped with the source's own Japanese captions burned across
        # the last shot while the log said "inpainted, not blurred".
        self.assertIn("allow_gpu=True",
                      inspect.getsource(agent_core.render_project_timeline))
        self.assertNotIn("allow_gpu=True",
                         inspect.getsource(agent_core.apply_timeline_edits_to_config))

    def test_the_pipeline_calls_the_remover_not_the_old_blur(self):
        import inspect
        import agent_core
        import scrape_v2
        self.assertIn("caption_remover.remove_caption_regions",
                      inspect.getsource(scrape_v2._captions_are_removable))
        self.assertIn("_capfix.remove_caption_regions",
                      inspect.getsource(agent_core.apply_timeline_edits_to_config))


def _write_clip(path, frames, fps=30.0):
    height, width = frames[0].shape[:2]
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (width, height))
    for frame in frames:
        writer.write(frame)
    writer.release()
    return path


class FillGateTests(unittest.TestCase):
    """Two different checks used to disagree about whether ProPainter could run, and the loser
    was always the video: ``propainter_available()`` returned True while the remover printed
    "ProPainter is the required backend and is unavailable", did nothing, and left somebody
    else's Japanese caption burned into a finished Short."""

    def setUp(self):
        if cv2 is None:
            self.skipTest("OpenCV unavailable")
        self._env = os.environ.get("CAPTION_REMOVER_PROPAINTER")
        os.environ.pop("CAPTION_REMOVER_PROPAINTER", None)

    def tearDown(self):
        os.environ.pop("CAPTION_REMOVER_PROPAINTER", None)
        if self._env is not None:
            os.environ["CAPTION_REMOVER_PROPAINTER"] = self._env

    def _run(self, tmp, **kwargs):
        """Drive the real function up to the fill decision and report what it decided.

        ``_run_propainter`` is the only thing replaced - everything before it (the ffmpeg work
        copy, the detector, the coverage refusal, the gate) is the shipped code.
        """
        import pipeline
        ffmpeg = pipeline.find_ffmpeg()
        if not ffmpeg:
            self.skipTest("ffmpeg unavailable")
        clip = _write_clip(Path(tmp) / "clip.mp4", _panning_clip_with_caption(frames=40))
        calls = []
        messages = []

        def fake_run(piece, mask_dir, out_dir, **rest):
            calls.append((Path(piece), Path(mask_dir)))
            raise RuntimeError("stub: the fill itself is not under test")

        real = cr._run_propainter
        cr._run_propainter = fake_run
        try:
            cr.remove_caption_regions(clip, ffmpeg, status_cb=messages.append, **kwargs)
        finally:
            cr._run_propainter = real
        return calls, " ".join(messages)

    def test_a_caller_that_says_nothing_gets_its_clip_cleaned(self):
        """The measured defect: with the GPU up and the checkout in place, this call reached the
        fill zero times and reported the backend as unavailable."""
        if not cr.propainter_available():
            self.skipTest(f"ProPainter cannot run here: {cr.propainter_unavailable_reason()}")
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            calls, messages = self._run(tmp)
        self.assertTrue(calls, f"ProPainter was never reached; the remover said: {messages}")

    def test_an_explicit_refusal_names_the_caller_not_the_backend(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            calls, messages = self._run(tmp, allow_gpu=False)
        self.assertFalse(calls)
        self.assertIn("allow_gpu=False", messages)
        self.assertNotIn("is unavailable", messages)

    def test_the_kill_switch_names_itself(self):
        import tempfile
        os.environ["CAPTION_REMOVER_PROPAINTER"] = "0"
        with tempfile.TemporaryDirectory() as tmp:
            calls, messages = self._run(tmp)
        self.assertFalse(calls)
        self.assertIn("CAPTION_REMOVER_PROPAINTER=0", messages)

    def test_a_real_unavailability_names_the_missing_thing(self):
        """"Unavailable" is not a diagnosis. A missing checkout, missing weights, no torch and a
        dead driver are four different problems with four different fixes."""
        import tempfile
        real = cr.propainter_unavailable_reason
        cr.propainter_unavailable_reason = lambda: "the moon is in the wrong phase"
        try:
            with tempfile.TemporaryDirectory() as tmp:
                calls, messages = self._run(tmp)
        finally:
            cr.propainter_unavailable_reason = real
        self.assertFalse(calls)
        self.assertIn("the moon is in the wrong phase", messages)

    def test_the_reason_points_at_the_checkout_when_it_is_missing(self):
        import tempfile
        real = cr.PROPAINTER_DIR
        with tempfile.TemporaryDirectory() as tmp:
            cr.PROPAINTER_DIR = Path(tmp) / "nothing_here"
            try:
                reason = cr.propainter_unavailable_reason()
                self.assertFalse(cr.propainter_available())
            finally:
                cr.PROPAINTER_DIR = real
        self.assertIn("checkout", reason)
        self.assertIn("nothing_here", reason)

    def test_the_reason_points_at_the_weights_when_they_are_missing(self):
        import tempfile
        real = cr.PROPAINTER_DIR
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "ProPainter"
            root.mkdir()
            (root / "inference_propainter.py").write_text("", encoding="utf-8")
            cr.PROPAINTER_DIR = root
            try:
                reason = cr.propainter_unavailable_reason()
            finally:
                cr.PROPAINTER_DIR = real
        self.assertIn("weights", reason)
        self.assertIn("ProPainter.pth", reason)


class FillLengthTests(unittest.TestCase):
    """ProPainter iterates the VIDEO and indexes the mask LIST. One mask short and their script
    dies with IndexError inside inference_propainter.py, which the remover reported as
    "ProPainter unavailable" - so a 585-frame clip could only ever come back uncleaned."""

    def setUp(self):
        if cv2 is None:
            self.skipTest("OpenCV unavailable")

    def test_every_frame_of_the_clip_gets_a_mask(self):
        import tempfile
        frames = _panning_clip_with_caption(frames=30, width=96, height=64)
        mask = np.zeros(frames[0].shape[:2], np.uint8)
        mask[24:40, 10:80] = 255
        with tempfile.TemporaryDirectory() as tmp:
            clip = _write_clip(Path(tmp) / "long.mp4", frames)
            written = cr._write_frame_masks(clip, mask, Path(tmp) / "masks")
            self.assertEqual(written, cr._clip_frame_count(clip))
            self.assertEqual(written, len(sorted(Path(tmp).joinpath("masks").glob("*.png"))))

    def test_the_mask_count_is_not_capped_by_the_frame_reader(self):
        """The masks used to come from _read_frames, which stops at 400 frames."""
        import tempfile
        frames = _panning_clip_with_caption(frames=60, width=64, height=48)
        mask = np.zeros(frames[0].shape[:2], np.uint8)
        mask[18:30, 8:56] = 255
        with tempfile.TemporaryDirectory() as tmp:
            clip = _write_clip(Path(tmp) / "long.mp4", frames)
            real_limit = cr._read_frames(clip, limit=10)
            self.assertEqual(len(real_limit), 10, "the reader still caps, as it should")
            self.assertGreater(cr._write_frame_masks(clip, mask, Path(tmp) / "masks"), 10)

    def test_a_long_clip_is_filled_in_pieces_and_comes_back_whole(self):
        """Memory grows with clip LENGTH, so long clips are split. The join must give back every
        frame: overlay repeats a short fill's last frame, which would freeze a stale patch of
        background over the tail of the video."""
        import tempfile
        import pipeline
        ffmpeg = pipeline.find_ffmpeg()
        if not ffmpeg:
            self.skipTest("ffmpeg unavailable")
        frames = _panning_clip_with_caption(frames=45, width=96, height=64)
        mask = np.zeros(frames[0].shape[:2], np.uint8)
        mask[24:40, 10:80] = 255
        seen = []

        def fake_run(piece, mask_dir, out_dir, **rest):
            # A real fill would return a rebuilt clip; a copy of the piece is the same SHAPE,
            # which is what the piece/join arithmetic is being tested on.
            count = cr._clip_frame_count(piece)
            seen.append((count, len(sorted(Path(mask_dir).glob("*.png")))))
            out = Path(out_dir) / Path(piece).stem / "inpaint_out.mp4"
            out.parent.mkdir(parents=True, exist_ok=True)
            import shutil as _shutil
            _shutil.copyfile(str(piece), str(out))
            return out

        real_run, real_max = cr._run_propainter, cr.MAX_FILL_FRAMES
        cr._run_propainter, cr.MAX_FILL_FRAMES = fake_run, 16
        try:
            with tempfile.TemporaryDirectory() as tmp:
                clip = _write_clip(Path(tmp) / "long.mp4", frames)
                total = cr._clip_frame_count(clip)
                outcome = cr._fill_in_pieces(clip, mask, Path(tmp), ffmpeg)
                self.assertIsNotNone(outcome, "the fill gave up on a clip it has to handle")
                filled, filled_frames, reported_total = outcome
                self.assertEqual((filled_frames, reported_total), (total, total),
                                 "with no runs given, every frame is filled")
                joined = cr._clip_frame_count(filled)
        finally:
            cr._run_propainter, cr.MAX_FILL_FRAMES = real_run, real_max
        self.assertGreater(len(seen), 1, "a 45-frame clip at 16 per piece is not one piece")
        for piece_frames, piece_masks in seen:
            self.assertLessEqual(piece_frames, 16, "a piece was larger than the measured limit")
            self.assertEqual(piece_frames, piece_masks,
                             "one mask per frame, or their script dies with IndexError")
        self.assertEqual(sum(count for count, _masks in seen), total,
                         "the pieces have to add up to the clip")
        self.assertEqual(joined, total, "the joined fill is shorter than the video it covers")


class TemporalScopingTests(unittest.TestCase):
    """A caption is usually on screen for part of a clip. Filling the clean frames costs a minute
    each and can only damage them - ProPainter rebuilds whatever the mask covers, whether or not
    there was ever text there."""

    def setUp(self):
        if cv2 is None:
            self.skipTest("OpenCV unavailable")

    def _half_captioned(self, frames=40):
        """The caption stops halfway; the background keeps panning either way."""
        with_text = _panning_clip_with_caption(frames=frames)
        without = _panning_clip_with_caption(frames=frames, text="")
        return with_text[:frames // 2] + without[frames // 2:], with_text

    def test_it_finds_the_stretch_that_carries_the_caption(self):
        import tempfile
        mixed, captioned = self._half_captioned(frames=60)
        mask = cr.detect_caption_mask(captioned)
        self.assertIsNotNone(mask)
        with tempfile.TemporaryDirectory() as tmp:
            clip = _write_clip(Path(tmp) / "mixed.mp4", mixed)
            runs = cr._text_frame_runs(clip, mask)
        self.assertIsNotNone(runs, "the split is unambiguous on this clip")
        self.assertEqual(len(runs), 1, f"one caption, one run, got {runs}")
        start, end = runs[0]
        self.assertEqual(start, 0, "the caption is there from the first frame")
        # It ends around frame 30 - padded, so a few frames either side are expected.
        self.assertGreaterEqual(end, 30)
        self.assertLess(end, 45, f"the clean tail is still being filled: {runs}")

    def test_a_caption_that_never_stops_is_not_split_on_a_guess(self):
        """Skipping frames that DO carry text is the silent no-op this module exists to stop, so
        an unclear signal has to mean "fill everything", never "fill nothing"."""
        import tempfile
        captioned = _panning_clip_with_caption(frames=60)
        mask = cr.detect_caption_mask(captioned)
        with tempfile.TemporaryDirectory() as tmp:
            clip = _write_clip(Path(tmp) / "all.mp4", captioned)
            self.assertIsNone(cr._text_frame_runs(clip, mask))

    def test_the_plan_covers_every_frame_exactly_once(self):
        plan = cr._fill_plan(100, [(20, 60)], 25)
        self.assertEqual(plan[0][0], 0)
        cursor = 0
        for start, count, _fill in plan:
            self.assertEqual(start, cursor, f"a gap or an overlap in {plan}")
            cursor += count
        self.assertEqual(cursor, 100, "the plan has to cover the whole clip")
        # runs are half-open (start, end): frames 20-59, so 40 frames are filled and 60 are not.
        self.assertEqual(sum(count for _s, count, fill in plan if fill), 40)
        self.assertEqual(sum(count for _s, count, fill in plan if not fill), 60)
        for _s, count, fill in plan:
            if fill:
                self.assertLessEqual(count, 25, "a fill piece outgrew the measured limit")

    def test_a_clip_with_no_runs_is_filled_end_to_end(self):
        plan = cr._fill_plan(100, None, 40)
        self.assertTrue(all(fill for _s, _c, fill in plan))
        self.assertEqual(sum(count for _s, count, _f in plan), 100)

    def test_the_untouched_frames_come_out_of_the_source_not_the_fill(self):
        """The overlay has to be switched OFF outside the caption's frames. The mask is one still
        image applied to every frame, so without the time gate the fill's own patch is keyed in
        over frames that never had text on them."""
        import inspect
        body = inspect.getsource(cr.remove_caption_regions)
        self.assertIn("enable='", body)
        self.assertIn("between(t,", body)


class FillWidthTests(unittest.TestCase):
    """The fill reconstructs at the best width the card can take AT THAT MOMENT.

    It used to be pinned to 360 after a session measured 540px OOMing on 2026-09-03 and concluded
    that "no 540px run has ever completed a 9:16 clip". The owner's own archive disproves the
    conclusion: the trash-can Short of 2026-08-22 was cleaned at 540 and came out with no patches
    and no smears. What differed was not the card but what else was on it - a local LLM, ComfyUI
    and a pile of desktop apps held 5-8 GB of the 11.

    That mattered, because reconstructing at 360 and scaling the result back to 1080 is a
    threefold enlargement of an area that was invented in the first place, which is precisely
    what the owner kept pointing at ("boxes worse than the captions"). So the width is chosen
    from the free VRAM, and the OOM retry stays as the safety net it was written to be.

    Measured while writing this: 5.7 GB free -> 360, 9.5 GB free -> 540."""

    def setUp(self):
        if cv2 is None:
            self.skipTest("OpenCV unavailable")

    def test_the_fill_goes_straight_to_the_width_that_works(self):
        import tempfile
        import pipeline
        ffmpeg = pipeline.find_ffmpeg()
        if not ffmpeg:
            self.skipTest("ffmpeg unavailable")
        frames = _panning_clip_with_caption(frames=20, width=180, height=320)
        mask = np.zeros(frames[0].shape[:2], np.uint8)
        mask[150:200, 20:160] = 255
        widths = []

        def fake_run(piece, mask_dir, out_dir, **rest):
            capture = cv2.VideoCapture(str(piece))
            widths.append(int(capture.get(cv2.CAP_PROP_FRAME_WIDTH)))
            capture.release()
            out = Path(out_dir) / Path(piece).stem / "inpaint_out.mp4"
            out.parent.mkdir(parents=True, exist_ok=True)
            import shutil as _shutil
            _shutil.copyfile(str(piece), str(out))
            return out

        real = cr._run_propainter
        cr._run_propainter = fake_run
        try:
            with tempfile.TemporaryDirectory() as tmp:
                clip = _write_clip(Path(tmp) / "clip.mp4", frames)
                cr._fill_in_pieces(clip, mask, Path(tmp), ffmpeg)
        finally:
            cr._run_propainter = real
        self.assertTrue(widths, "the fill never ran")
        # ONE width per fill - never a doomed attempt followed by a fallback. Which of the two it
        # is depends on the card at that moment, so both are acceptable; what is not acceptable
        # is running twice.
        self.assertEqual(len(set(widths)), 1, f"the fill ran at several widths: {widths}")
        self.assertIn(widths[0], (cr.FILL_WIDTH, cr.WORK_WIDTH))

    def test_the_width_is_the_one_that_was_measured_to_work(self):
        """It climbed to 540 for a while, on the theory that 360 was what smeared. The theory did
        not survive its own measurement - scene 04 of the eating-walk Short, everything else
        identical: 360 clean in 74s, 450 clean in 88s, 540 out of memory on a card with 10 GB
        free. What separates a good fill from a bad one here is what ELSE holds the card."""
        self.assertEqual(cr.fill_width_now(), cr.FILL_WIDTH)
        source = inspect.getsource(cr.fill_width_now)
        self.assertIn("PROPAINTER_FILL_WIDTH", source)
        self.assertIn("360px   clean", source)

    def test_an_experiment_can_still_ask_for_another_width(self):
        os.environ["PROPAINTER_FILL_WIDTH"] = "450"
        try:
            self.assertEqual(cr.fill_width_now(), 450)
        finally:
            os.environ.pop("PROPAINTER_FILL_WIDTH", None)


class BoxedCaptionRejectionTests(unittest.TestCase):
    """A caption on an opaque plate is the one kind the remover cannot help with: it takes the
    LETTERS off - deliberately, because painting the whole rectangle is what used to smear away
    the subject - and the empty plate stays. Vision decides it. A morphological test for "uniform
    area behind the text" was calibrated on real clips and came out inverted: a table seam scored
    best at 9.2 and the one genuine plate worst at 39.3."""

    def test_v2_rejects_a_caption_on_a_plate_but_keeps_a_plain_one(self):
        import scrape_v2
        def seg_with(desc):
            seg = scrape_v2.SegmentCandidate(
                segment_id="s", source_id="s", platform="tiktok", source_path="",
                start_time=0.0, end_time=3.0, duration=3.0, query="q")
            seg.visual_description = dict(desc, usable=True)
            return seg
        self.assertEqual(scrape_v2.editorial_rejection_reason(seg_with({"caption_on_solid_box": True})),
                         "caption sits on a solid background box")
        self.assertEqual(scrape_v2.editorial_rejection_reason(seg_with({"burned_captions": True})), "",
                         "an ordinary burned-in caption is still fine - it gets removed")

    def test_v3_rejects_it_with_a_named_reason(self):
        import scrape_v3
        chapter = scrape_v3.Chapter(chapter_id=0, title="C")
        self.assertEqual(scrape_v3.rejection_reason({"caption_on_solid_box": True}, chapter),
                         "caption_on_solid_box")
        self.assertIn("caption_on_solid_box", scrape_v3.V3_REJECTIONS)
        self.assertEqual(scrape_v3.rejection_reason({"burned_captions": True}, chapter), "")

    def test_both_engines_ask_the_model_for_it(self):
        import inspect
        import scrape_v2
        import scrape_v3
        self.assertIn("caption_on_solid_box", inspect.getsource(scrape_v2.describe_segments_v2))
        self.assertIn("caption_on_solid_box", scrape_v3.GRADE_PROMPT)

    def test_the_clip_menu_offers_a_per_clip_toggle(self):
        """The pass costs about a minute of GPU per clip, so the decision belongs to whoever is
        looking at the picture."""
        import app
        source = Path(app.__file__).read_text(encoding="utf-8")
        start = source.index("function openClipMenu(")
        menu = source[start:source.index("function openOverlayMenu(", start)]
        self.assertIn("Remove burned-in captions", menu)
        self.assertIn("scn.blur_captions", menu, "it must write the same field the inspector uses")
        for opener, closer in (("{", "}"), ("[", "]"), ("(", ")")):
            self.assertEqual(menu.count(opener), menu.count(closer), f"unbalanced {opener}")


class OrphanLifelineTests(unittest.TestCase):
    """Cancellation only covers the case where the app is alive to send it. When the app itself
    goes away - a crash, the task manager, a plain shutdown - nobody sends anything, and the fill
    kept a whole GPU busy for output nowhere collected. One was measured grinding for 25 minutes
    after its parent had gone, holding 10.6 of 11.3 GB."""

    def test_the_child_watches_a_pipe_rather_than_polling_for_its_parent(self):
        import inspect
        src = inspect.getsource(cr._run_propainter)
        self.assertIn("stdin=subprocess.PIPE", src,
                      "stdin is the lifeline; without it the child cannot notice")
        self.assertIn("sys.stdin.buffer.read(1)", src)
        self.assertIn("os._exit(3)", src)

    def test_the_child_exits_the_moment_the_lifeline_closes(self):
        """Asserting the source is not the same as watching it happen. Closing the write end is
        exactly what the operating system does when the parent dies, so this exercises the real
        mechanism without needing a parent to kill."""
        import subprocess
        import sys
        import time
        boot = ("import os, sys, threading, time;"
                "t = threading.Thread("
                "  target=lambda: (sys.stdin.buffer.read(1), os._exit(3)), daemon=True);"
                "t.start();"
                "time.sleep(120)")
        child = subprocess.Popen([sys.executable, "-c", boot], stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            time.sleep(1.0)
            self.assertIsNone(child.poll(), "the child died before the lifeline was cut")
            child.stdin.close()                      # what a dying parent does to the pipe
            try:
                code = child.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.fail("the child outlived its lifeline - an orphan would keep the GPU")
            self.assertEqual(code, 3, "it should exit through the watchdog, not by accident")
        finally:
            if child.poll() is None:
                child.kill()


class RetroToolTests(unittest.TestCase):
    """Old projects can be redone because nothing was lost: the blurred copy lives beside the
    original and the scene remembers the untouched source in caption_blur_src."""

    def _tool(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "recaption_project", Path("tools/recaption_project.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_it_rebuilds_from_the_original_never_from_the_processed_copy(self):
        tool = self._tool()
        config = {"scenes": [
            {"id": "01", "clip": "capblur_01_ab.mp4", "caption_blur_src": "scraped_00.mp4"},
            {"id": "02", "clip": "scraped_01.mp4"},
            {"id": "03", "clip": ""},
        ]}
        marked = tool.scenes_to_process(config, every=False)
        self.assertEqual([source for _scene, source in marked], ["scraped_00.mp4"],
                         "running a remover over its own output smears twice")
        everything = tool.scenes_to_process(config, every=True)
        self.assertEqual(sorted(source for _scene, source in everything),
                         ["scraped_00.mp4", "scraped_01.mp4"])

    def test_a_derived_file_is_never_treated_as_an_original(self):
        tool = self._tool()
        config = {"scenes": [{"id": "01", "clip": "speed_v2_01_x.mp4"},
                             {"id": "02", "clip": "capblur_02_y.mp4"}]}
        self.assertEqual(tool.scenes_to_process(config, every=True), [])


if __name__ == "__main__":
    unittest.main()
