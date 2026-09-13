"""A fill belongs to the seconds it was computed from, and to no other seconds.

Two defects met in one clip and produced everything the owner was calling "Geschmiere".

  1. The pass took `seconds` from the HEAD of the file and ignored the offset, while the edit
     shows a window that starts at seedance_start_trim. Scene 01 of the eating-walk Short is
     trimmed to 36.411s; the mask was built on second 0, ProPainter rebuilt second 0, and the
     composite - which keys the filled copy in by timestamp - printed second 0's background.
  2. The overlay was gated to the caption's frame runs only when those runs were known. When
     they were not, `enable` stayed empty and the overlay was ON FOR THE WHOLE FILE: a 2.4s
     fill keyed into all 45 seconds of the source.

Measured on capblur_01_ff0ba0fed2.mp4, the derivative that shipped: clean at t=0.8 (where the
fill was computed), and the same rebuilt block printed over t=18 and t=37 where no caption ever
was. Neither is a quality problem, and no width, VRAM setting or subvideo length changes either.
"""

import inspect
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import caption_remover as cr

SRC = inspect.getsource(cr.remove_caption_regions)


class TheFillReadsTheWindowTheEditShows(unittest.TestCase):
    def test_the_offset_reaches_the_work_clip(self):
        """`-t` alone cuts the right LENGTH out of the wrong PLACE."""
        self.assertIn('window_at = max(0.0, float(start or 0.0))', SRC)
        self.assertIn('source_args += ["-ss"', SRC)

    def test_the_seek_comes_before_the_input(self):
        """After -i it is a slow decode-and-discard, and on some builds it is ignored."""
        seek = SRC.index('source_args += ["-ss"')
        put_input = SRC.index('source_args += ["-i", str(path)]')
        self.assertLess(seek, put_input)

    def test_the_composite_is_handed_the_fill_at_the_right_time(self):
        """The base layer is the whole file, so the filled copy has to be pushed to its window."""
        self.assertIn('"-itsoffset"', SRC)
        offset = SRC.index('"-itsoffset"')
        fill_input = SRC.index('"-i", str(filled)')
        self.assertLess(offset, fill_input)

    def test_a_window_at_zero_adds_no_flags(self):
        """The common case - a clip used from its head - must stay byte-identical to before."""
        self.assertIn('if window_at > 0.001:', SRC)
        self.assertIn('if window_at > 0.001 else []', SRC)


class TheFillIsKeyedInOnlyThere(unittest.TestCase):
    def test_the_window_gate_does_not_depend_on_the_runs(self):
        """This is the one that painted a patch over 45 seconds of picture."""
        self.assertIn("elif window_end is not None:", SRC)
        self.assertIn("window_end = window_at + max(0.10, float(seconds or 0.0))", SRC)

    def test_the_run_gate_carries_the_offset_too(self):
        """`runs` are frame indices inside the window; `enable` is in the file's clock."""
        self.assertIn("window_at + (r0 - 0.5) / fps", SRC)
        self.assertIn("window_at + (r1 + 0.5) / fps", SRC)

    def test_the_loop_no_longer_shadows_the_offset(self):
        """`for start, end in runs` rebound the parameter this whole fix depends on."""
        self.assertNotIn("for start, end in runs", SRC)
        self.assertIn("for r0, r1 in runs", SRC)

    def test_the_base_shows_through_outside_the_window(self):
        """With the fill offset, overlay has no second input before the window starts."""
        self.assertIn("eof_action=pass:repeatlast=0", SRC)


class TheOldDerivativesAreRetired(unittest.TestCase):
    def test_the_clean_version_moved(self):
        """Every capblur_ built by the broken pass may carry a patch over the whole file. If the
        version does not move, a re-render reuses them and nothing the owner sees changes."""
        self.assertGreaterEqual(int(cr.CLEAN_VERSION), 4)

    def test_the_window_cuts_carry_the_version_as_well(self):
        """prepare_timeline_clips cuts its window out of whatever the scene points at, which can
        be one of those derivatives, and cached the cut under a name with no version in it."""
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "agent_core.py"), encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn('f"win{caption_remover.CLEAN_VERSION}_{path.stem[:26]}_"', src)
        self.assertNotIn('cut = path.with_name(f"win_{path.stem[:26]}', src)

    def test_the_version_does_not_break_the_post_id_lookup(self):
        """Derived copies are matched by the post id inside the name; a version glued to a digit
        run would change that id."""
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        import shell_v3
        original = "scraped_7594052716503092501.mp4"
        cut = "win%s_scraped_7594052716503092501_0036411.mp4" % cr.CLEAN_VERSION
        self.assertTrue(shell_v3._same_source(original, cut))


class TheCallersAgree(unittest.TestCase):
    def test_the_config_path_still_hands_over_the_trim(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "agent_core.py"), encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn("seconds=_used_len, start=_trim,", src)

    def test_the_timeline_path_cuts_first_so_its_window_starts_at_zero(self):
        """It is allowed to omit `start` only because the file it hands over IS the window."""
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "agent_core.py"), encoding="utf-8") as fh:
            src = fh.read()
        block = src[src.index("CUT THE WINDOW OUT FIRST"):][:2600]
        self.assertIn('"-ss", f"{max(0.0, trim - lead):.3f}"', block)
        self.assertIn("remove_caption_regions(", block)
        self.assertIn("str(target), ffmpeg, seconds=span + 0.4", block)


class EveryDecisionAsksAtTheInPoint(unittest.TestCase):
    """The fill was not the only thing reading the head of the file.

    Whether a scene gets cleaned at all is decided by caption_coverage. Measuring that at second
    0 while the edit cuts in at second 36 gets it wrong in both directions: a clip clean at its
    head and captioned where the scene starts is never cleaned, and one captioned only at its
    head is cleaned for nothing. Three of the four call sites already passed the offset; the
    config pre-pass did not.
    """

    def _sources(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for name in ("agent_core.py", "scrape_v3.py", "scrape_v4.py"):
            with open(os.path.join(here, name), encoding="utf-8") as fh:
                yield name, fh.read()

    def test_no_coverage_call_in_the_pipeline_omits_the_offset(self):
        bare = []
        for name, src in self._sources():
            for m in re.finditer(r"caption_coverage\(", src):
                line_start = src.rfind(chr(10), 0, m.start()) + 1
                line = src[line_start:src.find(chr(10), m.start())]
                if line.lstrip().startswith("#") or "caption_coverage()" in line:
                    continue                       # prose about the function, not a call to it
                # the argument list can wrap, so look at the whole call up to its closing paren
                depth, end = 0, m.start()
                for i in range(m.start(), min(len(src), m.start() + 600)):
                    if src[i] == "(":
                        depth += 1
                    elif src[i] == ")":
                        depth -= 1
                        if depth == 0:
                            end = i
                            break
                call = src[m.start():end + 1]
                if "start=" not in call:
                    bare.append("%s: %s" % (name, " ".join(call.split())[:90]))
        self.assertEqual(bare, [], "coverage measured from the head of the file: " + str(bare))

    def test_the_config_pre_pass_uses_the_scene_trim(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "agent_core.py"), encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn('start=_seconds_of(scene, "seedance_start_trim", "source_trim",', src)


if __name__ == "__main__":
    unittest.main()
