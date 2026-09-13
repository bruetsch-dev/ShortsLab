"""No source file may contain control characters.

Twice on 2026-08-29 a shell heredoc collapsed the backslash in a Python escape while writing
source: `\b` reached the file as a literal backspace (0x08). The damage is invisible in an
editor and silent at runtime - `re.search(r"<BS>one continuous<BS>", text)` compiles fine and
simply never matches, so `_is_compound_evidence` lost two of its three branches and the tests
still passed because a different branch happened to cover the example.

Bytes below 0x20 have no business in this codebase outside newline and tab.
"""

import glob
import os
import unittest

ALLOWED = {"\n", "\t"}
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class ControlCharacterTests(unittest.TestCase):
    def files(self):
        for pattern in ("*.py", os.path.join("tests", "*.py")):
            for path in glob.glob(os.path.join(ROOT, pattern)):
                yield path

    def test_no_python_source_contains_a_control_character(self):
        offenders = []
        for path in self.files():
            try:
                text = open(path, encoding="utf-8").read()
            except (OSError, UnicodeDecodeError):
                continue
            for index, char in enumerate(text):
                if ord(char) < 32 and char not in ALLOWED:
                    line = text.count("\n", 0, index) + 1
                    offenders.append(f"{os.path.basename(path)}:{line} chr({ord(char)})")
                    break
        self.assertEqual(offenders, [], "control characters in source: " + ", ".join(offenders))


if __name__ == "__main__":
    unittest.main()
