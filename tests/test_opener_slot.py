"""The generated opener must be visible, insertable, and actually used.

Reported 2026-08-31, looking at the pre-render review screen: "wo ist der platzhalter fuer den
hook clip, wo ist der prompt fuer den hook clip?" - the run wrote a prompt but nothing surfaced
it, and there was nowhere to put the finished clip back.

An upload that the renderer ignores is decoration, so the render path is asserted too.
"""

import unittest
from pathlib import Path

import app
import longform_video as lf
import sketch_hook_intro as shi

JS = Path("static/chat-shell.js").read_text(encoding="utf-8")
CSS = Path("static/chat-shell.css").read_text(encoding="utf-8")


class PayloadTests(unittest.TestCase):
    def test_the_payload_actually_builds(self):
        """Asserting on SOURCE TEXT let a real crash through: the first version read
        `longform_video.TARGET_SECONDS`, which lives in sketch_hook_intro, so every call to this
        endpoint raised AttributeError while the string-matching test stayed green."""
        import glob
        import os
        slugs = [os.path.basename(os.path.dirname(x))
                 for x in glob.glob("projects/_longform/*/state.json")]
        if not slugs:
            self.skipTest("no longform project on disk")
        built = [app.longform_frames_payload(s) for s in slugs[:6]]
        self.assertTrue(any(p.get("ok") for p in built),
                        "not one project produced a usable payload")
        for payload in built:
            if not payload.get("ok"):
                continue
            self.assertIsInstance(payload.get("opener_expected_seconds"), float)
            self.assertIsInstance(payload.get("opener_prompt"), str)
            self.assertIsInstance(payload.get("opener_clip"), str)

    def test_the_review_payload_carries_the_prompt_and_the_clip(self):
        source = open(app.__file__, encoding="utf-8").read()
        body = source[source.index("def longform_frames_payload("):]
        body = body[:body.index("\ndef ", 10)]
        for key in ('"opener_prompt"', '"opener_clip"', '"opener_query"', '"opener_enabled"',
                    '"opener_expected_seconds"'):
            self.assertIn(key, body, key)

    def test_the_unused_scan_uses_the_project_aspect(self):
        """A 9:16 short's spare art is vertical; a hardcoded 16:9 check hid all of it."""
        source = open(app.__file__, encoding="utf-8").read()
        body = source[source.index("def longform_frames_payload("):]
        body = body[:body.index("\ndef ", 10)]
        self.assertNotIn('_image_done(path, "16:9")', body)
        self.assertIn('(state or {}).get("aspect")', body)


class UploadTests(unittest.TestCase):
    def test_there_is_an_endpoint(self):
        source = open(app.__file__, encoding="utf-8").read()
        self.assertIn('parsed.path == "/longform-opener-upload"', source)
        self.assertTrue(hasattr(app, "longform_opener_replace"))

    def test_it_rejects_a_non_video(self):
        self.assertFalse(app.longform_opener_replace("nope", None).get("ok"))

    def test_it_reads_the_shape_parse_multipart_actually_hands_over(self):
        """parse_multipart returns a DICT - {"filename", "data"} - not an object. Reading it
        with getattr() gave None for every real upload, so the endpoint answered "No file was
        uploaded" no matter what was dropped on it, and the old test passed because it only ever
        passed None."""
        import glob
        import os
        states = sorted(glob.glob("projects/_longform/*/state.json"))
        if not states:
            self.skipTest("no longform project on disk")
        slug = os.path.basename(os.path.dirname(states[0]))
        clip = Path("assets/hook_intros/google_search.mp4")
        if not clip.is_file():
            self.skipTest("no sample clip")
        result = app.longform_opener_replace(
            slug, {"filename": "opener.mp4", "data": clip.read_bytes()})
        try:
            self.assertTrue(result.get("ok"), result.get("error"))
            self.assertGreater(float(result.get("seconds") or 0), 0.0)
        finally:
            app.longform_opener_replace(slug, None, remove=True)

    def test_an_empty_payload_is_still_refused(self):
        self.assertFalse(
            app.longform_opener_replace("x", {"filename": "a.mp4", "data": b""}).get("ok"))

    def test_removing_it_is_possible(self):
        import inspect
        self.assertIn("remove", inspect.signature(app.longform_opener_replace).parameters)


class UiTests(unittest.TestCase):
    def test_the_panel_exists_and_shows_the_prompt(self):
        self.assertIn("function paintOpener", JS)
        self.assertIn("paintOpener();", JS)
        self.assertIn("lf-opener-prompt", JS)
        self.assertIn("Copy prompt", JS)

    def test_the_timeline_shows_a_placeholder(self):
        self.assertIn("lf-nle-opener", JS)
        self.assertIn("OPENER", JS)

    def test_the_placeholder_is_styled(self):
        for cls in (".lf-opener-slot", ".lf-opener-ph", ".lf-nle-clip.lf-nle-opener"):
            self.assertIn(cls, CSS, cls)


class DropTests(unittest.TestCase):
    """The panel says "drop the file in", so the placeholder has to accept one.

    Reported 2026-08-31: "der placeholder unterstuetzt kein drag and drop in" - the first
    version only had a file-picker button.
    """

    def slot_code(self):
        block = JS[JS.index("function paintOpener()"):]
        return block[:block.index("function paintThumbs()")]

    def test_the_slot_takes_a_dropped_file(self):
        code = self.slot_code()
        for handler in ('slot.addEventListener("dragenter"', 'slot.addEventListener("dragover"',
                        'slot.addEventListener("drop"'):
            self.assertIn(handler, code, handler)

    def test_dragover_is_cancelled(self):
        """Without preventDefault the browser navigates away to the dropped file."""
        code = self.slot_code()
        over = code[code.index('slot.addEventListener("dragover"'):]
        self.assertIn("ev.preventDefault()", over[:200])

    def test_the_drop_and_the_picker_share_one_upload_path(self):
        """Two copies of the upload drift apart; one of them ends up not refreshing."""
        code = self.slot_code()
        self.assertIn("async function sendOpener(", code)
        # Count the paths that SEND A FILE - the same endpoint also serves Remove, which posts
        # no file and is a different action.
        self.assertEqual(code.count('fd.append("file"'), 1)
        self.assertIn("await sendOpener(files[0])", code)
        self.assertIn("await sendOpener(pick.files[0])", code)

    def test_a_non_video_drop_is_refused_before_uploading(self):
        code = self.slot_code()
        self.assertIn("mp4|mov|webm|mkv", code)

    def test_the_drop_state_is_visible(self):
        self.assertIn(".lf-opener-slot.drop-target", CSS)
        self.assertIn('slot.classList.add("drop-target")', self.slot_code())


class RenderTests(unittest.TestCase):
    def test_a_supplied_clip_replaces_the_stock_one(self):
        """Otherwise the upload changes nothing and the same stock seconds ship again."""
        source = open(lf.__file__, encoding="utf-8").read()
        block = source[source.index('if bool(state.get("hook_intro")) and not str(rendered).endswith'):]
        block = block[:block.index("save_state(")]
        self.assertIn('project_dir / "opener_clip.mp4"', block)
        self.assertIn("voice_over_opener(", block)
        self.assertIn("else:", block)
        self.assertIn("HOOK_INTRO_CLIP", block)

    def test_the_voice_over_helper_exists_and_adds_no_text(self):
        """The generated clip already shows the query - drawing it again would double it."""
        self.assertTrue(hasattr(shi, "voice_over_opener"))
        import inspect
        body = inspect.getsource(shi.voice_over_opener)
        self.assertIn("_speak_hook", body)
        self.assertNotIn("draw.text", body)

    def test_it_is_normalised_to_the_same_loudness_as_the_short(self):
        import inspect
        self.assertIn("HOOK_TARGET_LUFS", inspect.getsource(shi.voice_over_opener))


class ThumbnailAspectTests(unittest.TestCase):
    def test_the_thumbnail_follows_the_project_aspect(self):
        """A 9:16 short shipped 1672x941 thumbnails because the prompt said 16:9."""
        lf.set_project_aspect("9:16")
        try:
            self.assertIn("9:16 aspect ratio", lf._thumb_style_tail())
        finally:
            lf.set_project_aspect("16:9")
        self.assertIn("16:9 aspect ratio", lf._thumb_style_tail())

    def test_no_call_site_still_hardcodes_it(self):
        source = open(lf.__file__, encoding="utf-8").read()
        self.assertNotIn("{_THUMB_STYLE_TAIL}", source)


class TimelineScaleTests(unittest.TestCase):
    def test_a_short_video_opens_fitted(self):
        """4 px/s put 34.7s of clips into 139px inside a 900px canvas: 15% content."""
        self.assertIn('zoomChoice = duration<=240 ? "fit" : "4"', JS)

    def test_the_canvas_is_not_padded_past_the_viewport(self):
        self.assertNotIn("Math.max(900,Math.ceil(duration*timelineScale))", JS)


if __name__ == "__main__":
    unittest.main()
