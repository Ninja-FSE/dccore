"""A filename the code actually opens has to exist.

WHY THIS EXISTS

#203 renamed setup.py to configure.py, because setup.py is the filename pip
executes for `pip install .`. It updated every reference in the tree at the
time, and CI was green.

#200 had merged shortly before and added a test that opened the old name. #203
branched before that test existed, so no amount of grepping on #203's branch
could have found it. Both PRs were green alone. Together they broke main - a
test raising FileNotFoundError, and, worse because nothing would have caught
it, a line in the pre-flight check telling the operator to run a file that is
no longer there.

Neither PR made a mistake. That is the shape of concurrent work, and the only
defence is a check that runs against the merge result.

WHY IT IS THIS NARROW

The first version flagged every mention of a <name>.py token anywhere. It found
the real breakage and also flagged: comments explaining the rename, the
changelog describing files by the names they had at the time, gitignored
files that are absent by design, fixture names tests write into temporary
directories, and the test that exists to assert setup.py is NOT there. Six
allowlist entries and growing - which is the "list of excuses" shape.

So it checks the pattern that actually breaks: a literal filename handed
straight to something that opens it. A name discussed in prose cannot raise
FileNotFoundError; a name passed to os.path.join can.
"""

import ast
import io
import os
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SKIP_DIRS = {".git", "__pycache__", ".claude", "node_modules"}

# Calls whose string arguments name a file that is about to be touched.
OPENERS = {"open", "exists", "isfile", "getsize", "join", "abspath", "realpath"}

# Absent by design. Deliberately short: anything longer means the check is
# pointed at the wrong thing.
MAY_BE_ABSENT = {
    "admin_config.py": "gitignored - the operator's own overrides, absent in "
                       "every clean checkout, and the code tests for it",
    "local_config.py": "the pre-#178 name, still named by the migration that "
                       "exists precisely because it is gone",
}


def python_files():
    for root, dirs, files in os.walk(REPO_ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            if name.endswith(".py"):
                yield os.path.join(root, name)


def present():
    return {os.path.basename(path) for path in python_files()}


def opened_filenames():
    """{filename: [where]} for every .py literal passed to a path call."""
    found = {}
    for path in python_files():
        relative = os.path.relpath(path, REPO_ROOT)
        try:
            with io.open(path, encoding="utf-8") as handle:
                tree = ast.parse(handle.read())
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (func.attr if isinstance(func, ast.Attribute)
                    else func.id if isinstance(func, ast.Name) else None)
            if name not in OPENERS:
                continue
            for arg in node.args:
                if (isinstance(arg, ast.Constant) and isinstance(arg.value, str)
                        and arg.value.endswith(".py")):
                    found.setdefault(os.path.basename(arg.value), []).append(
                        f"{relative}:{node.lineno}")
    return found


class EveryFilenameTheCodeOpensExists(unittest.TestCase):

    def test_no_opened_filename_is_missing(self):
        here = present()
        missing = {name: where for name, where in opened_filenames().items()
                   if name not in here and name not in MAY_BE_ABSENT}

        self.assertEqual(
            missing, {},
            "these filenames are handed to a path call but no such file "
            "exists - a rename that did not finish:\n  " +
            "\n  ".join(f"{name}: {sorted(w)}" for name, w in missing.items()))

    def test_the_allowlist_does_not_rot(self):
        """A name that comes back must leave the list, or the list quietly
        stops meaning what it says."""
        here = present()
        resurrected = sorted(name for name in MAY_BE_ABSENT if name in here)

        self.assertEqual(resurrected, [],
                         "these exist again and should not be excused any more")

    def test_the_scan_finds_the_calls_it_is_looking_for(self):
        """Control. A scan that matched nothing would pass on any tree."""
        found = opened_filenames()

        self.assertTrue(found, "no path call with a literal filename was found "
                               "anywhere, which cannot be right")

    def test_it_would_catch_the_break_that_motivated_it(self):
        """The #200/#203 collision, reconstructed: a literal handed to
        os.path.join for a file that is not there."""
        source = ('import os\n'
                  'io.open(os.path.join(REPO_ROOT, "vanished_module.py"))\n')
        tree = ast.parse(source)

        seen = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = (func.attr if isinstance(func, ast.Attribute)
                        else func.id if isinstance(func, ast.Name) else None)
                if name in OPENERS:
                    seen += [a.value for a in node.args
                             if isinstance(a, ast.Constant)
                             and isinstance(a.value, str)
                             and a.value.endswith(".py")]

        self.assertIn("vanished_module.py", seen)
        self.assertNotIn("vanished_module.py", present())

    def test_prose_about_a_renamed_file_is_not_flagged(self):
        """The reason this is narrow. A comment or a changelog entry naming
        the old file cannot raise FileNotFoundError, and treating it as an
        error is what made the first version unusable."""
        tree = ast.parse('# setup.py was renamed\nX = "setup.py is gone"\n')

        flagged = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]

        self.assertEqual(flagged, [])


if __name__ == "__main__":
    unittest.main()
