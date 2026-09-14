"""A file called internal has to actually be excluded from the export.

WHAT WENT WRONG

`docs/UPDATES.md` is the full internal changelog - 50KB, written against this
repository's own PR and issue numbers and branch names, none of which resolve
anywhere outside it. #207 created `docs/UPDATES-PUBLIC.md` as the version that
ships and said the internal one "stays internal-only".

Nothing made that true. `git archive` is how the public tree gets extracted,
and `export-ignore` is the only thing it honours - without a rule, a file is
internal by intention and public in fact. The internal changelog was in the
archive, and it still named a real channel.

The final pre-extraction identity sweep did not catch it, because it read the
code and the docs for identities rather than asking which files leave the
building at all.

WHY A TEST RATHER THAN JUST THE RULE

An export-ignore rule is one line in a file nobody opens, and it fails
silently: the archive simply contains a bit more than intended, and nothing
says so. This asks `git archive` itself.

WHY IT SKIPS IN AN EXTRACTED TREE

This asks `git archive HEAD` about the repository IT IS RUNNING IN - and this
file ships, so it runs again inside every public checkout too, where that
question is a different one: `docs/UPDATES.md` there is the renamed public
changelog rather than the internal file INTERNAL names, so checking it
against the same list would flag the public repo's own real changelog as a
leak, on every clone, forever. See tests/exported_tree.py for the signal
used to tell the two repositories apart.
"""

import os
import subprocess
import sys
import tarfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from exported_tree import _this_is_an_export  # noqa: E402

# Files that exist to be read by the two of us and must not reach the public
# tree. Each needs an export-ignore rule in .gitattributes.
INTERNAL = ("docs/UPDATES.md", "docs/PUBLIC-REPO-WORKFLOW.md")


def archive_members():
    """Every path `git archive HEAD` would produce, or None if git cannot run."""
    try:
        result = subprocess.run(
            ["git", "archive", "--format=tar", "HEAD"],
            cwd=REPO_ROOT, capture_output=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0 or not result.stdout:
        return None

    import io as _io
    with tarfile.open(fileobj=_io.BytesIO(result.stdout)) as archive:
        return {name.replace("\\", "/") for name in archive.getnames()}


class TheExportLeavesInternalFilesBehind(unittest.TestCase):

    def setUp(self):
        if _this_is_an_export(self):
            self.skipTest("this is an extracted public tree - see "
                          "tests/exported_tree.py")
        self.members = archive_members()
        if self.members is None:
            self.skipTest("git archive is not available here")

    def test_no_internal_file_is_in_the_archive(self):
        leaked = sorted(name for name in INTERNAL if name in self.members)

        self.assertEqual(
            leaked, [],
            "these are marked internal but `git archive` includes them, which "
            "is how the public tree is extracted:\n  " + "\n  ".join(leaked) +
            "\n\nAdd an `export-ignore` rule for each in .gitattributes.")

    def test_the_public_changelog_does_ship(self):
        """The other half. Excluding the internal one is only correct because
        a public one exists - dropping both would leave the published repo with
        no changelog at all."""
        self.assertIn("docs/UPDATES-PUBLIC.md", self.members)

    def test_the_archive_is_not_empty(self):
        """Control. An archive that produced nothing would satisfy the
        exclusion above on any repository, forever."""
        self.assertGreater(len(self.members), 50)
        self.assertIn("oserve.py", self.members)


class TheRulesAreWrittenDown(unittest.TestCase):
    """export-ignore is honoured from the committed .gitattributes, so a rule
    that only exists in the working tree protects nothing on someone else's
    clone."""

    def test_every_internal_file_has_a_rule(self):
        import io

        if _this_is_an_export(self):
            self.skipTest("this is an extracted public tree - see "
                          "tests/exported_tree.py")

        with io.open(os.path.join(REPO_ROOT, ".gitattributes"),
                     encoding="utf-8") as handle:
            rules = [line.split("#", 1)[0].strip()
                     for line in handle if "export-ignore" in line.split("#", 1)[0]]

        for name in INTERNAL:
            with self.subTest(path=name):
                self.assertTrue(
                    any(name in rule for rule in rules),
                    f"{name} is listed as internal here but has no "
                    f"export-ignore rule in .gitattributes")


if __name__ == "__main__":
    unittest.main()
