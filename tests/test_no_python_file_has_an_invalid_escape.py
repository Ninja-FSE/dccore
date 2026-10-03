r"""No Python file in the repository has an invalid escape sequence.

"\S" or "C:\Program Files" in an ordinary string is an invalid escape: Python
3.12 and later warn about it every time the file is compiled, and a later
Python makes it a SyntaxError - at which point the file does not load at all,
and a test file that does not load is a whole set of tests that silently stop
running. Five sat in test docstrings quoting a regex or a Windows path; they
are raw strings now (an r before the quotes), which say exactly what they show.

Compiled here with that warning turned into an error, over every .py file, so
the next one fails here rather than in somebody's future Python.
"""

import io
import os
import unittest
import warnings

from tests import support  # noqa: F401  (path setup)

REPO_ROOT = support.REPO_ROOT


def python_files():
    for folder, dirs, files in os.walk(REPO_ROOT):
        # .git and the like, and caches: not the project's own source.
        dirs[:] = [d for d in dirs if not d.startswith(".") and d != "__pycache__"]
        for name in files:
            if name.endswith(".py"):
                yield os.path.join(folder, name)


class EveryFileCompilesCleanly(unittest.TestCase):
    def test_no_invalid_escape_anywhere(self):
        found = []
        checked = 0
        for path in python_files():
            with io.open(path, encoding="utf-8", errors="replace") as handle:
                source = handle.read()
            checked += 1
            with warnings.catch_warnings():
                # SyntaxWarning from Python 3.12 on; DeprecationWarning in 3.10
                # and 3.11, which CI runs too. As errors, compile() raises them
                # as a SyntaxError.
                warnings.simplefilter("error", SyntaxWarning)
                warnings.simplefilter("error", DeprecationWarning)
                try:
                    compile(source, path, "exec")
                except (SyntaxError, SyntaxWarning, DeprecationWarning) as err:
                    line = getattr(err, "lineno", "?")
                    found.append(f"{os.path.relpath(path, REPO_ROOT)}:{line}: {err}")
        self.assertGreater(checked, 100, "the walk found too few files to mean anything")
        self.assertEqual(found, [], "\n".join(found))


if __name__ == "__main__":
    unittest.main()
