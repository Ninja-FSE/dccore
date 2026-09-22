"""The test count differed between README (4994) and FUTURE.md (5237), and
neither matched the tree (audit L34, #698).

Two sentences in two files said the same number and drifted independently;
a reader comparing them saw a project that could not count its own tests
and discounted the rest of the numbers. The count lives in one place now -
FUTURE.md's Quality section - and README's Tests section points there. The
guard reads the shipped prose for any other four-or-more-digit "N tests"
claim, and refuses a FUTURE.md figure more than a tenth off what the
loader discovers, so a release roll that forgets the line fails the suite
while an ordinary PR adding a few tests does not have to touch it (the
release workflow says why: it is measured once, from the merged tree).
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

from exported_tree import internal_file_or_skip  # noqa: E402

SHIPPED = ["README.md", "docs/INSTALL.md", "docs/WINDOWS.md", "docs/MACOS.md", "docs/ADMIN-CONSOLE.md",
           "docs/CONVENTIONS.md", "docs/FUTURE.md"]
COUNT_CLAIM = re.compile(r"\b(\d{4,})\**\s+(?:tests|of them)\b")


def read(relative):
    path = os.path.join(REPO_ROOT, relative)
    if not os.path.exists(path):
        return ""
    with io.open(path, encoding="utf-8") as handle:
        return handle.read()


def discovered():
    loader = unittest.TestLoader()
    suite = loader.discover(os.path.join(REPO_ROOT, "tests"), top_level_dir=REPO_ROOT)

    def count(node):
        return sum(count(t) if isinstance(t, unittest.TestSuite) else 1 for t in node)
    return count(suite)


class OneNumberOnePlace(unittest.TestCase):

    def test_only_the_roadmap_carries_a_count(self):
        where = {relative: COUNT_CLAIM.findall(read(relative)) for relative in SHIPPED}
        carrying = sorted(relative for relative, found in where.items() if found)

        self.assertEqual(carrying, ["docs/FUTURE.md"], where)
        self.assertEqual(len(where["docs/FUTURE.md"]), 1)

    def test_the_readme_points_at_it(self):
        readme = read("README.md")

        self.assertIn("the count is kept in [docs/FUTURE.md](docs/FUTURE.md)", readme)

    def test_the_roadmaps_figure_is_near_the_tree(self):
        """A tenth: an ordinary PR need not touch the line; a release roll
        that forgot it cannot pass."""
        claimed = int(COUNT_CLAIM.search(read("docs/FUTURE.md")).group(1))
        actual = discovered()

        self.assertLessEqual(abs(claimed - actual), actual // 10,
                             "FUTURE.md says %d tests; the loader finds %d" % (claimed, actual))

    def test_the_release_workflow_names_the_one_place(self):
        """docs/PUBLIC-REPO-WORKFLOW.md is dev-only and does not ship - see
        exported_tree.py - so this is a no-op in an extracted public tree."""
        path = internal_file_or_skip(self, "docs/PUBLIC-REPO-WORKFLOW.md")
        with io.open(path, encoding="utf-8") as handle:
            workflow = handle.read()

        self.assertIn("the one place it lives", workflow)
        self.assertNotIn("AND in `README.md`'s own Tests", workflow)


if __name__ == "__main__":
    unittest.main()
