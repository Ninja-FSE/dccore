"""The Python installer pin was duplicated in prose and had no freshness
reminder (audit L47, #711).

start-dccore.bat pins PY_VERSION and two SHA-256 hashes; the pin fails safe
(a mismatch refuses to run the file) and is tested (the minor is tied to
the CI matrix, the hashes can be fetched and checked). What was missing:
nothing in the release workflow said to look at it, so a 3.x.y security
release lands and the launcher keeps installing the old one for as long
as nobody remembers; and docs/WINDOWS.md's sample transcript repeats the
literal number with nothing tying it to the launcher, so the first bump
leaves the guide naming a version the launcher no longer prints.

The release checklist has the step now; this test keeps the two numbers
equal and says which one was forgotten.
"""

import io
import os
import re
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tests.test_python_missing_help_do_not_fail import pins  # noqa: E402
from exported_tree import internal_file_or_skip  # noqa: E402


def read(*parts):
    with io.open(os.path.join(REPO_ROOT, *parts), encoding="utf-8") as handle:
        return handle.read()


class TheGuideAgreesWithTheLauncher(unittest.TestCase):

    def test_windows_md_names_the_pinned_version(self):
        version = pins()[0]
        guide = read("docs", "WINDOWS.md")
        named = re.findall(r"DCCore can download Python (\d+\.\d+\.\d+) from python\.org", guide)

        self.assertEqual(named, [version],
                         "WINDOWS.md's transcript says %s; start-dccore.bat pins %s - bump both" % (named, version))

    def test_no_other_prose_carries_a_three_part_python_version(self):
        """One number, in the launcher; the transcript quotes it. Anywhere
        else it would drift on its own.

        docs/PUBLIC-REPO-WORKFLOW.md is dev-only (export-ignored, #see
        exported_tree.py) and absent in an extracted public tree - checked
        only when it exists, same as the other two are checked regardless."""
        version = pins()[0]
        for relative in ("docs/INSTALL.md", "README.md", "docs/PUBLIC-REPO-WORKFLOW.md"):
            if not os.path.exists(os.path.join(REPO_ROOT, *relative.split("/"))):
                continue
            self.assertNotIn(version, read(*relative.split("/")), relative)


class TheReleaseChecklistHasTheStep(unittest.TestCase):
    """The checklist itself is dev-only and does not ship - see
    exported_tree.py - so this whole class is a no-op in an extracted
    public tree rather than a failure."""

    def test_it_names_the_pin_the_check_and_the_guide(self):
        path = internal_file_or_skip(self, os.path.join("docs", "PUBLIC-REPO-WORKFLOW.md"))
        with io.open(path, encoding="utf-8") as handle:
            workflow = handle.read()

        self.assertIn("**Check the Python pin in `scripts/windows/start-dccore.bat`**", workflow)
        self.assertIn("DCCORE_VERIFY_PYTHON_PIN=1", workflow)
        self.assertIn("tests/test_the_python_pin_has_one_home.py", workflow)
        self.assertLess(workflow.index("Check the Python pin"), workflow.index("**Tag the release**"))


if __name__ == "__main__":
    unittest.main()
