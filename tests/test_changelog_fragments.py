"""#1307: each PR writes its changelog entry to docs/changes/, and the release
roll joins them.

Every entry used to go at the top of `## Unreleased` in the two changelogs,
so any two open PRs conflicted there whatever their code. Now a PR writes
docs/changes/<issue>.md (and .public.md for a public bullet), and
scripts/roll_changelog.py writes them into both changelogs at release time.

Two halves: the fragments in this tree are well formed, so a release can be
rolled at any moment; and the roll, run on a scratch copy, does what
docs/changes/README.md says.
"""

import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))
try:
    import roll_changelog  # noqa: E402
finally:
    sys.path.remove(os.path.join(REPO_ROOT, "scripts"))

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from exported_tree import _this_is_an_export  # noqa: E402

# An extracted public tree has no docs/changes/ (export-ignored) and its one
# changelog is docs/UPDATES.md, the public one renamed (docs/PUBLIC-REPO-WORKFLOW.md,
# step 3): there is nothing there to roll or check (#1409).
EXPORT_SKIP = ("an extracted public tree has no docs/changes/ and no "
               "docs/UPDATES-PUBLIC.md to roll; checked in the development repository")


def read(path):
    with io.open(path, encoding="utf-8") as handle:
        return handle.read()


def write(path, text):
    with io.open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


class TheFragmentsInThisTree(unittest.TestCase):

    def test_every_one_can_be_rolled(self):
        fragments, problems = roll_changelog.load(REPO_ROOT)
        self.assertEqual(problems, [])

    def test_no_changelog_keeps_an_unreleased_section(self):
        """Between releases the folder is "Unreleased". An entry written into
        the changelog again would conflict with every other open PR."""
        if _this_is_an_export(self):
            self.skipTest(EXPORT_SKIP)
        self.assertEqual(roll_changelog.unreleased_problems(REPO_ROOT), [])

    def test_the_folder_does_not_ship(self):
        attributes = read(os.path.join(REPO_ROOT, ".gitattributes"))
        self.assertIn("docs/changes/** export-ignore", attributes.splitlines())


INTERNAL_HEAD = '# Log\n\nIntro.\n\n## \U0001F7E9 v1.0.0 (2026-01-01) - "Old"\n\n### Old entry (#1)\n\nOld text.\n'
PUBLIC_HEAD = "# Changelog\n\n## v1.0.0 — Old\n\n- **Old bullet.**\n"


class TheRoll(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="roll-")
        self.addCleanup(shutil.rmtree, self.root, True)
        os.makedirs(os.path.join(self.root, "docs", "changes"))
        write(os.path.join(self.root, roll_changelog.INTERNAL), INTERNAL_HEAD)
        write(os.path.join(self.root, roll_changelog.PUBLIC), PUBLIC_HEAD)
        write(self.changes("README.md"), "# The rules\n")
        self.said = []

    def changes(self, name):
        return os.path.join(self.root, "docs", "changes", name)

    def roll(self, version="v1.1.0", title="New", date="2026-10-12"):
        return roll_changelog.roll(version, title, date, repo=self.root, log=self.said.append)

    def internal(self):
        return read(os.path.join(self.root, roll_changelog.INTERNAL))

    def public(self):
        return read(os.path.join(self.root, roll_changelog.PUBLIC))

    def test_entries_go_under_the_new_heading_newest_issue_first(self):
        write(self.changes("1300.md"), "### Older (#1300)\n\nA.\n")
        write(self.changes("1310.md"), "### Newer (#1310)\n\nB.\n")
        write(self.changes("1310.public.md"), "- **Newer bullet.**\n")
        write(self.changes("1300.public.md"), "- **Older bullet.**\n")

        self.assertEqual(self.roll(), 0)

        self.assertEqual(self.internal(),
                         '# Log\n\nIntro.\n\n## \U0001F7E9 v1.1.0 (2026-10-12) - "New"\n\n'
                         "### Newer (#1310)\n\nB.\n\n### Older (#1300)\n\nA.\n\n"
                         '## \U0001F7E9 v1.0.0 (2026-01-01) - "Old"\n\n### Old entry (#1)\n\nOld text.\n')
        self.assertEqual(self.public(),
                         "# Changelog\n\n## v1.1.0 — New\n\n- **Newer bullet.**\n- **Older bullet.**\n\n"
                         "## v1.0.0 — Old\n\n- **Old bullet.**\n")

    def test_the_fragments_are_deleted_and_the_readme_kept(self):
        write(self.changes("1300.md"), "### Entry (#1300)\n\nA.\n")
        self.roll()
        self.assertEqual(os.listdir(os.path.join(self.root, "docs", "changes")), ["README.md"])

    def test_a_suffix_is_a_later_entry_of_the_same_issue(self):
        write(self.changes("1288.md"), "### Phase 1 (#1288)\n")
        write(self.changes("1288-phase2.md"), "### Phase 2 (#1288)\n")
        self.roll()
        text = self.internal()
        self.assertLess(text.index("Phase 2"), text.index("Phase 1"))

    def test_with_no_public_bullet_the_public_changelog_is_untouched(self):
        write(self.changes("1300.md"), "### Internal only (#1300)\n")
        self.roll()
        self.assertEqual(self.public(), PUBLIC_HEAD)

    def test_a_shell_comment_in_a_code_block_is_not_a_heading(self):
        write(self.changes("1300.md"), "### Entry (#1300)\n\n```\n# not a heading\n```\n")
        self.assertEqual(self.roll(), 0)

    def assert_refused_and_unchanged(self, says):
        self.assertEqual(self.roll(), 1)
        self.assertTrue(any(says in line for line in self.said), self.said)
        self.assertEqual((self.internal(), self.public()), (INTERNAL_HEAD, PUBLIC_HEAD))

    def test_a_malformed_fragment_stops_it_with_nothing_changed(self):
        cases = {
            "1300.md": ("Not a heading.\n", "starts with a '### ' heading"),
            "1301.md": ("### Fine\n\n## A level-two heading\n", "no '#' or '##' heading"),
            "1302.public.md": ("- **One.**\n- **Two.**\n", "one bullet on one line"),
            "1303.public.md": ("Plain text.\n", "starts with '- **'"),
            "1304.md": ("### Fine\n<<<<<<< HEAD\n", "merge conflict marker"),
            "notes.md": ("### Fine\n", "not a fragment name"),
            "1305.md": ("\n\n", "it is empty"),
        }
        for name, (text, says) in cases.items():
            with self.subTest(name=name):
                write(self.changes("1299.md"), "### Good (#1299)\n")
                write(self.changes(name), text)
                self.assert_refused_and_unchanged(says)
                self.assertTrue(os.path.exists(self.changes("1299.md")), "a good fragment was deleted")
                os.remove(self.changes(name))

    def test_a_bad_version_title_or_date_stops_it(self):
        write(self.changes("1300.md"), "### Entry (#1300)\n")
        for kwargs, says in (({"version": "1.1.0"}, "not a version"), ({"title": " "}, "needs a title"),
                             ({"date": "12/10/2026"}, "not a date")):
            with self.subTest(**kwargs):
                self.said = []
                self.assertEqual(self.roll(**kwargs), 1)
                self.assertTrue(any(says in line for line in self.said), self.said)
                self.assertEqual(self.internal(), INTERNAL_HEAD)

    def test_an_unreleased_section_left_in_a_changelog_stops_it(self):
        write(os.path.join(self.root, roll_changelog.PUBLIC),
              PUBLIC_HEAD.replace("## v1.0.0", "## Unreleased\n\n- **Stray.**\n\n## v1.0.0"))
        write(self.changes("1300.md"), "### Entry (#1300)\n")
        self.assertEqual(self.roll(), 1)
        self.assertTrue(any("Unreleased" in line for line in self.said), self.said)
        self.assertTrue(os.path.exists(self.changes("1300.md")))

    def test_nothing_to_roll_is_said_not_written(self):
        self.assertEqual(self.roll(), 1)
        self.assertTrue(any("nothing to roll" in line for line in self.said), self.said)
        self.assertEqual(self.internal(), INTERNAL_HEAD)


class TheCommandLine(unittest.TestCase):

    def test_check_on_this_tree_says_all_is_well(self):
        if _this_is_an_export(self):
            self.skipTest(EXPORT_SKIP)
        result = subprocess.run([sys.executable, os.path.join(REPO_ROOT, "scripts", "roll_changelog.py"), "--check"],
                                capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("all well formed", result.stdout)


if __name__ == "__main__":
    unittest.main()
