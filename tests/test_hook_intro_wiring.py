"""The hook intro is spoken, so it must be spoken by the short's own narrator.

It is built inside rebuild_from_disk - the editor's Rebuild button - which is handed nothing but
a project directory. Reading the hook settings from anywhere else was a NameError that took the
whole Rebuild down, and leaving the voice out let the TTS pick its default, so the intro arrived
in a stranger's voice in front of the short.
"""

import ast
import unittest
from pathlib import Path

import longform_video as lv

ROOT = Path(lv.__file__).resolve().parent


def _function(name):
    tree = ast.parse(ROOT.joinpath("longform_video.py").read_text(encoding="utf-8"))
    return next(n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == name)


class RebuildScopeTests(unittest.TestCase):
    def test_the_rebuild_resolves_every_name_it_uses(self):
        """The exact defect: `if hook_intro:` in a function that has no such variable."""
        fn = _function("rebuild_from_disk")
        bound = {t.id for t in ast.walk(fn)
                 if isinstance(t, ast.Name) and isinstance(t.ctx, ast.Store)}
        bound |= {a.arg for a in fn.args.args}
        bound |= {h.name for h in ast.walk(fn) if isinstance(h, ast.ExceptHandler) and h.name}
        bound |= {a.asname or a.name.split(".")[0]
                  for i in ast.walk(fn) if isinstance(i, ast.Import) for a in i.names}
        import builtins
        module = set(dir(lv)) | set(dir(builtins))
        used = {t.id for t in ast.walk(fn)
                if isinstance(t, ast.Name) and isinstance(t.ctx, ast.Load)}
        self.assertEqual(sorted(used - bound - module), [])

    def test_the_hook_settings_come_from_the_project_state(self):
        source = ROOT.joinpath("longform_video.py").read_text(encoding="utf-8")
        self.assertIn('if bool(state.get("hook_intro")) and not str(rendered).endswith', source)
        self.assertIn('str(state.get("hook_text") or "")', source)

    def test_the_intro_is_read_by_the_project_voice(self):
        source = ROOT.joinpath("longform_video.py").read_text(encoding="utf-8")
        block = source[source.index('if bool(state.get("hook_intro")) and not str(rendered).endswith'):]
        block = block[:block.index("save_state(")]
        # There are now TWO openers - the built-in clip and a user-generated one - and both must
        # be read by the same narrator, so the settings live in one dict passed to whichever
        # builder runs. Assert on the settings and on the fact that both calls receive them.
        self.assertIn('"voice": str(state.get("voice") or "") or None', block,
                      "the intro would be spoken by the TTS default voice")
        self.assertIn('"tts_model": str(state.get("tts_model") or "") or None', block)
        for call in ("voice_over_opener(", "build_hook_intro("):
            tail = block[block.index(call):]
            self.assertIn("**voice_kw", tail[:tail.index(")\n")],
                          f"{call} would use the default voice")

    def test_the_joined_file_does_not_overwrite_an_earlier_render(self):
        """Rebuild versions its MP4 on purpose; the hooked copy has to follow that version."""
        source = ROOT.joinpath("longform_video.py").read_text(encoding="utf-8")
        self.assertIn('joined = out.with_name(f"{out.stem}_with_hook.mp4")', source)

    def test_the_short_survives_a_broken_intro(self):
        source = ROOT.joinpath("longform_video.py").read_text(encoding="utf-8")
        block = source[source.index('if bool(state.get("hook_intro")) and not str(rendered).endswith'):]
        self.assertIn("Hook intro skipped", block[:block.index("save_state(")])


class VoicePassThroughTests(unittest.TestCase):
    def test_the_builder_asks_for_the_voice_it_is_given(self):
        import inspect

        import sketch_hook_intro as hi
        source = inspect.getsource(hi._speak_hook)
        self.assertIn("voice=voice or None", source)
        self.assertIn("model=tts_model or pipeline.DEFAULT_TTS_MODEL", source)


if __name__ == "__main__":
    unittest.main()
