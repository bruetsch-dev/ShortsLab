"""Joining the hook intro must never change the short's format.

Measured on a delivered sketch short (2026-08-29, `you_re_just_sitting_there_doing`):
  every source image  720x1280   (9:16, correct)
  hook_intro.mp4      720x1280   (9:16, correct)
  the bare render    1080x1920   (9:16, correct)
  the _with_hook.mp4 1920x1080   <- LANDSCAPE

`assemble_video` already takes its aspect explicitly, with a docstring explaining that reading
the module global is unsafe because any other job in the process may have changed it. The join
still read that global, so the last step scaled the finished portrait short up and CROPPED it
into landscape - the "EYE" caption was cut off the frame entirely.

The canvas therefore comes from the short being joined, not from module state.
"""

import unittest

import longform_video as lf


class ProbeTests(unittest.TestCase):
    def test_a_missing_file_is_not_an_error(self):
        """The join must still work if probing fails; it falls back to the global."""
        self.assertIsNone(lf._probe_video_size("no_such_file_anywhere.mp4"))

    def test_a_directory_is_not_a_video(self):
        self.assertIsNone(lf._probe_video_size("."))


class JoinTests(unittest.TestCase):
    def body(self):
        source = open(lf.__file__, encoding="utf-8").read()
        body = source[source.index("def prepend_hook_intro("):]
        return body[:body.index("\ndef ", 10)]

    def test_the_canvas_comes_from_the_short(self):
        self.assertIn("_probe_video_size(short_path)", self.body())

    def test_the_module_global_is_only_a_fallback(self):
        """video_size() may still be used, but never as the first choice."""
        body = self.body()
        line = next(l for l in body.splitlines() if "_probe_video_size(short_path)" in l)
        self.assertIn("or video_size()", line)

    def test_the_intro_is_still_conformed_to_that_canvas(self):
        """Both inputs must be made identical or concat drops frames silently."""
        body = self.body()
        self.assertEqual(body.count("force_original_aspect_ratio=increase"), 2)


if __name__ == "__main__":
    unittest.main()
