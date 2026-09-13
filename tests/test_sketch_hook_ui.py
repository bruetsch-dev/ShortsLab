"""The Google-hook intro is markable in the Sketch Short creator - and only there.

The engine accepted hook_intro/hook_text for a day before any UI sent them: the field existed in
run_longform_video, was saved to state, was built on rebuild - and no screen had a way to turn it
on. The section lives in the 9:16 settings step; a 16:9 explainer never sees it, matching the
server, which ignores the fields on any other canvas. Verified live in the browser: marking the
second sentence submitted hook_intro=on and hook_text with it, and flipping to 16:9 removed the
section and the fields from the submit.
"""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SHELL = (ROOT / "static" / "chat-shell.js").read_text(encoding="utf-8")
APP = (ROOT / "app.py").read_text(encoding="utf-8")


class ShellTests(unittest.TestCase):
    def test_the_section_exists_only_on_the_short_canvas(self):
        block = SHELL[SHELL.index('"Google hook intro"') - 900:]
        self.assertIn('(S.longform.aspect || "16:9") === "9:16"', block[:900])

    def test_the_hook_is_picked_from_the_script_s_own_sentences(self):
        """At the settings step the script sits behind the previous card - there is no text to
        select from, and the hook must be a sentence the narrator speaks anyway."""
        self.assertIn("split(/(?<=[.!?])\s+/)", SHELL)

    def test_marking_toggles_off_again(self):
        block = SHELL[SHELL.index('"Google hook intro"') - 2000:]
        self.assertIn('S.longform.hook_text = (S.longform.hook_text === t ? "" : t)', block)

    def test_a_stale_mark_is_dropped_when_the_script_changed(self):
        self.assertIn("if (S.longform.hook_text && !sentences.includes(S.longform.hook_text))",
                      SHELL)

    def test_submit_sends_the_fields_only_for_a_short(self):
        block = SHELL[SHELL.index("async function submitLongform()"):]
        self.assertIn('if (S.longform.aspect === "9:16" && S.longform.hook_intro === true)',
                      block[:1600])
        self.assertIn('fd.append("hook_intro", "on")', block[:1600])


class ServerTests(unittest.TestCase):
    def test_the_fields_are_read_and_gated_to_the_short_canvas(self):
        self.assertIn('hook_intro = aspect == "9:16" and agent_core.form_flag(fields, '
                      '"hook_intro", False)', APP)

    def test_they_reach_the_pipeline(self):
        # Not anchored on the closing bracket: this is about the two hook arguments being
        # handed to the run, and adding an unrelated argument after them is not a regression.
        self.assertIn("hook_intro=hook_intro, hook_text=hook_text", APP)

    def test_an_unmarked_hook_falls_back_to_the_first_sentence(self):
        """hook_line_of does that in the engine; the UI hint promises it."""
        lv = (ROOT / "longform_video.py").read_text(encoding="utf-8")
        self.assertIn("def hook_line_of", lv)


if __name__ == "__main__":
    unittest.main()
