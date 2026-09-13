"""Toggling the Google search intro must actually change the voiceover.

Reported 2026-08-29: "warum wird die hook bei sketch short garnicht mehr generiert wenn man
google search an hat?" - and the run really did stop producing one.

The Google intro moves the hook out of the narration, which removes the hook's dedicated TTS
take (`sketch_hook_take`). But the resume shortcut reused an already-stitched voiceover whenever
the script, narrator, model, settings and style matched - and none of those change when the
intro is switched on. So the run kept the audio recorded under the OTHER setting and never
generated the hook take. Measured on `projects/_longform/i_m_just_sitting_there_doing`: the
split for that script yields 2 parts, and the resumed state held 1.
"""

import re
import unittest

import longform_video as lf


class ResumeKeyTests(unittest.TestCase):
    def body(self):
        source = open(lf.__file__, encoding="utf-8").read()
        body = source[source.index("    sketch_hook_take = short_form and not hook_in_intro"):]
        return body[:body.index("\ndef ", 10)]

    def test_the_split_mode_is_part_of_the_resume_key(self):
        guard = self.body()
        shortcut = guard[guard.index('saved.get("voiceover_ready")'):]
        shortcut = shortcut[:shortcut.index("_log(")]
        self.assertIn("sketch_hook_take", shortcut,
                      "a voiceover from the other split is reused as if it were this one")

    def test_it_is_recorded_when_the_voiceover_is_written(self):
        """A key that is never saved can never be compared."""
        self.assertIn("sketch_hook_take=bool(sketch_hook_take)", self.body())

    def test_older_states_still_resume(self):
        """Every project on disk predates the field; refusing them all would re-buy the TTS."""
        guard = self.body()
        self.assertIn('saved.get("sketch_hook_take") is None', guard)

    def test_the_part_count_covers_states_written_before_the_field(self):
        """The measured project has no such key, so something else has to catch it - the two
        splits of one script do not produce the same number of parts."""
        guard = self.body()
        shortcut = guard[guard.index('saved.get("voiceover_ready")'):]
        shortcut = shortcut[:shortcut.index("_log(")]
        self.assertIn("tts_part_total", shortcut)
        self.assertIn("len(parts)", shortcut)


class SplitTests(unittest.TestCase):
    SCRIPT = ("I'm just sitting there, doing nothing, and one eyelid starts moving by itself.\n\n"
              "Most of the time, it's not my eye actually malfunctioning.\n\n"
              "It's a tiny muscle spasm called eyelid myokymia.")

    def test_the_hook_gets_its_own_take_without_the_google_intro(self):
        self.assertEqual(len(lf.split_sketch_short_for_tts(self.SCRIPT)), 2)

    def test_a_marked_hook_that_is_not_in_the_script_leaves_the_narration_alone(self):
        """The typed hook is a NEW sentence; there is nothing to remove from the narration."""
        line = lf.hook_line_of(self.SCRIPT, "Why does my eye suddenly start twitching?")
        self.assertEqual(line, "Why does my eye suddenly start twitching?")
        self.assertEqual(lf.drop_hook_from_narration(self.SCRIPT, line), self.SCRIPT)


if __name__ == "__main__":
    unittest.main()
