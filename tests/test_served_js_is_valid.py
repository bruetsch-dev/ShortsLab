"""The JavaScript the app serves must actually parse.

This cost an evening. A backslash-n written into the timeline editor's inline script - which
lives inside an ordinary Python string - reached the browser as a REAL newline and split a JS
string literal across two lines. That is a syntax error, and one syntax error kills the WHOLE
script: the media library spun on "LOADING MEDIA" for ever, the timeline never drew, and every
symptom pointed at the server, which was answering the same request in 0.1s throughout.

Nothing in the Python suite noticed, because the Python was perfectly valid. So this file checks
the thing that actually broke: the text the browser is handed.
"""

import re
import unittest
from pathlib import Path

import app

NEWLINE = chr(10)
BACKSLASH = chr(92)

# Where a `/` starts a regular expression rather than a division. Regex literals are why a naive
# quote count does not work: `.replace(/"/g, "&quot;")` holds one quote inside a regex.
_REGEX_MAY_START_AFTER = set("(,=:[!&|?{};+-*%~^<>")


def unterminated_string_lines(code):
    """Line numbers where a ' or " string literal is still open when the line ends.

    A small scanner rather than a regular expression, because the false positives matter: a
    check that cries wolf on every `.replace(/"/g, ...)` would be turned off within a week.
    """
    bad = []
    index, size = 0, len(code)
    line, quote = 1, ""
    in_line_comment = in_block_comment = in_regex = in_template = False
    previous = ""
    while index < size:
        char = code[index]
        if char == NEWLINE:
            if quote:
                bad.append((line, code.split(NEWLINE)[line - 1].strip()[:120]))
                quote = ""                 # report once, then resynchronise
            in_line_comment = in_regex = False
            line += 1
            index += 1
            continue
        if in_line_comment:
            index += 1
            continue
        if in_block_comment:
            if code.startswith("*/", index):
                in_block_comment = False
                index += 2
                continue
            index += 1
            continue
        if quote or in_template:
            if char == BACKSLASH:
                index += 2
                continue
            if (in_template and char == "`") or (quote and char == quote):
                quote, in_template = "", False
            index += 1
            continue
        if in_regex:
            if char == BACKSLASH:
                index += 2
                continue
            if char == "/":
                in_regex = False
            index += 1
            continue
        if code.startswith("//", index):
            in_line_comment = True
            index += 2
            continue
        if code.startswith("/*", index):
            in_block_comment = True
            index += 2
            continue
        if char == "/" and (previous == "" or previous in _REGEX_MAY_START_AFTER):
            in_regex = True
            index += 1
            continue
        if char in "'\"":
            quote = char
            index += 1
            continue
        if char == "`":
            in_template = True
            index += 1
            continue
        if not char.isspace():
            previous = char
        index += 1
    return bad


def inline_scripts(html):
    return re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S)


def broken_literals(html):
    problems = []
    for number, code in enumerate(inline_scripts(html)):
        for line, text in unterminated_string_lines(code):
            problems.append(f"script {number}, line {line}: {text}")
    return problems


class TimelineEditorScriptTests(unittest.TestCase):
    def setUp(self):
        import agent_core
        slug = next((d.name for d in sorted(agent_core.PROJECTS_DIR.iterdir())
                     if d.is_dir() and (d / "config" / "project.json").is_file()), None)
        if not slug:
            self.skipTest("no project on this machine to render the editor for")
        self.html = app.timeline_page(slug).decode("utf-8")

    def test_no_string_literal_is_split_across_lines(self):
        """The exact failure: a backslash-n in the Python source became a real newline."""
        problems = broken_literals(self.html)
        self.assertEqual(problems, [],
                         "unterminated JS string literal(s):" + NEWLINE + NEWLINE.join(problems))

    def test_the_confirm_dialog_builds_its_newline_in_js(self):
        """Belt and braces on the line that broke: it must not carry a raw escape."""
        source = open(app.__file__, encoding="utf-8").read()
        block = source[source.index("function startRescrape(btn){"):]
        block = block[:block.index("document.getElementById('tl-insp-blurcap')")]
        self.assertIn("String.fromCharCode(10,10)", block)


class ChatShellScriptTests(unittest.TestCase):
    def test_no_string_literal_is_split_across_lines(self):
        import chat_ui
        problems = broken_literals(chat_ui.chat_shell_page({}).decode("utf-8"))
        self.assertEqual(problems, [],
                         "unterminated JS string literal(s):" + NEWLINE + NEWLINE.join(problems))

    def test_longform_timeline_defines_duration_before_its_zoom_default(self):
        """The pre-render editor must not hit JavaScript's temporal-dead-zone.

        A regression placed the zoom select ahead of ``const duration``.  The browser then
        stopped at the first frame, which looked like a frozen preview and also hid the render
        action because the rest of the editor function never ran.
        """
        source = Path("static/chat-shell.js").read_text(encoding="utf-8")
        editor = source[source.index("async function openLongformPreRenderEditor"):
                        source.index("/* ---------------------------------------------------- legacy longform frame editor fallback")]
        self.assertLess(
            editor.index("const duration=Math.max(Number(data.audio_duration||0)"),
            editor.index("zoom.value=String(zoomChoice||(Number(duration)<=240?\"fit\":\"4\"))"),
        )

    def test_longform_timeline_uses_fullscreen_workspace_and_preview_transport(self):
        """The longform editor must have Clip-Short-like space and in-player controls."""
        source = Path("static/chat-shell.js").read_text(encoding="utf-8")
        editor = source[source.index("async function openLongformPreRenderEditor"):
                        source.index("/* ---------------------------------------------------- legacy longform frame editor fallback")]
        self.assertIn('"lf-frame-editor lf-prerender lf-fullscreen-editor"', editor)
        self.assertIn("document.body.appendChild(root)", editor)
        self.assertIn("preview.appendChild(audio)", editor)
        self.assertNotIn("inspector.appendChild(audio)", editor)


class DetectorTests(unittest.TestCase):
    """The check has to catch the real bug and stay quiet on ordinary code."""

    def test_it_catches_a_split_literal(self):
        self.assertTrue(unterminated_string_lines("var x = 'hello" + NEWLINE + "world';"))

    def test_it_accepts_an_escaped_newline(self):
        self.assertEqual(unterminated_string_lines(r"var x = 'hello\nworld';"), [])

    def test_it_accepts_an_apostrophe_in_a_comment(self):
        self.assertEqual(unterminated_string_lines("var a = 1;   // don't panic"), [])

    def test_it_accepts_a_quote_inside_the_other_quote(self):
        self.assertEqual(unterminated_string_lines("""var s = "it's fine";"""), [])

    def test_it_accepts_concatenation_across_lines(self):
        self.assertEqual(unterminated_string_lines("var s = 'a'" + NEWLINE + "  + 'b';"), [])

    def test_it_accepts_a_quote_inside_a_regex(self):
        """The false positive that would have got this check switched off."""
        self.assertEqual(
            unterminated_string_lines('var safe = t.replace(/"/g, "&quot;");'), [])

    def test_it_accepts_a_template_literal_spanning_lines(self):
        self.assertEqual(unterminated_string_lines("var s = `a" + NEWLINE + "b`;"), [])


if __name__ == "__main__":
    unittest.main()
